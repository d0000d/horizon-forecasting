"""Read-only verification of authenticated Metaculus history; never submit."""
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import sqlite3


def summarize(rows, results):
    accepted = {str(q) for q, state, _ in rows if state in
                {'submitted', 'already_forecast', 'forecast_accepted_comment_pending'}}
    visible = {str(row['question_id']) for row in results if any(
        item.get('author_id') == 308221 for item in row.get('forecasts', []))}
    errors = Counter()
    for _, state, detail in rows:
        if state == 'failed':
            try:
                reason = json.loads(detail or '{}').get('debug_error', 'unspecified ValueError')
            except ValueError:
                reason = 'unspecified legacy error'
            errors[reason] += 1
    return {'ledger_states': dict(Counter(state for _, state, _ in rows)),
            'failed_reasons': dict(errors), 'accepted_in_ledger': len(accepted),
            'accepted_verified_on_metaculus': len(accepted & visible),
            'accepted_not_verified': sorted(accepted - visible),
            'historically_blocked_with_forecast_now': sorted(
                {str(q) for q, state, _ in rows if state in {'blocked_payment', 'failed'}} & visible)}


async def main():
    import httpx
    from horizon.cloud import validate_bot
    path = Path('work/cloud/submissions.sqlite')
    if not path.is_file():
        raise RuntimeError('Historical submission ledger unavailable')
    with sqlite3.connect(path) as db:
        rows = db.execute('SELECT question,state,detail FROM submissions').fetchall()
    async with httpx.AsyncClient(base_url='https://www.metaculus.com', timeout=30,
            follow_redirects=False, headers={'Authorization': 'Token '+os.environ['METACULUS_TOKEN']}) as client:
        response = await client.get('/api/users/me/')
        response.raise_for_status()
        validate_bot(response.json())
        response = await client.get('/api/questions/bulk-forecast-read/',
            params=[('question_ids', int(q)) for q, _, _ in rows])
        response.raise_for_status()
        report = summarize(rows, response.json()['results'])
    print('PUBLICATION_VERIFICATION:', json.dumps(report, sort_keys=True))
    if report['accepted_not_verified']:
        raise RuntimeError('Some accepted ledger entries could not be verified on Metaculus')


if __name__ == '__main__':
    asyncio.run(main())
