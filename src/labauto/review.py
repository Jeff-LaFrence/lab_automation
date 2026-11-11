"""The overwatch reviewer: a generalist model reads one round's evidence pack and judges it.

This is layer 3 of the paired readout. Purpose-built evaluators (the classical
NMR lineshape fitter, the LC/MS analysis) do the per-spectrum work. The
overwatch is the generalist main model. It sees only the evidence pack
(evidence.json): LC/MS and NMR numbers per condition and per well, with the
flags that fixed code raised. It never sees raw data or the simulators' truth.
It gives a verdict per condition, names the faults it suspects, and cites
evidence paths such as "conditions.c01.lcms.mean".

The reply must match DataReview exactly: a misspelled or extra key is a parse
error, sent back to the model. Then check_review checks these rules, and only
these (CHECKED_RULES holds the same list, for review.md):

1. Every condition in the pack is judged exactly once, and no other key is.
2. Every condition verdict and every suspected fault has a non-empty reason.
3. Every evidence path resolves in the pack exactly as written. A bare
   section ("conditions", "wells", "plate") is refused. Each condition cites
   at least one path under conditions.<its key> or wells.<one of its wells>.
4. Numbers are copied, sign included. A condition reason may only use numbers
   found under conditions.<its key>, in its wells' rows, in the plate block,
   or at a path it cites. A well fault's reason (injection_failure,
   nmr_impurity) may only use numbers in its named wells' rows and the plate
   block. Any other fault touches the plate, so its reason may also use
   numbers under conditions (a drift reason quotes condition differences).
   The summary may only use numbers in the pack. Well names and condition
   keys are names, not numbers. Non-ASCII digits next to number-like text are
   refused.
5. No condition is "accept" when it carries LOW_REPLICATES, its agreement is
   "insufficient" or "disagree", it carries LCMS_NMR_DISAGREE, or the plate
   carries PLATE_QC_FAIL.
6. A plate with PLATE_QC_FAIL has round verdict "rerun_plate". Round verdict
   "accept" needs every condition accepted, no plate flag, and no fault named.
7. Every well a fault names is in the pack. injection_failure and
   nmr_impurity name at least one well.
8. The summary has at most 80 words.

A reply that fails gets the problems back, at most twice (3 attempts; each
attempt may retry once on a reply that is not readable JSON, so up to 6
replies). After that a person takes over.

qc_only_review is the baseline with no model: fixed rules map flags to
verdicts and fault kinds. qc_stats_review is a second baseline with no
model: the same rules plus two plate statistics, one for drift and one for
an impurity under the NMR singlet. score compares suspected faults with the
simulators' own fault log, well by well, for the fault detection evaluation.

The reviewer has two prompts (PROMPTS). "default" lists fault signatures,
some of which resemble how the evaluation's hard cases were built.
"no_hints" is the same prompt with that section removed.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from labauto.agents.llm import EndpointUnavailable, ScriptExhausted, Transcript, chat
from labauto.agents.planner import load_prompt

ROLE = "data_reviewer"
# The reviewer's system prompts, by name. no_hints drops the default prompt's fault signatures.
PROMPTS = {"default": "data_reviewer.md", "no_hints": "data_reviewer_no_hints.md"}
DEFAULT_PROMPT = "default"
FAULT_KINDS = (
    "injection_failure",
    "plate_drift",
    "nmr_broad_lines",
    "nmr_phase_error",
    "nmr_impurity",
)
# Faults that hit single wells. The others touch the whole plate.
WELL_FAULTS = ("injection_failure", "nmr_impurity")
# Named when the readouts disagree and the reviewer cannot tell why.
DISAGREEMENT = "lcms_nmr_disagreement"
MAX_RETRIES = 2
ATTEMPTS = MAX_RETRIES + 1
CALLS_PER_ATTEMPT = 2  # chat() retries once on a reply that is not readable JSON
MAX_SUMMARY_WORDS = 80
NEEDS_PERSON = "NEEDS a person"
# Model names that do not show a real model answered: treated like a scripted client.
NOT_A_MODEL = ("scripted", "unknown", "")
# Keys that would carry the simulators' truth. A pack holding any of them is refused.
TRUTH_KEYS = {"truth", "true_yield", "true_faults", "fault_log", "observed_yield"}
# Lists inside the pack are addressed by these fields, for example wells.E1.
ID_FIELDS = {"conditions": "key", "wells": "well"}
# Paths too broad to count as evidence.
SECTIONS = ("conditions", "wells", "plate")

CHECKED_RULES = (
    "every condition judged exactly once, and no unknown key",
    "every condition verdict and every suspected fault has a reason",
    "every evidence path resolves exactly as written, none is a bare section, and each "
    "condition cites a path under its own key or one of its wells",
    "every number in a condition reason is under that condition, in its wells' rows, in the "
    "plate block, or at a path it cites; in a well fault's reason, in its wells' rows or the "
    "plate block; in a plate-wide fault's reason, also under conditions; in the summary, in the "
    "pack; signs kept; no non-ASCII digits",
    "no accept for a condition with LOW_REPLICATES, LCMS_NMR_DISAGREE, agreement "
    "insufficient or disagree, or on a plate with PLATE_QC_FAIL",
    "PLATE_QC_FAIL means rerun_plate; round accept needs every condition accepted, no plate "
    "flag, and no fault named",
    "every named well is real, and injection_failure and nmr_impurity name wells",
    f"summary of {MAX_SUMMARY_WORDS} words or fewer",
)

# The baseline names a fault only for a flag that points at one fault kind.
FLAG_KINDS = {
    "IS_OUT_OF_RANGE": "injection_failure",
    "NMR_SIGNAL_INCONSISTENT": "nmr_impurity",
    "NMR_BROAD_LINES": "nmr_broad_lines",
    "NMR_PHASE_CORRECTED": "nmr_phase_error",
}


class Closed(BaseModel):
    """A reply part that refuses unknown keys, so a misspelled key is a parse error."""

    model_config = ConfigDict(extra="forbid")


class ConditionVerdict(Closed):
    """The reviewer's verdict on one condition, with the evidence behind it."""

    key: str
    verdict: Literal["accept", "flag", "rerun"]
    reason: str = ""
    evidence: list[str] = Field(default_factory=list)


