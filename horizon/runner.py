"""Council entry point. No network or paid calls unless --run is supplied."""
import argparse
import asyncio
import json
import os
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4

from .asknews_evidence import forecast_automatically
from .asknews_research import research_question
from .budget import Budget
from .council import Council
from .memory import Memory, encode
from .models import Question
from .storage import connect


def now():
    return datetime.now(timezone.utc)


def load_local_metaculus(path=Path('work/metaculus.dpapi')):
    if not os.environ.get('METACULUS_TOKEN') and path.exists():
        from .local_credentials import unprotect
        os.environ['METACULUS_TOKEN'] = unprotect(path.read_text(encoding='utf-8'))


def load_local_openrouter(path=Path('work/openrouter.dpapi')):
    if not os.environ.get('OPENROUTER_API_KEY') and path.exists():
        from .local_credentials import unprotect
        os.environ['OPENROUTER_API_KEY'] = unprotect(path.read_text(encoding='utf-8'))


def date(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def question_from_post(post, timestamp):
    q = post['question']
    if q['type'] not in ('binary', 'numeric', 'discrete') or q['status'] != 'open':
        raise ValueError('Unsupported or closed question')
    if q['type'] != 'binary':
        from .numeric import specification
        specification(q)
    close = date(q['scheduled_close_time'])
    deadline = date(q.get('scheduled_resolve_time') or q['scheduled_close_time'])
    return Question(str(q['id']), q['title'], q['resolution_criteria'], timestamp,
                    deadline, kind=q['type'], background=q.get('description') or '',
                    fine_print=q.get('fine_print') or '', close_time=close)


def has_forecast(post):
    q = post['question']
    # Missing authenticated forecast state is not equivalent to no forecast.
    if 'my_forecasts' not in q:
        raise ValueError('Authenticated forecast state missing')
    return bool((q['my_forecasts'] or {}).get('latest'))


class Ledger:
    """Persist attempts; retry only transient research failures before model inference."""
    def __init__(self, path):
        self.path = str(path)
        with connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS submissions '
                       '(question TEXT PRIMARY KEY, state TEXT NOT NULL, detail TEXT)')
            columns = {r[1] for r in db.execute('PRAGMA table_info(submissions)')}
            for name, definition in (('attempts', 'INTEGER NOT NULL DEFAULT 1'),
                                     ('updated_at', 'TEXT')):
                if name not in columns:
                    db.execute(f'ALTER TABLE submissions ADD COLUMN {name} {definition}')
            db.execute('CREATE TABLE IF NOT EXISTS submission_history '
                       '(question TEXT, state TEXT, detail TEXT, recorded_at TEXT)')

    def claim(self, question_id):
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state, attempts, updated_at FROM submissions WHERE question=?',
                             (question_id,)).fetchone()
            timestamp = now().isoformat()
            if row is None:
                db.execute('INSERT INTO submissions (question,state,detail,attempts,updated_at) '
                           'VALUES (?,?,?,1,?)', (question_id, 'started', '', timestamp))
                return True
            if row[0] != 'retryable_research_failure' or row[1] >= 3:
                return False
            if row[2] and (now()-date(row[2])).total_seconds() < 300:
                return False
            db.execute('UPDATE submissions SET state=?, detail=?, attempts=attempts+1, updated_at=? '
                       'WHERE question=?', ('started', '', timestamp, question_id))
            return True

    def set(self, question_id, state, detail=''):
        with connect(self.path) as db:
            timestamp = now().isoformat()
            db.execute('INSERT INTO submission_history VALUES (?,?,?,?)',
                       (question_id, state, detail, timestamp))
            db.execute('INSERT INTO submissions (question,state,detail,attempts,updated_at) '
                       'VALUES (?,?,?,1,?) ON CONFLICT(question) DO UPDATE SET '
                       'state=excluded.state,detail=excluded.detail,updated_at=excluded.updated_at',
                       (question_id, state, detail, timestamp))

    def totals(self):
        with connect(self.path) as db:
            states = dict(db.execute('SELECT state,COUNT(*) FROM submissions GROUP BY state'))
        accepted = sum(states.get(s, 0) for s in
                       ('submitted', 'already_forecast', 'forecast_accepted_comment_pending'))
        return {'tracked_questions': sum(states.values()), 'accepted_forecasts': accepted,
                'states': states}

    def result(self, question_id):
        with connect(self.path) as db:
            row = db.execute('SELECT state, detail, attempts FROM submissions WHERE question=?', (question_id,)).fetchone()
        if not row:
            return {}
        try:
            detail = json.loads(row[1])
        except (ValueError, TypeError):
            detail = {}
        if not isinstance(detail, dict):
            detail = {}
        return {'decision': row[0], 'decision_codes': detail.get('decision_codes', []),
                'attempts': row[2], 'retry_exhausted': row[0] == 'retryable_research_failure' and row[2] >= 3}


