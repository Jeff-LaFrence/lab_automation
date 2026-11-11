"""Turn a raw peak table into yields, then run the quality checks.

Per well:
- yield_pct = 100 * (product area / IS area) / k_product
- conversion_pct = 100 * (1 - (substrate area / IS area) / k_substrate)
- IS_OUT_OF_RANGE when the internal standard is more than 25% from the plate
  median. The well is excluded.

Per condition: mean, standard deviation, and count over wells not excluded.
REPLICATE_SPREAD when the range is above 15 points, LOW_REPLICATES when fewer
than 2 wells remain. Both are kept and flagged.

Per plate: the negative control mean must be below its limit, and the
reference control mean within its tolerance of the expected value. If either
fails, PLATE_QC_FAIL: every well on the plate is excluded.
"""

from __future__ import annotations

import statistics
from collections import defaultdict

from pydantic import BaseModel, Field

from labauto import labware
from labauto.models import Assay, Plan, QcEvent, QcReport
from labauto.virtual.lcms import as_run_conditions


class WellResult(BaseModel):
    """One analyzed well: as designed, as run, measured, and its quality flags."""

    well: str
    role: str
    condition_index: int | None
    replicate: int
    designed: dict[str, float] | None
    as_run: dict[str, float]
    volumes: dict[str, float]
    substrate_area: float
    product_area: float
    is_area: float
    yield_pct: float
    conversion_pct: float
    flags: list[str] = Field(default_factory=list)
    excluded: bool = False
    exclude_reason: str = ""


def well_yield(product: float, is_area: float, k_product: float) -> float:
    return 100.0 * (product / is_area) / k_product if is_area > 0 else 0.0


def well_conversion(substrate: float, is_area: float, k_substrate: float) -> float:
    return 100.0 * (1 - (substrate / is_area) / k_substrate) if is_area > 0 else 0.0


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def analyze_plate(
    peaks: dict[str, dict[str, float]],
    plan: Plan,
    volumes: dict[str, dict[str, float]],
    assay: Assay,
    reference_expected: float,
    round_number: int,
    attempt: int = 1,
) -> tuple[list[WellResult], QcReport]:
    """Analyze one plate. Returns per well results and the plate's quality report."""
    roles = {w.well: w for w in plan.wells}
    tol = assay.qc.internal_standard_tolerance
    events: list[QcEvent] = []
    results: list[WellResult] = []

    is_values = [p.get("internal_standard", 0.0) for p in peaks.values()]
    is_median = statistics.median(is_values) if is_values else 0.0
    for well in sorted(peaks, key=labware.column_order_key):
        p = peaks[well]
        pw = roles.get(well)
        vols = volumes.get(well, {})
        res = WellResult(
            well=well,
            role=pw.role if pw else "unknown",
            condition_index=pw.condition_index if pw else None,
            replicate=pw.replicate if pw else 0,
            designed=pw.conditions if pw else None,
            as_run=as_run_conditions(vols, assay),
            volumes=vols,
            substrate_area=p.get("substrate", 0.0),
            product_area=p.get("product", 0.0),
            is_area=p.get("internal_standard", 0.0),
            yield_pct=well_yield(
                p.get("product", 0.0), p.get("internal_standard", 0.0), assay.analysis.k_product
            ),
            conversion_pct=well_conversion(
                p.get("substrate", 0.0), p.get("internal_standard", 0.0), assay.analysis.k_substrate
            ),
        )
        if is_median > 0 and abs(res.is_area - is_median) / is_median > tol:
            res.flags.append("IS_OUT_OF_RANGE")
            res.excluded = True
            res.exclude_reason = "IS_OUT_OF_RANGE"
            events.append(
                QcEvent(
                    code="IS_OUT_OF_RANGE",
                    where=well,
                    message=(
                        f"Internal standard area {res.is_area:.0f} is "
                        f"{abs(res.is_area - is_median) / is_median * 100:.0f}% from the plate median "
                        f"{is_median:.0f}. The limit is {tol * 100:.0f}%. The well is excluded."
                    ),
                )
            )
        results.append(res)

    by_condition: dict[int, list[WellResult]] = defaultdict(list)
    for r in results:
        if r.role == "sample" and r.condition_index is not None:
            by_condition[r.condition_index].append(r)
    spread_max = assay.qc.replicate_spread_max
    for ci, wells in sorted(by_condition.items()):
        valid = [w.yield_pct for w in wells if not w.excluded]
        flags = []
        if len(valid) >= 2 and max(valid) - min(valid) > spread_max:
            flags.append("REPLICATE_SPREAD")
            events.append(
                QcEvent(
                    code="REPLICATE_SPREAD",
                    where=f"condition {ci}",
                    message=f"Replicates of condition {ci} span {max(valid) - min(valid):.1f} points. The limit is {spread_max:g}.",
                )
            )
        if len(valid) < 2:
            flags.append("LOW_REPLICATES")
            events.append(
                QcEvent(
                    code="LOW_REPLICATES",
                    where=f"condition {ci}",
                    message=f"Condition {ci} has {len(valid)} valid well(s). At least 2 are wanted.",
                )
            )
        for w in wells:
            w.flags += flags

    report = QcReport(round=round_number, attempt=attempt, plate_ok=True, is_median=is_median)
    problems = []
    neg = assay.controls.get("negative")
    if neg is not None:
        vals = [r.yield_pct for r in results if r.role == "negative_control" and not r.excluded]
        report.negative_mean = _mean(vals)
        report.negative_limit = neg.expect.get("yield_max")
        if report.negative_mean is None:
            problems.append("no valid negative control well")
        elif report.negative_limit is not None and report.negative_mean >= report.negative_limit:
            problems.append(
                f"negative control mean {report.negative_mean:.1f}% is not below {report.negative_limit:g}%"
            )
    ref = assay.controls.get("reference")
    if ref is not None:
        vals = [r.yield_pct for r in results if r.role == "reference_control" and not r.excluded]
        report.reference_mean = _mean(vals)
        report.reference_expected = reference_expected
        report.reference_tolerance = ref.expect.get("yield_tolerance")
        if report.reference_mean is None:
            problems.append("no valid reference control well")
        elif (
            report.reference_tolerance is not None
            and abs(report.reference_mean - reference_expected) > report.reference_tolerance
        ):
            problems.append(
                f"reference control mean {report.reference_mean:.1f}% is more than "
                f"{report.reference_tolerance:g} points from the expected {reference_expected:.1f}%"
            )
    if problems:
        report.plate_ok = False
        events.append(
            QcEvent(
                code="PLATE_QC_FAIL",
                where=f"round {round_number}",
                message="Plate failed its controls: "
                + "; ".join(problems)
                + ". Every well is excluded.",
            )
        )
        for r in results:
            r.excluded = True
            r.exclude_reason = "; ".join(x for x in (r.exclude_reason, "PLATE_QC_FAIL") if x)
            if "PLATE_QC_FAIL" not in r.flags:
                r.flags.append("PLATE_QC_FAIL")
    report.excluded_wells = [r.well for r in results if r.excluded]
    report.events = events
    return results, report
