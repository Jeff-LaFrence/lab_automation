"""The approval gate: what a person sees, and how approval is tied to the exact protocol.

Approval records the design hash and the protocol hash. Before anything uses
an approved run, both are recomputed from the files. A mismatch voids the
approval and returns the run to AWAITING_APPROVAL (principle P6).

Modes: human (a person), envelope (inside limits a person approved earlier),
demo_auto (the unattended demo; always labelled as such).
"""

from __future__ import annotations

import json
from pathlib import Path

from labauto.config import load_assay, load_workcell
from labauto.models import Approval, Design, RunState
from labauto.pipeline import design_hash, file_hash, sha256_text
from labauto.record import display_path, render_summary, utc_now, write_json

MODES = ("human", "envelope", "demo_auto")
UNNAMED = "unnamed person"


class ApprovalError(RuntimeError):
    """The run cannot be approved or rejected in its current state."""


def _state(run_dir: Path) -> RunState:
    from labauto.orchestrator import load_state

    return load_state(run_dir)


def current_hashes(run_dir: Path) -> tuple[str | None, str | None]:
    """Recompute the design and protocol hashes from the files in final/."""
    from labauto.orchestrator import load_check

    final = run_dir / "final"
    design_path = final / "design.json"
    if not design_path.exists():
        d_hash = None
    else:
        raw = design_path.read_text(encoding="utf-8")
        try:
            d_hash = design_hash(Design.model_validate_json(raw))
        except ValueError:
            # A design file that no longer reads is a change: its hash cannot match.
            d_hash = "unreadable:" + sha256_text(raw)
    try:
        res = load_check(final)
    except ValueError:
        res = None
    artifact = (
        final / Path(res.artifact_path).name if res and res.artifact_path else final / "protocol.py"
    )
    p_hash = file_hash(artifact) if artifact.exists() else None
    return d_hash, p_hash


def load_approval(run_dir: Path) -> Approval | None:
    path = run_dir / "approval.json"
    if not path.exists():
        return None
    return Approval.model_validate_json(path.read_text(encoding="utf-8"))


def approve(
    run_dir: Path,
    by: str | None = None,
    comment: str = "",
    mode: str = "human",
    envelope_ref: str | None = None,
) -> Approval:
    """Approve a run that is awaiting approval. Refuses if the files changed since the check."""
    from labauto.orchestrator import save_state, transition

    if mode not in MODES:
        raise ApprovalError(f"unknown approval mode '{mode}'")
    st = _state(run_dir)
    if st.state != "AWAITING_APPROVAL":
        raise ApprovalError(f"run {st.run_id} is {st.state}, not AWAITING_APPROVAL")
    d_hash, p_hash = current_hashes(run_dir)
    if (d_hash, p_hash) != (st.design_hash, st.protocol_hash):
        raise ApprovalError(
            "the design or protocol in final/ changed after the check; the hashes no longer match"
        )
    approval = Approval(
        decision="approved",
        mode=mode,  # type: ignore[arg-type]
        by=by or ("demo_auto" if mode == "demo_auto" else UNNAMED),
        at=utc_now(),
        comment=comment,
        design_hash=d_hash,
        protocol_hash=p_hash,
        envelope_ref=envelope_ref,
    )
    write_json(run_dir / "approval.json", approval)
    label = (
        "demo_auto (unattended demo, no person)"
        if mode == "demo_auto"
        else f"{mode} by {approval.by}"
    )
    transition(run_dir, st, "APPROVED", f"approved: {label}")
    save_state(run_dir, st)
    write_run_summary(run_dir, st)
    return approval


def reject(run_dir: Path, reason: str, by: str | None = None) -> Approval:
    """Reject a run that is awaiting approval."""
    from labauto.orchestrator import transition

    st = _state(run_dir)
    if st.state != "AWAITING_APPROVAL":
        raise ApprovalError(f"run {st.run_id} is {st.state}, not AWAITING_APPROVAL")
    d_hash, p_hash = current_hashes(run_dir)
    approval = Approval(
        decision="rejected",
        mode="human",
        by=by or UNNAMED,
        at=utc_now(),
        comment=reason,
        design_hash=d_hash,
        protocol_hash=p_hash,
    )
    write_json(run_dir / "approval.json", approval)
    transition(run_dir, st, "REJECTED", f"rejected: {reason}")
    write_run_summary(run_dir, st)
    return approval


def verify(run_dir: Path) -> bool:
    """Check an approved run before it is used. A hash mismatch voids the approval."""
    from labauto.orchestrator import transition

    st = _state(run_dir)
    approval = load_approval(run_dir)
    if st.state != "APPROVED" or approval is None or approval.decision != "approved":
        return False
    d_hash, p_hash = current_hashes(run_dir)
    if (d_hash, p_hash) == (approval.design_hash, approval.protocol_hash):
        return True
    changed = [
        n
        for n, a, b in (
            ("design", d_hash, approval.design_hash),
            ("protocol", p_hash, approval.protocol_hash),
        )
        if a != b
    ]
    approval.decision = "voided"
    approval.void_reason = f"{' and '.join(changed)} hash changed after approval"
    write_json(run_dir / "approval.json", approval)
    transition(run_dir, st, "AWAITING_APPROVAL", f"approval voided: {approval.void_reason}")
    write_run_summary(run_dir, st)
    return False


