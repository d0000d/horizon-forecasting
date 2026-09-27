"""Offline integration checks: all replies and sources are synthetic."""
import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4
from unittest import TestCase
from unittest.mock import patch

from test_horizon import Fake, NOW, Q, payload
from horizon.agents import parse_research, parse_red_team
from horizon.budget import Budget
from horizon.council import Council, Reply
from horizon.memory import Memory
from horizon.research import SourceSnapshot
from horizon.models import Evidence

QUOTE = 'The synthetic indicator is currently at 2.8 percent.'
SOURCE = SourceSnapshot('source-1', 'https://example.test/report', NOW,
                        QUOTE, 'synthetic-hash', primary=True)
ANALYSIS = json.dumps(dict(yes_condition='Published CPI exceeds 3%.',
                          no_condition='Published CPI does not exceed 3%.',
                          ambiguities=[], key_variables=['CPI'], needs_review=False))
RESEARCH = json.dumps(dict(evidence=[dict(source_id='source-1', quote=QUOTE,
                                         direction='neutral')], missing_information=[]))
CLEAN_AUDIT = json.dumps(dict(findings=[], targeted_questions=[]))


class RecordingFake(Fake):
    def __init__(self, replies):
        super().__init__(replies)
        self.prompts = []

    async def complete(self, prompt):
        self.prompts.append(prompt)
        return await super().complete(prompt)


