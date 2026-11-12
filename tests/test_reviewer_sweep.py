"""The overwatch on the baseline sweep's plates: scores, resume, the stop on outages, and the page.

A small sweep is built with reviewer_eval.baseline_sweep from the hand-made
packs in test_review, and the overwatch is a ScriptedClient, so no test
touches the network or a model.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from test_review import clean_pack, clean_reply_with_a_false_alarm, good, seeded_build

from labauto.agents.llm import LiveClient, ScriptedClient
from labauto.cli import main
from labauto.config import Settings, repo_root
from labauto.review import PROMPTS
from labauto.reviewer_eval import SCRIPTED_BANNER, baseline_sweep
from labauto.reviewer_sweep import (
    STOP_AFTER,
    SWEEP_OUTS,
    TRIAL_FILE,
    plate_out,
    plate_source,
    pooled_sentence,
    review_sweep,
)

CASES = [
    {
        "id": "one_injection",
        "lcms_faults": ["injection_failure"],
        "nmr_faults": [],
        "seed": 5,
        "no_fault_on": [5001],
    },
    {"id": "clean", "lcms_faults": [], "nmr_faults": [], "seed": 6},
    {
        "id": "hidden",
        "lcms_faults": [],
        "nmr_faults": ["nmr_impurity"],
        "seed": 7,
        "hard": True,
        "note": "an impurity no rule sees",
    },
]
# The sweep's plates in its order. Seed 5001 is skipped: its fault did not happen.
PLATES = [
    ("one_injection", 5002),
    ("one_injection", 5003),
    ("clean", 6001),
    ("clean", 6002),
    ("hidden", 7001),
    ("hidden", 7002),
]


def sweep_build(case, out_dir):
    """seeded_build, plus a hard case: a clean pack whose fault log lists an impurity in G1."""
    if case["id"] == "hidden":
        out_dir.mkdir(parents=True, exist_ok=True)
        return clean_pack(), [{"kind": "nmr_impurity", "wells": ["G1"]}]
    return seeded_build(case, out_dir)


def no_fault() -> dict:
    reply = clean_reply_with_a_false_alarm()
    reply["suspected_faults"] = []
    return reply


def names_g1() -> dict:
    reply = clean_reply_with_a_false_alarm()
    reply["suspected_faults"] = [
        {"kind": "nmr_impurity", "wells": ["G1"], "reason": "Its singlet reads high."}
    ]
    return reply


def invented() -> dict:
    reply = no_fault()
    reply["conditions"][0]["reason"] = "Readouts agree at 61.7."
    return reply


# One reply per plate, in PLATES order, except the last plate: three replies that each
# fail the number check, so it ends with no valid review.
REPLIES = [
    good(),  # one_injection: names injection_failure in A2, caught
    good(),
    no_fault(),  # clean 6001: QC plus statistics raise a false alarm here, the overwatch none
    clean_reply_with_a_false_alarm(),  # clean 6002: the overwatch names E1, a false alarm
    names_g1(),  # hidden 7001: the overwatch finds the impurity no rule sees
    invented(),
    invented(),
    invented(),
]

HEADER = (
    "| Case | Plates reviewed | True faults "
    "| QC rules: faults caught | QC rules: well recall | QC rules: plates with a false alarm "
    "| QC plus statistics: faults caught | QC plus statistics: well recall "
    "| QC plus statistics: plates with a false alarm "
    "| Overwatch alone: faults caught | Overwatch alone: well recall "
    "| Overwatch alone: plates with a false alarm "
    "| QC rules or overwatch: faults caught | QC rules or overwatch: well recall "
    "| QC rules or overwatch: plates with a false alarm "
    "| QC plus statistics or overwatch: faults caught "
    "| QC plus statistics or overwatch: well recall "
    "| QC plus statistics or overwatch: plates with a false alarm "
    "| No valid review | Ended on an outage |"
)
FULL_ROWS = [
    ["one_injection", "2 of 2", "2"] + ["2 of 2", "1.00 (2 of 2)", "0 of 2"] * 5 + ["0 of 2", "0"],
    ["clean", "2 of 2", "0"]
    + ["0 of 0", "n/a (0 of 0)", "0 of 2"]
    + ["0 of 0", "n/a (0 of 0)", "1 of 2"] * 3
    + ["0 of 0", "n/a (0 of 0)", "2 of 2"]
    + ["0 of 2", "0"],
    ["hidden (hard)", "2 of 2", "2"]
    + ["0 of 2", "0.00 (0 of 2)", "0 of 2"] * 2
    + ["1 of 2", "0.50 (1 of 2)", "0 of 2"] * 3
    + ["1 of 2", "0"],
    ["**Total**", "6 of 6", "4"]
    + ["2 of 4", "0.50 (2 of 4)", "0 of 6"]
    + ["2 of 4", "0.50 (2 of 4)", "1 of 6"]
    + ["3 of 4", "0.75 (3 of 4)", "1 of 6"] * 2
    + ["3 of 4", "0.75 (3 of 4)", "2 of 6"]
    + ["1 of 6", "0"],
    ["**Hard cases**", "2 of 2", "2"]
    + ["0 of 2", "0.00 (0 of 2)", "0 of 2"] * 2
    + ["1 of 2", "0.50 (1 of 2)", "0 of 2"] * 3
    + ["1 of 2", "0"],
]


def table(page: str) -> list[list[str]]:
    """The per case table's cells, header and divider left out."""
    lines = page.splitlines()
    start = lines.index(HEADER)
    rows = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        rows.append([c.strip() for c in line.strip("|").split("|")])
    return rows