class SuspectedFault(Closed):
    """A fault the reviewer thinks happened. Plate-wide faults may list no wells."""

    kind: Literal[
        "injection_failure",
        "plate_drift",
        "nmr_broad_lines",
        "nmr_phase_error",
        "nmr_impurity",
        "lcms_nmr_disagreement",
        "other",
    ]
    wells: list[str] = Field(default_factory=list)
    reason: str = ""


class DataReview(Closed):
    """One review of one round: a verdict for the round, one per condition, and suspected faults."""

    round_verdict: Literal["accept", "accept_with_flags", "rerun_plate"]
    conditions: list[ConditionVerdict]
    suspected_faults: list[SuspectedFault] = Field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------- the pack


def pack_dict(evidence: Any) -> dict:
    """The evidence pack as plain JSON data, from a pydantic model or a dict."""
    if hasattr(evidence, "model_dump"):
        return evidence.model_dump(mode="json")
    if isinstance(evidence, dict):
        return json.loads(json.dumps(evidence))
    raise TypeError(f"an evidence pack must be a model or a dict, not {type(evidence).__name__}")


def truth_in(pack: Any, where: str = "") -> list[str]:
    """Paths of any keys that would carry the simulators' truth. Empty means the pack is clean."""
    found = []
    if isinstance(pack, dict):
        for k, v in pack.items():
            here = f"{where}.{k}" if where else str(k)
            if k in TRUTH_KEYS:
                found.append(here)
            found += truth_in(v, here)
    elif isinstance(pack, list):
        for i, v in enumerate(pack):
            found += truth_in(v, f"{where}.{i}")
    return found


_MISSING = object()


def node_at(pack: dict, path: str) -> Any:
    """What a dotted evidence path points at, or _MISSING. The path is used exactly as written.

    Dicts are entered by key. The conditions list is entered by condition key
    and the wells list by well name. Other lists are entered by position or,
    for a list of flags, by the flag itself (plate.lcms_flags.PLATE_QC_FAIL).
    """
    if not isinstance(path, str) or not path:
        return _MISSING
    node: Any = pack
    parent = ""
    for seg in path.split("."):
        if isinstance(node, dict):
            if seg not in node:
                return _MISSING
            node = node[seg]
        elif isinstance(node, list):
            field = ID_FIELDS.get(parent)
            if field is not None and any(isinstance(x, dict) for x in node):
                match = [x for x in node if isinstance(x, dict) and x.get(field) == seg]
                if not match:
                    return _MISSING
                node = match[0]
            elif seg.isdigit() and int(seg) < len(node):
                node = node[int(seg)]
            elif seg in node:
                node = seg
            else:
                return _MISSING
        else:
            return _MISSING
        parent = seg
    return node


def resolves(pack: dict, path: str) -> bool:
    """True when a dotted evidence path, exactly as written, points at something in the pack."""
    return node_at(pack, path) is not _MISSING


def _condition_flags(cond: dict) -> set[str]:
    flags = set(cond.get("flags") or [])
    for side in ("lcms", "nmr"):
        flags |= set((cond.get(side) or {}).get("flags") or [])
    return flags


def _plate_flags(pack: dict) -> set[str]:
    plate = pack.get("plate") or {}
    return set(plate.get("lcms_flags") or []) | set(plate.get("nmr_flags") or [])


def plate_failed(pack: dict) -> bool:
    """True when either readout failed its plate controls."""
    return "PLATE_QC_FAIL" in _plate_flags(pack)


# ---------------------------------------------------------------- numbers

# A run of ASCII digits, with or without a leading dot, even inside a word ("a99", "n=3").
_NUMBER = re.compile(r"(?<![0-9.])\.?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_MINUS = "-\u2212"  # a hyphen or the Unicode minus sign
_NUMBER_LIKE = set("0123456789.,+-\u2212")


def _id_pattern(pack: dict) -> re.Pattern:
    """Well names, condition keys, and "1H": names that hold digits but are not numbers."""
    names = {"1H"}
    names |= {str(w.get("well")) for w in pack.get("wells") or [] if w.get("well")}
    names |= {str(c.get("key")) for c in pack.get("conditions") or [] if c.get("key")}
    alternatives = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z0-9_])(?:{alternatives})(?![A-Za-z0-9_])")


def signed_numbers(text: str) -> list[tuple[str, float, int]]:
    """Each number in the text as (as written, signed value, decimal places).

    A minus sign counts when it sits right before the number and does not
    follow a letter or digit, so "-3.7" is negative and "A2-3" is not.
    """
    out = []
    for m in _NUMBER.finditer(text):
        token, start = m.group(0), m.start()
        mantissa = re.split(r"[eE]", token)[0]
        places = len(mantissa.split(".", 1)[1]) if "." in mantissa else 0
        value = float(token)
        if (
            start >= 1
            and text[start - 1] in _MINUS
            and (start < 2 or not text[start - 2].isalnum())
        ):
            value, token = -value, "-" + token
        out.append((token, value, places))
    return out


def odd_digits(text: str) -> list[str]:
    """Non-ASCII digits, and non-ASCII numerals next to number-like text. Empty means none."""
    bad = []
    for i, ch in enumerate(text):
        if ch.isascii() or not ch.isnumeric():
            continue
        near = text[max(0, i - 1) : i] + text[i + 1 : i + 2]
        if ch.isdecimal() or any(
            c in _NUMBER_LIKE or (not c.isascii() and c.isnumeric()) for c in near
        ):
            bad.append(ch)
    return bad


def _values(node: Any, ids: re.Pattern) -> list[float]:
    """Every number under a pack node: numeric leaves, and numbers written inside strings."""
    if node is None or isinstance(node, bool):
        return []
    if isinstance(node, int | float):
        return [float(node)]
    if isinstance(node, str):
        return [v for _, v, _ in signed_numbers(ids.sub("x", node))]
    if isinstance(node, dict):
        return [v for x in node.values() for v in _values(x, ids)]
    if isinstance(node, list):
        return [v for x in node for v in _values(x, ids)]
    return []


