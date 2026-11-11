"""The planner commands on the command line, and the evaluation page. No network."""

from __future__ import annotations

import json

from conftest import ROOT

from labauto.agents.llm import ScriptedClient
from labauto.cli import main
from labauto.config import Settings
from labauto.orchestrator import Clients
from labauto.planner_eval import evaluate


def only_run(path):
    runs = [p for p in path.iterdir() if p.is_dir()]
    assert len(runs) == 1
    return runs[0]


def plan(tmp_path, scenario):
    return main(
        [
            "plan",
            "--request-file",
            str(ROOT / "examples" / "requests" / f"{scenario}.txt"),
            "--client",
            f"scripted:{ROOT / 'examples' / 'scripted' / f'{scenario}.json'}",
            "--runs",
            str(tmp_path),
        ]
    )


def test_plan_status_approve(tmp_path, capsys):
    assert plan(tmp_path, "02_too_big") == 0
    run = only_run(tmp_path)
    assert "AWAITING_APPROVAL" in capsys.readouterr().out
    assert main(["status", str(run)]) == 0
    assert main(["approve", str(run), "--by", "tester", "--comment", "ok"]) == 0
    approval = json.loads((run / "approval.json").read_text())
    assert approval["mode"] == "human" and approval["by"] == "tester"
    assert main(["approve", str(run)]) == 1  # already approved


def test_reject_needs_a_reason_and_records_it(tmp_path):
    plan(tmp_path, "01_simple_factorial")
    run = only_run(tmp_path)
    assert main(["reject", str(run), "--reason", "not today"]) == 0
    assert json.loads((run / "state.json").read_text())["state"] == "REJECTED"


def test_live_without_an_endpoint_says_so(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for var in ("LABAUTO_MAIN_BASE_URL",):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("labauto.cli_plan.load_settings", lambda: Settings())
    assert main(["plan", "anything", "--runs", str(tmp_path / "runs")]) == 2
    assert "No model endpoint is configured" in capsys.readouterr().out
    assert main(["eval-planner", "--out", str(tmp_path / "eval.md")]) == 1
    assert not (tmp_path / "eval.md").exists()


def test_resume_command_uses_the_recorded_client(tmp_path):
    plan(tmp_path, "03_out_of_range")
    run = only_run(tmp_path)
    assert main(["resume", str(run)]) == 0


def test_evaluation_page_from_scripted_runs(tmp_path):
    def factory():
        return Clients(
            local=ScriptedClient(
                json.loads((ROOT / "examples/scripted/05_reagent_limit.json").read_text())
            )
        )

    out = evaluate(
        factory,
        Settings(),
        trials=2,
        runs_dir=tmp_path / "runs",
        out_path=tmp_path / "eval.md",
        scenarios=["05_reagent_limit"],
    )
    text = out.read_text()
    assert "| 05_reagent_limit | 2 | 0 | 2 | 0 | 0 | 2 of 2 passing |" in text
    assert "Model, as reported by the endpoint: scripted" in text
    assert "TOO_MANY_WELLS: 2 times" in text and "SOURCE_SHORT: 2 times" in text
    assert "Example from 05_reagent_limit, attempt 1" in text


def test_demo_shows_three_planner_scenarios(tmp_path, capsys):
    assert main(["demo", "--runs", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    rows = [" ".join(line.split()) for line in out.splitlines()]
    assert "01_simple_factorial AWAITING_APPROVAL 1 40 wells, no deviations (scripted)" in rows
    assert any(
        r.startswith("02_too_big AWAITING_APPROVAL 2 84 wells, repaired TOO_MANY_WELLS")
        for r in rows
    )
    assert any(r.startswith("03_out_of_range AWAITING_APPROVAL 3") for r in rows)
    assert "scripted session (hand-written replies, not a model)" in out
    assert "UNEXPECTED" not in out
