"""The planner's reply schema names the assay's factors. No network."""

from __future__ import annotations

import json

from conftest import ROOT

from labauto.agents.llm import ScriptedClient, schema_hash, wire_schema
from labauto.agents.planner import reply_schema
from labauto.config import Settings, load_assay
from labauto.models import PlannerOutput
from labauto.orchestrator import Clients, load_state, start_run

ASSAY = ROOT / "configs" / "assay_reaction_screen.json"
WORKCELL = ROOT / "configs" / "workcell_ot2.json"


def design_props(schema: dict) -> dict:
    return schema["$defs"]["ModelDesign"]["properties"]


def branch(prop: dict, kind: str) -> dict:
    (found,) = [b for b in prop["anyOf"] if b.get("type") == kind]
    return found


def test_grid_and_conditions_name_exactly_the_assays_factors():
    assay = load_assay(ASSAY)
    names = list(assay.factors)
    props = design_props(reply_schema(assay))
    grid = branch(props["grid"], "object")
    assert list(grid["properties"]) == names
    assert grid["required"] == names
    assert grid["additionalProperties"] is False
    for spec in grid["properties"].values():
        assert spec == {"items": {"type": "number"}, "type": "array"}
    cond = branch(props["conditions"], "array")["items"]
    assert list(cond["properties"]) == names
    assert cond["required"] == names
    assert cond["additionalProperties"] is False
    assert all(spec == {"type": "number"} for spec in cond["properties"].values())


def test_only_the_factor_maps_change():
    """Everything outside grid and conditions is the generic wire schema, unchanged."""
    assay = load_assay(ASSAY)
    ours = reply_schema(assay)
    generic = wire_schema(PlannerOutput.model_json_schema())
    for key in ("grid", "conditions"):
        design_props(ours).pop(key)
        design_props(generic).pop(key)
    assert ours == generic


def test_reply_schema_does_not_change_the_pydantic_schema():
    before = PlannerOutput.model_json_schema()
    reply_schema(load_assay(ASSAY))
    assert PlannerOutput.model_json_schema() == before


class CapturingClient(ScriptedClient):
    """A scripted client that also keeps the schema each call sent."""

    def __init__(self, replies):
        super().__init__(replies)
        self.schemas: list[dict | None] = []

    def complete(self, label, messages, schema, temperature, max_tokens):
        self.schemas.append(schema)
        return super().complete(label, messages, schema, temperature, max_tokens)


def test_a_planner_run_sends_the_assays_schema(tmp_path):
    replies = json.loads((ROOT / "examples" / "scripted" / "02_too_big.json").read_text())
    client = CapturingClient(replies)
    request = (ROOT / "examples" / "requests" / "02_too_big.txt").read_text()
    run = start_run(request, str(ASSAY), str(WORKCELL), Clients(local=client), Settings(), tmp_path)
    assert load_state(run).state == "AWAITING_APPROVAL"
    expected = reply_schema(load_assay(ASSAY))
    assert client.schemas and all(s == expected for s in client.schemas)


def test_the_transcript_says_which_schema_a_planner_run_sent(tmp_path):
    """A run under the factor-named schema can be told from one under the generic schema."""
    replies = json.loads((ROOT / "examples" / "scripted" / "02_too_big.json").read_text())
    request = (ROOT / "examples" / "requests" / "02_too_big.txt").read_text()
    run = start_run(
        request,
        str(ASSAY),
        str(WORKCELL),
        Clients(local=ScriptedClient(replies)),
        Settings(),
        tmp_path,
    )
    lines = (run / "transcript.jsonl").read_text().splitlines()
    hashes = {json.loads(line)["schema_hash"] for line in lines}
    named = schema_hash(reply_schema(load_assay(ASSAY)))
    assert hashes == {named}
    assert named != schema_hash(wire_schema(PlannerOutput.model_json_schema()))


def test_a_reply_that_breaks_the_grammar_is_still_checked(tmp_path):
    """A server may ignore the schema. A wrong factor name then still fails the design rules."""
    replies = json.loads((ROOT / "examples" / "scripted" / "02_too_big.json").read_text())
    first = json.loads(json.dumps(replies[0]))  # a deep copy of the scripted first reply
    grid = first["design"]["grid"]
    grid["encoding"] = grid.pop("base_equiv")
    client = CapturingClient([first, *replies[1:]])
    request = (ROOT / "examples" / "requests" / "02_too_big.txt").read_text()
    run = start_run(request, str(ASSAY), str(WORKCELL), Clients(local=client), Settings(), tmp_path)
    errors = json.loads((run / "attempts" / "01_local" / "errors.json").read_text())
    codes = {e["code"] for e in errors}
    assert {"UNKNOWN_FACTOR", "MISSING_FACTOR"} <= codes
