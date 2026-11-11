"""Planner agent edge cases found in review: resume on the hosted rung, replay of hosted
sessions, damaged files, parsing, honest labels, and the evaluation page."""

from __future__ import annotations

import json
import sys
import types

import httpx
import pytest
from conftest import ROOT

from labauto import orchestrator
from labauto.agents.llm import (
    EndpointUnavailable,
    LiveClient,
    ScriptedClient,
    Transcript,
    chat,
    parse_reply,
)
from labauto.agents.planner import workcell_summary
from labauto.approval import ApprovalError, approve, load_approval, reject, verify
from labauto.cli_plan import SCRIPTED_LABEL, make_clients, recording_source, settings_for
from labauto.config import Endpoint, Settings
from labauto.models import AttemptRecord, PlanError, PlannerOutput
from labauto.orchestrator import Clients, load_attempts, load_state, next_step, resume, start_run

ASSAY = str(ROOT / "configs" / "assay_reaction_screen.json")
OT2 = str(ROOT / "configs" / "workcell_ot2.json")
CONFLICT = str(ROOT / "configs" / "workcell_ot2_conflict.json")


def reply(grid, reps=2):
    return {
        "action": "submit_design",
        "design": {
            "title": "t",
            "grid": grid,
            "conditions": None,
            "replicates": reps,
            "include_controls": True,
            "deviations": [],
            "assumptions": [],
            "notes": "",
        },
    }


OK = reply({"catalyst_mol_pct": [1, 2], "base_equiv": [1.0], "cosolvent_pct": [20]})
BIG = reply(
    {
        "catalyst_mol_pct": list(range(1, 11)),
        "base_equiv": [1.0, 1.5, 2.0, 2.5],
        "cosolvent_pct": [20],
    },
    3,
)
OFF = reply({"catalyst_mol_pct": [2.2], "base_equiv": [1.0], "cosolvent_pct": [20]})
BIG4 = reply(BIG["design"]["grid"], 4)


class Crash(BaseException):
    pass


# ---------------------------------------------------------------- hosted rung and resume


def test_resume_during_the_hosted_attempt_does_not_count_its_call_twice(tmp_path, monkeypatch):
    settings = Settings(
        hosted=Endpoint(label="hosted", provider="anthropic", model="m"), hosted_max_calls=1
    )
    real = orchestrator.transition
    hit = {"done": False}

    def crash_in_hosted_check(run_dir, st, to, reason):
        real(run_dir, st, to, reason)
        if to == "CHECKING" and st.rung == "hosted" and not hit["done"]:
            hit["done"] = True
            raise Crash()

    monkeypatch.setattr(orchestrator, "transition", crash_in_hosted_check)
    hosted = ScriptedClient([OK])
    with pytest.raises(Crash):
        start_run(
            "r",
            ASSAY,
            OT2,
            Clients(local=ScriptedClient([BIG, OFF, BIG4]), hosted=hosted),
            settings,
            tmp_path,
        )
    run_dir = next(tmp_path.iterdir())
    monkeypatch.setattr(orchestrator, "transition", real)
    idle = ScriptedClient([])
    st = resume(run_dir, Clients(local=idle, hosted=idle), settings)
    assert st.state == "AWAITING_APPROVAL"
    assert st.hosted_calls == 1
    assert [a.rung for a in load_attempts(run_dir)][-1] == "hosted"
    assert idle.calls == []


def test_replay_of_a_hosted_session_walks_the_same_ladder(tmp_path):
    settings = Settings(hosted=Endpoint(label="hosted", provider="anthropic", model="m"))
    first = start_run(
        "r",
        ASSAY,
        OT2,
        Clients(local=ScriptedClient([BIG, OFF, BIG4]), hosted=ScriptedClient([OK])),
        settings,
        tmp_path / "a",
    )
    assert load_state(first).state == "AWAITING_APPROVAL"
    spec = f"replay:{first / 'transcript.jsonl'}"
    replay_settings = settings_for(spec)
    assert replay_settings.hosted is not None
    second = start_run(
        "r", ASSAY, OT2, make_clients(spec, replay_settings), replay_settings, tmp_path / "b"
    )
    assert load_state(second).state == "AWAITING_APPROVAL"
    assert [a.rung for a in load_attempts(second)] == ["local", "local", "local", "hosted"]


def test_hosted_cap_of_zero_says_it_was_skipped():
    local = [
        AttemptRecord(
            number=i,
            rung="local",
            endpoint_label="local-main",
            errors=[PlanError(code=f"C{i}", layer="design", where="w", message="m")],
        )
        for i in (1, 2, 3)
    ]
    step = next_step(local, Settings(hosted=Endpoint(label="hosted"), hosted_max_calls=0), 0)
    assert step.kind == "handoff" and "skipped: its call cap is 0" in step.reason
    assert "hosted rung used" not in step.reason


