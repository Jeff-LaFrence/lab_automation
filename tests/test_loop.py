"""The closed loop: ground truth, the simulated LC/MS, analysis, deciders, envelope, campaign, report."""

from __future__ import annotations

import json

import pytest
from conftest import ROOT, make_design

from labauto import campaign as campaign_mod
from labauto.agents.llm import ScriptedClient
from labauto.analysis import analyze_plate
from labauto.campaign import (
    advance_campaign,
    approve_waiting_round,
    load_cstate,
    start_campaign,
    verify_round,
)
from labauto.decide.base import (
    build_history,
    grid_conditions,
    latin_hypercube,
    on_grid,
    recommendation,
)
from labauto.decide.model_decider import ModelDecider
from labauto.decide.optimizer import Optimizer, OptimizerDecider
from labauto.decide.random_decider import RandomDecider
from labauto.execute import ledger_from_plan, measure_and_analyze, result_rows, volumes_by_well
from labauto.expand import expand
from labauto.models import CampaignConfig, History
from labauto.report import NOTE_REJECTED, build_report, check_note_numbers
from labauto.virtual.ground_truth import grid_optimum, reference_expected, surface_for, true_yield
from labauto.virtual.lcms import LcmsSettings, read_plate

QUIET = LcmsSettings(
    yield_noise_sd=0, is_noise_sd=0, injection_failure_p=0, conversion_extra=(5, 5), rt_jitter_sd=0
)
ASSAY = str(ROOT / "configs" / "assay_reaction_screen.json")
OT2 = str(ROOT / "configs" / "workcell_ot2.json")

# ---------------------------------------------------------------- ground truth


def test_ground_truth_matches_the_published_numbers(assay):
    surface = surface_for(0)
    best, where = grid_optimum(assay, surface)
    assert round(best, 2) == 86.04
    assert where == {"catalyst_mol_pct": 6.5, "base_equiv": 1.75, "cosolvent_pct": 20.0}
    values = [true_yield(c, surface) for c in grid_conditions(assay)]
    assert len(values) == 1980
    assert sum(v >= best - 3 for v in values) == 13
    assert sum(v >= best - 5 for v in values) == 29
    ref = assay.controls["reference"].conditions
    assert round(true_yield(ref, surface), 2) == 50.61
    assert true_yield({"catalyst_mol_pct": 0, "base_equiv": 1.5, "cosolvent_pct": 20}, surface) == 0


def test_jittered_surface_stays_in_bounds_and_moves_the_reference(assay):
    s = surface_for(7)
    assert abs(s.cos_opt - 0.22) <= 0.08 and abs(s.base_intercept - 1.6) <= 0.4
    assert 0.7 * 2.0 <= s.cat_scale <= 1.5 * 2.0 and 0.9 * 92 <= s.ceiling <= 92
    assert surface_for(7) == s and surface_for(8) != s
    assert reference_expected(assay, 0) == 50
    assert reference_expected(assay, 7) == pytest.approx(
        true_yield(assay.controls["reference"].conditions, s)
    )


# ---------------------------------------------------------------- LC/MS and analysis


def plate(assay, good_design):
    plan = expand(good_design, assay)
    return plan, ledger_from_plan(plan)


def test_noise_free_analysis_recovers_the_true_yield(assay, good_design, tmp_path):
    plan, ledger = plate(assay, good_design)
    readings, results, qc = measure_and_analyze(
        plan,
        ledger,
        assay,
        surface_for(0),
        1,
        1,
        [],
        50,
        settings=QUIET,
        peaks_path=tmp_path / "peaks.csv",
    )
    truth = {r.well: r.true_yield for r in readings}
    for r in results:
        assert r.yield_pct == pytest.approx(truth[r.well], abs=1e-6)
    assert qc.plate_ok


def test_seeded_readings_repeat(assay, good_design):
    plan, ledger = plate(assay, good_design)
    a = read_plate(ledger, plan, assay, surface_for(0), seed=5)
    b = read_plate(ledger, plan, assay, surface_for(0), seed=5)
    c = read_plate(ledger, plan, assay, surface_for(0), seed=6)
    assert a == b and a != c


