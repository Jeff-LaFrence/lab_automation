"""The campaign loop: decide, check, approve, execute, analyze, and decide again.

Each round runs the full Stage 1 path: compile, simulate, and the ledger check.
The simulated LC/MS then reads the ledger, so the data follow what was
dispensed. State is saved after every step, and every step can be redone from
the files, so a campaign resumes after a crash.

Approval policies:
- always: a person approves every round.
- envelope: a person approves round 1, which creates envelope.json. A later
  round is approved by the envelope when the assay and workcell are the same,
  the well count is no larger, every check passed, the previous round had no
  anomaly, and the envelope still has rounds left. Otherwise a person approves.
- auto_demo: a scripted approver stands in for the person. Labelled demo_auto.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from labauto.agents.llm import Transcript
from labauto.analysis import analyze_plate
from labauto.config import load_assay, load_workcell
from labauto.decide.base import build_history
from labauto.decide.model_decider import ModelDecider
from labauto.decide.optimizer import OptimizerDecider, round_one
from labauto.decide.random_decider import RandomDecider
from labauto.execute import (
    design_for_round,
    measure_and_analyze,
    result_rows,
    volumes_by_well,
    write_rows,
)
from labauto.models import (
    Approval,
    CampaignConfig,
    CampaignState,
    CheckResult,
    Decision,
    Design,
    Envelope,
    QcReport,
)
from labauto.pipeline import check, design_hash, file_hash
from labauto.record import render_summary, utc_now, utc_stamp, write_check_files, write_json
from labauto.store import LocalStore
from labauto.virtual.ground_truth import reference_expected, surface_for
from labauto.virtual.lcms import load_peaks

MAX_AUTO_ROUNDS = 5


class CampaignError(RuntimeError):
    """The campaign cannot do what was asked in its current state."""


# ---------------------------------------------------------------- files


def round_dir(cdir: Path, r: int) -> Path:
    return cdir / "rounds" / f"{r:02d}"


def load_config(cdir: Path) -> CampaignConfig:
    return CampaignConfig.model_validate_json((cdir / "campaign.json").read_text(encoding="utf-8"))


def load_cstate(cdir: Path) -> CampaignState:
    return CampaignState.model_validate_json((cdir / "state.json").read_text(encoding="utf-8"))


def save_cstate(cdir: Path, st: CampaignState) -> None:
    st.updated_at = utc_now()
    (cdir / "state.json").write_text(st.model_dump_json(indent=2) + "\n", encoding="utf-8")


def step(
    cdir: Path, st: CampaignState, round_state: str, reason: str, state: str = "RUNNING"
) -> None:
    """Record one transition and save state.json."""
    st.history.append(
        {
            "round": str(st.round),
            "from": f"{st.state}/{st.round_state}",
            "to": f"{state}/{round_state}",
            "at": utc_now(),
            "reason": reason,
        }
    )
    st.state, st.round_state = state, round_state
    save_cstate(cdir, st)


def load_envelope(cdir: Path) -> Envelope | None:
    path = cdir / "envelope.json"
    return Envelope.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else None


def load_round_approval(rdir: Path) -> Approval | None:
    path = rdir / "approval.json"
    return Approval.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else None


def load_round_check(rdir: Path) -> CheckResult | None:
    path = rdir / "check.json"
    return (
        CheckResult.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else None
    )


# ---------------------------------------------------------------- start and resume


def start_campaign(
    cfg: CampaignConfig, clients=None, settings=None, base: str | Path = "campaigns"
) -> Path:
    """Create the campaign folder and run it as far as it can go."""
    cdir = Path(base) / cfg.campaign_id
    cdir.mkdir(parents=True, exist_ok=False)
    write_json(cdir / "campaign.json", cfg)
    st = CampaignState(campaign_id=cfg.campaign_id, state="RUNNING", updated_at=utc_now())
    st.history.append(
        {
            "round": "1",
            "from": "",
            "to": "RUNNING/DECIDING",
            "at": utc_now(),
            "reason": "campaign started",
        }
    )
    save_cstate(cdir, st)
    advance_campaign(cdir, clients, settings)
    return cdir


def new_campaign_id(strategy: str) -> str:
    return f"c-{utc_stamp()}-{strategy}-{uuid.uuid4().hex[:4]}"


def advance_campaign(cdir: Path, clients=None, settings=None) -> CampaignState:
    """Run rounds until the campaign is done or waits for a person."""
    cfg = load_config(cdir)
    st = load_cstate(cdir)
    if st.state in ("DONE", "NEEDS_HUMAN"):
        return st
    if st.state == "FAILED":
        back = st.history[-1]["from"].split("/") if st.history else ["RUNNING", "DECIDING"]
        step(
            cdir,
            st,
            back[1] if len(back) > 1 else "DECIDING",
            "resumed after a failure",
            back[0] or "RUNNING",
        )
    if st.state == "AWAITING_APPROVAL":
        approval = load_round_approval(round_dir(cdir, st.round))
        if approval is None or approval.decision != "approved":
            return st
        step(cdir, st, "EXECUTING", f"round {st.round} approved ({approval.mode})")
    try:
        _run(cdir, cfg, st, clients, settings)
    except Exception as exc:  # internal errors end in FAILED with where it happened
        from labauto.instruments.base import scrub_paths

        where = f"{st.state}/{st.round_state}"
        step(
            cdir,
            st,
            st.round_state,
            f"{type(exc).__name__} in round {st.round} at {where}: {scrub_paths(str(exc))[:300]}",
            "FAILED",
        )
    return st


def _decider(cfg: CampaignConfig, assay, clients, rdir: Path):
    if cfg.strategy == "random":
        return RandomDecider(assay)
    if cfg.strategy == "optimizer":
        return OptimizerDecider(assay)
    if clients is None:
        raise CampaignError("the model strategy needs a chat client")
    return ModelDecider(assay, clients.local, transcript=Transcript(rdir / "transcript.jsonl"))


def _run(cdir: Path, cfg: CampaignConfig, st: CampaignState, clients, settings) -> None:
    assay = load_assay(cfg.assay)
    workcell = load_workcell(cfg.workcell)
    store = LocalStore(cdir.parent)
    surface = surface_for(cfg.surface_seed)
    expected = reference_expected(assay, cfg.surface_seed)

    while st.round <= cfg.rounds:
        r = st.round
        rdir = round_dir(cdir, r)
        rdir.mkdir(parents=True, exist_ok=True)

        if st.round_state == "DECIDING":
            if (rdir / "decision.json").exists():
                decision = Decision.model_validate_json(
                    (rdir / "decision.json").read_text(encoding="utf-8")
                )
            else:
                history = build_history(store.load_results(cfg.campaign_id), assay, r)
                if r == 1:
                    decision = round_one(assay, cfg.conditions_per_round, cfg.seed, cfg.strategy)
                else:
                    decider = _decider(cfg, assay, clients, rdir)
                    decision = decider.decide(
                        history, cfg.conditions_per_round, cfg.brief, cfg.seed
                    )
                write_json(rdir / "decision.json", decision)
            if decision.fallback:
                st.anomalies[str(r)] = f"decider fallback: {decision.fallback}"
            step(
                cdir,
                st,
                "CHECKING",
                f"round {r}: {len(decision.picks)} conditions picked by {decision.strategy}",
            )

        if st.round_state == "CHECKING":
            decision = Decision.model_validate_json(
                (rdir / "decision.json").read_text(encoding="utf-8")
            )
            result = _check_round(rdir, decision, cfg, assay, workcell, r)
            if not result.ok and cfg.strategy == "model" and not decision.fallback:
                failed = rdir / "failed_check"
                failed.mkdir(exist_ok=True)
                for name in (
                    "design.json",
                    "plan.json",
                    "errors.json",
                    "check.json",
                    "worklist.csv",
                ):
                    if (rdir / name).exists():
                        shutil.move(str(rdir / name), str(failed / name))
                history = build_history(store.load_results(cfg.campaign_id), assay, r)
                codes = ", ".join(sorted({e.code for e in result.errors}))
                decision = OptimizerDecider(assay).decide(
                    history, cfg.conditions_per_round, cfg.brief, cfg.seed
                )
                decision.strategy = "model"
                decision.fallback = f"pipeline failure ({codes}); used the optimizer's picks"
                write_json(rdir / "decision.json", decision)
                st.anomalies[str(r)] = f"pipeline failure: {codes}"
                result = _check_round(rdir, decision, cfg, assay, workcell, r)
            if not result.ok:
                codes = ", ".join(sorted({e.code for e in result.errors}))
                st.anomalies[str(r)] = f"pipeline failure: {codes}"
                st.needs_human = f"round {r} failed the check pipeline: {codes}"
                step(cdir, st, "CHECKING", st.needs_human, "NEEDS_HUMAN")
                _round_summary(cdir, rdir, cfg, r)
                return
            step(cdir, st, "APPROVAL", f"round {r}: every check passed")

        if st.round_state == "APPROVAL":
            person, why = _needs_person(cdir, cfg, st, r)
            if person and cfg.approval_policy != "auto_demo":
                step(cdir, st, "APPROVAL", f"round {r} needs a person: {why}", "AWAITING_APPROVAL")
                _round_summary(cdir, rdir, cfg, r)
                return
            if person:
                _approve(cdir, r, "demo_auto", "demo_auto", f"stands in for a person: {why}")
            else:
                env = load_envelope(cdir)
                _approve(
                    cdir,
                    r,
                    "envelope",
                    env.approved_by,
                    "inside the approval envelope",
                    envelope_ref=f"envelope.json (from {env.approval_ref})",
                )
                env.auto_rounds_used += 1
                write_json(cdir / "envelope.json", env)
            step(cdir, st, "EXECUTING", f"round {r} approved")

        if st.round_state == "EXECUTING":
            if not verify_round(cdir, r):
                st.needs_human = (
                    f"round {r}: approval voided because the files changed after approval"
                )
                step(cdir, st, "APPROVAL", st.needs_human, "AWAITING_APPROVAL")
                _round_summary(cdir, rdir, cfg, r)
                return
            res = load_round_check(rdir)
            readings, results, qc = measure_and_analyze(
                res.plan,
                res.sim.ledger,
                assay,
                surface,
                cfg.seed,
                r,
                cfg.faults,
                expected,
                peaks_path=rdir / "raw" / "lcms_peaks.csv",
            )
            step(cdir, st, "ANALYZING", f"round {r}: plate read, {len(readings)} wells")

        if st.round_state == "ANALYZING":
            res = load_round_check(rdir)
            decision = Decision.model_validate_json(
                (rdir / "decision.json").read_text(encoding="utf-8")
            )
            # The analysis works from the raw peak table the instrument wrote.
            results, qc = analyze_plate(
                load_peaks(rdir / "raw" / "lcms_peaks.csv"),
                res.plan,
                volumes_by_well(res.sim.ledger, res.plan.plate),
                assay,
                expected,
                r,
            )
            approval = load_round_approval(rdir)
            decided_by = (
                "lhs"
                if r == 1
                else decision.strategy + (f":{decision.model}" if decision.model else "")
            )
            if decision.fallback:
                decided_by += " (fallback: optimizer)"
            rows = result_rows(
                results,
                assay,
                campaign_id=cfg.campaign_id,
                round_number=r,
                attempt=1,
                run_id=f"rounds/{r:02d}",
                design_hash=approval.design_hash if approval else res.design_hash,
                protocol_hash=approval.protocol_hash if approval else res.protocol_hash,
                decided_by=decided_by,
                timestamp=utc_now(),
            )
            write_rows(rdir / "results.csv", rows)
            write_json(rdir / "qc.json", qc)
            if r not in st.appended_rounds:
                store.append_results(cfg.campaign_id, rows)
                st.appended_rounds.append(r)
            if not qc.plate_ok:
                st.anomalies[str(r)] = "PLATE_QC_FAIL"
            _round_summary(cdir, rdir, cfg, r)
            st.round = r + 1
            step(
                cdir,
                st,
                "DECIDING",
                f"round {r} analyzed: plate {'passed' if qc.plate_ok else 'FAILED'} quality checks",
            )

    step(cdir, st, "DONE", f"{cfg.rounds} rounds complete", "DONE")
    from labauto.report import build_report

    build_report(cdir)


def _check_round(
    rdir: Path, decision: Decision, cfg: CampaignConfig, assay, workcell, r: int
) -> CheckResult:
    design = design_for_round(
        decision, assay, workcell.workcell_id, cfg.campaign_id, r, cfg.replicates, cfg.seed
    )
    result = check(design, assay, workcell, out_dir=rdir)
    write_check_files(rdir, result, assay)
    write_json(rdir / "check.json", result)
    return result


# ---------------------------------------------------------------- approval


def _needs_person(cdir: Path, cfg: CampaignConfig, st: CampaignState, r: int) -> tuple[bool, str]:
    if cfg.approval_policy == "always":
        return True, "policy is always"
    if cfg.approval_policy == "auto_demo":
        return True, "the demo has no person"
    env = load_envelope(cdir)
    if env is None:
        return True, "no approval envelope yet (round 1 is approved by a person)"
    res = load_round_check(round_dir(cdir, r))
    wells = len(res.plan.wells) if res and res.plan else 0
    if env.assay_id != cfg.assay_id or env.workcell_id != cfg.workcell_id:
        return True, "assay or workcell differs from the envelope"
    if wells > env.wells_approved:
        return True, f"{wells} wells is more than the {env.wells_approved} approved"
    if res is None or not res.ok:
        return True, "a check did not pass"
    prev = st.anomalies.get(str(r - 1))
    if prev:
        return True, f"the previous round had an anomaly ({prev})"
    if env.auto_rounds_used >= env.max_auto_rounds:
        return True, f"the envelope's {env.max_auto_rounds} automatic rounds are used up"
    return False, ""


def _approve(
    cdir: Path, r: int, mode: str, by: str, comment: str, envelope_ref: str | None = None
) -> Approval:
    rdir = round_dir(cdir, r)
    d_hash, p_hash = round_hashes(rdir)
    approval = Approval(
        decision="approved",
        mode=mode,  # type: ignore[arg-type]
        by=by,
        at=utc_now(),
        comment=comment,
        design_hash=d_hash,
        protocol_hash=p_hash,
        envelope_ref=envelope_ref,
    )
    write_json(rdir / "approval.json", approval)
    return approval


def round_hashes(rdir: Path) -> tuple[str | None, str | None]:
    """Recompute the design and protocol hashes from a round's files."""
    path = rdir / "design.json"
    if not path.exists():
        return None, None
    d_hash = design_hash(Design.model_validate_json(path.read_text(encoding="utf-8")))
    res = load_round_check(rdir)
    art = rdir / Path(res.artifact_path).name if res and res.artifact_path else rdir / "protocol.py"
    return d_hash, file_hash(art) if art.exists() else None


