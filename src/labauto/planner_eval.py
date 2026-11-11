"""Planner evaluation: run each request of a test set several times and publish what happened.

A test set (see testsets.py) names the requests and says, for each one,
whether a deviation or an assumption is needed and what a faithful final
design looks like. The table and the failure notes are built by code from the
run folders. Each failure note quotes a real error from a real attempt, or a
real reason a final design was not faithful. Numbers are published whatever
they are.
"""

from __future__ import annotations

import shlex
import statistics
import subprocess
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from labauto.config import Settings, load_assay, repo_root
from labauto.models import Assay
from labauto.orchestrator import Clients, load_attempts, load_check, load_state, start_run
from labauto.testsets import (
    CORE_SET,
    FaithfulRules,
    LoadedSet,
    display_path,
    faithful_failures,
    load_set,
    set_label,
)


class NoModelAnswered(RuntimeError):
    """No trial got a single reply from a model, so there is nothing to publish."""


SPEC_COLUMNS = [
    "Trials",
    "Valid first try",
    "Valid after local repairs",
    "Went to hosted",
    "Handed to person",
    "Deviations declared when needed",
    "Median seconds",
]
EXTRA_COLUMNS = ["Assumptions listed when needed", "Faithful", "Failed (internal error)"]


def default_out_path(set_name: str) -> Path:
    """Where the page goes: docs/planner_eval.md for the core set, as SPEC.md says, else one page per set."""
    if set_name == CORE_SET:
        return Path("docs/planner_eval.md")
    return Path(f"docs/planner_eval_{set_name}.md")


def eval_command(
    set_arg: str, trials: int, model: str | None = None, out: str | None = None
) -> str:
    """The command that makes the page, as printed at its top.

    --runs is left out: it only says where the run folders go.
    """
    parts = ["labauto", "eval-planner", "--set", set_arg, "--trials", str(trials)]
    if model:
        parts += ["--model", model]
    if out:
        parts += ["--out", out]
    return " ".join(shlex.quote(p) for p in parts)


def git_commit() -> str:
    """The current commit, short form, with "-dirty" when files had changed, or 'unknown'.

    Manifests and code may be edited between commits, so a page made from a changed tree says so.
    """
    try:
        out = subprocess.run(
            ["git", "describe", "--always", "--dirty", "--abbrev=7"],
            capture_output=True,
            text=True,
            cwd=repo_root(),
            timeout=10,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def trial_facts(
    run_dir: Path, rules: FaithfulRules | None = None, assay: Assay | None = None
) -> dict:
    """What one trial did, read back from its run folder.

    "faithful" is None when the trial has no final design. Otherwise it is
    the list of reasons the final design breaks the rules; empty means
    faithful. The assay is read from the run's state when not given.
    """
    st = load_state(run_dir)
    attempts = load_attempts(run_dir)
    final = load_check(run_dir / "final") if (run_dir / "final").exists() else None
    design = final.design if final and final.design else None
    faithful = None
    if design is not None:
        faithful = faithful_failures(
            design, assay or load_assay(st.assay), rules or FaithfulRules()
        )
    return {
        "state": st.state,
        "first_ok": bool(attempts and attempts[0].ok),
        "local_ok": any(a.ok and a.rung == "local" for a in attempts),
        "hosted": any(a.rung == "hosted" for a in attempts),
        "person": st.state == "NEEDS_HUMAN",
        "failed": st.state == "FAILED",
        "failed_reason": st.history[-1].reason if st.state == "FAILED" and st.history else "",
        "deviations": len(design.deviations) if design else None,
        "assumptions": len(design.assumptions) if design else None,
        "faithful": faithful,
        "seconds": sum(a.seconds for a in attempts),
        "models": {a.model for a in attempts if a.model},
        "failures": [(a.number, e) for a in attempts for e in a.errors],
    }


def evaluate(
    clients_factory: Callable[[], Clients],
    settings: Settings,
    trials: int = 5,
    runs_dir: str | Path = "runs/eval",
    out_path: str | Path | None = None,
    testset: str | Path | LoadedSet = CORE_SET,
    ids: list[str] | None = None,
    command: str | None = None,
) -> Path:
    """Run every request of a test set `trials` times and write the evaluation page.

    Each request runs with its own assay and workcell. Run folders go to
    <runs_dir>/<set>/<request id>/. `ids` limits the run to some requests.
    """
    tset = testset if isinstance(testset, LoadedSet) else load_set(testset)
    requests = tset.requests if ids is None else [tset.get(i) for i in ids]
    facts: dict[str, list[dict]] = {}
    for req in requests:
        assay = load_assay(req.assay)
        rows = facts.setdefault(req.id, [])
        for _ in range(trials):
            run_dir = start_run(
                req.text,
                req.assay,
                req.workcell,
                clients_factory(),
                settings,
                Path(runs_dir) / tset.name / req.id,
            )
            rows.append(trial_facts(run_dir, req.expect.faithful, assay))
    if not any(r["models"] for rows in facts.values() for r in rows):
        failed = sum(r["failed"] for rows in facts.values() for r in rows)
        raise NoModelAnswered(
            f"no model replied in any trial ({failed} trials ended FAILED), so there are no numbers to publish"
        )
    out = Path(out_path) if out_path else default_out_path(tset.name)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        render(facts, trials, tset, command or eval_command(set_label(tset), trials)),
        encoding="utf-8",
    )
    return out


