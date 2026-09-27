import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, Mock
from uuid import uuid4
from horizon.asknews_research import research, research_question
from test_horizon import Q


class AskNewsTests(TestCase):
    def setUp(self):
        self.path = Path('work/test-temp') / uuid4().hex
        self.path.mkdir(parents=True)
        self.search = AsyncMock()
        self.client = SimpleNamespace(news=SimpleNamespace(search_news=self.search))

    def tearDown(self):
        for file in self.path.iterdir():
            file.unlink()
        self.path.rmdir()

    def test_preserves_response_and_provenance(self):
        data = {'as_dicts': [{'article_url': 'https://example.test/a', 'summary': 'Synthetic summary'}],
                'as_string': 'Synthetic provider context'}
        self.search.return_value = SimpleNamespace(model_dump=Mock(return_value=data))
        record, path = asyncio.run(research(self.client, 'synthetic question', self.path))
        self.assertEqual(record['status'], 'ok')
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['response'], data)
        self.assertIsNone(record['actual_cost'])
        self.assertEqual(self.search.await_count, 1)
        self.assertEqual(self.search.call_args.kwargs['strategy'], 'latest news')

    def test_failure_does_not_leak_exception_or_retry(self):
        self.search.side_effect = RuntimeError('secret-key-must-not-be-written')
        record, path = asyncio.run(research(self.client, 'synthetic question', self.path))
        self.assertEqual(record['status'], 'failed')
        self.assertNotIn('secret-key', path.read_text(encoding='utf-8'))
        self.assertEqual(self.search.await_count, 1)

    def test_invalid_input_never_calls_api(self):
        with self.assertRaises(ValueError):
            asyncio.run(research(self.client, '', self.path))
        self.search.assert_not_called()

    def test_filters_are_sent_and_audited(self):
        self.search.return_value = SimpleNamespace(model_dump=Mock(return_value={'as_dicts': []}))
        record, _ = asyncio.run(research(self.client, 'synthetic', self.path,
                                         required_terms=['CPI'], languages=['en']))
        self.assertEqual(record['status'], 'empty')
        self.assertEqual(self.search.call_args.kwargs['string_guarantee'], ['CPI'])
        self.assertEqual(record['filters']['languages'], ['en'])

    def test_dual_search_deduplicates_and_preserves_audit(self):
        self.search.return_value = SimpleNamespace(model_dump=Mock(return_value={
            'as_dicts':[{'article_id':'a','summary':'Same article'}]}))
        record, path = asyncio.run(research_question(self.client,Q,self.path))
        self.assertEqual(len(record['response']['as_dicts']),1)
        self.assertEqual({call.kwargs['strategy'] for call in self.search.call_args_list},
                         {'latest news','news knowledge'})
        self.assertIn(Q.criteria,self.search.call_args_list[1].kwargs['query'])
        self.assertEqual(len(record['searches']),2)
        self.assertFalse(record['partial'])
        self.assertTrue(path.exists())

    def test_partial_search_failure_is_visible_without_retry(self):
        self.search.side_effect = [RuntimeError('private'),SimpleNamespace(model_dump=Mock(
            return_value={'as_dicts':[{'article_id':'a'}]}))]
        record, _ = asyncio.run(research_question(self.client,Q,self.path))
        self.assertEqual(record['status'],'ok')
        self.assertTrue(record['partial'])
        self.assertEqual(self.search.await_count,2)

    def test_invalid_strategy_never_calls_provider(self):
        with self.assertRaises(ValueError):
            asyncio.run(research(self.client,'query',self.path,strategy='invalid'))
        self.search.assert_not_called()
