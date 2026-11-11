"""Closed loop edge cases found in review: the envelope's real inputs, bad tool calls, honest
labels, crash safety, the model note, and the benchmark's bookkeeping."""

from __future__ import annotations

import json
import shutil

import pytest
from conftest import ROOT

from labauto import campaign as campaign_mod
from labauto.agents.llm import ScriptedClient
from labauto.campaign import advance_campaign, approve_waiting_round, start_campaign
from labauto.cli import main
from labauto.decide.base import grid_conditions, latin_hypercube
from labauto.decide.model_decider import DeciderTurn, ModelDecider, PickIn, validate_final
from labauto.decide.optimizer import Optimizer
from labauto.models import CampaignConfig, History
from labauto.orchestrator import Clients
from labauto.report import (
    NOTE_EMPTY,
    NOTE_REJECTED,
    build_report,
    check_note_numbers,
    model_note_writer,
)

ASSAY = str(ROOT / "configs" / "assay_reaction_screen.json")
OT2 = str(ROOT / "configs" / "workcell_ot2.json")


def config(**kw):
    base = dict(
        campaign_id="c-h",
        brief="b",
        assay_id="generic_reaction_screen_v1",
        workcell_id="ot2_bench",
        strategy="random",
        rounds=2,
        approval_policy="auto_demo",
        seed=1,
        surface_seed=0,
        assay=ASSAY,
        workcell=OT2,
    )
    base.update(kw)
    return CampaignConfig(**base)


class Crash(BaseException):
    pass


# ---------------------------------------------------------------- envelope


def test_envelope_notices_a_swapped_workcell(tmp_path):
    wc = tmp_path / "wc.json"
    shutil.copy(OT2, wc)
    cdir = start_campaign(config(approval_policy="envelope", workcell=str(wc)), base=tmp_path / "c")
    approve_waiting_round(cdir)
    env = json.loads((cdir / "envelope.json").read_text())
    assert env["workcell_id"] == "ot2_bench" and len(env["workcell_sha256"]) == 64
    shutil.copy(ROOT / "configs" / "workcell_tecan_evo.json", wc)
    st = advance_campaign(cdir)
    assert st.state == "AWAITING_APPROVAL" and st.round == 2
    assert "differs from the envelope" in st.history[-1]["reason"]


def test_envelope_notices_an_edited_workcell_file(tmp_path):
    wc = tmp_path / "wc.json"
    shutil.copy(OT2, wc)
    cdir = start_campaign(config(approval_policy="envelope", workcell=str(wc)), base=tmp_path / "c")
    approve_waiting_round(cdir)
    data = json.loads(wc.read_text())
    data["inventory"]["solvent"]["volume_ul"] = 13000
    wc.write_text(json.dumps(data))
    st = advance_campaign(cdir)
    assert st.state == "AWAITING_APPROVAL" and "workcell file changed" in st.history[-1]["reason"]


# ---------------------------------------------------------------- model decider


def history_with_data(assay):
    from labauto.execute import ledger_from_plan, measure_and_analyze, result_rows
    from labauto.expand import expand
    from labauto.models import Design
    from labauto.virtual.ground_truth import surface_for

    conds = latin_hypercube(assay, 8, 1)
    plan = expand(Design(design_id="d-x", conditions=conds, replicates=3), assay)
    _, results, _ = measure_and_analyze(
        plan, ledger_from_plan(plan), assay, surface_for(0), 1, 1, [], 50
    )
    rows = result_rows(
        results,
        assay,
        campaign_id="x",
        round_number=1,
        attempt=1,
        run_id="r",
        design_hash="",
        protocol_hash="",
        decided_by="lhs",
        timestamp="",
    )
    from labauto.decide.base import build_history

    return build_history(rows, assay, 2)


