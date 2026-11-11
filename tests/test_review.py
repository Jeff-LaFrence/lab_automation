"""The overwatch reviewer, its checks, the two fixed rule baselines, scoring, and the commands.

Most tests use a small hand-made evidence pack and ScriptedClient, so they do
not depend on the NMR or pairing code, the network, or a model. One slow test
builds every fault case with paired.build_case and checks that both
baselines, the checks, and the scoring agree on them.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from labauto.agents.llm import EndpointUnavailable, ScriptedClient, Transcript
from labauto.agents.planner import load_prompt
from labauto.agents.router import route
from labauto.cli import main
from labauto.cli_review import (
    NO_REPLY_LABEL,
    QC_ONLY_LABEL,
    SCRIPTED_LABEL,
    UNNAMED_LABEL,
    next_scope,
    who_reviewed,
)
from labauto.config import Endpoint, Settings
from labauto.planner_eval import NoModelAnswered
from labauto.review import (
    CHECKED_RULES,
    FAULT_KINDS,
    NEEDS_PERSON,
    PROMPTS,
    WELL_FAULTS,
    DataReview,
    check_review,
    drift_signature,
    drift_stats,
    impurity_stats,
    matches,
    merge_faults,
    pack_dict,
    qc_only_review,
    qc_stats_review,
    resolves,
    review_messages,
    review_round,
    score,
    truth_in,
)
from labauto.reviewer_eval import (
    BASELINE_SEEDS,
    SCRIPTED_BANNER,
    SEED_STRIDE,
    SEED_TRIES,
    SWEEP_DIR,
    UNNAMED_BANNER,
    baseline_sweep,
    evaluate,
    sweep_section,
    sweep_seed,
)

# ---------------------------------------------------------------- a hand-made pack

GROUPS = {"c01": ["E1", "F1", "G1"], "c02": ["H1", "A2", "B2"], "c03": ["C2", "D2", "E2"]}


def _well(well: str, key: str, i: int) -> dict:
    return {
        "well": well,
        "key": key,
        "lcms": {"yield_pct": 40.0 + i, "is_ratio": 1.01, "excluded": False, "flags": []},
        "nmr": {
            "yield_pct": 40.5 + i,
            "naive_yield_pct": 39.5 + i,
            "yield_from_doublet": 40.4 + i,
            "yield_from_singlet": 40.6 + i,
            "rmse_over_noise": 1.04,
            "is_snr": 212.5,
            "linewidth_hz": 1.21,
            "phase_deg": 0.2,
            "excluded": False,
            "flags": [],
        },
    }


def _cond(key: str, lcms_mean: float, nmr_mean: float) -> dict:
    return {
        "key": key,
        "conditions": {"catalyst_mol_pct": 2.0, "base_equiv": 1.5, "cosolvent_pct": 20},
        "wells": GROUPS[key],
        "lcms": {"mean": lcms_mean, "sd": 1.3, "n": 3, "flags": []},
        "nmr": {"mean": nmr_mean, "sd": 1.1, "n": 3, "flags": []},
        "difference": round(lcms_mean - nmr_mean, 2),
        "combined_se": 0.98,
        "agreement": "agree",
        "flags": [],
    }


def clean_pack() -> dict:
    """Three conditions in triplicate, no flags anywhere."""
    wells = [_well(w, k, i) for i, (k, ws) in enumerate(GROUPS.items()) for w in ws]
    return {
        "campaign_id": "test",
        "round": 2,
        "assay_id": "generic_reaction_screen_v1",
        "synthetic": True,
        "evaluator": "lineshape least squares (classical)",
        "plate": {
            "lcms_flags": [],
            "nmr_flags": [],
            "controls": {
                "negative": {"lcms_mean": 0.4, "nmr_mean": 0.6, "limit": 5.0},
                "reference": {
                    "lcms_mean": 50.9,
                    "nmr_mean": 51.2,
                    "expected": 50.6,
                    "tolerance": 8.0,
                },
            },
        },
        "conditions": [
            _cond("c01", 50.2, 49.8),
            _cond("c02", 31.5, 32.1),
            _cond("c03", 12.7, 13.0),
        ],
        "wells": wells,
    }


def flagged_pack() -> dict:
    """c02 lost well A2 to an injection failure; c03 kept one LC/MS well."""
    pack = clean_pack()
    wells = {w["well"]: w for w in pack["wells"]}
    wells["A2"]["lcms"].update(is_ratio=0.31, excluded=True, flags=["IS_OUT_OF_RANGE"])
    pack["conditions"][1]["lcms"]["n"] = 2
    c03 = pack["conditions"][2]
    c03["lcms"].update(n=1, sd=None, flags=["LOW_REPLICATES"])
    c03["agreement"] = "insufficient"
    return pack


def drifted_pack() -> dict:
    """The LC/MS plate failed, its reference control reads low, and LC/MS reads below NMR."""
    pack = clean_pack()
    pack["plate"]["lcms_flags"] = ["PLATE_QC_FAIL"]
    pack["plate"]["controls"]["reference"]["lcms_mean"] = 39.0
    return pack


GOOD = {
    "round_verdict": "accept_with_flags",
    "conditions": [
        {
            "key": "c01",
            "verdict": "accept",
            "reason": "LC/MS 50.2 and NMR 49.8 agree.",
            "evidence": ["conditions.c01.lcms.mean", "conditions.c01.nmr.mean"],
        },
        {
            "key": "c02",
            "verdict": "flag",
            "reason": "Well A2 has a low internal standard and is excluded.",
            "evidence": ["wells.A2.lcms.flags.IS_OUT_OF_RANGE", "wells.A2.lcms.is_ratio"],
        },
        {
            "key": "c03",
            "verdict": "rerun",
            "reason": "Only one valid LC/MS well; the difference is -0.3.",
            "evidence": ["conditions.c03.lcms.flags", "conditions.c03.agreement"],
        },
    ],
    "suspected_faults": [
        {
            "kind": "injection_failure",
            "wells": ["A2"],
            "reason": "Internal standard ratio 0.31 is far from the rest of the plate.",
        }
    ],
    "summary": "One injection failure in A2. Condition c03 needs a rerun.",
}


def good() -> dict:
    return copy.deepcopy(GOOD)


# ---------------------------------------------------------------- check_review


def test_a_correct_review_passes():
    assert check_review(DataReview.model_validate(good()), flagged_pack()) == []


def _drop_c03(r):
    r["conditions"] = r["conditions"][:2]


def _twice(r):
    r["conditions"].append(copy.deepcopy(r["conditions"][0]))


def _unknown_key(r):
    r["conditions"].append(
        {"key": "c99", "verdict": "flag", "reason": "x", "evidence": ["plate.lcms_flags"]}
    )


def _unknown_well(r):
    r["suspected_faults"][0]["wells"] = ["A2", "H12"]


def _bad_path(r):
    r["conditions"][0]["evidence"] = ["conditions.c01.lcms.median"]


def _wells_by_key(r):
    r["conditions"][0]["evidence"] = ["wells.c01.lcms.mean"]


def _padded_path(r):
    r["conditions"][0]["evidence"] = [" conditions.c01.lcms.mean"]


def _bare_section(r):
    r["conditions"][0]["evidence"] = ["conditions.c01.lcms.mean", "wells"]


def _off_condition(r):
    r["conditions"][0]["evidence"] = ["conditions.c02.lcms.mean", "plate.controls"]
    r["conditions"][0]["reason"] = "Agrees."


def _no_evidence(r):
    r["conditions"][0]["evidence"] = []


def _no_reason(r):
    r["conditions"][1]["reason"] = "  "


def _fault_no_reason(r):
    r["suspected_faults"][0]["reason"] = ""


def _invented_reason(r):
    r["conditions"][0]["reason"] = "LC/MS 61.7 and NMR 49.8 agree."


def _number_from_another_condition(r):
    r["conditions"][0]["reason"] = "LC/MS 50.2, well above c02 at 31.5."


def _sign_dropped(r):
    r["conditions"][2]["reason"] = "The difference is 0.3."


def _sign_added(r):
    r["conditions"][2]["reason"] = "LC/MS mean -12.7."


def _fullwidth_digit(r):
    r["conditions"][0]["reason"] = "LC/MS 50.\uff12 and NMR 49.8 agree."


def _arabic_digit(r):
    r["summary"] = "Condition c03 needs \u0663 more wells."


def _invented_summary(r):
    r["summary"] = "About 17 percent of wells look wrong."


def _invented_fault_reason(r):
    r["suspected_faults"][0]["reason"] = "Internal standard at 0.12 of normal."


def _fault_number_from_another_well(r):
    # 1.01 is the ratio of every other well, but not of A2 and not in the plate block.
    r["suspected_faults"][0]["reason"] = "Ratio 0.31 against 1.01 elsewhere."


def _accept_low(r):
    r["conditions"][2]["verdict"] = "accept"


def _accept_inconsistent_round(r):
    r["round_verdict"] = "accept"


def _long_summary(r):
    r["summary"] = " ".join(["word"] * 81)


def _well_fault_without_wells(r):
    r["suspected_faults"][0]["wells"] = []


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (_drop_c03, "c03 is not judged"),
        (_twice, "c01 is judged 2 times"),
        (_unknown_key, "'c99' is not in the evidence pack"),
        (_unknown_well, "'H12', which is not in the evidence pack"),
        (_bad_path, "'conditions.c01.lcms.median', which does not resolve"),
        (_wells_by_key, "'wells.c01.lcms.mean', which does not resolve"),
        (_padded_path, "' conditions.c01.lcms.mean', which does not resolve"),
        (_bare_section, "cites 'wells', a whole section"),
        (_off_condition, "c01 cites no path under conditions.c01 or under one of its wells"),
        (_no_evidence, "c01 cites no evidence path"),
        (_no_reason, "Condition c02 has no reason"),
        (_fault_no_reason, "Suspected fault 1 (injection_failure) has no reason"),
        (_invented_reason, "it cites: 61.7"),
        (_number_from_another_condition, "it cites: 31.5"),
        (_sign_dropped, "it cites: 0.3"),
        (_sign_added, "it cites: -12.7"),
        (_fullwidth_digit, "c01 reason has non-ASCII digits"),
        (_arabic_digit, "The summary has non-ASCII digits"),
        (_invented_summary, "summary has numbers that are not in the evidence pack: 17"),
        (_invented_fault_reason, "named wells' rows or the plate block: 0.12"),
        (_fault_number_from_another_well, "named wells' rows or the plate block: 1.01"),
        (_accept_low, "carries LOW_REPLICATES"),
        (_accept_low, "agreement is insufficient"),
        (_accept_inconsistent_round, "Use accept_with_flags"),
        (_accept_inconsistent_round, "the review names suspected faults"),
        (_long_summary, "The summary has 81 words. The limit is 80."),
        (_well_fault_without_wells, "names no wells"),
    ],
)
def test_check_review_catches_each_rule(mutate, expected):
    reply = good()
    mutate(reply)
    problems = check_review(DataReview.model_validate(reply), flagged_pack())
    assert any(expected in p for p in problems), problems


def test_a_cited_path_lets_a_reason_use_its_numbers():
    reply = good()
    reply["conditions"][0]["reason"] = "LC/MS 50.2, well above c02 at 31.5."
    reply["conditions"][0]["evidence"].append("conditions.c02.lcms.mean")
    assert check_review(DataReview.model_validate(reply), flagged_pack()) == []


def test_names_are_not_numbers_but_unknown_names_are():
    reply = good()
    reply["summary"] = "Wells A2, E1 and G1 in c01 and c02, read by 1H NMR."
    assert check_review(DataReview.model_validate(reply), flagged_pack()) == []
    reply["summary"] = "Condition c99 is fine."
    problems = check_review(DataReview.model_validate(reply), flagged_pack())
    assert any("not in the evidence pack: 99" in p for p in problems), problems


def test_a_disagreeing_condition_may_not_be_accepted():
    pack = flagged_pack()
    pack["conditions"][0].update(agreement="disagree", flags=["LCMS_NMR_DISAGREE"])
    problems = check_review(DataReview.model_validate(good()), pack)
    assert any("c01 is accepted but its LC/MS and NMR readouts disagree" in p for p in problems)


def test_a_failed_plate_must_be_rerun_and_accepts_nothing():
    pack = flagged_pack()
    pack["plate"]["lcms_flags"] = ["PLATE_QC_FAIL"]
    problems = check_review(DataReview.model_validate(good()), pack)
    assert any("must be rerun_plate, not accept_with_flags" in p for p in problems)
    assert any("c01 is accepted on a plate with PLATE_QC_FAIL" in p for p in problems)
    reply = good()
    reply["round_verdict"] = "rerun_plate"
    reply["conditions"][0]["verdict"] = "rerun"
    assert check_review(DataReview.model_validate(reply), pack) == []


def test_round_accept_needs_no_plate_flag_and_no_fault():
    pack = clean_pack()
    pack["plate"]["nmr_flags"] = ["NMR_LOW_SNR"]
    reply = {
        "round_verdict": "accept",
        "conditions": [
            {"key": k, "verdict": "accept", "reason": "Agree.", "evidence": [f"conditions.{k}"]}
            for k in GROUPS
        ],
        "suspected_faults": [],
        "summary": "",
    }
    problems = check_review(DataReview.model_validate(reply), pack)
    assert any("the plate carries NMR_LOW_SNR" in p for p in problems), problems
    assert check_review(DataReview.model_validate(reply), clean_pack()) == []


def test_evidence_paths_resolve_by_key_well_position_and_flag():
    pack = flagged_pack()
    for path in (
        "plate.controls.reference.expected",
        "conditions.c02.wells.A2",
        "wells.E1.nmr.rmse_over_noise",
        "wells.A2.lcms.flags.IS_OUT_OF_RANGE",
        "conditions.c03.lcms.flags.0",
        "conditions.c03.lcms.sd",  # present, even though it is null
    ):
        assert resolves(pack, path), path
    for path in (
        "",
        "wells.Z9",
        "conditions.c01.lcms.flags.LOW_REPLICATES",
        "plate.lcms_flags.x",
        " wells.E1",
        "wells.E1 ",
        "conditions..c01",
    ):
        assert not resolves(pack, path), path


def test_unknown_fault_kinds_fail_the_reply_format():
    reply = good()
    reply["suspected_faults"][0]["kind"] = "bad_luck"
    with pytest.raises(ValueError):
        DataReview.model_validate(reply)


@pytest.mark.parametrize("where", ["top", "condition", "fault"], ids=["top", "condition", "fault"])
def test_a_misspelled_key_fails_the_reply_format(where):
    reply = good()
    target = {
        "top": reply,
        "condition": reply["conditions"][0],
        "fault": reply["suspected_faults"][0],
    }[where]
    target["evidnce"] = []
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        DataReview.model_validate(reply)


def test_review_md_lists_the_rules_code_checks():
    from labauto.cli_review import render_review

    record = {
        "round": 2,
        "reviewed_by": SCRIPTED_LABEL,
        "reviewer_kind": "scripted",
        "evaluator": "x",
        "created_at": "now",
        "review": good(),
        "attempts": [],
        "note": "",
    }
    md = render_review(record)
    for rule in CHECKED_RULES:
        assert rule in md


# ---------------------------------------------------------------- review_round


def test_a_good_first_reply_is_accepted_and_recorded(tmp_path):
    client = ScriptedClient([good()])
    transcript = Transcript(tmp_path / "transcript.jsonl")
    review, info = review_round(flagged_pack(), client, "local-main", transcript)
    assert review is not None and review.round_verdict == "accept_with_flags"
    assert info["model"] == "scripted" and len(info["attempts"]) == 1
    assert info["outage"] is False
    records = [json.loads(x) for x in (tmp_path / "transcript.jsonl").read_text().splitlines()]
    assert [r["role"] for r in records] == ["data_reviewer"]
    sent = client.calls[0][1]
    assert "evidence pack" in sent[0]["content"] and '"c03"' in sent[1]["content"]


def test_a_bad_reply_gets_the_problems_back_and_the_retry_passes():
    bad = good()
    _drop_c03(bad)
    client = ScriptedClient([bad, good()])
    review, info = review_round(flagged_pack(), client, "local-main")
    assert review is not None
    assert len(client.calls) == 2
    feedback = client.calls[1][1][-1]["content"]
    assert "Condition c03 is not judged" in feedback
    assert info["attempts"][0]["problems"][0]["kind"] == "coverage"
    assert info["attempts"][1]["problems"] == []


def test_bad_replies_until_the_retries_run_out_end_with_no_review_and_a_note():
    bad = good()
    _invented_reason(bad)
    client = ScriptedClient([bad, bad, bad])
    review, info = review_round(flagged_pack(), client, "local-main")
    assert review is None
    assert len(client.calls) == 3  # the first reply and 2 retries
    assert info["note"].startswith(NEEDS_PERSON)
    assert "in 3 attempts (3 replies)" in info["note"]
    assert "61.7" in info["note"]
    assert info["outage"] is False


def test_an_unreadable_reply_counts_as_a_failed_attempt():
    client = ScriptedClient(["no json here", "still none", good()])
    review, info = review_round(flagged_pack(), client, "local-main")
    assert review is not None
    assert info["attempts"][0]["parse_error"].startswith("SCHEMA_INVALID")


def test_a_misspelled_key_goes_back_to_the_model_as_a_parse_error():
    bad = good()
    bad["conditions"][0]["evidnce"] = bad["conditions"][0].pop("evidence")
    client = ScriptedClient([bad, bad, good()])
    review, info = review_round(flagged_pack(), client, "local-main")
    assert review is not None
    assert "evidnce" in info["attempts"][0]["parse_error"]
    assert "Extra inputs are not permitted" in client.calls[1][1][-1]["content"]


class Down:
    def complete(self, *args, **kwargs):
        raise EndpointUnavailable("local-main did not answer (ConnectError)")


def test_an_unreachable_model_gives_no_review_and_says_so():
    review, info = review_round(flagged_pack(), Down(), "local-main")
    assert review is None and info["replies"] == 0 and info["outage"] is True
    assert "gave no reply" in info["note"]


def test_a_pack_holding_truth_is_refused():
    pack = flagged_pack()
    pack["wells"][0]["truth"] = {"true_yield": 40.0}
    with pytest.raises(ValueError, match="truth"):
        review_round(pack, ScriptedClient([good()]), "local-main")


# ---------------------------------------------------------------- the baseline


def test_qc_only_review_maps_flags_to_verdicts_and_faults():
    pack = flagged_pack()
    review = qc_only_review(pack)
    verdicts = {v.key: v.verdict for v in review.conditions}
    assert verdicts == {"c01": "accept", "c02": "flag", "c03": "rerun"}
    assert review.round_verdict == "accept_with_flags"
    assert [(f.kind, f.wells) for f in review.suspected_faults] == [("injection_failure", ["A2"])]
    assert check_review(review, pack) == []


def test_qc_only_review_accepts_a_clean_plate():
    review = qc_only_review(clean_pack())
    assert review.round_verdict == "accept"
    assert {v.verdict for v in review.conditions} == {"accept"}
    assert review.suspected_faults == []
    assert check_review(review, clean_pack()) == []


def test_qc_only_review_names_drift_only_with_the_drift_signature():
    pack = drifted_pack()
    assert drift_signature(pack)
    review = qc_only_review(pack)
    assert review.round_verdict == "rerun_plate"
    assert {v.verdict for v in review.conditions} == {"rerun"}
    assert [(f.kind, f.wells) for f in review.suspected_faults] == [("plate_drift", [])]
    assert "39.0" in review.suspected_faults[0].reason
    assert check_review(review, pack) == []


@pytest.mark.parametrize(
    "change",
    ["reference_in_tolerance", "lcms_above_nmr", "nmr_side_failure"],
)
def test_qc_only_review_calls_any_other_plate_failure_other(change):
    pack = drifted_pack()
    if change == "reference_in_tolerance":
        pack["plate"]["controls"]["reference"]["lcms_mean"] = 50.9
    elif change == "lcms_above_nmr":
        for c in pack["conditions"]:
            c["lcms"]["mean"] = c["nmr"]["mean"] + 1.0
    else:
        pack["plate"]["lcms_flags"] = []
        pack["plate"]["nmr_flags"] = ["PLATE_QC_FAIL"]
        pack["plate"]["controls"]["reference"]["lcms_mean"] = 50.9
    assert not drift_signature(pack)
    review = qc_only_review(pack)
    assert review.round_verdict == "rerun_plate"
    assert [(f.kind, f.wells) for f in review.suspected_faults] == [("other", [])]
    assert check_review(review, pack) == []


def test_qc_only_review_does_not_accept_a_round_with_plate_flags():
    pack = clean_pack()
    pack["plate"]["nmr_flags"] = ["NMR_LOW_SNR"]
    review = qc_only_review(pack)
    assert review.suspected_faults == []
    assert review.round_verdict == "accept_with_flags"
    assert check_review(review, pack) == []


def test_qc_only_review_names_nmr_faults_from_nmr_flags():
    pack = clean_pack()
    pack["plate"]["nmr_flags"] = ["NMR_BROAD_LINES"]
    for w in pack["wells"]:
        w["nmr"]["flags"] = ["NMR_PHASE_CORRECTED"]
    pack["wells"][4]["nmr"]["flags"].append("NMR_SIGNAL_INCONSISTENT")
    pack["conditions"][0].update(agreement="disagree", flags=["LCMS_NMR_DISAGREE"])
    review = qc_only_review(pack)
    named = {f.kind: f.wells for f in review.suspected_faults}
    assert named["nmr_broad_lines"] == []
    assert named["nmr_phase_error"] == [w["well"] for w in pack["wells"]]
    assert named["nmr_impurity"] == [pack["wells"][4]["well"]]
    assert "lcms_nmr_disagreement" not in named  # a disagreement alone names no cause
    assert {v.verdict for v in review.conditions} == {"flag"}
    assert check_review(review, pack) == []


# ---------------------------------------------------------------- the plate statistics baseline


def drift_quiet_pack(lows=(4.1, 3.6, 3.2)) -> dict:
    """No flag anywhere, but LC/MS reads below NMR on every condition by the given points."""
    pack = clean_pack()
    for c, low in zip(pack["conditions"], lows, strict=True):
        c["lcms"]["mean"] = round(c["nmr"]["mean"] - low, 1)
        c["difference"] = round(c["lcms"]["mean"] - c["nmr"]["mean"], 1)
    return pack


def _set_gap(pack: dict, well: str, gap: float) -> None:
    nmr = next(w for w in pack["wells"] if w["well"] == well)["nmr"]
    nmr["yield_from_singlet"] = round(nmr["yield_from_doublet"] + gap, 1)


def test_qc_stats_review_raises_no_false_alarm_on_a_clean_plate():
    pack = clean_pack()
    assert not drift_stats(pack)["fires"] and impurity_stats(pack)["wells"] == []
    review = qc_stats_review(pack)
    assert review.round_verdict == "accept"
    assert {v.verdict for v in review.conditions} == {"accept"}
    assert review.suspected_faults == []
    assert check_review(review, pack) == []
    base = qc_only_review(pack)
    assert [v.model_dump() for v in review.conditions] == [v.model_dump() for v in base.conditions]


def test_qc_stats_review_names_drift_that_raises_no_flag():
    pack = drift_quiet_pack()
    assert qc_only_review(pack).suspected_faults == []
    stats = drift_stats(pack)
    assert stats["fires"] and stats["low"] == 3 and stats["median"] == -3.6
    review = qc_stats_review(pack)
    assert [(f.kind, f.wells) for f in review.suspected_faults] == [("plate_drift", [])]
    # The reason quotes the middle condition's difference, a number under conditions.
    assert (
        "The median difference (LC/MS minus NMR) is c02's -3.6."
        in review.suspected_faults[0].reason
    )
    assert review.round_verdict == "accept_with_flags"
    assert {v.verdict for v in review.conditions} == {"flag"}
    assert all(f"conditions.{v.key}.difference" in v.evidence for v in review.conditions)
    assert check_review(review, pack) == []


@pytest.mark.parametrize(
    "lows, fires",
    [
        ((4.1, 3.6, 3.2), True),
        ((2.6, 2.5, 2.4), False),  # every condition low, but the median is above -3 points
        ((4.1, 3.6, -0.5), False),  # median -3.6, but only 2 of 3 conditions low (under 80%)
        ((3.0, 3.0, 3.0), True),  # exactly at the limits
    ],
)
def test_the_drift_rule_needs_both_the_share_and_the_median(lows, fires):
    pack = drift_quiet_pack(lows)
    assert drift_stats(pack)["fires"] is fires
    kinds = [f.kind for f in qc_stats_review(pack).suspected_faults]
    assert kinds == (["plate_drift"] if fires else [])
    assert check_review(qc_stats_review(pack), pack) == []


def test_the_drift_rule_counts_only_conditions_with_two_wells_a_side():
    pack = drift_quiet_pack((4.1, 3.6, -5.0))
    assert not drift_stats(pack)["fires"]
    c03 = pack["conditions"][2]
    c03["lcms"].update(n=1, sd=None, flags=["LOW_REPLICATES"])
    c03["agreement"] = "insufficient"
    stats = drift_stats(pack)
    assert [k for k, _ in stats["counted"]] == ["c01", "c02"] and stats["median"] == -3.85
    review = qc_stats_review(pack)
    drift = next(f for f in review.suspected_faults if f.kind == "plate_drift")
    assert "lies between c01's -4.1 and c02's -3.6." in drift.reason
    assert check_review(review, pack) == []


def test_qc_stats_review_does_not_name_drift_twice():
    pack = drifted_pack()
    for c in pack["conditions"]:
        c["lcms"]["mean"] = round(c["nmr"]["mean"] - 4.0, 1)
        c["difference"] = -4.0
    assert drift_signature(pack) and drift_stats(pack)["fires"]
    review = qc_stats_review(pack)
    assert [f.kind for f in review.suspected_faults] == ["plate_drift"]
    assert review.round_verdict == "rerun_plate"
    assert check_review(review, pack) == []


def test_qc_stats_review_names_a_well_whose_singlet_reads_above_its_doublet():
    pack = clean_pack()  # every gap is 0.2, so the spread is 0 and the 1.5 point floor rules
    _set_gap(pack, "F1", 1.6)
    _set_gap(pack, "A2", 1.4)  # under the floor
    assert qc_only_review(pack).suspected_faults == []
    stats = impurity_stats(pack)
    assert stats["spread"] == 0 and stats["limit"] == 1.5 and stats["wells"] == ["F1"]
    review = qc_stats_review(pack)
    assert [(f.kind, f.wells) for f in review.suspected_faults] == [("nmr_impurity", ["F1"])]
    verdicts = {v.key: v.verdict for v in review.conditions}
    assert verdicts == {"c01": "flag", "c02": "accept", "c03": "accept"}
    c01 = review.conditions[0]
    assert "wells.F1.nmr.yield_from_singlet" in c01.evidence and "F1" in c01.reason
    assert review.round_verdict == "accept_with_flags"
    assert check_review(review, pack) == []


def test_the_impurity_bar_rises_with_the_plates_own_scatter():
    pack = clean_pack()
    wells = [w["well"] for w in pack["wells"]]
    for well, gap in zip(wells[:8], [-0.6, 1.0, 0.2] * 3, strict=False):
        _set_gap(pack, well, gap)
    # Median gap 0.2, median absolute deviation 0.8: s = 1.19 and the bar is 4.74 points.
    _set_gap(pack, wells[8], 4.0)
    stats = impurity_stats(pack)
    assert round(stats["spread"], 2) == 1.19 and round(stats["limit"], 2) == 4.74
    assert stats["wells"] == []  # 4.0 is over the floor but under four spreads
    _set_gap(pack, wells[8], 5.0)
    assert impurity_stats(pack)["wells"] == [wells[8]]
    review = qc_stats_review(pack)
    assert [(f.kind, f.wells) for f in review.suspected_faults] == [("nmr_impurity", [wells[8]])]
    assert check_review(review, pack) == []


def test_the_impurity_rule_skips_excluded_wells_and_wells_qc_already_named():
    pack = clean_pack()
    rows = {w["well"]: w for w in pack["wells"]}
    _set_gap(pack, "E1", 6.0)
    rows["E1"]["nmr"]["flags"] = ["NMR_SIGNAL_INCONSISTENT"]
    _set_gap(pack, "H1", 3.0)
    rows["H1"]["nmr"]["excluded"] = True
    assert "H1" not in impurity_stats(pack)["gaps"]
    review = qc_stats_review(pack)
    assert [(f.kind, f.wells) for f in review.suspected_faults] == [("nmr_impurity", ["E1"])]
    assert review.suspected_faults[0].reason == "Wells flagged NMR_SIGNAL_INCONSISTENT."
    assert check_review(review, pack) == []


def test_a_plate_wide_fault_may_quote_condition_numbers_but_a_well_fault_may_not():
    reply = good()
    reply["suspected_faults"].append(
        {"kind": "plate_drift", "wells": [], "reason": "c01 differs by 0.4; c02 LC/MS 31.5."}
    )
    assert check_review(DataReview.model_validate(reply), flagged_pack()) == []
    reply["suspected_faults"][0]["reason"] = "Ratio 0.31, and c02 LC/MS reads 31.5."
    problems = check_review(DataReview.model_validate(reply), flagged_pack())
    assert any("named wells' rows or the plate block: 31.5" in p for p in problems), problems
    reply = good()
    reply["suspected_faults"].append(
        {"kind": "plate_drift", "wells": [], "reason": "LC/MS reads 61.7 on c01."}
    )
    problems = check_review(DataReview.model_validate(reply), flagged_pack())
    assert any("the plate block, or under conditions: 61.7" in p for p in problems), problems


# ---------------------------------------------------------------- the prompts


def test_the_no_hints_prompt_is_the_default_without_its_fault_signatures():
    default, bare = load_prompt(PROMPTS["default"]), load_prompt(PROMPTS["no_hints"])
    start, end = default.index("Fault signatures\n"), default.index("Reply with one JSON")
    assert default[:start] + default[end:] == bare
    assert "Fault signatures" not in bare and "Check the reference control" not in bare
    for kind in ("injection_failure", "plate_drift", "nmr_impurity", "lcms_nmr_disagreement"):
        assert kind in bare
    assert review_messages(flagged_pack(), "no_hints")[0]["content"] == bare
    assert review_messages(flagged_pack())[0]["content"] == default
    with pytest.raises(ValueError, match="unknown reviewer prompt"):
        review_messages(flagged_pack(), "hints")


def test_review_round_sends_the_chosen_prompt_and_records_it():
    client = ScriptedClient([good()])
    review, info = review_round(flagged_pack(), client, "local-main", prompt="no_hints")
    assert review is not None and info["prompt"] == "no_hints"
    assert client.calls[0][1][0]["content"] == load_prompt(PROMPTS["no_hints"])


def test_the_prompts_tell_the_number_rule_code_checks():
    for name in PROMPTS.values():
        text = load_prompt(name)
        rule = "An injection_failure or nmr_impurity reason uses its wells and the plate block"
        assert rule in text
        assert "Any other fault's reason may also use numbers under conditions" in text


# ---------------------------------------------------------------- scoring


def _false_alarm_total(s: dict) -> int:
    """False alarms rebuilt from the per-kind detail: extra wells, plus wrong kinds once each."""
    return sum(len(e["extra"]) for e in s["named"]) + sum(
        1 for e in s["named"] if e["outcome"] == "false alarm" and not e["extra"]
    )


def test_a_well_fault_is_caught_only_with_a_shared_well():
    true = [{"kind": "injection_failure", "wells": ["A2", "C2"]}]
    hit = score(true, [{"kind": "injection_failure", "wells": ["C2"]}])
    assert len(hit["caught"]) == 1 and hit["missed"] == [] and hit["false_alarms"] == []
    assert hit["wells"] == {"true": 2, "hits": 1, "missed": 1, "extra": 0}
    wrong_well = score(true, [{"kind": "injection_failure", "wells": ["E1"]}])
    assert wrong_well["caught"] == [] and len(wrong_well["missed"]) == 1
    assert wrong_well["false_alarms"] == [{"kind": "injection_failure", "wells": ["E1"]}]
    assert wrong_well["wells"] == {"true": 2, "hits": 0, "missed": 2, "extra": 1}


def test_naming_every_well_does_not_score_like_the_exact_answer():
    pack = flagged_pack()
    true = [{"kind": "injection_failure", "wells": ["A2"]}]
    exact = score(true, [{"kind": "injection_failure", "wells": ["A2"]}])
    every = score(
        true, [{"kind": "injection_failure", "wells": [w["well"] for w in pack["wells"]]}]
    )
    assert len(exact["caught"]) == len(every["caught"]) == 1
    assert exact["false_alarms"] == [] and exact["wells"]["extra"] == 0
    assert len(every["false_alarms"]) == len(pack["wells"]) - 1
    assert every["wells"] == {"true": 1, "hits": 1, "missed": 0, "extra": len(pack["wells"]) - 1}


def test_grouping_wells_into_entries_does_not_change_the_score():
    true = [
        {"kind": "injection_failure", "wells": ["A2", "C2"]},
        {"kind": "nmr_impurity", "wells": ["E1"]},
    ]
    split = [
        {"kind": "injection_failure", "wells": ["A2"]},
        {"kind": "injection_failure", "wells": ["C2", "E2"]},
        {"kind": "nmr_impurity", "wells": ["E1"]},
        {"kind": "nmr_impurity", "wells": ["E1"]},
    ]
    merged = [
        {"kind": "injection_failure", "wells": ["A2", "C2", "E2"]},
        {"kind": "nmr_impurity", "wells": ["E1"]},
    ]
    assert merge_faults(split) == merged
    assert score(true, split) == score(true, merged)
    assert score(true, split)["false_alarms"] == [{"kind": "injection_failure", "wells": ["E2"]}]


def test_a_wrong_plate_fault_counts_once_however_often_it_is_named():
    named = [
        {"kind": "plate_drift", "wells": []},
        {"kind": "plate_drift", "wells": ["E1"]},
        {"kind": "other", "wells": []},
        {"kind": "other", "wells": ["A2"]},
    ]
    s = score([], named)
    assert [f["kind"] for f in s["false_alarms"]] == ["plate_drift", "other"]
    assert _false_alarm_total(s) == len(s["false_alarms"])


def test_a_plate_wide_fault_is_caught_by_kind_whatever_the_wells():
    true = [{"kind": "plate_drift", "wells": []}]
    assert matches({"kind": "plate_drift", "wells": []}, true[0])
    assert matches({"kind": "plate_drift", "wells": ["E1"]}, true[0])
    assert not matches({"kind": "nmr_broad_lines", "wells": []}, true[0])
    s = score(true, [{"kind": "plate_drift", "wells": ["E1"]}])
    assert len(s["caught"]) == 1 and s["false_alarms"] == []


def test_a_disagreement_is_a_false_alarm_only_on_a_clean_plate():
    named = [{"kind": "lcms_nmr_disagreement", "wells": ["E1"]}]
    faulty = score([{"kind": "nmr_impurity", "wells": ["D2"]}], named)
    assert faulty["false_alarms"] == [] and len(faulty["excused"]) == 1
    clean = score([], named + [{"kind": "lcms_nmr_disagreement", "wells": ["F1"]}])
    assert clean["false_alarms"] == [{"kind": "lcms_nmr_disagreement", "wells": ["E1", "F1"]}]


def test_missed_faults_and_false_alarms_are_counted():
    true = [
        {"kind": "nmr_impurity", "wells": ["D2"]},
        {"kind": "nmr_phase_error", "wells": []},
    ]
    named = [
        {"kind": "nmr_phase_error", "wells": []},
        {"kind": "nmr_broad_lines", "wells": []},
        {"kind": "other", "wells": []},
    ]
    s = score(true, named)
    assert s["true"] == 2
    assert [f["kind"] for f in s["caught"]] == ["nmr_phase_error"]
    assert [f["kind"] for f in s["missed"]] == ["nmr_impurity"]
    assert [f["kind"] for f in s["false_alarms"]] == ["nmr_broad_lines", "other"]
    assert s["wells"] == {"true": 1, "hits": 0, "missed": 1, "extra": 0}
    clean = score([], [{"kind": "injection_failure", "wells": ["E1"]}])
    assert clean["true"] == 0 and len(clean["false_alarms"]) == 1


# ---------------------------------------------------------------- router


def test_the_data_reviewer_always_goes_to_the_main_endpoint(tmp_path):
    main_ep = Endpoint(label="local-main", base_url="u", model="m", context_tokens=32000)
    small = Endpoint(label="local-small", base_url="u", model="s", context_tokens=64000)
    log = tmp_path / "model_calls.jsonl"
    both = Settings(main=main_ep, small=small)
    assert route("data_reviewer", 100, both, log).label == "local-main"
    assert route("data_reviewer", 100, Settings(main=main_ep), log).label == "local-main"
    big = route("data_reviewer", 30000, both, log)
    assert big.label == "local-main" and big.trim  # a large pack never moves it to small
    assert "overwatch is the main model" in big.reason
    assert route("report_note", 100, both, log).label == "local-small"


# ---------------------------------------------------------------- the review command


def campaign_with_evidence(tmp_path, pack=None):
    cdir = tmp_path / "camp"
    rdir = cdir / "rounds" / "02" / "review"
    rdir.mkdir(parents=True)
    (rdir / "evidence.json").write_text(json.dumps(pack or flagged_pack()))
    return cdir, rdir


def test_review_command_with_a_scripted_client_says_so(tmp_path):
    cdir, rdir = campaign_with_evidence(tmp_path)
    script = tmp_path / "review_scripted.json"
    script.write_text(json.dumps([good()]))
    code = main(["review", str(cdir), "--round", "2", "--client", f"scripted:{script}"])
    assert code == 0
    record = json.loads((rdir / "review.json").read_text())
    assert record["reviewed_by"] == SCRIPTED_LABEL and record["valid"]
    assert record["reviewer_kind"] == "scripted"
    assert record["review"]["round_verdict"] == "accept_with_flags"
    md = (rdir / "review.md").read_text()
    assert f"Reviewed by: {SCRIPTED_LABEL}" in md and "| c02 | flag |" in md
    lines = [json.loads(x) for x in (rdir / "transcript.jsonl").read_text().splitlines()]
    assert [x["role"] for x in lines] == ["data_reviewer"]
    assert lines[0]["scope"] == "data_reviewer-r02-1"
    routes = [json.loads(x) for x in (rdir / "model_calls.jsonl").read_text().splitlines()]
    assert [r["label"] for r in routes] == ["local-main"]
    assert not (cdir / "transcript.jsonl").exists()
    assert not (cdir / "model_calls.jsonl").exists()


def test_review_command_qc_only_needs_no_model(tmp_path):
    cdir, rdir = campaign_with_evidence(tmp_path)
    assert main(["review", str(cdir), "--round", "2", "--client", "qc-only"]) == 0
    record = json.loads((rdir / "review.json").read_text())
    assert record["reviewed_by"] == QC_ONLY_LABEL and record["model"] is None
    assert f"Reviewed by: {QC_ONLY_LABEL}" in (rdir / "review.md").read_text()
    assert not (rdir / "transcript.jsonl").exists()


def test_review_command_names_a_replayed_model(tmp_path):
    cdir, rdir = campaign_with_evidence(tmp_path)
    recording = tmp_path / "recording.jsonl"
    rows = [
        {"role": "decider", "raw": "{}", "model": "test-model"},
        {"role": "data_reviewer", "raw": json.dumps(good()), "model": "test-model"},
    ]
    recording.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert main(["review", str(cdir), "--round", "2", "--client", f"replay:{recording}"]) == 0
    record = json.loads((rdir / "review.json").read_text())
    assert record["reviewed_by"].startswith("model test-model, replayed from ")
    assert record["reviewer_kind"] == "model"


def test_review_command_refuses_a_recording_with_no_reviewer_replies(tmp_path, capsys):
    cdir, rdir = campaign_with_evidence(tmp_path)
    recording = tmp_path / "recording.jsonl"
    recording.write_text(json.dumps({"role": "decider", "raw": json.dumps(good())}) + "\n")
    assert main(["review", str(cdir), "--round", "2", "--client", f"replay:{recording}"]) == 2
    assert "no data_reviewer replies" in capsys.readouterr().out
    assert not (rdir / "review.json").exists()


def test_review_command_without_a_valid_review_needs_a_person(tmp_path):
    cdir, rdir = campaign_with_evidence(tmp_path)
    bad = good()
    _accept_low(bad)
    script = tmp_path / "bad.json"
    script.write_text(json.dumps([bad, bad, bad]))
    assert main(["review", str(cdir), "--round", "2", "--client", f"scripted:{script}"]) == 1
    record = json.loads((rdir / "review.json").read_text())
    assert record["review"] is None and record["note"].startswith(NEEDS_PERSON)
    assert "No valid review" in (rdir / "review.md").read_text()


def test_review_command_without_evidence_fails(tmp_path, capsys):
    assert main(["review", str(tmp_path), "--round", "1", "--client", "qc-only"]) == 2
    assert "no evidence pack" in capsys.readouterr().out


def test_who_reviewed_counts_replies_and_treats_a_missing_name_as_scripted():
    assert who_reviewed("live", {"replies": 0, "model": "some-model"}) == NO_REPLY_LABEL
    assert who_reviewed("live", {"replies": 2, "model": ""}) == UNNAMED_LABEL
    assert who_reviewed("live", {"replies": 1, "model": "unknown"}) == UNNAMED_LABEL
    assert who_reviewed("scripted:x", {"replies": 1, "model": "scripted"}) == SCRIPTED_LABEL
    named = who_reviewed("live", {"replies": 1, "model": "m", "label": "local-main"})
    assert named == "model m (local-main)"


def test_a_second_review_gets_a_fresh_transcript_scope(tmp_path):
    t = Transcript(tmp_path / "transcript.jsonl")
    assert next_scope(t, 3) == "data_reviewer-r03-1"
    t.append({"scope": "data_reviewer-r03-1", "attempt": 1, "call": 1, "raw": "{}"})
    t.append({"scope": "data_reviewer-r03-1", "attempt": 2, "call": 1, "raw": "{}"})
    assert next_scope(t, 3) == "data_reviewer-r03-2"
    assert next_scope(t, 4) == "data_reviewer-r04-1"


def test_eval_reviewer_refuses_a_model_tag_without_the_live_client(tmp_path):
    script = tmp_path / "s.json"
    script.write_text("[]")
    out = tmp_path / "page.md"
    args = ["eval-reviewer", "--client", f"scripted:{script}", "--model", "tag", "--out", str(out)]
    assert main(args) == 2


def test_eval_reviewer_with_a_scripted_client_needs_its_own_out_path(tmp_path, capsys):
    script = tmp_path / "s.json"
    script.write_text("[]")
    assert main(["eval-reviewer", "--client", f"scripted:{script}"]) == 2
    out = capsys.readouterr().out
    assert "does not write docs/reviewer_eval.md" in out and "--out" in out


def _no_evaluate(monkeypatch):
    import labauto.reviewer_eval as reviewer_eval

    def never(*args, **kwargs):
        raise AssertionError("evaluate must not run")

    monkeypatch.setattr(reviewer_eval, "evaluate", never)


def test_eval_reviewer_refuses_a_default_page_by_any_spelling(tmp_path, monkeypatch, capsys):
    from labauto.config import repo_root

    _no_evaluate(monkeypatch)
    script = tmp_path / "s.json"
    script.write_text("[]")
    client = ["--client", f"scripted:{script}"]
    for out, page in (
        ("docs/reviewer_eval.md", "docs/reviewer_eval.md"),
        ("docs/../docs/reviewer_eval.md", "docs/reviewer_eval.md"),
        (str(repo_root() / "docs" / "reviewer_eval.md"), "docs/reviewer_eval.md"),
        ("docs/reviewer_eval_no_hints.md", "docs/reviewer_eval_no_hints.md"),
    ):
        for prompt in ("default", "no_hints"):
            args = ["eval-reviewer", *client, "--prompt", prompt, "--out", out]
            assert main(args) == 2, (out, prompt)
            assert f"does not write {page}." in capsys.readouterr().out
    assert main(["eval-reviewer", *client, "--prompt", "no_hints"]) == 2
    assert "does not write docs/reviewer_eval_no_hints.md." in capsys.readouterr().out


def test_evaluate_refuses_a_default_page_for_a_client_that_is_not_live(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # so a broken guard could only write inside tmp_path
    for prompt, page in (("default", "docs/reviewer_eval.md"), ("no_hints", None)):
        with pytest.raises(ValueError, match="only a live client writes docs/reviewer_eval"):
            evaluate(
                ScriptedClient([good()]),
                None,
                1,
                tmp_path / "runs",
                Path(page) if page else None,
                "cmd",
                CASES,
                fake_build,
                prompt=prompt,
            )
    assert not (tmp_path / "docs").exists() and not (tmp_path / "runs").exists()


def test_eval_reviewer_live_no_hints_run_writes_its_own_default_page(monkeypatch):
    import labauto.cli_plan as cli_plan
    import labauto.config as config
    import labauto.reviewer_eval as reviewer_eval

    seen = {}

    def fake_evaluate(client, settings, **kwargs):
        seen.update(kwargs)
        return reviewer_eval.EvalResult(kwargs["out_path"], 1, 0)

    monkeypatch.setattr(config, "load_settings", lambda: Settings())
    monkeypatch.setattr(
        cli_plan, "make_clients", lambda spec, settings: cli_plan.Clients(None, None, spec)
    )
    monkeypatch.setattr(reviewer_eval, "evaluate", fake_evaluate)
    assert main(["eval-reviewer", "--prompt", "no_hints"]) == 0
    assert str(seen["out_path"]) == "docs/reviewer_eval_no_hints.md"
    assert seen["prompt"] == "no_hints" and "--prompt no_hints" in seen["command"]
    seen.clear()
    assert main(["eval-reviewer"]) == 0
    assert str(seen["out_path"]) == "docs/reviewer_eval.md" and seen["prompt"] == "default"
    assert "--prompt" not in seen["command"]
    assert seen["baseline_seeds"] == BASELINE_SEEDS == 10 and seen["workers"] >= 1
    assert "--baseline-seeds" not in seen["command"]


def test_eval_reviewer_prompt_choices_match_the_prompt_files():
    import argparse

    from labauto.cli_review import add_parsers

    parser = argparse.ArgumentParser()
    add_parsers(parser.add_subparsers())
    sub = parser._subparsers._group_actions[0].choices["eval-reviewer"]
    choices = next(a.choices for a in sub._actions if a.dest == "prompt")
    assert list(choices) == list(PROMPTS)
    seeds = next(a.default for a in sub._actions if a.dest == "baseline_seeds")
    assert seeds == BASELINE_SEEDS


def test_both_prompts_name_every_fault_kind():
    """The page says the no_hints prompt still names the fault kinds. It does."""
    for name in PROMPTS.values():
        text = load_prompt(name)
        assert all(kind in text for kind in FAULT_KINDS), name


# ---------------------------------------------------------------- the evaluation page


CASES = [
    {"id": "one_injection", "lcms_faults": ["injection_failure"], "nmr_faults": [], "seed": 5},
    {"id": "clean", "lcms_faults": [], "nmr_faults": [], "seed": 6},
]


def fake_build(case, out_dir):
    """Stands in for paired.build_case: a hand-made pack and its true fault log."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if case["id"] == "one_injection":
        return flagged_pack(), [{"kind": "injection_failure", "wells": ["A2"]}]
    return clean_pack(), []