def hashes(folder: Path) -> dict[str, str]:
    return {
        str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def assert_portable(text: str) -> None:
    """No absolute path, home folder, or user name in a page that may be committed."""
    for local in (str(repo_root().resolve()), str(Path.home())):
        assert local not in text
    assert not re.search(r"(?<![\w.<])/(home|Users|root|tmp|mnt|var)/", text)


@pytest.fixture
def source(tmp_path) -> Path:
    """An eval-reviewer run folder holding a baseline sweep of three cases, two seeds each."""
    folder = tmp_path / "eval_run"
    baseline_sweep(CASES, sweep_build, folder, 2)
    return folder


def test_the_sweep_review_scores_every_plate_next_to_both_baselines(tmp_path, source):
    before = hashes(source)
    client = ScriptedClient(REPLIES)
    out = tmp_path / "sweep.md"
    result = review_sweep(source, client, runs_dir=tmp_path / "runs", out_path=out, command="cmd")
    assert result.path == out and result.plates == 6 and result.reviewed == 6
    assert result.outages == 0 and not result.stopped and result.asked == 6
    assert len(client.calls) == len(REPLIES)
    # The overwatch saw each plate's own evidence pack, read from the sweep folder.
    pack = json.loads((plate_source(source, "one_injection", 5002) / "evidence.json").read_text())
    assert client.calls[0][1][1]["content"] == "EVIDENCE PACK\n" + json.dumps(pack)
    page = out.read_text()
    assert page.splitlines()[0] == "# Overwatch on the baseline sweep"
    assert page.splitlines()[2] == SCRIPTED_BANNER
    assert table(page) == FULL_ROWS
    assert "not finished" not in page
    assert "The overwatch reviewed 6 of 6 plates in the sweep." in page
    assert (
        "On the 6 reviewed plates, pooling the overwatch with QC plus statistics caught 1 more "
        "true fault (2 to 3 of 4), named 1 more faulty well (2 to 3 of 4), and added a false "
        "alarm on 1 more plate (1 to 2 of 6). On the 2 reviewed hard plates, pooling the "
        "overwatch with QC plus statistics caught 1 more true fault (0 to 1 of 2), named 1 more "
        "faulty well (0 to 1 of 2), and added no plate with a false alarm." in page
    )
    assert f"the baseline sweep of `{source.relative_to(repo_root())}`" in page
    assert "Overwatch model, as reported by the endpoint: scripted" in page
    assert "Overwatch prompt: default (data_reviewer.md)" in page
    assert "Model replies: 8" in page
    assert "No valid review: 1 of 6 reviewed plates." in page
    assert "Git commit: " in page and "Command: `cmd`" in page
    assert "Pooling can only add false alarms." in page
    assert "set with the hard-case recipes known" in page and "One trial per plate." in page
    assert (
        "- invented_number: 3 times, on 1 plate in hidden. Example from hidden, seed 7002, "
        "reply 1:" in page
    )
    assert "61.7" in page and "No valid review on 1 plate:" in page
    assert "- hidden, seed 7002: NEEDS a person" in page
    assert "- Overwatch alone, false alarms: nmr_impurity in clean." in page
    assert "- QC plus statistics, missed: nmr_impurity in hidden (2)." in page
    assert_portable(page)
    # Each plate has its trial, transcript, and route log. Skipped seed 5001 is not a plate.
    run_dir = result.run_dir
    for cid, seed in PLATES:
        for name in (TRIAL_FILE, "transcript.jsonl", "model_calls.jsonl"):
            assert (plate_out(run_dir, cid, seed) / name).is_file(), (cid, seed, name)
    assert not (run_dir / "one_injection" / "seed_5001").exists()
    trial = json.loads((plate_out(run_dir, "hidden", 7001) / TRIAL_FILE).read_text())
    assert [f["kind"] for f in trial["either_stats"]["caught"]] == ["nmr_impurity"]
    assert trial["qc_stats"]["score"]["caught"] == [] and trial["qc_stats"]["problems"] == []
    run = json.loads((run_dir / "run.json").read_text())
    assert run["source"] == str(source.relative_to(repo_root()))
    assert run["prompt"] == "default" and run["prompt_file"] == "data_reviewer.md"
    assert run["cases"] == ["one_injection", "clean", "hidden"] and run["command"] == "cmd"
    assert_portable(json.dumps(run))
    # The source was only read.
    assert hashes(source) == before


def test_a_run_stops_after_outages_in_a_row_and_a_resume_finishes_it(tmp_path, source):
    before = hashes(source)
    out = tmp_path / "sweep.md"
    first = ScriptedClient(REPLIES[:2])  # silent from the third plate on
    result = review_sweep(source, first, runs_dir=tmp_path / "runs", out_path=out)
    assert result.stopped and result.reviewed == 2 and result.outages == STOP_AFTER == 3
    assert result.asked == 5 and len(first.calls) == 5
    run_dir = result.run_dir
    # The sixth plate was never tried. The page was still written, from the two plates done.
    assert not (plate_out(run_dir, "hidden", 7002) / TRIAL_FILE).exists()
    page = out.read_text()
    assert (
        "**This run is not finished: the overwatch reviewed 2 of 6 plates in its cases. 3 ended "
        "on an endpoint outage and 1 was not tried yet." in page
    )
    assert f"--resume {run_dir.relative_to(repo_root())}` continues it." in page
    rows = table(page)
    assert rows[0] == FULL_ROWS[0]
    assert rows[1][:3] == ["clean", "0 of 2", "0"] and rows[1][-1] == "2"
    assert rows[3][:3] == ["**Total**", "2 of 6", "2"] and rows[3][-1] == "3"
    assert "added no plate with a false alarm. No hard plate has been reviewed yet." in page
    assert "Ended on an endpoint outage on 3 plates:" in page
    done = {p: (plate_out(run_dir, *p) / TRIAL_FILE).read_bytes() for p in PLATES[:2]}
    assert json.loads((plate_out(run_dir, "clean", 6001) / TRIAL_FILE).read_text())["outage"]

    second = ScriptedClient(REPLIES[2:])
    result = review_sweep(source, second, resume=run_dir, out_path=out, command="again")
    assert not result.stopped and result.reviewed == 6 and result.outages == 0
    # No call for the two plates done; each outage plate was tried again.
    assert result.asked == 4 and len(second.calls) == len(REPLIES) - 2
    assert all(m[1]["content"] != first.calls[0][1][1]["content"] for _, m in second.calls)
    assert {p: (plate_out(run_dir, *p) / TRIAL_FILE).read_bytes() for p in PLATES[:2]} == done
    assert not json.loads((plate_out(run_dir, "clean", 6001) / TRIAL_FILE).read_text())["outage"]
    page = out.read_text()
    assert table(page) == FULL_ROWS and "not finished" not in page
    assert "Command: `labauto eval-reviewer-sweep`  \nResumed with: `again`" in page
    run = json.loads((run_dir / "run.json").read_text())
    assert [s["command"] for s in run["sessions"]] == ["labauto eval-reviewer-sweep", "again"]
    # A finished run asks nothing more.
    third = ScriptedClient([])
    result = review_sweep(source, third, resume=run_dir, out_path=out)
    assert result.asked == 0 and third.calls == [] and result.reviewed == 6
    assert hashes(source) == before


def test_a_run_where_the_model_never_answers_writes_no_page(tmp_path, source):
    out = tmp_path / "sweep.md"
    result = review_sweep(source, ScriptedClient([]), runs_dir=tmp_path / "runs", out_path=out)
    assert result.path is None and result.stopped and result.reviewed == 0
    assert not out.exists()


def test_cases_select_and_order_the_plates(tmp_path, source):
    client = ScriptedClient([names_g1(), *[invented()] * 3, good(), good()])
    out = tmp_path / "sweep.md"
    result = review_sweep(
        source,
        client,
        cases=["hidden", "one_injection"],
        runs_dir=tmp_path / "runs",
        out_path=out,
    )
    assert result.plates == 4 and result.reviewed == 4 and len(client.calls) == 6
    hidden = json.loads((plate_source(source, "hidden", 7001) / "evidence.json").read_text())
    assert client.calls[0][1][1]["content"] == "EVIDENCE PACK\n" + json.dumps(hidden)
    page = out.read_text()
    # The table keeps the sweep's order and lists only the run's cases.
    assert table(page) == [FULL_ROWS[0], FULL_ROWS[2]] + [
        ["**Total**", "4 of 4", "4"]
        + ["2 of 4", "0.50 (2 of 4)", "0 of 4"] * 2
        + ["3 of 4", "0.75 (3 of 4)", "0 of 4"] * 3
        + ["1 of 4", "0"],
        FULL_ROWS[4],
    ]
    assert (
        "The overwatch reviewed 4 of 6 plates in the sweep. This run covers 2 of the sweep's 3 "
        "cases (one_injection, hidden), with 4 plates." in page
    )
    run = json.loads((result.run_dir / "run.json").read_text())
    assert run["cases"] == ["hidden", "one_injection"]
    # A resume may add a case; the run then covers it too.
    more = ScriptedClient([no_fault(), clean_reply_with_a_false_alarm()])
    result = review_sweep(source, more, cases=["clean"], resume=result.run_dir, out_path=out)
    assert result.asked == 2 and result.plates == 6 and result.reviewed == 6
    run = json.loads((result.run_dir / "run.json").read_text())
    assert run["cases"] == ["hidden", "one_injection", "clean"]
    assert [r[0] for r in table(out.read_text())][:3] == ["one_injection", "clean", "hidden (hard)"]
    for bad, message in (
        (["nope"], "unknown case nope"),
        (["clean", "clean"], "named more than once"),
    ):
        with pytest.raises(ValueError, match=message):
            review_sweep(source, more, cases=bad, runs_dir=tmp_path / "bad", out_path=out)
    assert not (tmp_path / "bad").exists()


def test_the_default_pages_are_for_a_live_client_only(tmp_path, source, monkeypatch):
    monkeypatch.chdir(tmp_path)  # so a broken guard could only write inside tmp_path
    runs = tmp_path / "runs"
    for prompt, out, page in (
        ("default", None, "docs/reviewer_sweep.md"),
        ("no_hints", None, "docs/reviewer_sweep_no_hints.md"),
        ("default", "docs/../docs/reviewer_sweep_no_hints.md", "docs/reviewer_sweep_no_hints.md"),
        ("default", repo_root() / "docs" / "reviewer_sweep.md", "docs/reviewer_sweep.md"),
    ):
        with pytest.raises(ValueError, match=f"only a live client writes {page}"):
            review_sweep(
                source, ScriptedClient(REPLIES), prompt=prompt, runs_dir=runs, out_path=out
            )
    with pytest.raises(ValueError, match="docs/reviewer_eval.md is the eval-reviewer page"):
        review_sweep(
            source, ScriptedClient(REPLIES), runs_dir=runs, out_path="docs/reviewer_eval.md"
        )
    assert not runs.exists() and not (tmp_path / "docs").exists()


def test_the_command_refuses_a_default_page_for_a_scripted_client(tmp_path, source, capsys):
    script = tmp_path / "s.json"
    script.write_text(json.dumps(REPLIES))
    base = ["eval-reviewer-sweep", str(source), "--client", f"scripted:{script}"]
    base += ["--runs", str(tmp_path / "runs")]
    for extra, page in (
        ([], "docs/reviewer_sweep.md"),
        (["--prompt", "no_hints"], "docs/reviewer_sweep_no_hints.md"),
        (["--out", str(repo_root() / "docs" / "reviewer_sweep.md")], "docs/reviewer_sweep.md"),
    ):
        assert main([*base, *extra]) == 2
        assert f"does not write {page}." in capsys.readouterr().out
    assert main([*base, "--out", "docs/reviewer_eval_no_hints.md"]) == 2
    assert "is the eval-reviewer page" in capsys.readouterr().out
    assert main([*base, "--model", "tag", "--out", str(tmp_path / "p.md")]) == 2
    assert not (tmp_path / "runs").exists()


def test_the_command_prints_how_to_resume_and_the_resume_finishes(tmp_path, source, capsys):
    before = hashes(source)
    script = tmp_path / "s.json"
    out = tmp_path / "sweep.md"
    args = ["eval-reviewer-sweep", str(source), "--client", f"scripted:{script}"]
    args += ["--runs", str(tmp_path / "runs"), "--out", str(out)]
    script.write_text(json.dumps(REPLIES[:2]))
    assert main(args) == 1
    printed = capsys.readouterr().out
    assert f"Wrote {out}" in printed
    assert "The endpoint stopped answering 3 times in a row, so the run stopped." in printed
    assert "Not reviewed yet: 3 ended on an outage, 1 not tried. To resume:" in printed
    resume = printed.split("To resume:\n", 1)[1].splitlines()[0].split()
    assert resume[:2] == ["labauto", "eval-reviewer-sweep"] and "--resume" in resume
    page = out.read_text()
    assert_portable(page)
    command = re.search(r"Generated by `([^`]*)`", page).group(1)
    assert command.startswith(f"labauto eval-reviewer-sweep {source.relative_to(repo_root())} ")
    assert "--client scripted:" in command and "--out " in command

    script.write_text(json.dumps(REPLIES[2:]))
    assert main(resume[1:]) == 0
    printed = capsys.readouterr().out
    assert "reviewed 6 of 6 plates" in printed and "To resume" not in printed
    page = out.read_text()
    assert table(page) == FULL_ROWS and "Resumed with: `labauto eval-reviewer-sweep" in page
    assert_portable(page)
    assert hashes(source) == before


def test_a_resume_keeps_its_prompt_and_source_and_nothing_is_written_in_the_source(
    tmp_path, source
):
    out = tmp_path / "sweep.md"
    result = review_sweep(source, ScriptedClient([]), runs_dir=tmp_path / "runs", out_path=out)
    with pytest.raises(ValueError, match="uses the default prompt, not no_hints"):
        review_sweep(
            source, ScriptedClient([]), prompt="no_hints", resume=result.run_dir, out_path=out
        )
    other = tmp_path / "other"
    baseline_sweep(CASES, sweep_build, other, 1)
    with pytest.raises(ValueError, match="reviews the sweep of"):
        review_sweep(other, ScriptedClient([]), resume=result.run_dir, out_path=out)
    with pytest.raises(ValueError, match="not a sweep review run"):
        review_sweep(source, ScriptedClient([]), resume=tmp_path / "nothing", out_path=out)
    before = hashes(source)
    for runs, page in ((source / "runs", out), (tmp_path / "runs", source / "page.md")):
        with pytest.raises(ValueError, match="inside the source"):
            review_sweep(source, ScriptedClient(REPLIES), runs_dir=runs, out_path=page)
    with pytest.raises(ValueError, match="no baseline sweep"):
        review_sweep(tmp_path / "runs", ScriptedClient([]), runs_dir=tmp_path / "x", out_path=out)
    assert hashes(source) == before


def test_the_pooled_sentence_says_plainly_when_the_overwatch_added_nothing():
    base = {"reviewed": 10, "true": 12, "true_wells": 30}
    same = {**base, "qc_stats_caught": 11, "either_stats_caught": 11}
    same |= {"qc_stats_hits": 20, "either_stats_hits": 20}
    assert pooled_sentence(
        {**same, "qc_stats_alarm": 1, "either_stats_alarm": 1}, "the plates"
    ) == (
        "On the plates, pooling the overwatch with QC plus statistics added nothing: it caught no "
        "more true faults (11 of 12) and named no more faulty wells (20 of 30), and it added no "
        "plate with a false alarm."
    )
    assert pooled_sentence(
        {**same, "qc_stats_alarm": 1, "either_stats_alarm": 3}, "the plates"
    ) == (
        "On the plates, pooling the overwatch with QC plus statistics added nothing: it caught no "
        "more true faults (11 of 12) and named no more faulty wells (20 of 30). It only added a "
        "false alarm on 2 more plates (1 to 3 of 10)."
    )
    more = {**same, "either_stats_hits": 22, "qc_stats_alarm": 0, "either_stats_alarm": 0}
    assert pooled_sentence(more, "the plates") == (
        "On the plates, pooling the overwatch with QC plus statistics caught no more true faults "
        "(11 of 12), named 2 more faulty wells (20 to 22 of 30), and added no plate with a false "
        "alarm."
    )


def test_the_command_is_registered_next_to_eval_reviewer():
    import argparse

    from labauto.cli_review import add_parsers

    parser = argparse.ArgumentParser()
    add_parsers(parser.add_subparsers())
    choices = list(parser._subparsers._group_actions[0].choices)
    assert choices.index("eval-reviewer-sweep") == choices.index("eval-reviewer") + 1
    sub = parser._subparsers._group_actions[0].choices["eval-reviewer-sweep"]
    dests = {a.dest for a in sub._actions}
    assert {"source", "prompt", "cases", "resume", "runs", "out", "client"} <= dests
    choices = next(a.choices for a in sub._actions if a.dest == "prompt")
    assert list(choices) == list(PROMPTS) == list(SWEEP_OUTS)


class FakeLive(LiveClient):
    """A live client in type only: it serves scripted replies and opens no connection."""

    def __init__(self, replies: list):
        super().__init__({})
        self.script = ScriptedClient(replies, model="some-model")

    def complete(self, label, messages, schema, temperature, max_tokens):
        return self.script.complete(label, messages, schema, temperature, max_tokens)


def test_a_live_run_needs_an_endpoint_and_may_write_its_default_page(
    tmp_path, source, monkeypatch, capsys
):
    import labauto.cli_plan as cli_plan
    import labauto.config as config

    monkeypatch.chdir(tmp_path)  # the default page then resolves inside tmp_path
    monkeypatch.setattr(config, "load_settings", lambda: Settings())
    args = ["eval-reviewer-sweep", str(source), "--runs", str(tmp_path / "runs")]
    assert main(args) == 1
    printed = capsys.readouterr().out
    assert "No model endpoint is configured" in printed
    assert "docs/reviewer_sweep.md was not written." in printed
    assert not (tmp_path / "runs").exists() and not (tmp_path / "docs").exists()

    live = FakeLive(REPLIES)
    monkeypatch.setattr(
        cli_plan, "make_clients", lambda spec, settings: cli_plan.Clients(live, None, spec)
    )
    assert main(args) == 0
    page = (tmp_path / "docs" / "reviewer_sweep.md").read_text()
    assert table(page) == FULL_ROWS and SCRIPTED_BANNER not in page
    assert "Overwatch model, as reported by the endpoint: some-model" in page
    assert "Generated by `labauto eval-reviewer-sweep " in page and "--client" not in page
