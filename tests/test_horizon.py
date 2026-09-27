import asyncio
import json
import math
from uuid import uuid4
import unittest
import sys
import types
from unittest.mock import AsyncMock, patch
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from horizon.analysis import analyze, deduplicate
from horizon.budget import Budget, BudgetExceeded
from horizon.council import Council, Reply
from horizon.ensemble import pool, disagreement
from horizon.evaluation import ablate, scores
from horizon.memory import Memory
from horizon.models import AgentForecast, Evidence, Question, Signal, probability
from horizon.openai_provider import OpenAIProvider
from horizon.prompts import parse_forecast
from horizon.quant import Observation, baselines, validate_quantiles

NOW = datetime(2026,9,15,tzinfo=timezone.utc)
Q = Question('1','Will CPI exceed 3%?', 'YES if published CPI exceeds 3% on October 15.',NOW,NOW+timedelta(days=30),units='%')


def agent(name,p):
    return AgentForecast(name,p,0.8,('driver',),'counter','crux','mock')


def payload(p):
    return json.dumps(dict(probability=p,confidence=0.5,drivers=['driver'],counterargument='counter',crux='crux'))


class Fake:
    max_output_tokens = 1000
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = 0
    def quote_eur(self,prompt):
        return 0.01
    async def complete(self,prompt):
        self.calls += 1
        v = next(self.replies)
        if isinstance(v,Exception):
            raise v
        if v == 'timeout':
            await asyncio.sleep(1)
        return Reply(v,'mock',0.005,10,20)


class CoreTests(unittest.TestCase):
    def test_probabilities_reject_invalid(self):
        for p in (-0.1,1.1,float('nan'),float('inf'),True,'0.5'):
            with self.subTest(p=p),self.assertRaises(ValueError): probability(p)

    def test_parser_failures(self):
        for text in ('','{}','null','[]','text',payload(2),payload(float('nan')),
                     payload(0.5).replace('"crux": "crux"','"crux": "a", "crux": "b"')):
            with self.subTest(text=text),self.assertRaises((ValueError,TypeError)):
                parse_forecast(text,'inside','mock')

    def test_parser_success(self):
        self.assertEqual(parse_forecast(payload(.61),'inside','mock').probability,.61)

    def test_log_pool_known_result(self):
        self.assertAlmostEqual(pool([agent('a',.2),agent('b',.8)],{'a':.5,'b':.5}),.5)
        self.assertAlmostEqual(pool([agent('a',.2),agent('b',.8)],{'a':1,'b':0}),.2)

    def test_market_shrinkage(self):
        self.assertGreater(pool([agent('a',.1)],{'a':1},market=.7),.1)

    def test_ensemble_invalid(self):
        for agents,weights in (([],{}),([agent('a',.4)],{'a':float('nan')}),
                               ([agent('a',.4),agent('a',.5)],{'a':1})):
            with self.assertRaises(ValueError): pool(agents,weights)

    def test_disagreement(self):
        self.assertAlmostEqual(disagreement([agent('a',.22),agent('b',.84)])[0],.62)

    def test_question_timezone_and_deadline(self):
        with self.assertRaises(ValueError): Question('1','x','c',NOW.replace(tzinfo=None),NOW)
        with self.assertRaises(ValueError): Question('1','x','c',NOW,NOW)

    def test_routing_hash(self):
        self.assertEqual(analyze(Q).domain,'economics')
        self.assertEqual(analyze(Q).horizon_days,30)
        self.assertEqual(len(Q.criteria_hash),64)

    def test_dedup_and_future_evidence(self):
        a = Evidence('1','Reuters report','https://one.test/a',NOW,NOW,origin_id='reuters-1')
        b = Evidence('2','Syndicated rewrite','https://two.test/b',NOW,NOW,origin_id='reuters-1')
        self.assertEqual(len(deduplicate([a,b],Q)),1)
        c = Evidence('3','Future','https://three.test',NOW,NOW+timedelta(seconds=1))
        with self.assertRaises(ValueError): deduplicate([c],Q)

    def test_dedup_transitive_bridge(self):
        items = [Evidence('1','a','https://x.test/1',NOW,NOW,origin_id='a'),
                 Evidence('2','b','https://x.test/2',NOW,NOW,origin_id='b'),
                 Evidence('3','b','https://x.test/1',NOW,NOW,origin_id='c')]
        self.assertEqual(len(deduplicate(items,Q)),1)

    def test_openai_request_contract_without_network(self):
        response = types.SimpleNamespace(usage=types.SimpleNamespace(input_tokens=100,output_tokens=200),
                                         output_text=payload(.5),status='completed',model='mock-snapshot')
        create = AsyncMock(return_value=response)
        factory = unittest.mock.Mock(return_value=types.SimpleNamespace(responses=types.SimpleNamespace(create=create)))
        module = types.SimpleNamespace(AsyncOpenAI=factory)
        with patch.dict(sys.modules,{'openai':module}):
            provider = OpenAIProvider(model='mock',input_usd_per_million=.25,
                output_usd_per_million=2,eur_per_usd_upper_bound=1,approved=True)
            reply = asyncio.run(provider.complete('JSON test'))
            self.assertAlmostEqual(reply.cost_eur,.000425)
            self.assertGreater(provider.quote_eur('JSON test'),reply.cost_eur)
            self.assertEqual(factory.call_args.kwargs['max_retries'],0)
            self.assertEqual(create.call_args.kwargs['max_output_tokens'],2000)
            self.assertFalse(create.call_args.kwargs['store'])
            response.status = 'incomplete'
            self.assertEqual(asyncio.run(provider.complete('JSON test')).text,'')

    def test_quant_baselines(self):
        series = [Observation(NOW-timedelta(days=2-i),NOW,float(i+1)) for i in range(3)]
        result = baselines(series,NOW,2)
        self.assertEqual(result['no_change'],[3,3])
        self.assertEqual(result['linear_trend'],[4,5])
        self.assertEqual(result['moving_average'],[2,2])

    def test_quant_invalid_and_quantiles(self):
        with self.assertRaises(ValueError): baselines([],NOW)
        with self.assertRaises(ValueError): validate_quantiles([(.1,5),(.9,4)])
        self.assertEqual(validate_quantiles([(.1,4),(.9,5)]),[(.1,4),(.9,5)])

    def test_scoring(self):
        self.assertEqual(scores(.5,1)['brier'],.25)
        self.assertAlmostEqual(scores(.5,0)['log_loss'],math.log(2))
        self.assertTrue(math.isfinite(scores(0,1)['log_loss']))

    def test_live_adapter_requires_permission_before_import(self):
        with self.assertRaises(PermissionError):
            OpenAIProvider(model='unused',input_usd_per_million=1,output_usd_per_million=2,eur_per_usd_upper_bound=1)


