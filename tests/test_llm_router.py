"""Chat client parsing, transcripts, the live client against a mock server, and the router.

No network: the live client talks to httpx.MockTransport, which runs in process.
"""

from __future__ import annotations

import json

import httpx
import pytest

from labauto.agents.llm import (
    EndpointUnavailable,
    LiveClient,
    ReplayClient,
    ScriptedClient,
    Transcript,
    chat,
    extract_json,
)
from labauto.agents.router import estimate_tokens, route
from labauto.config import Endpoint, Settings, load_settings
from labauto.models import PlannerOutput

GOOD = {
    "action": "submit_design",
    "design": {
        "title": "t",
        "grid": {"catalyst_mol_pct": [1], "base_equiv": [1.0], "cosolvent_pct": [20]},
        "conditions": None,
        "replicates": 2,
        "include_controls": True,
        "deviations": [],
        "assumptions": [],
        "notes": "",
    },
}

# ---------------------------------------------------------------- parsing


def test_extract_plain_json():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_from_think_tags():
    assert extract_json('<think>maybe {"a": 0}</think>\n{"a": 1}') == {"a": 1}


def test_extract_after_a_dangling_think_close():
    assert extract_json('reasoning here</think>{"a": 2}') == {"a": 2}


def test_extract_from_a_code_fence():
    assert extract_json('Here it is:\n```json\n{"a": {"b": [1, 2]}}\n```\nDone.') == {
        "a": {"b": [1, 2]}
    }


def test_extract_first_complete_object_after_text():
    assert extract_json('Sure. {"a": 1} and also {"b": 2}') == {"a": 1}


def test_extract_without_json_fails():
    with pytest.raises(ValueError):
        extract_json("no braces here")


# ---------------------------------------------------------------- chat()


def test_malformed_json_is_retried_with_the_error(tmp_path):
    client = ScriptedClient(["{not json", json.dumps(GOOD)])
    t = Transcript(tmp_path / "transcript.jsonl")
    result = chat(
        client, "local-main", [{"role": "user", "content": "x"}], PlannerOutput, transcript=t
    )
    assert result.parsed is not None and result.calls == 2
    retry_messages = client.calls[1][1]
    assert retry_messages[-1]["role"] == "user"
    assert "could not be used" in retry_messages[-1]["content"]
    lines = [json.loads(x) for x in (tmp_path / "transcript.jsonl").read_text().splitlines()]
    assert [x["call"] for x in lines] == [1, 2]
    assert lines[0]["parse_error"] and lines[1]["parse_error"] is None


def test_two_failures_give_schema_invalid():
    client = ScriptedClient(["nope", '{"action": "submit_design"}'])
    result = chat(client, "local-main", [{"role": "user", "content": "x"}], PlannerOutput)
    assert result.parsed is None
    assert result.error.startswith("SCHEMA_INVALID")


def test_think_tags_and_code_fences_are_parsed():
    replies = [
        "<think>plan it</think>" + json.dumps(GOOD),
        "```json\n" + json.dumps(GOOD) + "\n```",
    ]
    client = ScriptedClient(replies)
    for _ in replies:
        assert chat(client, "local-main", [], PlannerOutput).parsed is not None


def test_max_calls_one_means_no_retry():
    client = ScriptedClient(["bad", json.dumps(GOOD)])
    result = chat(client, "hosted", [], PlannerOutput, max_calls=1)
    assert result.calls == 1 and result.parsed is None


def test_recorded_reply_is_not_requested_again(tmp_path):
    path = tmp_path / "transcript.jsonl"
    meta = {"scope": "planner", "attempt": 1}
    chat(
        ScriptedClient([json.dumps(GOOD)]),
        "local-main",
        [],
        PlannerOutput,
        transcript=Transcript(path),
        meta=meta,
    )
    empty = ScriptedClient([])
    again = chat(empty, "local-main", [], PlannerOutput, transcript=Transcript(path), meta=meta)
    assert again.parsed is not None
    assert empty.calls == []


def test_replay_client_serves_in_order(tmp_path):
    path = tmp_path / "rec.jsonl"
    path.write_text("\n".join(json.dumps({"raw": f'{{"n": {i}}}', "model": "m"}) for i in range(2)))
    client = ReplayClient(path)
    assert [chat(client, "local-main", []).parsed for _ in range(2)] == [{"n": 0}, {"n": 1}]


# ---------------------------------------------------------------- live client on a mock server


