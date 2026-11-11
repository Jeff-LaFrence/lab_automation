"""Pair the simulated LC/MS and NMR readouts of one plate, and write the evidence pack.

Three layers sit behind a paired readout:
1. A spectrum evaluator turns each NMR spectrum into numbers and diagnostics
   (nmr_analysis.py).
2. This module pairs those numbers with the LC/MS results, condition by
   condition, raises flags with fixed rules, and writes evidence.json.
3. A reviewer model reads only evidence.json (review.py).

The evidence pack never holds the hidden truth: no true yields and no fault
log. The fault cases at the end build plates with known faults for the reviewer
evaluation. Their fault log comes from the simulators' own records and is
written to its own file. All data here are synthetic.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from labauto import labware
from labauto.analysis import WellResult
from labauto.models import Assay, Design, LedgerEntry, Plan, QcReport
from labauto.record import write_json

EVALUATOR_NAME = "lineshape least squares (classical)"
MIN_WELLS = 2  # fewer usable wells than this on either side: "insufficient"
DISAGREE_FLOOR = 5.0  # points of yield
DISAGREE_SE_FACTOR = 2.0
PLATE_PATTERN_SHARE = 0.5  # IS_OUT_OF_RANGE on at least half the wells becomes a plate flag
CONTROL_KEYS = {"negative_control": "negative", "reference_control": "reference"}
LCMS_STATS_NOTE = (
    "The LC/MS plate failed its controls, so every LC/MS well row shows excluded. "
    "The LC/MS condition statistics and control means still count wells excluded only "
    "by PLATE_QC_FAIL, so their means stay visible. Wells excluded for a reason of "
    "their own are left out."
)


# ---------------------------------------------------------------- evidence models


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Readout(Strict):
    """One readout's statistics for one condition: mean, standard deviation, usable wells."""

    mean: float | None = None
    sd: float | None = None
    n: int = 0
    flags: list[str] = Field(default_factory=list)


class ConditionEvidence(Strict):
    """Both readouts of one condition side by side, and whether they agree."""

    key: str
    conditions: dict[str, float]
    wells: list[str]
    lcms: Readout
    nmr: Readout
    difference: float | None = None
    combined_se: float | None = None
    agreement: Literal["agree", "disagree", "insufficient"]
    flags: list[str] = Field(default_factory=list)


class LcmsWellEvidence(Strict):
    yield_pct: float
    is_ratio: float | None = None  # internal standard area over the plate median
    excluded: bool
    flags: list[str] = Field(default_factory=list)


class NmrWellEvidence(Strict):
    yield_pct: float | None = None
    naive_yield_pct: float | None = None
    yield_from_doublet: float | None = None
    yield_from_singlet: float | None = None
    rmse_over_noise: float | None = None
    is_snr: float | None = None
    linewidth_hz: float | None = None
    phase_deg: float | None = None
    excluded: bool
    flags: list[str] = Field(default_factory=list)


class WellEvidence(Strict):
    """One well. key names its condition (c01, ...) or its control (negative, reference)."""

    well: str
    key: str
    lcms: LcmsWellEvidence | None = None
    nmr: NmrWellEvidence | None = None


class NegativeControlEvidence(Strict):
    lcms_mean: float | None = None
    nmr_mean: float | None = None
    limit: float | None = None


class ReferenceControlEvidence(Strict):
    lcms_mean: float | None = None
    nmr_mean: float | None = None
    expected: float | None = None
    tolerance: float | None = None


class ControlsEvidence(Strict):
    negative: NegativeControlEvidence | None = None
    reference: ReferenceControlEvidence | None = None


class PlateEvidence(Strict):
    lcms_flags: list[str] = Field(default_factory=list)
    nmr_flags: list[str] = Field(default_factory=list)
    controls: ControlsEvidence = Field(default_factory=ControlsEvidence)
    # Set only when the LC/MS plate failed: says how the condition statistics treat the wells.
    lcms_stats_note: str | None = None


class Evidence(Strict):
    """evidence.json: everything a reviewer may see about one plate, and nothing hidden."""

    campaign_id: str | None = None
    round: int
    assay_id: str
    synthetic: Literal[True] = True
    evaluator: str
    plate: PlateEvidence
    conditions: list[ConditionEvidence]
    wells: list[WellEvidence]


class FaultEvent(Strict):
    """One fault the simulators really applied. wells is empty for a plate wide fault."""

    kind: Literal[
        "injection_failure", "plate_drift", "nmr_broad_lines", "nmr_phase_error", "nmr_impurity"
    ]
    wells: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- statistics


def r1(x: float | None) -> float | None:
    """Round to 1 place. Adding 0.0 turns -0.0 into 0.0."""
    return None if x is None else round(float(x), 1) + 0.0