class StatefulTests(unittest.TestCase):
    def setUp(self):
        scratch = Path('work/test-temp').resolve()
        scratch.mkdir(parents=True,exist_ok=True)
        self.path = scratch / str(uuid4())
        self.path.mkdir()
        self.budget = Budget(self.path/'budget.sqlite')
        self.memory = Memory(self.path/'memory.sqlite')
    def tearDown(self):
        for file in self.path.iterdir():
            file.unlink()
        self.path.rmdir()
    def forecast(self,replies,**kwargs):
        provider = Fake(replies)
        council = Council(provider,self.budget,self.memory,'run',timeout=.01,red_team_mode='off')
        return asyncio.run(council.forecast(Q,**kwargs)),provider

    def test_reservations_survive_restart(self):
        self.budget.reserve('run','1',.03)
        restarted = Budget(self.path/'budget.sqlite')
        with self.assertRaises(BudgetExceeded): restarted.reserve('run','1',.03)

    def test_atomic_reservations(self):
        def reserve(i):
            try: self.budget.reserve('run','1',.02); return 1
            except BudgetExceeded: return 0
        with ThreadPoolExecutor(max_workers=5) as pool_:
            self.assertEqual(sum(pool_.map(reserve,range(5))),2)

    def test_settlement_unknown_and_overshoot(self):
        ticket = self.budget.reserve('run','1',.01)
        with self.assertRaises(BudgetExceeded): self.budget.settle(ticket,.02)
        self.assertAlmostEqual(self.budget.used_eur(),.02)
        with self.assertRaises(ValueError): self.budget.settle(ticket,0)

    def test_run_limit(self):
        budget = Budget(self.path/'budget2.sqlite',run_eur=.04,question_eur=.03)
        budget.reserve('run','a',.03)
        with self.assertRaises(BudgetExceeded): budget.reserve('run','b',.02)

    def test_total_limit_across_runs(self):
        budget = Budget(self.path/'budget3.sqlite',total_eur=.04,run_eur=.03,question_eur=.03)
        budget.reserve('run1','a',.03)
        with self.assertRaises(BudgetExceeded): budget.reserve('run2','a',.02)

    def test_simple_two_calls_and_persistence(self):
        record,provider = self.forecast([payload(.52),payload(.56)])
        self.assertEqual(provider.calls,2)
        self.assertEqual(record.status,'ok')
        self.assertEqual(record.raw,record.calibrated)
        self.assertEqual(self.memory.records()[0]['question']['id'],'1')
        self.assertAlmostEqual(self.budget.used_eur(),.01)

    def test_hard_three_calls(self):
        record,provider = self.forecast([payload(.22),payload(.84),payload(.6)])
        self.assertEqual(provider.calls,3)
        self.assertEqual(record.status,'review')

    def test_partial_failure_isolated(self):
        record,provider = self.forecast(['',payload(.4),payload(.45)])
        self.assertIsNotNone(record.raw)
        self.assertEqual(provider.calls,3)
        self.assertEqual(record.events[0]['error'],'ValueError')

    def test_all_failed_abstains(self):
        record,_ = self.forecast(['', 'malformed', '{}'])
        self.assertEqual(record.status,'abstain')
        self.assertIsNone(record.raw)
        self.assertAlmostEqual(self.budget.used_eur(),.015)

    def test_timeout_and_sensitive_error(self):
        record,_ = self.forecast(['timeout',RuntimeError('SECRET MUST NEVER BE SAVED'),payload(.5)])
        self.assertEqual(record.status,'review')
        self.assertNotIn('SECRET',json.dumps(self.memory.records()))
        self.assertAlmostEqual(self.budget.used_eur(),.025)

    def test_future_market_stops_before_calls(self):
        market = Signal(.6,NOW+timedelta(seconds=1),'source','reason')
        with self.assertRaises(ValueError): self.forecast([],market=market)

    def test_paired_ablation(self):
        record,_ = self.forecast([payload(.52),payload(.56)])
        results = ablate([(record,1,.5)],{'outside':.4,'inside':.4,'skeptic':.2})
        self.assertEqual(results['baseline']['n'],1)
        self.assertEqual(results['baseline']['brier'],.25)


if __name__=='__main__':
    unittest.main()
