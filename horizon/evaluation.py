"""Score frozen forecasts. This is not an LLM historical replay engine."""
from math import log
from statistics import mean
from random import Random
from .ensemble import pool
from .models import probability


def scores(p, outcome):
    probability(p)
    if type(outcome) is not int or outcome not in (0,1):
        raise ValueError("Binary outcome required")
    bounded = min(1-1e-15,max(1e-15,p))
    return {'brier':(p-outcome)**2,'log_loss':-log(bounded if outcome else 1-bounded)}


def paired_comparison(rows, *, samples=2000, seed=2026):
    """Question-level paired bootstrap of frozen binary predictions.

    Rows are (candidate, baseline, outcome). Missing predictions are excluded
    from paired scores and explicitly counted; negative deltas favor candidate.
    Correlated question families require a separate cluster-bootstrap analysis.
    """
    if type(samples) is not int or samples < 100:
        raise ValueError('Use at least 100 bootstrap samples')
    rows = list(rows)
    deltas = {metric: [] for metric in ('brier', 'log_loss')}
    for candidate, baseline, outcome in rows:
        scores(.5, outcome)  # Missing pairs must still have valid outcomes.
        # Validate observed values even when the pair is incomplete.
        left = scores(candidate, outcome) if candidate is not None else None
        right = scores(baseline, outcome) if baseline is not None else None
        if left is not None and right is not None:
            for metric in deltas:
                deltas[metric].append(left[metric]-right[metric])
    n = len(deltas['brier'])
    result = {'total': len(rows), 'paired': n, 'excluded': len(rows)-n,
              'candidate_missing': sum(row[0] is None for row in rows),
              'baseline_missing': sum(row[1] is None for row in rows),
              'method': 'paired question bootstrap; negative delta favors candidate'}
    rng = Random(seed)
    draws = {metric: [] for metric in deltas}
    if n >= 2:
        for _ in range(samples):
            indices = [rng.randrange(n) for _ in range(n)]
            for metric, values in deltas.items():
                draws[metric].append(mean(values[i] for i in indices))
    for metric, values in deltas.items():
        ordered = sorted(draws[metric])
        result[metric] = {'mean_delta': mean(values) if n else None,
                          'ci95': [ordered[int(.025*(samples-1))],
                                   ordered[int(.975*(samples-1))]] if ordered else None}
    return result


def ablate(rows, weights):
    """Paired complete-case comparison; no training or outcome-fed prompts."""
    result = {name:[] for name in ('council','without_base_rate','without_market','without_skeptic','baseline')}
    for record, outcome, baseline in rows:
        if record.raw is None or baseline is None:
            continue
        prior = record.base_rate.probability if record.base_rate else None
        market = record.market.probability if record.market else None
        for name in result:
            agents = [a for a in record.agents if name!='without_skeptic' or a.agent!='skeptic']
            if not agents:
                raise ValueError("Ablation lacks a usable paired forecast")
            p = baseline if name=='baseline' else pool(agents,weights,
                None if name=='without_base_rate' else prior,None if name=='without_market' else market)
            result[name].append(scores(p,outcome))
    return {name:{'n':len(values),**{metric:mean(v[metric] for v in values) if values else None
                                  for metric in ('brier','log_loss')}} for name,values in result.items()}
