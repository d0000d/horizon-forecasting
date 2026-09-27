from unittest import TestCase
from horizon.evaluation import paired_comparison


class PairedEvaluationTests(TestCase):
    def test_better_forecasts_have_negative_delta(self):
        result = paired_comparison([(.8,.5,1),(.2,.5,0)])
        self.assertAlmostEqual(result['brier']['mean_delta'], -.21)
        self.assertTrue(all(x < 0 for x in result['brier']['ci95']))

    def test_missing_forecasts_are_visible(self):
        result = paired_comparison([(None,.5,1),(.5,None,0),(.5,.5,1)])
        self.assertEqual((result['total'],result['paired'],result['excluded']),(3,1,2))
        self.assertEqual(result['candidate_missing'],1)
        self.assertIsNone(result['brier']['ci95'])
        self.assertEqual(result['brier']['mean_delta'],0)

    def test_seed_reproducible_and_empty_explicit(self):
        rows = [(.7,.6,1),(.1,.3,1),(.2,.5,0)]
        self.assertEqual(paired_comparison(rows),paired_comparison(rows))
        self.assertIsNone(paired_comparison([])['log_loss']['mean_delta'])

    def test_invalid_observed_prediction_rejected(self):
        with self.assertRaises(ValueError):
            paired_comparison([(1.1,None,1)])
