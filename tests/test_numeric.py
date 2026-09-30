import asyncio
import json
from copy import deepcopy
from datetime import timedelta
from unittest import TestCase
from unittest.mock import patch, AsyncMock
from horizon.numeric import grid, standardize, validate_cdf, parse, anchors
from horizon.runner import process, Metaculus
from test_runner import RunnerTests
from test_horizon import NOW


def question(kind='numeric', count=200, low=0, high=100, zero=None, lower=False, upper=False):
    return {'type':kind,'scaling':{'range_min':low,'range_max':high,'zero_point':zero,
            'inbound_outcome_count':count}, 'open_lower_bound':lower,'open_upper_bound':upper}


class DistributionTests(TestCase):
    def test_linear_log_and_discrete_grids(self):
        self.assertEqual(grid(question(count=2)),[0,50,100])
        values = grid(question(count=2,low=1,high=100,zero=0))
        self.assertAlmostEqual(values[1],10)
        self.assertEqual(len(grid(question('discrete',count=8,low=-.5,high=7.5))),9)

    def test_closed_and_open_bounds_and_extreme_concentration(self):
        for lower in (False,True):
            for upper in (False,True):
                q = question(lower=lower,upper=upper)
                cdf = standardize([0]*100+[1]*101,q)
                validate_cdf(cdf,q)
                self.assertEqual(cdf[0], .001 if lower else 0)
                self.assertEqual(cdf[-1], .999 if upper else 1)
                self.assertLessEqual(max(b-a for a,b in zip(cdf,cdf[1:])),.2)

    def test_discrete_cdf_count_and_monotonicity(self):
        q=question('discrete',count=5)
        cdf=standardize([0,.1,.2,.7,.9,1],q)
        self.assertEqual(len(cdf),6)
        validate_cdf(cdf,q)

    def test_rejects_invalid_outputs_and_scaling(self):
        q=question(count=2)
        for bad in ([0,float('nan'),1],[0,.9,.2],[-.1,.5,1],[0,True,1],[1,1,1]):
            with self.assertRaises(ValueError): standardize(bad,q)
        with self.assertRaises(ValueError): grid(question(low=10,high=1))
        with self.assertRaises(ValueError): grid(question(low=-1,high=1,zero=0))

    def test_parse_preserves_order_requires_sources_and_interpolates(self):
        q=question()
        body={'cdf':[i/20 for i in range(21)],'source_ids':['a'],'rationale':'Test','can_forecast':True}
        cdf,_,_=parse(json.dumps(body),q,['a'])
        self.assertEqual(len(cdf),201)
        self.assertAlmostEqual(cdf[100],.5)
        body['source_ids']=['invented']
        with self.assertRaises(ValueError): parse(json.dumps(body),q,['a'])

    def test_numeric_submission_payload(self):
        from types import SimpleNamespace
        client=AsyncMock()
        client.post.return_value=SimpleNamespace(raise_for_status=lambda:None)
        asyncio.run(Metaculus(client).submit_numeric('42',[0,.5,1]))
        payload=client.post.call_args.kwargs['json'][0]
        self.assertIsNone(payload['probability_yes'])
        self.assertEqual(payload['continuous_cdf'],[0,.5,1])


class NumericRunnerTests(RunnerTests):
    def setUp(self):
        super().setUp()
        self.post['question'].update(question())
        self.forecast_mock=AsyncMock(return_value=([i/200 for i in range(201)],'Numeric rationale'))
        self.patch=patch('horizon.numeric.forecast',self.forecast_mock)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def test_full_chain_and_restart_prevents_duplicate(self):
        self.assertEqual(self.run_case(True),'submitted')
        self.api.submit_numeric.assert_awaited_once()
        self.api.submit.assert_not_called()
        self.assertEqual(self.run_case(True),'already_attempted')
        self.forecast_mock.assert_awaited_once()

    def test_ambiguous_submission_never_retries(self):
        self.api.submit_numeric.side_effect=TimeoutError()
        self.assertEqual(self.run_case(True),'submission_uncertain')
        self.assertEqual(self.run_case(True),'already_attempted')
        self.api.submit_numeric.assert_awaited_once()

    def test_changed_bounds_prevent_submission(self):
        fresh=deepcopy(self.post)
        fresh['question']['scaling']['range_max']=200
        self.api.detail.return_value=fresh
        self.assertEqual(self.run_case(True),'failed')
        self.api.submit_numeric.assert_not_called()

    def test_missing_auth_state_and_nonbinary_rejected(self):
        self.post['question'].pop('my_forecasts')
        with self.assertRaises(ValueError): self.run_case()

    def test_review_abstains_automatically(self):
        self.forecast_mock.side_effect=ValueError('No defensible forecast')
        self.assertEqual(self.run_case(True),'failed')
        self.api.submit_numeric.assert_not_called()