def r2(x: float | None) -> float | None:
    """Round to 2 places. Adding 0.0 turns -0.0 into 0.0."""
    return None if x is None else round(float(x), 2) + 0.0


def condition_key(condition_index: int) -> str:
    """c01 for condition index 0, c02 for 1, and so on."""
    return f"c{condition_index + 1:02d}"


def readout_stats(values: list[float], spread_max: float) -> Readout:
    """Mean, standard deviation, and count over usable wells, with the analysis.py flags."""
    n = len(values)
    flags = []
    if n >= 2 and max(values) - min(values) > spread_max:
        flags.append("REPLICATE_SPREAD")
    if n < 2:
        flags.append("LOW_REPLICATES")
    return Readout(
        mean=r1(statistics.fmean(values)) if n else None,
        sd=r2(statistics.stdev(values)) if n >= 2 else None,
        n=n,
        flags=flags,
    )


def compare(lcms: Readout, nmr: Readout) -> tuple[float | None, float | None, str]:
    """Difference (LC/MS minus NMR), combined standard error, and the agreement word.

    Disagree when the difference is larger than both 5 points and twice the
    combined standard error. Insufficient when either side has fewer than 2
    usable wells. Works on the rounded numbers in the pack, so a reader can
    redo it by hand.
    """
    difference = None
    if lcms.mean is not None and nmr.mean is not None:
        difference = r1(lcms.mean - nmr.mean)
    combined_se = None
    if lcms.sd is not None and nmr.sd is not None and lcms.n > 0 and nmr.n > 0:
        combined_se = round(math.sqrt(lcms.sd**2 / lcms.n + nmr.sd**2 / nmr.n), 2)
    if difference is None or combined_se is None or lcms.n < MIN_WELLS or nmr.n < MIN_WELLS:
        return difference, combined_se, "insufficient"
    limit = max(DISAGREE_FLOOR, DISAGREE_SE_FACTOR * combined_se)
    return difference, combined_se, "disagree" if abs(difference) > limit else "agree"


def _well_level_excluded(w: WellResult) -> bool:
    """Excluded for a reason of its own, not only because the whole plate failed."""
    reasons = {x.strip() for x in w.exclude_reason.split(";")} - {"", "PLATE_QC_FAIL"}
    return w.excluded and bool(reasons)


def lcms_condition_stats(lcms_wells: list[WellResult], assay: Assay) -> dict[int, Readout]:
    """Per condition LC/MS statistics, over wells not excluded for a well level reason.

    This mirrors analysis.py, which computes condition statistics before a plate
    failure excludes every well. A plate failure is carried as a plate flag, so
    the means stay visible to the reviewer.
    """
    by_ci: dict[int, list[float]] = defaultdict(list)
    seen: set[int] = set()
    for w in lcms_wells:
        if w.role == "sample" and w.condition_index is not None:
            seen.add(w.condition_index)
            if not _well_level_excluded(w):
                by_ci[w.condition_index].append(w.yield_pct)
    spread = assay.qc.replicate_spread_max
    return {ci: readout_stats(by_ci.get(ci, []), spread) for ci in sorted(seen)}


def _field(obj: Any, name: str) -> Any:
    """An attribute of a fit or condition result, or a key when it is a plain dict."""
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _usable(fit: Any) -> bool:
    return not _field(fit, "excluded") and _field(fit, "yield_pct") is not None


def nmr_condition_stats(
    nmr_fits: list[Any], nmr_conditions: list[Any] | None, plan: Plan, assay: Assay
) -> dict[int, Readout]:
    """Per condition NMR statistics: the evaluator's own numbers (NmrCondition) where given.

    A condition the evaluator did not report is computed from the fits with the
    same rule as the LC/MS side.
    """
    ci_of = {w.well: w.condition_index for w in plan.wells if w.role == "sample"}
    values: dict[int, list[float]] = defaultdict(list)
    for fit in nmr_fits:
        ci = ci_of.get(_field(fit, "well"))
        if ci is not None and _usable(fit):
            values[ci].append(float(_field(fit, "yield_pct")))
    given = {int(_field(c, "condition_index")): c for c in nmr_conditions or []}
    out = {}
    for ci in sorted({c for c in ci_of.values() if c is not None}):
        c = given.get(ci)
        if c is None:
            out[ci] = readout_stats(values.get(ci, []), assay.qc.replicate_spread_max)
            continue
        out[ci] = Readout(
            mean=r1(_field(c, "mean")),
            sd=r2(_field(c, "sd")),
            n=int(_field(c, "n") or 0),
            flags=list(_field(c, "flags") or []),
        )
    return out


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _widespread(flag_lists: list[list[str]], codes: tuple[str, ...]) -> list[str]:
    """Codes that show up on at least half of the measured wells."""
    n = len(flag_lists)
    return [c for c in codes if n and sum(c in f for f in flag_lists) / n >= PLATE_PATTERN_SHARE]


