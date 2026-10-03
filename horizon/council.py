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
                 select_evidence=False, role_providers=None, clock=None, auto_resolve=False):
        self.provider, self.budget, self.memory, self.run_id = provider,budget,memory,run_id
        self.calibration = calibration or IdentityCalibration()
        self.threshold, self.timeout = probability(threshold), timeout
        if timeout <= 0 or red_team_mode not in ("off", "adaptive", "always"):
            raise ValueError("Invalid Council configuration")
        self.weights = weights or {"outside":0.4,"inside":0.4,"skeptic":0.2}
        self.red_team_mode, self.analyze_resolution = red_team_mode, analyze_resolution
        self.select_evidence = select_evidence
        self.auto_resolve = auto_resolve
        self.role_providers = dict(role_providers or {})
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def forecast(self, question, evidence=(), base_rate=None, market=None, *, snapshots=()):
        from .agents import role_prompt, parse_analysis, parse_research, parse_red_team, parse_selection, parse_adjudication
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
        selection = None

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

        for role in ('outside','inside','skeptic'):
            if not fatal_budget and not halted:
                await forecaster(role)
        spread, cruxes = disagreement(agents)
        # Three blind forecasts on every question; adversarial review remains separate.
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
        codes = []
        if spread >= self.threshold: codes.append('forecast_disagreement')
        if len(agents) < 2: codes.append('insufficient_forecasters')
        if tail: codes.append('extreme_probability')
        if any(e['status'] != 'success' for e in events): codes.append('stage_failure')
        if not evidence and self.select_evidence: codes.append('no_relevant_evidence')
        concerns = []
        if resolution:
            concerns += [{'concern_id': 'criteria-'+str(i), 'issue': issue}
                         for i, issue in enumerate(resolution['ambiguities'])]
            if resolution['needs_review'] and not resolution['ambiguities']:
                codes.append('unexplained_resolution_flag')
        if selection:
            concerns += [{'concern_id': 'missing-'+str(i), 'issue': issue}
                         for i, issue in enumerate(selection['missing_information'])]
        if audit:
            for i, finding in enumerate(audit['findings']):
                if finding['severity'] in ('high', 'critical'):
                    codes.append('serious_audit_finding')
                elif finding['severity'] == 'medium':
                    concerns.append({'concern_id': 'audit-'+str(i), 'issue': finding['issue']})
        review = spread>=self.threshold or len(agents)<2 or tail or flagged or stage_failed
        adjudication = None
        # One bounded pass within the original attempt, never retry until an answer passes.
        if self.auto_resolve and review and concerns and not codes and raw is not None and evidence:
            context = {'concerns': concerns, 'evidence': evidence}
            adjudication = await invoke('adjudicator', role_prompt('adjudicator', question, context),
                lambda r: parse_adjudication(r.text, concerns, evidence, question))
            if adjudication and all(d['resolved'] for d in adjudication['decisions']):
                review = False
            else:
                codes.append('unresolved_concerns')
        elif concerns:
            codes.append('unresolved_concerns')
        if review and not codes:
            codes.append('incomplete_analysis')
        if fatal_budget or halted or (self.auto_resolve and self.clock() >= (question.close_time or question.deadline)):
            raw = calibrated = None
            codes.append('budget_or_deadline_stop')
        status = 'abstain' if raw is None else ('review' if review else 'ok')
        record = ForecastRecord(question,analyze(question),agents,evidence,base_rate,market,raw,calibrated,
                                spread,cruxes,status,events,calibration_version=self.calibration.version,
                                research=research,resolution_analysis=resolution,red_team=audit)
        record.decision_codes = sorted(set(codes))
        record.adjudication = adjudication
        self.memory.save(record)
        return record