class PipelineTests(TestCase):
    def setUp(self):
        self.path = Path('work/test-temp') / str(uuid4())
        self.path.mkdir(parents=True)
        self.budget = Budget(self.path / 'budget.sqlite')
        self.memory = Memory(self.path / 'memory.sqlite')
        # Windows creates a local socket pair when initializing the event loop.
        self.loop = asyncio.new_event_loop()
        # Once initialized, any network connection fails this suite.
        self.network = patch('socket.socket.connect', side_effect=AssertionError('Offline test'))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.loop.close()
        for file in self.path.iterdir():
            file.unlink()
        self.path.rmdir()

    def run_forecast(self, replies, *, snapshots=(), budget=None, **config):
        provider = RecordingFake(replies)
        council = Council(provider, budget or self.budget, self.memory, 'offline',
                          timeout=.02, **config)
        result = self.loop.run_until_complete(council.forecast(Q, snapshots=snapshots))
        return result, provider

    def test_full_pipeline_persists_verified_evidence_and_audit(self):
        result, provider = self.run_forecast(
            [ANALYSIS, RESEARCH, payload(.4), payload(.6), CLEAN_AUDIT],
            snapshots=[SOURCE], analyze_resolution=True, red_team_mode='always')
        self.assertEqual(provider.calls, 5)
        self.assertEqual(result.status, 'ok')
        self.assertAlmostEqual(result.raw, .5)
        self.assertAlmostEqual(self.budget.used_eur(), .025)
        self.assertEqual([e['agent'] for e in result.events],
                         ['analyst', 'research', 'outside', 'inside', 'red_team'])
        self.assertTrue(all(e['status'] == 'success' for e in result.events))
        saved = Memory(self.path / 'memory.sqlite').records()[0]
        self.assertEqual(saved['evidence'][0]['text'], QUOTE)
        self.assertIsNone(saved['evidence'][0]['published_at'])
        self.assertEqual(saved['red_team']['findings'], [])
        # Initial forecasters see sources but not each other's probability.
        self.assertIn(QUOTE, provider.prompts[3])
        self.assertNotIn('"probability": 0.4', provider.prompts[3])

    def test_adaptive_audit_skipped_for_close_forecasts(self):
        result, provider = self.run_forecast([payload(.52), payload(.56)])
        self.assertEqual(provider.calls, 2)
        self.assertIsNone(result.red_team)
        self.assertEqual(result.status, 'ok')

    def test_disagreement_audit_flags_without_replacing_probability(self):
        audit = json.dumps(dict(findings=[dict(severity='high', issue='Weak prior',
                                              evidence_ids=[])], targeted_questions=[]))
        result, provider = self.run_forecast([payload(.2), payload(.8), audit])
        self.assertEqual(provider.calls, 3)
        self.assertEqual(len(result.agents), 2)
        self.assertAlmostEqual(result.raw, .5)
        self.assertEqual(result.status, 'review')

    def test_extreme_forecast_triggers_audit(self):
        result, provider = self.run_forecast([payload(.02), payload(.03), CLEAN_AUDIT])
        self.assertEqual(provider.calls, 3)
        self.assertEqual(result.status, 'review')

    def test_failed_audit_keeps_forecast_for_review(self):
        result, _ = self.run_forecast([payload(.5), payload(.55), '{}'], red_team_mode='always')
        self.assertIsNotNone(result.raw)
        self.assertEqual(result.status, 'review')
        self.assertEqual(result.events[-1]['error'], 'ValueError')

    def test_fabricated_quote_is_rejected_and_flagged(self):
        fabricated = RESEARCH.replace(QUOTE, 'This quote does not exist in the source text.')
        result, _ = self.run_forecast([fabricated, payload(.5), payload(.55)], snapshots=[SOURCE])
        self.assertEqual(result.evidence, [])
        self.assertIsNone(result.research)
        self.assertEqual(result.status, 'review')

    def test_future_snapshot_rejected_before_call(self):
        provider = RecordingFake([])
        council = Council(provider, self.budget, self.memory, 'offline')
        with self.assertRaises(ValueError):
            self.loop.run_until_complete(council.forecast(Q, snapshots=[replace(SOURCE, fetched_at=NOW+timedelta(seconds=1))]))
        self.assertEqual(provider.calls, 0)
        self.assertEqual(self.budget.used_eur(), 0)

    def test_budget_refusal_stops_remaining_stages(self):
        budget = Budget(self.path / 'small.sqlite', question_eur=.012)
        result, provider = self.run_forecast([ANALYSIS, RESEARCH], budget=budget,
                                             analyze_resolution=True, snapshots=[SOURCE])
        self.assertEqual(provider.calls, 1)
        self.assertEqual(result.status, 'abstain')
        self.assertEqual(result.events[-1]['error'], 'BudgetExceeded')
        self.assertIsNone(result.raw)

    def test_provider_overshoot_stops_before_second_call(self):
        provider = RecordingFake([])
        async def overshoot(prompt):
            provider.calls += 1
            return Reply(payload(.5), 'mock', .02, 1, 1)
        provider.complete = overshoot
        result = self.loop.run_until_complete(Council(provider, self.budget, self.memory, 'offline').forecast(Q))
        self.assertEqual(provider.calls, 1)
        self.assertEqual(result.status, 'abstain')
        self.assertAlmostEqual(self.budget.used_eur(), .02)

    def test_timeout_reservation_survives_restart(self):
        result, _ = self.run_forecast(['timeout', payload(.5), payload(.55)])
        self.assertEqual(result.events[0]['error'], 'TimeoutError')
        restarted = Budget(self.path / 'budget.sqlite')
        self.assertAlmostEqual(restarted.used_eur(), .02)

    def test_unknown_citations_and_duplicate_json_rejected(self):
        with self.assertRaises(ValueError):
            parse_research(RESEARCH.replace('source-1', 'invented'), [SOURCE], Q)
        with self.assertRaises(ValueError):
            parse_red_team('{"findings":[],"findings":[],"targeted_questions":[]}', [])
        with self.assertRaises(ValueError):
            parse_red_team(json.dumps(dict(findings=[dict(severity='high', issue='Unsupported',
                                                         evidence_ids=['invented'])],
                                           targeted_questions=[])), [])

    def test_selector_filters_before_forecasting(self):
        items = [Evidence('a','Relevant evidence','https://a.test',NOW,NOW),
                 Evidence('b','Brazil-only irrelevant evidence','https://b.test',NOW,NOW)]
        provider = RecordingFake([json.dumps(dict(selected_ids=['a'], missing_information=[])),
                                  payload(.5), payload(.55)])
        council = Council(provider,self.budget,self.memory,'select',select_evidence=True)
        record = self.loop.run_until_complete(council.forecast(Q,evidence=items))
        self.assertEqual(record.status,'ok')
        self.assertEqual([e.id for e in record.evidence],['a'])
        self.assertNotIn('Brazil-only',provider.prompts[1])
        self.assertEqual(self.memory.records()[0]['research']['selection']['selected_ids'],['a'])

    def test_selector_invalid_or_empty_abstains_without_forecasters(self):
        for selection in ([],['invented'],['a','a']):
            provider = RecordingFake([json.dumps(dict(selected_ids=selection,missing_information=[]))])
            council = Council(provider,self.budget,self.memory,str(selection),select_evidence=True)
            items = [Evidence('a','Evidence','https://a.test',NOW,NOW)]
            record = self.loop.run_until_complete(council.forecast(Q,evidence=items))
            self.assertEqual(record.status,'abstain')
            self.assertEqual(provider.calls,1)

    def test_deadline_stops_before_any_cost(self):
        result, provider = self.run_forecast([],clock=lambda:Q.deadline)
        self.assertEqual(result.status,'abstain')
        self.assertEqual(provider.calls,0)
        self.assertEqual(self.budget.used_eur(),0)

    def test_deadline_between_roles_abstains(self):
        moments = iter([NOW,Q.deadline,Q.deadline])
        result, provider = self.run_forecast([payload(.5)],clock=lambda:next(moments))
        self.assertEqual(result.status,'abstain')
        self.assertEqual(provider.calls,1)

    def test_distinct_role_providers(self):
        outside = RecordingFake([payload(.4)])
        inside = RecordingFake([payload(.6)])
        result, default = self.run_forecast([],role_providers={'outside':outside,'inside':inside})
        self.assertEqual(result.status,'ok')
        self.assertEqual((outside.calls,inside.calls,default.calls),(1,1,0))