def _control_check(neg: float | None, ref: float | None, assay: Assay, expected: float) -> bool:
    """The analysis.py plate rule: negative below its limit, reference inside its tolerance."""
    ok = True
    if "negative" in assay.controls:
        limit = assay.controls["negative"].expect.get("yield_max")
        ok = ok and neg is not None and (limit is None or neg < limit)
    if "reference" in assay.controls:
        tol = assay.controls["reference"].expect.get("yield_tolerance")
        ok = ok and ref is not None and (tol is None or abs(ref - expected) <= tol)
    return ok


# ---------------------------------------------------------------- pairing


def pair_round(
    lcms_wells: list[WellResult],
    lcms_conditions: dict[int, Readout] | None,
    nmr_fits: list[Any],
    nmr_conditions: list[Any] | None,
    plan: Plan,
    assay: Assay,
    round_number: int,
    campaign_id: str | None = None,
    evaluator_name: str = EVALUATOR_NAME,
    *,
    reference_expected: float | None = None,
    lcms_qc: QcReport | None = None,
) -> Evidence:
    """Pair one plate's LC/MS and NMR results into the evidence pack.

    Per condition: both means, the difference, the combined standard error, and
    agree, disagree, or insufficient. LCMS_NMR_DISAGREE marks a disagreement.
    Per plate: each readout's control means and plate flags. Per well: the
    numbers and diagnostics a reviewer may cite.
    """
    if reference_expected is None:
        ref_spec = assay.controls.get("reference")
        reference_expected = float(ref_spec.expect.get("yield_center", 0.0)) if ref_spec else 0.0
    if lcms_conditions is None:
        lcms_conditions = lcms_condition_stats(lcms_wells, assay)
    nmr_stats = nmr_condition_stats(nmr_fits, nmr_conditions, plan, assay)
    lcms_by_well = {w.well: w for w in lcms_wells}
    fit_by_well = {_field(f, "well"): f for f in nmr_fits}

    is_values = [w.is_area for w in lcms_wells]
    is_median = lcms_qc.is_median if lcms_qc and lcms_qc.is_median else _median_or_none(is_values)

    wells_of: dict[int, list[str]] = defaultdict(list)
    cond_of: dict[int, dict[str, float]] = {}
    for w in plan.wells:
        if w.role == "sample" and w.condition_index is not None:
            wells_of[w.condition_index].append(w.well)
            cond_of[w.condition_index] = dict(w.conditions or {})

    conditions = []
    for ci in sorted(wells_of):
        lc = lcms_conditions.get(ci, Readout(flags=["LOW_REPLICATES"]))
        nm = nmr_stats.get(ci, Readout(flags=["LOW_REPLICATES"]))
        difference, combined_se, agreement = compare(lc, nm)
        conditions.append(
            ConditionEvidence(
                key=condition_key(ci),
                conditions=cond_of[ci],
                wells=sorted(wells_of[ci], key=labware.column_order_key),
                lcms=lc,
                nmr=nm,
                difference=difference,
                combined_se=combined_se,
                agreement=agreement,
                flags=["LCMS_NMR_DISAGREE"] if agreement == "disagree" else [],
            )
        )

    wells = []
    for pw in sorted(plan.wells, key=lambda w: labware.column_order_key(w.well)):
        key = (
            condition_key(pw.condition_index)
            if pw.role == "sample" and pw.condition_index is not None
            else CONTROL_KEYS.get(pw.role, pw.role)
        )
        wells.append(
            WellEvidence(
                well=pw.well,
                key=key,
                lcms=_lcms_well(lcms_by_well.get(pw.well), is_median),
                nmr=_nmr_well(fit_by_well.get(pw.well)),
            )
        )

    plate = _plate_evidence(
        plan, assay, lcms_wells, nmr_fits, lcms_by_well, reference_expected, lcms_qc
    )
    return Evidence(
        campaign_id=campaign_id,
        round=round_number,
        assay_id=assay.assay_id,
        evaluator=evaluator_name,
        plate=plate,
        conditions=conditions,
        wells=wells,
    )


