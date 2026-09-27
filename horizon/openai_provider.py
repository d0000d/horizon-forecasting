"""Explicit opt-in adapter; never constructed by the offline CLI/tests."""
from decimal import Decimal
from .council import Reply


class OpenAIProvider:
    def __init__(self, *, model, input_usd_per_million, output_usd_per_million,
                 eur_per_usd_upper_bound, approved=False, max_output_tokens=2000,
                 max_input_bytes=16000):
        if not approved:
            raise PermissionError("Paid API use requires explicit user approval")
        self.model = model
        self.input_rate = Decimal(str(input_usd_per_million))
        self.output_rate = Decimal(str(output_usd_per_million))
        self.fx = Decimal(str(eur_per_usd_upper_bound))
        if any(not x.is_finite() or x<=0 for x in (self.input_rate,self.output_rate,self.fx)):
            raise ValueError("Verified positive prices and conservative FX bound required")
        if type(max_output_tokens) is not int or not 256<=max_output_tokens<=4000:
            raise ValueError("Output cap must be 256..4000")
        self.max_output_tokens = max_output_tokens
        self.max_input_bytes = max_input_bytes
        from openai import AsyncOpenAI
        self.client = AsyncOpenAI(max_retries=0,timeout=60)

    def quote_eur(self, prompt):
        n = len(prompt.encode('utf-8'))
        if n>self.max_input_bytes:
            raise ValueError("Input too large; do not silently truncate resolution criteria")
        # Conservative text token bound plus framing allowance; no tools/images.
        return float(((n+1024)*self.input_rate+self.max_output_tokens*self.output_rate)*self.fx/1_000_000)

    async def complete(self, prompt):
        self.quote_eur(prompt)
        response = await self.client.responses.create(
            model=self.model,input=prompt,max_output_tokens=self.max_output_tokens,
            reasoning={"effort":"low"},text={"format":{"type":"json_object"}},store=False,
        )
        usage = response.usage
        if usage is None:
            raise ValueError("Missing usage; retain full reservation")
        cost = (usage.input_tokens*self.input_rate+usage.output_tokens*self.output_rate)*self.fx/1_000_000
        # Incomplete output stays invalid, but its billable usage is retained.
        text = response.output_text if response.status=='completed' else ''
        return Reply(text,response.model,float(cost),usage.input_tokens,usage.output_tokens)
