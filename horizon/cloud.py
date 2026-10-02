"""Scheduled cloud entry point; persistent operational state, no research uploads."""
import argparse
import asyncio
import io
import json
import os
from pathlib import Path
from datetime import datetime
import shutil
import subprocess
import sys
import zipfile
from time import monotonic

from .runner import now
from .storage import connect

FILES = {'checkpoint.json', 'seen.json', 'report.json', 'budget.sqlite',
         'dry-runs.sqlite', 'submissions.sqlite'}
ARTIFACT = 'horizon-operational-state'


def validate_bot(identity):
    if identity.get('id') != 308221 or identity.get('is_bot') is not True:
        raise ValueError('Metaculus credential does not belong to the Horizon bot')


async def verify_connections():
    import httpx
    from asknews_sdk import AsyncAskNewsSDK
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        response = await client.get('https://www.metaculus.com/api/users/me/',
            headers={'Authorization': 'Token '+os.environ['METACULUS_TOKEN']})
        response.raise_for_status()
        validate_bot(response.json())
        response = await client.get('https://openrouter.ai/api/v1/key',
            headers={'Authorization': 'Bearer '+os.environ['OPENROUTER_API_KEY']})
        response.raise_for_status()
    async with AsyncAskNewsSDK(api_key=os.environ['ASKNEWS_API_KEY'], retries=0,
        timeout=30, follow_redirects=False) as news:
        await news.news.search_news(query='Metaculus FutureEval', n_articles=1,
                                    return_type='both', strategy='latest news')
    return {'metaculus_bot_id': 308221, 'openrouter': 'authenticated', 'asknews': 'authenticated'}


def previous_run(runs, number):
    previous = [r for r in runs if r['run_number'] < number]
    if not previous:
        if number != 1:
            raise RuntimeError('Previous run history missing; refusing a state reset')
        return None
    last = max(previous, key=lambda r: r['run_number'])
    if last['run_number'] != number-1 or last['status'] != 'completed':
        raise RuntimeError('Previous run is missing or still active')
    if last['conclusion'] not in ('success', 'failure'):
        raise RuntimeError('Previous run interrupted; inspect and reconcile before resuming')
    # Failed health gates are recoverable only when restore_zip validates the
    # complete checkpoint from this exact predecessor. Missing state still blocks.
    return last


def restore_zip(data, destination, expected_run):
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or not set(names) <= FILES:
            raise ValueError('Unexpected state archive contents')
        if not {'checkpoint.json', 'seen.json', 'budget.sqlite'} <= set(names):
            raise ValueError('Incomplete state archive')
        if sum(x.file_size for x in archive.infolist()) > 20_000_000:
            raise ValueError('Oversized state archive')
        checkpoint = json.loads(archive.read('checkpoint.json'))
        if checkpoint != {'schema': 1, 'run_id': str(expected_run), 'complete': True}:
            raise ValueError('State checkpoint does not match predecessor')
        for name in names:
            (destination/name).write_bytes(archive.read(name))


def export_state(source, destination, run_id):
    destination.mkdir(parents=True, exist_ok=True)
    # Explicit allowlist: never include credentials, memory, news, or model traces.
    for name in FILES-{'checkpoint.json'}:
        if (source/name).exists():
            shutil.copyfile(source/name, destination/name)
    (destination/'checkpoint.json').write_text(json.dumps({
        'schema': 1, 'run_id': str(run_id), 'complete': True}), encoding='utf-8')