def clean_reply_with_a_false_alarm() -> dict:
    return {
        "round_verdict": "accept_with_flags",
        "conditions": [
            {
                "key": k,
                "verdict": "accept",
                "reason": "Readouts agree.",
                "evidence": [f"conditions.{k}.agreement"],
            }
            for k in GROUPS
        ],
        "suspected_faults": [{"kind": "nmr_impurity", "wells": ["E1"], "reason": "A guess."}],
        "summary": "Looks fine.",
    }


def test_eval_page_from_a_scripted_client_carries_the_banner_and_the_scores(tmp_path):
    client = ScriptedClient([good(), clean_reply_with_a_false_alarm()])
    out = tmp_path / "reviewer_eval.md"
    result = evaluate(
        client,
        None,
        trials=1,
        runs_dir=tmp_path / "runs",
        out_path=out,
        command="labauto eval-reviewer --trials 1 --client scripted:x.json",
        cases=CASES,
        build=fake_build,
    )
    assert result.path == out and result.trials == 2 and result.outages == 0
    page = out.read_text()
    lines = page.splitlines()
    assert lines[0] == "# Reviewer evaluation" and lines[2] == SCRIPTED_BANNER
    assert (
        "| Case | Faults injected | True faults | QC rules: caught | QC rules: false alarms "
        "| QC plus statistics: caught | QC plus statistics: false alarms "
        "| Overwatch alone: caught | Overwatch alone: false alarms "
        "| QC rules or overwatch: caught | QC rules or overwatch: false alarms "
        "| QC plus statistics or overwatch: caught "
        "| QC plus statistics or overwatch: false alarms "
        "| No valid review | Ended on an outage |" in page
    )
    assert (
        "| Overwatch alone: well precision | Overwatch alone: well recall "
        "| QC rules or overwatch: well precision | QC rules or overwatch: well recall "
        "| QC plus statistics or overwatch: well precision "
        "| QC plus statistics or overwatch: well recall |" in page
    )
    assert "QC plus overwatch" not in page and "Either" not in page
    assert (
        "| one_injection | injection_failure | 1 | 1 of 1 | 0 | 1 of 1 | 0 | 1 of 1 | 0 "
        "| 1 of 1 | 0 | 1 of 1 | 0 | 0 of 1 | 0 of 1 |" in page
    )
    assert (
        "| clean | none | 0 | 0 of 0 | 0 | 0 of 0 | 0 | 0 of 0 | 1 | 0 of 0 | 1 | 0 of 0 | 1 "
        "| 0 of 1 | 0 of 1 |" in page
    )
    assert (
        "| **Total** | | 1 | 1 of 1 | 0 | 1 of 1 | 0 | 1 of 1 | 1 | 1 of 1 | 1 | 1 of 1 | 1 "
        "| 0 of 2 | 0 of 2 |" in page
    )
    assert (
        "| one_injection | 1 | 1.00 (1 of 1) | 1.00 (1 of 1) | 1.00 (1 of 1) | 1.00 (1 of 1) "
        "| 1.00 (1 of 1) | 1.00 (1 of 1) | 1.00 (1 of 1) | 1.00 (1 of 1) | 1.00 (1 of 1) "
        "| 1.00 (1 of 1) |" in page
    )
    assert (
        "| clean | 0 | n/a (0 of 0) | n/a (0 of 0) | n/a (0 of 0) | n/a (0 of 0) "
        "| 0.00 (0 of 1) | n/a (0 of 0) | 0.00 (0 of 1) | n/a (0 of 0) | 0.00 (0 of 1) "
        "| n/a (0 of 0) |" in page
    )
    assert (
        "| **Total** | 1 | 1.00 (1 of 1) | 1.00 (1 of 1) | 1.00 (1 of 1) | 1.00 (1 of 1) "
        "| 0.50 (1 of 2) | 1.00 (1 of 1) | 0.50 (1 of 2) | 1.00 (1 of 1) | 0.50 (1 of 2) "
        "| 1.00 (1 of 1) |" in page
    )
    # The how-to-read section explains both pooled columns and what a model can still add.
    assert "QC plus statistics or overwatch pools the faults QC plus statistics and" in page
    assert "a model can only show a gain there in well recall" in page
    assert "shows it only in Overwatch alone against QC plus statistics" in page
    assert "Both prompts name the fault kinds" in page and "not free of hints" in page
    assert (
        "The rules' constants are fixed in review.py: STATS_MIN_N (2), DRIFT_LOW_SHARE (0.8), "
        "DRIFT_MEDIAN_MAX (-3), IMPURITY_FLOOR (1.5), IMPURITY_SPREADS (4)." in page
    )
    assert "This run skipped the baseline seed sweep (--baseline-seeds 0)" in page
    assert "## Baselines across" not in page and "set before" not in page
    assert "on scored cases" not in page  # no outage, so no second count
    assert "in 3 attempts (up to 6 replies" in page
    assert "## How to read this" in page and "2 cases with 1 true fault is a small set" in page
    assert "Overwatch prompt: default (" in page
    assert "fault signatures, and some of them resemble how the hard cases were built" in page
    assert "Overwatch model, as reported by the endpoint: scripted" in page
    assert "nmr_impurity (E1): false alarm (1 extra well)" in page
    assert "injection_failure (A2): caught" in page
    assert "- QC plus statistics: round verdict accept_with_flags. Named: injection_failure" in page
    assert "synthetic" in page
    run_dir = next((tmp_path / "runs").iterdir())
    case_dir = run_dir / "one_injection" / "reviewer"
    for name in (
        "evidence.json",
        "true_faults.json",
        "qc_only.json",
        "qc_stats.json",
        "trial_01.json",
    ):
        assert (case_dir / name).exists(), name
    assert json.loads((case_dir / "qc_stats.json").read_text())["problems"] == []
    assert json.loads((run_dir / "run.json").read_text())["prompt"] == "default"
    assert json.loads((case_dir / "trial_01.json").read_text())["info"]["prompt"] == "default"