@pytest.mark.parametrize(
    "bad",
    [
        {"action": "tool", "tool": "suggest", "args": {"n": "eight"}},
        {"action": "tool", "tool": "suggest", "args": {"n": None}},
        {"action": "tool", "tool": "predict", "args": {"conditions": [{"catalyst_mol_pct": 6.5}]}},
        {"action": "tool", "tool": "predict", "args": {"conditions": ["6.5 mol%"]}},
        {"action": "tool", "tool": "results", "args": {"top_k": -1}},
    ],
)
def test_bad_tool_arguments_go_back_to_the_model(assay, bad):
    history = history_with_data(assay)
    client = ScriptedClient([bad, bad, bad])
    d = ModelDecider(assay, client).decide(history, 8, "", 1)
    assert d.fallback and "bad arguments" in d.fallback
    assert "failed" in client.calls[1][1][-1]["content"]
    assert len(d.picks) == 8


def test_a_pick_without_a_reason_is_invalid(assay):
    history = History(next_round=2)
    picks = [PickIn(conditions=c, source="own") for c in grid_conditions(assay)[:8]]
    errors = validate_final(DeciderTurn(action="final", picks=picks), 8, history, assay)
    assert any("has no reason" in e for e in errors)


def test_duplicate_picks_within_a_round_are_invalid(assay):
    c = grid_conditions(assay)[0]
    picks = [PickIn(conditions=c, source="own", reason="r")] * 8
    errors = validate_final(
        DeciderTurn(action="final", picks=picks), 8, History(next_round=2), assay
    )
    assert any("repeats another pick" in e for e in errors)


def test_exactly_two_retries_before_the_fallback(assay):
    history = history_with_data(assay)
    bad = {"action": "final", "picks": [], "round_note": ""}
    client = ScriptedClient([bad, bad, bad, bad])
    d = ModelDecider(assay, client).decide(history, 8, "", 1)
    assert d.fallback and len(client.queues["*"]) == 1  # three answers used, the fourth never asked


# ---------------------------------------------------------------- optimizer and Latin hypercube


def test_no_model_means_no_prediction(assay):
    opt = Optimizer(assay, History(next_round=2), 1)
    s = opt.suggest(4)
    assert len(s) == 4 and all(
        x["predicted_mean"] is None and "no model yet" in x["note"] for x in s
    )
    assert opt.predict([grid_conditions(assay)[0]])[0]["predicted_mean"] is None


def test_suggest_with_nothing_left_returns_nothing(assay):
    from labauto.models import ConditionStat

    stats = [
        ConditionStat(conditions=c, mean=50.0, n=1, rounds=[1]) for c in grid_conditions(assay)
    ]
    assert Optimizer(assay, History(next_round=2, stats=stats[:1980]), 1).candidates == []
    opt = Optimizer.__new__(Optimizer)
    opt.candidates, opt.gp = [], None
    assert Optimizer.suggest(opt, 8) == []


def test_latin_hypercube_limits(assay):
    assert latin_hypercube(assay, 0, 1) == []
    assert len(latin_hypercube(assay, 5000, 1)) == 1980


def test_round_one_is_labelled_as_code(tmp_path):
    cdir = start_campaign(config(rounds=1), base=tmp_path)
    decision = json.loads((cdir / "rounds" / "01" / "decision.json").read_text())
    design = json.loads((cdir / "rounds" / "01" / "design.json").read_text())
    assert decision["strategy"] == "lhs" and design["created_by"]["role"] == "code"
    summary = (cdir / "rounds" / "01" / "summary.md").read_text()
    assert "shared Latin hypercube (code)" in summary and "model written" not in summary
    assert "synthetic" in summary


# ---------------------------------------------------------------- crash safety and records


def test_crash_right_after_the_append_does_not_duplicate_rows(tmp_path, monkeypatch):
    real = campaign_mod.LocalStore.append_results
    hit = {"n": 0}

    def crashing(self, campaign_id, rows):
        real(self, campaign_id, rows)
        if rows and rows[0]["round"] == 2 and not hit["n"]:
            hit["n"] = 1
            raise Crash()

    monkeypatch.setattr(campaign_mod.LocalStore, "append_results", crashing)
    with pytest.raises(Crash):
        start_campaign(config(), base=tmp_path)
    cdir = tmp_path / "c-h"
    assert advance_campaign(cdir).state == "DONE"
    lines = (cdir / "results_all.csv").read_text().splitlines()
    assert len(lines) == 1 + 2 * 28