async def restore_cloud(state):
    import httpx
    repo = os.environ['GITHUB_REPOSITORY']
    run_id = os.environ['GITHUB_RUN_ID']
    number = int(os.environ['GITHUB_RUN_NUMBER'])
    if os.environ.get('GITHUB_RUN_ATTEMPT', '1') != '1':
        raise RuntimeError('Reruns require explicit state reconciliation')
    async with httpx.AsyncClient(base_url='https://api.github.com', timeout=30,
        headers={'Authorization': 'Bearer '+os.environ['GH_TOKEN'],
                 'Accept': 'application/vnd.github+json'}) as client:
        response = await client.get(f'/repos/{repo}/actions/runs/{run_id}')
        response.raise_for_status()
        workflow_id = response.json()['workflow_id']
        response = await client.get(f'/repos/{repo}/actions/workflows/{workflow_id}/runs',
                                    params={'per_page': 100})
        response.raise_for_status()
        runs = response.json()['workflow_runs']
        # Concurrency can cancel a queued run before any job starts. Only those
        # provably empty runs may be skipped; a cancelled executing job blocks.
        candidates = sorted((r for r in runs if r['run_number'] < number),
                            key=lambda r: r['run_number'], reverse=True)
        expected = number-1
        last = None
        for candidate in candidates:
            if candidate['run_number'] != expected or candidate['status'] != 'completed':
                raise RuntimeError('Previous run history missing or still active')
            if candidate['conclusion'] in ('cancelled', 'skipped'):
                jobs = await client.get(f'/repos/{repo}/actions/runs/{candidate["id"]}/jobs')
                jobs.raise_for_status()
                if jobs.json().get('total_count') != 0:
                    raise RuntimeError('Interrupted job requires state reconciliation')
                expected -= 1
                continue
            if candidate['conclusion'] not in ('success', 'failure'):
                raise RuntimeError('Previous run needs reconciliation')
            last = candidate
            break
        if last is None and expected != 0:
            raise RuntimeError('Previous durable run missing')
        if last is None:
            state.mkdir(parents=True, exist_ok=True)
            return
        response = await client.get(f'/repos/{repo}/actions/runs/{last["id"]}/artifacts')
        response.raise_for_status()
        artifacts = [a for a in response.json()['artifacts'] if a['name'] == ARTIFACT and not a['expired']]
        if len(artifacts) != 1:
            raise RuntimeError('Durable state unavailable; refusing to restart budget or attempts')
        response = await client.get(f'/repos/{repo}/actions/artifacts/{artifacts[0]["id"]}/zip')
        # GitHub artifact download redirects to a signed storage URL. Do not forward credentials.
        if response.status_code in (301, 302, 303, 307, 308):
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as download:
                response = await download.get(response.headers['location'])
        response.raise_for_status()
        restore_zip(response.content, state, last['id'])


async def inventory():
    import httpx
    token = os.environ.get('METACULUS_TOKEN')
    if not token:
        raise RuntimeError('METACULUS_TOKEN missing')
    rows, offset = [], 0
    async with httpx.AsyncClient(base_url='https://www.metaculus.com', timeout=30,
        headers={'Authorization': 'Token '+token}, follow_redirects=False) as client:
        while True:
            response = await client.get('/api/posts/', params={
                'tournaments': 'fall-futureeval-2026', 'statuses': 'open',
                'limit': 100, 'offset': offset, 'order_by': 'id'})
            response.raise_for_status()
            page = response.json()['results']
            for post in page:
                if not any(post.get(k) for k in ('question', 'group_of_questions', 'conditional')):
                    continue  # Announcements/notebooks are not forecast questions.
                q = post.get('question') or {}
                rows.append({'post_id': int(post['id']), 'title': str(post.get('title') or q.get('title') or ''),
                             'supported': q.get('type') in ('binary', 'numeric', 'discrete') and q.get('status') == 'open'})
            if len(page) < 100:
                return rows
            offset += len(page)


def update_inventory(posts, seen):
    new = [p for p in posts if str(p['post_id']) not in seen]
    return new, sorted(set(seen) | {str(p['post_id']) for p in posts})


def submission_totals(state):
    """Count unique accepted questions across runs, including closed questions."""
    path = state/'submissions.sqlite'
    counts = {}
    if path.exists():
        with connect(path) as db:
            counts = dict(db.execute('SELECT state, COUNT(*) FROM submissions GROUP BY state'))
    accepted = ('submitted', 'already_forecast', 'forecast_accepted_comment_pending')
    return {'accepted_total': sum(counts.get(key, 0) for key in accepted),
            'submission_states': counts,
            'uncertain_total': counts.get('submission_uncertain', 0),
            'comment_pending_total': counts.get('forecast_accepted_comment_pending', 0)}