def unsupported_numbers(text: str, allowed: list[float], ids: re.Pattern) -> list[str]:
    """Numbers in the text that match no allowed value after rounding, sign kept.

    Names (well names, condition keys, "1H") are replaced by a letter first, so
    their digits are not read as numbers and a hyphen after them is not a sign.
    """
    bad = []
    for token, value, places in signed_numbers(ids.sub("x", text or "")):
        if not any(round(t, places) == round(value, places) for t in allowed):
            bad.append(token)
    return bad


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + ".")


# ---------------------------------------------------------------- checks


def _problems(review: DataReview, pack: dict) -> list[tuple[str, str]]:
    """Every problem with a review, as (kind, plain words). Empty means it passes."""
    out: list[tuple[str, str]] = []
    conds = {c["key"]: c for c in pack.get("conditions") or []}
    rows = {w["well"]: w for w in pack.get("wells") or []}
    plate = pack.get("plate") or {}
    plate_flags = _plate_flags(pack)
    failed = plate_failed(pack)
    ids = _id_pattern(pack)

    counts = Counter(v.key for v in review.conditions)
    for key in conds:
        if counts[key] == 0:
            out.append(("coverage", f"Condition {key} is not judged. Judge every condition once."))
        elif counts[key] > 1:
            out.append(
                ("coverage", f"Condition {key} is judged {counts[key]} times. Judge it once.")
            )
    for key in counts:
        if key not in conds:
            out.append(
                (
                    "unknown_key",
                    f"Condition key '{key}' is not in the evidence pack. "
                    f"The keys are: {', '.join(conds)}.",
                )
            )

    for v in review.conditions:
        cond = conds.get(v.key)
        own_wells = list((cond or {}).get("wells") or [])
        if not v.reason.strip():
            out.append(("no_reason", f"Condition {v.key} has no reason."))
        if not v.evidence:
            out.append(
                (
                    "no_evidence",
                    f"Condition {v.key} cites no evidence path. Cite at least one, "
                    f"for example conditions.{v.key}.agreement.",
                )
            )
        good_paths = []
        for path in v.evidence:
            if path in SECTIONS:
                out.append(
                    (
                        "broad_path",
                        f"Condition {v.key} cites '{path}', a whole section. Cite a path into "
                        f"it, like conditions.{v.key}.lcms.mean.",
                    )
                )
            elif not resolves(pack, path):
                out.append(
                    (
                        "bad_path",
                        f"Condition {v.key} cites '{path}', which does not resolve in the "
                        "evidence pack exactly as written. Write paths like "
                        "conditions.<key>.lcms.mean or wells.<well>.nmr.flags, with no spaces.",
                    )
                )
            else:
                good_paths.append(path)
        if cond is not None and v.evidence:
            own = [f"conditions.{v.key}", *(f"wells.{w}" for w in own_wells)]
            if not any(_under(p, o) for p in good_paths for o in own):
                out.append(
                    (
                        "off_condition_evidence",
                        f"Condition {v.key} cites no path under conditions.{v.key} or under "
                        f"one of its wells ({', '.join(own_wells)}). Cite at least one.",
                    )
                )
        scope = [cond or {}, plate, *(rows[w] for w in own_wells if w in rows)]
        scope += [node_at(pack, p) for p in good_paths]
        bad = unsupported_numbers(v.reason, _values(scope, ids), ids)
        if bad:
            out.append(
                (
                    "invented_number",
                    f"Condition {v.key} reason has numbers that are not under "
                    f"conditions.{v.key}, in its wells' rows, in the plate block, or at a path "
                    f"it cites: {', '.join(bad)}. Copy numbers exactly, sign included, from "
                    "those places, or leave them out.",
                )
            )
        odd = odd_digits(v.reason)
        if odd:
            out.append(
                (
                    "non_ascii_digit",
                    f"Condition {v.key} reason has non-ASCII digits: {' '.join(odd)}. "
                    "Write numbers with the digits 0 to 9.",
                )
            )
        if cond is not None and v.verdict == "accept":
            flags = _condition_flags(cond)
            if "LOW_REPLICATES" in flags:
                out.append(
                    (
                        "accept_low_replicates",
                        f"Condition {v.key} is accepted but carries LOW_REPLICATES. "
                        "Use flag or rerun.",
                    )
                )
            if cond.get("agreement") == "insufficient":
                out.append(
                    (
                        "accept_insufficient",
                        f"Condition {v.key} is accepted but its agreement is insufficient. "
                        "Use flag or rerun.",
                    )
                )
            if cond.get("agreement") == "disagree" or "LCMS_NMR_DISAGREE" in flags:
                out.append(
                    (
                        "accept_disagree",
                        f"Condition {v.key} is accepted but its LC/MS and NMR readouts "
                        "disagree. Use flag or rerun.",
                    )
                )
            if failed:
                out.append(
                    (
                        "accept_failed_plate",
                        f"Condition {v.key} is accepted on a plate with PLATE_QC_FAIL. "
                        "Use flag or rerun.",
                    )
                )

    if failed and review.round_verdict != "rerun_plate":
        out.append(
            (
                "plate_qc_fail",
                "The plate carries PLATE_QC_FAIL, so the round verdict must be rerun_plate, "
                f"not {review.round_verdict}.",
            )
        )
    if review.round_verdict == "accept":
        not_accepted = [v.key for v in review.conditions if v.verdict != "accept"]
        if not_accepted:
            out.append(
                (
                    "round_inconsistent",
                    "The round verdict is accept, but these conditions are flagged or rerun: "
                    f"{', '.join(not_accepted)}. Use accept_with_flags.",
                )
            )
        if plate_flags:
            out.append(
                (
                    "round_accept_plate_flags",
                    "The round verdict is accept, but the plate carries "
                    f"{', '.join(sorted(plate_flags))}. Use accept_with_flags or rerun_plate.",
                )
            )
        if review.suspected_faults:
            out.append(
                (
                    "round_accept_faults",
                    "The round verdict is accept, but the review names suspected faults. "
                    "Use accept_with_flags or rerun_plate.",
                )
            )

    for i, f in enumerate(review.suspected_faults, start=1):
        if not f.reason.strip():
            out.append(("fault_no_reason", f"Suspected fault {i} ({f.kind}) has no reason."))
        for w in f.wells:
            if w not in rows:
                out.append(
                    (
                        "unknown_well",
                        f"Suspected fault {i} ({f.kind}) names well '{w}', which is not in the "
                        "evidence pack.",
                    )
                )
        if f.kind in WELL_FAULTS and not f.wells:
            out.append(
                (
                    "no_wells",
                    f"Suspected fault {i} ({f.kind}) names no wells. Name the wells it touches.",
                )
            )
        scope = [plate, *(rows[w] for w in f.wells if w in rows)]
        places = "named wells' rows or the plate block"
        if f.kind not in WELL_FAULTS:
            scope.append(pack.get("conditions") or [])
            places = "named wells' rows, the plate block, or under conditions"
        bad = unsupported_numbers(f.reason, _values(scope, ids), ids)
        if bad:
            out.append(
                (
                    "invented_number",
                    f"Suspected fault {i} ({f.kind}) reason has numbers that are not in its "
                    f"{places}: {', '.join(bad)}. Copy numbers exactly, sign included, from "
                    "those places, or leave them out.",
                )
            )
        odd = odd_digits(f.reason)
        if odd:
            out.append(
                (
                    "non_ascii_digit",
                    f"Suspected fault {i} ({f.kind}) reason has non-ASCII digits: "
                    f"{' '.join(odd)}. Write numbers with the digits 0 to 9.",
                )
            )

    words = len(review.summary.split())
    if words > MAX_SUMMARY_WORDS:
        out.append(
            (
                "summary_too_long",
                f"The summary has {words} words. The limit is {MAX_SUMMARY_WORDS}.",
            )
        )
    bad = unsupported_numbers(review.summary, _values(pack, ids), ids)
    if bad:
        out.append(
            (
                "invented_number",
                f"The summary has numbers that are not in the evidence pack: {', '.join(bad)}.",
            )
        )
    odd = odd_digits(review.summary)
    if odd:
        out.append(
            (
                "non_ascii_digit",
                f"The summary has non-ASCII digits: {' '.join(odd)}. "
                "Write numbers with the digits 0 to 9.",
            )
        )
    return out