def when_needed(rows: list[dict], key: str, needed: bool) -> str:
    """'k of n passing' for a list that must not be empty on passing trials, or n/a when not needed."""
    if not needed:
        return "n/a"
    passed = [r for r in rows if r[key] is not None]
    return f"{sum(1 for r in passed if r[key] > 0)} of {len(passed)} passing"


def render(
    facts: dict[str, list[dict]], trials: int, tset: LoadedSet, command: str | None = None
) -> str:
    """The evaluation page as Markdown.

    The core set keeps the SPEC.md 8.11 columns in SPEC order, with the extra
    columns at the end. Other sets use the same columns with "Request" in
    place of "Scenario".
    """
    command = command or eval_command(set_label(tset), trials)
    first = "Scenario" if tset.name == CORE_SET else "Request"
    columns = [first, *SPEC_COLUMNS, *EXTRA_COLUMNS]
    title = (
        "# Planner evaluation" if tset.name == CORE_SET else f"# Planner evaluation: {tset.name}"
    )
    lines = [
        title,
        "",
        f"Test set: {tset.name}. {tset.description}  ",
        f"Manifest: `{display_path(tset.manifest_path)}`  ",
        f"Generated by `{command}`. Every number below comes from the run folders.",
        "",
        "| " + " | ".join(columns) + " |",
        "|" + "---|" * len(columns),
    ]
    expect = {r.id: r.expect for r in tset.requests}
    models: set[str] = set()
    failures: dict[str, list[tuple[str, int, object]]] = defaultdict(list)
    unfaithful: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for rid, rows in facts.items():
        n = len(rows)
        dev = when_needed(rows, "deviations", expect[rid].deviation_required)
        assumed = when_needed(rows, "assumptions", expect[rid].assumption_required)
        judged = [r for r in rows if r["faithful"] is not None]
        if expect[rid].faithful.model_dump(exclude_none=True, exclude_defaults=True):
            faithful = f"{sum(1 for r in judged if not r['faithful'])} of {len(judged)} passing"
        else:
            faithful = "n/a"  # no rules to judge by
        median = statistics.median(r["seconds"] for r in rows) if rows else 0
        lines.append(
            f"| {rid} | {n} | {sum(r['first_ok'] for r in rows)} | {sum(r['local_ok'] for r in rows)} "
            f"| {sum(r['hosted'] for r in rows)} | {sum(r['person'] for r in rows)} | {dev} | {median:.1f} "
            f"| {assumed} | {faithful} | {sum(r['failed'] for r in rows)} |"
        )
        for r in rows:
            models |= r["models"]
            if r["failed"]:
                failures["FAILED (internal error)"].append((rid, 0, r["failed_reason"]))
            for number, e in r["failures"]:
                failures[e.code].append((rid, number, e))
            for reason in r["faithful"] or []:
                rule, _, text = reason.partition(": ")
                unfaithful[rule].append((rid, text))
    if "scripted" in models:
        lines[1:1] = [
            "",
            "**Scripted client: these numbers come from hand-written replies, not from a model.**",
        ]
    lines += [
        "",
        "Deviations and assumptions are counted on trials that passed every check, and only for "
        "requests whose manifest entry says one is needed. n/a means none is needed. Faithful counts "
        "the passing trials whose final design also meets the manifest's rules for what was asked.",
        "",
        f"Model, as reported by the endpoint: {', '.join(sorted(models)) or 'none recorded'}  ",
        f"Date: {datetime.now(timezone.utc).date().isoformat()}  ",
        "Git commit: " + git_commit(),
        "",
        "## Failure notes",
        "",
    ]
    if not failures:
        lines.append("No attempt failed a check, and no trial failed with an internal error.")
    for code, items in sorted(failures.items(), key=lambda kv: -len(kv[1])):
        rid, number, e = items[0]
        where = Counter(s for s, _, _ in items)
        spread = ", ".join(f"{s} ({c})" for s, c in where.most_common())
        if isinstance(e, str):
            lines.append(f"- {code}: {len(items)} trials, in {spread}. Example from {rid}: {e}")
        else:
            lines.append(
                f"- {code}: {len(items)} times, in {spread}. Example from {rid}, attempt {number}: {e.message}"
            )
    if unfaithful:
        lines += ["", "Final designs that passed every check but were not faithful, by rule:", ""]
    elif any(r["faithful"] is not None for rows in facts.values() for r in rows):
        lines += ["", "Every final design met the faithful rules in the manifest."]
    for rule, items in sorted(unfaithful.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        rid, text = items[0]
        where = Counter(s for s, _ in items)
        spread = ", ".join(f"{s} ({c})" for s, c in where.most_common())
        lines.append(
            f"- Not faithful, rule {rule}: {len(items)} trials, in {spread}. Example from {rid}: {text}"
        )
    return "\n".join(lines) + "\n"
