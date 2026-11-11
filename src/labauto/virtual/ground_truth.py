"""A synthetic yield surface, hidden from every decider.

It has an interior optimum and an interaction between base and co-solvent (the
best base level moves with the co-solvent fraction), so it is not trivial. It
models no real chemistry. The numbers are made up to make a fair test.

With c = catalyst mol %, b = base equivalents, s = co-solvent fraction:

    f_cat  = (1 - exp(-c / 2.0)) * (1 - 0.02 * max(0, c - 6))
    b_opt  = 1.6 + 1.0 * s
    f_base = exp(-((b - b_opt)^2) / (2 * 0.6^2))
    f_cos  = exp(-((s - 0.22)^2) / (2 * 0.12^2))
    yield  = 92 * f_cat * f_base * f_cos
"""

from __future__ import annotations

import math

import numpy as np
from pydantic import BaseModel

from labauto.models import Assay

CATALYST = "catalyst_mol_pct"
BASE = "base_equiv"
COSOLVENT = "cosolvent_pct"


class Surface(BaseModel):
    """The surface's parameters. Surface seed 0 gives exactly the published ones."""

    ceiling: float = 92.0
    cat_scale: float = 2.0
    penalty_onset: float = 6.0
    penalty_slope: float = 0.02
    base_intercept: float = 1.6
    base_slope: float = 1.0
    base_width: float = 0.6
    cos_opt: float = 0.22
    cos_width: float = 0.12


def surface_for(surface_seed: int) -> Surface:
    """Seed 0 gives the published surface. Any other seed jitters it with that seed."""
    if surface_seed == 0:
        return Surface()
    rng = np.random.default_rng(surface_seed)
    return Surface(
        cos_opt=0.22 + rng.uniform(-0.08, 0.08),
        base_intercept=1.6 + rng.uniform(-0.4, 0.4),
        base_slope=1.0 + rng.uniform(-0.5, 0.5),
        cat_scale=2.0 * rng.uniform(0.7, 1.5),
        penalty_onset=6.0 + rng.uniform(-1.5, 1.5),
        ceiling=92.0 * rng.uniform(0.9, 1.0),
    )


def true_yield(conditions: dict[str, float], surface: Surface) -> float:
    """Noise free yield in percent for one condition."""
    c = float(conditions.get(CATALYST, 0.0))
    b = float(conditions.get(BASE, 0.0))
    s = float(conditions.get(COSOLVENT, 0.0)) / 100.0
    p = surface
    f_cat = (1 - math.exp(-c / p.cat_scale)) * (1 - p.penalty_slope * max(0.0, c - p.penalty_onset))
    b_opt = p.base_intercept + p.base_slope * s
    f_base = math.exp(-((b - b_opt) ** 2) / (2 * p.base_width**2))
    f_cos = math.exp(-((s - p.cos_opt) ** 2) / (2 * p.cos_width**2))
    return max(0.0, min(100.0, p.ceiling * f_cat * f_base * f_cos))


def grid_optimum(assay: Assay, surface: Surface) -> tuple[float, dict[str, float]]:
    """The best true yield on the allowed grid, and where it is."""
    from labauto.decide.base import grid_conditions

    best = max(grid_conditions(assay), key=lambda cond: true_yield(cond, surface))
    return true_yield(best, surface), best


def reference_expected(assay: Assay, surface_seed: int) -> float:
    """The reference control's expected yield for the quality checks.

    For surface seed 0 this is the assay's own yield_center. For a jittered
    surface it is recomputed from the surface, standing in for a lab's
    historical control chart.
    """
    ref = assay.controls.get("reference")
    if ref is None:
        return 0.0
    if surface_seed == 0:
        return float(ref.expect.get("yield_center", 0.0))
    return true_yield(ref.conditions or {}, surface_for(surface_seed))