def test_eval_page_from_a_client_with_no_model_name_carries_a_banner(tmp_path):
    client = ScriptedClient([good(), clean_reply_with_a_false_alarm()], model="")
    out = tmp_path / "eval.md"
    evaluate(client, None, 1, tmp_path / "runs", out, "cmd", CASES, fake_build)
    page = out.read_text()
    assert page.splitlines()[2] == UNNAMED_BANNER
    assert "as reported by the endpoint: unknown (no name reported)" in page


def test_trials_that_end_on_an_outage_are_counted_apart_and_not_scored(tmp_path):
    client = ScriptedClient([good()])  # the second case finds the client silent
    out = tmp_path / "eval.md"
    result = evaluate(client, None, 1, tmp_path / "runs", out, "cmd", CASES, fake_build)
    assert result.outages == 1 and result.trials == 2
    page = out.read_text()
    assert "**1 of 2 overwatch trials ended because the endpoint stopped answering" in page
    assert (
        "| clean | none | 0 | 0 of 0 | 0 | 0 of 0 | 0 | 0 of 0 | 0 | 0 of 0 | 0 | 0 of 0 | 0 "
        "| 0 of 0 | 1 of 1 |" in page
    )
    assert (
        "| **Total** | | 1; 1 on scored cases | 1 of 1; 1 of 1 on scored cases "
        "| 0; 0 on scored cases | 1 of 1; 1 of 1 on scored cases | 0; 0 on scored cases "
        "| 1 of 1 | 0 | 1 of 1 | 0 | 1 of 1 | 0 | 0 of 1 | 1 of 2 |" in page
    )
    assert "left out of the overwatch columns and the two pooled columns" in page
    assert "clean, trial 1: ScriptExhausted" in page