def assess_health(report, posts):
    """Separate job execution from actual coverage and confirmed submissions."""
    reasons = list(report['blocked'])
    unsupported = [p['post_id'] for p in posts if not p['supported']]
    if unsupported:
        reasons.append('Unsupported open questions: '+', '.join(map(str, unsupported)))
    decisions = {int(q['post_id']): q.get('decision', q.get('status', 'unknown'))
                 for q in report['questions']}
    if report['mode'] != 'observe':
        pending = [p['post_id'] for p in posts if p['supported'] and p['post_id'] not in decisions]
        if pending:
            reasons.append('Unprocessed open questions: '+', '.join(map(str, pending)))
        good = {'submitted', 'already_forecast'}
        if report['mode'] == 'dry-run':
            good.add('dry_run')
        for ident, decision in decisions.items():
            if decision not in good:
                reasons.append(f'Question {ident}: {decision}')
    if report.get('schedule_delayed'):
        reasons.append('Schedule delayed')
    report['blocked'] = list(dict.fromkeys(reasons))
    report['health'] = ('degraded' if reasons else 'observe' if report['mode'] == 'observe'
                        else 'idle' if not posts else 'ok')
    report['submitted_this_run'] = sum(q.get('status') == 'submitted' for q in report['questions'])
    report['accepted_visible'] = sum(q.get('status') == 'already_forecast' for q in report['questions'])
    return report


async def notify_github(report):
    """Public question links only; assign the owner to deliver a GitHub notification."""
    import httpx
    repo = os.environ['GITHUB_REPOSITORY']
    owner = repo.split('/')[0]
    async with httpx.AsyncClient(base_url='https://api.github.com', timeout=30,
        headers={'Authorization': 'Bearer '+os.environ['GH_TOKEN'],
                 'Accept': 'application/vnd.github+json'}) as client:
        for post in report['new_questions']:
            ident = post['post_id']
            title = f'Horizon: novo pitanje #{ident}'
            # Owner notifications are durable, independently of the Codex app.
            response = await client.post(f'/repos/{repo}/issues', json={
                'title': title, 'assignees': [owner],
                'body': f'Novo pitanje: https://www.metaculus.com/questions/{ident}/\n\n'
                        + ('Podržan tip pitanja.' if post['supported'] else 'Ovaj tip pitanja još nije podržan.')
                        + f'\n\nNačin rada: **{report["mode"]}**. '
                        + 'Obavijest o pitanju nije potvrda objavljene prognoze.\n\n'
                        + f'Izvještaj: https://github.com/{repo}/actions/runs/{os.environ["GITHUB_RUN_ID"]}'})
            response.raise_for_status()
        if report['daily_report'] or report.get('health_changed', False):
            response = await client.post(f'/repos/{repo}/issues', json={
                'title': 'Horizon: dnevni izvještaj '+report['checked_at'][:10],
                'assignees': [owner],
                'body': f'Način rada: **{report["mode"]}**\n\n'
                        + f'Otvoreno pitanja: {report["open_posts"]}; podržanih: {report["supported_open"]}.\n\n'
                        + f'Stanje: **{report.get("health", "unknown")}**; potvrđene nove objave: {report.get("submitted_this_run", 0)}.\n\n'
                        + f'Ukupno prihvaćenih pitanja u trajnoj evidenciji: **{report["accepted_total"]}**.\n\n'
                        + f'Neizvjesna slanja: {report["uncertain_total"]}; čeka komentar: {report["comment_pending_total"]}.\n\n'
                        + 'Prepreke: '+(', '.join(report['blocked']) or 'nema prijavljenih u ovoj provjeri')
                        + f'\n\nDetalji: https://github.com/{repo}/actions/runs/{os.environ["GITHUB_RUN_ID"]}'})
            response.raise_for_status()
        for result in report.get('new_results', []):
            ident = int(result['post_id'])
            response = await client.post(f'/repos/{repo}/issues', json={
                'title': f'Horizon: ishod obrade #{ident}', 'assignees': [owner],
                'body': f'https://www.metaculus.com/questions/{ident}/\n\n'
                    + f'Ishod: **{result["decision"]}**\n\n'
                    + 'Kodovi razloga: '+(', '.join(result.get('decision_codes', [])) or 'nema')
                    + '\n\nAbstained znači automatski odustanak; ne čeka tvoje odobrenje.'
                    + f'\n\nDetalji: https://github.com/{repo}/actions/runs/{os.environ["GITHUB_RUN_ID"]}'})
            response.raise_for_status()
        if report.get('schedule_delayed') and not report.get('was_schedule_delayed'):
            response = await client.post(f'/repos/{repo}/issues', json={
                'title': 'Horizon: raspored kasni', 'assignees': [owner],
                'body': f'Razmak između provjera: {report["gap_minutes"]} minuta. '
                    + 'Ciljani raspored je 20 minuta; GitHub ga nije održao. '
                    + 'Kratko otvorena pitanja mogla su biti propuštena.\n\n'
                    + f'https://github.com/{repo}/actions/runs/{os.environ["GITHUB_RUN_ID"]}'})
            response.raise_for_status()


