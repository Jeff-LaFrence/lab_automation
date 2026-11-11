"""Test sets, the faithful check, and the evaluation page built from them. No network."""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path

import pytest
from conftest import ROOT, make_design

from labauto.agents.llm import ScriptedClient
from labauto.cli import main
from labauto.config import Settings
from labauto.models import Design
from labauto.orchestrator import Clients
from labauto.planner_eval import default_out_path, evaluate, render
from labauto.testsets import (
    TESTSETS_DIR,
    FaithfulRules,
    SetError,
    UnknownSet,
    faithful_failures,
    list_sets,
    load_set,
)

ASSAY = "configs/assay_reaction_screen.json"
OT2 = "configs/workcell_ot2.json"
SPEC_SCENARIOS = [
    "01_simple_factorial",
    "02_too_big",
    "03_out_of_range",
    "04_ambiguous",
    "05_reagent_limit",
]
EXPECT = {"as_asked_valid": True, "deviation_required": False, "assumption_required": False}


def write_set(root: Path, name: str, requests: list[dict], **top) -> Path:
    """Write a small test set: one request file per entry, then the manifest."""
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    entries = []
    for r in requests:
        r = dict(r)
        text = r.pop("text", "Screen catalyst at 2 mol % with base at 1.5 equivalents.")
        entry = {
            "id": r["id"],
            "file": f"{r['id']}.txt",
            "intent": "What a faithful answer is.",
            "arithmetic": "The numbers behind it.",
            **r,
        }
        if "expect" not in entry and "expect_from" not in entry:
            entry["expect"] = EXPECT
        if text is not None:
            (folder / entry["file"]).write_text(text + "\n" if text else "")
        entries.append(entry)
    manifest = {
        "set": name,
        "description": "A set written by a test.",
        "assay": ASSAY,
        "workcell": OT2,
        "requests": entries,
        **top,
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return folder


# ---------------------------------------------------------------- the loader


def test_a_set_loads_by_folder_or_manifest_path(tmp_path):
    folder = write_set(tmp_path, "small", [{"id": "a"}, {"id": "b"}])
    for arg in (folder, folder / "manifest.json"):
        tset = load_set(arg)
        assert tset.name == "small" and tset.ids() == ["a", "b"]
        assert tset.requests[0].text.startswith("Screen catalyst")
        assert tset.requests[0].assay == ASSAY and tset.requests[0].workcell == OT2
        assert tset.requests[0].expect_source == "small/a"


def test_a_request_may_override_the_assay_and_workcell(tmp_path):
    other = "configs/workcell_ot2_conflict.json"
    tset = load_set(write_set(tmp_path, "small", [{"id": "a"}, {"id": "b", "workcell": other}]))
    assert [r.workcell for r in tset.requests] == [OT2, other]
    with pytest.raises(SetError, match="workcell file configs/nowhere.json does not exist"):
        load_set(write_set(tmp_path, "bad", [{"id": "a", "workcell": "configs/nowhere.json"}]))


def test_expect_from_takes_the_expectations_of_another_set(tmp_path):
    rules = {"fixed": {"catalyst_mol_pct": 2}, "replicates": [3]}
    write_set(tmp_path, "base", [{"id": "a", "expect": {**EXPECT, "faithful": rules}}])
    para = write_set(
        tmp_path,
        "para",
        [{"id": "x", "expect_from": "base/a"}, {"id": "y", "expect_from": "para/x"}],
    )
    tset = load_set(para)
    for r in tset.requests:
        assert r.expect_source == "base/a"
        assert r.expect.faithful.fixed == {"catalyst_mol_pct": 2.0}
        assert r.expect.faithful.replicates == [3]


@pytest.mark.parametrize(
    "target, message",
    [
        ("nowhere/a", "there is no test set 'nowhere'"),
        ("base/zz", "set 'base' has no request 'zz'"),
        ("no-slash", "must look like '<set>/<id>'"),
    ],
)
def test_expect_from_a_missing_target_fails_clearly(tmp_path, target, message):
    write_set(tmp_path, "base", [{"id": "a"}])
    para = write_set(tmp_path, "para", [{"id": "x", "expect_from": target}])
    with pytest.raises(SetError, match=message):
        load_set(para)


def test_expect_from_cycles_fail_clearly(tmp_path):
    loop = write_set(
        tmp_path,
        "loop",
        [{"id": "a", "expect_from": "loop/b"}, {"id": "b", "expect_from": "loop/a"}],
    )
    with pytest.raises(SetError, match=r"cycle: loop/a -> loop/b -> loop/a"):
        load_set(loop)
    itself = write_set(tmp_path, "self", [{"id": "a", "expect_from": "self/a"}])
    with pytest.raises(SetError, match=r"cycle: self/a -> self/a"):
        load_set(itself)


@pytest.mark.parametrize(
    "where",
    ["manifest", "request", "expect", "faithful"],
)
def test_unknown_keys_are_rejected(tmp_path, where):
    request = {"id": "a", "expect": {**EXPECT, "faithful": {"replicates": [3]}}}
    top = {}
    if where == "manifest":
        top["notes"] = "not a field"
    elif where == "request":
        request["notes"] = "not a field"
    elif where == "expect":
        request["expect"]["valid"] = True
    else:
        request["expect"]["faithful"]["fixd"] = {"catalyst_mol_pct": 2}
    with pytest.raises(SetError, match="Extra inputs are not permitted"):
        load_set(write_set(tmp_path, "small", [request], **top))


def test_missing_or_empty_request_files_fail(tmp_path):
    with pytest.raises(SetError, match="small/a: request file a.txt does not exist"):
        load_set(write_set(tmp_path, "small", [{"id": "a", "text": None}]))
    with pytest.raises(SetError, match="empty/a: request file a.txt is empty"):
        load_set(write_set(tmp_path, "empty", [{"id": "a", "text": "  "}]))


def test_manifest_shape_errors(tmp_path):
    both = {"id": "a", "expect_from": "core/02_too_big", "expect": EXPECT}
    for name, entry in (("both", both), ("neither", {"id": "a", "expect": None})):
        with pytest.raises(SetError, match="exactly one of 'expect' and 'expect_from'"):
            load_set(write_set(tmp_path, name, [entry]))
    with pytest.raises(SetError, match="request id 'a' appears more than once"):
        load_set(write_set(tmp_path, "twice", [{"id": "a"}, {"id": "a"}]))
    with pytest.raises(SetError, match="'set' is 'other' but the folder is 'named'"):
        load_set(write_set(tmp_path, "named", [{"id": "a"}], set="other"))
    with pytest.raises(SetError, match="as_asked_valid: Field required"):
        bare = {"deviation_required": False, "assumption_required": False}
        load_set(write_set(tmp_path, "bare", [{"id": "a", "expect": bare}]))


@pytest.mark.parametrize(
    "faithful, message",
    [
        (
            {"fixed": {"catalyst_mol_pct": 15}},
            "catalyst_mol_pct 15.0, which is not an allowed value",
        ),
        ({"allowed": {"catalyst_mol_pct": [1.2]}}, "catalyst_mol_pct 1.2, which is not an allowed"),
        ({"fixed": {"temperature": 20}}, "names factor 'temperature'"),
        (
            {"grid": {"catalyst_mol_pct": [1, 2]}},
            "must name every factor; it leaves out base_equiv",
        ),
        ({"required_any": {"base_equiv": []}}, "gives no values for base_equiv"),
        ({"min_conditions": 5, "max_conditions": 4}, "min_conditions .5. is larger than"),
    ],
)
def test_rules_no_valid_design_could_meet_are_refused(tmp_path, faithful, message):
    request = {"id": "a", "expect": {**EXPECT, "faithful": faithful}}
    with pytest.raises(SetError, match=message):
        load_set(write_set(tmp_path, "small", [request]))


def test_an_unknown_set_names_the_known_sets():
    with pytest.raises(UnknownSet) as info:
        load_set("no_such_set")
    assert "core" in info.value.known
    assert "Known sets: " in str(info.value) and "core" in str(info.value)


# ---------------------------------------------------------------- committed sets


def test_every_folder_under_testsets_has_a_manifest():
    root = ROOT / TESTSETS_DIR
    folders = sorted(d.name for d in root.iterdir() if d.is_dir())
    assert folders == list_sets()
    assert "core" in folders


@pytest.mark.parametrize("name", list_sets())
def test_every_committed_set_loads_and_its_request_files_are_not_empty(name):
    tset = load_set(name)
    assert tset.name == name and tset.requests
    for r in tset.requests:
        assert r.file.is_file(), r.file
        assert r.file.read_text(encoding="utf-8").strip(), r.file
        assert r.text


def test_core_is_exactly_the_five_spec_scenarios():
    core = load_set("core")
    assert core.ids() == SPEC_SCENARIOS
    requests = ROOT / "examples" / "requests"
    assert sorted(p.stem for p in requests.glob("*.txt")) == SPEC_SCENARIOS
    assert [r.file.resolve() for r in core.requests] == [
        (requests / f"{s}.txt").resolve() for s in SPEC_SCENARIOS
    ]
    # SPEC.md 8.11: deviations are needed for scenarios 02, 03, and 05.
    assert {r.id for r in core.requests if r.expect.deviation_required} == {
        "02_too_big",
        "03_out_of_range",
        "05_reagent_limit",
    }
    assert all(r.assay == ASSAY and r.workcell == OT2 for r in core.requests)


@pytest.mark.parametrize("scenario", SPEC_SCENARIOS)
def test_the_hand_written_answers_are_faithful_under_the_core_rules(scenario, assay):
    replies = json.loads((ROOT / "examples" / "scripted" / f"{scenario}.json").read_text())
    final = Design.model_validate(replies[-1]["design"])
    rules = load_set("core").get(scenario).expect.faithful
    assert faithful_failures(final, assay, rules) == []


# ---------------------------------------------------------------- the faithful check

GRID = {"catalyst_mol_pct": [1, 2, 4, 8], "base_equiv": [1.0, 1.5, 2.0], "cosolvent_pct": [20]}
ALL_TWELVE = [
    {"catalyst_mol_pct": c, "base_equiv": b, "cosolvent_pct": 20.0}
    for c, b in itertools.product([1.0, 2.0, 4.0, 8.0], [1.0, 1.5, 2.0])
]


@pytest.fixture
def factorial() -> Design:
    """12 conditions in triplicate with controls: 36 sample wells."""
    return make_design(grid=GRID, replicates=3, include_controls=True)


@pytest.mark.parametrize(
    "rules",
    [
        {"grid": GRID},
        {
            "grid": {
                "cosolvent_pct": [20.0],
                "base_equiv": [2, 1.5, 1],
                "catalyst_mol_pct": [8, 4, 2, 1],
            }
        },
        {"conditions": list(reversed(ALL_TWELVE))},
        {"fixed": {"cosolvent_pct": 20}},
        {"allowed": {"catalyst_mol_pct": [1, 2, 4, 8, 10]}},
        {"required_values": {"base_equiv": [1.5, 2.0]}},
        {"required_any": {"base_equiv": [2.0, 3.0]}},
        {"replicates": [3]},
        {"replicates": [2, 3]},
        {"include_controls": True},
        {"min_conditions": 12, "max_conditions": 12},
        {"min_sample_wells": 36, "max_sample_wells": 36},
        {},
    ],
)
def test_each_rule_passes_when_it_holds(factorial, assay, rules):
    assert faithful_failures(factorial, assay, FaithfulRules.model_validate(rules)) == []


@pytest.mark.parametrize(
    "rules, reason",
    [
        (
            {"grid": {**GRID, "catalyst_mol_pct": [1, 2, 4]}},
            "grid: 9 conditions were asked for and the design has 12; 3 were not asked for, "
            "for example catalyst_mol_pct 8.0, base_equiv 1.0, cosolvent_pct 20.0",
        ),
        (
            {"conditions": ALL_TWELVE[:2] + [{**ALL_TWELVE[0], "cosolvent_pct": 40}]},
            "conditions: 3 conditions were asked for and the design has 12; 1 asked for are "
            "missing, for example catalyst_mol_pct 1.0, base_equiv 1.0, cosolvent_pct 40.0; "
            "10 were not asked for, for example catalyst_mol_pct 1.0, base_equiv 2.0, cosolvent_pct 20.0",
        ),
        (
            {"fixed": {"catalyst_mol_pct": 2}},
            "fixed: catalyst_mol_pct should be 2.0 in every condition, but 9 of 12 conditions "
            "use 1.0, 4.0, 8.0",
        ),
        (
            {"allowed": {"catalyst_mol_pct": [1, 2]}},
            "allowed: catalyst_mol_pct may only be 1.0, 2.0, but the design also uses 4.0, 8.0",
        ),
        (
            {"required_values": {"base_equiv": [1.5, 2.5], "catalyst_mol_pct": [10]}},
            "required_values: no condition has base_equiv 2.5; no condition has catalyst_mol_pct 10.0",
        ),
        (
            {"required_any": {"base_equiv": [2.5, 3.0]}},
            "required_any: no condition has base_equiv at any of 2.5, 3.0",
        ),
        ({"replicates": [2]}, "replicates: the design has 3; expected 2"),
        ({"replicates": [1, 2]}, "replicates: the design has 3; expected one of 1, 2"),
        (
            {"include_controls": False},
            "include_controls: controls are on; they should be off",
        ),
        (
            {"min_conditions": 13},
            "min_conditions: the design has 12 conditions; at least 13 were expected",
        ),
        (
            {"max_conditions": 11},
            "max_conditions: the design has 12 conditions; at most 11 were expected",
        ),
        (
            {"min_sample_wells": 37},
            "min_sample_wells: the design has 36 sample wells (12 conditions x 3 replicates); "
            "at least 37 were expected",
        ),
        (
            {"max_sample_wells": 35},
            "max_sample_wells: the design has 36 sample wells (12 conditions x 3 replicates); "
            "at most 35 were expected",
        ),
    ],
)
def test_each_rule_fails_with_one_plain_reason(factorial, assay, rules, reason):
    assert faithful_failures(factorial, assay, FaithfulRules.model_validate(rules)) == [reason]


def test_every_failed_rule_gives_its_own_reason(factorial, assay):
    rules = FaithfulRules.model_validate(
        {"fixed": {"catalyst_mol_pct": 2}, "replicates": [2], "include_controls": True}
    )
    reasons = faithful_failures(factorial, assay, rules)
    assert [r.split(":")[0] for r in reasons] == ["fixed", "replicates"]


def test_a_conditions_design_is_judged_like_a_grid_design(assay):
    design = make_design(grid=None, conditions=ALL_TWELVE, replicates=3)
    assert faithful_failures(design, assay, FaithfulRules(grid=GRID)) == []


def test_values_are_compared_on_the_volume_grid(assay):
    whole = make_design(grid={"catalyst_mol_pct": [1], "base_equiv": [1], "cosolvent_pct": [20]})
    point = make_design(
        grid={"catalyst_mol_pct": [1.0], "base_equiv": [1.0000000001], "cosolvent_pct": [20.0]}
    )
    for design in (whole, point):
        for value in (1, 1.0):
            rules = FaithfulRules(fixed={"catalyst_mol_pct": value, "base_equiv": value})
            assert faithful_failures(design, assay, rules) == []
        one = {"catalyst_mol_pct": [1.0], "base_equiv": [1], "cosolvent_pct": [20.0]}
        assert faithful_failures(design, assay, FaithfulRules(grid=one)) == []
    # 1.2 mol % is 2.4 uL and 1.0 mol % is 2 uL: different steps, so no match.
    near = FaithfulRules(fixed={"catalyst_mol_pct": 1.2})
    assert faithful_failures(whole, assay, near) == [
        "fixed: catalyst_mol_pct should be 1.2 in every condition, but 1 of 1 conditions use 1.0"
    ]
    off = make_design(grid={"catalyst_mol_pct": [1.2], "base_equiv": [1.0], "cosolvent_pct": [20]})
    assert faithful_failures(off, assay, FaithfulRules(allowed={"catalyst_mol_pct": [1.0]})) == [
        "allowed: catalyst_mol_pct may only be 1.0, but the design also uses 1.2"
    ]


# ---------------------------------------------------------------- the evaluation page


def scripted(scenario: str) -> Clients:
    path = ROOT / "examples" / "scripted" / f"{scenario}.json"
    return Clients(local=ScriptedClient(json.loads(path.read_text())))


def cells(text: str, first: str) -> list[str]:
    """The cells of the table row that starts with this request id."""
    for line in text.splitlines():
        if line.startswith(f"| {first} |"):
            return [c.strip() for c in line.strip("|").split("|")]
    raise AssertionError(f"no row for {first}")


def test_scripted_evaluation_of_a_small_set(tmp_path):
    requests = ROOT / "examples" / "requests"
    tiny = write_set(
        tmp_path,
        "tiny",
        [
            {
                "id": "fits",
                "text": (requests / "01_simple_factorial.txt").read_text(),
                "expect": {**EXPECT, "faithful": {"grid": GRID, "replicates": [3]}},
            },
            {
                "id": "limit",
                "text": (requests / "05_reagent_limit.txt").read_text(),
                "expect": {
                    "as_asked_valid": False,
                    "deviation_required": True,
                    "assumption_required": True,
                    "faithful": {"fixed": {"catalyst_mol_pct": 10}, "replicates": [3]},
                },
            },
            {
                "id": "conflict",
                "text": (requests / "01_simple_factorial.txt").read_text(),
                "workcell": "configs/workcell_ot2_conflict.json",
            },
        ],
    )
    clients = iter(
        [
            scripted("01_simple_factorial"),
            scripted("05_reagent_limit"),
            scripted("01_simple_factorial"),
        ]
    )
    out = evaluate(
        lambda: next(clients),
        Settings(),
        trials=1,
        runs_dir=tmp_path / "runs",
        out_path=tmp_path / "eval.md",
        testset=tiny,
    )
    text = out.read_text()
    assert text.startswith("# Planner evaluation: tiny")
    assert "not from a model" in text
    assert "Test set: tiny. A set written by a test." in text
    assert "Manifest: `" in text and "tiny/manifest.json`" in text
    assert "Generated by `labauto eval-planner --set " in text and " --trials 1`" in text
    assert (
        "| Request | Trials | Valid first try | Valid after local repairs | Went to hosted "
        "| Handed to person | Deviations declared when needed | Median seconds "
        "| Assumptions listed when needed | Faithful | Failed (internal error) |"
    ) in text
    fits, limit, conflict = cells(text, "fits"), cells(text, "limit"), cells(text, "conflict")
    assert fits[:7] + fits[8:] == [
        "fits",
        "1",
        "1",
        "1",
        "0",
        "0",
        "n/a",
        "n/a",
        "1 of 1 passing",
        "0",
    ]
    assert limit[:7] + limit[8:] == [
        "limit",
        "1",
        "0",
        "1",
        "0",
        "0",
        "1 of 1 passing",
        "1 of 1 passing",
        "0 of 1 passing",
        "0",
    ]
    assert conflict[5] == "1" and conflict[9] == "n/a"  # no faithful rules: nothing to judge
    assert (
        "- Not faithful, rule replicates: 1 trials, in limit (1). "
        "Example from limit: the design has 2; expected 3"
    ) in text
    assert "DECK_CONFLICT: 1 times, in conflict (1)" in text
    assert str(Path.home()) not in text
    # Run folders are grouped by set and request, and each request used its own workcell.
    run = next((tmp_path / "runs" / "tiny" / "conflict").iterdir())
    assert json.loads((run / "state.json").read_text())["workcell"].endswith(
        "workcell_ot2_conflict.json"
    )
    assert next((tmp_path / "runs" / "tiny" / "fits").iterdir()).is_dir()


def test_the_core_page_keeps_the_spec_columns_first():
    text = render({}, 3, load_set("core"))
    assert text.startswith("# Planner evaluation\n")
    assert (
        "| Scenario | Trials | Valid first try | Valid after local repairs | Went to hosted "
        "| Handed to person | Deviations declared when needed | Median seconds "
        "| Assumptions listed when needed | Faithful | Failed (internal error) |"
    ) in text
    assert "Manifest: `examples/testsets/core/manifest.json`" in text
    assert "Generated by `labauto eval-planner --set core --trials 3`" in text


# ---------------------------------------------------------------- the command line


def test_default_out_paths():
    assert default_out_path("core") == Path("docs/planner_eval.md")
    assert default_out_path("paraphrase") == Path("docs/planner_eval_paraphrase.md")


@pytest.fixture
def fake_evaluate(monkeypatch):
    """Stand in for evaluate so the command can be checked without calling any model."""
    seen: dict = {}

    def fake(clients_factory, settings, **kwargs):
        seen.update(kwargs, settings=settings)
        return kwargs["out_path"]

    monkeypatch.setattr("labauto.planner_eval.evaluate", fake)
    monkeypatch.setenv("LABAUTO_MAIN_BASE_URL", "http://endpoint.invalid/v1")
    monkeypatch.setenv("LABAUTO_MAIN_MODEL", "model-from-env")
    return seen


def test_eval_planner_defaults_to_the_core_page(fake_evaluate):
    assert main(["eval-planner"]) == 0
    assert fake_evaluate["out_path"] == Path("docs/planner_eval.md")
    assert fake_evaluate["testset"].name == "core"
    assert fake_evaluate["trials"] == 5 and fake_evaluate["runs_dir"] == "runs/eval"
    assert fake_evaluate["settings"].main.model == "model-from-env"
    assert fake_evaluate["command"] == "labauto eval-planner --set core --trials 5"


def test_eval_planner_names_other_pages_by_set_and_takes_a_model(tmp_path, fake_evaluate):
    folder = write_set(tmp_path, "tiny", [{"id": "a"}])
    args = ["eval-planner", "--set", str(folder), "--trials", "2", "--model", "other:7b"]
    assert main(args) == 0
    assert fake_evaluate["out_path"] == Path("docs/planner_eval_tiny.md")
    assert fake_evaluate["settings"].main.model == "other:7b"
    assert os.environ["LABAUTO_MAIN_MODEL"] == "model-from-env"  # for that run only
    assert fake_evaluate["command"].endswith("--trials 2 --model other:7b")
    assert main(["eval-planner", "--out", "docs/elsewhere.md"]) == 0
    assert fake_evaluate["out_path"] == Path("docs/elsewhere.md")
    assert fake_evaluate["command"].endswith("--out docs/elsewhere.md")


def test_eval_planner_with_an_unknown_set_lists_the_known_ones(capsys):
    assert main(["eval-planner", "--set", "no_such_set"]) == 2
    out = capsys.readouterr().out
    assert "unknown test set 'no_such_set'" in out and "Known sets: " in out and "core" in out


# ---------------------------------------------------------------- pinning the expectations to code
#
# Each held-out request's literal design, and one faithful repair, go through labauto's own checks
# (every layer but the vendor simulator, for speed). If the assay, workcell, or rules change, these
# fail before a stale arithmetic line or a wrong as_asked_valid can be published.

from labauto.config import load_assay as _load_assay  # noqa: E402
from labauto.config import load_workcell as _load_workcell  # noqa: E402
from labauto.models import Design as _Design  # noqa: E402
from labauto.pipeline import check as _check  # noqa: E402
from labauto.testsets import faithful_failures as _faithful_failures  # noqa: E402
from labauto.testsets import load_set as _load_set  # noqa: E402

CAT, BASE, COS = "catalyst_mol_pct", "base_equiv", "cosolvent_pct"


def _grid(cat, base, cos=None):
    g = {CAT: cat, BASE: base}
    if cos is not None:
        g[COS] = cos
    return {"grid": g}


# id: (literal design or None, literal error codes, faithful repair)
HELD_OUT_PINS = {
    "h01_dose_response": (
        dict(**_grid([0.5, 1.0, 2.0, 3.0, 5.0, 7.5, 10.0], [2.0], [25.0]), replicates=3),
        set(),
        dict(**_grid([0.5, 1.0, 2.0, 3.0, 5.0, 7.5, 10.0], [2.0], [25.0]), replicates=3),
    ),
    "h02_condition_list": (
        None,  # same as the faithful design below
        set(),
        dict(
            conditions=[
                {CAT: 3.0, BASE: 1.5, COS: 20.0},
                {CAT: 6.0, BASE: 1.5, COS: 20.0},
                {CAT: 3.0, BASE: 2.5, COS: 20.0},
                {CAT: 3.0, BASE: 1.5, COS: 35.0},
                {CAT: 6.5, BASE: 1.75, COS: 20.0},
                {CAT: 8.0, BASE: 3.0, COS: 0.0},
            ],
            replicates=3,
        ),
    ),
    "h03_plate_uniformity": (
        None,
        set(),
        dict(conditions=[{CAT: 2.0, BASE: 1.5, COS: 20.0}], replicates=96, include_controls=False),
    ),
    "h04_temperature": (
        dict(
            grid={
                CAT: [1.0, 2.0, 4.0],
                "temperature_c": [25.0, 40.0, 60.0],
                BASE: [1.5],
                COS: [20.0],
            },
            replicates=2,
        ),
        {"UNKNOWN_FACTOR"},
        dict(**_grid([1.0, 2.0, 4.0], [1.5], [20.0]), replicates=2),
    ),
    "h05_between_steps": (
        dict(**_grid([1.2, 2.5, 5.0], [1.1, 2.0], [15.0]), replicates=3),
        {"FACTOR_OFF_GRID"},
        dict(**_grid([1.0, 2.5, 5.0], [1.0, 2.0], [15.0]), replicates=3),
    ),
    "h06_injection_cap": (
        dict(**_grid([2.0, 4.0, 6.0, 8.0], [1.0, 1.5, 2.0, 2.5], [20.0]), replicates=4),
        set(),  # the LC/MS queue limit is in the request, not in the code's checks
        dict(**_grid([2.0, 4.0, 6.0, 8.0], [1.0, 1.5, 2.0, 2.5], [20.0]), replicates=3),
    ),
    "h07_vague_cosolvent": (
        None,
        set(),
        dict(
            conditions=[{CAT: 2.0, BASE: 1.5, COS: c} for c in (0.0, 10.0, 20.0, 30.0)],
            replicates=3,
        ),
    ),
    "h08_notation": (
        None,
        set(),
        dict(
            **_grid([2.0, 4.0], [0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25], [10.0, 30.0]),
            replicates=2,
        ),
    ),
    "h09_tip_limit": (
        dict(**_grid([0.5, 1.0], [0.5, 0.75], [0.0, 5.0]), replicates=11),
        {"SOURCE_SHORT", "TIPS_SHORT"},
        dict(**_grid([0.5, 1.0], [0.5, 0.75], [0.0, 5.0]), replicates=4),
    ),
    "h10_solvent_limit": (
        dict(
            **_grid([0.5, 1.0, 1.5, 2.0, 2.5, 3.0], [1.0, 1.25], [0.0]),
            replicates=8,
            include_controls=False,
        ),
        {"SOURCE_SHORT"},
        dict(
            **_grid([0.5, 1.0, 1.5, 2.0, 2.5, 3.0], [1.0, 1.25], [0.0]),
            replicates=7,
            include_controls=False,
        ),
    ),
    "h11_unstated_factor": (
        dict(**_grid([2.0, 4.0, 6.0], [1.0, 2.0]), replicates=3),
        {"MISSING_FACTOR"},
        dict(**_grid([2.0, 4.0, 6.0], [1.0, 2.0], [20.0]), replicates=3),
    ),
}


def _run(spec, assay, workcell, name):
    design = _Design(design_id=f"pin-{name}", **{"include_controls": True, **spec})
    return design, _check(design, assay, workcell, None, skip_layers=("simulator", "ledger"))


def test_every_held_out_request_is_pinned():
    assert {r.id for r in _load_set("held_out").requests} == set(HELD_OUT_PINS)


@pytest.mark.parametrize("rid", sorted(HELD_OUT_PINS))
def test_held_out_expectations_match_the_checks(rid):
    tset = _load_set("held_out")
    req = next(r for r in tset.requests if r.id == rid)
    assay, workcell = _load_assay(req.assay), _load_workcell(req.workcell)
    literal, codes, repair = HELD_OUT_PINS[rid]
    literal = literal or repair
    _, result = _run(literal, assay, workcell, rid + "-literal")
    assert {e.code for e in result.errors} == codes
    if req.expect.as_asked_valid is not None:
        assert result.ok == req.expect.as_asked_valid
    design, result = _run(repair, assay, workcell, rid + "-repair")
    assert result.ok, [e.code for e in result.errors]
    assert _faithful_failures(design, assay, req.expect.faithful) == []


def test_paraphrases_take_core_expectations_and_04_stays_vague():
    import re

    tset = _load_set("paraphrase")
    assert [r.expect_source for r in tset.requests] == [
        "core/01_simple_factorial",
        "core/02_too_big",
        "core/03_out_of_range",
        "core/04_ambiguous",
        "core/05_reagent_limit",
    ]
    p04 = next(r for r in tset.requests if r.id == "p04_ambiguous")
    assert re.search(r"\d", p04.text) is None


def test_display_path_outside_the_repository_shows_only_the_last_name():
    from labauto.testsets import display_path

    shown = display_path(Path("/nowhere/someuser/myset"))
    assert shown == "<outside the repository>/myset"
    assert "someuser" not in shown