def test_injection_failure_is_excluded(assay, good_design):
    plan, ledger = plate(assay, good_design)
    readings = read_plate(ledger, plan, assay, surface_for(0), 1, settings=QUIET)
    peaks = {
        r.well: {
            "substrate": r.substrate_area,
            "product": r.product_area,
            "internal_standard": r.is_area,
        }
        for r in readings
    }
    peaks["E1"] = {
        k: v * 0.3 for k, v in peaks["E1"].items()
    }  # a short injection: every area smaller
    results, qc = analyze_plate(peaks, plan, volumes_by_well(ledger, plan.plate), assay, 50, 1)
    e1 = next(r for r in results if r.well == "E1")
    assert e1.excluded and e1.exclude_reason == "IS_OUT_OF_RANGE"
    assert qc.plate_ok and "E1" in qc.excluded_wells


def test_forced_injection_failures_hit_the_internal_standard(assay, good_design):
    plan, ledger = plate(assay, good_design)
    forced = QUIET.model_copy(update={"injection_failure_p": 1.0})
    readings = read_plate(
        ledger, plan, assay, surface_for(0), 1, faults=["injection_failure"], settings=forced
    )
    assert all(r.injection_failure and r.is_area <= 0.5e6 for r in readings)


def test_plate_drift_fails_the_reference_control(assay, good_design):
    plan, ledger = plate(assay, good_design)
    _, results, qc = measure_and_analyze(
        plan, ledger, assay, surface_for(0), 1, 2, ["plate_drift@2"], 50, settings=QUIET
    )
    assert not qc.plate_ok
    assert any(e.code == "PLATE_QC_FAIL" for e in qc.events)
    assert all(r.excluded and "PLATE_QC_FAIL" in r.exclude_reason for r in results)
    _, _, other_round = measure_and_analyze(
        plan, ledger, assay, surface_for(0), 1, 3, ["plate_drift@2"], 50, settings=QUIET
    )
    assert other_round.plate_ok


def test_replicate_spread_and_low_replicates_are_flagged(assay):
    design = make_design(replicates=2)
    plan = expand(design, assay)
    ledger = ledger_from_plan(plan)
    readings = read_plate(ledger, plan, assay, surface_for(0), 1, settings=QUIET)
    peaks = {
        r.well: {
            "substrate": r.substrate_area,
            "product": r.product_area,
            "internal_standard": r.is_area,
        }
        for r in readings
    }
    samples = [w for w in plan.wells if w.role == "sample" and w.condition_index == 0]
    peaks[samples[0].well]["product"] *= (
        3.0  # condition 0 yields about 15%, so this spreads it by 29
    )
    results, _ = analyze_plate(peaks, plan, volumes_by_well(ledger, plan.plate), assay, 50, 1)
    flagged = [r for r in results if r.condition_index == 0]
    assert all("REPLICATE_SPREAD" in r.flags for r in flagged)
    peaks[samples[0].well]["internal_standard"] *= 0.2
    results, _ = analyze_plate(peaks, plan, volumes_by_well(ledger, plan.plate), assay, 50, 1)
    assert all("LOW_REPLICATES" in r.flags for r in results if r.condition_index == 0)


def test_measured_yield_follows_what_was_dispensed(assay, good_design):
    plan, ledger = plate(assay, good_design)
    target = next(w for w in plan.wells if w.role == "sample" and w.add_ul["catalyst"] == 2)
    altered = [
        e.model_copy(update={"volume_ul": 16.0})
        if (e.dest_well, e.reagent) == (target.well, "catalyst")
        else e
        for e in ledger
    ]
    _, results, _ = measure_and_analyze(
        plan, altered, assay, surface_for(0), 1, 1, [], 50, settings=QUIET
    )
    r = next(x for x in results if x.well == target.well)
    as_run = dict(target.conditions, catalyst_mol_pct=8.0)
    assert r.as_run["catalyst_mol_pct"] == 8.0 and r.designed["catalyst_mol_pct"] == 1.0
    assert r.yield_pct == pytest.approx(true_yield(as_run, surface_for(0)), abs=1e-6)
    assert r.yield_pct != pytest.approx(true_yield(target.conditions, surface_for(0)), abs=1)


# ---------------------------------------------------------------- deciders


def test_latin_hypercube_is_shared_and_on_the_grid(assay):
    a = latin_hypercube(assay, 8, 1)
    assert a == latin_hypercube(assay, 8, 1) != latin_hypercube(assay, 8, 2)
    assert len({json.dumps(c, sort_keys=True) for c in a}) == 8
    assert all(on_grid(c, assay) for c in a)
    for f in assay.factors:
        assert len({c[f] for c in a}) >= 5  # spread over each factor's range


