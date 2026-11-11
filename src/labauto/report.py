"""The campaign report, built by code from the results.

Numbers come from code (principle P7). A model may write a short note, but
before it is included, code pulls every number out of it and checks that each
one appears in the report's tables. If any does not, the note is dropped.
"""

from __future__ import annotations

import os
import re
import statistics
from pathlib import Path

from labauto.config import fmt, load_assay, repo_root
from labauto.decide.base import build_history, recommendation
from labauto.models import Decision, QcReport

NOTE_REJECTED = "Model note rejected: it contained numbers that are not in the data."
# Every run of digits counts, with or without a leading dot, even inside a word
# ("a99", "n=3", ".9"), so a number cannot slip past the check by its spelling.
_NUMBER = re.compile(r"(?<![\d.])\.?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
MAX_NOTE_WORDS = 120


def numbers_in(text: str) -> list[str]:
    return _NUMBER.findall(text)


def check_note_numbers(note: str, tables: str) -> list[str]:
    """Numbers in the note that do not appear in the tables after rounding. Empty means the note passes."""
    table_values = [float(x) for x in numbers_in(tables)]
    bad = []
    for token in numbers_in(note):
        value = float(token)
        mantissa = re.split(r"[eE]", token)[0]
        places = len(mantissa.split(".", 1)[1]) if "." in mantissa else 0
        if not any(round(t, places) == round(value, places) for t in table_values):
            bad.append(token)
    return bad


def _f(x: float | None, places: int = 1) -> str:
    return "" if x is None else f"{x:.{places}f}"


def _cond(c: dict[str, float]) -> str:
    return ", ".join(f"{k} {fmt(v)}" for k, v in c.items())


NOTE_EMPTY = "Model note rejected: the model returned no note."


def model_note_writer(clients, cdir: Path):
    """A note writer that asks the report_note endpoint for a short note on the tables.

    Returns (writer, source): the source says whether a model or a scripted
    client wrote it. Failures give no note, never a made up one.
    """
    from labauto.agents.llm import Transcript, chat
    from labauto.agents.planner import load_prompt
    from labauto.agents.router import estimate_tokens, route

    def write(tables: str) -> tuple[str, str]:
        messages = [
            {"role": "system", "content": load_prompt("report_note.md")},
            {"role": "user", "content": tables},
        ]
        decision = route("report_note", estimate_tokens(messages), None, cdir / "model_calls.jsonl")
        try:
            result = chat(
                clients.local,
                decision.label,
                messages,
                temperature=0.3,
                transcript=Transcript(cdir / "transcript.jsonl"),
                role="report_note",
                meta={"scope": "report_note", "attempt": 1},
                max_calls=1,
                text_only=True,
            )
        except Exception as exc:  # no note is better than a made up one
            return "", f"the model could not be reached ({type(exc).__name__})"
        source = (
            "a scripted client (hand-written text, not a model)"
            if result.model == "scripted"
            else f"model {result.model or 'unknown'}"
        )
        return (result.parsed or {}).get("text", ""), source

    return write


def build_report(cdir: Path, note_writer=None) -> Path:
    """Write report.md and progress.png for a campaign. note_writer(tables) -> str is optional."""
    from labauto.campaign import (
        decided_by_text,
        load_config,
        load_cstate,
        load_round_approval,
        round_dir,
    )
    from labauto.store import LocalStore

    cfg = load_config(cdir)
    st = load_cstate(cdir)
    assay = load_assay(cfg.assay)
    rows = LocalStore(cdir.parent).load_results(cfg.campaign_id)
    done = sorted({int(r["round"]) for r in rows})

    # Rounds table: best measured mean so far, quality events, approval mode, fallbacks, model.
    round_lines = [
        "| Round | Best measured mean so far (%) | Quality events | Approval | Fallback | Decided by |",
        "|---|---|---|---|---|---|",
    ]
    best_by_round = []
    for r in done:
        history = build_history([x for x in rows if int(x["round"]) <= r], assay, r + 1)
        rec = recommendation(history)
        best = rec["mean"] if rec else None
        best_by_round.append(best)
        rdir = round_dir(cdir, r)
        qc_path = rdir / "qc.json"
        qc = (
            QcReport.model_validate_json(qc_path.read_text(encoding="utf-8"))
            if qc_path.exists()
            else None
        )
        events = sorted({e.code for e in qc.events}) if qc else []
        approval = load_round_approval(rdir)
        decision = Decision.model_validate_json(
            (rdir / "decision.json").read_text(encoding="utf-8")
        )
        by = decided_by_text(decision)
        round_lines.append(
            f"| {r} | {_f(best)} | {', '.join(events) or 'none'} | {approval.mode if approval else 'none'} "
            f"| {decision.fallback or 'none'} | {by} |"
        )

    history = build_history(rows, assay, (done[-1] if done else 0) + 1)
    measured = sorted((s for s in history.stats if s.mean is not None), key=lambda s: -s.mean)[:5]
    top_lines = ["| Condition | Mean (%) | SD | n | Flags |", "|---|---|---|---|---|"]
    for s in measured:
        top_lines.append(
            f"| {_cond(s.conditions)} | {_f(s.mean)} | {_f(s.sd)} | {s.n} | {', '.join(s.flags) or 'none'} |"
        )

    rec = recommendation(history)
    trace_lines = []
    if rec:
        names = list(assay.factors)
        for x in rows:
            if x["role"] != "sample":
                continue
            if all(abs(float(x[f"designed_{f}"]) - rec["conditions"][f]) < 1e-9 for f in names):
                trace_lines.append(
                    f"| {x['run_id']} | {x['well']} | {_f(float(x['yield_pct']))} | {x['excluded']} "
                    f"| {x['design_hash'][:16]} | {x['protocol_hash'][:16]} |"
                )

    plot_progress(done, best_by_round, cdir / "progress.png")

    tables = "\n".join(round_lines + top_lines)
    out = [
        f"# Campaign report: {cfg.campaign_id}",
        "",
        "Built by code from `results_all.csv`. The data are synthetic: a made up yield surface read by a simulated LC/MS.",
        "",
        "## Settings",
        "",
        f"- brief: {cfg.brief or '(none)'}",
        f"- strategy: {cfg.strategy}; rounds: {cfg.rounds}; conditions per round: {cfg.conditions_per_round}; replicates: {cfg.replicates}",
        f"- seed: {cfg.seed}; surface seed: {cfg.surface_seed}; approval: {cfg.approval_policy}; faults: {', '.join(cfg.faults) or 'none'}",
        f"- assay: {cfg.assay_id}; workcell: {cfg.workcell_id}; state: {st.state}",
        "",
        "## Rounds",
        "",
        *round_lines,
        "",
        "![Progress](progress.png)",
        "",
        "## Top 5 conditions",
        "",
        *top_lines,
        "",
        "## Recommendation",
        "",
    ]
    if rec:
        out.append(
            f"The condition with the highest measured mean: {_cond(rec['conditions'])}, "
            f"mean {_f(rec['mean'])}% over {rec['n']} valid wells."
        )
        out += [
            "",
            "## Traceability",
            "",
            "Every well behind the recommendation, with the hashes of the design and protocol that made it.",
            "",
            "| Run folder | Well | Yield (%) | Excluded | Design hash | Protocol hash |",
            "|---|---|---|---|---|---|",
            *trace_lines,
        ]
    else:
        out.append("No condition has a valid measurement yet.")
    out += ["", "## Model note", ""]
    if note_writer is None:
        out.append("None. No live model was available to write a note for this campaign.")
    else:
        written = note_writer(tables)
        note, source = written if isinstance(written, tuple) else (written, "a model")
        note = (note or "").strip()
        words = len(note.split())
        bad = check_note_numbers(note, tables)
        if not note:
            out.append(
                NOTE_EMPTY + (f" ({source})" if source.startswith("the model could not") else "")
            )
        elif bad:
            out.append(NOTE_REJECTED)
        elif words > MAX_NOTE_WORDS:
            out.append(f"Model note rejected: it has {words} words, the limit is {MAX_NOTE_WORDS}.")
        else:
            out += [
                f"Written by {source} from the tables above, not by code. Every number in it was checked against the tables.",
                "",
                f"> {note}",
            ]
    path = cdir / "report.md"
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


def plot_progress(rounds: list[int], best: list[float | None], path: Path) -> None:
    """progress.png: the best measured mean so far, by round."""
    os.environ.setdefault("MPLCONFIGDIR", str(repo_root() / ".labauto" / "mpl"))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(5.5, 3.2), dpi=110)
    pts = [(r, b) for r, b in zip(rounds, best, strict=True) if b is not None]
    if pts:
        ax.plot([p[0] for p in pts], [p[1] for p in pts], marker="o", color="#1f6fb4")
    ax.set_xlabel("Round")
    ax.set_ylabel("Best measured mean (%)")
    ax.set_xticks(rounds or [1])
    ax.grid(alpha=0.3)
    ax.set_title("Best measured mean by round")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def mean_or_none(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None