class Metaculus:
    def __init__(self, client):
        self.client = client

    async def posts(self, tournament):
        offset = 0
        while True:
            response = await self.client.get('/api/posts/', params={
                'tournaments': tournament, 'statuses': 'open',
                'include_description': 'true',
                'limit': 100, 'offset': offset, 'order_by': 'id'})
            response.raise_for_status()
            rows = response.json()['results']
            for post in rows:
                if post.get('question') and post['question'].get('type') in ('binary', 'numeric', 'discrete'):
                    yield post
            if len(rows) < 100:
                break
            offset += len(rows)

    async def detail(self, post_id):
        response = await self.client.get(f'/api/posts/{int(post_id)}/')
        response.raise_for_status()
        return response.json()

    async def submit_numeric(self, question_id, value):
        response = await self.client.post('/api/questions/forecast/', json=[{
            'question': int(question_id), 'source': 'api', 'probability_yes': None,
            'probability_yes_per_category': None, 'continuous_cdf': value}])
        response.raise_for_status()

    async def submit(self, question_id, value):
        response = await self.client.post('/api/questions/forecast/', json=[{
            'question': int(question_id), 'source': 'api', 'probability_yes': value,
            'probability_yes_per_category': None, 'continuous_cdf': None}])
        response.raise_for_status()

    async def comment(self, post_id, text):
        response = await self.client.post('/api/comments/create/', json={
            'on_post': int(post_id), 'text': text, 'parent': None,
            'included_forecast': True, 'is_private': True})
        response.raise_for_status()


def rationale(record):
    lines = [f'Horizon Council: {record.calibrated:.2%}',
             'Aggregation: weighted log-odds pool; calibration: '+record.calibration_version]
    for agent in record.agents:
        lines += [f'\n{agent.agent} ({agent.model}): {agent.probability:.2%}',
                  *agent.drivers, 'Counterargument: '+agent.counterargument,
                  'Crux: '+agent.crux]
    lines += ['\nSources (AskNews summaries, not verified publisher quotations):']
    lines += [e.source for e in record.evidence]
    return '\n'.join(lines)


def failure_state(exc, state, phase):
    if state != 'started':
        return state  # Never retry an uncertain or accepted submission.
    import httpx
    status = getattr(getattr(exc, 'response', None), 'status_code', None)
    transient = isinstance(exc, (TimeoutError, httpx.TransportError)) or status in (429, 502, 503, 504)
    return 'retryable_research_failure' if phase == 'research' and transient else 'failed'


async def process(post, api, council, fetch_research, ledger, *, publish=False, clock=now):
    if post['question']['type'] in ('numeric', 'discrete'):
        return await process_numeric(post, api, council, fetch_research, ledger, publish=publish, clock=clock)
    question_id = str(post['question']['id'])
    if has_forecast(post):
        ledger.set(str(post['question']['id']), 'already_forecast')
        return 'already_forecast'
    question = question_from_post(post, clock())
    # Do not regenerate predictions based on their probability or review outcome.
    if not ledger.claim(question_id):
        return 'already_attempted'
    state = 'started'
    phase = 'research'
    try:
        cutoff = question.close_time-timedelta(seconds=60)
        if clock() >= cutoff:
            ledger.set(question_id, 'deadline')
            return 'deadline'
        source = await asyncio.wait_for(fetch_research(question),
                                        min(60, (cutoff-clock()).total_seconds()))
        phase = 'forecast'
        # Evidence is available at forecast time, after research completes.
        question = replace(question, as_of=clock(), close_time=cutoff)
        record = await forecast_automatically(council, question, source)
        if record.status != 'ok':
            final = 'abstained_review' if record.status == 'review' and council.auto_resolve else record.status
            ledger.set(question_id, final, json.dumps({'decision_codes': record.decision_codes}))
            return final  # Terminal automatic decision, no human probability editing.
        if not publish:
            ledger.set(question_id, 'dry_run')
            return 'dry_run'
        latest = await api.detail(post['id'])
        refreshed = question_from_post(latest, clock())
        if has_forecast(latest):
            ledger.set(question_id, 'already_forecast')
            return 'already_forecast'
        if refreshed.id != question.id or refreshed.criteria_hash != question.criteria_hash:
            raise ValueError('Question changed during forecasting')
        if clock() >= min(cutoff, refreshed.close_time-timedelta(seconds=60)):
            ledger.set(question_id, 'deadline')
            return 'deadline'
        # Mark before sending: even a timeout after acceptance cannot resend.
        state = 'submission_uncertain'
        ledger.set(question_id, state)
        await api.submit(question_id, record.calibrated)
        state = 'forecast_accepted_comment_pending'
        ledger.set(question_id, state)
        await api.comment(post['id'], rationale(record))
        ledger.set(question_id, 'submitted')
        return 'submitted'
    except Exception as exc:
        final = failure_state(exc, state, phase)
        ledger.set(question_id, final, json.dumps({'decision_codes': [type(exc).__name__], 'phase': phase}))
        return final