@pytest.mark.slow
def test_optimizer_finds_a_near_optimal_condition_in_six_rounds(assay):
    from labauto.benchmark import decider_for, fast_campaign

    scores = fast_campaign(
        assay, decider_for("optimizer", assay), "optimizer", seed=1, surface_seed=0
    )
    assert scores[-1] >= grid_optimum(assay, surface_for(0))[0] - 3


def history_after_round_one(assay):
    from labauto.decide.optimizer import round_one

    decision = round_one(assay, 8, 1, "test")
    design = make_design(grid=None, conditions=[p.conditions for p in decision.picks], replicates=3)
    plan = expand(design, assay)
    _, results, _ = measure_and_analyze(
        plan, ledger_from_plan(plan), assay, surface_for(0), 1, 1, [], 50, settings=QUIET
    )
    rows = result_rows(
        results,
        assay,
        campaign_id="t",
        round_number=1,
        attempt=1,
        run_id="r",
        design_hash="",
        protocol_hash="",
        decided_by="lhs",
        timestamp="",
    )
    return build_history(rows, assay, 2)


def test_random_and_optimizer_pick_untested_grid_conditions(assay):
    history = history_after_round_one(assay)
    tested = {json.dumps(s.conditions, sort_keys=True) for s in history.stats}
    for decider in (RandomDecider(assay), OptimizerDecider(assay)):
        d = decider.decide(history, 8, "", 1)
        keys = {json.dumps(p.conditions, sort_keys=True) for p in d.picks}
        assert len(keys) == 8 and not keys & tested
        assert all(on_grid(p.conditions, assay) for p in d.picks)


def test_optimizer_tools(assay):
    opt = Optimizer(assay, history_after_round_one(assay), 1)
    assert len(opt.suggest(20)) == 16
    assert (
        len(opt.predict([{"catalyst_mol_pct": 6.5, "base_equiv": 1.75, "cosolvent_pct": 20}] * 12))
        == 10
    )
    top = opt.results(top_k=3)
    assert len(top) == 3 and top[0]["mean"] >= top[1]["mean"]


def final(picks, note="ok"):
    return {"action": "final", "picks": picks, "round_note": note}


def own_picks(assay, history, n=8, start=0):
    tested = {json.dumps(s.conditions, sort_keys=True) for s in history.stats}
    pool = [c for c in grid_conditions(assay) if json.dumps(c, sort_keys=True) not in tested]
    return [
        {"conditions": c, "source": "own", "reason": "spread the search"}
        for c in pool[start : start + n]
    ]


def test_model_decider_tool_call_then_final(assay):
    history = history_after_round_one(assay)
    client = ScriptedClient(
        [{"action": "tool", "tool": "suggest", "args": {"n": 8}}, final(own_picks(assay, history))]
    )
    d = ModelDecider(assay, client).decide(history, 8, "brief", 1)
    assert d.fallback is None and len(d.picks) == 8
    assert [t.tool for t in d.tool_calls] == ["suggest"]
    assert "Result of suggest" in client.calls[1][1][-1]["content"]


def test_model_decider_invalid_then_valid(assay):
    history = history_after_round_one(assay)
    too_few = own_picks(assay, history, n=5)
    client = ScriptedClient([final(too_few), final(own_picks(assay, history))])
    d = ModelDecider(assay, client).decide(history, 8, "", 1)
    assert d.fallback is None
    assert "Give exactly 8 picks" in client.calls[1][1][-1]["content"]


def test_model_decider_falls_back_after_repeated_invalid_answers(assay):
    history = history_after_round_one(assay)
    bad = own_picks(assay, history)
    bad[0] = {
        "conditions": {"catalyst_mol_pct": 2.2, "base_equiv": 1.0, "cosolvent_pct": 20},
        "source": "own",
        "reason": "x",
    }
    long_reason = own_picks(assay, history)
    long_reason[0]["reason"] = " ".join(["word"] * 25)
    bad_source = own_picks(assay, history)
    bad_source[0]["source"] = "gut"
    client = ScriptedClient([final(bad), final(long_reason), final(bad_source)])
    d = ModelDecider(assay, client).decide(history, 8, "", 1)
    assert d.fallback and "after 2 retries" in d.fallback
    assert all(p.source == "optimizer" for p in d.picks) and len(d.picks) == 8