async def tick(mode, state, output, *, local=False, restore=True):
    from .budget import Budget
    if not local and restore:
        await restore_cloud(state)
    # A failed/uncertain tick must never upload an earlier tick's checkpoint.
    (output/'checkpoint.json').unlink(missing_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    Budget(state/'budget.sqlite')
    inventory_error = None
    try:
        posts = await inventory()
    except Exception as exc:
        # No spending has occurred. Preserve state and surface the problem so
        # transient read failures do not reset the ledger or permanently wedge it.
        posts = []
        inventory_error = 'Question inventory failed: '+type(exc).__name__
    seen_path = state/'seen.json'
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else []
    new, seen = update_inventory(posts, seen)
    seen_path.write_text(json.dumps(seen), encoding='utf-8')
    old_report = json.loads((state/'report.json').read_text(encoding='utf-8')) if (state/'report.json').exists() else {}
    today = now().date().isoformat()
    gap = round((now()-datetime.fromisoformat(old_report['checked_at'])).total_seconds()/60, 1) if old_report.get('checked_at') else None
    report = {'checked_at': now().isoformat(), 'mode': mode, 'open_posts': None if inventory_error else len(posts),
              'new_questions': new, 'supported_open': sum(p['supported'] for p in posts),
              'questions': [], 'blocked': [inventory_error] if inventory_error else [], 'publishing_enabled': mode == 'publish',
              'daily_report': old_report.get('last_daily') != today, 'last_daily': today,
              'connections': old_report.get('connections', {}),
              'connections_checked_date': old_report.get('connections_checked_date')}
    report.update(gap_minutes=gap, schedule_delayed=gap is not None and gap > 60,
                  was_schedule_delayed=old_report.get('schedule_delayed', False))
    if mode != 'observe':
        report['blocked'] += [key+' missing' for key in
            ('METACULUS_TOKEN', 'OPENROUTER_API_KEY', 'ASKNEWS_API_KEY') if not os.environ.get(key)]
        if posts and not report['blocked'] and report['connections_checked_date'] != today:
            try:
                report['connections'] = await verify_connections()
                report['connections_checked_date'] = today
            except Exception as exc:
                report['blocked'].append('Service connection check failed: '+type(exc).__name__)
        if posts and not report['blocked']:
            command = [sys.executable, '-m', 'horizon.runner', '--config', 'configs/openrouter.example.json',
                       '--run', '--allow-paid-api', '--tournament', 'fall-futureeval-2026',
                       '--state', str(state), '--max-questions', '3']
            if mode == 'publish':
                command.append('--publish')
            # A timeout intentionally prevents checkpoint export. The next job fails closed.
            run_report = state/'latest-run.json'
            run_report.unlink(missing_ok=True)
            result = await asyncio.to_thread(subprocess.run, command, timeout=900,
                                            capture_output=True, text=True)
            run_report = state/'latest-run.json'
            if result.returncode != 0:
                report['blocked'].append('Forecast runner failed: exit '+str(result.returncode))
            if run_report.exists():
                report['questions'] = json.loads(run_report.read_text(encoding='utf-8'))['questions']
    assess_health(report, posts)
    report.update(submission_totals(state))
    report['health_changed'] = report['blocked'] != old_report.get('blocked', []) or report['health'] != old_report.get('health')
    report['runtime_sha'] = os.environ.get('HORIZON_RUNTIME_SHA', 'local')
    outcomes = dict(old_report.get('notified_outcomes', {}))
    report['new_results'] = []
    for q in report['questions']:
        decision = q.get('decision', q['status'])
        if decision in ('already_attempted', 'started'):
            continue
        key = str(int(q['post_id']))
        if outcomes.get(key) != decision:
            report['new_results'].append({'post_id': int(key), 'decision': decision,
                                         'decision_codes': q.get('decision_codes', [])})
            outcomes[key] = decision
    report['notified_outcomes'] = outcomes
    (state/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    export_state(state, output, os.environ.get('GITHUB_RUN_ID', 'local'))
    if not local and os.environ.get('HORIZON_NOTIFY') == 'true':
        await notify_github(report)
    summary = ['# Horizon status', f'Checked: {report["checked_at"]}', f'Mode: **{mode}**',
               f'Health: **{report["health"]}**; submitted this run: {report["submitted_this_run"]}',
               f'Accepted questions, all runs: **{report["accepted_total"]}**; uncertain sends: {report["uncertain_total"]}',
               'Cumulative submission states: '+json.dumps(report['submission_states']),
               f'Runtime commit: {report["runtime_sha"]}',
               f'Open posts: {report["open_posts"]}; supported posts: {report["supported_open"]}',
               f'New posts: {len(new)}', '', '## New questions']
    # Only stable numeric links in the public summary; no untrusted Markdown from titles.
    summary += [f'- https://www.metaculus.com/questions/{p["post_id"]}/ '
                + ('(supported)' if p['supported'] else '(unsupported type)') for p in new]
    summary += ['', '## Processing']+[f'- {q["post_id"]}: {q.get("decision", q["status"])}; reasons: '+', '.join(q.get('decision_codes', [])) for q in report['questions']]
    summary += ['', '## Schedule', f'Gap since previous check: {gap} minutes; target: 20 minutes.',
                'Schedule delayed: '+str(report['schedule_delayed'])]
    summary += ['', '## Blockers']+['- '+x for x in report['blocked']]
    summary += ['', '## Connections', json.dumps(report['connections']),
                'Last verified: '+str(report['connections_checked_date'])]
    summary += ['', 'No forecast probabilities, credentials, research text or model traces are uploaded.']
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        Path(os.environ['GITHUB_STEP_SUMMARY']).write_text('\n\n'.join(summary), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True), flush=True)
    return report


async def watch(args):
    started = monotonic()
    first = True
    while True:
        report = await tick(args.mode, args.state, args.output,
                            local=args.local, restore=first)
        first = False
        remaining = args.watch_minutes*60-(monotonic()-started)
        # Reserve 15 minutes for a paid forecasting batch and state export.
        if args.watch_minutes == 0 or remaining < args.interval_seconds+900:
            return report
        await asyncio.sleep(args.interval_seconds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['observe', 'dry-run', 'publish'], default='observe')
    parser.add_argument('--state', type=Path, default=Path('work/cloud'))
    parser.add_argument('--output', type=Path, default=Path('work/cloud-export'))
    parser.add_argument('--local', action='store_true')
    parser.add_argument('--watch-minutes', type=int, default=0)
    parser.add_argument('--interval-seconds', type=int, default=300)
    args = parser.parse_args()
    if not 0 <= args.watch_minutes <= 330 or not 60 <= args.interval_seconds <= 1200:
        parser.error('Watch must be 0..330 minutes; interval 60..1200 seconds')
    if args.local:
        from .runner import load_local_metaculus, load_local_openrouter
        load_local_metaculus()
        load_local_openrouter()
    report = asyncio.run(watch(args))
    if report['health'] == 'degraded':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