def test_after_an_outage_the_total_row_also_counts_the_baselines_on_scored_cases(tmp_path):
    # The clean case is scored; the case with the fault finds the client silent.
    client = ScriptedClient([clean_reply_with_a_false_alarm()])
    out = tmp_path / "eval.md"
    cases = [CASES[1], CASES[0]]
    result = evaluate(client, None, 1, tmp_path / "runs", out, "cmd", cases, fake_build)
    assert result.outages == 1
    page = out.read_text()
    assert "The Total rows also give each baseline over the scored cases only" in page
    assert (
        "| **Total** | | 1; 0 on scored cases | 1 of 1; 0 of 0 on scored cases "
        "| 0; 0 on scored cases | 1 of 1; 0 of 0 on scored cases | 0; 0 on scored cases "
        "| 0 of 0 | 1 | 0 of 0 | 1 | 0 of 0 | 1 | 0 of 1 | 1 of 2 |" in page
    )
    assert (
        "| **Total** | 1; 0 on scored cases | 1.00 (1 of 1); n/a (0 of 0) on scored cases "
        "| 1.00 (1 of 1); n/a (0 of 0) on scored cases "
        "| 1.00 (1 of 1); n/a (0 of 0) on scored cases "
        "| 1.00 (1 of 1); n/a (0 of 0) on scored cases "
        "| 0.00 (0 of 1) | n/a (0 of 0) | 0.00 (0 of 1) | n/a (0 of 0) | 0.00 (0 of 1) "
        "| n/a (0 of 0) |" in page
    )