def mock_server(structured_ok: bool, seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "mock-model"}]})
        body = json.loads(request.content)
        seen.append(body)
        if "response_format" in body and not structured_ok:
            return httpx.Response(400, json={"error": "response_format not supported"})
        if body.get("max_tokens") == 32:
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})
        return httpx.Response(
            200,
            json={
                "model": "mock-model",
                "choices": [
                    {"message": {"content": json.dumps(GOOD), "reasoning_content": "thought"}}
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            },
        )

    return httpx.MockTransport(handler)


ENDPOINT = Endpoint(
    label="local-main", base_url="http://mock.invalid/v1", model="mock-model", api_key="secret-key"
)


@pytest.mark.parametrize("structured_ok", [True, False])
def test_live_client_probes_structured_output(structured_ok, tmp_path):
    LiveClient._structured.clear()
    seen: list = []
    client = LiveClient({"local-main": ENDPOINT}, transport=mock_server(structured_ok, seen))
    t = Transcript(tmp_path / "transcript.jsonl")
    result = chat(
        client,
        "local-main",
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        PlannerOutput,
        transcript=t,
    )
    assert result.parsed is not None
    assert result.prompt_tokens == 11 and result.model == "mock-model"
    final = seen[-1]
    if structured_ok:
        assert "response_format" in final
    else:
        assert "response_format" not in final
        assert "JSON schema" in final["messages"][0]["content"]
        assert [m["role"] for m in final["messages"]] == ["system", "user"]
    text = (tmp_path / "transcript.jsonl").read_text()
    assert "mock.invalid" not in text and "secret-key" not in text
    assert json.loads(text.splitlines()[0])["reasoning"] == "thought"


def test_live_client_check_server_reports_without_hostnames():
    LiveClient._structured.clear()
    client = LiveClient({"local-main": ENDPOINT}, transport=mock_server(True, []))
    report = client.check_server("local-main")
    assert report["models_endpoint"] and report["structured_output"]
    assert "mock.invalid" not in json.dumps(report)


def test_live_client_down_raises():
    def down(request):
        raise httpx.ConnectError("refused")

    client = LiveClient({"local-main": ENDPOINT}, transport=httpx.MockTransport(down))
    with pytest.raises(EndpointUnavailable):
        client.complete("local-main", [], None, 0.3, 100)


def test_settings_never_show_the_key():
    s = load_settings(
        env_file=None,
        environ={"LABAUTO_MAIN_BASE_URL": "http://x.invalid/v1", "LABAUTO_MAIN_API_KEY": "k-123"},
    )
    assert s.main is not None and s.main.api_key == "k-123"
    assert "k-123" not in repr(s) and "k-123" not in s.model_dump_json()


# ---------------------------------------------------------------- router


def settings(main=16000, small=None):
    s = Settings(main=Endpoint(label="local-main", base_url="u", model="m", context_tokens=main))
    if small:
        s.small = Endpoint(label="local-small", base_url="u", model="s", context_tokens=small)
    return s


def test_roles_go_to_main_and_report_note_to_small(tmp_path):
    log = tmp_path / "model_calls.jsonl"
    assert route("planner", 100, settings(small=8000), log).label == "local-main"
    assert route("report_note", 100, settings(small=8000), log).label == "local-small"
    assert route("report_note", 100, settings(), log).label == "local-main"
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    assert len(lines) == 3 and all(x["reason"] for x in lines)


def test_big_prompt_moves_to_the_larger_context(tmp_path):
    r = route("report_note", 7000, settings(main=32000, small=8000), tmp_path / "log.jsonl")
    assert r.label == "local-main" and not r.trim
    assert "above 80%" in r.reason


def test_prompt_that_fits_nowhere_is_trimmed(tmp_path):
    r = route("repair", 20000, settings(main=16000, small=8000), tmp_path / "log.jsonl")
    assert r.trim and r.label == "local-main"
    assert "trimmed" in json.loads((tmp_path / "log.jsonl").read_text())["reason"]


def test_token_estimate_is_characters_over_four():
    assert estimate_tokens([{"content": "x" * 400}]) == 100


def test_reply_schema_sent_to_the_server_requires_every_key():
    """A grammar server lets a model skip optional keys; a local model then skipped "grid"."""
    from labauto.agents.llm import wire_schema
    from labauto.decide.model_decider import DeciderTurn

    sent = wire_schema(PlannerOutput.model_json_schema())
    design = sent["$defs"]["ModelDesign"]
    assert design["required"] == list(design["properties"])
    assert {"grid", "conditions", "include_controls", "notes"} <= set(design["required"])
    assert sent["$defs"]["Deviation"]["required"] == ["requested", "planned", "reason"]
    turn = wire_schema(DeciderTurn.model_json_schema())
    assert turn["required"] == ["action", "tool", "args", "picks", "round_note"]
    source = PlannerOutput.model_json_schema()
    wire_schema(source)
    assert "grid" not in source["$defs"]["ModelDesign"]["required"]  # the input is not changed


def test_live_client_sends_the_all_required_schema():
    LiveClient._structured.clear()
    seen: list = []
    client = LiveClient({"local-main": ENDPOINT}, transport=mock_server(True, seen))
    chat(client, "local-main", [{"role": "user", "content": "u"}], PlannerOutput)
    schema = seen[-1]["response_format"]["json_schema"]["schema"]
    assert "grid" in schema["$defs"]["ModelDesign"]["required"]


def one_reply_server(message: dict, finish_reason: str = "stop"):
    """A mock server whose chat replies are all the given message."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "mock-model"}]})
        body = json.loads(request.content)
        if body.get("max_tokens") == 32:
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})
        return httpx.Response(
            200, json={"choices": [{"message": message, "finish_reason": finish_reason}]}
        )

    return httpx.MockTransport(handler)


def test_ollama_reasoning_field_is_recorded(tmp_path):
    """Ollama returns thinking as "reasoning"; llama.cpp as "reasoning_content". Both are kept."""
    LiveClient._structured.clear()
    message = {"content": json.dumps(GOOD), "reasoning": "worked it out"}
    client = LiveClient({"local-main": ENDPOINT}, transport=one_reply_server(message))
    t = Transcript(tmp_path / "transcript.jsonl")
    result = chat(
        client, "local-main", [{"role": "user", "content": "u"}], PlannerOutput, transcript=t
    )
    assert result.parsed is not None
    record = json.loads((tmp_path / "transcript.jsonl").read_text().splitlines()[0])
    assert record["reasoning"] == "worked it out"


def test_a_reply_cut_off_at_the_token_limit_says_so():
    LiveClient._structured.clear()
    message = {"content": '{"action": "submit_design", "design": {"title": "half'}
    client = LiveClient({"local-main": ENDPOINT}, transport=one_reply_server(message, "length"))
    result = chat(
        client, "local-main", [{"role": "user", "content": "u"}], PlannerOutput, max_tokens=50
    )
    assert result.parsed is None
    assert result.error.startswith("SCHEMA_INVALID")
    assert "hit the 50 token limit" in result.error
