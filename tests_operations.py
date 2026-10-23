import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from datetime import timedelta
from unittest import TestCase
from unittest.mock import AsyncMock, Mock, patch
from horizon.runner import Ledger
from horizon.numeric import forecast
from test_numeric import question as specification
from test_horizon import NOW


class BoundedRecoveryTests(TestCase):
    def test_publication_verification_does_not_infer_success_from_ledger(self):
        from verify_publication import summarize
        rows = [('1', 'submitted', ''), ('2', 'already_forecast', ''),
                ('3', 'blocked_payment', ''), ('4', 'failed', '{}')]
        report = summarize(rows, [{'question_id': 1, 'forecasts': [{'author_id': 308221}]},
                                  {'question_id': 2, 'forecasts': [{'author_id': 99}]}])
        self.assertEqual(report['accepted_verified_on_metaculus'], 1)
        self.assertEqual(report['accepted_not_verified'], ['2'])
        self.assertEqual(report['ledger_states']['blocked_payment'], 1)

    def test_numeric_recovery_once_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'ledger.sqlite'
            ledger = Ledger(path)
            detail = json.dumps({'decision_codes': ['ValueError'], 'retryable': True,
                                 'debug_error': 'Numeric independent audit did not approve publication'})
            self.assertTrue(ledger.claim('n'))
            ledger.set('n', 'failed', detail)
            self.assertTrue(ledger.claim('n'))
            ledger.set('n', 'failed', detail)
            self.assertFalse(Ledger(path).claim('n'))
            ledger.set('n', 'submission_uncertain', detail)
            self.assertFalse(Ledger(path).claim('n'))

    def test_payment_and_transport_retries_are_finite(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / 'ledger.sqlite')
            for state in ('blocked_payment', 'failed'):
                self.assertTrue(ledger.claim(state))
                for _ in range(3):
                    ledger.set(state, state, json.dumps({'retryable': True}))
                    self.assertTrue(ledger.claim(state))
                ledger.set(state, state, json.dumps({'retryable': True}))
                self.assertFalse(ledger.claim(state))

    def test_explicit_evidence_refusal_never_retries_stale_flag(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / 'ledger.sqlite')
            ledger.claim('x')
            ledger.set('x', 'failed', json.dumps({'retryable': True,
                       'debug_error': 'Categorical evidence does not support a forecast'}))
            self.assertFalse(ledger.claim('x'))

    def test_numeric_revision_requires_new_approval_and_accounts_every_call(self):
        q = specification()
        question = SimpleNamespace(id='n', text='Count?', criteria='Official count',
            fine_print='', background='', as_of=NOW, close_time=NOW+timedelta(hours=1))
        evidence = [SimpleNamespace(id='s', source='https://example.org', text='Evidence')]
        estimate = {'can_forecast': True, 'cdf': [i/20 for i in range(21)],
                    'rationale': 'Evidence and uncertainty', 'source_ids': ['s']}
        rejected = {'can_publish': False, 'issues': ['Wrong time window'],
                    'rationale': 'Check window', 'source_ids': ['s']}
        approved = dict(rejected, can_publish=True, issues=[])
        for final in (approved, rejected):
            provider = Mock()
            provider.complete = AsyncMock(side_effect=[SimpleNamespace(text=json.dumps(x), cost_eur=.1)
                for x in (estimate, estimate, estimate, rejected, estimate, final)])
            budget = Mock()
            council = SimpleNamespace(provider=provider, budget=budget, run_id='r', timeout=10, clock=lambda: NOW)
            with patch('horizon.numeric.prepare_evidence', return_value=evidence):
                if final['can_publish']:
                    values, reason = asyncio.run(forecast(council, question, q, {}))
                    self.assertEqual(len(values), 201)
                    self.assertIn('Revision after audit', reason)
                else:
                    with self.assertRaisesRegex(ValueError, 'did not approve'):
                        asyncio.run(forecast(council, question, q, {}))
            self.assertEqual(provider.complete.await_count, 6)
            self.assertEqual(budget.reserve.call_count, 6)
            self.assertEqual(budget.settle.call_count, 6)

    def test_revised_evidence_refusal_stops_before_second_audit(self):
        q = specification()
        question = SimpleNamespace(id='n', text='Count?', criteria='Official count',
            fine_print='', background='', as_of=NOW, close_time=NOW+timedelta(hours=1))
        evidence = [SimpleNamespace(id='s', source='https://example.org', text='Evidence')]
        estimate = {'can_forecast': True, 'cdf': [i/20 for i in range(21)],
                    'rationale': 'Reason', 'source_ids': ['s']}
        rejected = {'can_publish': False, 'issues': ['Insufficient evidence'],
                    'rationale': 'Refuse', 'source_ids': ['s']}
        provider = Mock()
        provider.complete = AsyncMock(side_effect=[SimpleNamespace(text=json.dumps(x), cost_eur=.1)
            for x in (estimate, estimate, estimate, rejected, dict(estimate, can_forecast=False))])
        council = SimpleNamespace(provider=provider, budget=Mock(), run_id='r', timeout=10, clock=lambda: NOW)
        with patch('horizon.numeric.prepare_evidence', return_value=evidence):
            with self.assertRaisesRegex(ValueError, 'cannot resolve'):
                asyncio.run(forecast(council, question, q, {}))
        self.assertEqual(provider.complete.await_count, 5)
