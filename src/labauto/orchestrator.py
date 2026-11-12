"""The orchestrator: a state machine that runs the planner, the checks, and the ladder.

Code decides control flow. The model only fills the planner role inside the
PLANNING state (principle P1). State is written to state.json after every
transition, so a run can be resumed after a crash. Resuming never repeats a
model call whose reply is already in transcript.jsonl.

States: RECEIVED, PLANNING, CHECKING, REPAIRING, AWAITING_APPROVAL, APPROVED,
REJECTED, NEEDS_HUMAN, FAILED.

The escalation ladder (principle P4):
- local rung: up to 3 attempts (one first try and two repairs),
- hosted rung: 1 attempt that sees the full history, if configured,
- then a person.
A rung is left early when an attempt repeats the previous attempt's errors.
An error that needs a person skips the rest of the ladder.
"""

from __future__ import annotations

import json
import os
import shutil
import traceback
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from labauto.agents.llm import ChatClient, Transcript
from labauto.agents.planner import build_messages, propose, to_design
from labauto.agents.router import estimate_tokens, hosted_route, route
from labauto.config import Settings, load_assay, load_workcell
from labauto.instruments.base import scrub_paths
from labauto.models import (
    AttemptRecord,
    CheckResult,
    CreatedBy,
    NeedsHuman,
    PlanError,
    RunState,
    Transition,
)
from labauto.pipeline import check
from labauto.record import (
    check_record,
    new_run_dir,
    portable,
    utc_now,
    write_check_files,
    write_json,
)

RESTING = {"AWAITING_APPROVAL", "APPROVED", "REJECTED", "NEEDS_HUMAN"}
LOCAL_ATTEMPTS = 3
HOSTED_ATTEMPTS = 1


@dataclass
class Clients:
    """The chat clients for each rung. Scripted and replay runs use one client for both."""

    local: ChatClient
    hosted: ChatClient | None = None
    spec: str = ""


@dataclass
class Step:
    """What the ladder says to do next."""

    kind: str  # "attempt", "pass", or "handoff"
    rung: str = ""
    reason: str = ""


# ---------------------------------------------------------------- state files


def load_state(run_dir: Path) -> RunState:
    return RunState.model_validate_json((run_dir / "state.json").read_text(encoding="utf-8"))


def save_state(run_dir: Path, st: RunState) -> None:
    """Write state.json in one step (a temporary file, then a rename), so a crash never leaves half a file."""
    st.updated_at = utc_now()
    tmp = run_dir / "state.json.tmp"
    tmp.write_text(st.model_dump_json(indent=2, by_alias=True) + "\n", encoding="utf-8")
    os.replace(tmp, run_dir / "state.json")


def transition(run_dir: Path, st: RunState, to: str, reason: str) -> None:
    """Move to a new state, record why, and write state.json."""
    st.history.append(Transition(from_=st.state, to=to, at=utc_now(), reason=reason))
    st.state = to
    save_state(run_dir, st)


def attempt_dir(run_dir: Path, number: int, rung: str) -> Path:
    return run_dir / "attempts" / f"{number:02d}_{rung}"


def load_attempts(run_dir: Path) -> list[AttemptRecord]:
    """Completed attempts, in order. attempt.json is written last, so it marks completion."""
    out = []
    for path in sorted((run_dir / "attempts").glob("*/attempt.json")):
        out.append(AttemptRecord.model_validate_json(path.read_text(encoding="utf-8")))
    return sorted(out, key=lambda a: a.number)