def test_model_decider_repeat_limit(assay):
    history = history_after_round_one(assay)
    picks = own_picks(assay, history, n=5) + [
        {"conditions": s.conditions, "source": "confirm", "reason": "repeat"}
        for s in history.stats[:3]
    ]
    client = ScriptedClient([final(picks), final(own_picks(assay, history))])
    ModelDecider(assay, client).decide(history, 8, "", 1)
    assert "3 picks repeat conditions already tested" in client.calls[1][1][-1]["content"]


def test_model_decider_tool_call_cap(assay):
    history = history_after_round_one(assay)
    tool = {"action": "tool", "tool": "results", "args": {"top_k": 3}}
    client = ScriptedClient([tool] * 7 + [final(own_picks(assay, history))])
    d = ModelDecider(assay, client).decide(history, 8, "", 1)
    assert len(d.tool_calls) == 6
    assert "used all 6 tool calls" in client.calls[7][1][-1]["content"]
    assert d.fallback is None


# ---------------------------------------------------------------- campaign, envelope, resume


def config(tmp_path, **kw):
    base = dict(
        campaign_id=kw.pop("campaign_id", "c-test"),
        brief="find the best yield",
        assay_id="generic_reaction_screen_v1",
        workcell_id="ot2_bench",
        strategy="optimizer",
        rounds=3,
        approval_policy="auto_demo",
        seed=1,
        surface_seed=0,
        assay=ASSAY,
        workcell=OT2,
    )
    base.update(kw)
    return CampaignConfig(**base)


def test_three_rounds_end_to_end_with_auto_demo(tmp_path):
    cdir = start_campaign(config(tmp_path), base=tmp_path)
    st = load_cstate(cdir)
    assert st.state == "DONE"
    for r in (1, 2, 3):
        rdir = cdir / "rounds" / f"{r:02d}"
        for name in (
            "decision.json",
            "protocol.py",
            "sim_log.txt",
            "ledger.csv",
            "results.csv",
            "qc.json",
            "approval.json",
            "summary.md",
        ):
            assert (rdir / name).exists(), name
        assert (rdir / "raw" / "lcms_peaks.csv").exists()
        assert json.loads((rdir / "approval.json").read_text())["mode"] == "demo_auto"
        assert json.loads((rdir / "check.json").read_text())["layer_status"]["ledger"] == "pass"
    rows = (cdir / "results_all.csv").read_text().splitlines()
    assert len(rows) == 1 + 3 * 28
    assert (cdir / "report.md").exists() and (cdir / "progress.png").exists()
    assert "demo_auto" in (cdir / "rounds" / "02" / "summary.md").read_text()


class Crash(BaseException):
    pass


def test_resume_in_the_middle_of_a_campaign(tmp_path, monkeypatch):
    whole = start_campaign(config(tmp_path, campaign_id="c-whole"), base=tmp_path / "a")
    real = campaign_mod.step
    hit = {"n": 0}

    def crashing(cdir, st, round_state, reason, state="RUNNING"):
        real(cdir, st, round_state, reason, state)
        if st.round == 2 and round_state == "ANALYZING" and not hit["n"]:
            hit["n"] = 1
            raise Crash()

    monkeypatch.setattr(campaign_mod, "step", crashing)
    with pytest.raises(Crash):
        start_campaign(config(tmp_path, campaign_id="c-whole"), base=tmp_path / "b")
    cdir = tmp_path / "b" / "c-whole"
    assert load_cstate(cdir).round_state == "ANALYZING"
    monkeypatch.setattr(campaign_mod, "step", real)
    assert advance_campaign(cdir).state == "DONE"

    def numbers(d):
        return [line.rsplit(",", 1)[0] for line in (d / "results_all.csv").read_text().splitlines()]

    assert numbers(cdir) == numbers(whole)


def test_envelope_approves_a_normal_round_then_stops_after_an_anomaly(tmp_path):
    cdir = start_campaign(
        config(tmp_path, approval_policy="envelope", rounds=4, faults=["plate_drift@2"]),
        base=tmp_path,
    )
    st = load_cstate(cdir)
    assert st.state == "AWAITING_APPROVAL" and st.round == 1
    approve_waiting_round(cdir, by="tester")
    env = json.loads((cdir / "envelope.json").read_text())
    assert (
        env["wells_approved"] == 28
        and env["approved_by"] == "tester"
        and env["max_auto_rounds"] == 5
    )
    st = advance_campaign(cdir)
    r2 = json.loads((cdir / "rounds" / "02" / "approval.json").read_text())
    assert r2["mode"] == "envelope" and "rounds/01/approval.json" in r2["envelope_ref"]
    assert st.state == "AWAITING_APPROVAL" and st.round == 3
    assert "anomaly" in st.history[-1]["reason"] and "PLATE_QC_FAIL" in st.history[-1]["reason"]


