import io
import json
from pathlib import Path
from contextlib import contextmanager
from uuid import uuid4
import unittest
import zipfile
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from horizon.cloud import export_state, previous_run, restore_zip, update_inventory, notify_github, tick


@contextmanager
def tempdir():
    root = Path('work/test-temp')/uuid4().hex
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        for path in sorted(root.rglob('*'), key=lambda p: len(p.parts), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
        root.rmdir()


class CloudTests(unittest.TestCase):
    def test_notice_contains_only_public_link_and_is_assigned_to_owner(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = SimpleNamespace(raise_for_status=lambda: None)
        report = {'new_questions': [{'post_id': 42, 'supported': True, 'title': 'untrusted @mention'}],
                  'mode': 'observe', 'daily_report': False}
        with patch.dict('sys.modules', {'httpx': SimpleNamespace(AsyncClient=Mock(return_value=client))}), \
             patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_RUN_ID': '7', 'GH_TOKEN': 'fixture'}):
            asyncio.run(notify_github(report))
        payload = client.post.call_args.kwargs['json']
        self.assertEqual(payload['assignees'], ['owner'])
        self.assertIn('/questions/42/', payload['body'])
        self.assertNotIn('@mention', payload['body'])
        self.assertNotIn('fixture', str(payload))

    def test_missing_research_key_blocks_paid_process_and_preserves_state(self):
        with tempdir() as root, patch.dict(os.environ, {}, clear=True), \
             patch('horizon.cloud.inventory', AsyncMock(return_value=[])), \
             patch('horizon.cloud.subprocess.run') as process:
            report = asyncio.run(tick('publish', root/'state', root/'export', local=True))
            self.assertIn('ASKNEWS_API_KEY missing', report['blocked'])
            process.assert_not_called()
            self.assertTrue((root/'export'/'checkpoint.json').exists())

    def test_missing_failed_or_running_predecessor_blocks(self):
        self.assertIsNone(previous_run([], 1))
        for runs in ([], [{'run_number': 1, 'status': 'in_progress', 'conclusion': None}],
                     [{'run_number': 1, 'status': 'completed', 'conclusion': 'failure'}]):
            with self.assertRaises(RuntimeError):
                previous_run(runs, 2)
        with self.assertRaises(RuntimeError):
            previous_run([{'run_number': 1, 'status': 'completed', 'conclusion': 'success'}], 3)

    def test_predecessor_is_latest_even_if_older_success_exists(self):
        rows = [{'run_number': 1, 'status': 'completed', 'conclusion': 'success'},
                {'run_number': 2, 'status': 'completed', 'conclusion': 'cancelled'}]
        with self.assertRaises(RuntimeError):
            previous_run(rows, 3)

    def test_repeated_inventory_reports_only_new_posts(self):
        rows = [{'post_id': 5}, {'post_id': 7}]
        new, seen = update_inventory(rows, ['5'])
        self.assertEqual(new, [{'post_id': 7}])
        self.assertEqual(update_inventory(rows, seen)[0], [])

    def test_export_never_contains_secrets_research_or_forecasts(self):
        with tempdir() as d:
            root = Path(d); source = root/'source'; source.mkdir()
            for name in ('budget.sqlite', 'seen.json', 'memory.sqlite', 'secret.dpapi', 'research.json'):
                (source/name).write_text('fixture')
            export_state(source, root/'export', '42')
            self.assertEqual({p.name for p in (root/'export').iterdir()},
                             {'budget.sqlite', 'seen.json', 'checkpoint.json'})

    def archive(self, extra=None, run='42'):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as z:
            z.writestr('checkpoint.json', json.dumps({'schema': 1, 'run_id': run, 'complete': True}))
            z.writestr('seen.json', '[]'); z.writestr('budget.sqlite', b'fixture')
            if extra:
                z.writestr(extra, b'fixture')
        return buffer.getvalue()

    def test_restore_rejects_wrong_checkpoint_and_unexpected_paths(self):
        with tempdir() as d:
            for archive in (self.archive('../escape'), self.archive('memory.sqlite'), self.archive(run='41')):
                with self.assertRaises(ValueError):
                    restore_zip(archive, Path(d), '42')

    def test_restore_preserves_operational_state(self):
        with tempdir() as d:
            restore_zip(self.archive(), Path(d), '42')
            self.assertEqual((Path(d)/'budget.sqlite').read_bytes(), b'fixture')
