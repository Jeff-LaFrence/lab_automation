"""A Gaussian process optimizer over the allowed grid, with Thompson sampling for batches.

Candidates are the grid conditions not yet measured. Inputs are scaled to 0..1
per factor. The target is each condition's mean over valid wells; plates that
failed quality checks are already left out of the history.

The same three functions are tools for the model decider: suggest, predict,
and results.
"""

from __future__ import annotations

import builtins
import time
import warnings

import numpy as np
from sklearn.exceptions import ConvergenceWarning
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import ConstantKernel, Matern, WhiteKernel

from labauto.blas import one_blas_thread
from labauto.decide.base import key, latin_hypercube, unit_scale, untested
from labauto.models import Assay, Decision, History, Pick

MAX_SUGGEST = 16
MAX_PREDICT = 10
NO_MODEL = "no model yet: fewer than 2 conditions have valid measurements"


class Optimizer:
    """A fitted model of yield over the grid, built from the history."""

    def __init__(self, assay: Assay, history: History, seed: int):
        self.assay = assay
        self.history = history
        self.seed = seed
        self.names = list(assay.factors)
        self.candidates = untested(assay, history)
        measured = [s for s in history.stats if s.mean is not None]
        self.gp = None
        if len(measured) >= 2:
            x = unit_scale([s.conditions for s in measured], assay)
            y = np.array([s.mean for s in measured], dtype=float)
            kernel = (
                ConstantKernel(1.0)
                * Matern(
                    length_scale=[0.5] * len(self.names), length_scale_bounds=(0.05, 3.0), nu=2.5
                )
                + WhiteKernel()
            )
            self.gp = GaussianProcessRegressor(
                kernel=kernel, normalize_y=True, n_restarts_optimizer=1, random_state=seed
            )
            with warnings.catch_warnings(), one_blas_thread():
                warnings.simplefilter("ignore", ConvergenceWarning)
                self.gp.fit(x, y)

    def _predict(self, conds: list[dict[str, float]]) -> tuple[np.ndarray, np.ndarray]:
        if self.gp is None:
            return np.zeros(len(conds)), np.ones(len(conds))
        with one_blas_thread():
            return self.gp.predict(unit_scale(conds, self.assay), return_std=True)

    def suggest(self, n: int) -> list[dict]:
        """Up to 16 untested conditions by Thompson sampling, with predicted mean and sd.

        With fewer than 2 measured conditions there is no model yet: the picks
        come from a Latin hypercube over untested conditions, with no prediction.
        """
        n = min(int(n), MAX_SUGGEST, len(self.candidates))
        if n <= 0:
            return []
        if self.gp is None:
            done = {key(c, self.names) for c in self._tested()}
            picks = latin_hypercube(self.assay, n, self.seed, exclude=done)
            return [
                {"conditions": c, "predicted_mean": None, "predicted_sd": None, "note": NO_MODEL}
                for c in picks
            ]
        else:
            x = unit_scale(self.candidates, self.assay)
            with one_blas_thread():
                samples = self.gp.sample_y(x, n_samples=n, random_state=self.seed)
            chosen: list[int] = []
            for j in range(samples.shape[1]):
                for idx in np.argsort(-samples[:, j]):
                    if int(idx) not in chosen:
                        chosen.append(int(idx))
                        break
            picks = [self.candidates[i] for i in chosen]
        mean, sd = self._predict(picks)
        return [
            {
                "conditions": c,
                "predicted_mean": round(float(m), 2),
                "predicted_sd": round(float(s), 2),
            }
            for c, m, s in zip(picks, mean, sd, strict=True)
        ]

    def _tested(self) -> list[dict[str, float]]:
        return [s.conditions for s in self.history.stats if s.n > 0]

    def predict(self, conditions: list[dict[str, float]]) -> list[dict]:
        """Predicted mean and sd for up to 10 conditions."""
        conds = list(conditions)[:MAX_PREDICT]
        if self.gp is None:
            return [
                {"conditions": c, "predicted_mean": None, "predicted_sd": None, "note": NO_MODEL}
                for c in conds
            ]
        mean, sd = self._predict(conds)
        return [
            {
                "conditions": c,
                "predicted_mean": round(float(m), 2),
                "predicted_sd": round(float(s), 2),
            }
            for c, m, s in zip(conds, mean, sd, strict=True)
        ]

    def results(self, top_k: int | None = None, round: int | None = None) -> list[dict]:
        """Measured condition means with count, sd, and flags, best first."""
        # The tool's argument is named "round", as the model sees it, so the
        # builtin is reached through the builtins module here.
        rnd = builtins.round
        stats = [s for s in self.history.stats if round is None or round in s.rounds]
        stats.sort(key=lambda s: (s.mean is None, -(s.mean or 0.0)))
        if top_k:
            stats = stats[: int(top_k)]
        return [
            {
                "conditions": s.conditions,
                "mean": None if s.mean is None else rnd(s.mean, 1),
                "sd": None if s.sd is None else rnd(s.sd, 1),
                "n": s.n,
                "flags": s.flags,
                "rounds": s.rounds,
            }
            for s in stats
        ]


class OptimizerDecider:
    """Picks with the optimizer's suggest(n)."""

    name = "optimizer"

    def __init__(self, assay: Assay):
        self.assay = assay

    def decide(self, history: History, n: int, brief: str, seed: int) -> Decision:
        start = time.perf_counter()
        opt = Optimizer(self.assay, history, round_seed(seed, history.next_round))
        picks = [
            Pick(
                conditions=s["conditions"],
                source="optimizer",
                reason=(
                    f"Thompson sample; predicted {s['predicted_mean']:.1f} +/- {s['predicted_sd']:.1f}"
                    if s["predicted_mean"] is not None
                    else "Latin hypercube: too little valid data for the model"
                ),
            )
            for s in opt.suggest(n)
        ]
        return Decision(
            round=history.next_round,
            strategy=self.name,
            picks=picks,
            seconds=round(time.perf_counter() - start, 3),
        )


def round_seed(seed: int, round_number: int) -> int:
    """One seed per campaign seed and round, so each round's draws differ but repeat."""
    return seed * 1000 + round_number


def round_one(assay: Assay, n: int, seed: int) -> Decision:
    """The shared round 1 design: a Latin hypercube, the same for every strategy."""
    picks = [
        Pick(conditions=c, source="lhs", reason="shared round 1 Latin hypercube")
        for c in latin_hypercube(assay, n, seed)
    ]
    return Decision(
        round=1, strategy="lhs", picks=picks, round_note="Round 1 is a shared Latin hypercube."
    )


def same_keys(a: list[dict], b: list[dict], names: list[str]) -> bool:
    return {key(x, names) for x in a} == {key(x, names) for x in b}
