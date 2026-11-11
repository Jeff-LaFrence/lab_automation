"""Run folders: every file a person needs to see what was checked and what would run.

summary.md is written by code and fits on one screen. It is meant for someone
who does not program.
"""

from __future__ import annotations

import csv
import json
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from labauto import labware
from labauto.config import fmt, fmt_ul
from labauto.models import Assay, CheckResult, PlanError, Workcell
from labauto.schedule import CSV_COLUMNS

LAYER_TITLES = {
    "design": "Design rules",
    "plan": "Plan rules",
    "workcell": "Workcell rules",
    "simulator": "Simulator",
    "ledger": "Ledger",
}


def utc_stamp(now: datetime | None = None) -> str:
    """A UTC timestamp for folder names, for example 20260101T120000Z."""
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ")


def utc_now() -> str:
    """The current time as an ISO 8601 UTC string."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_run_dir(base: str | Path = "runs", prefix: str = "") -> Path:
    """Create runs/<UTC timestamp>_<short id>/ and return it."""
    base = Path(base)
    while True:
        path = base / f"{prefix}{utc_stamp()}_{uuid.uuid4().hex[:4]}"
        if not path.exists():
            path.mkdir(parents=True)
            return path


def display_path(path: str | Path) -> str:
    """A path for records people read: relative to the working folder, or the folder name alone.

    Never a home folder path.
    """
    p = Path(path).resolve()
    try:
        return str(p.relative_to(Path.cwd().resolve()))
    except ValueError:
        return f"<run folder {p.name}>" if p.is_dir() else p.name


def portable(path: str | Path) -> str:
    """A path to store for resuming: relative to the working folder or the repository when possible."""
    from labauto.config import repo_root

    p = Path(path)
    if not p.is_absolute():
        return str(p)
    for base in (Path.cwd().resolve(), repo_root().resolve()):
        try:
            return str(p.resolve().relative_to(base))
        except ValueError:
            continue
    return str(p)


def check_record(result: CheckResult) -> CheckResult:
    """The check result as stored in check.json: the artifact named by file name only."""
    name = Path(result.artifact_path).name if result.artifact_path else None
    return result.model_copy(update={"artifact_path": name})


def write_json(path: Path, data: object) -> None:
    """Write JSON with two space indents. Pydantic models are dumped first."""
    if hasattr(data, "model_dump"):
        data = data.model_dump(mode="json")
    elif isinstance(data, list):
        data = [d.model_dump(mode="json") if hasattr(d, "model_dump") else d for d in data]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def write_check_files(run_dir: Path, result: CheckResult, assay: Assay) -> list[str]:
    """Write the files for one check result. Returns the names written."""
    written: list[str] = []
    if result.design is not None:
        write_json(run_dir / "design.json", result.design)
        written.append("design.json")
    if result.plan is not None:
        write_json(run_dir / "plan.json", result.plan)
        written.append("plan.json")
        write_well_map(run_dir / "well_map.csv", result, assay)
        written.append("well_map.csv")
    if result.worklist is not None:
        with open(run_dir / "worklist.csv", "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(CSV_COLUMNS)
            for row in result.worklist.rows:
                d = row.model_dump(mode="json")
                writer.writerow([d[c] for c in CSV_COLUMNS])
        written.append("worklist.csv")
    if result.artifact_path:
        art = Path(result.artifact_path)
        written.append(art.name)
        if art.with_suffix(".py").exists() and art.suffix != ".py":
            written.append(art.with_suffix(".py").name)
    if result.sim is not None:
        (run_dir / "sim_log.txt").write_text("\n".join(result.sim.steps) + "\n", encoding="utf-8")
        written.append("sim_log.txt")
        if result.sim.ok:
            write_ledger(run_dir / "ledger.csv", result, assay)
            written.append("ledger.csv")
    write_json(run_dir / "errors.json", result.errors)
    return written


def write_ledger(path: Path, result: CheckResult, assay: Assay) -> None:
    """ledger.csv: what the simulator dispensed, per well and reagent (the as-run record)."""
    order = {r: i for i, r in enumerate(assay.addition_order)}
    entries = sorted(
        result.sim.ledger if result.sim else [],
        key=lambda e: (labware.column_order_key(e.dest_well), order.get(e.reagent, 99)),
    )
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["dest_labware", "dest_well", "reagent", "volume_ul"])
        for e in entries:
            writer.writerow([e.dest_labware, e.dest_well, e.reagent, fmt_ul(e.volume_ul)])


def write_well_map(path: Path, result: CheckResult, assay: Assay) -> None:
    """well_map.csv: each well's role, condition values, and reagent volumes as designed."""
    plan = result.plan
    factors = list(assay.factors)
    reagents = list(assay.addition_order)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["well", "role", "condition_index", "replicate"]
            + factors
            + [f"{r}_ul" for r in reagents]
            + ["total_ul"]
        )
        for w in sorted(plan.wells, key=lambda w: labware.column_order_key(w.well)):
            conds = w.conditions or {}
            writer.writerow(
                [
                    w.well,
                    w.role,
                    "" if w.condition_index is None else w.condition_index,
                    w.replicate,
                ]
                + [fmt(conds[f]) if f in conds else "" for f in factors]
                + [fmt_ul(w.add_ul.get(r, 0)) for r in reagents]
                + [fmt_ul(w.total_ul())]
            )


# ---------------------------------------------------------------- summary