def test_needs_human_is_cleared_once_the_campaign_moves_on(tmp_path):
    cdir = start_campaign(config(approval_policy="always", rounds=1), base=tmp_path)
    approve_waiting_round(cdir)
    protocol = cdir / "rounds" / "01" / "protocol.py"
    text = protocol.read_text()
    protocol.write_text(text + "# edited\n")
    st = advance_campaign(cdir)
    assert st.needs_human
    protocol.write_text(text)
    approve_waiting_round(cdir)
    st = advance_campaign(cdir)
    assert st.state == "DONE" and st.needs_human is None


def test_campaign_records_hold_no_absolute_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    cdir = start_campaign(config(approval_policy="always", rounds=1), base=tmp_path)
    for name in ("rounds/01/check.json", "rounds/01/summary.md"):
        assert str(tmp_path) not in (cdir / name).read_text(), name


def test_unknown_fault_is_refused(tmp_path, capsys):
    code = main(
        ["loop", "--strategy", "random", "--fault", "plate-drift@2", "--campaigns", str(tmp_path)]
    )
    assert code == 2
    assert "unknown fault 'plate-drift@2'" in capsys.readouterr().out


# ---------------------------------------------------------------- report and model note


def test_note_numbers_cannot_hide_in_their_spelling():
    tables = "| 1 | 73.3 |\n| 2 | 87.4 |"
    assert check_note_numbers("about .9 points", tables) == [".9"]
    assert check_note_numbers("best a99", tables) == ["99"]
    assert check_note_numbers("1e2 runs", tables) == ["1e2"]
    assert check_note_numbers("87.4% by round 2", tables) == []


def test_scripted_note_is_labelled_scripted(tmp_path):
    cdir = start_campaign(config(rounds=1), base=tmp_path)
    clients = Clients(local=ScriptedClient(["One round ran."]))
    text = build_report(cdir, note_writer=model_note_writer(clients, cdir)).read_text()
    assert "Written by a scripted client (hand-written text, not a model)" in text
    assert "One round ran." in text
    record = json.loads((cdir / "transcript.jsonl").read_text().splitlines()[0])
    assert record["role"] == "report_note"


def test_empty_and_invented_notes_are_rejected_with_the_right_reason(tmp_path):
    cdir = start_campaign(config(rounds=1), base=tmp_path)
    assert NOTE_EMPTY in build_report(cdir, note_writer=lambda t: ("  ", "a model")).read_text()
    assert (
        NOTE_REJECTED
        in build_report(cdir, note_writer=lambda t: ("It reached 99.9%.", "a model")).read_text()
    )


# ---------------------------------------------------------------- benchmark


def test_benchmark_counts_model_fallbacks_and_handles_more_model_seeds(tmp_path, assay):
    from labauto.benchmark import run_benchmark, write_outputs

    def factory():
        return ModelDecider(assay, ScriptedClient(["not json"] * 200))

    result = run_benchmark(
        assay, ["random", "model"], seeds=2, model_seeds=3, rounds=2, model_factory=factory
    )
    assert set(result["scores"]["model"]) == {1, 2, 3}
    md, png = tmp_path / "out" / "b.md", tmp_path / "img" / "b.png"
    write_outputs(result, 2, "test", md, png, "scripted", "abc")
    text = md.read_text()
    assert "model: 3 of 3 decided rounds fell back" in text and "not the model" in text
    assert "](../img/b.png)" in text


def test_benchmark_command_names_a_custom_plot(tmp_path, monkeypatch):
    import labauto.benchmark as bench

    seen = {}
    monkeypatch.setattr(
        bench,
        "run_benchmark",
        lambda *a, **k: {
            "scores": {},
            "optimum": {},
            "seconds": {},
            "skipped": {},
            "fallbacks": {},
        },
    )
    monkeypatch.setattr(
        bench, "write_outputs", lambda result, rounds, command, *a: seen.update(command=command)
    )
    main(["benchmark", "--strategies", "random", "--png", str(tmp_path / "p.png")])
    assert f"--png {tmp_path / 'p.png'}" in seen["command"] and "--out" not in seen["command"]