def test_eval_reviewer_exits_non_zero_after_an_outage(tmp_path, monkeypatch, capsys):
    import labauto.reviewer_eval as reviewer_eval

    real = reviewer_eval.evaluate

    def small_evaluate(client, settings, **kwargs):
        return real(client, settings, cases=CASES, build=fake_build, **{**kwargs, "workers": 1})

    monkeypatch.setattr(reviewer_eval, "evaluate", small_evaluate)
    script = tmp_path / "s.json"
    out = tmp_path / "page.md"
    args = ["eval-reviewer", "--runs", str(tmp_path / "runs"), "--out", str(out)]

    script.write_text(json.dumps([good(), clean_reply_with_a_false_alarm()]))
    assert main([*args, "--client", f"scripted:{script}"]) == 0
    assert out.exists()

    script.write_text(json.dumps([good()]))
    assert main([*args, "--client", f"scripted:{script}"]) == 1
    assert "1 of 2 overwatch trials ended" in capsys.readouterr().out


def test_eval_page_marks_hard_cases(tmp_path):
    hard = [{**CASES[0], "hard": True, "note": "made quiet on purpose"}]
    out = tmp_path / "eval.md"
    evaluate(ScriptedClient([good()]), None, 1, tmp_path / "runs", out, "cmd", hard, fake_build)
    page = out.read_text()
    assert "| one_injection (hard) | injection_failure |" in page
    assert "- Hard case, made to slip past the fixed flags: made quiet on purpose" in page
    # Here the plain rules already catch the hard case, and the page says so.
    assert "QC rules catch 1 of 1 true faults and QC plus statistics catch 1 of 1" in page
    assert "so here the plain rules leave no gap" in page


