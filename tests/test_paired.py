"""Paired LC/MS and NMR readout: agreement rules, the evidence pack, fault cases, the nmr command."""

from __future__ import annotations

import hashlib
import json
import math
import typing
from types import SimpleNamespace

import pytest
from conftest import ROOT, make_design

from labauto.analysis import WellResult, analyze_plate
from labauto.cli import main
from labauto.execute import ledger_from_plan, volumes_by_well
from labauto.expand import expand
from labauto.models import CampaignConfig, QcReport
from labauto.paired import (
    FAULT_CASES,
    Evidence,
    FaultEvent,
    Readout,
    build_case,
    compare,
    fault_plan,
    faults_on_round,
    lcms_condition_stats,
    pair_round,
    r1,
    r2,
)
from labauto.review import qc_only_review, score
from labauto.virtual.ground_truth import surface_for
from labauto.virtual.lcms import read_plate

ASSAY = str(ROOT / "configs" / "assay_reaction_screen.json")
OT2 = str(ROOT / "configs" / "workcell_ot2.json")
TRUTH_WORDS = ("true_yield", "observed_yield", "impurity", "truth", "injection", "drift", "fault")
FAULT_CASES_BY_ID = {c["id"]: c for c in FAULT_CASES}


def needs_evaluator():
    """Skip when the spectrum evaluator (nmr_analysis.py, built alongside) is missing."""
    return pytest.importorskip(
        "labauto.nmr_analysis", reason="labauto.nmr_analysis (the spectrum evaluator) is missing"
    )


def all_keys(obj) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in all_keys(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in all_keys(v)}
    return set()


def assert_no_truth(evidence_text: str) -> None:
    data = json.loads(evidence_text)
    keys = {k.lower() for k in all_keys(data)}
    lowered = evidence_text.lower()
    for word in TRUTH_WORDS:
        assert not any(word in k for k in keys), word
        assert word not in lowered, word
    assert data["synthetic"] is True


# ---------------------------------------------------------------- agreement rule


def test_close_means_agree():
    diff, se, word = compare(Readout(mean=50.0, sd=2.0, n=3), Readout(mean=52.0, sd=2.0, n=3))
    assert (diff, se, word) == (-2.0, 1.63, "agree")


def test_far_means_with_tight_replicates_disagree():
    diff, se, word = compare(Readout(mean=60.0, sd=1.0, n=3), Readout(mean=50.0, sd=1.0, n=3))
    assert (diff, word) == (10.0, "disagree")
    assert se == pytest.approx(0.82)


def test_noisy_replicates_widen_the_limit():
    # 10 points apart, but the combined standard error is 8.16, so the limit is 16.3.
    _, se, word = compare(Readout(mean=60.0, sd=10.0, n=3), Readout(mean=50.0, sd=10.0, n=3))
    assert se == pytest.approx(8.16)
    assert word == "agree"


def test_five_point_floor_is_not_a_disagreement():
    assert compare(Readout(mean=55.0, sd=0.5, n=3), Readout(mean=50.0, sd=0.5, n=3))[2] == "agree"
    assert (
        compare(Readout(mean=55.1, sd=0.5, n=3), Readout(mean=50.0, sd=0.5, n=3))[2] == "disagree"
    )


def test_rounding_never_gives_negative_zero():
    for value in (r1(-0.04), r2(-0.004), compare(Readout(mean=50.0), Readout(mean=50.0))[0]):
        assert value == 0.0 and math.copysign(1.0, value) == 1.0
        assert json.dumps(value) == "0.0"
    assert r1(-0.06) == -0.1 and r2(None) is None


def test_fewer_than_two_usable_wells_is_insufficient():
    diff, se, word = compare(Readout(mean=80.0, n=1), Readout(mean=40.0, sd=1.0, n=3))
    assert diff == 40.0 and se is None and word == "insufficient"
    assert compare(Readout(n=0), Readout(mean=40.0, sd=1.0, n=3))[2] == "insufficient"


# ---------------------------------------------------------------- pairing on hand-made inputs


def small_plan(assay):
    """Three conditions in duplicate plus the four control wells, in column order."""
    grid = {"catalyst_mol_pct": [1, 2, 4], "base_equiv": [1.0], "cosolvent_pct": [20]}
    return expand(make_design(grid=grid), assay)


