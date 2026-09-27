"""Scheduled cloud entry point; persistent operational state, no research uploads."""
import argparse
import asyncio
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

from .runner import now

FILES = {'checkpoint.json', 'seen.json', 'report.json', 'budget.sqlite',
         'dry-runs.sqlite', 'submissions.sqlite'}
ARTIFACT = 'horizon-operational-state'


def previous_run(runs, number):
    previous = [r for r in runs if r['run_number'] < number]
    if not previous:
        if number != 1:
            raise RuntimeError('Previous run history missing; refusing a state reset')
        return None
    last = max(previous, key=lambda r: r['run_number'])
    if last['run_number'] != number-1 or last['status'] != 'completed':
        raise RuntimeError('Previous run is missing or still active')
    if last['conclusion'] != 'success':
        raise RuntimeError('Previous run failed; inspect and reconcile before resuming')
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
        last = previous_run(response.json()['workflow_runs'], number)
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
                             'supported': q.get('type') == 'binary' and q.get('status') == 'open'})
            if len(page) < 100:
                return rows
            offset += len(page)


def update_inventory(posts, seen):
    new = [p for p in posts if str(p['post_id']) not in seen]
    return new, sorted(set(seen) | {str(p['post_id']) for p in posts})


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
                        + ('Podržano binarno pitanje.' if post['supported'] else 'Ovaj tip pitanja još nije podržan.')
                        + f'\n\nNačin rada: **{report["mode"]}**. '
                        + 'Obavijest o pitanju nije potvrda objavljene prognoze.\n\n'
                        + f'Izvještaj: https://github.com/{repo}/actions/runs/{os.environ["GITHUB_RUN_ID"]}'})
            response.raise_for_status()
        if report['daily_report']:
            response = await client.post(f'/repos/{repo}/issues', json={
                'title': 'Horizon: dnevni izvještaj '+report['checked_at'][:10],
                'assignees': [owner],
                'body': f'Način rada: **{report["mode"]}**\n\n'
                        + f'Otvoreno pitanja: {report["open_posts"]}; podržanih: {report["supported_open"]}.\n\n'
                        + 'Prepreke: '+(', '.join(report['blocked']) or 'nema prijavljenih u ovoj provjeri')
                        + f'\n\nDetalji: https://github.com/{repo}/actions/runs/{os.environ["GITHUB_RUN_ID"]}'})
            response.raise_for_status()


async def tick(mode, state, output, *, local=False):
    from .budget import Budget
    if not local:
        await restore_cloud(state)
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
    report = {'checked_at': now().isoformat(), 'mode': mode, 'open_posts': None if inventory_error else len(posts),
              'new_questions': new, 'supported_open': sum(p['supported'] for p in posts),
              'questions': [], 'blocked': [inventory_error] if inventory_error else [], 'publishing_enabled': mode == 'publish',
              'daily_report': old_report.get('last_daily') != today, 'last_daily': today}
    if mode != 'observe':
        report['blocked'] += [key+' missing' for key in
            ('METACULUS_TOKEN', 'OPENROUTER_API_KEY', 'ASKNEWS_API_KEY') if not os.environ.get(key)]
        if not report['blocked']:
            command = [sys.executable, '-m', 'horizon.runner', '--config', 'configs/openrouter.example.json',
                       '--run', '--allow-paid-api', '--tournament', 'fall-futureeval-2026',
                       '--state', str(state), '--max-questions', '3']
            if mode == 'publish':
                command.append('--publish')
            # A timeout intentionally prevents checkpoint export. The next job fails closed.
            result = await asyncio.to_thread(subprocess.run, command, timeout=900,
                                            capture_output=True, text=True)
            run_report = state/'latest-run.json'
            if result.returncode != 0:
                raise RuntimeError('Forecast runner failed; inspect local state before resuming')
            if run_report.exists():
                report['questions'] = json.loads(run_report.read_text(encoding='utf-8'))['questions']
    (state/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    if not local and os.environ.get('HORIZON_NOTIFY') == 'true':
        await notify_github(report)
    export_state(state, output, os.environ.get('GITHUB_RUN_ID', 'local'))
    summary = ['# Horizon status', f'Checked: {report["checked_at"]}', f'Mode: **{mode}**',
               f'Open posts: {len(posts)}; supported binary posts: {report["supported_open"]}',
               f'New posts: {len(new)}', '', '## New questions']
    # Only stable numeric links in the public summary; no untrusted Markdown from titles.
    summary += [f'- https://www.metaculus.com/questions/{p["post_id"]}/ '
                + ('(binary)' if p['supported'] else '(unsupported type)') for p in new]
    summary += ['', '## Processing']+[f'- {q["post_id"]}: {q["status"]}' for q in report['questions']]
    summary += ['', '## Blockers']+['- '+x for x in report['blocked']]
    summary += ['', 'No forecast probabilities, credentials, research text or model traces are uploaded.']
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        Path(os.environ['GITHUB_STEP_SUMMARY']).write_text('\n\n'.join(summary), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['observe', 'dry-run', 'publish'], default='observe')
    parser.add_argument('--state', type=Path, default=Path('work/cloud'))
    parser.add_argument('--output', type=Path, default=Path('work/cloud-export'))
    parser.add_argument('--local', action='store_true')
    args = parser.parse_args()
    if args.local:
        from .runner import load_local_metaculus, load_local_openrouter
        load_local_metaculus()
        load_local_openrouter()
    asyncio.run(tick(args.mode, args.state, args.output, local=args.local))


if __name__ == '__main__':
    main()