def quiet_build(case, out_dir):
    """A hard case the QC rules miss: drift with no flag, and a small gap in well F1."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pack = drift_quiet_pack()
    _set_gap(pack, "F1", 1.6)
    return pack, [{"kind": "plate_drift", "wells": []}, {"kind": "nmr_impurity", "wells": ["F1"]}]


def test_eval_page_shows_the_gap_the_statistics_close(tmp_path):
    hard = [{"id": "quiet", "lcms_faults": [], "nmr_faults": [], "seed": 1, "hard": True}]
    reply = {
        "round_verdict": "accept_with_flags",
        "conditions": [
            {"key": k, "verdict": "flag", "reason": "Reads low.", "evidence": [f"conditions.{k}"]}
            for k in GROUPS
        ],
        "suspected_faults": [],
        "summary": "Nothing named.",
    }
    out = tmp_path / "eval.md"
    evaluate(ScriptedClient([reply]), None, 1, tmp_path / "runs", out, "cmd", hard, quiet_build)
    page = out.read_text()
    assert (
        "| quiet (hard) | none | 2 | 0 of 2 | 0 | 2 of 2 | 0 | 0 of 2 | 0 | 0 of 2 | 0 "
        "| 2 of 2 | 0 | 0 of 1 | 0 of 1 |" in page
    )
    assert (
        "| quiet (hard) | 1 | n/a (0 of 0) | 0.00 (0 of 1) | 1.00 (1 of 1) | 1.00 (1 of 1) "
        "| n/a (0 of 0) | 0.00 (0 of 1) | n/a (0 of 0) | 0.00 (0 of 1) | 1.00 (1 of 1) "
        "| 1.00 (1 of 1) |" in page
    )
    assert "QC rules catch 0 of 2 true faults and QC plus statistics catch 2 of 2" in page
    assert "so simple statistics close that gap here" in page
    assert "compare it with QC plus statistics too" in page
    assert "with the hard-case recipes known" in page
    assert "Over all cases, QC plus statistics catch 2 of 2 true faults" in page
    assert "So on these case plates QC plus statistics leave no fault for a model to add" in page
    assert "- QC rules, missed: nmr_impurity in quiet; plate_drift in quiet." in page


def test_a_no_hints_run_sends_the_bare_prompt_and_says_so(tmp_path):
    client = ScriptedClient([good(), clean_reply_with_a_false_alarm()])
    out = tmp_path / "eval.md"
    evaluate(client, None, 1, tmp_path / "runs", out, "cmd", CASES, fake_build, prompt="no_hints")
    assert all(c[1][0]["content"] == load_prompt(PROMPTS["no_hints"]) for c in client.calls)
    page = out.read_text()
    assert "Overwatch prompt: no_hints (" in page
    assert "fault signatures section removed" in page
    assert "lists fault signatures that resemble how the hard cases were built" in page
    run_dir = next((tmp_path / "runs").iterdir())
    assert json.loads((run_dir / "run.json").read_text())["prompt"] == "no_hints"
    with pytest.raises(ValueError, match="unknown reviewer prompt"):
        evaluate(client, None, 1, tmp_path / "runs", out, "cmd", CASES, fake_build, prompt="x")


def test_eval_failure_notes_quote_real_rejected_replies(tmp_path):
    bad = good()
    _invented_reason(bad)
    client = ScriptedClient([bad, good()])
    out = tmp_path / "eval.md"
    evaluate(client, None, 1, tmp_path / "runs", out, "cmd", CASES[:1], fake_build)
    page = out.read_text()
    assert "- invented_number: 1 time, in one_injection." in page
    assert "61.7" in page


def test_the_stats_pooled_column_adds_the_overwatch_to_the_best_fixed_rules(tmp_path):
    hard = [{"id": "quiet", "lcms_faults": [], "nmr_faults": [], "seed": 1, "hard": True}]
    reply = {
        "round_verdict": "accept_with_flags",
        "conditions": [
            {"key": k, "verdict": "flag", "reason": "Reads low.", "evidence": [f"conditions.{k}"]}
            for k in GROUPS
        ],
        "suspected_faults": [
            {"kind": "plate_drift", "wells": [], "reason": "LC/MS reads below NMR everywhere."},
            {"kind": "nmr_impurity", "wells": ["G1"], "reason": "Its singlet reads high."},
        ],
        "summary": "Drift, and perhaps an impurity in G1.",
    }
    out = tmp_path / "eval.md"
    evaluate(ScriptedClient([reply]), None, 1, tmp_path / "runs", out, "cmd", hard, quiet_build)
    page = out.read_text()
    # The overwatch catches the drift and names one wrong well. The QC rules name nothing, so
    # pooled with them nothing changes. QC plus statistics catch both faults, so pooled with
    # them the model adds no fault, only its false alarm.
    assert (
        "| quiet (hard) | none | 2 | 0 of 2 | 0 | 2 of 2 | 0 | 1 of 2 | 1 | 1 of 2 | 1 | 2 of 2 "
        "| 1 | 0 of 1 | 0 of 1 |" in page
    )
    assert (
        "| quiet (hard) | 1 | n/a (0 of 0) | 0.00 (0 of 1) | 1.00 (1 of 1) | 1.00 (1 of 1) "
        "| 0.00 (0 of 1) | 0.00 (0 of 1) | 0.00 (0 of 1) | 0.00 (0 of 1) | 0.50 (1 of 2) "
        "| 1.00 (1 of 1) |" in page
    )
    trial = json.loads(next((tmp_path / "runs").glob("*/quiet/reviewer/trial_01.json")).read_text())
    assert [f["kind"] for f in trial["either_stats"]["caught"]] == ["plate_drift", "nmr_impurity"]
    assert [f["kind"] for f in trial["either"]["caught"]] == ["plate_drift"]


def seeded_build(case, out_dir):
    """Like fake_build, but the plate depends on the seed, as paired.build_case's does.

    On an odd seed the clean plate has a well whose singlet reads 3 points above
    its doublet, which QC plus statistics name as an impurity: a false alarm.
    A case lacks its fault, and raises as paired.build_case does, on a seed in
    its no_fault_on list, or on every seed when it has never.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    seed = case["seed"]
    if case.get("never") or seed in case.get("no_fault_on", []):
        raise ValueError(
            f"fault case {case['id']}: injection_failure did not happen with seed {seed}. "
            "Pick a seed where it does."
        )
    if case["id"] != "clean":
        return flagged_pack(), [{"kind": "injection_failure", "wells": ["A2"]}]
    pack = clean_pack()
    if seed % 2:
        _set_gap(pack, "F1", 3.0)
    return pack, []