def test_same_codes_in_new_places_is_progress():
    def attempt(n, where):
        return AttemptRecord(
            number=n,
            rung="local",
            endpoint_label="l",
            errors=[PlanError(code="FACTOR_OFF_GRID", layer="design", where=where, message="m")],
        )

    step = next_step([attempt(1, "grid.a[0]"), attempt(2, "grid.a[1]")], Settings(), 0)
    assert step.kind == "attempt" and step.rung == "local"


# ---------------------------------------------------------------- approval and damaged files


def test_unreadable_design_voids_the_approval(tmp_path):
    run_dir = start_run("r", ASSAY, OT2, Clients(local=ScriptedClient([OK])), Settings(), tmp_path)
    approve(run_dir, by="tester")
    (run_dir / "final" / "design.json").write_text('{"title": "edited", "bogus": 1}')
    assert not verify(run_dir)
    assert load_state(run_dir).state == "AWAITING_APPROVAL"
    assert load_approval(run_dir).decision == "voided"
    with pytest.raises(ApprovalError):
        approve(run_dir)


def test_valid_but_changed_design_voids_the_approval(tmp_path):
    run_dir = start_run("r", ASSAY, OT2, Clients(local=ScriptedClient([OK])), Settings(), tmp_path)
    approve(run_dir)
    path = run_dir / "final" / "design.json"
    data = json.loads(path.read_text())
    data["title"] = "changed after approval"
    path.write_text(json.dumps(data))
    assert not verify(run_dir)
    assert "design" in load_approval(run_dir).void_reason


def test_edited_protocol_blocks_approval(tmp_path):
    run_dir = start_run("r", ASSAY, OT2, Clients(local=ScriptedClient([OK])), Settings(), tmp_path)
    protocol = run_dir / "final" / "protocol.py"
    protocol.write_text(protocol.read_text() + "# edited\n")
    with pytest.raises(ApprovalError):
        approve(run_dir)


def test_reject_stores_the_reason_and_needs_a_waiting_run(tmp_path):
    run_dir = start_run("r", ASSAY, OT2, Clients(local=ScriptedClient([OK])), Settings(), tmp_path)
    reject(run_dir, "wrong co-solvent", by="tester")
    a = load_approval(run_dir)
    assert a.comment == "wrong co-solvent" and a.by == "tester"
    with pytest.raises(ApprovalError):
        reject(run_dir, "again")


def test_needs_human_summary_names_the_files_to_open(tmp_path):
    run_dir = start_run(
        "r", ASSAY, CONFLICT, Clients(local=ScriptedClient([OK])), Settings(), tmp_path
    )
    text = (run_dir / "summary.md").read_text()
    assert "attempts/01_local/errors.json" in text and "attempts/01_local/design.json" in text


def test_resting_run_gets_its_summary_back_on_resume(tmp_path):
    run_dir = start_run("r", ASSAY, OT2, Clients(local=ScriptedClient([OK])), Settings(), tmp_path)
    (run_dir / "summary.md").unlink()
    resume(run_dir, Clients(local=ScriptedClient([])), Settings())
    assert (run_dir / "summary.md").exists()


def test_records_hold_no_absolute_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    run_dir = start_run("r", ASSAY, OT2, Clients(local=ScriptedClient([OK])), Settings(), tmp_path)
    for name in ("state.json", "summary.md", "final/check.json"):
        assert str(ROOT) not in (run_dir / name).read_text(), name


def test_torn_transcript_line_does_not_block_resume(tmp_path):
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        json.dumps({"scope": "planner", "attempt": 1, "call": 1, "raw": "{}"})
        + "\n"
        + '{"scope": "plan'
    )
    t = Transcript(path)
    assert len(t.records) == 1


# ---------------------------------------------------------------- parsing and clients


def test_unclosed_think_block_is_not_an_answer():
    draft = json.dumps(OK)
    parsed, error = parse_reply(f"<think>Draft: {draft} hmm, maybe fewer", PlannerOutput)
    assert parsed is None and error


def test_valid_object_is_found_past_a_broken_one():
    text = '{"action": "submit_design", "design": {"grid": {"a": [1]} ' + json.dumps(OK)
    assert parse_reply(text, PlannerOutput)[0] is not None


def test_backticks_inside_a_fenced_reply():
    data = json.loads(json.dumps(OK))
    data["design"]["notes"] = "see ``` above"
    assert parse_reply("```json\n" + json.dumps(data) + "\n```", PlannerOutput)[0] is not None


def test_retry_message_carries_the_parse_error():
    client = ScriptedClient(['{"action": "submit_design"}', json.dumps(OK)])
    chat(client, "local-main", [], PlannerOutput)
    retry = client.calls[1][1][-1]["content"]
    assert "design" in retry and "Field required" in retry


