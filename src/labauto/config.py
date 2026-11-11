"""Load assay and workcell files, and answer simple questions about factors.

A factor value maps to a volume: volume_ul = value / value_per_ul. The allowed
values are the ones whose volume is a whole multiple of step_ul between min_ul
and max_ul.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field

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
    """Volume in microliters that gives this factor value.

    A value that on_grid accepts is snapped to its exact step, so the volume
    the plan asks for is the same number the protocol will dispense.
    """
    vol = value / spec.value_per_ul
    steps = (vol - spec.min_ul) / spec.step_ul
    if abs(steps - round(steps)) < TOLERANCE:
        return clean(spec.min_ul + round(steps) * spec.step_ul)
    return vol


def snap_value(value: float, spec: FactorSpec) -> float:
    """The factor value that matches the snapped volume, for example 1.5000001 to 1.5."""
    return clean(factor_volume(value, spec) * spec.value_per_ul)


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


def fmt_code(x: float) -> str:
    """Write a volume into protocol code with no loss of precision."""
    x = float(x)
    return str(int(x)) if x.is_integer() else repr(x)


def fmt_precise(x: float) -> str:
    """Format a volume for an error message with enough digits to show small differences."""
    x = float(x)
    return str(int(x)) if x.is_integer() else f"{x:.6f}".rstrip("0").rstrip(".")


def fmt_ul(x: float) -> str:
    """Format a volume: whole numbers without a decimal point."""
    x = clean(x)
    if float(x).is_integer():
        return str(int(x))
    return f"{x:g}"


MISSING = "missing"


def condition_key(cond: dict[str, float], factors: dict[str, FactorSpec]) -> tuple:
    """A hashable key for a condition, for finding duplicates.

    Values are compared by the volume they give, so 2.0 and 2.0000001 match.
    A factor left out of the condition gets a fixed marker, so two conditions
    that leave out the same factor still match.
    """
    return tuple(
        round(factor_volume(cond[f], spec), 6) if f in cond else MISSING
        for f, spec in factors.items()
    )


def sample_wells(assay: Assay, include_controls: bool) -> list[str]:
    """Wells free for samples, in column order, after the control wells are taken."""
    from labauto import labware

    taken: set[str] = set()
    if include_controls:
        for ctrl in assay.controls.values():
            taken.update(ctrl.wells)
    return [w for w in labware.well_names(assay.plate_labware) if w not in taken]


# ---------------------------------------------------------------- model endpoints (Stage 2)

ENV_PREFIX = "LABAUTO_"


class Endpoint(BaseModel):
    """One model endpoint. The key is never printed or written to a record."""

    label: str
    provider: str = "openai_compatible"
    base_url: str = ""
    model: str = ""
    api_key: str = Field(default="", repr=False, exclude=True)
    context_tokens: int = 16000


class Settings(BaseModel):
    """The configured endpoints. Any of them may be missing."""

    main: Endpoint | None = None
    small: Endpoint | None = None
    hosted: Endpoint | None = None
    hosted_max_calls: int = 2


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def load_settings(env_file: str | Path | None = ".env", environ: dict | None = None) -> Settings:
    """Read endpoint settings from .env and the environment. Environment variables win."""
    from dotenv import dotenv_values

    values: dict[str, str] = {}
    if env_file is not None:
        for candidate in (Path(env_file), repo_root() / Path(env_file).name):
            if candidate.is_file():
                values.update({k: v for k, v in dotenv_values(candidate).items() if v is not None})
                break
    env = os.environ if environ is None else environ
    values.update({k: v for k, v in env.items() if k.startswith(ENV_PREFIX)})

    def get(name: str) -> str:
        return (values.get(ENV_PREFIX + name) or "").strip()

    settings = Settings(hosted_max_calls=_int(get("HOSTED_MAX_CALLS_PER_RUN"), 2))
    if get("MAIN_BASE_URL"):
        settings.main = Endpoint(
            label="local-main",
            base_url=get("MAIN_BASE_URL"),
            model=get("MAIN_MODEL"),
            api_key=get("MAIN_API_KEY"),
            context_tokens=_int(get("MAIN_CONTEXT_TOKENS"), 16000),
        )
    if get("SMALL_BASE_URL"):
        settings.small = Endpoint(
            label="local-small",
            base_url=get("SMALL_BASE_URL"),
            model=get("SMALL_MODEL"),
            api_key=get("MAIN_API_KEY"),
            context_tokens=_int(get("SMALL_CONTEXT_TOKENS"), 8000),
        )
    provider = get("HOSTED_PROVIDER")
    if provider:
        settings.hosted = Endpoint(
            label="hosted",
            provider=provider,
            base_url=get("HOSTED_BASE_URL"),
            model=get("HOSTED_MODEL"),
            api_key=get("HOSTED_API_KEY"),
            context_tokens=_int(get("HOSTED_CONTEXT_TOKENS"), 200000),
        )
    return settings
