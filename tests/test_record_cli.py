"""Run records and the command line."""

from __future__ import annotations

import json

from labauto.cli import main

RUN_FILES = {
    "input.json",
    "design.json",
    "plan.json",
    "worklist.csv",
    "protocol.py",
    "sim_log.txt",
    "ledger.csv",
    "well_map.csv",
    "errors.json",
    "summary.md",
}


def only_run(path):
    runs = [p for p in path.iterdir() if p.is_dir()]
    assert len(runs) == 1
    return runs[0]


def test_passing_run_writes_every_file(root, tmp_path):
    code = main(
        ["check", str(root / "examples/designs/good_factorial.json"), "--runs", str(tmp_path)]
    )
    assert code == 0
    run = only_run(tmp_path)
    assert RUN_FILES <= {p.name for p in run.iterdir()}
    summary = (run / "summary.md").read_text()
    assert summary.startswith(f"# Run {run.name}: PASS")
    assert "12 conditions x 3 replicates + 4 controls = 40 wells, 200 uL each" in summary
    assert "catalyst 278 of 1080 uL usable" in summary
    assert "Tips: small 38 of 96, large 4 of 96" in summary
    assert "Ledger: simulated dispenses match the plan for all 40 wells" in summary
    assert json.loads((run / "errors.json").read_text()) == []


def test_caught_run_title_and_errors(root, tmp_path):
    code = main(
        ["check", str(root / "examples/designs/bad_off_grid.json"), "--runs", str(tmp_path)]
    )
    assert code == 1
    run = only_run(tmp_path)
    summary = (run / "summary.md").read_text()
    assert summary.startswith(f"# Run {run.name}: CAUGHT at design")
    assert "FACTOR_OFF_GRID" in summary
    assert "Hint:" in summary
    errors = json.loads((run / "errors.json").read_text())
    assert errors[0]["code"] == "FACTOR_OFF_GRID"


def test_plan_input_is_detected(root, tmp_path):
    code = main(["check", str(root / "examples/plans/bad_overfill.json"), "--runs", str(tmp_path)])
    assert code == 1
    summary = (only_run(tmp_path) / "summary.md").read_text()
    assert "CAUGHT at plan" in summary
    assert "WELL_OVERFILL" in summary


def test_exit_code_2_on_internal_error(tmp_path):
    assert main(["check", str(tmp_path / "missing.json"), "--runs", str(tmp_path)]) == 2
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert main(["check", str(bad), "--runs", str(tmp_path)]) == 2


def test_schema_invalid_is_a_caught_error(tmp_path):
    f = tmp_path / "d.json"
    f.write_text(json.dumps({"title": "x", "replicates": "many"}))
    assert main(["check", str(f), "--runs", str(tmp_path / "runs")]) == 1


def test_demo_prints_six_expected_rows(tmp_path, capsys):
    assert main(["demo", "--runs", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "UNEXPECTED" not in out
    for line in [
        "bad_off_grid             CAUGHT   design     FACTOR_OFF_GRID catalyst_mol_pct=2.2 (nearest 2.0, 2.5)",
        "bad_too_many_wells       CAUGHT   design     TOO_MANY_WELLS 120 needed, 92 available",
        "bad_reagent_short        CAUGHT   workcell   SOURCE_SHORT catalyst 1328 uL needed, 1080 usable",
        "bad_overfill             CAUGHT   plan       WELL_OVERFILL plate:E1 600 uL, limit 250",
        "deck_conflict            CAUGHT   workcell   DECK_CONFLICT slot 2: plate, reservoir",
    ]:
        assert line in out
    assert "good_factorial           PASS                40 wells," in out
