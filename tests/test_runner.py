import asyncio
import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from uuid import uuid4
from unittest import TestCase
from unittest.mock import AsyncMock

from horizon.budget import Budget
from horizon.council import Council
from horizon.memory import Memory
from horizon.runner import Ledger, process, question_from_post, has_forecast
from test_horizon import NOW, Fake, payload


class RunnerTests(TestCase):
    def setUp(self):
        self.root = Path('work/test-temp')/uuid4().hex
        self.root.mkdir(parents=True)
        self.post = {'id': 7, 'question': {'id': 1, 'type': 'binary', 'status': 'open',
            'title': 'Will test CPI exceed 3%?', 'resolution_criteria': 'Yes if CPI >3%.',
            'scheduled_close_time': (NOW+timedelta(hours=1)).isoformat(),
            'scheduled_resolve_time': (NOW+timedelta(days=30)).isoformat(),
            'my_forecasts': {'latest': None}}}
        self.api = AsyncMock()
        self.api.detail.return_value = self.post
        self.fetch = AsyncMock(return_value={'provider':'AskNews', 'status':'ok',
            'completed_at': NOW.isoformat(), 'response': {'as_dicts': [
                {'article_id':'a','article_url':'https://example.test/a','summary':'Test CPI.'}]}})
        self.provider = Fake([json.dumps({'selected_ids':['asknews-a'],'missing_information':[]}),
                              payload(.5),payload(.55)])
        self.council = Council(self.provider,Budget(self.root/'budget.sqlite'),
                               Memory(self.root/'memory.sqlite'),'test',
                               select_evidence=True,clock=lambda:NOW)
        self.ledger = Ledger(self.root/'ledger.sqlite')

    def tearDown(self):
        for file in self.root.iterdir():
            file.unlink()
        self.root.rmdir()

    def run_case(self, publish=False):
        return asyncio.run(process(self.post,self.api,self.council,self.fetch,
                                   self.ledger,publish=publish,clock=lambda:NOW))

    def test_full_chain_and_restart_prevents_duplicate(self):
        self.assertEqual(self.run_case(True),'submitted')
        self.api.submit.assert_awaited_once()
        self.api.comment.assert_awaited_once()
        self.assertIn('driver',self.api.comment.call_args.args[1])
        self.assertIn('https://example.test/a',self.api.comment.call_args.args[1])
        self.ledger = Ledger(self.root/'ledger.sqlite')
        self.assertEqual(self.run_case(True),'already_attempted')
        self.assertEqual(self.provider.calls,3)

    def test_dry_run_never_publishes(self):
        self.assertEqual(self.run_case(),'dry_run')
        self.api.submit.assert_not_called()
        self.api.comment.assert_not_called()

    def test_ambiguous_submission_never_retries(self):
        self.api.submit.side_effect = TimeoutError()
        self.assertEqual(self.run_case(True),'submission_uncertain')
        self.assertEqual(self.run_case(True),'already_attempted')
        self.api.comment.assert_not_called()
        self.api.submit.assert_awaited_once()

    def test_comment_failure_distinguished(self):
        self.api.comment.side_effect = TimeoutError()
        self.assertEqual(self.run_case(True),'forecast_accepted_comment_pending')
        self.assertEqual(self.run_case(True),'already_attempted')

    def test_changed_criteria_blocks_submission(self):
        fresh = deepcopy(self.post)
        fresh['question']['resolution_criteria'] = 'Changed criteria'
        self.api.detail.return_value = fresh
        self.assertEqual(self.run_case(True),'failed')
        self.api.submit.assert_not_called()

    def test_already_forecast_never_spends(self):
        self.post['question']['my_forecasts']['latest'] = {'forecast_values':[.5]}
        self.assertEqual(self.run_case(True),'already_forecast')
        self.fetch.assert_not_called()
        self.assertEqual(self.provider.calls,0)

    def test_missing_auth_state_and_nonbinary_rejected(self):
        self.post['question'].pop('my_forecasts')
        with self.assertRaises(ValueError):
            has_forecast(self.post)
        self.post['question']['type'] = 'numeric'
        with self.assertRaises(ValueError):
            question_from_post(self.post,NOW)

    def test_review_abstains_automatically(self):
        self.provider.replies = iter([json.dumps({'selected_ids':['asknews-a'],
                                                'missing_information':['Missing primary source']}),
                                     payload(.5),payload(.55)])
        self.assertEqual(self.run_case(True),'review')
        self.api.submit.assert_not_called()

    def test_close_buffer_avoids_research_cost(self):
        self.post['question']['scheduled_close_time'] = (NOW+timedelta(seconds=30)).isoformat()
        self.assertEqual(self.run_case(True),'deadline')
        self.fetch.assert_not_called()
