"""Data review commands: review (the overwatch model on one round) and eval-reviewer (fault detection evaluation).

The overwatch is the generalist main model. Its transcript and route log for a
round sit next to the evidence pack, under rounds/NN/review/.

eval-reviewer runs one overwatch prompt per run (--prompt default or
no_hints). Each prompt has its own default page under docs/, and only a live
client may write a default page, whatever spelling of its path is given.
After the model's cases it runs the two fixed baselines on --baseline-seeds
extra seeds per case (0 skips this). That sweep calls no model.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from labauto.record import utc_now

EXIT_OK, EXIT_NEEDS_PERSON, EXIT_FAILED = 0, 1, 2
QC_ONLY = "qc-only"
QC_ONLY_LABEL = "qc-only (fixed rules, no model)"
SCRIPTED_LABEL = "a scripted client (hand-written replies, not a model)"
UNNAMED_LABEL = "a client that reported no model name (treated as scripted, not a model)"
NO_REPLY_LABEL = "no one: the reviewer model gave no reply"


def review_dir(campaign: Path, round_number: int) -> Path:
    """Where a round's evidence pack and review live: rounds/NN/review/."""
    return campaign / "rounds" / f"{round_number:02d}" / "review"


def reviewer_kind(spec: str, info: dict) -> str:
    """qc-only, none (no reply), scripted (a scripted client or no model name), or model."""
    from labauto.review import NOT_A_MODEL

    if spec == QC_ONLY:
        return QC_ONLY
    if not info.get("replies"):
        return "none"
    return "scripted" if (info.get("model") or "") in NOT_A_MODEL else "model"


def who_reviewed(spec: str, info: dict) -> str:
    """Say who wrote the review, from what answered, never from a file name.

    No reply is decided from the reply count. A reply with an empty or unknown
    model name is treated like a scripted one.
    """
    kind = reviewer_kind(spec, info)
    if kind == QC_ONLY:
        return QC_ONLY_LABEL
    if kind == "none":
        return NO_REPLY_LABEL
    model = info.get("model") or ""
    if kind == "scripted":
        return SCRIPTED_LABEL if model == "scripted" else UNNAMED_LABEL
    if spec.startswith("replay:"):
        return f"model {model}, replayed from {Path(spec.partition(':')[2]).name}"
    return f"model {model} ({info.get('label', 'local-main')})"


def next_scope(transcript, round_number: int) -> str:
    """A fresh transcript scope for this review, so an earlier review's replies are never reused."""
    prefix = f"data_reviewer-r{round_number:02d}-"
    used = {
        r.get("scope", "") for r in transcript.records if str(r.get("scope", "")).startswith(prefix)
    }
    return f"{prefix}{len(used) + 1}"


def _cell(text: str) -> str:
    return " ".join(str(text).replace("|", "/").split())


def render_review(record: dict) -> str:
    """review.md: the review in words a scientist can read, with who wrote it."""
    from labauto.review import CHECKED_RULES

    review = record["review"]
    lines = [
        f"# Data review: round {record['round']}",
        "",
        "The data are synthetic: simulated LC/MS and 1H NMR readouts of a made up yield surface.",
        "",
        f"- Reviewed by: {record['reviewed_by']}",
        f"- Evidence: evidence.json, NMR evaluator: {record.get('evaluator') or 'not named'}. "
        "The reviewer saw only this file.",
        f"- Written: {record['created_at']}",
    ]
    if record["reviewer_kind"] != QC_ONLY:
        lines.append("- Checked by code: " + "; ".join(CHECKED_RULES) + ".")
    lines.append("")
    if review is None:
        lines += [
            f"**No valid review.** {record['note']}",
            "",
            "## Replies and the problems code found",
            "",
        ]
        for a in record["attempts"]:
            msgs = [p["message"] for p in a.get("problems") or []]
            lines.append(f"- Reply {a['attempt']}: " + (" ".join(msgs) if msgs else "no problems"))
        return "\n".join(lines) + "\n"
    lines += [
        f"Round verdict: **{review['round_verdict']}**",
        "",
        "| Condition | Verdict | Reason | Evidence |",
        "|---|---|---|---|",
    ]
    for v in review["conditions"]:
        lines.append(
            f"| {v['key']} | {v['verdict']} | {_cell(v['reason'])} | {_cell(', '.join(v['evidence']))} |"
        )
    lines += ["", "## Suspected faults", ""]
    if not review["suspected_faults"]:
        lines.append("None named.")
    for f in review["suspected_faults"]:
        wells = ", ".join(f["wells"]) if f["wells"] else "plate-wide"
        lines.append(f"- {f['kind']} ({wells}): {_cell(f['reason'])}")
    lines += ["", "## Summary", ""]
    if record["reviewer_kind"] == QC_ONLY:
        lines.append("Written by code from fixed rules.")
    else:
        lines.append(f"Written by {record['reviewed_by']}, not by code.")
    lines += ["", f"> {_cell(review['summary']) or '(empty)'}"]
    if record["attempts"] and len(record["attempts"]) > 1:
        lines += ["", "## Earlier replies that code rejected", ""]
        for a in record["attempts"][:-1]:
            msgs = [p["message"] for p in a.get("problems") or []]
            lines.append(f"- Reply {a['attempt']}: " + " ".join(msgs))
    return "\n".join(lines) + "\n"


