"""Audit frozen binary predictions, including abstentions and related events.

Input JSON rows: question_id, cluster, frozen_at, resolved_at, outcome,
candidate (nullable), baseline (nullable), cost_eur. One row per eligible question.
The caller must supply genuine pre-outcome archives and a complete cohort.
"""
import argparse
import json
from datetime import datetime
from math import isfinite, sqrt
from random import Random
from statistics import mean
from pathlib import Path
from .evaluation import scores


def audit(rows, *, samples=2000, seed=2026):
    if type(samples) is not int or samples < 100:
        raise ValueError('At least 100 resamples required')
    rows = list(rows)
    seen, groups, bins = set(), {}, [[] for _ in range(10)]
    paired, operational, costs, candidate_scores = [], [], [], []
    for row in rows:
        qid = row['question_id']
        if not isinstance(qid, str) or not qid or qid in seen:
            raise ValueError('Unique nonempty question IDs required')
        seen.add(qid)
        cluster = row['cluster']
        if not isinstance(cluster, str) or not cluster:
            raise ValueError('Predeclared event cluster required')
        dates = [datetime.fromisoformat(row[k].replace('Z', '+00:00'))
                 for k in ('frozen_at', 'resolved_at')]
        if any(d.utcoffset() is None for d in dates) or dates[0] >= dates[1]:
            raise ValueError('Forecast must be frozen before resolution with timezone')
        p, b, y = row['candidate'], row['baseline'], row['outcome']
        scores(.5, y)
        cs = scores(p, y) if p is not None else None
        bs = scores(b, y) if b is not None else None
        cost = row['cost_eur']
        if isinstance(cost, bool) or not isinstance(cost, (float,int)) or not isfinite(cost) or cost < 0:
            raise ValueError('Finite nonnegative cost required')
        costs.append(cost)
        if p is not None:
            candidate_scores.append(cs)
            bins[min(9, int(p*10))].append((p,y))
        if bs is not None:
            # Analytical deployment policy: baseline substitutes for abstention.
            operational.append((cs or bs)['brier']-bs['brier'])
        if cs is not None and bs is not None:
            delta = {k:cs[k]-bs[k] for k in cs}
            paired.append(delta)
            groups.setdefault(cluster, []).append(delta)
    rng, draws = Random(seed), {k:[] for k in ('brier','log_loss')}
    clusters = list(groups)
    if len(clusters) >= 2:
        for _ in range(samples):
            selected = [d for _ in clusters for d in groups[rng.choice(clusters)]]
            for metric in draws:
                draws[metric].append(mean(d[metric] for d in selected))
    metrics = {}
    for metric, values in draws.items():
        values.sort()
        metrics[metric] = {'delta':mean(d[metric] for d in paired) if paired else None,
            'cluster_ci95':[values[int(.025*(samples-1))],values[int(.975*(samples-1))]] if values else None}
    reliability = []
    for index, bucket in enumerate(bins):
        if not bucket:
            continue
        n, rate, z = len(bucket), mean(y for _,y in bucket), 1.95996398454
        center = (rate+z*z/(2*n))/(1+z*z/n)
        half = z*sqrt(rate*(1-rate)/n+z*z/(4*n*n))/(1+z*z/n)
        reliability.append({'bin':index,'n':n,'mean_forecast':mean(p for p,_ in bucket),
                            'observed_rate':rate,'wilson95':[center-half,center+half]})
    covered = sum(row['candidate'] is not None for row in rows)
    return {'eligible':len(rows),'forecasted':covered,'coverage':covered/len(rows) if rows else None,
            'candidate_scores':{k:mean(s[k] for s in candidate_scores) if candidate_scores else None
                                for k in ('brier','log_loss')},
            'paired':len(paired),'paired_clusters':len(groups),'metrics':metrics,
            'baseline_fallback_brier_delta':mean(operational) if operational else None,
            'baseline_fallback_n':len(operational),'total_cost_eur':sum(costs),
            'cost_per_forecast_eur':sum(costs)/covered if covered else None,
            'calibration_bins':reliability,
            'limitations':['Negative delta favors candidate; no tournament score implied.',
                'Wilson bins assume independent outcomes; related events widen uncertainty.',
                'Few clusters give unreliable bootstrap intervals.',
                'Timestamps alone cannot prove absence of leakage or a complete cohort.',
                'Baseline fallback is an analysis only; it does not change live abstention.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = audit(json.loads(args.input.read_text(encoding='utf-8')))
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False), encoding='utf-8')


if __name__ == '__main__':
    main()