async def process_numeric(post, api, council, fetch_research, ledger, *, publish=False, clock=now):
    from .numeric import forecast, specification
    question = question_from_post(post, clock())
    if has_forecast(post):
        ledger.set(str(post['question']['id']), 'already_forecast')
        return 'already_forecast'
    if not ledger.claim(question.id):
        return 'already_attempted'
    state = 'started'
    phase = 'research'
    try:
        cutoff = question.close_time-timedelta(seconds=60)
        if clock() >= cutoff:
            ledger.set(question.id, 'deadline')
            return 'deadline'
        source = await asyncio.wait_for(fetch_research(question), min(60,(cutoff-clock()).total_seconds()))
        phase = 'forecast'
        question = replace(question, as_of=clock(), close_time=cutoff)
        cdf, explanation = await forecast(council, question, post['question'], source)
        if not publish:
            ledger.set(question.id, 'dry_run')
            return 'dry_run'
        latest = await api.detail(post['id'])
        refreshed = question_from_post(latest, clock())
        if has_forecast(latest):
            ledger.set(question.id, 'already_forecast')
            return 'already_forecast'
        if (refreshed.id != question.id or refreshed.kind != question.kind
                or refreshed.criteria_hash != question.criteria_hash
                or specification(latest['question']) != specification(post['question'])):
            raise ValueError('Question changed during forecasting')
        if clock() >= min(cutoff,refreshed.close_time-timedelta(seconds=60)):
            ledger.set(question.id, 'deadline')
            return 'deadline'
        state = 'submission_uncertain'
        ledger.set(question.id,state)
        await api.submit_numeric(question.id,cdf)
        state = 'forecast_accepted_comment_pending'
        ledger.set(question.id,state)
        await api.comment(post['id'],explanation)
        ledger.set(question.id,'submitted')
        return 'submitted'
    except Exception as exc:
        state = failure_state(exc, state, phase)
        ledger.set(question.id,state,json.dumps({'decision_codes':[type(exc).__name__], 'phase': phase}))
        return state


def preflight(config, credential_file):
    problems = []
    provider_name = 'openai'
    if config and config.exists():
        try:
            provider_name = json.loads(config.read_text(encoding='utf-8')).get('provider','openai')
            if provider_name not in ('openai','openrouter'):
                problems.append('Unsupported model provider')
            else:
                # Validate configuration without creating a client or spending.
                from .openrouter_provider import OpenRouterProvider
                settings = json.loads(config.read_text(encoding='utf-8'))
                settings.pop('provider', None)
                if provider_name == 'openai':
                    settings['model'] = 'openai/'+settings['model']
                checker = OpenRouterProvider(approved=True, client=object(), **settings)
                if checker.quote_eur('') > 0.05:
                    problems.append('Minimum model reservation exceeds EUR 0.05 question budget')
                elif checker.quote_eur('')*6 > 0.05:
                    problems.append('Six minimum Council reservations exceed EUR 0.05 question budget')
                elif checker.quote_eur('x'*checker.max_input_bytes) > 0.05:
                    problems.append('Maximum-size model call exceeds EUR 0.05 question budget')
        except (ValueError, AttributeError, TypeError, KeyError):
            problems.append('Invalid model configuration')
    model_key = 'OPENROUTER_API_KEY' if provider_name == 'openrouter' else 'OPENAI_API_KEY'
    for key in ('METACULUS_TOKEN', model_key):
        if not os.environ.get(key) or os.environ.get(key) == 'REPLACE_ME':
            problems.append(key+' missing')
    if not os.environ.get('ASKNEWS_API_KEY') and not credential_file.exists():
        problems.append('AskNews credential missing')
    if not config or not config.exists():
        problems.append('Verified model/pricing configuration missing')
    return problems