def check_review(review: DataReview, evidence: Any) -> list[str]:
    """Every problem with a review, in plain words. An empty list means code accepts it."""
    return [msg for _, msg in _problems(review, pack_dict(evidence))]


# ---------------------------------------------------------------- baseline


def drift_signature(pack: dict) -> bool:
    """The reference control's LC/MS mean is below expected minus tolerance, and LC/MS reads
    below NMR on most conditions (more than half of those with both means)."""
    ref = ((pack.get("plate") or {}).get("controls") or {}).get("reference") or {}
    lc, expected, tol = ref.get("lcms_mean"), ref.get("expected"), ref.get("tolerance")
    if lc is None or expected is None or tol is None or not lc < expected - tol:
        return False
    both = [
        c
        for c in pack.get("conditions") or []
        if (c.get("lcms") or {}).get("mean") is not None
        and (c.get("nmr") or {}).get("mean") is not None
    ]
    low = sum(1 for c in both if c["lcms"]["mean"] < c["nmr"]["mean"])
    return bool(both) and low * 2 > len(both)


def qc_only_review(evidence: Any) -> DataReview:
    """The deterministic baseline: fixed rules turn flags into verdicts and fault kinds. No model.

    Conditions: rerun on a failed plate (PLATE_QC_FAIL), LOW_REPLICATES, or
    agreement "insufficient"; flag on any other flag, a disagreement, or a
    flagged well; else accept.
    Faults: IS_OUT_OF_RANGE on wells names injection_failure on those wells,
    NMR_SIGNAL_INCONSISTENT on wells names nmr_impurity on those wells,
    NMR_BROAD_LINES names nmr_broad_lines, and NMR_PHASE_CORRECTED names
    nmr_phase_error (plate-wide when it is a plate flag). PLATE_QC_FAIL names
    plate_drift only with the drift signature (drift_signature): the reference
    control's LC/MS mean below expected minus tolerance, and LC/MS below NMR on
    most conditions. Any other PLATE_QC_FAIL names "other". Flags that point
    at no single cause (REPLICATE_SPREAD, LOW_REPLICATES, LCMS_NMR_DISAGREE, a
    poor fit, low signal) shape verdicts but name no fault.
    Round: rerun_plate on PLATE_QC_FAIL; accept only when every condition is
    accepted, the plate has no flag, and no fault is named; else
    accept_with_flags.
    """
    pack = pack_dict(evidence)
    plate = pack.get("plate") or {}
    failed = plate_failed(pack)
    well_rows = {w["well"]: w for w in pack.get("wells") or []}

    verdicts = []
    for cond in pack.get("conditions") or []:
        key = cond["key"]
        evidence_paths = [f"conditions.{key}.agreement"]
        reasons = []
        for side, label in (("lcms", "LC/MS"), ("nmr", "NMR")):
            for flag in (cond.get(side) or {}).get("flags") or []:
                evidence_paths.append(f"conditions.{key}.{side}.flags.{flag}")
                reasons.append(f"{flag} ({label})")
        for flag in cond.get("flags") or []:
            evidence_paths.append(f"conditions.{key}.flags.{flag}")
            reasons.append(flag)
        flagged_wells = []
        for w in cond.get("wells") or []:
            row = well_rows.get(w) or {}
            for side in ("lcms", "nmr"):
                part = row.get(side) or {}
                for flag in part.get("flags") or []:
                    if flag == "PLATE_QC_FAIL":
                        continue
                    evidence_paths.append(f"wells.{w}.{side}.flags.{flag}")
                    flagged_wells.append(w)
        agreement = cond.get("agreement")
        flags = _condition_flags(cond)
        if failed:
            verdict, why = "rerun", "The plate failed its controls (PLATE_QC_FAIL)."
            evidence_paths.append(
                "plate.lcms_flags.PLATE_QC_FAIL"
                if "PLATE_QC_FAIL" in (plate.get("lcms_flags") or [])
                else "plate.nmr_flags.PLATE_QC_FAIL"
            )
        elif "LOW_REPLICATES" in flags or agreement == "insufficient":
            verdict, why = "rerun", "Too few valid wells to judge."
        elif reasons or agreement == "disagree" or flagged_wells:
            verdict, why = "flag", "Flagged by quality checks."
        else:
            verdict, why = "accept", "No flags, and the two readouts agree."
        detail = []
        if reasons:
            detail.append("Flags: " + ", ".join(dict.fromkeys(reasons)) + ".")
        if agreement in ("disagree", "insufficient"):
            detail.append(f"Agreement: {agreement}.")
        if flagged_wells:
            detail.append("Flagged wells: " + ", ".join(dict.fromkeys(flagged_wells)) + ".")
        verdicts.append(
            ConditionVerdict(
                key=key,
                verdict=verdict,
                reason=" ".join([why, *detail]),
                evidence=list(dict.fromkeys(evidence_paths)),
            )
        )

    faults: list[SuspectedFault] = []
    if failed:
        sides = " and ".join(
            name
            for name, flags in (("LC/MS", plate.get("lcms_flags")), ("NMR", plate.get("nmr_flags")))
            if "PLATE_QC_FAIL" in (flags or [])
        )
        if drift_signature(pack):
            ref = plate["controls"]["reference"]
            faults.append(
                SuspectedFault(
                    kind="plate_drift",
                    wells=[],
                    reason=(
                        f"The plate failed its {sides} controls (PLATE_QC_FAIL). The reference "
                        f"control's LC/MS mean {ref['lcms_mean']} is below its expected value "
                        f"{ref['expected']} by more than the tolerance {ref['tolerance']}, and "
                        "LC/MS reads below NMR on most conditions."
                    ),
                )
            )
        else:
            faults.append(
                SuspectedFault(
                    kind="other",
                    wells=[],
                    reason=(
                        f"The plate failed its {sides} controls (PLATE_QC_FAIL) without the "
                        "drift pattern, so the fixed rules name no single cause."
                    ),
                )
            )
    plate_level = _plate_flags(pack)
    for flag, kind in FLAG_KINDS.items():
        hit = [
            w
            for w, row in well_rows.items()
            if flag in ((row.get("lcms") or {}).get("flags") or [])
            or flag in ((row.get("nmr") or {}).get("flags") or [])
        ]
        if flag in plate_level and kind not in WELL_FAULTS:
            faults.append(SuspectedFault(kind=kind, wells=[], reason=f"Plate flag {flag}."))
        elif hit:
            faults.append(SuspectedFault(kind=kind, wells=hit, reason=f"Wells flagged {flag}."))

    if failed:
        round_verdict = "rerun_plate"
    elif faults or plate_level or any(v.verdict != "accept" for v in verdicts):
        round_verdict = "accept_with_flags"
    else:
        round_verdict = "accept"
    kinds = ", ".join(dict.fromkeys(f.kind for f in faults)) or "none"
    summary = (
        f"Fixed rules, no model. Round verdict {round_verdict}. Suspected faults: {kinds}. "
        "Each condition verdict follows its own flags."
    )
    return DataReview(
        round_verdict=round_verdict,
        conditions=verdicts,
        suspected_faults=faults,
        summary=summary,
    )