def test_probe_outage_is_not_cached_as_no():
    LiveClient._structured.clear()
    state = {"up": False}

    def handler(request):
        if not state["up"]:
            raise httpx.ConnectError("down")
        body = json.loads(request.content)
        content = '{"ok": true}' if body.get("max_tokens") == 32 else json.dumps(OK)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    ep = Endpoint(label="local-main", base_url="http://mock.invalid/v1", model="m")
    client = LiveClient({"local-main": ep}, transport=httpx.MockTransport(handler))
    with pytest.raises(EndpointUnavailable):
        client.supports_structured("local-main")
    state["up"] = True
    assert client.supports_structured("local-main") is True


def test_transcript_records_the_schema_text_that_was_sent(tmp_path):
    LiveClient._structured.clear()

    def handler(request):
        body = json.loads(request.content)
        if "response_format" in body:
            return httpx.Response(400, json={"error": "no"})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(OK)}}]})

    ep = Endpoint(label="local-main", base_url="http://mock.invalid/v1", model="m")
    client = LiveClient({"local-main": ep}, transport=httpx.MockTransport(handler))
    t = Transcript(tmp_path / "t.jsonl")
    chat(
        client,
        "local-main",
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        PlannerOutput,
        transcript=t,
    )
    rec = t.records[0]
    assert "JSON schema" in rec["messages"][0]["content"]


def test_hosted_refusal_keeps_the_real_text(monkeypatch, tmp_path):
    fake = types.ModuleType("anthropic")

    class Resp:
        stop_reason = "refusal"
        content = []
        model = "hosted-model"
        usage = types.SimpleNamespace(input_tokens=5, output_tokens=0)

    class Client:
        def __init__(self, **kw):
            self.messages = types.SimpleNamespace(create=lambda **kw: Resp())

    fake.Anthropic = Client
    fake.APIStatusError = type("APIStatusError", (Exception,), {})
    fake.APIConnectionError = type("APIConnectionError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    from labauto.agents.llm import HostedClient

    t = Transcript(tmp_path / "t.jsonl")
    result = chat(
        HostedClient(Endpoint(label="hosted", provider="anthropic", model="m")),
        "hosted",
        [{"role": "user", "content": "u"}],
        PlannerOutput,
        transcript=t,
        max_calls=1,
    )
    assert result.parsed is None
    rec = t.records[0]
    assert rec["raw"] == "" and rec["stop_reason"] == "refusal" and rec["temperature"] is None


# ---------------------------------------------------------------- labels and the planner context


def test_a_renamed_scripted_recording_is_still_labelled_scripted(tmp_path):
    src = ROOT / "examples" / "recordings" / "01_simple_factorial.scripted.jsonl"
    renamed = tmp_path / "01_simple_factorial.jsonl"
    renamed.write_text(src.read_text())
    assert recording_source(renamed) == SCRIPTED_LABEL


def test_shared_tip_rack_is_counted_once_for_the_planner(assay, workcell):
    data = workcell.model_dump(mode="json")
    data["pipettes"][1]["tips"] = "tips_20"
    shared = type(workcell).model_validate(data)
    text = workcell_summary(shared, assay)
    assert (
        text.count("96 tips") == 1 and "the small pipette" in text and "the large pipette" in text
    )


def test_eval_refuses_to_publish_when_no_model_answered(tmp_path):
    from labauto.planner_eval import NoModelAnswered, evaluate

    class Down:
        def complete(self, *a, **k):
            raise EndpointUnavailable("down")

    with pytest.raises(NoModelAnswered):
        evaluate(
            lambda: Clients(local=Down()),
            Settings(),
            trials=1,
            runs_dir=tmp_path / "r",
            out_path=tmp_path / "e.md",
            ids=["01_simple_factorial"],
        )
    assert not (tmp_path / "e.md").exists()


def test_eval_page_marks_scripted_numbers_and_counts_failures(tmp_path):
    from labauto.planner_eval import evaluate

    replies = json.loads((ROOT / "examples" / "scripted" / "01_simple_factorial.json").read_text())
    clients = iter([Clients(local=ScriptedClient(replies)), Clients(local=ScriptedClient([]))])
    out = evaluate(
        lambda: next(clients),
        Settings(),
        trials=2,
        runs_dir=tmp_path / "r",
        out_path=tmp_path / "e.md",
        ids=["01_simple_factorial"],
    )
    text = out.read_text()
    assert "not from a model" in text
    assert (
        "| 01_simple_factorial | 2 | 1 | 1 | 0 | 0 | n/a |" in text
        and text.rstrip().count("FAILED (internal error)") == 1
    )