def _table(page: str, heading: str) -> list[list[str]]:
    """The cells of the first table after a heading, header and divider left out."""
    lines = page.split(heading, 1)[1].splitlines()
    start = next(i for i, x in enumerate(lines) if x.startswith("|"))
    rows = []
    for x in lines[start + 2 :]:
        if not x.startswith("|"):
            break
        rows.append([c.strip() for c in x.strip("|").split("|")])
    return rows


def _numbers(cell: str) -> tuple[int, ...]:
    """The counts in a cell: "3", "2 of 4", or "0.50 (2 of 4)"."""
    inner = cell.split("(")[-1].rstrip(")")
    return tuple(int(x) for x in inner.split(" of "))


def test_the_baseline_sweep_runs_both_baselines_on_other_seeds_with_no_model(tmp_path):
    seen = []

    def recording_build(case, out_dir):
        seen.append(case["seed"])
        return seeded_build(case, out_dir)

    client = ScriptedClient([good(), clean_reply_with_a_false_alarm()])
    out = tmp_path / "eval.md"
    result = evaluate(
        client, None, 1, tmp_path / "runs", out, "cmd", CASES, recording_build, baseline_seeds=2
    )
    # One model reply per case. The sweep built four more plates and asked the model nothing.
    assert len(client.calls) == 2
    assert seen == [5, 6, 5001, 5002, 6001, 6002]
    page = out.read_text()
    assert "## Baselines across 2 extra seeds per case" in page
    assert "No model runs in this section, so it costs no model calls." in page
    assert (
        "The section Baselines across 2 extra seeds per case shows how both baselines do on "
        "seeds the case table never used." in page
    )
    assert (
        "On the 4 sweep plates, QC plus statistics catch 2 of 2 true faults and QC rules catch "
        "2 of 2." in page
    )
    assert (
        "| Case | Plates | Seeds skipped | QC rules: faults caught | QC rules: well recall "
        "| QC rules: plates with a false alarm | QC plus statistics: faults caught "
        "| QC plus statistics: well recall | QC plus statistics: plates with a false alarm |"
        in page
    )
    rows = _table(page, "## Baselines across 2 extra seeds per case")
    assert rows == [
        ["one_injection", "2", "0", "2 of 2", "1.00 (2 of 2)", "0 of 2"]
        + ["2 of 2", "1.00 (2 of 2)", "0 of 2"],
        ["clean", "2", "0", "0 of 0", "n/a (0 of 0)", "0 of 2"]
        + ["0 of 0", "n/a (0 of 0)", "1 of 2"],
        ["**Total**", "4", "0", "2 of 2", "1.00 (2 of 2)", "0 of 4"]
        + ["2 of 2", "1.00 (2 of 2)", "1 of 4"],
    ]
    # The Total row adds up the case rows, column by column.
    *cases, total = rows
    for col in range(1, len(total)):
        sums = [sum(x) for x in zip(*(_numbers(r[col]) for r in cases), strict=True)]
        assert list(_numbers(total[col])) == sums, total[col]
    assert (
        "False alarms by kind, counted in plates. QC rules: nmr_impurity on 0 of 4 plates with "
        "no true nmr_impurity fault. QC plus statistics: nmr_impurity on 1 of 4 plates with no "
        "true nmr_impurity fault." in page
    )
    # Each case used other seeds, never its own, and the folders hold what the table counts.
    run_dir = next((tmp_path / "runs").iterdir())
    assert json.loads((run_dir / "run.json").read_text())["baseline_seeds"] == 2
    sweep = json.loads((run_dir / SWEEP_DIR / "sweep.json").read_text())
    assert sweep["model_calls"] == 0 and sweep["n"] == 2
    assert result.sweep == sweep
    used = {e["case"]["id"]: [p["seed"] for p in e["plates"]] for e in sweep["cases"]}
    assert used == {"one_injection": [5001, 5002], "clean": [6001, 6002]}
    assert used["one_injection"] == [sweep_seed(5, k) for k in (1, 2)]
    assert all(e["case"]["seed"] not in used[e["case"]["id"]] for e in sweep["cases"])
    for seed in (5001, 5002):
        rdir = run_dir / SWEEP_DIR / "one_injection" / f"seed_{seed}" / "reviewer"
        for name in ("evidence.json", "true_faults.json", "qc_only.json", "qc_stats.json"):
            assert (rdir / name).exists(), name
    # A sweep plate built like the case's plate scores like it.
    case_dir = run_dir / "one_injection" / "reviewer"
    for key, name in (("qc", "qc_only.json"), ("qc_stats", "qc_stats.json")):
        case_score = json.loads((case_dir / name).read_text())["score"]
        assert sweep["cases"][0]["plates"][0][key]["score"] == case_score


