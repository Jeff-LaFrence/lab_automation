"""The random decider: seeded uniform picks from conditions not yet measured."""

from __future__ import annotations

import random

from labauto.decide.base import untested
from labauto.models import Assay, Decision, History, Pick


class RandomDecider:
    """A baseline. Picks n untested grid conditions uniformly at random."""

    name = "random"

    def __init__(self, assay: Assay):
        self.assay = assay

    def decide(self, history: History, n: int, brief: str, seed: int) -> Decision:
        rng = random.Random(f"{seed}-{history.next_round}")
        pool = untested(self.assay, history)
        picks = rng.sample(pool, min(n, len(pool)))
        return Decision(
            round=history.next_round,
            strategy=self.name,
            picks=[
                Pick(conditions=c, source="random", reason="uniform random pick") for c in picks
            ],
        )
