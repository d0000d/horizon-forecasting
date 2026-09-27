from copy import deepcopy
from unittest import TestCase
from horizon.reliability import audit
from horizon.evaluation import paired_comparison


def row(qid, cluster, p=.8):
    return dict(question_id=qid, cluster=cluster, candidate=p, baseline=.5,
                outcome=1, cost_eur=.01, frozen_at='2026-01-01T00:00:00Z',
                resolved_at='2026-02-01T00:00:00Z')


class ReliabilityTests(TestCase):
    def test_abstention_cost_and_fallback_are_visible(self):
        result = audit([row('a','x'),row('b','y',None)])
        self.assertEqual(result['coverage'],.5)
        self.assertAlmostEqual(result['baseline_fallback_brier_delta'],-.105)
        self.assertEqual(result['cost_per_forecast_eur'],.02)
        self.assertIsNone(result['metrics']['brier']['cluster_ci95'])

    def test_related_questions_do_not_invent_independent_clusters(self):
        result = audit([row(str(i),'same') for i in range(30)])
        self.assertEqual(result['paired_clusters'],1)
        self.assertIsNone(result['metrics']['brier']['cluster_ci95'])

    def test_cluster_intervals_and_calibration(self):
        rows = [row('a','x'),row('b','y')]
        result = audit(rows)
        self.assertEqual(result,audit(rows))
        self.assertTrue(all(x<0 for x in result['metrics']['brier']['cluster_ci95']))
        bucket = result['calibration_bins'][0]
        self.assertEqual(bucket['observed_rate'],1)
        self.assertLess(bucket['wilson95'][0],.5)

    def test_reject_duplicate_leakage_and_invalid_missing_outcomes(self):
        original = row('a','x')
        with self.assertRaises(ValueError):
            audit([original,original])
        for field, value in [('frozen_at','2026-03-01T00:00:00Z'),
                             ('resolved_at','2026-02-01T00:00:00'),
                             ('cost_eur',float('nan')),('outcome',True)]:
            changed = deepcopy(original)
            changed[field] = value
            with self.assertRaises(ValueError):
                audit([changed])
        with self.assertRaises(ValueError):
            paired_comparison([(None,None,2)])

    def test_empty_data_does_not_claim_skill(self):
        result = audit([])
        self.assertIsNone(result['coverage'])
        self.assertIsNone(result['metrics']['brier']['delta'])
