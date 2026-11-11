"""Load assay and workcell files, and answer simple questions about factors.

A factor value maps to a volume: volume_ul = value / value_per_ul. The allowed
values are the ones whose volume is a whole multiple of step_ul between min_ul
and max_ul.
"""

from __future__ import annotations

import json
from pathlib import Path

from labauto.models import Assay, FactorSpec, Workcell

TOLERANCE = 1e-6


def repo_root() -> Path:
    """The folder that holds configs/ and examples/: the source checkout if present."""
    here = Path(__file__).resolve().parents[2]
    if (here / "configs").is_dir():
        return here
    return Path.cwd()


def resolve(path: str | Path) -> Path:
    """Find a file given relative to the working folder, or else to the repository."""
    p = Path(path)
    if p.is_absolute() or p.exists():
        return p
    candidate = repo_root() / p
    return candidate if candidate.exists() else p


DEFAULT_ASSAY = "configs/assay_reaction_screen.json"
DEFAULT_WORKCELL = "configs/workcell_ot2.json"


def load_json(path: str | Path) -> dict:
    """Read one JSON file."""
    with open(resolve(path), encoding="utf-8") as fh:
        return json.load(fh)


def load_assay(path: str | Path = DEFAULT_ASSAY) -> Assay:
    """Read and check an assay definition file."""
    return Assay.model_validate(load_json(path))


def load_workcell(path: str | Path = DEFAULT_WORKCELL) -> Workcell:
    """Read and check a workcell file."""
    return Workcell.model_validate(load_json(path))


def factor_volume(value: float, spec: FactorSpec) -> float:
    """Volume in microliters that gives this factor value."""
    vol = value / spec.value_per_ul
    nearest = round(vol)
    return float(nearest) if abs(vol - nearest) < TOLERANCE else vol


def allowed_volumes(spec: FactorSpec) -> list[float]:
    """Every allowed dispense volume for a factor, smallest first."""
    out = []
    n_steps = int(round((spec.max_ul - spec.min_ul) / spec.step_ul))
    for i in range(n_steps + 1):
        out.append(spec.min_ul + i * spec.step_ul)
    return out


def allowed_values(spec: FactorSpec) -> list[float]:
    """Every allowed factor value, smallest first, rounded to clean numbers."""
    return [clean(v * spec.value_per_ul) for v in allowed_volumes(spec)]


def clean(x: float) -> float:
    """Round away floating point noise, for example 1.5000000000000002 to 1.5."""
    return round(float(x), 9)


def in_range(value: float, spec: FactorSpec) -> bool:
    """True when the value's volume lies between the factor's min and max."""
    vol = value / spec.value_per_ul
    return spec.min_ul - TOLERANCE <= vol <= spec.max_ul + TOLERANCE


def on_grid(value: float, spec: FactorSpec) -> bool:
    """True when the value's volume is a whole number of steps from the minimum."""
    vol = value / spec.value_per_ul
    steps = (vol - spec.min_ul) / spec.step_ul
    return abs(steps - round(steps)) < TOLERANCE


def nearest_allowed(value: float, spec: FactorSpec) -> list[float]:
    """The allowed values on either side of an off grid value."""
    values = allowed_values(spec)
    below = [v for v in values if v < value]
    above = [v for v in values if v > value]
    out = []
    if below:
        out.append(below[-1])
    if above:
        out.append(above[0])
    return out


def fmt(x: float) -> str:
    """Format a factor value for people: 2.0, 2.5, 1.75."""
    x = clean(x)
    if float(x).is_integer():
        return f"{x:.1f}"
    return f"{x:g}"


def fmt_ul(x: float) -> str:
    """Format a volume: whole numbers without a decimal point."""
    x = clean(x)
    if float(x).is_integer():
        return str(int(x))
    return f"{x:g}"


def condition_key(cond: dict[str, float], factor_order: list[str]) -> tuple:
    """A hashable key for a condition, for finding duplicates."""
    return tuple(clean(cond.get(f, float("nan"))) for f in factor_order)


def sample_wells(assay: Assay, include_controls: bool) -> list[str]:
    """Wells free for samples, in column order, after the control wells are taken."""
    from labauto import labware

    taken: set[str] = set()
    if include_controls:
        for ctrl in assay.controls.values():
            taken.update(ctrl.wells)
    return [w for w in labware.well_names(assay.plate_labware) if w not in taken]