# ---------------------------------------------------------------- plate statistics baseline

# The constants of qc_stats_review's two plate statistics. qc_stats_review's docstring says
# where each number comes from. eval-reviewer's baseline sweep runs these rules on other seeds.
STATS_MIN_N = 2  # usable wells on each side for a condition to count toward drift
DRIFT_LOW_SHARE = 0.8  # LC/MS mean below NMR mean on at least this share of those conditions
DRIFT_MEDIAN_MAX = -3.0  # and their median difference (LC/MS minus NMR) at most this, in points
IMPURITY_FLOOR = 1.5  # points: a singlet minus doublet gap under this is never named
IMPURITY_SPREADS = 4.0  # the gap must also reach this many times the plate's spread (s)
MAD_TO_SD = 1.4826  # the median absolute deviation times this estimates a normal sd
_EPS = 1e-9  # pack numbers carry one decimal, so this only absorbs float error


def drift_stats(evidence: Any) -> dict:
    """The drift statistic over conditions with at least STATS_MIN_N usable wells on each side.

    Returns "counted" (key, difference) pairs, using the pack's own difference
    (LC/MS mean minus NMR mean), "low" (how many are below zero), "median"
    (their median, 2 places), and "fires" (whether the drift rule fires).
    """
    pack = pack_dict(evidence)
    counted = []
    for c in pack.get("conditions") or []:
        lc, nm = c.get("lcms") or {}, c.get("nmr") or {}
        if (lc.get("n") or 0) < STATS_MIN_N or (nm.get("n") or 0) < STATS_MIN_N:
            continue
        if c.get("difference") is not None:
            counted.append((c["key"], float(c["difference"])))
    low = sum(1 for _, d in counted if d < 0)
    median = round(statistics.median(d for _, d in counted), 2) if counted else None
    fires = (
        median is not None
        and low >= DRIFT_LOW_SHARE * len(counted) - _EPS
        and median <= DRIFT_MEDIAN_MAX + _EPS
    )
    return {"counted": counted, "low": low, "median": median, "fires": fires}


