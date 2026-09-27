"""Bridge reviewed AskNews summaries into Council evidence.

Article selection is explicit: successful search is not a relevance judgment.
Summaries retain their provenance and must not be treated as publisher quotes.
"""
from datetime import datetime
from urllib.parse import urlsplit
from .analysis import deduplicate
from .models import Evidence, aware


def prepare_evidence(record, question, selected_ids):
    if record.get('provider') != 'AskNews' or record.get('status') != 'ok':
        raise ValueError('A successful AskNews record is required')
    available = aware(datetime.fromisoformat(record['completed_at']))
    if available > question.as_of:
        raise ValueError('Research was unavailable at forecast time')
    if not selected_ids or len(selected_ids) > 10 or len(set(selected_ids)) != len(selected_ids):
        raise ValueError('Select 1-10 unique reviewed article IDs')
    articles = record['response']['as_dicts']
    by_id = {}
    for article in articles:
        identity = article.get('article_id')
        if not identity or identity in by_id:
            raise ValueError('Missing or duplicate article identity')
        by_id[identity] = article
    evidence = []
    for identity in selected_ids:
        if identity not in by_id:
            raise ValueError('Selected article is absent from research')
        article = by_id[identity]
        url = article.get('article_url', '')
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            raise ValueError('Invalid publisher URL')
        summary = article.get('summary')
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError('Missing summary')
        published = aware(datetime.fromisoformat(article['pub_date'])) if article.get('pub_date') else None
        evidence.append(Evidence(
            id='asknews-'+identity,
            text='AskNews-generated summary; not a verified publisher quotation.\n'+summary,
            source=url, published_at=published, available_at=available,
            primary=False, direction='neutral', origin_id='asknews-'+identity))
    return deduplicate(evidence, question)


async def forecast_from_research(council, question, record, selected_ids, **signals):
    evidence = prepare_evidence(record, question, selected_ids)
    # Validation runs before any forecasting model call.
    return await council.forecast(question, evidence=evidence, **signals)


async def forecast_automatically(council, question, record, **signals):
    if not council.select_evidence:
        raise ValueError('Automatic pipeline requires the evidence selector')
    candidates = [article['article_id'] for article in record.get('response', {}).get('as_dicts', [])]
    evidence = prepare_evidence(record, question, candidates)
    return await council.forecast(question, evidence=evidence, **signals)
