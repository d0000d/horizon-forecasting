import asyncio
from unittest import TestCase
from unittest.mock import AsyncMock, Mock
from horizon.openrouter_provider import OpenRouterProvider


class OpenRouterTests(TestCase):
    def make(self, data):
        client = AsyncMock()
        client.post.return_value = Mock(json=lambda:data)
        provider = OpenRouterProvider(model='test/model',input_usd_per_million=1,
            output_usd_per_million=2,eur_per_usd_upper_bound=1.1,
            approved=True,client=client)
        return provider, client

    def test_billed_usage_and_pinned_request(self):
        provider, client = self.make({'model':'test/model','usage':{
            'cost':.001,'prompt_tokens':100,'completion_tokens':200},
            'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]})
        reply = asyncio.run(provider.complete('JSON please'))
        self.assertAlmostEqual(reply.cost_eur,.0011)
        self.assertEqual(reply.text,'{}')
        request = client.post.call_args.kwargs['json']
        self.assertFalse(request['provider']['allow_fallbacks'])
        self.assertEqual(request['max_tokens'],2000)
        self.assertGreater(provider.quote_eur('JSON please'),reply.cost_eur)

    def test_missing_usage_does_not_invent_zero_cost(self):
        provider, _ = self.make({'usage':{'prompt_tokens':1,'completion_tokens':2}})
        with self.assertRaises(ValueError):
            asyncio.run(provider.complete('JSON'))

    def test_truncated_output_preserves_billed_cost(self):
        provider, _ = self.make({'usage':{'cost':.01,'prompt_tokens':1,'completion_tokens':2},
            'choices':[{'finish_reason':'length','message':{'content':'{"unfinished"'}}]})
        reply = asyncio.run(provider.complete('JSON'))
        self.assertEqual(reply.text,'')
        self.assertAlmostEqual(reply.cost_eur,.011)
