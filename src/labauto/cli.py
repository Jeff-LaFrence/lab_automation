"""Command line entry point: labauto check, labauto demo."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from labauto.config import DEFAULT_ASSAY, DEFAULT_WORKCELL, load_assay, load_json, load_workcell
from labauto.models import Assay, CheckResult, Workcell
from labauto.pipeline import check
from labauto.record import new_run_dir, result_title, write_run
from labauto.validate import parse_design, parse_plan

EXIT_PASS, EXIT_CAUGHT, EXIT_INTERNAL = 0, 1, 2


def looks_like_plan(data: object) -> bool:
    """A plan has a list of wells. Anything else is read as a design."""
    return isinstance(data, dict) and "wells" in data


def check_data(data: object, assay: Assay, workcell: Workcell, out_dir: Path) -> CheckResult:
    """Parse raw JSON as a design or a plan, then run the check pipeline on it."""
    if looks_like_plan(data):
        item, errors = parse_plan(data)
        layer, kind = "plan", "plan"
    else:
        item, errors = parse_design(data)
        layer, kind = "design", "design"
    if item is None:
        return CheckResult(
            ok=False,
            failed_layer=layer,
            errors=errors,
            input_kind=kind,
            instrument=workcell.instrument,
            layer_status={"design": "not applicable", "plan": "fail"}
            if layer == "plan"
            else {"design": "fail"},
        )
    return check(item, assay, workcell, out_dir=out_dir)


def input_label(path: str | Path) -> str:
    """How the input file is named in run records: relative to here, never a home path."""
    p = Path(path).resolve()
    try:
        return str(p.relative_to(Path.cwd().resolve()))
    except ValueError:
        return p.name


def cmd_check(args: argparse.Namespace) -> int:
    assay = load_assay(args.assay)
    workcell = load_workcell(args.workcell)
    data = load_json(args.file)
    run_dir = new_run_dir(args.runs)
    result = check_data(data, assay, workcell, run_dir)
    summary = write_run(run_dir, result, assay, workcell, input_label(args.file), data)
    print(f"{result_title(result)}    run folder: {run_dir}")
    for e in result.errors:
        print(f"  {e.code} at {e.where}: {e.message}")
        if e.fix_hint:
            print(f"    hint: {e.fix_hint}")
    print(f"Summary: {summary}")
    return EXIT_PASS if result.ok else EXIT_CAUGHT


# ---------------------------------------------------------------- demo


@dataclass
class DemoCase:
    name: str
    file: str
    workcell: str
    expect_layer: str | None
    expect_code: str | None = None


STAGE1_CASES = [
    DemoCase(
        "good_factorial", "examples/designs/good_factorial.json", "configs/workcell_ot2.json", None
    ),
    DemoCase(
        "bad_off_grid",
        "examples/designs/bad_off_grid.json",
        "configs/workcell_ot2.json",
        "design",
        "FACTOR_OFF_GRID",
    ),
    DemoCase(
        "bad_too_many_wells",
        "examples/designs/bad_too_many_wells.json",
        "configs/workcell_ot2.json",
        "design",
        "TOO_MANY_WELLS",
    ),
    DemoCase(
        "bad_reagent_short",
        "examples/designs/bad_reagent_short.json",
        "configs/workcell_ot2.json",
        "workcell",
        "SOURCE_SHORT",
    ),
    DemoCase(
        "bad_overfill",
        "examples/plans/bad_overfill.json",
        "configs/workcell_ot2.json",
        "plan",
        "WELL_OVERFILL",
    ),
    DemoCase(
        "deck_conflict",
        "examples/designs/good_factorial.json",
        "configs/workcell_ot2_conflict.json",
        "workcell",
        "DECK_CONFLICT",
    ),
]


def table_row(case: str, result: str, layer: str, detail: str) -> str:
    return f"{case:<29}{result:<9}{layer:<11}{detail}".rstrip()


def run_demo_case(case: DemoCase, assay: Assay, runs: str) -> tuple[str, bool, CheckResult]:
    """Run one demo case. Returns its table row, whether it matched, and the check result."""
    workcell = load_workcell(case.workcell)
    data = load_json(case.file)
    run_dir = new_run_dir(runs)
    result = check_data(data, assay, workcell, run_dir)
    write_run(run_dir, result, assay, workcell, case.file, data)
    if result.ok:
        steps = len(result.sim.steps) if result.sim else 0
        detail = f"{len(result.plan.wells)} wells, {steps} steps simulated, ledger matches"
        row = table_row(case.name, "PASS", "", detail)
        matched = case.expect_layer is None
    else:
        row = table_row(case.name, "CAUGHT", result.failed_layer or "", result.errors[0].brief())
        matched = (
            result.failed_layer == case.expect_layer
            and bool(result.errors)
            and result.errors[0].code == case.expect_code
        )
    if not matched:
        row += "    (UNEXPECTED)"
    return row, matched, result


def tecan_row(assay: Assay, runs: str, ot2_result: CheckResult | None) -> tuple[str, bool]:
    """The second instrument row: the good design on the Tecan EVO workcell."""
    name = "good_factorial (tecan_evo)"
    if importlib.util.find_spec("pylabrobot") is None:
        return table_row(name, "SKIPPED", "", "(pylabrobot not installed)"), True
    case = DemoCase(
        name, "examples/designs/good_factorial.json", "configs/workcell_tecan_evo.json", None
    )
    row, matched, result = run_demo_case(case, assay, runs)
    if not matched:
        return row, False
    same = ot2_result is not None and ot2_result.plan == result.plan
    if same:
        return table_row(name, "PASS", "", "same plan, second instrument"), True
    return table_row(name, "PASS", "", "plans differ between instruments    (UNEXPECTED)"), False


def cmd_demo(args: argparse.Namespace) -> int:
    start = time.perf_counter()
    assay = load_assay(DEFAULT_ASSAY)
    print("Stage 1: designs and plans through the check pipeline")
    print()
    print(table_row("case", "result", "layer", "detail"))
    all_ok = True
    good = None
    for case in STAGE1_CASES:
        row, matched, result = run_demo_case(case, assay, args.runs)
        if case.name == "good_factorial":
            good = result
        all_ok &= matched
        print(row)
    row, matched = tecan_row(assay, args.runs, good)
    all_ok &= matched
    print(row)

    from labauto.cli_plan import demo_planner

    all_ok &= demo_planner(args.runs, live=getattr(args, "live", False))

    from labauto.cli_loop import demo_loop

    all_ok &= demo_loop(args.runs)
    print(f"Run folders are in {args.runs}/. Finished in {time.perf_counter() - start:.1f} s.")
    return EXIT_PASS if all_ok else EXIT_CAUGHT


# ---------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="labauto",
        description="Check, compile, and simulate liquid handler protocols from experiment designs.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="check a design or plan file through every layer")
    p.add_argument("file", help="a design or plan JSON file")
    p.add_argument("--assay", default=DEFAULT_ASSAY)
    p.add_argument("--workcell", default=DEFAULT_WORKCELL)
    p.add_argument("--runs", default="runs", help="folder for run records")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("demo", help="run the demo cases with no network")
    p.add_argument("--runs", default="runs", help="folder for run records")
    p.add_argument(
        "--live", action="store_true", help="run the planner scenarios on the configured endpoint"
    )
    p.set_defaults(func=cmd_demo)

    from labauto.cli_loop import add_parsers as add_loop_parsers
    from labauto.cli_nmr import add_parsers as add_nmr_parsers
    from labauto.cli_paired import add_parsers as add_paired_parsers
    from labauto.cli_plan import add_parsers
    from labauto.cli_review import add_parsers as add_review_parsers

    add_parsers(sub)
    add_loop_parsers(sub)
    add_nmr_parsers(sub)
    add_paired_parsers(sub)
    add_review_parsers(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INTERNAL
    except Exception:
        print("internal error:", file=sys.stderr)
        traceback.print_exc()
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