def cmd_review(args: argparse.Namespace) -> int:
    from labauto.agents.llm import ReplayClient, Transcript
    from labauto.agents.router import estimate_tokens, route
    from labauto.cli_plan import ConfigError, make_clients, settings_for
    from labauto.review import ROLE, qc_only_review, review_messages, review_round

    cdir = Path(args.campaign)
    rdir = review_dir(cdir, args.round)
    evidence_path = rdir / "evidence.json"
    if not evidence_path.exists():
        print(f"error: no evidence pack at {evidence_path}.")
        print("Run the paired readout for this round first (labauto nmr).")
        return EXIT_FAILED
    pack = json.loads(evidence_path.read_text(encoding="utf-8"))
    spec = args.client
    info: dict = {"attempts": [], "note": "", "model": "", "label": ""}
    if spec == QC_ONLY:
        review = qc_only_review(pack)
        kind = QC_ONLY
    else:
        try:
            settings = settings_for(spec)
            clients = make_clients(spec, settings)
        except ConfigError as exc:
            print(f"error: {exc}")
            print("For the fixed rule baseline with no model, use --client qc-only.")
            return EXIT_FAILED
        spec = clients.spec
        client = clients.local
        if isinstance(client, ReplayClient):
            # A campaign transcript also holds decider and note calls: replay the reviewer's only.
            client.records = [r for r in client.records if r.get("role") == ROLE]
            if not client.records:
                print(f"error: the recording holds no {ROLE} replies to replay.")
                return EXIT_FAILED
        decision = route(
            ROLE,
            estimate_tokens(review_messages(pack)),
            settings,
            rdir / "model_calls.jsonl",
            {"round": args.round},
        )
        transcript = Transcript(rdir / "transcript.jsonl")
        review, info = review_round(
            pack, client, decision.label, transcript, scope=next_scope(transcript, args.round)
        )
        kind = reviewer_kind(spec, info)
    record = {
        "round": args.round,
        "campaign_id": pack.get("campaign_id"),
        "synthetic": True,
        "reviewed_by": who_reviewed(spec, info),
        "reviewer_kind": kind,
        "client": spec,
        "model": info.get("model") or None,
        "endpoint_label": info.get("label") or None,
        "evaluator": pack.get("evaluator"),
        "evidence": "evidence.json",
        "created_at": utc_now(),
        "valid": review is not None,
        "review": review.model_dump(mode="json") if review is not None else None,
        "attempts": info.get("attempts", []),
        "note": info.get("note", ""),
    }
    (rdir / "review.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    (rdir / "review.md").write_text(render_review(record), encoding="utf-8")
    print(f"Wrote {rdir / 'review.json'} and review.md. Reviewed by {record['reviewed_by']}.")
    if review is None:
        print(record["note"])
        return EXIT_NEEDS_PERSON
    print(f"Round verdict: {review.round_verdict}.")
    return EXIT_OK


def cmd_eval_reviewer(args: argparse.Namespace) -> int:
    from labauto.cli_plan import ConfigError, make_clients, settings_for
    from labauto.config import load_settings
    from labauto.planner_eval import NoModelAnswered
    from labauto.reviewer_eval import (
        BASELINE_SEEDS,
        DEFAULT_OUTS,
        MAX_BASELINE_SEEDS,
        SWEEP_WORKERS,
        default_page,
        evaluate,
        shown_path,
    )

    if args.trials < 1:
        print("error: --trials must be at least 1.")
        return EXIT_FAILED
    if not 0 <= args.baseline_seeds <= MAX_BASELINE_SEEDS:
        print(f"error: --baseline-seeds must be 0 to {MAX_BASELINE_SEEDS}.")
        return EXIT_FAILED
    out = args.out or DEFAULT_OUTS[args.prompt]
    page = default_page(out)
    if args.client != "live" and page is not None:
        print(f"error: a {args.client.partition(':')[0]} client does not write {page}.")
        print("Pass --out <file> to write its page somewhere else.")
        return EXIT_FAILED
    if args.client == "live":
        settings = load_settings()
        if args.model and settings.main is not None:
            # Evaluate this model on the main endpoint, where the overwatch always runs.
            settings.main = settings.main.model_copy(update={"model": args.model})
            settings.small = None
    elif args.model:
        print("error: --model applies only to --client live.")
        return EXIT_FAILED
    else:
        settings = settings_for(args.client)
    try:
        clients = make_clients(args.client, settings)
    except ConfigError as exc:
        print(f"error: {exc}")
        print(f"{out} was not written.")
        return EXIT_NEEDS_PERSON
    kind, _, source = clients.spec.partition(":")
    command = " ".join(
        ["labauto eval-reviewer", f"--trials {args.trials}"]
        + ([f"--prompt {args.prompt}"] if args.prompt != "default" else [])
        + ([f"--model {args.model}"] if args.model else [])
        + (
            [f"--baseline-seeds {args.baseline_seeds}"]
            if args.baseline_seeds != BASELINE_SEEDS
            else []
        )
        + ([f"--client {kind}:{shown_path(source)}"] if args.client != "live" else [])
        + ([f"--runs {shown_path(args.runs)}"] if args.runs != "runs/eval_reviewer" else [])
        + ([f"--out {shown_path(out)}"] if args.out is not None else [])
    )
    try:
        result = evaluate(
            clients.local,
            settings,
            trials=args.trials,
            runs_dir=args.runs,
            out_path=Path(out),
            command=command,
            prompt=args.prompt,
            baseline_seeds=args.baseline_seeds,
            workers=min(os.cpu_count() or 1, SWEEP_WORKERS),
        )
    except NoModelAnswered as exc:
        print(f"error: {exc}")
        print(f"{out} was not written.")
        return EXIT_NEEDS_PERSON
    print(f"Wrote {result.path}")
    if result.sweep:
        sweep = result.sweep
        plates = sum(len(c["plates"]) for c in sweep["cases"])
        skipped = sum(len(c["skipped"]) for c in sweep["cases"])
        print(
            f"Baseline sweep: {plates} plates on up to {sweep['n']} extra seeds per case "
            f"({skipped} seeds skipped) in {sweep['seconds']:.0f} s, with no model calls."
        )
    if result.outages:
        print(
            f"{result.outages} of {result.trials} overwatch trials ended because the endpoint "
            "stopped answering. They are not scored, and the page says so."
        )
        return EXIT_NEEDS_PERSON
    return EXIT_OK


def add_parsers(sub) -> None:
    """Register this module's commands on the main parser."""
    p = sub.add_parser("review", help="have the overwatch reviewer judge one round's evidence pack")
    p.add_argument("campaign")
    p.add_argument("--round", type=int, required=True)
    p.add_argument(
        "--client",
        default="live",
        help="live, replay:<file>, scripted:<file>, or qc-only (fixed rules, no model)",
    )
    p.set_defaults(func=cmd_review)

    p = sub.add_parser(
        "eval-reviewer", help="score fixed QC and the overwatch reviewer on injected faults"
    )
    p.add_argument("--trials", type=int, default=1, help="overwatch reviews per case")
    p.add_argument(
        "--model",
        default=None,
        help="model name to use on the main endpoint, where the overwatch runs (live only)",
    )
    p.add_argument("--client", default="live", help="live, replay:<file>, or scripted:<file>")
    p.add_argument(
        "--prompt",
        default="default",
        choices=["default", "no_hints"],
        help="the overwatch prompt: default lists fault signatures, no_hints drops them",
    )
    p.add_argument("--runs", default="runs/eval_reviewer")
    p.add_argument(
        "--baseline-seeds",
        type=int,
        default=10,
        help="extra seeds per case on which to run the two fixed baselines, with no model "
        "(default 10; 0 skips it)",
    )
    p.add_argument(
        "--out",
        default=None,
        help="the page to write (default docs/reviewer_eval.md, or "
        "docs/reviewer_eval_no_hints.md with --prompt no_hints; live only; "
        "replay and scripted runs must name another file)",
    )
    p.set_defaults(func=cmd_eval_reviewer)
