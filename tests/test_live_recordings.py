"""Recorded live model sessions: real, clean, and reproducible offline.

A live recording is any file in examples/recordings/ that is not a scripted one
(*.scripted.jsonl). These tests run in CI with no model and no network.
"""

from __future__ import annotations

import getpass
import json
import re
import socket
from pathlib import Path

import pytest
from conftest import ROOT

RECORDINGS = ROOT / "examples" / "recordings"
LIVE = sorted(p for p in RECORDINGS.glob("*.jsonl") if not p.name.endswith(".scripted.jsonl"))

# Things a committed recording must never hold.
FORBIDDEN = [
    (r"://", "a URL"),
    (r"(?<![\w.])/(home|Users|root|srv|opt|tmp|var|etc|usr|mnt)/", "an absolute path"),
    (r"\b127\.0\.0\.1\b|\blocalhost\b", "a local address"),
    (r"(?i)\bbearer\s+\S|\bsk-[A-Za-z0-9]{8,}|api[_-]?key\s*[:=]", "a key"),
]


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_there_are_live_recordings():
    assert LIVE, "no live recording in examples/recordings/"


@pytest.mark.parametrize("path", LIVE, ids=lambda p: p.name)
def test_a_live_recording_comes_from_a_real_model(path):
    models = {r.get("model") or "" for r in records(path)}
    assert models and not models & {"", "scripted", "unknown"}, models
    labels = {r.get("endpoint_label") for r in records(path)}
    assert labels <= {"local-main", "local-small", "hosted"}, labels


@pytest.mark.parametrize("path", LIVE, ids=lambda p: p.name)
def test_a_live_recording_holds_nothing_private(path):
    text = path.read_text(encoding="utf-8")
    for pattern, what in FORBIDDEN:
        match = re.search(pattern, text)
        assert match is None, f"{path.name} holds {what}: {match.group(0)!r}"
    # This machine's own names, read at test time so they are never written into the repo.
    for name in {socket.gethostname(), getpass.getuser()}:
        if name and len(name) >= 4:
            assert re.search(rf"\b{re.escape(name)}\b", text) is None, (
                f"{path.name} names this machine"
            )


def test_the_decider_recording_replays_the_live_decisions(tmp_path):
    """The demo's settings (seed 1, surface seed 0) reproduce what the model chose live."""
    from labauto.agents.llm import ReplayClient
    from labauto.campaign import load_cstate, start_campaign
    from labauto.models import CampaignConfig
    from labauto.orchestrator import Clients

    path = RECORDINGS / "decider.jsonl"
    if not path.exists():
        pytest.skip("no live decider recording")
    finals = {}
    for r in records(path):
        parsed = r.get("parsed") or {}
        if parsed.get("action") == "final":
            finals[r["scope"]] = parsed["picks"]
    cfg = CampaignConfig(
        campaign_id="replay-live",
        brief="Find the condition with the highest yield.",
        assay_id="generic_reaction_screen_v1",
        workcell_id="ot2_bench",
        strategy="model",
        rounds=3,
        approval_policy="auto_demo",
        seed=1,
        surface_seed=0,
        assay=str(ROOT / "configs" / "assay_reaction_screen.json"),
        workcell=str(ROOT / "configs" / "workcell_ot2.json"),
        client="replay:examples/recordings/decider.jsonl",
    )
    clients = Clients(local=ReplayClient(path), spec="replay:decider.jsonl")
    cdir = start_campaign(cfg, clients, None, tmp_path)
    assert load_cstate(cdir).state == "DONE"
    for rnd in (2, 3):
        decision = json.loads((cdir / "rounds" / f"{rnd:02d}" / "decision.json").read_text())
        assert not decision.get("fallback"), decision.get("fallback")
        picked = [p["conditions"] for p in decision["picks"]]
        recorded = [p["conditions"] for p in finals[f"decider-r{rnd:02d}"]]
        assert picked == recorded


SCENARIOS = sorted(
    p.name.removesuffix(".jsonl")
    for p in LIVE
    if p.name[:2].isdigit()  # planner scenarios are named 01_... to 05_...
)


def test_every_planner_scenario_has_a_live_recording():
    expected = sorted(p.stem for p in (ROOT / "examples" / "requests").glob("*.txt"))
    assert SCENARIOS == expected


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_a_planner_recording_replays_to_the_same_final_design(tmp_path, scenario):
    """Trial 1 of the live evaluation, replayed offline, ends the same way with the same design."""
    from labauto.agents.llm import ReplayClient
    from labauto.config import Settings
    from labauto.orchestrator import Clients, load_attempts, load_state, start_run

    path = RECORDINGS / f"{scenario}.jsonl"
    recs = records(path)
    request = (ROOT / "examples" / "requests" / f"{scenario}.txt").read_text()
    run = start_run(
        request,
        str(ROOT / "configs" / "assay_reaction_screen.json"),
        str(ROOT / "configs" / "workcell_ot2.json"),
        Clients(local=ReplayClient(path)),
        Settings(),
        tmp_path / "runs",
    )
    assert load_state(run).state == "AWAITING_APPROVAL"
    attempts = load_attempts(run)
    assert len(attempts) == len(recs)
    assert {a.model for a in attempts} == {recs[0]["model"]}
    final = json.loads((run / "final" / "design.json").read_text())
    recorded = recs[-1]["parsed"]["design"]
    for key in (
        "grid",
        "conditions",
        "replicates",
        "include_controls",
        "deviations",
        "assumptions",
    ):
        assert final[key] == recorded[key], key