def _median_or_none(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _lcms_well(w: WellResult | None, is_median: float | None) -> LcmsWellEvidence | None:
    if w is None:
        return None
    return LcmsWellEvidence(
        yield_pct=r1(w.yield_pct),
        is_ratio=r2(w.is_area / is_median) if is_median else None,
        excluded=w.excluded,
        flags=list(w.flags),
    )


def _nmr_well(fit: Any) -> NmrWellEvidence | None:
    if fit is None:
        return None
    return NmrWellEvidence(
        yield_pct=r1(_field(fit, "yield_pct")),
        naive_yield_pct=r1(_field(fit, "naive_yield_pct")),
        yield_from_doublet=r1(_field(fit, "yield_from_doublet")),
        yield_from_singlet=r1(_field(fit, "yield_from_singlet")),
        rmse_over_noise=r2(_field(fit, "rmse_over_noise")),
        is_snr=r1(_field(fit, "is_snr")),
        linewidth_hz=r2(_field(fit, "linewidth_hz")),
        phase_deg=r1(_field(fit, "phase_deg")),
        excluded=bool(_field(fit, "excluded")),
        flags=list(_field(fit, "flags") or []),
    )


def _plate_evidence(
    plan: Plan,
    assay: Assay,
    lcms_wells: list[WellResult],
    nmr_fits: list[Any],
    lcms_by_well: dict[str, WellResult],
    expected: float,
    lcms_qc: QcReport | None,
) -> PlateEvidence:
    """Control means for each readout, and the plate flags."""
    from labauto.nmr_analysis import control_means, plate_flags

    means: dict[tuple[str, str], float | None] = {}
    nmr_means = control_means(nmr_fits, plan)
    for role, key in CONTROL_KEYS.items():
        wells = [w.well for w in plan.wells if w.role == role]
        lc = [
            lcms_by_well[w].yield_pct
            for w in wells
            if w in lcms_by_well and not _well_level_excluded(lcms_by_well[w])
        ]
        means[(role, "lcms")], means[(role, "nmr")] = _mean(lc), nmr_means.get(key)

    controls = ControlsEvidence()
    if "negative" in assay.controls:
        controls.negative = NegativeControlEvidence(
            lcms_mean=r1(means[("negative_control", "lcms")]),
            nmr_mean=r1(means[("negative_control", "nmr")]),
            limit=assay.controls["negative"].expect.get("yield_max"),
        )
    if "reference" in assay.controls:
        controls.reference = ReferenceControlEvidence(
            lcms_mean=r1(means[("reference_control", "lcms")]),
            nmr_mean=r1(means[("reference_control", "nmr")]),
            expected=r1(expected),
            tolerance=assay.controls["reference"].expect.get("yield_tolerance"),
        )

    lcms_failed = (
        not lcms_qc.plate_ok
        if lcms_qc is not None
        else any("PLATE_QC_FAIL" in w.flags for w in lcms_wells)
    )
    lcms_flags = ["PLATE_QC_FAIL"] if lcms_failed else []
    lcms_flags += _widespread([w.flags for w in lcms_wells], ("IS_OUT_OF_RANGE",))
    nmr_flags = []
    neg, ref = means[("negative_control", "nmr")], means[("reference_control", "nmr")]
    if nmr_fits and not _control_check(neg, ref, assay, expected):
        nmr_flags.append("PLATE_QC_FAIL")
    nmr_flags += plate_flags(nmr_fits)
    return PlateEvidence(
        lcms_flags=lcms_flags,
        nmr_flags=nmr_flags,
        controls=controls,
        lcms_stats_note=LCMS_STATS_NOTE if lcms_failed else None,
    )


# ---------------------------------------------------------------- files


def write_evidence(path: Path, evidence: Evidence) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, evidence)
    return path


def load_evidence(path: Path) -> Evidence:
    return Evidence.model_validate_json(Path(path).read_text(encoding="utf-8"))