def plate_yields(plan, by_condition, negative=1.0, reference=50.0):
    """A yield for every well: controls fixed, samples from {condition index: [rep1, rep2]}."""
    out = {}
    for w in plan.wells:
        if w.role == "negative_control":
            out[w.well] = negative
        elif w.role == "reference_control":
            out[w.well] = reference
        else:
            out[w.well] = by_condition[w.condition_index][w.replicate - 1]
    return out


def hand_made(plan, lcms_yields, nmr_yields, *, excluded=(), nmr_flags=None, plate_ok=True):
    """LC/MS well results and NMR fits with chosen yields for every well of the plan."""
    lcms, fits = [], []
    for w in plan.wells:
        bad = w.well in excluded
        flags = (["IS_OUT_OF_RANGE"] if bad else []) + ([] if plate_ok else ["PLATE_QC_FAIL"])
        lcms.append(
            WellResult(
                well=w.well,
                role=w.role,
                condition_index=w.condition_index,
                replicate=w.replicate,
                designed=w.conditions,
                as_run=w.conditions or {},
                volumes=w.add_ul,
                substrate_area=1.0,
                product_area=1.0,
                is_area=300_000.0 if bad else 1_000_000.0,
                yield_pct=lcms_yields[w.well],
                conversion_pct=0.0,
                flags=flags,
                excluded=bad or not plate_ok,
                exclude_reason="; ".join(flags),
            )
        )
        y = nmr_yields[w.well]
        fits.append(
            SimpleNamespace(
                well=w.well,
                yield_pct=y,
                naive_yield_pct=y,
                yield_from_doublet=y,
                yield_from_singlet=y,
                rmse_over_noise=1.0,
                is_snr=300.0,
                linewidth_hz=1.2,
                phase_deg=0.5,
                excluded=False,
                flags=list((nmr_flags or {}).get(w.well, [])),
            )
        )
    return lcms, fits, QcReport(round=1, plate_ok=plate_ok, is_median=1_000_000.0)


def test_pair_round_marks_agree_disagree_and_insufficient(assay):
    plan = small_plan(assay)
    lcms_y = plate_yields(plan, {0: [30.0, 32.0], 1: [60.0, 61.0], 2: [45.0, 47.0]})
    nmr_y = plate_yields(plan, {0: [31.0, 31.5], 1: [48.0, 49.0], 2: [46.0, 46.0]})
    lost = next(w.well for w in plan.wells if w.condition_index == 2 and w.replicate == 1)
    lcms, fits, qc = hand_made(plan, lcms_y, nmr_y, excluded={lost})
    ev = pair_round(lcms, None, fits, None, plan, assay, 1, campaign_id="c-x", lcms_qc=qc)

    by_key = {c.key: c for c in ev.conditions}
    assert by_key["c01"].agreement == "agree" and by_key["c01"].flags == []
    assert by_key["c02"].agreement == "disagree"
    assert by_key["c02"].flags == ["LCMS_NMR_DISAGREE"]
    assert by_key["c02"].difference == pytest.approx(12.0)
    assert by_key["c03"].agreement == "insufficient"
    assert by_key["c03"].lcms.n == 1 and "LOW_REPLICATES" in by_key["c03"].lcms.flags
    assert by_key["c03"].nmr.n == 2

    wells = {w.well: w for w in ev.wells}
    assert wells[lost].lcms.excluded and wells[lost].lcms.is_ratio == pytest.approx(0.3)
    assert wells["A1"].key == "negative" and wells["C1"].key == "reference"
    assert ev.plate.controls.reference.expected == 50.0
    assert ev.plate.lcms_flags == [] and ev.plate.nmr_flags == []
    assert ev.plate.lcms_stats_note is None
    assert ev.campaign_id == "c-x" and ev.synthetic is True
    assert_no_truth(ev.model_dump_json())


def test_evaluator_condition_numbers_are_used_when_given(assay):
    plan = small_plan(assay)
    y = plate_yields(plan, {0: [30.0, 31.0], 1: [40.0, 41.0], 2: [50.0, 51.0]})
    lcms, fits, qc = hand_made(plan, y, y)
    given = [{"condition_index": 1, "mean": 20.0, "sd": 1.0, "n": 2, "flags": []}]
    ev = pair_round(lcms, None, fits, given, plan, assay, 1, lcms_qc=qc)
    assert ev.conditions[1].nmr.mean == 20.0 and ev.conditions[1].agreement == "disagree"
    assert ev.conditions[0].nmr.mean == 30.5  # not given, so computed from the fits


