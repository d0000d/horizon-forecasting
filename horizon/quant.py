from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from statistics import mean
from typing import Protocol
from .models import aware, probability


@dataclass(frozen=True)
class Observation:
    timestamp: datetime
    available_at: datetime
    value: float


class QuantBackend(Protocol):
    def forecast(self, observations: list[Observation], as_of: datetime, steps: int) -> dict[str,list[float]]: ...


def baselines(observations, as_of, steps=1):
    aware(as_of)
    if not isinstance(steps,int) or isinstance(steps,bool) or steps < 1 or len(observations)<3:
        raise ValueError("Need >=3 real observations and a positive step count")
    for o in observations:
        if max(aware(o.timestamp),aware(o.available_at)) > as_of or not isfinite(o.value):
            raise ValueError("Invalid/future observation")
    times = [o.timestamp for o in observations]
    gaps = [(b-a).total_seconds() for a,b in zip(times,times[1:])]
    if min(gaps)<=0 or max(gaps)!=min(gaps):
        raise ValueError("Regular increasing time series required")
    y = [o.value for o in observations]
    n = len(y); center = (n-1)/2
    slope = sum((i-center)*(v-mean(y)) for i,v in enumerate(y))/sum((i-center)**2 for i in range(n))
    level = y[0]
    for v in y[1:]:
        level = 0.3*v+0.7*level
    return {'no_change':[y[-1]]*steps,'moving_average':[mean(y[-3:])]*steps,
            'linear_trend':[mean(y)+slope*(n+i-center) for i in range(steps)],
            'exponential_smoothing':[level]*steps}


def validate_quantiles(pairs):
    if len(pairs)<2:
        raise ValueError("Need multiple quantiles")
    for p,v in pairs:
        probability(p)
        if not 0<p<1 or not isfinite(v):
            raise ValueError("Invalid quantile")
    if any(p>=q or x>y for (p,x),(q,y) in zip(pairs,pairs[1:])):
        raise ValueError("Quantiles must have increasing levels and nondecreasing values")
    return pairs