def approve_waiting_round(cdir: Path, by: str | None = None, comment: str = "") -> Approval:
    """A person approves the round the campaign is waiting on."""
    cfg = load_config(cdir)
    st = load_cstate(cdir)
    if st.state != "AWAITING_APPROVAL":
        raise CampaignError(f"campaign {cfg.campaign_id} is {st.state}, not AWAITING_APPROVAL")
    r = st.round
    res = load_round_check(round_dir(cdir, r))
    if (res.design_hash, res.protocol_hash) != round_hashes(round_dir(cdir, r)):
        raise CampaignError(
            f"round {r}'s files changed after the check; the hashes no longer match"
        )
    approval = _approve(cdir, r, "human", by or "unnamed person", comment)
    if cfg.approval_policy == "envelope" and load_envelope(cdir) is None:
        env = Envelope(
            assay_id=cfg.assay_id,
            workcell_id=cfg.workcell_id,
            wells_approved=len(res.plan.wells),
            approved_by=approval.by,
            approved_at=approval.at,
            approval_ref=f"rounds/{r:02d}/approval.json",
            max_auto_rounds=MAX_AUTO_ROUNDS,
        )
        write_json(cdir / "envelope.json", env)
    _round_summary(cdir, round_dir(cdir, r), cfg, r)
    return approval


