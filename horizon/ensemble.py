from math import exp, log
from .models import AgentForecast, probability


def disagreement(agents: list[AgentForecast]) -> tuple[float, list[str]]:
    values = [a.probability for a in agents]
    return (max(values)-min(values) if values else 0.0,
            sorted({a.crux for a in agents}))


def pool(agents: list[AgentForecast], weights: dict[str, float],
         base_rate: float | None = None, market: float | None = None,
         prior_weight: float = 0.2, market_weight: float = 0.2) -> float:
    """Explicit experimental log pool. Self-reported confidence is not a weight."""
    if not agents:
        raise ValueError("No successful agents; abstain")
    if len({a.agent for a in agents}) != len(agents):
        raise ValueError("Duplicate forecaster identities")
    values = []
    for a in agents:
        w = weights.get(a.agent, 0)
        probability(w)
        if w:
            values.append((a.probability, w))
    if not values:
        raise ValueError("No positive agent weights")
    total = sum(w for _, w in values)
    values = [(p, w/total) for p, w in values]
    for p, w in ((base_rate, prior_weight), (market, market_weight)):
        probability(w)
        if p is not None and w:
            values.append((probability(p), w))
    def logit(p):
        p = min(1-1e-6, max(1e-6, p))
        return log(p/(1-p))
    z = sum(logit(p)*w for p,w in values)/sum(w for _,w in values)
    return probability(1/(1+exp(-z)))
