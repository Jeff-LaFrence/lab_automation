"""The repair loop, the escalation ladder, approval, resume, and replay. All with scripted clients."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import ROOT

from labauto import orchestrator
from labauto.agents.llm import ReplayClient, ScriptedClient, ScriptExhausted
from labauto.approval import ApprovalError, approve, load_approval, reject, verify
from labauto.config import Endpoint, Settings
from labauto.orchestrator import Clients, load_attempts, load_state, resume, start_run

ASSAY = str(ROOT / "configs" / "assay_reaction_screen.json")
OT2 = str(ROOT / "configs" / "workcell_ot2.json")
CONFLICT = str(ROOT / "configs" / "workcell_ot2_conflict.json")
REQUEST = "Screen catalyst at 1 and 2 mol % with base at 1.0 equivalents and 20% co-solvent."


def reply(grid=None, reps=2, deviations=(), conditions=None):
    grid = (
        grid
        if grid is not None or conditions is not None
        else {
            "catalyst_mol_pct": [1, 2],
            "base_equiv": [1.0],
            "cosolvent_pct": [20],
        }
    )
    return {
        "action": "submit_design",
        "design": {
            "title": "t",
            "grid": grid,
            "conditions": conditions,
            "replicates": reps,
            "include_controls": True,
            "deviations": list(deviations),
            "assumptions": [],
            "notes": "",
        },
    }


BIG = {
    "catalyst_mol_pct": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
    "base_equiv": [1.0, 1.5, 2.0, 2.5],
    "cosolvent_pct": [20],
}
OFF = {"catalyst_mol_pct": [2.2], "base_equiv": [1.0], "cosolvent_pct": [20]}
HOSTED = Settings(hosted=Endpoint(label="hosted", provider="anthropic", model="m"))


def run(replies, tmp_path, settings=None, workcell=OT2, hosted=None):
    local = ScriptedClient(replies)
    clients = Clients(local=local, hosted=hosted or local, spec="scripted:test")
    run_dir = start_run(
        REQUEST, ASSAY, workcell, clients, settings or Settings(), tmp_path / "runs"
    )
    return run_dir, local


def test_clean_first_try_reaches_awaiting_approval(tmp_path):
    run_dir, _ = run([reply()], tmp_path)
    st = load_state(run_dir)
    assert st.state == "AWAITING_APPROVAL"
    assert [h.to for h in st.history] == ["RECEIVED", "PLANNING", "CHECKING", "AWAITING_APPROVAL"]
    for name in (
        "request.txt",
        "state.json",
        "transcript.jsonl",
        "model_calls.jsonl",
        "summary.md",
    ):
        assert (run_dir / name).exists()
    for name in (
        "design.json",
        "errors.json",
        "plan.json",
        "worklist.csv",
        "protocol.py",
        "sim_log.txt",
    ):
        assert (run_dir / "attempts" / "01_local" / name).exists()
    assert (run_dir / "final" / "ledger.csv").exists() and (
        run_dir / "final" / "well_map.csv"
    ).exists()
    state_json = json.loads((run_dir / "state.json").read_text())
    assert state_json["history"][1]["from"] == "RECEIVED"


def test_bad_then_good_sends_the_errors_back(tmp_path):
    run_dir, client = run(
        [
            reply(BIG, reps=3),
            reply(BIG, reps=2, deviations=[{"requested": "3", "planned": "2", "reason": "fit"}]),
        ],
        tmp_path,
    )
    attempts = load_attempts(run_dir)
    assert [a.ok for a in attempts] == [False, True]
    assert attempts[0].errors[0].code == "TOO_MANY_WELLS"
    repair_prompt = client.calls[1][1][-1]["content"]
    assert "TOO_MANY_WELLS" in repair_prompt and "fix_hint" in repair_prompt
    assert load_state(run_dir).state == "AWAITING_APPROVAL"


def test_malformed_reply_is_retried_inside_one_attempt(tmp_path):
    run_dir, client = run(["this is not json", json.dumps(reply())], tmp_path)
    attempts = load_attempts(run_dir)
    assert len(attempts) == 1 and attempts[0].ok and attempts[0].calls == 2


def test_two_unreadable_replies_make_a_schema_invalid_attempt(tmp_path):
    run_dir, _ = run(["nope", "still nope", json.dumps(reply())], tmp_path)
    attempts = load_attempts(run_dir)
    assert attempts[0].errors[0].code == "SCHEMA_INVALID"
    assert attempts[1].ok


def test_no_progress_leaves_the_rung_early(tmp_path):
    run_dir, client = run([reply(OFF), reply(OFF), reply()], tmp_path)
    st = load_state(run_dir)
    assert len(load_attempts(run_dir)) == 2
    assert st.state == "NEEDS_HUMAN"
    assert "no progress" in st.needs_human.text
    assert len(client.queues["*"]) == 1  # the third reply was never asked for


def test_hosted_rung_succeeds_after_local_is_exhausted(tmp_path):
    local = [reply(BIG, reps=3), reply(OFF), reply(BIG, reps=4)]
    hosted = ScriptedClient([reply()])
    run_dir, _ = run(local, tmp_path, settings=HOSTED, hosted=hosted)
    attempts = load_attempts(run_dir)
    assert [a.rung for a in attempts] == ["local", "local", "local", "hosted"]
    assert attempts[-1].endpoint_label == "hosted" and attempts[-1].ok
    assert (run_dir / "attempts" / "04_hosted").is_dir()
    assert load_state(run_dir).hosted_calls == 1
    hosted_prompt = hosted.calls[0][1][-1]["content"]
    assert "Attempt 1" in hosted_prompt and "Attempt 3" in hosted_prompt  # sees the full history


def test_local_and_hosted_exhausted_ends_with_a_person(tmp_path):
    run_dir, _ = run(
        [reply(BIG, reps=3), reply(OFF), reply(BIG, reps=4)],
        tmp_path,
        settings=HOSTED,
        hosted=ScriptedClient([reply(OFF)]),
    )
    st = load_state(run_dir)
    assert st.state == "NEEDS_HUMAN" and st.needs_human.kind == "handoff"
    assert len(load_attempts(run_dir)) == 4


def test_hosted_not_configured_is_skipped_and_recorded(tmp_path):
    run_dir, _ = run([reply(BIG, reps=3), reply(OFF), reply(BIG, reps=4)], tmp_path)
    st = load_state(run_dir)
    assert st.state == "NEEDS_HUMAN"
    assert "hosted rung not configured" in st.needs_human.text
    assert "hosted rung not configured" in st.notes
    assert "hosted rung not configured" in (run_dir / "summary.md").read_text()


def test_hosted_call_cap_is_respected(tmp_path):
    settings = Settings(
        hosted=Endpoint(label="hosted", provider="anthropic", model="m"), hosted_max_calls=1
    )
    hosted = ScriptedClient(["not json", json.dumps(reply())])
    run_dir, _ = run(
        [reply(BIG, reps=3), reply(OFF), reply(BIG, reps=4)],
        tmp_path,
        settings=settings,
        hosted=hosted,
    )
    assert load_state(run_dir).hosted_calls == 1
    assert load_state(run_dir).state == "NEEDS_HUMAN"
    assert len(hosted.queues["*"]) == 1


def test_needs_human_error_skips_to_the_person(tmp_path):
    run_dir, client = run([reply(), reply()], tmp_path, workcell=CONFLICT)
    st = load_state(run_dir)
    assert st.state == "NEEDS_HUMAN"
    assert len(load_attempts(run_dir)) == 1
    assert "DECK_CONFLICT" in st.needs_human.text
    assert len(client.queues["*"]) == 1


def test_routing_decisions_are_logged(tmp_path):
    run_dir, _ = run([reply(BIG, reps=3), reply()], tmp_path)
    lines = [json.loads(x) for x in (run_dir / "model_calls.jsonl").read_text().splitlines()]
    assert [x["role"] for x in lines] == ["planner", "repair"]
    assert all(x["reason"] and x["label"] == "local-main" for x in lines)


def test_tiny_context_trims_the_history(tmp_path):
    settings = Settings(
        main=Endpoint(label="local-main", base_url="u", model="m", context_tokens=1000)
    )
    run_dir, client = run([reply(BIG, reps=3), reply(OFF), reply()], tmp_path, settings=settings)
    lines = [json.loads(x) for x in (run_dir / "model_calls.jsonl").read_text().splitlines()]
    assert lines[-1]["trim"] is True
    last_prompt = client.calls[-1][1][-1]["content"]
    assert "trimmed to the latest of 2" in last_prompt
    assert "Attempt 1 " not in last_prompt


# ---------------------------------------------------------------- approval


def test_approve_ties_the_hashes(tmp_path):
    run_dir, _ = run([reply()], tmp_path)
    approval = approve(run_dir, by="tester")
    st = load_state(run_dir)
    assert st.state == "APPROVED"
    assert approval.design_hash == st.design_hash and approval.protocol_hash == st.protocol_hash
    assert approval.mode == "human"
    assert verify(run_dir)


def test_approve_needs_a_run_awaiting_approval(tmp_path):
    run_dir, _ = run([reply(OFF), reply(OFF)], tmp_path)
    with pytest.raises(ApprovalError):
        approve(run_dir)


def test_reject(tmp_path):
    run_dir, _ = run([reply()], tmp_path)
    reject(run_dir, "wrong co-solvent")
    assert load_state(run_dir).state == "REJECTED"
    assert load_approval(run_dir).decision == "rejected"


def test_changed_protocol_voids_the_approval(tmp_path):
    run_dir, _ = run([reply()], tmp_path)
    approve(run_dir)
    protocol = run_dir / "final" / "protocol.py"
    protocol.write_text(protocol.read_text() + "\n# edited\n")
    assert not verify(run_dir)
    assert load_state(run_dir).state == "AWAITING_APPROVAL"
    approval = load_approval(run_dir)
    assert approval.decision == "voided" and "protocol" in approval.void_reason


def test_changed_design_blocks_approval(tmp_path):
    run_dir, _ = run([reply()], tmp_path)
    design = run_dir / "final" / "design.json"
    data = json.loads(design.read_text())
    data["replicates"] = 9
    design.write_text(json.dumps(data))
    with pytest.raises(ApprovalError):
        approve(run_dir)


def test_demo_auto_is_labelled(tmp_path):
    run_dir, _ = run([reply()], tmp_path)
    approve(run_dir, mode="demo_auto")
    assert load_approval(run_dir).mode == "demo_auto"
    summary = (run_dir / "summary.md").read_text()
    assert "demo_auto" in summary.splitlines()[0]
    assert "no person approved this" in summary


def test_summary_carries_the_approval_header(tmp_path):
    run_dir, _ = run(
        [
            reply(BIG, reps=3),
            reply(
                BIG,
                reps=2,
                deviations=[
                    {"requested": "3 replicates", "planned": "2 replicates", "reason": "fit"}
                ],
            ),
        ],
        tmp_path,
    )
    text = (run_dir / "summary.md").read_text()
    for part in (
        "## Request",
        REQUEST,
        "## Changes from what was asked",
        "asked: 3 replicates",
        "## Assumptions",
        "## How we got here",
        "failed  TOO_MANY_WELLS",
        "To approve:",
        "## Checks",
    ):
        assert part in text


# ---------------------------------------------------------------- resume


class Crash(BaseException):
    """Stands in for the process dying: not caught by the orchestrator."""


def crash_after(monkeypatch, state):
    real = orchestrator.transition
    hit = {"done": False}

    def wrapped(run_dir, st, to, reason):
        real(run_dir, st, to, reason)
        if to == state and not hit["done"]:
            hit["done"] = True
            raise Crash(state)

    monkeypatch.setattr(orchestrator, "transition", wrapped)


SCRIPT = [reply(BIG, reps=3), reply(BIG, reps=2)]


@pytest.mark.parametrize("state", ["PLANNING", "CHECKING", "REPAIRING"])
def test_resume_from_each_working_state_without_repeating_calls(tmp_path, monkeypatch, state):
    crash_after(monkeypatch, state)
    first = ScriptedClient(SCRIPT)
    with pytest.raises(Crash):
        start_run(REQUEST, ASSAY, OT2, Clients(local=first), Settings(), tmp_path / "runs")
    run_dir = next((tmp_path / "runs").iterdir())
    assert load_state(run_dir).state == state
    second = ScriptedClient(SCRIPT)
    st = resume(run_dir, Clients(local=second), Settings())
    assert st.state == "AWAITING_APPROVAL"
    total_calls = len(first.calls) + len(second.calls)
    lines = (run_dir / "transcript.jsonl").read_text().splitlines()
    assert total_calls == len(lines) == 2  # every model call was made exactly once


def test_resume_from_received(tmp_path, monkeypatch):
    real = orchestrator.advance
    monkeypatch.setattr(orchestrator, "advance", lambda *a, **k: None)
    run_dir = start_run(
        REQUEST, ASSAY, OT2, Clients(local=ScriptedClient([])), Settings(), tmp_path / "runs"
    )
    monkeypatch.setattr(orchestrator, "advance", real)
    assert load_state(run_dir).state == "RECEIVED"
    assert (
        resume(run_dir, Clients(local=ScriptedClient([reply()])), Settings()).state
        == "AWAITING_APPROVAL"
    )


def test_resume_after_failure(tmp_path):
    run_dir = start_run(
        REQUEST, ASSAY, OT2, Clients(local=ScriptedClient([])), Settings(), tmp_path / "runs"
    )
    st = load_state(run_dir)
    assert st.state == "FAILED" and "ScriptExhausted" in st.history[-1].reason
    assert (run_dir / "error.txt").exists()
    assert (
        resume(run_dir, Clients(local=ScriptedClient([reply()])), Settings()).state
        == "AWAITING_APPROVAL"
    )


@pytest.mark.parametrize("final", ["AWAITING_APPROVAL", "NEEDS_HUMAN", "APPROVED", "REJECTED"])
def test_resume_from_resting_states_makes_no_calls(tmp_path, final):
    script = [reply()] if final != "NEEDS_HUMAN" else [reply(OFF), reply(OFF)]
    run_dir, _ = run(script, tmp_path)
    if final == "APPROVED":
        approve(run_dir)
    if final == "REJECTED":
        reject(run_dir, "no")
    idle = ScriptedClient([])
    assert resume(run_dir, Clients(local=idle), Settings()).state == final
    assert idle.calls == []


def test_scripted_client_runs_out_cleanly():
    with pytest.raises(ScriptExhausted):
        ScriptedClient([]).complete("local-main", [], None, 0.3, 10)


# ---------------------------------------------------------------- replay


def design_fields(run_dir: Path) -> dict:
    data = json.loads((run_dir / "final" / "design.json").read_text())
    return {k: v for k, v in data.items() if k not in ("design_id", "workcell_id")}


@pytest.mark.parametrize("scenario", ["01_simple_factorial", "02_too_big", "03_out_of_range"])
def test_replay_of_a_committed_recording_reproduces_the_final_design(tmp_path, scenario):
    path = ROOT / "examples" / "recordings" / f"{scenario}.scripted.jsonl"
    records = [json.loads(x) for x in path.read_text().splitlines()]
    request = (ROOT / "examples" / "requests" / f"{scenario}.txt").read_text()
    runs = []
    for i in range(2):
        clients = Clients(local=ReplayClient(path))
        runs.append(start_run(request, ASSAY, OT2, clients, Settings(), tmp_path / f"r{i}"))
    assert all(load_state(r).state == "AWAITING_APPROVAL" for r in runs)
    assert design_fields(runs[0]) == design_fields(runs[1])
    recorded = records[-1]["parsed"]["design"]
    final = design_fields(runs[0])
    for key in (
        "grid",
        "conditions",
        "replicates",
        "include_controls",
        "deviations",
        "assumptions",
        "title",
    ):
        assert final[key] == recorded[key], key
