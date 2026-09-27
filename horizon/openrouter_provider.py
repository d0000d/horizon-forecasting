"""Explicit OpenRouter transport; never falls back to a personal OpenAI key."""
from decimal import Decimal
import os
from .council import Reply


class OpenRouterProvider:
    def __init__(self, *, model, input_usd_per_million, output_usd_per_million,
                 eur_per_usd_upper_bound, approved=False, max_output_tokens=2000,
                 max_input_bytes=16000, client=None):
        if not approved:
            raise PermissionError('Model API use requires approval')
        if not isinstance(model,str) or '/' not in model or model.startswith('~') or ':online' in model:
            raise ValueError('Explicit model ID required; no aliases or search plugins')
        self.model = model
        self.input_rate = Decimal(str(input_usd_per_million))
        self.output_rate = Decimal(str(output_usd_per_million))
        self.fx = Decimal(str(eur_per_usd_upper_bound))
        if any(not v.is_finite() or v <= 0 for v in (self.input_rate,self.output_rate,self.fx)):
            raise ValueError('Verified positive price bounds required')
        if type(max_output_tokens) is not int or not 256 <= max_output_tokens <= 4000:
            raise ValueError('Invalid output limit')
        if type(max_input_bytes) is not int or max_input_bytes <= 0:
            raise ValueError('Invalid input limit')
        self.max_output_tokens = max_output_tokens
        self.max_input_bytes = max_input_bytes
        if client is None:
            import httpx
            key = os.environ.get('OPENROUTER_API_KEY')
            if not key or key == 'REPLACE_ME':
                raise ValueError('OPENROUTER_API_KEY missing')
            client = httpx.AsyncClient(base_url='https://openrouter.ai/api/v1/',
                headers={'Authorization':'Bearer '+key}, timeout=60, follow_redirects=False)
        self.client = client

    def quote_eur(self, prompt):
        size = len(prompt.encode('utf-8'))
        if size > self.max_input_bytes:
            raise ValueError('Input too large')
        return float(((size+1024)*self.input_rate+self.max_output_tokens*self.output_rate)*self.fx/1_000_000)

    async def complete(self, prompt):
        self.quote_eur(prompt)
        response = await self.client.post('chat/completions', json={
            'model':self.model, 'messages':[{'role':'user','content':prompt}],
            'max_tokens':self.max_output_tokens, 'stream':False,
            'response_format':{'type':'json_object'},
            'provider':{'allow_fallbacks':False, 'require_parameters':True,
                        'max_price':{'prompt':float(self.input_rate),
                                     'completion':float(self.output_rate)}}})
        response.raise_for_status()
        data = response.json()
        usage = data.get('usage') or {}
        # Missing billable cost keeps Council's full reservation outstanding.
        cost = usage.get('cost')
        counts = [usage.get('prompt_tokens'),usage.get('completion_tokens')]
        if cost is None or isinstance(cost,bool) or any(type(v) is not int or v < 0 for v in counts):
            raise ValueError('Missing or invalid usage')
        usd = Decimal(str(cost))
        if not usd.is_finite() or usd < 0:
            raise ValueError('Invalid billed cost')
        choice = (data.get('choices') or [{}])[0]
        content = (choice.get('message') or {}).get('content')
        valid = choice.get('finish_reason') == 'stop' and isinstance(content,str)
        return Reply(content if valid else '', data.get('model') or self.model,
                     float(usd*self.fx), *counts)

    async def close(self):
        await self.client.aclose()
