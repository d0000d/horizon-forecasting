"""Deploy bounded advisory-review publication to the pinned recovery runtime."""
from pathlib import Path


def replace_once(path, old, new):
    target = Path(path)
    source = target.read_text(encoding='utf-8')
    if source.count(old) != 1:
        raise SystemExit(f'Unexpected recovery runtime: {path}')
    target.write_text(source.replace(old, new), encoding='utf-8')


replace_once('horizon/models.py',
    '    adjudication: dict | None = None',
    '    adjudication: dict | None = None\n    publication_notes: list[str] = field(default_factory=list)')
replace_once('horizon/council.py',
    "        status = 'abstain' if raw is None else ('review' if review else 'ok')",
    """        publication_notes = []
        # Publish the existing ensemble estimate, never invent a replacement.
        # Only advisory concerns qualify; failed stages and serious findings do not.
        if (self.auto_resolve and review and set(codes) == {'unresolved_concerns'}
                and raw is not None and calibrated is not None and evidence
                and len(agents) >= 2 and audit is not None
                and all(event['status'] == 'success' for event in events)):
            publication_notes = [c['issue'] for c in concerns]
            codes = ['published_with_unresolved_uncertainty']
            review = False
        status = 'abstain' if raw is None else ('review' if review else 'ok')""")
replace_once('horizon/council.py',
    '        record.adjudication = adjudication',
    '        record.adjudication = adjudication\n        record.publication_notes = publication_notes')
replace_once('horizon/runner.py',
    "    lines += ['\\nSources (AskNews summaries, not verified publisher quotations):']",
    """    if record.publication_notes:
        lines += ['\\nLimitations: this is an uncertain forecast, not a verified outcome.',
                  'The ensemble estimate is published with unresolved advisory concerns:']
        lines += ['- '+note for note in record.publication_notes]
    lines += ['\\nSources (AskNews summaries, not verified publisher quotations):']""")
# The production workflow installs a durable claim() implementation first.
replace_once('horizon/runner.py',
    "            if not payment_blocked and not (technical or legacy_stop):",
    """            advisory_recovery = (
                state == 'abstained_review' and codes == {'unresolved_concerns'}
                and not db.execute(
                    'SELECT 1 FROM attempt_history WHERE question=? AND state=?',
                    (question_id, 'abstained_review')).fetchone())
            if not payment_blocked and not (technical or legacy_stop or advisory_recovery):""")

# These regressions execute against the actual patched runtime in CI.
Path('tests/test_review_recovery.py').write_text('''import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from horizon.council import Council
from horizon.budget import Budget
from horizon.memory import Memory
from horizon.models import Evidence
from horizon.runner import Ledger, rationale
from test_horizon import Fake, Q, NOW, payload

class AdvisoryRecoveryTests(unittest.TestCase):
    def forecast(self, *, severity='medium', malformed=False, auto=True, evidence=True):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            audit=json.dumps({'findings':[{'severity':severity,'issue':'Future policy remains uncertain',
                'evidence_ids':['source']}], 'targeted_questions':[]})
            adjudication=json.dumps({'decisions':[{'concern_id':'audit-0','resolved':False,
                'basis':'','evidence_ids':[]}]})
            replies=[payload(.45),payload(.5),payload(.55),audit,adjudication]
            if malformed: replies[1]='{}'
            council=Council(Fake(replies),Budget(root/'b.sqlite'),Memory(root/'m.sqlite'),'r',
                auto_resolve=auto,red_team_mode='always',clock=lambda:NOW)
            items=[Evidence('source','Current dated status quo','https://example.test',NOW,NOW)] if evidence else []
            return asyncio.run(council.forecast(Q,evidence=items))

    def test_advisory_forecast_publishes_same_estimate_and_discloses_uncertainty(self):
        record=self.forecast()
        self.assertEqual(record.status,'ok')
        self.assertIn('published_with_unresolved_uncertainty',record.decision_codes)
        self.assertAlmostEqual(record.raw,record.calibrated)
        self.assertGreater(record.calibrated,.45)
        self.assertLess(record.calibrated,.55)
        self.assertIn('Future policy remains uncertain',rationale(record))

    def test_selected_evidence_with_missing_future_input_remains_advisory(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            selection=json.dumps({'selected_ids':['source'],'missing_information':['Future policy is unknown']})
            audit=json.dumps({'findings':[],'targeted_questions':[]})
            adjudication=json.dumps({'decisions':[{'concern_id':'missing-0','resolved':False,
                'basis':'','evidence_ids':[]}]})
            replies=[selection,payload(.45),payload(.5),payload(.55),audit,adjudication]
            council=Council(Fake(replies),Budget(root/'b.sqlite'),Memory(root/'m.sqlite'),'r',
                select_evidence=True,auto_resolve=True,red_team_mode='always',clock=lambda:NOW)
            evidence=[Evidence('source','Current status quo','https://example.test',NOW,NOW)]
            record=asyncio.run(council.forecast(Q,evidence=evidence))
            self.assertEqual(record.status,'ok')
            self.assertIn('Future policy is unknown',record.publication_notes)

    def test_serious_findings_still_block(self):
        for severity in ('high','critical'):
            self.assertEqual(self.forecast(severity=severity).status,'review')

    def test_failed_stage_still_blocks(self):
        self.assertNotEqual(self.forecast(malformed=True).status,'ok')

    def test_no_evidence_or_disabled_auto_mode_still_blocks(self):
        self.assertEqual(self.forecast(evidence=False).status,'review')
        self.assertEqual(self.forecast(auto=False).status,'review')

    def test_advisory_ledger_recovery_happens_once_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'submissions.sqlite'
            ledger=Ledger(path)
            detail=json.dumps({'decision_codes':['unresolved_concerns']})
            self.assertTrue(ledger.claim('question'))
            ledger.set('question','abstained_review',detail)
            self.assertTrue(Ledger(path).claim('question'))
            ledger.set('question','abstained_review',detail)
            self.assertFalse(Ledger(path).claim('question'))

    def test_submission_markers_cannot_be_reopened(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=Ledger(Path(directory)/'s.sqlite')
            for state in ('submitted','submission_uncertain','forecast_accepted_comment_pending'):
                ledger.claim(state)
                ledger.set(state,state,json.dumps({'decision_codes':['unresolved_concerns']}))
                self.assertFalse(ledger.claim(state))
''', encoding='utf-8')