def impurity_stats(evidence: Any) -> dict:
    """The impurity statistic: the singlet minus doublet gap of every usable NMR well.

    A usable well has an NMR row that is not excluded and has both signals.
    Returns "gaps" (well to gap, 1 place, in pack order), "spread" (MAD_TO_SD
    times the median absolute deviation of the gaps), "limit" (the larger of
    IMPURITY_FLOOR and IMPURITY_SPREADS times the spread), and "wells" (the
    wells whose gap reaches the limit).
    """
    pack = pack_dict(evidence)
    gaps: dict[str, float] = {}
    for row in pack.get("wells") or []:
        nmr = row.get("nmr") or {}
        singlet, doublet = nmr.get("yield_from_singlet"), nmr.get("yield_from_doublet")
        if nmr and not nmr.get("excluded") and singlet is not None and doublet is not None:
            gaps[row["well"]] = round(singlet - doublet, 1)
    if not gaps:
        return {"gaps": {}, "spread": None, "limit": None, "wells": []}
    middle = statistics.median(gaps.values())
    spread = MAD_TO_SD * statistics.median(abs(g - middle) for g in gaps.values())
    limit = max(IMPURITY_FLOOR, IMPURITY_SPREADS * spread)
    wells = [w for w, g in gaps.items() if g >= limit - _EPS]
    return {"gaps": gaps, "spread": spread, "limit": limit, "wells": wells}


