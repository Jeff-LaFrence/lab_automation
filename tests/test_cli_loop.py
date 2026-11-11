"""The closed loop on the command line."""

from __future__ import annotations

import json

from conftest import ROOT

from labauto.cli import main


def only(path):
    items = [p for p in path.iterdir() if p.is_dir()]
    assert len(items) == 1
    return items[0]


def test_loop_with_a_person_approving_each_round(tmp_path, capsys):
    args = [
        "loop",
        "--strategy",
        "random",
        "--rounds",
        "2",
        "--approval",
        "always",
        "--campaigns",
        str(tmp_path),
    ]
    assert main(args) == 1  # waiting for a person
    cdir = only(tmp_path)
    assert main(["status", str(cdir)]) == 0
    assert "AWAITING_APPROVAL" in capsys.readouterr().out
    for _ in range(2):
        assert main(["approve", str(cdir), "--by", "tester"]) == 0
        code = main(["resume", str(cdir)])
    assert code == 0
    state = json.loads((cdir / "state.json").read_text())
    assert state["state"] == "DONE"
    approval = json.loads((cdir / "rounds" / "02" / "approval.json").read_text())
    assert approval["mode"] == "human" and approval["by"] == "tester"
    assert main(["report", str(cdir)]) == 0


def test_approve_refuses_a_campaign_that_is_not_waiting(tmp_path):
    main(
        [
            "loop",
            "--strategy",
            "random",
            "--rounds",
            "1",
            "--approval",
            "auto_demo",
            "--campaigns",
            str(tmp_path),
        ]
    )
    assert main(["approve", str(only(tmp_path))]) == 1


def test_model_strategy_needs_a_client(tmp_path, monkeypatch, capsys):
    from labauto.config import Settings

    monkeypatch.setattr("labauto.cli_plan.load_settings", lambda: Settings())
    assert main(["loop", "--strategy", "model", "--campaigns", str(tmp_path)]) == 2
    assert "live results are pending" in capsys.readouterr().out.lower()


def test_model_strategy_from_a_scripted_file_is_resumable(tmp_path):
    script = ROOT / "examples" / "scripted" / "decider.json"
    args = [
        "loop",
        "--strategy",
        "model",
        "--rounds",
        "2",
        "--approval",
        "always",
        "--client",
        f"scripted:{script}",
        "--campaigns",
        str(tmp_path),
    ]
    assert main(args) == 1
    cdir = only(tmp_path)
    assert str(ROOT) not in (cdir / "campaign.json").read_text()
    main(["approve", str(cdir)])
    main(["resume", str(cdir)])
    main(["approve", str(cdir)])
    assert main(["resume", str(cdir)]) == 0
    decision = json.loads((cdir / "rounds" / "02" / "decision.json").read_text())
    assert decision["model"] == "scripted" and decision["fallback"] is None
