"""The check pipeline: every layer, in order, stopping at the first layer that fails.

Order: design rules, expand, plan rules, schedule, workcell rules, compile,
simulate, ledger check. A Plan input starts at the plan rules.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from labauto import labware
from labauto.config import TOLERANCE, fmt_precise, repo_root
from labauto.expand import expand
from labauto.instruments import get_instrument
from labauto.models import Assay, CheckResult, Design, Plan, PlanError, SimResult, Workcell
from labauto.schedule import schedule
from labauto.validate import validate_design, validate_plan, validate_worklist

LAYERS = ["design", "plan", "workcell", "simulator", "ledger"]


def canonical_json(obj: object) -> str:
    """JSON with sorted keys and no spaces, so equal content gives an equal hash."""
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump(mode="json")
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def design_hash(design: Design) -> str:
    """sha256 of the design's canonical JSON."""
    return sha256_text(canonical_json(design))


def file_hash(path: str | Path) -> str:
    """sha256 of a file's bytes, used for the compiled protocol."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fill_ids(design: Design, assay: Assay, workcell: Workcell) -> Design:
    """Fill the fields code owns: the design id, assay id, and workcell id when empty."""
    updates: dict = {}
    if not design.assay_id:
        updates["assay_id"] = assay.assay_id
    if not design.workcell_id:
        updates["workcell_id"] = workcell.workcell_id
    filled = design.model_copy(update=updates)
    if not filled.design_id:
        filled = filled.model_copy(update={"design_id": "d-" + design_hash(filled)[:12]})
    return filled


def ledger_check(plan: Plan, sim: SimResult) -> list[PlanError]:
    """Compare what the simulator dispensed with the plan, well by well and in total."""
    got: dict[tuple[str, str], float] = defaultdict(float)
    errors: list[PlanError] = []
    for e in sim.ledger:
        if e.dest_labware != plan.plate:
            errors.append(
                _mismatch(
                    f"{e.dest_labware}:{e.dest_well}:{e.reagent}",
                    f"The simulator dispensed {fmt_precise(e.volume_ul)} uL of {e.reagent} into "
                    f"{e.dest_labware} {e.dest_well}, which is not the plate.",
                    f"{e.dest_labware}:{e.dest_well} unexpected",
                )
            )
            continue
        got[(e.dest_well, e.reagent)] += e.volume_ul
    want = {(w.well, r): v for w in plan.wells for r, v in w.add_ul.items() if v > 0}
    keys = sorted(set(got) | set(want), key=lambda k: (labware.column_order_key(k[0]), k[1]))
    for well, reagent in keys:
        g, w = got.get((well, reagent), 0.0), want.get((well, reagent), 0.0)
        if abs(g - w) > TOLERANCE:
            errors.append(
                _mismatch(
                    f"{plan.plate}:{well}:{reagent}",
                    f"The plan puts {fmt_precise(w)} uL of {reagent} in {well}. "
                    f"The simulator dispensed {fmt_precise(g)} uL.",
                    f"{plan.plate}:{well} {reagent} planned {fmt_precise(w)}, dispensed {fmt_precise(g)}",
                )
            )
    if sim.well_volumes is not None:
        planned = {w.well: w.total_ul() for w in plan.wells if w.total_ul() > 0}
        for well in sorted(set(planned) | set(sim.well_volumes), key=labware.column_order_key):
            g, w = sim.well_volumes.get(well, 0.0), planned.get(well, 0.0)
            if abs(g - w) > TOLERANCE:
                errors.append(
                    _mismatch(
                        f"{plan.plate}:{well}:tracked",
                        f"The plan fills {well} to {fmt_precise(w)} uL. The simulator's volume "
                        f"tracker holds {fmt_precise(g)} uL there.",
                        f"{plan.plate}:{well} planned {fmt_precise(w)}, tracked {fmt_precise(g)}",
                    )
                )
    by_reagent: dict[str, float] = defaultdict(float)
    for (_, reagent), v in got.items():
        by_reagent[reagent] += v
    for reagent in sorted(set(by_reagent) | set(plan.totals_ul)):
        g, w = by_reagent.get(reagent, 0.0), plan.totals_ul.get(reagent, 0.0)
        if abs(g - w) > TOLERANCE:
            errors.append(
                _mismatch(
                    f"total:{reagent}",
                    f"The plan totals {fmt_precise(w)} uL of {reagent}. "
                    f"The simulator drew {fmt_precise(g)} uL in all.",
                    f"total {reagent} planned {fmt_precise(w)}, dispensed {fmt_precise(g)}",
                )
            )
    return errors


def _mismatch(where: str, message: str, short: str) -> PlanError:
    return PlanError(
        code="LEDGER_MISMATCH",
        layer="ledger",
        where=where,
        message=message,
        fix_hint="This points to a fault in this project's compiler. Keep the run folder.",
        options=["ask_human"],
        needs_human=True,
        short=short,
    )


def _fail(result: CheckResult, layer: str, errors: list[PlanError]) -> CheckResult:
    result.ok = False
    result.failed_layer = layer
    result.errors = errors
    result.layer_status[layer] = "fail"
    return result


def check(
    item: Design | Plan,
    assay: Assay,
    workcell: Workcell,
    out_dir: str | Path | None = None,
    skip_layers: Iterable[str] = (),
) -> CheckResult:
    """Run a design or a plan through every layer. Stops at the first layer with errors.

    out_dir receives the compiled protocol. skip_layers exists for tests only:
    it lets a test send a bad plan straight to the simulator.
    """
    skip = set(skip_layers)
    result = CheckResult(
        ok=False,
        input_kind="design" if isinstance(item, Design) else "plan",
        instrument=workcell.instrument,
        layer_status={layer: "not run" for layer in LAYERS},
    )

    if isinstance(item, Design):
        design = fill_ids(item, assay, workcell)
        result.design = design
        result.design_hash = design_hash(design)
        if "design" in skip:
            result.layer_status["design"] = "skipped"
        else:
            errors = validate_design(design, assay)
            if errors:
                return _fail(result, "design", errors)
            result.layer_status["design"] = "pass"
        plan = expand(design, assay, check=False)
    else:
        result.layer_status["design"] = "not applicable"
        plan = item
    result.plan = plan

    if "plan" in skip:
        result.layer_status["plan"] = "skipped"
    else:
        errors = validate_plan(plan, assay)
        if errors:
            return _fail(result, "plan", errors)
        result.layer_status["plan"] = "pass"

    worklist = schedule(plan, workcell, assay)
    result.worklist = worklist
    if "workcell" in skip:
        result.layer_status["workcell"] = "skipped"
    else:
        errors = validate_worklist(worklist, plan, workcell, assay)
        if errors:
            return _fail(result, "workcell", errors)
        result.layer_status["workcell"] = "pass"

    inst = get_instrument(workcell.instrument)
    if out_dir is None:
        scratch = repo_root() / ".labauto" / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        out_dir = tempfile.mkdtemp(prefix="check_", dir=scratch)
    artifact = inst.compile(worklist, workcell, plan, Path(out_dir))
    result.artifact_path = str(artifact)
    result.protocol_hash = file_hash(artifact)

    sim = inst.simulate(artifact, workcell)
    result.sim = sim
    if not sim.ok:
        return _fail(result, "simulator", [sim.error] if sim.error else [])
    result.layer_status["simulator"] = "pass"

    if "ledger" in skip:
        result.layer_status["ledger"] = "skipped"
    else:
        errors = ledger_check(plan, sim)
        if errors:
            return _fail(result, "ledger", errors)
        result.layer_status["ledger"] = "pass"

    result.ok = True
    return result