def _middle(counted: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """The one or two conditions in the middle when sorted by difference: the median's own."""
    ranked = sorted(counted, key=lambda kd: kd[1])
    n = len(ranked)
    return ranked[(n - 1) // 2 : n // 2 + 1]


def qc_stats_review(evidence: Any) -> DataReview:
    """The second baseline with no model: qc_only_review plus two plate statistics.

    Both rules read only numbers in the pack. Their thresholds are the module
    constants STATS_MIN_N, DRIFT_LOW_SHARE, DRIFT_MEDIAN_MAX, IMPURITY_FLOOR
    and IMPURITY_SPREADS. They were not chosen blind: the recipes of the hard
    fault cases were known when they were set. So read this baseline as what
    simple fixed rules can do once someone knows what to look for. The
    baseline sweep of eval-reviewer runs them on seeds other than the case
    seeds. Where each number comes from, using noise levels the docs state:

    Drift rule. Count the conditions with at least STATS_MIN_N (2) usable
    wells on each side. When LC/MS reads below NMR (difference below zero) on
    at least DRIFT_LOW_SHARE (80%) of them, and the median difference is at
    most DRIFT_MEDIAN_MAX (-3 points), name plate_drift, unless the QC rules
    already named it.
    - Noise. An LC/MS well reads its true yield plus normal noise with sd 2.5
      points (SPEC.md, virtual LC/MS). An NMR well on a plate starts from its
      true yield plus a draw with sd 2 points, and the fit adds about 0.3
      points (mean abs error, docs/nmr.md). So one condition's difference of
      means has sd about sqrt(2.5^2 + 2^2) / sqrt(n): 1.8 points with 3 wells
      a side, 2.3 with 2.
    - Share. With no drift both readouts measure the same yield, so each
      difference is as likely to fall below zero as above. The chance that
      80% or more of k conditions fall below by luck is 5% for k = 10 and
      under 1% for k = 20.
    - Median. The median of k such differences has sd about 1.25 x 1.8 /
      sqrt(k), 0.7 points for k = 10. A median of -3 points is about 4 of
      those sd from zero. It is also under the 5 point floor at which one
      condition is called disagree, so the rule looks for a shift too small to
      flag any single condition. Both parts must hold, so a few low outliers,
      or a lucky run of signs, do not fire it alone.

    Impurity rule. For each usable NMR well (kept, with both signals), gap =
    yield_from_singlet - yield_from_doublet. s = MAD_TO_SD (1.4826) times the
    median absolute deviation of the gaps over all usable wells. A well whose
    gap is at least max(IMPURITY_FLOOR (1.5 points), IMPURITY_SPREADS (4) x s)
    is named under nmr_impurity, unless the QC rules already named it.
    - Both signals measure the same product, so on a clean well the gap is fit
      error only. docs/nmr.md gives the clean fit's mean abs error as 0.49
      points for the doublet and 0.23 for the singlet. A mean abs error is
      0.8 sd for normal error, so a clean gap has sd about 0.7 points if the
      two errors are independent.
    - An impurity under the singlet adds to the singlet only, so only a
      positive gap counts.
    - s comes from the plate itself, so a plate whose gaps are all wider
      (broad lines, a phase error) gets a higher bar. The median absolute
      deviation ignores up to half the wells, so a few impure wells do not
      raise it, and 1.4826 turns it into a normal sd.
    - Under normal theory, four sd has a one-sided chance of about 3 in
      100000 per clean well, or about 0.2% for any of 70 wells. That is an
      estimate from normal theory, not a measured rate. The measured rate in this
      simulator is higher: fit errors need not be normal, and s is itself
      estimated from one plate. The baseline sweep on the eval page
      (docs/reviewer_eval.md, section "Baselines across N extra seeds per
      case") measures it: its false alarm line counts the plates with no true
      nmr_impurity on which this rule named one.
    - The floor only matters on a plate whose own s is under 0.375 points,
      where 1.5 points is more than 4 of that plate's sd. The pack rounds each
      signal to 0.1 points, so a quiet plate's median absolute deviation can
      be 0.1 or 0; the floor stops such a plate from naming wells on rounding.

    Verdicts. A condition the QC rules accepted becomes "flag" when the drift
    rule fires, or when one of its wells is named for an impurity; its reason
    and evidence say why. The round verdict follows the qc_only_review rule.
    """
    pack = pack_dict(evidence)
    base = qc_only_review(pack)
    verdicts = [v.model_copy(deep=True) for v in base.conditions]
    by_key = {v.key: v for v in verdicts}
    conds = {c["key"]: c for c in pack.get("conditions") or []}
    rows = {w["well"]: w for w in pack.get("wells") or []}
    faults = list(base.suspected_faults)
    named_kinds = {f.kind for f in faults}
    named_wells = {w for f in faults if f.kind == "nmr_impurity" for w in f.wells}

    def note(key: str, text: str, paths: list[str]) -> None:
        v = by_key.get(key)
        if v is None:
            return
        if v.verdict == "accept":
            v.verdict, v.reason = "flag", text
        else:
            v.reason = f"{v.reason} {text}"
        v.evidence = list(dict.fromkeys([*v.evidence, *paths]))

    drift = drift_stats(pack)
    if drift["fires"] and "plate_drift" not in named_kinds:
        middle = [f"{k}'s {d}" for k, d in _middle(drift["counted"])]
        where = f"is {middle[0]}" if len(middle) == 1 else f"lies between {' and '.join(middle)}"
        reason = (
            "Plate statistics: LC/MS reads below NMR on most conditions with replicates on "
            "both sides, by more than the drift rule allows. The median difference (LC/MS minus "
            f"NMR) {where}."
        )
        ref = ((pack.get("plate") or {}).get("controls") or {}).get("reference") or {}
        shown = [
            f"{label} {ref[field]}"
            for label, field in (
                ("LC/MS", "lcms_mean"),
                ("NMR", "nmr_mean"),
                ("expected", "expected"),
            )
            if ref.get(field) is not None
        ]
        if shown:
            reason += " Reference control: " + ", ".join(shown) + "."
        faults.append(SuspectedFault(kind="plate_drift", wells=[], reason=reason))
        for key, cond in conds.items():
            text = "Plate statistics suggest LC/MS reads low across the plate (plate_drift)."
            if cond.get("difference") is not None:
                text += f" Difference {cond['difference']}."
            note(key, text, [f"conditions.{key}.difference"])

    impurity = impurity_stats(pack)
    new_wells = [w for w in impurity["wells"] if w not in named_wells]
    if new_wells:
        parts = []
        for w in new_wells:
            nmr = rows[w]["nmr"]
            parts.append(
                f"{w}: singlet {nmr['yield_from_singlet']}, doublet {nmr['yield_from_doublet']}"
            )
            note(
                rows[w].get("key", ""),
                f"Plate statistics: well {w}'s NMR singlet {nmr['yield_from_singlet']} reads "
                f"above its doublet {nmr['yield_from_doublet']} by more than the plate's "
                "scatter allows (nmr_impurity suspected).",
                [f"wells.{w}.nmr.yield_from_singlet", f"wells.{w}.nmr.yield_from_doublet"],
            )
        faults.append(
            SuspectedFault(
                kind="nmr_impurity",
                wells=new_wells,
                reason=(
                    "Plate statistics: the NMR singlet reads above the doublet by more than the "
                    "plate's own scatter allows. " + "; ".join(parts) + "."
                ),
            )
        )

    plate_level = _plate_flags(pack)
    if plate_failed(pack):
        round_verdict = "rerun_plate"
    elif faults or plate_level or any(v.verdict != "accept" for v in verdicts):
        round_verdict = "accept_with_flags"
    else:
        round_verdict = "accept"
    kinds = ", ".join(dict.fromkeys(f.kind for f in faults)) or "none"
    summary = (
        f"Fixed rules and two plate statistics, no model. Round verdict {round_verdict}. "
        f"Suspected faults: {kinds}. Each condition verdict follows its own flags and the "
        "plate statistics."
    )
    return DataReview(
        round_verdict=round_verdict,
        conditions=verdicts,
        suspected_faults=faults,
        summary=summary,
    )


# ---------------------------------------------------------------- the model


def prompt_file(prompt: str = DEFAULT_PROMPT) -> str:
    """The file of a named reviewer prompt. An unknown name is an error."""
    if prompt not in PROMPTS:
        raise ValueError(f"unknown reviewer prompt '{prompt}'; known: {', '.join(PROMPTS)}")
    return PROMPTS[prompt]


def review_messages(evidence: Any, prompt: str = DEFAULT_PROMPT) -> list[dict]:
    """The system prompt and the evidence pack, as the reviewer sees them."""
    pack = pack_dict(evidence)
    return [
        {"role": "system", "content": load_prompt(prompt_file(prompt))},
        {"role": "user", "content": "EVIDENCE PACK\n" + json.dumps(pack)},
    ]


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def review_round(
    evidence: Any,
    client,
    label: str,
    transcript: Transcript | None = None,
    *,
    scope: str = ROLE,
    temperature: float = 0.2,
    prompt: str = DEFAULT_PROMPT,
) -> tuple[DataReview | None, dict]:
    """Ask the reviewer model for a review, check it, and send problems back at most twice.

    Returns (review, info). The review is None when no valid review came back;
    info then holds a note that a person must look at the round. info also
    records the prompt name, the model name, the calls made, the problems
    found per reply, and "outage": True when the run ended because the client
    stopped answering.
    """
    pack = pack_dict(evidence)
    leaks = truth_in(pack)
    if leaks:
        raise ValueError(
            "the evidence pack holds truth the reviewer must not see: " + ", ".join(leaks)
        )
    messages = review_messages(pack, prompt)
    info: dict = {
        "label": label,
        "prompt": prompt,
        "model": "",
        "calls": 0,
        "replies": 0,
        "tokens": 0,
        "seconds": 0.0,
        "attempts": [],
        "error": None,
        "outage": False,
        "note": "",
    }
    for attempt in range(1, ATTEMPTS + 1):
        try:
            result = chat(
                client,
                label,
                messages,
                DataReview,
                temperature=temperature,
                transcript=transcript,
                role=ROLE,
                meta={"scope": scope, "attempt": attempt},
                max_calls=CALLS_PER_ATTEMPT,
            )
        except (EndpointUnavailable, ScriptExhausted) as exc:
            info["error"] = f"{type(exc).__name__}: {exc}"
            info["outage"] = True
            break
        info["calls"] += result.calls
        info["replies"] += result.calls
        info["tokens"] += result.prompt_tokens + result.completion_tokens
        info["seconds"] = round(info["seconds"] + result.seconds, 3)
        info["model"] = result.model or info["model"]
        record: dict = {"attempt": attempt, "calls": result.calls, "parse_error": None}
        if result.parsed is None:
            record["parse_error"] = result.error
            record["problems"] = [{"kind": "unreadable", "message": result.error or ""}]
            info["attempts"].append(record)
            messages = [
                *messages,
                {"role": "assistant", "content": result.text},
                {
                    "role": "user",
                    "content": f"That reply could not be used: {result.error}. "
                    "Reply with the whole review as one JSON object in the reply format.",
                },
            ]
            continue
        review = DataReview.model_validate(result.parsed)
        problems = _problems(review, pack)
        record["problems"] = [{"kind": k, "message": m} for k, m in problems]
        info["attempts"].append(record)
        if not problems:
            return review, info
        messages = [
            *messages,
            {"role": "assistant", "content": result.text},
            {
                "role": "user",
                "content": "Code checked your review and found these problems:\n- "
                + "\n- ".join(m for _, m in problems)
                + "\nReply with the whole corrected review as one JSON object.",
            },
        ]
    if not info["attempts"]:
        info["note"] = f"{NEEDS_PERSON}: the reviewer model gave no reply ({info['error']})."
        return None, info
    last = " ".join(p["message"] for p in info["attempts"][-1]["problems"])
    info["note"] = (
        f"{NEEDS_PERSON}: the reviewer gave no valid review in "
        f"{_plural(len(info['attempts']), 'attempt')} "
        f"({_plural(info['replies'], 'reply', 'replies')}). Last problems: {last}"
    )
    if info["error"]:
        info["note"] += f" Then the model gave no reply ({info['error']})."
    return None, info


# ---------------------------------------------------------------- scoring


def _fault(x: Any) -> dict:
    """A fault as {"kind", "wells"}, from a model or a dict."""
    if isinstance(x, dict):
        return {"kind": x["kind"], "wells": list(x.get("wells") or [])}
    return {"kind": x.kind, "wells": list(x.wells or [])}


def merge_faults(items: list) -> list[dict]:
    """One entry per kind, in first-seen order, with the wells of every entry of that kind."""
    merged: dict[str, list[str]] = {}
    for x in items or []:
        f = _fault(x)
        wells = merged.setdefault(f["kind"], [])
        wells += [w for w in f["wells"] if w not in wells]
    return [{"kind": k, "wells": w} for k, w in merged.items()]


def matches(suspected: Any, true_fault: Any) -> bool:
    """Same kind, and for a fault on wells, at least one well in common."""
    s, t = _fault(suspected), _fault(true_fault)
    if s["kind"] != t["kind"]:
        return False
    return not t["wells"] or bool(set(s["wells"]) & set(t["wells"]))


def score(true_faults: list, suspected: list) -> dict:
    """Score one review against the true fault log, fault by fault and well by well.

    Entries of the same kind are merged first, on both sides, so how a review
    groups its wells cannot change the score.

    Faults: a true fault is caught when the review names its kind and, for a
    fault on wells, at least one of its wells. Otherwise it is missed.

    Wells, for injection_failure and nmr_impurity: hits are true wells named
    under the right kind, missed are true wells not named, extra are named
    wells the log does not list for that kind. Each extra well is one false
    alarm. A well kind named with no wells, when the plate has no such fault,
    is one false alarm.

    Plate-wide kinds and "other": one false alarm when no true fault of that
    kind exists, however often it is named. Wells named on a plate-wide kind
    are not scored. lcms_nmr_disagreement is not a false alarm when the plate
    holds a true fault of any kind; on a clean plate it is one.
    """
    truth = merge_faults(true_faults)
    named = merge_faults(suspected)
    true_by_kind = {t["kind"]: t for t in truth}
    named_by_kind = {s["kind"]: s for s in named}

    caught = [
        t for t in truth if t["kind"] in named_by_kind and matches(named_by_kind[t["kind"]], t)
    ]
    missed = [t for t in truth if t not in caught]

    false_alarms: list[dict] = []
    excused: list[dict] = []
    detail: list[dict] = []
    for s in named:
        kind = s["kind"]
        t = true_by_kind.get(kind)
        entry = {"kind": kind, "wells": s["wells"], "hits": [], "extra": [], "missed": []}
        if kind in WELL_FAULTS:
            true_wells = t["wells"] if t else []
            entry["hits"] = [w for w in s["wells"] if w in true_wells]
            entry["extra"] = [w for w in s["wells"] if w not in true_wells]
            entry["missed"] = [w for w in true_wells if w not in s["wells"]]
            false_alarms += [{"kind": kind, "wells": [w]} for w in entry["extra"]]
            if t is None and not s["wells"]:
                false_alarms.append({"kind": kind, "wells": []})
                entry["outcome"] = "false alarm"
            elif t is not None and matches(s, t):
                entry["outcome"] = "caught"
            elif t is not None:
                entry["outcome"] = "wrong wells"
            else:
                entry["outcome"] = "false alarm"
        elif kind == DISAGREEMENT and truth:
            excused.append({"kind": kind, "wells": s["wells"]})
            entry["outcome"] = "not scored"
        elif t is None:
            false_alarms.append({"kind": kind, "wells": s["wells"]})
            entry["outcome"] = "false alarm"
        else:
            entry["outcome"] = "caught"
        detail.append(entry)

    wells = {"true": 0, "hits": 0, "missed": 0, "extra": 0}
    for kind in WELL_FAULTS:
        true_wells = true_by_kind[kind]["wells"] if kind in true_by_kind else []
        named_wells = named_by_kind[kind]["wells"] if kind in named_by_kind else []
        hits = sum(1 for w in named_wells if w in true_wells)
        wells["true"] += len(true_wells)
        wells["hits"] += hits
        wells["missed"] += len(true_wells) - hits
        wells["extra"] += len(named_wells) - hits
    return {
        "true": len(truth),
        "caught": caught,
        "missed": missed,
        "false_alarms": false_alarms,
        "excused": excused,
        "wells": wells,
        "named": detail,
    }


def fault_text(fault: Any) -> str:
    """A fault in a few words: kind, and its wells or 'plate-wide'."""
    f = _fault(fault)
    return f"{f['kind']} ({', '.join(f['wells']) if f['wells'] else 'plate-wide'})"