def test_envelope_stops_when_its_rounds_are_used(tmp_path):
    cdir = start_campaign(config(tmp_path, approval_policy="envelope", rounds=4), base=tmp_path)
    approve_waiting_round(cdir)
    env = json.loads((cdir / "envelope.json").read_text())
    env["max_auto_rounds"] = 1
    (cdir / "envelope.json").write_text(json.dumps(env))
    st = advance_campaign(cdir)
    assert st.state == "AWAITING_APPROVAL" and st.round == 3
    assert "used up" in st.history[-1]["reason"]


def test_always_waits_for_a_person_every_round(tmp_path):
    cdir = start_campaign(config(tmp_path, approval_policy="always", rounds=2), base=tmp_path)
    for r in (1, 2):
        st = load_cstate(cdir)
        assert st.state == "AWAITING_APPROVAL" and st.round == r
        approve_waiting_round(cdir, by="tester")
        st = advance_campaign(cdir)
    assert st.state == "DONE"


def test_changed_protocol_voids_a_round_approval(tmp_path):
    cdir = start_campaign(config(tmp_path, approval_policy="always", rounds=1), base=tmp_path)
    approve_waiting_round(cdir)
    protocol = cdir / "rounds" / "01" / "protocol.py"
    protocol.write_text(protocol.read_text() + "# edited\n")
    assert not verify_round(cdir, 1)
    st = advance_campaign(cdir)
    assert st.state == "AWAITING_APPROVAL"


def test_model_strategy_with_scripted_client(tmp_path, assay):
    history = history_after_round_one(assay)
    picks = own_picks(assay, history)
    client = ScriptedClient([{"action": "tool", "tool": "suggest", "args": {"n": 8}}, final(picks)])
    from labauto.orchestrator import Clients

    cdir = start_campaign(
        config(tmp_path, strategy="model", rounds=2), Clients(local=client), base=tmp_path
    )
    assert load_cstate(cdir).state == "DONE"
    decision = json.loads((cdir / "rounds" / "02" / "decision.json").read_text())
    assert (
        decision["strategy"] == "model"
        and decision["model"] == "scripted"
        and not decision["fallback"]
    )
    assert (cdir / "rounds" / "02" / "transcript.jsonl").exists()


# ---------------------------------------------------------------- report


def test_note_number_check():
    tables = (
        "| 1 | 73.3 | none |\n| 2 | 87.4 | IS_OUT_OF_RANGE |\n| catalyst 6.5 | 86.6 | 2.2 | 3 |"
    )
    assert (
        check_note_numbers("Round 2 reached 87.4% at 6.5 mol % catalyst, from 3 wells.", tables)
        == []
    )
    assert check_note_numbers("The best mean was 91.2%.", tables) == ["91.2"]
    assert check_note_numbers("About 87% after round 2.", tables) == []


def test_report_rejects_a_note_with_an_invented_number(tmp_path):
    cdir = start_campaign(config(tmp_path, rounds=2), base=tmp_path)
    text = build_report(
        cdir, note_writer=lambda tables: "The campaign reached 99.9% yield."
    ).read_text()
    assert NOTE_REJECTED in text
    good = build_report(cdir, note_writer=lambda tables: "Two rounds ran.").read_text()
    assert "Two rounds ran." in good and "Written by a model" in good


def test_recommendation_is_the_highest_measured_mean(assay):
    history = history_after_round_one(assay)
    rec = recommendation(history)
    assert rec["mean"] == max(s.mean for s in history.stats if s.mean is not None)
    assert isinstance(History(next_round=1), History)


def test_benchmark_outputs(tmp_path, assay):
    from labauto.benchmark import run_benchmark, write_outputs

    result = run_benchmark(assay, ["random"], seeds=3, rounds=2)
    write_outputs(result, 2, "test", tmp_path / "b.md", tmp_path / "b.png", None, "abc")
    text = (tmp_path / "b.md").read_text()
    assert "| random | 3 |" in text and "fast path" in text
    assert (tmp_path / "b.png").stat().st_size > 1000