def test_the_sweep_skips_a_seed_without_the_fault_and_takes_the_next(tmp_path):
    cases = [
        {**CASES[0], "no_fault_on": [5001]},
        {"id": "never", "lcms_faults": ["injection_failure"], "nmr_faults": [], "seed": 7},
    ]
    cases[1]["never"] = True
    sweep = baseline_sweep(cases, seeded_build, tmp_path, 2)
    one, never = sweep["cases"]
    assert [p["seed"] for p in one["plates"]] == [5002, 5003]
    assert [p["seed"] for p in one["skipped"]] == [5001]
    assert (
        "did not happen"
        in json.loads(
            (tmp_path / SWEEP_DIR / "one_injection" / "seed_5001" / "skipped.json").read_text()
        )["reason"]
    )
    # A case whose fault never happens gives up after SEED_TRIES x n seeds, and says so.
    assert never["plates"] == [] and len(never["skipped"]) == SEED_TRIES * 2
    lines = sweep_section(sweep, "runs/x")
    assert "| one_injection | 2 | 1 | 2 of 2 | 1.00 (2 of 2) | 0 of 2 |" in "\n".join(lines)
    assert (
        "| never | 0 | 10 | 0 of 0 | n/a (0 of 0) | 0 of 0 | 0 of 0 | n/a (0 of 0) | 0 of 0 |"
        in lines
    )

    def broken(case, out_dir):
        raise ValueError("some other problem")

    with pytest.raises(ValueError, match="some other problem"):
        baseline_sweep(CASES, broken, tmp_path / "broken", 1)


def test_sweep_seeds_never_repeat_the_case_seed():
    seeds = [sweep_seed(101, k) for k in range(1, SEED_STRIDE)]
    assert 101 not in seeds and len(set(seeds)) == len(seeds) and seeds[0] == 101 * SEED_STRIDE + 1
    for k in (0, SEED_STRIDE):
        with pytest.raises(ValueError):
            sweep_seed(101, k)


def test_the_sweep_gives_the_same_plates_in_worker_processes(tmp_path):
    one = baseline_sweep(CASES, seeded_build, tmp_path / "one", 2, workers=1)
    two = baseline_sweep(CASES, seeded_build, tmp_path / "two", 2, workers=2)
    assert one["cases"] == two["cases"]
    assert sweep_section(one, "x") == sweep_section(two, "x")


def test_eval_reviewer_baseline_seeds_zero_skips_the_sweep(tmp_path, monkeypatch, capsys):
    import labauto.reviewer_eval as reviewer_eval

    real = reviewer_eval.evaluate

    def small_evaluate(client, settings, **kwargs):
        return real(client, settings, cases=CASES, build=fake_build, **{**kwargs, "workers": 1})

    monkeypatch.setattr(reviewer_eval, "evaluate", small_evaluate)
    script = tmp_path / "s.json"
    script.write_text(json.dumps([good(), clean_reply_with_a_false_alarm()]))
    out = tmp_path / "page.md"

    def args(runs: str, seeds: str) -> list[str]:
        return ["eval-reviewer", "--client", f"scripted:{script}", "--out", str(out)] + [
            "--runs",
            str(tmp_path / runs),
            "--baseline-seeds",
            seeds,
        ]

    assert main(args("none", "0")) == 0
    page = out.read_text()
    assert "## Baselines across" not in page and "--baseline-seeds 0" in page
    assert "This run skipped the baseline seed sweep (--baseline-seeds 0)" in page
    assert "Baseline sweep" not in capsys.readouterr().out
    run_dir = next((tmp_path / "none").iterdir())
    assert not (run_dir / SWEEP_DIR).exists()

    assert main(args("two", "2")) == 0
    page = out.read_text()
    assert "## Baselines across 2 extra seeds per case" in page and "--baseline-seeds 2" in page
    printed = capsys.readouterr().out
    assert "Baseline sweep: 4 plates on up to 2 extra seeds per case (0 seeds skipped)" in printed
    assert "with no model calls." in printed

    for bad in ("-1", "101"):
        assert main(args("bad", bad)) == 2
        assert "--baseline-seeds must be 0 to 100" in capsys.readouterr().out
    assert not (tmp_path / "bad").exists()


def test_the_impurity_false_alarm_figure_is_called_a_bound_and_points_to_the_sweep():
    doc = " ".join(qc_stats_review.__doc__.split())
    assert "about 0.2% for any of 70 wells. That is an estimate from normal theory" in doc
    assert "The measured rate in this simulator is higher" in doc
    assert '"Baselines across N extra seeds per case"' in doc
    assert "before the rules ran" not in doc


def test_eval_writes_nothing_when_no_model_answers(tmp_path):
    out = tmp_path / "eval.md"
    with pytest.raises(NoModelAnswered):
        evaluate(Down(), None, 1, tmp_path / "runs", out, "cmd", CASES, fake_build)
    assert not out.exists()


# ---------------------------------------------------------------- real fault cases


@pytest.mark.slow
def test_the_baselines_checks_and_scores_agree_on_every_fault_case(tmp_path):
    from labauto import paired

    for case in paired.FAULT_CASES:
        evidence, true_faults = paired.build_case(case, tmp_path / case["id"])
        pack = pack_dict(evidence)
        truth = [f.model_dump() for f in true_faults]
        assert truth_in(pack) == [], case["id"]

        for review in (qc_only_review(pack), qc_stats_review(pack)):
            assert check_review(review, pack) == [], case["id"]
            # A reply that passes the checks is taken on the first attempt.
            taken, info = review_round(
                pack, ScriptedClient([review.model_dump(mode="json")]), "local-main"
            )
            assert taken == review and len(info["attempts"]) == 1, case["id"]

            s = score(truth, review.suspected_faults)
            assert s["true"] == len(merge_faults(truth)) == len(truth)
            assert len(s["caught"]) + len(s["missed"]) == s["true"]
            true_wells = sum(len(f["wells"]) for f in truth if f["kind"] in WELL_FAULTS)
            w = s["wells"]
            assert w["true"] == true_wells and w["hits"] + w["missed"] == true_wells
            named_wells = sum(
                len(e["wells"])
                for e in merge_faults(review.suspected_faults)
                if e["kind"] in WELL_FAULTS
            )
            assert w["hits"] + w["extra"] == named_wells
            assert len(s["false_alarms"]) == _false_alarm_total(s)
            # Naming the same faults twice changes nothing.
            assert score(truth, [*review.suspected_faults, *review.suspected_faults]) == s

            if not case["lcms_faults"] and not case["nmr_faults"]:
                assert truth == [] and s["caught"] == [] and s["missed"] == []
                assert s["false_alarms"] == [], case["id"]
        if case["id"] == "injection_failure":
            assert [f["kind"] for f in truth] == ["injection_failure"]