def write_nmr_fits(path: Path, fits: list[Any]) -> Path:
    """nmr_fits.csv: one row per well with every number the evaluator returned."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [f.model_dump() if hasattr(f, "model_dump") else dict(f) for f in fits]
    columns = list(rows[0]) if rows else ["well"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    k: ";".join(v)
                    if isinstance(v, list)
                    else (round(v, 4) if isinstance(v, float) else v)
                    for k, v in row.items()
                }
            )
    return path


def _fmt(x: float | None, places: int = 1) -> str:
    return "n/a" if x is None else f"{x:.{places}f}"


def _readout_cell(r: Readout) -> str:
    if r.mean is None:
        return f"n/a (n {r.n})"
    sd = f" +/- {r.sd:.1f}" if r.sd is not None else ""
    return f"{r.mean:.1f}{sd} (n {r.n})"


def _condition_text(conditions: dict[str, float]) -> str:
    short = {"catalyst_mol_pct": "cat", "base_equiv": "base", "cosolvent_pct": "cosolv"}
    return ", ".join(f"{short.get(k, k)} {v:g}" for k, v in conditions.items())


def faults_on_round(faults: list[str] | None, round_number: int) -> list[str]:
    """The faults in a list that apply on this round: named without @R, or with @R for this round."""
    out = []
    for f in faults or []:
        _, _, at = str(f).partition("@")
        if not at or (at.isdigit() and int(at) == round_number):
            out.append(str(f))
    return out


def settings_changed(settings: Any) -> dict[str, Any]:
    """The simulator settings that differ from their defaults, as JSON data."""
    if settings is None:
        return {}
    now = settings.model_dump(mode="json")
    default = type(settings)().model_dump(mode="json")
    return {k: v for k, v in now.items() if v != default.get(k)}


def _settings_text(changed: dict[str, Any]) -> str:
    def one(x: Any) -> str:
        return f"{x:g}" if isinstance(x, (int, float)) and not isinstance(x, bool) else str(x)

    def value(v: Any) -> str:
        return " to ".join(one(x) for x in v) if isinstance(v, list) else one(v)

    return ", ".join(f"{k} {value(v)}" for k, v in changed.items())


def simulated_lines(
    lcms_faults: list[str] | None,
    lcms_settings: Any,
    nmr_faults: list[str] | None,
    nmr_settings: Any,
) -> list[str]:
    """The paired.md lines that say which simulated faults and settings this run used."""
    lines = []
    for label, faults, settings in (
        ("LC/MS", lcms_faults, lcms_settings),
        ("NMR", nmr_faults, nmr_settings),
    ):
        if faults is None:
            text = "not recorded for this run"
        else:
            text = ", ".join(faults) or "none"
        changed = settings_changed(settings)
        if changed:
            text += f". Settings changed from the defaults: {_settings_text(changed)}"
        lines.append(f"- {label}: {text}.")
    return lines


def render_paired_md(
    evidence: Evidence, title: str, files: list[str], simulated: list[str] | None = None
) -> str:
    """paired.md: the evidence pack as a table a person can read.

    simulated holds the lines that name the faults and settings the simulators
    used. They go in their own section, which is never part of the evidence pack.
    """
    p = evidence.plate
    out = [
        f"# {title}",
        "",
        "Synthetic data. Both readouts are simulated from a made up yield surface. "
        "No real chemistry is modeled and no instrument was run.",
        "",
        f"- NMR evaluator: {evidence.evaluator}",
        "- Difference is LC/MS mean minus NMR mean, in yield points.",
        f"- Agreement: disagree when the difference is larger than both {DISAGREE_FLOOR:g} points "
        f"and {DISAGREE_SE_FACTOR:g} x the combined standard error. Insufficient when either "
        f"readout has fewer than {MIN_WELLS} usable wells.",
    ]
    if simulated:
        out += [
            "",
            "## Simulated faults",
            "",
            "Faults switched on in the simulators for this run. A fault on single wells "
            "(injection_failure, nmr_impurity) hits wells at random and may hit none. This "
            "section is for people. It is not in the evidence pack, and a reviewer never sees it.",
            "",
            *simulated,
        ]
    out += [
        "",
        "## Plate",
        "",
        f"- LC/MS plate flags: {', '.join(p.lcms_flags) or 'none'}",
        f"- NMR plate flags: {', '.join(p.nmr_flags) or 'none'}",
    ]
    if p.lcms_stats_note:
        out.append(f"- Note: {p.lcms_stats_note}")
    neg, ref = p.controls.negative, p.controls.reference
    if neg is not None:
        out.append(
            f"- Negative control: LC/MS {_fmt(neg.lcms_mean)}%, NMR {_fmt(neg.nmr_mean)}% "
            f"(limit: below {neg.limit:g}%)"
            if neg.limit is not None
            else f"- Negative control: LC/MS {_fmt(neg.lcms_mean)}%, NMR {_fmt(neg.nmr_mean)}%"
        )
    if ref is not None:
        tol = f" +/- {ref.tolerance:g}" if ref.tolerance is not None else ""
        out.append(
            f"- Reference control: LC/MS {_fmt(ref.lcms_mean)}%, NMR {_fmt(ref.nmr_mean)}% "
            f"(expected {_fmt(ref.expected)}{tol})"
        )
    out += [
        "",
        "## Conditions",
        "",
        "| condition | values | LC/MS mean | NMR mean | difference | agreement | flags |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in evidence.conditions:
        flags = list(c.flags)
        flags += [f"LC/MS: {f}" for f in c.lcms.flags]
        flags += [f"NMR: {f}" for f in c.nmr.flags]
        out.append(
            f"| {c.key} | {_condition_text(c.conditions)} | {_readout_cell(c.lcms)} | "
            f"{_readout_cell(c.nmr)} | {_fmt(c.difference)} | {c.agreement} | "
            f"{'; '.join(flags) or ''} |"
        )
    counts = defaultdict(int)
    for c in evidence.conditions:
        counts[c.agreement] += 1
    out += [
        "",
        f"{len(evidence.conditions)} conditions: {counts['agree']} agree, "
        f"{counts['disagree']} disagree, {counts['insufficient']} insufficient.",
    ]
    flagged = [w for w in evidence.wells if (w.lcms and w.lcms.flags) or (w.nmr and w.nmr.flags)]
    well_lines = []
    for w in flagged:
        parts = []
        if w.lcms and w.lcms.flags:
            parts.append("LC/MS: " + ", ".join(w.lcms.flags))
        if w.nmr and w.nmr.flags:
            parts.append("NMR: " + ", ".join(w.nmr.flags))
        well_lines.append(f"- {w.well} ({w.key}): {'; '.join(parts)}")
    out += ["", "## Flagged wells", ""] + (well_lines or ["None."])
    out += ["", "Files: " + ", ".join(files), ""]
    return "\n".join(out)


# ---------------------------------------------------------------- NMR readout and pairing


def nmr_and_pair(
    out_dir: Path,
    *,
    plan: Plan,
    ledger: list[LedgerEntry],
    assay: Assay,
    surface: Any,
    plate_seed: int,
    round_number: int,
    nmr_faults: list[str],
    lcms_wells: list[WellResult],
    lcms_qc: QcReport | None,
    reference_expected: float,
    campaign_id: str | None,
    title: str,
    evaluator: Any = None,
    nmr_settings: Any = None,
    lcms_faults: list[str] | None = None,
    lcms_settings: Any = None,
) -> tuple[Evidence, Any, list[Any]]:
    """Read the plate with the simulated NMR, evaluate every spectrum, and pair it with LC/MS.

    Writes nmr/ (spectra.npz, truth.json, nmr_fits.csv, nmr_run.json),
    review/evidence.json, and paired.md under out_dir. Returns the evidence, the
    NMR plate (with its hidden truth, for the fault log), and the fits.

    nmr_settings (NmrSettings, default settings when None) go to the simulated
    NMR and into nmr_run.json. lcms_faults and lcms_settings are what the LC/MS
    ran with. They are only written into paired.md, for people. None of these
    reach the evidence pack.
    """
    from labauto.nmr_analysis import LineshapeFitter, analyze_plate_nmr
    from labauto.virtual.nmr import NmrSettings, load_spectra, read_plate_nmr, write_spectra

    evaluator = evaluator or LineshapeFitter()
    nmr_settings = nmr_settings or NmrSettings()
    nmr_dir = out_dir / "nmr"
    nmr_dir.mkdir(parents=True, exist_ok=True)
    plate = read_plate_nmr(
        ledger,
        plan,
        assay,
        surface,
        plate_seed,
        round_number=round_number,
        faults=nmr_faults,
        settings=nmr_settings,
    )
    write_spectra(nmr_dir, plate)
    write_json(
        nmr_dir / "nmr_run.json",
        {
            "synthetic": True,
            "note": "Settings and injected faults for this simulated NMR run. Hidden from reviewers.",
            "round": round_number,
            "seed": plate_seed,
            "faults": list(nmr_faults),
            "evaluator": evaluator.name,
            "settings": nmr_settings.model_dump(mode="json"),
            "settings_changed": settings_changed(nmr_settings),
        },
    )
    # The evaluator works from the spectra file, the way the LC/MS analysis works from the peak table.
    ppm, spectra = load_spectra(nmr_dir)
    fits, nmr_conditions = analyze_plate_nmr(ppm, spectra, plan, assay, evaluator=evaluator)
    write_nmr_fits(nmr_dir / "nmr_fits.csv", fits)
    evidence = pair_round(
        lcms_wells,
        None,
        fits,
        nmr_conditions,
        plan,
        assay,
        round_number,
        campaign_id=campaign_id,
        evaluator_name=evaluator.name,
        reference_expected=reference_expected,
        lcms_qc=lcms_qc,
    )
    write_evidence(out_dir / "review" / "evidence.json", evidence)
    files = [
        "review/evidence.json (what a reviewer reads)",
        "raw/lcms_peaks.csv",
        "nmr/spectra.npz",
        "nmr/nmr_fits.csv",
        "nmr/truth.json and nmr/nmr_run.json (hidden from reviewers)",
    ]
    simulated = simulated_lines(
        None if lcms_faults is None else faults_on_round(lcms_faults, round_number),
        lcms_settings,
        faults_on_round(nmr_faults, round_number),
        nmr_settings,
    )
    (out_dir / "paired.md").write_text(
        render_paired_md(evidence, title, files, simulated), encoding="utf-8"
    )
    return evidence, plate, fits


# ---------------------------------------------------------------- fault cases

FAULT_ROUND = 1
MILD_DRIFT_SEED, SUBTLE_IMPURITY_SEED, COMBINED_HARD_SEED = 1111, 1212, 1313

# A fixed plate for the reviewer evaluation: 22 conditions in triplicate plus
# the four control wells, 70 wells. A 4 x 3 grid at 20% co-solvent, six points
# that move the co-solvent, and four corners. Yields span low to near optimal.
FAULT_DESIGN_CONDITIONS: list[dict[str, float]] = (
    [
        {"catalyst_mol_pct": c, "base_equiv": b, "cosolvent_pct": 20}
        for c in (1, 2, 4, 6.5)
        for b in (1.0, 1.75, 2.5)
    ]
    + [
        {"catalyst_mol_pct": c, "base_equiv": 1.75, "cosolvent_pct": s}
        for c in (2, 6.5, 10)
        for s in (5, 35)
    ]
    + [
        {"catalyst_mol_pct": 0.5, "base_equiv": 1.0, "cosolvent_pct": 0},
        {"catalyst_mol_pct": 10, "base_equiv": 3.0, "cosolvent_pct": 40},
        {"catalyst_mol_pct": 4, "base_equiv": 0.5, "cosolvent_pct": 20},
        {"catalyst_mol_pct": 8, "base_equiv": 2.25, "cosolvent_pct": 25},
    ]
)

# Hard cases change simulator settings so that no fixed QC rule has a flag
# for the fault. A mild drift: LC/MS product areas x 0.90 instead of 0.75, so
# the reference control stays inside its tolerance and no PLATE_QC_FAIL is
# raised, but the LC/MS reads low against the NMR on most conditions. A subtle
# impurity: 1.5 to 3.5 yield points of extra signal under the product singlet.
# With the singlet versus doublet scatter of a clean well (about 0.5 points sd)
# that stays under the 5 point limit of NMR_SIGNAL_INCONSISTENT, but sits well
# above the scatter, so a careful reader of the pack can still see it.
MILD_DRIFT = {"drift_factor": 0.90}
SUBTLE_IMPURITY = {"impurity_p": 0.10, "impurity_amount": [0.015, 0.035]}

# Each case runs both instruments with its own faults and a fixed seed. Clean
# cases turn every fault off, including the LC/MS injection failure that a
# campaign has on by default. Optional keys: lcms_settings (LcmsSettings
# fields) and nmr_settings (NmrSettings fields), and note, a few words for
# people. hard marks a case that fixed QC has no dedicated flag for.
FAULT_CASES: list[dict] = [
    {"id": "clean_a", "lcms_faults": [], "nmr_faults": [], "seed": 101},
    {"id": "clean_b", "lcms_faults": [], "nmr_faults": [], "seed": 202},
    {
        "id": "injection_failure",
        "lcms_faults": ["injection_failure"],
        "nmr_faults": [],
        "seed": 303,
    },
    {"id": "plate_drift", "lcms_faults": ["plate_drift@1"], "nmr_faults": [], "seed": 404},
    {"id": "nmr_broad_lines", "lcms_faults": [], "nmr_faults": ["nmr_broad_lines@1"], "seed": 505},
    {"id": "nmr_phase_error", "lcms_faults": [], "nmr_faults": ["nmr_phase_error@1"], "seed": 606},
    {"id": "nmr_impurity", "lcms_faults": [], "nmr_faults": ["nmr_impurity"], "seed": 707},
    {
        "id": "combined_wells",
        "lcms_faults": ["injection_failure"],
        "nmr_faults": ["nmr_impurity"],
        "seed": 808,
    },
    {
        "id": "combined_plate",
        "lcms_faults": ["plate_drift@1"],
        "nmr_faults": ["nmr_broad_lines@1"],
        "seed": 909,
    },
    {
        "id": "mild_drift",
        "lcms_faults": ["plate_drift@1"],
        "nmr_faults": [],
        "seed": MILD_DRIFT_SEED,
        "lcms_settings": dict(MILD_DRIFT),
        "hard": True,
        "note": "LC/MS product areas x 0.90; the reference control stays in tolerance",
    },
    {
        "id": "subtle_impurity",
        "lcms_faults": [],
        "nmr_faults": ["nmr_impurity"],
        "seed": SUBTLE_IMPURITY_SEED,
        "nmr_settings": dict(SUBTLE_IMPURITY),
        "hard": True,
        "note": "impurity of 1.5 to 3.5 yield points, under the NMR_SIGNAL_INCONSISTENT limit",
    },
    {
        "id": "combined_hard",
        "lcms_faults": ["plate_drift@1"],
        "nmr_faults": ["nmr_impurity"],
        "seed": COMBINED_HARD_SEED,
        "lcms_settings": dict(MILD_DRIFT),
        "nmr_settings": dict(SUBTLE_IMPURITY),
        "hard": True,
        "note": "mild_drift and subtle_impurity on one plate",
    },
]


def fault_design(assay: Assay) -> Design:
    """The fixed design every fault case runs."""
    return Design(
        design_id="d-fault-eval",
        assay_id=assay.assay_id,
        workcell_id="ot2_bench",
        title="Fixed plate for the reviewer fault evaluation (synthetic)",
        conditions=[dict(c) for c in FAULT_DESIGN_CONDITIONS],
        replicates=3,
        include_controls=True,
        layout={"strategy": "randomized", "seed": 7},
        created_by={"role": "code"},
    )


def fault_plan(assay: Assay) -> Plan:
    from labauto.expand import expand

    return expand(fault_design(assay), assay)


def fault_log(
    lcms_readings: list[Any], lcms_faults: list[str], nmr_plate: Any, round_number: int
) -> list[FaultEvent]:
    """The faults the simulators really applied, from their own records.

    Injection failures come from each LC/MS reading's own flag, drift from the
    simulator's own rule for which rounds drift, impurity wells from the NMR
    truth, and plate wide NMR faults from the NMR plate's list of applied faults.
    Nothing here is read from a quality flag.
    """
    from labauto.virtual.lcms import drifted_rounds

    events = []
    hit = [r.well for r in lcms_readings if r.injection_failure]
    if hit:
        events.append(FaultEvent(kind="injection_failure", wells=hit))
    if round_number in drifted_rounds(lcms_faults):
        events.append(FaultEvent(kind="plate_drift"))
    applied = {str(f).partition("@")[0] for f in getattr(nmr_plate, "plate_faults", [])}
    for kind in ("nmr_broad_lines", "nmr_phase_error"):
        if kind in applied:
            events.append(FaultEvent(kind=kind))
    truth = getattr(nmr_plate, "truth", {}) or {}
    impure = sorted(
        (w for w, t in truth.items() if t.get("impurity")), key=labware.column_order_key
    )
    if impure:
        events.append(FaultEvent(kind="nmr_impurity", wells=impure))
    return events


def build_case(case: dict, out_dir: Path) -> tuple[Evidence, list[FaultEvent]]:
    """Build one fault case: both readouts, analysis, pairing, and the true fault log.

    Writes plan.json, raw/lcms_peaks.csv, raw/lcms_run.json, qc.json, nmr/,
    review/evidence.json, paired.md, and fault_log.json under out_dir. The run
    files and fault_log.json hold the settings and the truth, and are never
    part of the evidence.
    """
    from labauto.config import DEFAULT_ASSAY, load_assay
    from labauto.execute import lcms_seed, ledger_from_plan, measure_and_analyze
    from labauto.virtual.ground_truth import reference_expected, surface_for
    from labauto.virtual.lcms import LcmsSettings
    from labauto.virtual.nmr import NmrSettings

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    assay = load_assay(DEFAULT_ASSAY)
    plan = fault_plan(assay)
    ledger = ledger_from_plan(plan)
    surface = surface_for(0)
    expected = reference_expected(assay, 0)
    seed = int(case["seed"])
    lcms_faults = list(case["lcms_faults"])
    lcms_settings = LcmsSettings(**(case.get("lcms_settings") or {}))
    nmr_settings = NmrSettings(**(case.get("nmr_settings") or {}))
    write_json(out_dir / "plan.json", plan)
    readings, results, qc = measure_and_analyze(
        plan,
        ledger,
        assay,
        surface,
        seed,
        FAULT_ROUND,
        lcms_faults,
        expected,
        settings=lcms_settings,
        peaks_path=out_dir / "raw" / "lcms_peaks.csv",
    )
    write_json(
        out_dir / "raw" / "lcms_run.json",
        {
            "synthetic": True,
            "note": "Settings and injected faults for this simulated LC/MS run. "
            "Hidden from reviewers.",
            "round": FAULT_ROUND,
            "seed": lcms_seed(seed, FAULT_ROUND),
            "faults": lcms_faults,
            "settings": lcms_settings.model_dump(mode="json"),
            "settings_changed": settings_changed(lcms_settings),
        },
    )
    write_json(out_dir / "qc.json", qc)
    evidence, nmr_plate, _ = nmr_and_pair(
        out_dir,
        plan=plan,
        ledger=ledger,
        assay=assay,
        surface=surface,
        plate_seed=lcms_seed(seed, FAULT_ROUND),
        round_number=FAULT_ROUND,
        nmr_faults=list(case["nmr_faults"]),
        lcms_wells=results,
        lcms_qc=qc,
        reference_expected=expected,
        campaign_id=None,
        title=f"Paired LC/MS and NMR readout: fault case {case['id']}",
        nmr_settings=nmr_settings,
        lcms_faults=lcms_faults,
        lcms_settings=lcms_settings,
    )
    events = fault_log(readings, lcms_faults, nmr_plate, FAULT_ROUND)
    wanted = {f.partition("@")[0] for f in case["lcms_faults"] + case["nmr_faults"]}
    missing = sorted(wanted - {e.kind for e in events})
    if missing:
        raise ValueError(
            f"fault case {case['id']}: {', '.join(missing)} did not happen with seed {seed}. "
            "Pick a seed where it does."
        )
    (out_dir / "fault_log.json").write_text(
        json.dumps(
            {
                "case": case["id"],
                "synthetic": True,
                "note": "The faults the simulators applied. Hidden from reviewers.",
                "events": [e.model_dump() for e in events],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return evidence, events