def what_would_run(result: CheckResult, assay: Assay, workcell: Workcell) -> list[str]:
    """Plain lines describing the plate: wells, reagent use, tips."""
    lines: list[str] = []
    plan = result.plan
    if plan is None:
        return lines
    if not plan.wells:
        return ["0 wells"]
    samples = [w for w in plan.wells if w.role == "sample"]
    controls = [w for w in plan.wells if w.role != "sample"]
    n_cond = len({w.condition_index for w in samples})
    totals = {round(w.total_ul(), 6) for w in plan.wells}
    each = (
        f", {fmt_ul(next(iter(totals)))} uL each"
        if len(totals) == 1
        else (f", {fmt_ul(min(totals))} to {fmt_ul(max(totals))} uL per well")
    )
    reps = result.design.replicates if result.design else None
    if reps and n_cond * reps == len(samples):
        head = f"{n_cond} conditions x {reps} replicates"
    else:
        head = f"{len(samples)} sample wells"
    lines.append(f"{head} + {len(controls)} controls = {len(plan.wells)} wells{each}")
    if result.worklist is not None:
        drawn: dict[str, float] = defaultdict(float)
        for row in result.worklist.rows:
            drawn[row.reagent] += row.volume_ul
        parts = []
        for reagent, need in drawn.items():
            inv = workcell.inventory.get(reagent)
            if inv is None:
                parts.append((float("inf"), f"{reagent} {fmt_ul(need)} uL (not loaded)"))
                continue
            usable = inv.volume_ul * (1 - workcell.dead_volume_fraction)
            frac = need / usable if usable else float("inf")
            parts.append((frac, f"{reagent} {fmt_ul(need)} of {fmt_ul(usable)} uL usable"))
        parts.sort(key=lambda p: -p[0])
        lines.append("Reagent use: " + ", ".join(p[1] for p in parts))
        lines.append("Tips: " + tip_text(result, workcell))
    return lines


def tip_text(result: CheckResult, workcell: Workcell) -> str:
    """Tips used per pipette, out of its rack. Shared racks are reported per rack instead."""
    rows = result.worklist.rows if result.worklist else []
    racks = [p.tips for p in workcell.pipettes]

    def have(rack: str) -> int:
        entry = workcell.deck_entry(rack)
        spec = labware.get(entry.labware) if entry else None
        return spec.count if spec else 0

    if len(set(racks)) == len(racks):
        parts = []
        for pip in workcell.pipettes:
            used = sum(1 for r in rows if r.pipette == pip.name and r.tip == "new")
            parts.append(f"{pip.name} {used} of {have(pip.tips)}")
        return ", ".join(parts)
    needed = result.worklist.tips_needed if result.worklist else {}
    return ", ".join(
        f"{rack} {needed.get(rack, 0)} of {have(rack)}" for rack in dict.fromkeys(racks)
    )


def check_lines(result: CheckResult) -> list[str]:
    """One line per layer: pass, fail, skipped, or not run."""
    lines = []
    for layer, title in LAYER_TITLES.items():
        status = result.layer_status.get(layer, "not run")
        if layer == "simulator":
            title = f"Simulator ({result.instrument})"
            if status == "pass" and result.sim:
                status = f"pass, {len(result.sim.steps)} steps"
        if layer == "ledger" and status == "pass" and result.plan:
            status = f"simulated dispenses match the plan for all {len(result.plan.wells)} wells"
        if status == "fail":
            codes = sorted({e.code for e in result.errors})
            status = "CAUGHT " + ", ".join(codes)
        lines.append(f"- {title}: {status}")
    return lines


def error_lines(errors: list[PlanError]) -> list[str]:
    """Each error with its message and hint."""
    lines = []
    for e in errors:
        lines.append(f"- {e.code} at {e.where}")
        lines.append(f"  {e.message}")
        if e.fix_hint:
            lines.append(f"  Hint: {e.fix_hint}")
        if e.needs_human:
            lines.append("  A person must fix this. No change to the design can.")
    return lines


def result_title(result: CheckResult) -> str:
    return "PASS" if result.ok else f"CAUGHT at {result.failed_layer}"


def render_summary(
    run_id: str,
    result: CheckResult,
    assay: Assay,
    workcell: Workcell,
    input_label: str,
    files: list[str],
) -> str:
    """The text of summary.md for one check."""
    out = [f"# Run {run_id}: {result_title(result)}", ""]
    out.append(f"Input: {input_label}  ")
    out.append(
        f"Assay: {assay.assay_id}    Workcell: {workcell.workcell_id} ({workcell.instrument})"
    )
    out.append("")
    body = what_would_run(result, assay, workcell)
    if body:
        out.append("## What would be run" if result.ok else "## What was planned")
        out.append("")
        out += [f"- {line}" for line in body]
        out.append("")
    out.append("## Checks")
    out.append("")
    out += check_lines(result)
    out.append("")
    if result.errors:
        out.append("## Errors")
        out.append("")
        out += error_lines(result.errors)
        out.append("")
    out.append(f"Files: {', '.join(files)}  ")
    if result.design_hash:
        design_text = result.design_hash
    elif result.input_kind == "plan":
        design_text = "none (plan input)"
    else:
        design_text = "none (the design did not parse)"
    out.append(f"Design hash: {design_text}  ")
    out.append(f"Protocol hash: {result.protocol_hash or 'none (not compiled)'}")
    return "\n".join(out) + "\n"


def write_run(
    run_dir: Path,
    result: CheckResult,
    assay: Assay,
    workcell: Workcell,
    input_label: str,
    input_data: object,
) -> Path:
    """Write a complete Stage 1 run folder and return the path to summary.md."""
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "input.json", input_data)
    files = write_check_files(run_dir, result, assay)
    summary = render_summary(run_dir.name, result, assay, workcell, input_label, files)
    path = run_dir / "summary.md"
    path.write_text(summary, encoding="utf-8")
    return path
