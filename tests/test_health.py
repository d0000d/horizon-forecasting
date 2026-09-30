import unittest
from horizon.cloud import assess_health, previous_run, restore_zip

class HealthTests(unittest.TestCase):
    def report(self, questions=None, mode='publish'):
        return dict(blocked=[], questions=questions or [], mode=mode)

    def test_unsupported_is_not_healthy(self):
        r=assess_health(self.report(), [dict(post_id=1,supported=False)])
        self.assertEqual(r['health'],'degraded')

    def test_review_and_unprocessed_are_not_success(self):
        for questions in ([],[dict(post_id=1,status='review')],
                          [dict(post_id=1,status='already_attempted',decision='abstained_review')]):
            self.assertEqual(assess_health(self.report(questions),[dict(post_id=1,supported=True)])['health'],'degraded')

    def test_submission_and_idle_are_distinct(self):
        self.assertEqual(assess_health(self.report(),[])['health'],'idle')
        r=assess_health(self.report([dict(post_id=1,status='submitted')]),[dict(post_id=1,supported=True)])
        self.assertEqual(r['health'],'ok')
        self.assertEqual(r['submitted_this_run'],1)

    def test_dry_run_is_never_counted_as_submission(self):
        r=assess_health(self.report([dict(post_id=1,status='dry_run')], 'dry-run'),[dict(post_id=1,supported=True)])
        self.assertEqual(r['health'],'ok')
        self.assertEqual(r['submitted_this_run'],0)

    def test_failed_health_gate_requires_real_valid_checkpoint(self):
        last=dict(run_number=1,status='completed',conclusion='failure')
        self.assertEqual(previous_run([last],2),last)
        # Recovery never bypasses archive validation, even after a failed run.
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(Exception):
                restore_zip(b'',Path(d),'1')
