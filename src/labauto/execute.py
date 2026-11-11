"""One plate, after approval: measure it, analyze it, and turn it into results rows.

The campaign loop and the benchmark share these steps. The loop feeds them the
simulator's ledger. The benchmark's fast path feeds them the plan's volumes.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from labauto.analysis import WellResult, analyze_plate
from labauto.config import fmt_ul
from labauto.models import Assay, Decision, Design, Layout, LedgerEntry, Plan, QcReport
from labauto.virtual.ground_truth import Surface
from labauto.virtual.lcms import LcmsSettings, WellReading, read_plate, write_peaks


def design_for_round(
    decision: Decision,
    assay: Assay,
    workcell_id: str,
    campaign_id: str,
    round_number: int,
    replicates: int,
    seed: int,
    attempt: int = 1,
) -> Design:
    """Code builds the plate's design from the picks: explicit conditions, controls on."""
    role = {"random": "random", "optimizer": "optimizer", "model": "model"}.get(
        decision.strategy, "code"
    )
    if decision.fallback and decision.strategy == "model":
        role = "optimizer"
    suffix = f"-a{attempt}" if attempt > 1 else ""
    return Design(
        design_id=f"d-{campaign_id}-r{round_number:02d}{suffix}",
        assay_id=assay.assay_id,
        workcell_id=workcell_id,
        title=f"Round {round_number} of campaign {campaign_id} ({decision.strategy})",
        conditions=[dict(p.conditions) for p in decision.picks],
        replicates=replicates,
        include_controls=True,
        layout=Layout(strategy="randomized", seed=seed * 100 + round_number),
        created_by={"role": role, "model": decision.model, "rung": None},
    )


def ledger_from_plan(plan: Plan) -> list[LedgerEntry]:
    """The fast path: take the plan's volumes as the as-run record."""
    return [
        LedgerEntry(dest_labware=plan.plate, dest_well=w.well, reagent=r, volume_ul=v)
        for w in plan.wells
        for r, v in w.add_ul.items()
        if v > 0
    ]


def lcms_seed(seed: int, round_number: int, attempt: int = 1) -> int:
    """The instrument's noise seed for one plate."""
    return seed * 10_000 + round_number * 10 + attempt


def volumes_by_well(ledger: list[LedgerEntry], plate: str) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for e in ledger:
        if e.dest_labware == plate:
            out[e.dest_well][e.reagent] = out[e.dest_well].get(e.reagent, 0.0) + e.volume_ul
    return dict(out)


def measure_and_analyze(
    plan: Plan,
    ledger: list[LedgerEntry],
    assay: Assay,
    surface: Surface,
    seed: int,
    round_number: int,
    faults: list[str],
    reference_expected: float,
    attempt: int = 1,
    settings: LcmsSettings | None = None,
    peaks_path: Path | None = None,
) -> tuple[list[WellReading], list[WellResult], QcReport]:
    """Read the plate with the simulated LC/MS, then analyze the peak table it wrote."""
    from labauto.virtual.lcms import load_peaks

    s = lcms_seed(seed, round_number, attempt)
    readings = read_plate(ledger, plan, assay, surface, s, round_number, faults, settings)
    if peaks_path is not None:
        write_peaks(peaks_path, readings, s, settings)
        peaks = load_peaks(peaks_path)
    else:
        peaks = {
            r.well: {
                "substrate": r.substrate_area,
                "product": r.product_area,
                "internal_standard": r.is_area,
            }
            for r in readings
        }
    results, qc = analyze_plate(
        peaks,
        plan,
        volumes_by_well(ledger, plan.plate),
        assay,
        reference_expected,
        round_number,
        attempt,
    )
    return readings, results, qc


def result_rows(
    results: list[WellResult],
    assay: Assay,
    *,
    campaign_id: str,
    round_number: int,
    attempt: int,
    run_id: str,
    design_hash: str | None,
    protocol_hash: str | None,
    decided_by: str,
    timestamp: str,
) -> list[dict]:
    """One results row per well, with as-designed and as-run values side by side."""
    rows = []
    for r in results:
        row = {
            "campaign_id": campaign_id,
            "round": round_number,
            "attempt": attempt,
            "run_id": run_id,
            "well": r.well,
            "role": r.role,
            "condition_index": "" if r.condition_index is None else r.condition_index,
            "replicate": r.replicate,
        }
        for f in assay.factors:
            row[f"designed_{f}"] = "" if not r.designed or f not in r.designed else r.designed[f]
        for f in assay.factors:
            row[f"asrun_{f}"] = r.as_run.get(f, "")
        for reagent in assay.addition_order:
            row[f"{reagent}_ul"] = fmt_ul(r.volumes.get(reagent, 0.0))
        row.update(
            {
                "substrate_area": round(r.substrate_area, 4),
                "product_area": round(r.product_area, 4),
                "is_area": round(r.is_area, 4),
                "yield_pct": round(r.yield_pct, 4),
                "conversion_pct": round(r.conversion_pct, 4),
                "qc_flags": ";".join(r.flags),
                "excluded": "true" if r.excluded else "false",
                "exclude_reason": r.exclude_reason,
                "design_hash": design_hash or "",
                "protocol_hash": protocol_hash or "",
                "decided_by": decided_by,
                "timestamp": timestamp,
            }
        )
        rows.append(row)
    return rows


def write_rows(path: Path, rows: list[dict]) -> None:
    """Write results rows as CSV."""
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