def verify_round(cdir: Path, r: int) -> bool:
    """Before a round is executed, recompute its hashes. A mismatch voids the approval."""
    rdir = round_dir(cdir, r)
    approval = load_round_approval(rdir)
    if approval is None or approval.decision != "approved":
        return False
    if round_hashes(rdir) == (approval.design_hash, approval.protocol_hash):
        return True
    approval.decision = "voided"
    approval.void_reason = "design or protocol hash changed after approval"
    write_json(rdir / "approval.json", approval)
    return False


# ---------------------------------------------------------------- round summary


def _round_summary(cdir: Path, rdir: Path, cfg: CampaignConfig, r: int) -> None:
    res = load_round_check(rdir)
    decision = Decision.model_validate_json((rdir / "decision.json").read_text(encoding="utf-8"))
    approval = load_round_approval(rdir)
    st = load_cstate(cdir)
    out = [f"# Campaign {cfg.campaign_id}, round {r}", ""]
    out += [
        "## Decision",
        "",
        f"- strategy: {decision.strategy}" + (f" ({decision.model})" if decision.model else ""),
    ]
    if decision.fallback:
        out.append(f"- fallback: {decision.fallback}")
    if decision.round_note:
        out.append(f"- round note (model written): {decision.round_note}")
    out.append(f"- tool calls: {len(decision.tool_calls)}")
    out.append("")
    if approval:
        mode = (
            "demo_auto (no person approved this)" if approval.mode == "demo_auto" else approval.mode
        )
        out += [
            "## Approval",
            "",
            f"- {approval.decision}, mode {mode}, by {approval.by}, at {approval.at}",
        ]
        if approval.envelope_ref:
            out.append(f"- envelope: {approval.envelope_ref}")
        out.append("")
    elif st.state == "AWAITING_APPROVAL" and st.round == r:
        out += [
            "## Approval",
            "",
            f"Waiting for a person: `labauto approve {cdir}`, then `labauto resume {cdir}`.",
            "",
        ]
    qc_path = rdir / "qc.json"
    if qc_path.exists():
        qc = QcReport.model_validate_json(qc_path.read_text(encoding="utf-8"))
        out += ["## Quality checks", "", f"- plate: {'passed' if qc.plate_ok else 'FAILED'}"]
        if qc.negative_mean is not None:
            out.append(
                f"- negative control mean {qc.negative_mean:.1f}% (limit below {qc.negative_limit:g}%)"
            )
        if qc.reference_mean is not None:
            out.append(
                f"- reference control mean {qc.reference_mean:.1f}% (expected {qc.reference_expected:.1f} +/- {qc.reference_tolerance:g})"
            )
        for e in qc.events:
            out.append(f"- {e.code} at {e.where}: {e.message}")
        out.append("")
    if res is not None:
        assay = load_assay(cfg.assay)
        workcell = load_workcell(cfg.workcell)
        files = sorted(p.name for p in rdir.iterdir() if p.is_file())
        body = render_summary(
            f"{cfg.campaign_id} round {r}", res, assay, workcell, "decision.json", files
        )
        out += body.split("\n", 2)[2].splitlines()
    (rdir / "summary.md").write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def campaign_status(cdir: Path) -> str:
    cfg = load_config(cdir)
    st = load_cstate(cdir)
    lines = [
        f"Campaign {cfg.campaign_id}: {st.state}, round {min(st.round, cfg.rounds)} of {cfg.rounds} ({st.round_state})"
    ]
    if st.needs_human:
        lines.append(f"Needs a person: {st.needs_human}")
    for k, v in sorted(st.anomalies.items()):
        lines.append(f"Anomaly in round {k}: {v}")
    if (cdir / "report.md").exists():
        lines.append(f"Report: {cdir / 'report.md'}")
    return "\n".join(lines)


def dump(cdir: Path) -> dict:
    return json.loads((cdir / "state.json").read_text(encoding="utf-8"))
