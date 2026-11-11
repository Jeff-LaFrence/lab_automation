"""A simulated LC/MS that reads what was dispensed, not what was intended.

For each well it works out the as-run factor values from the ledger volumes,
looks up the hidden true yield, adds noise, and writes three peak areas:
substrate, product, and an internal standard. Data are synthetic.

Faults:
- injection_failure: each well with probability 0.02 gets a small injection.
  Its internal standard area is multiplied by 0.2 to 0.5, and the noise on its
  product ratio is tripled. On by default.
- plate_drift@R: in round R, every product area on the plate is multiplied by 0.75.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

import numpy as np
from pydantic import BaseModel

from labauto import labware
from labauto.config import clean
from labauto.models import Assay, LedgerEntry, Plan
from labauto.virtual.ground_truth import Surface, true_yield

RETENTION = {"substrate": 1.87, "product": 2.41, "internal_standard": 3.05}


class LcmsSettings(BaseModel):
    """Noise and fault settings. The defaults are the published ones."""

    yield_noise_sd: float = 2.5
    is_noise_sd: float = 0.03
    conversion_extra: tuple[float, float] = (2.0, 8.0)
    injection_failure_p: float = 0.02
    drift_factor: float = 0.75
    rt_jitter_sd: float = 0.01


class WellReading(BaseModel):
    """One well as the instrument saw it, plus the hidden truth (kept for tests and scoring)."""

    well: str
    as_run: dict[str, float]
    volumes: dict[str, float]
    true_yield: float
    observed_yield: float
    conversion: float
    substrate_area: float
    product_area: float
    is_area: float
    injection_failure: bool = False


def as_run_conditions(volumes: dict[str, float], assay: Assay) -> dict[str, float]:
    """Factor values from the volumes actually dispensed: value = volume x value_per_ul."""
    out = {}
    for reagent, spec in assay.reagents.items():
        if spec.role == "factor" and spec.factor:
            factor = assay.factors[spec.factor]
            out[spec.factor] = clean(volumes.get(reagent, 0.0) * factor.value_per_ul)
    return out


def drifted_rounds(faults: list[str]) -> set[int]:
    """Rounds named in plate_drift@R faults."""
    out = set()
    for f in faults:
        name, _, at = f.partition("@")
        if name == "plate_drift" and at.isdigit():
            out.add(int(at))
    return out


def read_plate(
    ledger: list[LedgerEntry],
    plan: Plan,
    assay: Assay,
    surface: Surface,
    seed: int,
    round_number: int = 1,
    faults: list[str] | None = None,
    settings: LcmsSettings | None = None,
) -> list[WellReading]:
    """Measure every well that received liquid. Same inputs and seed give the same readings."""
    faults = list(faults or [])
    cfg = settings or LcmsSettings()
    rng = np.random.default_rng(seed)
    volumes: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for e in ledger:
        if e.dest_labware == plan.plate:
            volumes[e.dest_well][e.reagent] += e.volume_ul
    injection = "injection_failure" in faults
    drift = round_number in drifted_rounds(faults)
    k_p, k_s = assay.analysis.k_product, assay.analysis.k_substrate
    readings = []
    for well in sorted(volumes, key=labware.column_order_key):
        vols = dict(volumes[well])
        cond = as_run_conditions(vols, assay)
        truth = true_yield(cond, surface)
        failed = bool(injection and rng.random() < cfg.injection_failure_p)
        noise_sd = cfg.yield_noise_sd * (3 if failed else 1)
        observed = float(np.clip(truth + rng.normal(0.0, noise_sd) if noise_sd else truth, 0, 100))
        lo, hi = cfg.conversion_extra
        conversion = float(min(100.0, observed + rng.uniform(lo, hi)))
        is_area = assay.analysis.internal_standard_area * (
            1 + (rng.normal(0.0, cfg.is_noise_sd) if cfg.is_noise_sd else 0.0)
        )
        if failed:
            is_area *= rng.uniform(0.2, 0.5)
        product = k_p * (observed / 100.0) * is_area
        if drift:
            product *= cfg.drift_factor
        substrate = k_s * (1 - conversion / 100.0) * is_area
        readings.append(
            WellReading(
                well=well,
                as_run=cond,
                volumes=vols,
                true_yield=truth,
                observed_yield=observed,
                conversion=conversion,
                substrate_area=substrate,
                product_area=product,
                is_area=is_area,
                injection_failure=failed,
            )
        )
    return readings


def write_peaks(
    path: Path, readings: list[WellReading], seed: int, settings: LcmsSettings | None = None
) -> None:
    """raw/lcms_peaks.csv: well, analyte, rt_min, area. Three rows per well."""
    cfg = settings or LcmsSettings()
    rng = np.random.default_rng(seed + 7919)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["well", "analyte", "rt_min", "area"])
        for r in readings:
            for analyte, area in (
                ("substrate", r.substrate_area),
                ("product", r.product_area),
                ("internal_standard", r.is_area),
            ):
                rt = RETENTION[analyte] + (
                    rng.normal(0.0, cfg.rt_jitter_sd) if cfg.rt_jitter_sd else 0.0
                )
                writer.writerow([r.well, analyte, f"{rt:.3f}", f"{area:.4f}"])


def load_peaks(path: Path) -> dict[str, dict[str, float]]:
    """Read raw/lcms_peaks.csv back as {well: {analyte: area}}."""
    out: dict[str, dict[str, float]] = defaultdict(dict)
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out[row["well"]][row["analyte"]] = float(row["area"])
    return dict(out)