def load_check(folder: Path) -> CheckResult | None:
    path = folder / "check.json"
    if not path.exists():
        return None
    return CheckResult.model_validate_json(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- the ladder


def stalled(attempts: list[AttemptRecord]) -> bool:
    """True when the last attempt repeated the previous attempt's errors (same codes, same places)."""
    return len(attempts) >= 2 and attempts[-1].signature() == attempts[-2].signature()


def next_step(attempts: list[AttemptRecord], settings: Settings, hosted_calls: int) -> Step:
    """Decide the next step from the attempt history alone."""
    if attempts and attempts[-1].ok:
        return Step("pass", reason=f"attempt {attempts[-1].number} passed every check")
    if attempts:
        human = [e for e in attempts[-1].errors if e.needs_human]
        if human:
            e = human[0]
            return Step(
                "handoff",
                reason=f"{e.code} at {e.where} needs a person, because no change to the design can fix it",
            )
    local = [a for a in attempts if a.rung == "local"]
    hosted = [a for a in attempts if a.rung == "hosted"]
    if not hosted and len(local) < LOCAL_ATTEMPTS and not stalled(local):
        return Step(
            "attempt",
            rung="local",
            reason=f"local rung, attempt {len(local) + 1} of {LOCAL_ATTEMPTS}",
        )
    why_local = (
        f"no progress: attempt {local[-1].number} repeated the errors of attempt {local[-2].number}"
        if stalled(local) and len(local) < LOCAL_ATTEMPTS
        else f"{len(local)} local attempts used"
    )
    if settings.hosted is None:
        return Step(
            "handoff", reason=f"{why_local}; hosted rung not configured; handing to a person"
        )
    if not hosted and settings.hosted_max_calls <= 0:
        return Step(
            "handoff",
            reason=f"{why_local}; hosted rung skipped: its call cap is {settings.hosted_max_calls}; handing to a person",
        )
    if len(hosted) < HOSTED_ATTEMPTS and hosted_calls < settings.hosted_max_calls:
        return Step("attempt", rung="hosted", reason=f"{why_local}; escalating to the hosted rung")
    return Step("handoff", reason=f"{why_local}; hosted rung used; handing to a person")


def hosted_calls_before(transcript: Transcript, number: int) -> int:
    """Hosted calls recorded for attempts before this one. Read from the transcript, so a
    resume never counts a recorded call twice."""
    return sum(
        1
        for r in transcript.records
        if r.get("scope") == "planner"
        and r.get("rung") == "hosted"
        and (r.get("attempt") or 0) < number
    )


# ---------------------------------------------------------------- running


def start_run(
    request: str,
    assay_path: str,
    workcell_path: str,
    clients: Clients,
    settings: Settings,
    runs_dir: str | Path = "runs",
    seed: int = 0,
) -> Path:
    """Create a run folder for a request and drive it as far as it can go."""
    run_dir = new_run_dir(runs_dir)
    (run_dir / "request.txt").write_text(request.strip() + "\n", encoding="utf-8")
    st = RunState(
        run_id=run_dir.name,
        state="RECEIVED",
        updated_at=utc_now(),
        assay=portable(assay_path),
        workcell=portable(workcell_path),
        seed=seed,
        client=clients.spec,
    )
    st.history.append(Transition(from_="", to="RECEIVED", at=utc_now(), reason="request received"))
    save_state(run_dir, st)
    advance(run_dir, clients, settings)
    return run_dir


def resume(run_dir: Path, clients: Clients, settings: Settings) -> RunState:
    """Continue a run from its saved state. A FAILED run goes back to where it failed."""
    st = load_state(run_dir)
    if st.state == "FAILED":
        back = st.history[-1].from_ if st.history else "RECEIVED"
        transition(run_dir, st, back or "RECEIVED", "resumed after a failure")
    if st.state in RESTING:
        _write_summary(run_dir, st)  # in case a crash came between the state change and the summary
        return st
    # Scripted and replay clients serve replies in order. Move them past the
    # replies the transcript already holds, so the next call gets the next reply.
    for rec in Transcript(run_dir / "transcript.jsonl").records:
        if rec.get("scope") != "planner":
            continue
        client = clients.hosted if rec.get("rung") == "hosted" and clients.hosted else clients.local
        if hasattr(client, "skip"):
            client.skip(1, rec.get("endpoint_label", "*"))
    return advance(run_dir, clients, settings)


def advance(run_dir: Path, clients: Clients, settings: Settings) -> RunState:
    """Run the ladder until the run rests: awaiting approval, needing a person, or failed."""
    st = load_state(run_dir)
    if st.state in RESTING or st.state == "FAILED":
        return st
    try:
        _drive(run_dir, st, clients, settings)
    except Exception as exc:  # any internal error ends in FAILED, with where it happened
        where = st.state
        (run_dir / "error.txt").write_text(scrub_paths(traceback.format_exc()), encoding="utf-8")
        st.needs_human = None
        transition(
            run_dir,
            st,
            "FAILED",
            f"{type(exc).__name__} during {where}: {scrub_paths(str(exc))[:300]}",
        )
        _write_summary(run_dir, st)
    return st


def _drive(run_dir: Path, st: RunState, clients: Clients, settings: Settings) -> None:
    assay = load_assay(st.assay)
    workcell = load_workcell(st.workcell)
    request = (run_dir / "request.txt").read_text(encoding="utf-8")
    transcript = Transcript(run_dir / "transcript.jsonl")
    calls_log = run_dir / "model_calls.jsonl"
    attempts = load_attempts(run_dir)

    while True:
        used = hosted_calls_before(transcript, len(attempts) + 1)
        step = next_step(attempts, settings, used)
        if step.kind == "pass":
            _finish(run_dir, st, attempts[-1], step.reason)
            return
        for note in ("hosted rung not configured", "hosted rung skipped: its call cap is"):
            if note in step.reason:
                text = (
                    note if note.endswith("configured") else f"{note} {settings.hosted_max_calls}"
                )
                if text not in st.notes:
                    st.notes.append(text)
        if step.kind == "handoff":
            st.needs_human = NeedsHuman(kind="handoff", text=step.reason)
            transition(run_dir, st, "NEEDS_HUMAN", step.reason)
            _write_summary(run_dir, st)
            return

        number = len(attempts) + 1
        rung = step.rung
        if st.state != "PLANNING" or st.attempt != number:
            st.rung, st.attempt = rung, number
            transition(run_dir, st, "PLANNING", step.reason)

        messages = build_messages(assay, workcell, request, attempts)
        est = estimate_tokens(messages)
        log_extra = {"attempt": number, "rung": rung}
        if rung == "hosted":
            decision = hosted_route(est, settings, calls_log, log_extra)
            client = clients.hosted or clients.local
            max_calls = max(1, min(2, settings.hosted_max_calls - used))
        else:
            decision = route(
                "planner" if number == 1 else "repair", est, settings, calls_log, log_extra
            )
            client = clients.local
            max_calls = 2
        if decision.trim:
            messages = build_messages(assay, workcell, request, attempts, trim=True)
        result = propose(
            client,
            decision.label,
            messages,
            first=number == 1,
            transcript=transcript,
            meta={"scope": "planner", "attempt": number, "rung": rung},
            max_calls=max_calls,
            assay=assay,
        )
        if rung == "hosted":
            st.hosted_calls = used + result.calls
        transition(
            run_dir, st, "CHECKING", f"attempt {number}: reply received from {decision.label}"
        )

        record = _check_attempt(run_dir, number, rung, result, assay, workcell, st.seed)
        attempts.append(record)
        if record.ok:
            continue
        codes = ", ".join(sorted({e.code for e in record.errors}))
        transition(run_dir, st, "REPAIRING", f"attempt {number} failed: {codes}")


def _check_attempt(run_dir, number, rung, result, assay, workcell, seed) -> AttemptRecord:
    folder = attempt_dir(run_dir, number, rung)
    folder.mkdir(parents=True, exist_ok=True)
    record = AttemptRecord(
        number=number,
        rung=rung,
        endpoint_label=result.endpoint_label,
        model=result.model,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        seconds=result.seconds,
        calls=result.calls,
    )
    design = None
    if result.parsed is None:
        record.reply_error = result.error
        record.errors = [_schema_error(result.error or "no reply")]
    else:
        try:
            design = to_design(
                result.parsed,
                assay,
                workcell,
                design_id=f"d-{run_dir.name}-{number:02d}",
                created_by=CreatedBy(
                    role="model", model=result.model or None, rung=result.endpoint_label
                ),
                seed=seed,
            )
        except ValidationError as exc:
            record.reply_error = str(exc)
            record.errors = [_schema_error(str(exc))]
    if design is not None:
        res = check(design, assay, workcell, out_dir=folder)
        write_check_files(folder, res, assay)
        write_json(folder / "check.json", check_record(res))
        record.design = res.design.model_dump(mode="json") if res.design else None
        record.errors = res.errors
        record.ok = res.ok
        record.failed_layer = res.failed_layer
        record.design_hash = res.design_hash
        record.protocol_hash = res.protocol_hash
    else:
        write_json(folder / "errors.json", record.errors)
        record.failed_layer = "design"
    write_json(folder / "attempt.json", record)
    return record


def _schema_error(text: str) -> PlanError:
    return PlanError(
        code="SCHEMA_INVALID",
        layer="design",
        where="reply",
        message=f"The planner's reply could not be used as a design. {text}"[:1500],
        fix_hint="Reply with exactly one JSON object in the reply format.",
        options=["fix_format"],
        short="reply",
    )


def _finish(run_dir: Path, st: RunState, passing: AttemptRecord, reason: str) -> None:
    """Copy the passing attempt to final/ and wait for a person."""
    src = attempt_dir(run_dir, passing.number, passing.rung)
    final = run_dir / "final"
    if final.exists():
        shutil.rmtree(final)
    shutil.copytree(src, final)
    st.final_attempt = passing.number
    st.design_hash = passing.design_hash
    st.protocol_hash = passing.protocol_hash
    st.needs_human = None
    transition(run_dir, st, "AWAITING_APPROVAL", reason)
    _write_summary(run_dir, st)


def _write_summary(run_dir: Path, st: RunState) -> None:
    from labauto.approval import write_run_summary

    write_run_summary(run_dir, st)


def run_summary_dict(run_dir: Path) -> dict:
    """A short machine readable view of a run, for the CLI and the demo."""
    st = load_state(run_dir)
    attempts = load_attempts(run_dir)
    final = load_check(run_dir / "final") if (run_dir / "final").exists() else None
    design = final.design if final else None
    return {
        "run_id": st.run_id,
        "state": st.state,
        "attempts": len(attempts),
        "codes": [sorted({e.code for e in a.errors}) for a in attempts],
        "deviations": len(design.deviations) if design else 0,
        "wells": len(final.plan.wells) if final and final.plan else 0,
        "needs_human": st.needs_human.text if st.needs_human else None,
        "design": json.loads(design.model_dump_json()) if design else None,
    }