def test_plate_failure_keeps_lcms_means_visible(assay):
    plan = small_plan(assay)
    y = plate_yields(plan, {0: [30.0, 31.0], 1: [40.0, 41.0], 2: [50.0, 51.0]}, reference=30.0)
    lcms, fits, qc = hand_made(plan, y, y, plate_ok=False)
    ev = pair_round(lcms, None, fits, None, plan, assay, 1, lcms_qc=qc)
    assert ev.plate.lcms_flags == ["PLATE_QC_FAIL"]
    # The NMR reads the same low reference control, so its own control check fails too.
    assert "PLATE_QC_FAIL" in ev.plate.nmr_flags
    assert all(c.lcms.n == 2 and c.agreement == "agree" for c in ev.conditions)
    assert all(w.lcms.excluded for w in ev.wells)
    # The pack says why the condition counts include wells that every row shows as excluded.
    note = ev.plate.lcms_stats_note
    assert note and "PLATE_QC_FAIL" in note and "excluded" in note
    assert_no_truth(ev.model_dump_json())


def test_a_flag_on_most_nmr_wells_becomes_a_plate_flag(assay):
    plan = small_plan(assay)
    y = plate_yields(plan, {0: [30.0, 31.0], 1: [40.0, 41.0], 2: [50.0, 51.0]})
    wells = [w.well for w in plan.wells]
    most = {w: ["NMR_BROAD_LINES"] for w in wells[: len(wells) // 2 + 1]}
    few = {w: ["NMR_BROAD_LINES", "NMR_SIGNAL_INCONSISTENT"] for w in wells[:2]}
    lcms, fits, qc = hand_made(plan, y, y, nmr_flags={**most, **few})
    ev = pair_round(lcms, None, fits, None, plan, assay, 1, lcms_qc=qc)
    assert "NMR_BROAD_LINES" in ev.plate.nmr_flags
    assert "NMR_SIGNAL_INCONSISTENT" not in ev.plate.nmr_flags


def test_lcms_condition_flags_match_analysis(assay, good_design):
    plan = expand(good_design, assay)
    ledger = ledger_from_plan(plan)
    surface = surface_for(0)
    readings = read_plate(ledger, plan, assay, surface, 11, 1, ["injection_failure"])
    peaks = {
        r.well: {
            "substrate": r.substrate_area,
            "product": r.product_area,
            "internal_standard": r.is_area,
        }
        for r in readings
    }
    results, _ = analyze_plate(peaks, plan, volumes_by_well(ledger, "plate"), assay, 50.0, 1)
    stats = lcms_condition_stats(results, assay)
    codes = {"REPLICATE_SPREAD", "LOW_REPLICATES"}
    for w in results:
        if w.role == "sample":
            assert set(stats[w.condition_index].flags) == set(w.flags) & codes


# ---------------------------------------------------------------- fault cases


@pytest.fixture(scope="module")
def built_cases(tmp_path_factory):
    needs_evaluator()
    base = tmp_path_factory.mktemp("fault_cases")
    return {
        case["id"]: (base / case["id"], *build_case(case, base / case["id"]))
        for case in FAULT_CASES
    }


HARD_CASES = ("mild_drift", "subtle_impurity", "combined_hard")


def test_fault_cases_cover_the_contract():
    ids = {c["id"] for c in FAULT_CASES}
    for needed in (
        "clean_a",
        "clean_b",
        "injection_failure",
        "plate_drift",
        "nmr_broad_lines",
        "nmr_phase_error",
        "nmr_impurity",
        *HARD_CASES,
    ):
        assert needed in ids
    assert any(c["lcms_faults"] and c["nmr_faults"] for c in FAULT_CASES)
    assert len({c["seed"] for c in FAULT_CASES}) == len(FAULT_CASES)
    assert len(ids) == len(FAULT_CASES)
    hard = {c["id"] for c in FAULT_CASES if c.get("hard")}
    assert hard == set(HARD_CASES)
    for c in FAULT_CASES:
        assert set(c) <= {
            "id",
            "lcms_faults",
            "nmr_faults",
            "seed",
            "lcms_settings",
            "nmr_settings",
            "hard",
            "note",
        }, c["id"]
        json.dumps(c)  # cases are plain data, so the evaluation can write them out


def test_fault_plate_is_fixed(assay):
    plan = fault_plan(assay)
    samples = [w for w in plan.wells if w.role == "sample"]
    assert len({w.condition_index for w in samples}) == 22
    assert len(samples) == 66 and len(plan.wells) == 70


def test_every_fault_case_builds_with_its_true_fault_log(built_cases):
    for case in FAULT_CASES:
        out, evidence, events = built_cases[case["id"]]
        wanted = {f.partition("@")[0] for f in case["lcms_faults"] + case["nmr_faults"]}
        assert {e.kind for e in events} == wanted, case["id"]
        if case["id"].startswith("clean"):
            assert events == []
        else:
            assert events
        for e in events:
            if e.kind in ("injection_failure", "nmr_impurity"):
                assert e.wells, e.kind
            else:
                assert e.wells == []
        for name in (
            "plan.json",
            "raw/lcms_peaks.csv",
            "raw/lcms_run.json",
            "nmr/spectra.npz",
            "nmr/truth.json",
            "nmr/nmr_fits.csv",
            "nmr/nmr_run.json",
            "review/evidence.json",
            "paired.md",
            "fault_log.json",
        ):
            assert (out / name).exists(), (case["id"], name)
        assert len(evidence.conditions) == 22 and len(evidence.wells) == 70
        # paired.md, for people, names the faults switched on. The evidence pack does not.
        md = (out / "paired.md").read_text()
        assert "## Simulated faults" in md
        lcms_line = next(x for x in md.splitlines() if x.startswith("- LC/MS: "))
        nmr_line = next(x for x in md.splitlines() if x.startswith("- NMR: "))
        assert lcms_line.startswith(f"- LC/MS: {', '.join(case['lcms_faults']) or 'none'}")
        assert nmr_line.startswith(f"- NMR: {', '.join(case['nmr_faults']) or 'none'}")
        assert ("Settings changed" in lcms_line) == bool(case.get("lcms_settings"))
        assert ("Settings changed" in nmr_line) == bool(case.get("nmr_settings"))


def test_fault_log_comes_from_the_simulators(built_cases):
    out, _, events = built_cases["nmr_impurity"]
    truth = json.loads((out / "nmr" / "truth.json").read_text())["wells"]
    impure = [e for e in events if e.kind == "nmr_impurity"][0].wells
    assert sorted(impure) == sorted(w for w, t in truth.items() if t["impurity"])
    out, _, events = built_cases["plate_drift"]
    assert [e.kind for e in events] == ["plate_drift"]
    assert json.loads((out / "qc.json").read_text())["plate_ok"] is False


def test_run_files_record_the_settings_used(built_cases):
    for case in FAULT_CASES:
        out = built_cases[case["id"]][0]
        lcms_run = json.loads((out / "raw" / "lcms_run.json").read_text())
        nmr_run = json.loads((out / "nmr" / "nmr_run.json").read_text())
        assert lcms_run["synthetic"] is True and nmr_run["synthetic"] is True
        assert lcms_run["faults"] == case["lcms_faults"]
        assert nmr_run["faults"] == case["nmr_faults"]
        assert lcms_run["settings_changed"] == case.get("lcms_settings", {})
        assert nmr_run["settings_changed"] == case.get("nmr_settings", {})
        for key, value in case.get("lcms_settings", {}).items():
            assert lcms_run["settings"][key] == value
        for key, value in case.get("nmr_settings", {}).items():
            assert nmr_run["settings"][key] == value
        evidence_text = (out / "review" / "evidence.json").read_text()
        assert "settings" not in evidence_text and "drift_factor" not in evidence_text


def test_mild_drift_passes_plate_qc_but_reads_low(built_cases):
    for case_id in ("mild_drift", "combined_hard"):
        out, ev, events = built_cases[case_id]
        assert "plate_drift" in {e.kind for e in events}
        assert json.loads((out / "qc.json").read_text())["plate_ok"] is True
        assert "PLATE_QC_FAIL" not in ev.plate.lcms_flags + ev.plate.nmr_flags
        assert ev.plate.lcms_stats_note is None
        ref = ev.plate.controls.reference
        assert abs(ref.lcms_mean - ref.expected) <= ref.tolerance
        lower = [c.key for c in ev.conditions if c.difference is not None and c.difference < 0]
        assert len(lower) > len(ev.conditions) / 2, case_id


def test_subtle_impurity_slips_past_the_signal_check(built_cases):
    for case_id in ("subtle_impurity", "combined_hard"):
        out, ev, events = built_cases[case_id]
        impure = next(e.wells for e in events if e.kind == "nmr_impurity")
        rows = {w.well: w for w in ev.wells}
        quiet = [w for w in impure if "NMR_SIGNAL_INCONSISTENT" not in rows[w].nmr.flags]
        assert quiet, case_id
        truth = json.loads((out / "nmr" / "truth.json").read_text())["wells"]
        low, high = FAULT_CASES_BY_ID[case_id]["nmr_settings"]["impurity_amount"]
        assert all(low <= truth[w]["impurity_amount"] <= high for w in impure)


def test_the_qc_rules_miss_a_fault_in_every_hard_case(built_cases):
    """The margin a reviewer can win on: each hard case holds a fault the QC rules miss.

    In combined_hard the QC rules miss both faults, and no impure well carries a flag.
    """
    for case_id in HARD_CASES:
        _, evidence, events = built_cases[case_id]
        truth = [e.model_dump() for e in events]
        assert score(truth, qc_only_review(evidence).suspected_faults)["missed"], case_id
    _, evidence, events = built_cases["combined_hard"]
    truth = [e.model_dump() for e in events]
    s = score(truth, qc_only_review(evidence).suspected_faults)
    assert s["caught"] == []
    assert sorted(f["kind"] for f in s["missed"]) == ["nmr_impurity", "plate_drift"]
    for case_id in ("subtle_impurity", "combined_hard"):
        _, evidence, events = built_cases[case_id]
        impure = next(e.wells for e in events if e.kind == "nmr_impurity")
        rows = {w.well: w for w in evidence.wells}
        flagged = [w for w in impure if "NMR_SIGNAL_INCONSISTENT" in rows[w].nmr.flags]
        assert impure and flagged == [], case_id


def test_evidence_files_hold_no_truth(built_cases):
    for case_id, (out, evidence, _) in built_cases.items():
        text = (out / "review" / "evidence.json").read_text()
        assert_no_truth(text)
        assert Evidence.model_validate_json(text) == evidence, case_id


@pytest.mark.parametrize("case_id", ["combined_wells", "combined_hard"])
def test_fault_cases_are_deterministic_by_seed(built_cases, tmp_path, case_id):
    case = FAULT_CASES_BY_ID[case_id]
    out, evidence, events = built_cases[case_id]
    evidence2, events2 = build_case(case, tmp_path / case_id)
    assert evidence2 == evidence and events2 == events
    for name in (
        "review/evidence.json",
        "nmr/nmr_fits.csv",
        "nmr/nmr_run.json",
        "raw/lcms_peaks.csv",
        "raw/lcms_run.json",
        "fault_log.json",
        "paired.md",
    ):
        assert (tmp_path / case_id / name).read_bytes() == (out / name).read_bytes(), name
    # clean_a and clean_b differ only in their seed.
    assert built_cases["clean_a"][1] != built_cases["clean_b"][1]


def test_faults_on_round_keeps_untagged_and_this_round():
    faults = ["injection_failure", "plate_drift@1", "plate_drift@3", "nmr_impurity@1"]
    assert faults_on_round(faults, 1) == ["injection_failure", "plate_drift@1", "nmr_impurity@1"]
    assert faults_on_round(faults, 3) == ["injection_failure", "plate_drift@3"]
    assert faults_on_round(None, 1) == []


# ---------------------------------------------------------------- copied constants


def test_copied_constants_stay_in_sync():
    from labauto import cli_paired, review
    from labauto.virtual import nmr as virtual_nmr

    assert cli_paired.NMR_FAULTS == virtual_nmr.NMR_FAULTS
    assert set(review.FAULT_KINDS) == set(
        typing.get_args(FaultEvent.model_fields["kind"].annotation)
    )
    nmr_analysis = needs_evaluator()
    from labauto import paired

    assert paired.EVALUATOR_NAME == nmr_analysis.LineshapeFitter.name


# ---------------------------------------------------------------- the nmr command


def test_parse_nmr_faults_keeps_or_adds_the_round_tag():
    from labauto.cli_paired import RoundError, parse_nmr_faults

    assert parse_nmr_faults(["nmr_impurity@2"], 2) == ["nmr_impurity@2"]
    assert parse_nmr_faults(["nmr_impurity", "nmr_broad_lines@2", "nmr_broad_lines"], 2) == [
        "nmr_impurity@2",
        "nmr_broad_lines@2",
    ]
    for bad in ("nmr_noise", "nmr_impurity@", "nmr_impurity@x", "plate_drift@2"):
        with pytest.raises(RoundError, match="unknown NMR fault"):
            parse_nmr_faults([bad], 2)
    with pytest.raises(RoundError, match="names round 1, but this command reads round 2"):
        parse_nmr_faults(["nmr_impurity@1"], 2)


def sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def small_campaign(tmp_path_factory):
    from labauto.campaign import load_cstate, start_campaign

    base = tmp_path_factory.mktemp("nmr_campaign")
    cfg = CampaignConfig(
        campaign_id="c-nmr",
        assay_id="generic_reaction_screen_v1",
        workcell_id="ot2_bench",
        strategy="random",
        rounds=2,
        conditions_per_round=4,
        approval_policy="auto_demo",
        seed=3,
        assay=ASSAY,
        workcell=OT2,
    )
    cdir = start_campaign(cfg, base=base)
    assert load_cstate(cdir).state == "DONE"
    return cdir


def test_nmr_command_writes_the_paired_files(small_campaign, capsys):
    needs_evaluator()
    cdir = small_campaign
    rdir = cdir / "rounds" / "02"
    before = {p: sha(p) for p in cdir.rglob("*") if p.is_file()}
    assert main(["nmr", str(cdir)]) == 0  # the latest round read is 2
    out = capsys.readouterr().out
    assert "Round 2" in out and "Synthetic" in out
    for name in (
        "nmr/spectra.npz",
        "nmr/truth.json",
        "nmr/nmr_fits.csv",
        "review/evidence.json",
        "paired.md",
    ):
        assert (rdir / name).exists(), name
    # Nothing the campaign wrote has changed.
    assert {p: sha(p) for p in before} == before

    text = (rdir / "review" / "evidence.json").read_text()
    assert_no_truth(text)
    ev = Evidence.model_validate_json(text)
    assert ev.campaign_id == "c-nmr" and ev.round == 2 and len(ev.conditions) == 4
    # The LC/MS side matches the loop's own results.csv.
    import csv

    rows = list(csv.DictReader(open(rdir / "results.csv", encoding="utf-8")))
    for w in ev.wells:
        row = next(r for r in rows if r["well"] == w.well)
        assert w.lcms.yield_pct == pytest.approx(float(row["yield_pct"]), abs=0.051)
    md = (rdir / "paired.md").read_text()
    assert "Synthetic data" in md and "| c01 |" in md and "LC/MS plate flags" in md
    # The campaign's own LC/MS fault setting and no NMR fault, stated for people.
    assert "- LC/MS: injection_failure." in md and "- NMR: none." in md


def test_nmr_command_with_faults_and_errors(small_campaign, capsys):
    needs_evaluator()
    cdir = small_campaign
    assert main(["nmr", str(cdir), "--round", "1", "--fault", "nmr_broad_lines"]) == 0
    run = json.loads((cdir / "rounds" / "01" / "nmr" / "nmr_run.json").read_text())
    assert run["faults"] == ["nmr_broad_lines@1"] and run["synthetic"] is True
    truth = json.loads((cdir / "rounds" / "01" / "nmr" / "truth.json").read_text())
    assert truth["plate_faults"] == ["nmr_broad_lines"]
    assert "NMR_BROAD_LINES" in (cdir / "rounds" / "01" / "review" / "evidence.json").read_text()
    assert "- NMR: nmr_broad_lines@1." in (cdir / "rounds" / "01" / "paired.md").read_text()
    capsys.readouterr()
    assert main(["nmr", str(cdir), "--round", "1", "--fault", "nmr_noise"]) == 2
    assert "unknown NMR fault" in capsys.readouterr().out
    assert main(["nmr", str(cdir), "--round", "1", "--fault", "nmr_impurity@2"]) == 2
    assert "names round 2" in capsys.readouterr().out
    assert main(["nmr", str(cdir), "--round", "5"]) == 2
    assert "no LC/MS reading" in capsys.readouterr().out
    assert main(["nmr", str(cdir.parent)]) == 2
