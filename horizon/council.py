import asyncio
from dataclasses import dataclass
from time import monotonic
from datetime import datetime, timezone
from typing import Protocol
from .analysis import analyze, deduplicate
from .budget import BudgetExceeded
from .ensemble import disagreement, pool
from .models import ForecastRecord, IdentityCalibration, probability
from .prompts import forecast_prompt, parse_forecast


@dataclass(frozen=True)
class Reply:
    text: str
    model: str
    cost_eur: float
    input_tokens: int
    output_tokens: int


class Provider(Protocol):
    max_output_tokens: int
    def quote_eur(self, prompt: str) -> float: ...
    async def complete(self, prompt: str) -> Reply: ...


class Council:
    def __init__(self, provider, budget, memory, run_id, *, calibration=None,
                 threshold=0.25, timeout=60, weights=None,
                 red_team_mode="adaptive", analyze_resolution=False,
                 select_evidence=False, role_providers=None, clock=None):
        self.provider, self.budget, self.memory, self.run_id = provider,budget,memory,run_id
        self.calibration = calibration or IdentityCalibration()
        self.threshold, self.timeout = probability(threshold), timeout
        if timeout <= 0 or red_team_mode not in ("off", "adaptive", "always"):
            raise ValueError("Invalid Council configuration")
        self.weights = weights or {"outside":0.4,"inside":0.4,"skeptic":0.2}
        self.red_team_mode, self.analyze_resolution = red_team_mode, analyze_resolution
        self.select_evidence = select_evidence
        self.role_providers = dict(role_providers or {})
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def forecast(self, question, evidence=(), base_rate=None, market=None, *, snapshots=()):
        from .agents import role_prompt, parse_analysis, parse_research, parse_red_team, parse_selection
        from .memory import encode
        if question.kind != 'binary':
            raise ValueError("Council V1 supports binary only")
        for signal in (base_rate, market):
            if signal and signal.available_at > question.as_of:
                raise ValueError("Future signal rejected")
        if len(snapshots)>4 or len({s.id for s in snapshots})!=len(snapshots):
            raise ValueError("Invalid source snapshot set")
        for source in snapshots:
            if max(source.fetched_at, source.published_at or source.fetched_at)>question.as_of:
                raise ValueError("Future source rejected before any API call")
        evidence = deduplicate(list(evidence), question)
        agents, events = [], []
        fatal_budget = False
        research = resolution = audit = None
        stage_failed = False
        halted = False

        async def invoke(role,prompt,parser):
            nonlocal fatal_budget, halted
            start = monotonic()
            event = {"agent":role,"status":"failed","prompt_version":"council-v2"}
            try:
                remaining = ((question.close_time or question.deadline)-self.clock()).total_seconds()
                if remaining <= 0:
                    halted = True
                    event['error'] = 'DeadlineExceeded'
                    return None
                provider = self.role_providers.get(role, self.provider)
                quote = provider.quote_eur(prompt)
                ticket = self.budget.reserve(self.run_id,question.id,quote)
                event.update(reservation_id=ticket,reserved_eur=quote)
                reply = await asyncio.wait_for(provider.complete(prompt),min(self.timeout,remaining))
                event.update(model=reply.model,cost_eur=reply.cost_eur,input_tokens=reply.input_tokens,
                             output_tokens=reply.output_tokens)
                self.budget.settle(ticket,reply.cost_eur)
                result = parser(reply)
                event['status'] = 'success'
                return result
            except BudgetExceeded:
                fatal_budget = True
                event['error'] = 'BudgetExceeded'
            except Exception as exc:
                event['error'] = type(exc).__name__
            finally:
                event['latency_seconds'] = monotonic()-start
                events.append(event)
            return None

        if self.analyze_resolution:
            resolution = await invoke('analyst',role_prompt('analyst',question,{}),
                                      lambda r:parse_analysis(r.text))
            stage_failed |= resolution is None
        if snapshots and not fatal_budget and not halted:
            researched = await invoke('research',role_prompt('research',question,list(snapshots)),
                                      lambda r:parse_research(r.text,snapshots,question))
            if researched is not None:
                research,new_evidence = researched
                evidence = deduplicate(evidence+new_evidence,question)
            stage_failed |= researched is None or not evidence

        if self.select_evidence and not fatal_budget and not halted:
            if not evidence:
                halted = True
                events.append({'agent':'selector','status':'failed','error':'NoEvidence'})
            else:
                selection = await invoke('selector', role_prompt('selector',question,evidence),
                                         lambda r:parse_selection(r.text,evidence))
                research = {'source_research':research, 'selection':selection}
                if selection is None or not selection['selected_ids']:
                    halted = True
                else:
                    evidence = [e for e in evidence if e.id in selection['selected_ids']]
                    stage_failed |= bool(selection['missing_information'])

        async def forecaster(role):
            # The forecasters never see each other's initial probabilities.
            prompt = forecast_prompt(role,question,evidence,base_rate)
            if resolution:
                prompt += '\nResolution analysis (verify against original criteria): '+encode(resolution)
            result = await invoke(role,prompt,lambda r:parse_forecast(r.text,role,r.model))
            if result is not None:
                agents.append(result)

        for role in ('outside','inside'):
            if not fatal_budget and not halted:
                await forecaster(role)
        spread, cruxes = disagreement(agents)
        # The skeptic only recovers missing forecasts; adversarial review is a separate role.
        if not fatal_budget and not halted and (len(agents)<2 or (self.red_team_mode=='off' and spread>=self.threshold)):
            await forecaster('skeptic')
        spread, cruxes = disagreement(agents)
        extreme = any(a.probability<=.05 or a.probability>=.95 for a in agents)
        needs_audit = self.red_team_mode=='always' or (
            self.red_team_mode=='adaptive' and (spread>=self.threshold or extreme or len(agents)<2))
        if agents and needs_audit and not fatal_budget and not halted:
            context = {'agents':agents,'evidence':evidence,'base_rate':base_rate,'resolution_analysis':resolution}
            audit = await invoke('red_team',role_prompt('red_team',question,context),
                                 lambda r:parse_red_team(r.text,evidence))
            stage_failed |= audit is None
        raw = pool(agents,self.weights,base_rate.probability if base_rate else None,
                   market.probability if market else None) if agents and not fatal_budget and not halted else None
        if self.clock() >= (question.close_time or question.deadline):
            raw = None
            events.append({'agent':'deadline_guard','status':'failed','error':'DeadlineExceeded'})
        calibrated = probability(self.calibration.transform(raw)) if raw is not None else None
        tail = calibrated is not None and (calibrated<=.05 or calibrated>=.95)
        flagged = bool(audit and any(f['severity'] in ('medium','high','critical') for f in audit['findings']))
        flagged |= bool(resolution and (resolution['needs_review'] or resolution['ambiguities']))
        review = spread>=self.threshold or len(agents)<2 or tail or flagged or stage_failed
        status = 'abstain' if raw is None else ('review' if review else 'ok')
        record = ForecastRecord(question,analyze(question),agents,evidence,base_rate,market,raw,calibrated,
                                spread,cruxes,status,events,calibration_version=self.calibration.version,
                                research=research,resolution_analysis=resolution,red_team=audit)
        self.memory.save(record)
        return record
