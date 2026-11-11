"""The Decider slot, the search space, and the history a decider is allowed to see.

A decider sees measured results only. It never sees the hidden yield surface.
Plates that failed quality checks and excluded wells are left out of the means.
"""

from __future__ import annotations

import itertools
import random
import statistics
from collections import defaultdict
from typing import Protocol

import numpy as np

from labauto.config import allowed_values, clean
from labauto.models import Assay, ConditionStat, Decision, History


class Decider(Protocol):
    """Anything that picks the next round's conditions."""

    name: str

    def decide(self, history: History, n: int, brief: str, seed: int) -> Decision: ...


def grid_conditions(assay: Assay) -> list[dict[str, float]]:
    """Every allowed condition: 20 x 11 x 9 = 1,980 for the shipped assay."""
    names = list(assay.factors)
    levels = [allowed_values(assay.factors[f]) for f in names]
    return [dict(zip(names, combo, strict=True)) for combo in itertools.product(*levels)]


def key(cond: dict[str, float], factors: list[str]) -> tuple:
    """A hashable key for a condition."""
    return tuple(clean(cond[f]) for f in factors)


def on_grid(cond: dict[str, float], assay: Assay) -> bool:
    """True when every factor is present and takes an allowed value."""
    if set(cond) != set(assay.factors):
        return False
    return all(
        clean(cond[f]) in {clean(v) for v in allowed_values(assay.factors[f])}
        for f in assay.factors
    )


def unit_scale(conds: list[dict[str, float]], assay: Assay) -> np.ndarray:
    """Scale each factor to 0..1 over its allowed range."""
    cols = []
    for f, spec in assay.factors.items():
        values = allowed_values(spec)
        lo, hi = values[0], values[-1]
        span = (hi - lo) or 1.0
        cols.append([(float(c[f]) - lo) / span for c in conds])
    return np.array(cols, dtype=float).T


def latin_hypercube(assay: Assay, n: int, seed: int) -> list[dict[str, float]]:
    """A seeded Latin hypercube over the grid, shared by every strategy for round 1.

    Each factor's levels are split into n bins, one level is drawn per bin,
    and each factor's picks are shuffled on their own.
    """
    rng = random.Random(seed)
    columns = {}
    for f, spec in assay.factors.items():
        levels = allowed_values(spec)
        bins = [b for b in np.array_split(np.arange(len(levels)), n)]
        picks = []
        for b in bins:
            idx = list(b) if len(b) else [rng.randrange(len(levels))]
            picks.append(levels[rng.choice(idx)])
        rng.shuffle(picks)
        columns[f] = picks
    conds = [{f: columns[f][i] for f in assay.factors} for i in range(n)]
    seen, out = set(), []
    names = list(assay.factors)
    pool = grid_conditions(assay)
    for c in conds:
        while key(c, names) in seen:
            c = rng.choice(pool)
        seen.add(key(c, names))
        out.append(c)
    return out


def build_history(rows: list[dict], assay: Assay, next_round: int) -> History:
    """Condition means and flags from the results rows, plus last round's picks and quality events."""
    names = list(assay.factors)
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("role") != "sample":
            continue
        cond = {f: float(r[f"designed_{f}"]) for f in names}
        grouped[key(cond, names)].append(r)
    stats = []
    for k, rs in grouped.items():
        valid = [float(r["yield_pct"]) for r in rs if str(r.get("excluded")).lower() != "true"]
        flags = sorted({f for r in rs for f in str(r.get("qc_flags", "")).split(";") if f})
        stats.append(
            ConditionStat(
                conditions=dict(zip(names, k, strict=True)),
                mean=statistics.fmean(valid) if valid else None,
                sd=statistics.stdev(valid) if len(valid) >= 2 else None,
                n=len(valid),
                flags=flags,
                rounds=sorted({int(r["round"]) for r in rs}),
            )
        )
    last = next_round - 1
    last_picks = []
    for s in stats:
        if last in s.rounds:
            last_picks.append(
                {
                    "conditions": s.conditions,
                    "mean": None if s.mean is None else round(s.mean, 1),
                    "n": s.n,
                    "flags": s.flags,
                }
            )
    events = sorted(
        {
            f"round {r['round']}: {f}"
            for r in rows
            for f in str(r.get("qc_flags", "")).split(";")
            if f in ("PLATE_QC_FAIL", "IS_OUT_OF_RANGE")
        }
    )
    return History(next_round=next_round, stats=stats, last_picks=last_picks, qc_events=events)


def tested_keys(history: History, assay: Assay) -> set[tuple]:
    """Conditions with at least one valid measurement. These leave the candidate list."""
    names = list(assay.factors)
    return {key(s.conditions, names) for s in history.stats if s.n > 0}


def untested(assay: Assay, history: History) -> list[dict[str, float]]:
    done = tested_keys(history, assay)
    names = list(assay.factors)
    return [c for c in grid_conditions(assay) if key(c, names) not in done]


def recommendation(history: History) -> dict | None:
    """The tested condition with the highest measured mean."""
    measured = [s for s in history.stats if s.mean is not None]
    if not measured:
        return None
    best = max(measured, key=lambda s: s.mean)
    return {
        "conditions": best.conditions,
        "mean": best.mean,
        "sd": best.sd,
        "n": best.n,
        "flags": best.flags,
    }
