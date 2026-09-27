"""Fetch and persist AskNews context without invoking forecasting models.

Use ASKNEWS_API_KEY from the process environment. Outputs contain provider
summaries, not verified verbatim quotations from the original publishers.
"""
import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


async def research(client, query, output, *, n_articles=4, required_terms=(), languages=(),
                   strategy='latest news'):
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise ValueError('Supply a nonempty query up to 2000 characters')
    if type(n_articles) is not int or not 1 <= n_articles <= 10:
        raise ValueError('Request between 1 and 10 articles')
    if strategy not in ('latest news', 'news knowledge'):
        raise ValueError('Unsupported research strategy')
    if any(not isinstance(x, str) or not x.strip() for x in (*required_terms, *languages)):
        raise ValueError('Filters must contain nonempty strings')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    run = output / (uuid4().hex + '.json')
    record = dict(query=query, started_at=datetime.now(timezone.utc).isoformat(),
                  provider='AskNews', status='started', n_articles=n_articles,
                  content_type='provider_summaries', actual_cost=None, strategy=strategy)
    filters = {}
    if required_terms:
        filters.update(string_guarantee=list(required_terms), string_guarantee_op='AND')
    if languages:
        filters['languages'] = list(languages)
    record['filters'] = filters
    # Preserve the attempt even on timeouts; do not automatically retry.
    run.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
    try:
        response = await asyncio.wait_for(client.news.search_news(
            query=query, n_articles=n_articles, return_type='both',
            strategy=strategy, **filters), timeout=60)
        data = response.model_dump(mode='json')
        if not data.get('as_dicts'):
            record['status'] = 'empty'
        else:
            record['status'] = 'ok'
        record['response'] = data
    except Exception as exc:
        record.update(status='failed', error=type(exc).__name__)
    record['completed_at'] = datetime.now(timezone.utc).isoformat()
    run.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    return record, run


async def research_question(client, question, output):
    """Two predetermined searches, independent of any forecast or its probability.

    Latest developments plus criteria-focused background. Preserve raw responses
    and a merged manifest. Query shortening never alters the forecast criteria.
    """
    query = (question.text[:900]+'\nResolution conditions: '+question.criteria[:1050])[:2000]
    results = await asyncio.gather(
        research(client, question.text[:2000], output, n_articles=5),
        research(client, query, output, n_articles=5, strategy='news knowledge'))
    articles, seen, searches = [], set(), []
    for record, path in results:
        searches.append({'file':path.name, 'strategy':record['strategy'], 'status':record['status']})
        for article in record.get('response', {}).get('as_dicts') or []:
            identity = article.get('article_id')
            if not identity:
                raise ValueError('Research article lacks identity')
            if identity not in seen:
                seen.add(identity)
                articles.append(article)
    record = {'provider':'AskNews', 'status':'ok' if articles else
              ('failed' if any(r['status']=='failed' for r,_ in results) else 'empty'),
              'completed_at':datetime.now(timezone.utc).isoformat(),
              'content_type':'provider_summaries', 'actual_cost':None,
              'plan_version':'latest-and-criteria-v1', 'searches':searches,
              'partial':any(r['status']=='failed' for r,_ in results),
              'response':{'as_dicts':articles}}
    path = Path(output)/(uuid4().hex+'-combined.json')
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    return record, path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('query')
    parser.add_argument('--output', type=Path, default=Path('work/asknews-research'))
    parser.add_argument('--credential-file', type=Path)
    parser.add_argument('--required-term', action='append', default=[])
    parser.add_argument('--language', action='append', default=[])
    args = parser.parse_args()
    key = os.environ.get('ASKNEWS_API_KEY')
    if not key and args.credential_file:
        from .local_credentials import unprotect
        key = unprotect(args.credential_file.read_text(encoding='utf-8'))
    if not key:
        parser.exit(2, 'ASKNEWS_API_KEY is not configured. No request made.\n')
    from asknews_sdk import AsyncAskNewsSDK
    async def run():
        async with AsyncAskNewsSDK(api_key=key, retries=0, timeout=45, follow_redirects=False) as client:
            return await research(client, args.query, args.output,
                                  required_terms=args.required_term, languages=args.language)
    record, path = asyncio.run(run())
    print(f"AskNews research: {record['status']}; saved: {path}")
    if record['status'] == 'failed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