# ---------------------------------------------------------------- summary.md


def _attempt_lines(run_dir: Path, st: RunState) -> list[str]:
    from labauto.orchestrator import load_attempts

    lines = []
    for a in load_attempts(run_dir):
        status = "passed" if a.ok else "failed"
        codes = ", ".join(sorted({e.code for e in a.errors}))
        model = f" ({a.model})" if a.model else ""
        lines.append(f"- attempt {a.number}  {a.endpoint_label}{model}  {status}  {codes}".rstrip())
    for note in st.notes:
        lines.append(f"- {note}")
    return lines or ["- no attempts yet"]


def write_run_summary(run_dir: Path, st: RunState) -> Path:
    """summary.md for a planned run: the approval header above the Stage 1 content."""
    from labauto.orchestrator import load_attempts, load_check

    request = (run_dir / "request.txt").read_text(encoding="utf-8").strip()
    approval = load_approval(run_dir)
    title = st.state
    if st.state == "APPROVED" and approval:
        title += (
            " (demo_auto, no person)"
            if approval.mode == "demo_auto"
            else f" ({approval.mode}, {approval.by})"
        )
    out = [f"# Run {st.run_id}: {title}", "", "## Request", "", f'> "{request}"', ""]

    final = load_check(run_dir / "final") if (run_dir / "final").exists() else None
    attempts = load_attempts(run_dir)
    shown = final
    if shown is None and attempts:
        last = attempts[-1]
        from labauto.orchestrator import attempt_dir

        shown = load_check(attempt_dir(run_dir, last.number, last.rung))
    design = shown.design if shown else None

    if design is not None:
        out += ["## Changes from what was asked", "", "From the planner's deviations list.", ""]
        if design.deviations:
            for d in design.deviations:
                out.append(f"- asked: {d.requested}   planned: {d.planned}   why: {d.reason}")
        else:
            out.append("- none declared: the planner says this design is exactly what was asked")
        out += ["", "## Assumptions", ""]
        out += [f"- {a}" for a in design.assumptions] or ["- none listed"]
        out.append("")

    out += ["## How we got here", ""] + _attempt_lines(run_dir, st) + [""]

    if st.state == "AWAITING_APPROVAL":
        where = display_path(run_dir)
        out += [
            f"To approve: `labauto approve {where}`    "
            f'To reject: `labauto reject {where} --reason "..."`',
            "",
        ]
    if st.state == "NEEDS_HUMAN" and st.needs_human:
        out += ["## Why a person is needed", "", st.needs_human.text, ""]
    if st.state == "FAILED" and st.history:
        out += [
            "## Internal error",
            "",
            st.history[-1].reason,
            "",
            "See error.txt. `labauto resume` retries.",
            "",
        ]
    if approval is not None:
        out += ["## Approval", ""]
        mode = (
            "demo_auto (unattended demo, no person approved this)"
            if approval.mode == "demo_auto"
            else approval.mode
        )
        out += [
            f"- decision: {approval.decision}",
            f"- mode: {mode}",
            f"- by: {approval.by}",
            f"- at: {approval.at}",
        ]
        if approval.comment:
            out.append(f"- comment: {approval.comment}")
        if approval.envelope_ref:
            out.append(f"- envelope: {approval.envelope_ref}")
        if approval.void_reason:
            out.append(f"- voided: {approval.void_reason}")
        out.append("")

    if shown is not None:
        assay = load_assay(st.assay)
        workcell = load_workcell(st.workcell)
        from labauto.orchestrator import attempt_dir

        folder = (
            run_dir / "final"
            if final is not None
            else attempt_dir(run_dir, attempts[-1].number, attempts[-1].rung)
        )
        where = folder.relative_to(run_dir).as_posix()
        files = [f"{where}/{p.name}" for p in sorted(folder.glob("*")) if p.is_file()]
        body = render_summary(st.run_id, shown, assay, workcell, f"request.txt ({where})", files)
        out += body.split("\n", 2)[2].splitlines()
    path = run_dir / "summary.md"
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    return path


def status_text(run_dir: Path) -> str:
    """A few lines for labauto status."""
    st = _state(run_dir)
    lines = [f"Run {st.run_id}: {st.state}"]
    if st.needs_human:
        lines.append(f"Needs a person ({st.needs_human.kind}): {st.needs_human.text}")
    lines += [line.lstrip("- ") for line in _attempt_lines(run_dir, st)]
    approval = load_approval(run_dir)
    if approval:
        lines.append(
            f"Approval: {approval.decision} ({approval.mode}, {approval.by}, {approval.at})"
        )
    lines.append(f"Summary: {run_dir / 'summary.md'}")
    return "\n".join(lines)


def approval_json(run_dir: Path) -> dict | None:
    path = run_dir / "approval.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