async def run(args):
    import httpx
    from asknews_sdk import AsyncAskNewsSDK
    from .openai_provider import OpenAIProvider
    from .local_credentials import unprotect
    config = json.loads(args.config.read_text(encoding='utf-8'))
    provider_name = config.pop('provider','openai')
    if provider_name == 'openrouter':
        from .openrouter_provider import OpenRouterProvider
        provider = OpenRouterProvider(approved=True, **config)
    else:
        provider = OpenAIProvider(approved=True, **config)
    args.state.mkdir(parents=True, exist_ok=True)
    council = Council(provider, Budget(args.state/'budget.sqlite'),
                      Memory(args.state/'memory.sqlite'), uuid4().hex,
                      select_evidence=True, analyze_resolution=True, auto_resolve=True)
    key = os.environ.get('ASKNEWS_API_KEY') or unprotect(args.credential_file.read_text())
    ledger = Ledger(args.state/('submissions.sqlite' if args.publish else 'dry-runs.sqlite'))
    summary = []
    async with httpx.AsyncClient(base_url='https://www.metaculus.com', timeout=30,
                                follow_redirects=False, headers={
                                    'Authorization': 'Token '+os.environ['METACULUS_TOKEN']}) as transport:
        api = Metaculus(transport)
        async with AsyncAskNewsSDK(api_key=key, retries=0, timeout=45,
                                   follow_redirects=False) as news:
            async def fetch(question):
                result, _ = await research_question(news, question, args.state/'research')
                return result
            async for post in api.posts(args.tournament):
                try:
                    detail = await api.detail(post['id'])
                    status = await process(detail, api, council, fetch, ledger, publish=args.publish)
                except Exception as exc:
                    status = 'failed:'+type(exc).__name__
                decision = ledger.result(str(post['question']['id']))
                if decision.get('decision') == 'review':
                    # Legacy runs did not retain reasons. Never invent a cause or rerun a selected outcome.
                    decision = {'decision': 'abstained_legacy_review', 'decision_codes': ['legacy_reason_unavailable']}
                summary.append({'post_id': post['id'], 'status': status, **decision})
                print(json.dumps(summary[-1]))
                if sum(x['status'] not in ('already_forecast', 'already_attempted')
                       for x in summary) >= args.max_questions:
                    break
    report = {'finished_at': now(), 'tournament': args.tournament, 'publish': args.publish,
              'questions': summary, 'submission_totals': ledger.totals(), 'reserved_or_spent_eur': council.budget.used_eur()}
    (args.state/'latest-run.json').write_text(encode(report), encoding='utf-8')
    if provider_name == 'openrouter':
        await provider.close()
    else:
        await provider.client.close()
    return 1 if any(x['status'].startswith(('failed','submission_uncertain','forecast_accepted'))
                    for x in summary) else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--allow-paid-api', action='store_true')
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--tournament', default='bot-testing-area',
                        choices=['bot-testing-area', 'fall-futureeval-2026', 'minibench'])
    parser.add_argument('--config', type=Path)
    parser.add_argument('--credential-file', type=Path, default=Path('work/asknews.dpapi'))
    parser.add_argument('--state', type=Path, default=Path('work/council-runner'))
    parser.add_argument('--max-questions', type=int, default=1)
    args = parser.parse_args()
    load_local_metaculus()
    load_local_openrouter()
    if not 1 <= args.max_questions <= 5:
        parser.error('--max-questions must be 1..5')
    problems = preflight(args.config, args.credential_file)
    if not args.run:
        print(json.dumps({'engine': 'Horizon Council', 'network_calls': 0, 'blocking': problems}))
        return 2 if problems else 0
    if not args.allow_paid_api:
        parser.error('--run requires --allow-paid-api')
    if problems:
        print(json.dumps({'blocking': problems}))
        return 2
    return asyncio.run(run(args))


if __name__ == '__main__':
    raise SystemExit(main())
