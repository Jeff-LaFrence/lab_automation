"""Fault detection evaluation: what fixed rules catch, and what the overwatch adds.

For each case in paired.FAULT_CASES, code builds one round with the case's
faults (simulated LC/MS and NMR), runs two baselines with no model and the
overwatch model on the evidence pack, and scores all three against the fault
log the simulators kept. The reviewers never see that log. The baselines are
qc_only_review (QC rules) and qc_stats_review (QC rules plus two plate
statistics). The overwatch runs with one prompt per run: "default", which
lists fault signatures, or "no_hints", which does not. Two pooled columns
add the overwatch's faults to each baseline's: "QC rules or overwatch" and
"QC plus statistics or overwatch". The second asks whether the model adds
anything on top of the best fixed rules.

Each case runs on one seed. So the run also sweeps the two baselines, with no
model, over extra seeds per case (baseline_sweep): seed k of a case is the case
seed times SEED_STRIDE plus k, never the case seed itself. That shows how much
the baselines move from plate to plate.

Scoring is review.score: faults by kind, and wells for the well faults, with
same-kind entries merged first. Trials that ended because the endpoint
stopped answering are counted on their own and left out of the overwatch
columns. The page is built by code from the case folders, and the numbers
are published whatever they are.
"""

from __future__ import annotations

import json
import multiprocessing
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from labauto.agents.llm import LiveClient, Transcript
from labauto.agents.router import estimate_tokens, route
from labauto.config import Settings
from labauto.planner_eval import NoModelAnswered, git_commit
from labauto.record import portable, utc_stamp
from labauto.review import (
    ATTEMPTS,
    CALLS_PER_ATTEMPT,
    DEFAULT_PROMPT,
    DRIFT_LOW_SHARE,
    DRIFT_MEDIAN_MAX,
    IMPURITY_FLOOR,
    IMPURITY_SPREADS,
    NOT_A_MODEL,
    ROLE,
    STATS_MIN_N,
    check_review,
    fault_text,
    pack_dict,
    prompt_file,
    qc_only_review,
    qc_stats_review,
    review_messages,
    review_round,
    score,
)

DEFAULT_OUT = "docs/reviewer_eval.md"
# The page each prompt writes by default. Only a live client may write these.
DEFAULT_OUTS = {"default": DEFAULT_OUT, "no_hints": "docs/reviewer_eval_no_hints.md"}
# The default pages of eval-reviewer-sweep (reviewer_sweep.py). This command never writes them.
SWEEP_OUTS = {"default": "docs/reviewer_sweep.md", "no_hints": "docs/reviewer_sweep_no_hints.md"}
PROMPT_DIR = "src/labauto/agents/prompts"
# The reviewers on the page, in column order, with the key each row stores their score under.
QC, STATS = "QC rules", "QC plus statistics"
# The two pooled columns: a baseline's faults and the overwatch's, as one review.
QC_OR, STATS_OR = "QC rules or overwatch", "QC plus statistics or overwatch"
# The baseline sweep: extra seeds per case, with no model.
BASELINE_SEEDS = 10  # the eval-reviewer default
MAX_BASELINE_SEEDS = 100
SEED_STRIDE = 1000  # seed k of a case is case seed x SEED_STRIDE + k, for k from 1
SEED_TRIES = 5  # a case tries at most this many times N seeds to get N plates
SWEEP_DIR = "baseline_seeds"
SWEEP_WORKERS = 8  # at most this many processes build sweep plates at once
# paired.build_case raises a ValueError with these words when a seed did not give the case's fault.
NO_FAULT_ON_SEED = "did not happen"
SCRIPTED_BANNER = (
    "**Scripted client: these numbers come from hand-written replies, not from a model.**"
)
UNNAMED_BANNER = (
    "**Unnamed client: the replies carry no model name, so they are treated as scripted. "
    "These numbers may come from hand-written replies, not from a model.**"
)
REASON_CHARS = 200


class EvalResult(NamedTuple):
    """Where the page went, how many overwatch trials ran, and how many ended on an outage.

    sweep is the baseline sweep's summary (see baseline_sweep), or None when it was skipped.
    """

    path: Path
    trials: int
    outages: int
    sweep: dict | None = None


def default_page(path: str | Path, pages: Iterable[str] | None = None) -> str | None:
    """The default page a path resolves to, or None.

    pages are the default pages to check; when not given, this module's own.
    A default page is checked against the working folder and the repository,
    so naming it by another spelling or an absolute path still matches.
    """
    from labauto.config import repo_root

    target = Path(path).resolve()
    for page in DEFAULT_OUTS.values() if pages is None else pages:
        for base in (Path.cwd(), repo_root()):
            if (base / page).resolve() == target:
                return page
    return None


def shown_path(path: str | Path) -> str:
    """A path as the page shows it: relative to the repository, or only its name when outside.

    The page may be committed, so it never carries a home folder or other local path.
    """
    text = portable(path)
    return text if not Path(text).is_absolute() else f"<outside the repository>/{Path(text).name}"


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _faults(items: list) -> list[dict]:
    return [x.model_dump(mode="json") if hasattr(x, "model_dump") else dict(x) for x in items or []]


def _baseline(review, pack: dict, truth: list[dict]) -> dict:
    """A baseline's review, its score, and any problem code's checks find in it."""
    return {
        "review": review.model_dump(mode="json"),
        "score": score(truth, review.suspected_faults),
        "problems": check_review(review, pack),
    }


def run_case(
    case: dict,
    out_dir: Path,
    build: Callable,
    client,
    settings: Settings | None,
    trials: int,
    prompt: str = DEFAULT_PROMPT,
) -> dict:
    """Build one case, review it with both baselines and the model, and score all three.

    A trial that ended on an endpoint outage keeps its record but has no score.
    """
    evidence, true_faults = build(case, out_dir)
    pack = pack_dict(evidence)
    truth = _faults(true_faults)
    rdir = out_dir / "reviewer"
    _write(rdir / "evidence.json", pack)
    _write(rdir / "true_faults.json", truth)
    qc = qc_only_review(pack)
    qc_record = _baseline(qc, pack, truth)
    _write(rdir / "qc_only.json", qc_record)
    stats_review = qc_stats_review(pack)
    stats_record = _baseline(stats_review, pack, truth)
    _write(rdir / "qc_stats.json", stats_record)
    out_trials = []
    for t in range(1, trials + 1):
        decision = route(
            ROLE,
            estimate_tokens(review_messages(pack, prompt)),
            settings,
            rdir / "model_calls.jsonl",
            {"case": case["id"], "trial": t, "prompt": prompt},
        )
        review, info = review_round(
            pack,
            client,
            decision.label,
            Transcript(rdir / "transcript.jsonl"),
            scope=f"trial-{t:02d}",
            prompt=prompt,
        )
        outage = bool(info.get("outage"))
        named = review.suspected_faults if review else []
        trial = {
            "trial": t,
            "review": review.model_dump(mode="json") if review else None,
            "info": info,
            "outage": outage,
            "score": None if outage else score(truth, named),
            "either": None if outage else score(truth, [*qc.suspected_faults, *named]),
            "either_stats": None
            if outage
            else score(truth, [*stats_review.suspected_faults, *named]),
        }
        _write(rdir / f"trial_{t:02d}.json", trial)
        out_trials.append(trial)
    return {
        "case": case,
        "evaluator": pack.get("evaluator", ""),
        "truth": truth,
        "qc": qc_record,
        "qc_stats": stats_record,
        "trials": out_trials,
    }


def sweep_seed(case_seed: int, k: int) -> int:
    """The k-th extra seed of a case, for k from 1 to SEED_STRIDE - 1. Never the case seed."""
    if not 1 <= k < SEED_STRIDE:
        raise ValueError(f"sweep seed index {k} is outside 1 to {SEED_STRIDE - 1}")
    return int(case_seed) * SEED_STRIDE + k


def _drop_spectra(out_dir: Path) -> None:
    """Remove raw spectra (*.npz) from a sweep plate's folder. They are most of its size."""
    for f in sorted(Path(out_dir).rglob("*.npz")):
        f.unlink()


def sweep_plate(case: dict, seed: int, out_dir: Path, build: Callable) -> dict:
    """Build one case on another seed and score both baselines on it. No model is called.

    Writes the evidence pack, the true faults, and both baseline records under
    out_dir/reviewer, as run_case does, then drops the raw spectra. Returns
    the seed, the true faults, and each baseline's score and check problems.
    When the case's fault did not happen on this seed, writes skipped.json and
    returns the seed with "skipped" and the reason.
    """
    out_dir = Path(out_dir)
    try:
        evidence, true_faults = build({**case, "seed": seed}, out_dir)
    except ValueError as exc:
        if NO_FAULT_ON_SEED not in str(exc):
            raise
        _drop_spectra(out_dir)
        _write(out_dir / "skipped.json", {"seed": seed, "reason": str(exc)})
        return {"seed": seed, "skipped": str(exc)}
    pack = pack_dict(evidence)
    truth = _faults(true_faults)
    rdir = out_dir / "reviewer"
    _write(rdir / "evidence.json", pack)
    _write(rdir / "true_faults.json", truth)
    out = {"seed": seed, "truth": truth}
    for key, name, review in (
        ("qc", "qc_only.json", qc_only_review(pack)),
        ("qc_stats", "qc_stats.json", qc_stats_review(pack)),
    ):
        record = _baseline(review, pack, truth)
        _write(rdir / name, record)
        out[key] = {"score": record["score"], "problems": record["problems"]}
    _drop_spectra(out_dir)
    return out


def baseline_sweep(
    cases: list[dict], build: Callable, base: Path, n: int, workers: int = 1
) -> dict:
    """Run both baselines on n extra seeds per case, with no model.

    Seeds come from sweep_seed, k = 1, 2, and so on. A seed on which the
    case's fault did not happen is skipped and the next k is tried, up to
    SEED_TRIES times n seeds per case, so a case can end with fewer than n
    plates; the page shows how many it got. Plates go under base/SWEEP_DIR,
    one folder per case and seed. With workers above 1, plates are built in
    that many processes, so build must be a module-level function; the plates
    and their order do not depend on workers.
    Writes SWEEP_DIR/sweep.json and returns the same summary.
    """
    start = time.monotonic()
    root = Path(base) / SWEEP_DIR
    limit = min(SEED_TRIES * n, SEED_STRIDE - 1)
    plates: dict[str, list[dict]] = {c["id"]: [] for c in cases}
    skipped: dict[str, list[dict]] = {c["id"]: [] for c in cases}
    next_k = {c["id"]: 1 for c in cases}
    pool = None
    if workers > 1 and n > 0 and cases:
        pool = ProcessPoolExecutor(
            min(workers, n * len(cases)), mp_context=multiprocessing.get_context("spawn")
        )
    try:
        while True:
            jobs = []
            for case in cases:
                cid = case["id"]
                for _ in range(n - len(plates[cid])):
                    if next_k[cid] > limit:
                        break
                    seed = sweep_seed(case["seed"], next_k[cid])
                    next_k[cid] += 1
                    jobs.append((cid, (case, seed, root / cid / f"seed_{seed}", build)))
            if not jobs:
                break
            if pool is None:
                results = [sweep_plate(*job) for _, job in jobs]
            else:
                futures = [pool.submit(sweep_plate, *job) for _, job in jobs]
                results = [f.result() for f in futures]
            for (cid, _), result in zip(jobs, results, strict=True):
                (skipped if "skipped" in result else plates)[cid].append(result)
    finally:
        if pool is not None:
            pool.shutdown(cancel_futures=True)
    summary = {
        "synthetic": True,
        "model_calls": 0,
        "note": "Both fixed baselines on extra seeds per case. No model was called. "
        "Raw spectra (*.npz) are not kept.",
        "n": n,
        "seed_rule": f"case seed x {SEED_STRIDE} + k, k from 1",
        "workers": workers,
        "seconds": round(time.monotonic() - start, 1),
        "cases": [
            {"case": c, "plates": plates[c["id"]], "skipped": skipped[c["id"]]} for c in cases
        ],
    }
    _write(root / "sweep.json", summary)
    return summary


def evaluate(
    client,
    settings: Settings | None = None,
    trials: int = 1,
    runs_dir: str | Path = "runs/eval_reviewer",
    out_path: Path | None = None,
    command: str = "labauto eval-reviewer",
    cases: list[dict] | None = None,
    build: Callable | None = None,
    *,
    prompt: str = DEFAULT_PROMPT,
    baseline_seeds: int = 0,
    workers: int = 1,
) -> EvalResult:
    """Run every fault case, review each with both baselines and the model, and write the page.

    The page goes to out_path, or to the prompt's default page. Only a live
    client may write a default page: any other client raises ValueError before
    a case is built. No client may write an eval-reviewer-sweep page. Raises NoModelAnswered, and writes no page, when the model
    never replied. After the model's cases, baseline_sweep runs both baselines
    on baseline_seeds extra seeds per case (0 skips it), in up to workers
    processes. The sweep calls no model.
    """
    prompt_file(prompt)  # an unknown prompt fails here, before any case is built
    if not 0 <= baseline_seeds <= MAX_BASELINE_SEEDS:
        raise ValueError(f"baseline_seeds must be 0 to {MAX_BASELINE_SEEDS}")
    if out_path is None:
        out_path = Path(DEFAULT_OUTS[prompt])
    page = default_page(out_path)
    if page is not None and not isinstance(client, LiveClient):
        raise ValueError(f"only a live client writes {page}; give another out_path")
    other = default_page(out_path, SWEEP_OUTS.values())
    if other is not None:
        raise ValueError(f"{other} is the eval-reviewer-sweep page; give another out_path")
    if cases is None or build is None:
        from labauto import paired

        cases = paired.FAULT_CASES if cases is None else cases
        build = build or paired.build_case
    base = Path(runs_dir) / utc_stamp()
    n = 1
    while base.exists():
        n += 1
        base = Path(runs_dir) / f"{utc_stamp()}_{n}"
    _write(
        base / "run.json",
        {
            "synthetic": True,
            "command": command,
            "prompt": prompt,
            "prompt_file": prompt_file(prompt),
            "trials": trials,
            "cases": [c["id"] for c in cases],
            "baseline_seeds": baseline_seeds,
        },
    )
    rows = []
    for case in cases:
        rows.append(run_case(case, base / case["id"], build, client, settings, trials, prompt))
        if not any(t["info"]["replies"] for r in rows for t in r["trials"]):
            errors = {t["info"]["error"] for r in rows for t in r["trials"]}
            raise NoModelAnswered(
                "the reviewer model gave no reply ("
                + "; ".join(sorted(e for e in errors if e))
                + "), so there are no numbers to publish"
            )
    sweep = baseline_sweep(cases, build, base, baseline_seeds, workers) if baseline_seeds else None
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        render(rows, trials, command, shown_path(base), prompt, sweep), encoding="utf-8"
    )
    outages = sum(1 for r in rows for t in r["trials"] if t["outage"])
    return EvalResult(out_path, trials * len(rows), outages, sweep)


def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")


def _short(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= REASON_CHARS else text[: REASON_CHARS - 3] + "..."


def _of(k: int, n: int) -> str:
    return f"{k} of {n}"


def _ratio(k: int, n: int) -> str:
    """A share with its counts, or n/a when there is nothing to divide by."""
    return f"{k / n:.2f} ({k} of {n})" if n else "n/a (0 of 0)"


def model_names(rows: list[dict]) -> list[str]:
    """Model names reported by trials that got at least one reply. Empty names stay empty."""
    return sorted(
        {t["info"].get("model") or "" for r in rows for t in r["trials"] if t["info"]["replies"]}
    )


def _sum(scores: list[dict], field: str) -> int:
    return sum(len(s[field]) for s in scores)


def _wells(scores: list[dict], field: str) -> int:
    return sum(s["wells"][field] for s in scores)


# Per case counts that the Total row also gives over scored cases only.
_BASE_KEYS = ("true", "true_wells", "qc_caught", "qc_false", "qc_hits", "qc_named")
_BASE_KEYS += ("st_caught", "st_false", "st_hits", "st_named")
_SCORED = " on scored cases"


def _counts(r: dict) -> dict:
    """One case's numbers for both tables. *_s copies count only when a trial was scored."""
    scored = [t for t in r["trials"] if not t["outage"]]
    ow = [t["score"] for t in scored]
    ei = [t["either"] for t in scored]
    es = [t["either_stats"] for t in scored]
    c = {
        "true": len(r["truth"]),
        "true_wells": r["qc"]["score"]["wells"]["true"],
        "ow_caught": _sum(ow, "caught"),
        "ow_false": _sum(ow, "false_alarms"),
        "ei_caught": _sum(ei, "caught"),
        "ei_false": _sum(ei, "false_alarms"),
        "es_caught": _sum(es, "caught"),
        "es_false": _sum(es, "false_alarms"),
        "ow_target": len(r["truth"]) * len(scored),
        "invalid": sum(1 for t in scored if t["review"] is None),
        "scored": len(scored),
        "outage": len(r["trials"]) - len(scored),
        "trials": len(r["trials"]),
        "ow_hits": _wells(ow, "hits"),
        "ow_named": _wells(ow, "hits") + _wells(ow, "extra"),
        "ow_true_wells": _wells(ow, "true"),
        "ei_hits": _wells(ei, "hits"),
        "ei_named": _wells(ei, "hits") + _wells(ei, "extra"),
        "ei_true_wells": _wells(ei, "true"),
        "es_hits": _wells(es, "hits"),
        "es_named": _wells(es, "hits") + _wells(es, "extra"),
        "es_true_wells": _wells(es, "true"),
    }
    for key, record in (("qc", r["qc"]), ("st", r["qc_stats"])):
        sc = record["score"]
        c[f"{key}_caught"] = len(sc["caught"])
        c[f"{key}_false"] = len(sc["false_alarms"])
        c[f"{key}_hits"] = sc["wells"]["hits"]
        c[f"{key}_named"] = sc["wells"]["hits"] + sc["wells"]["extra"]
    for k in _BASE_KEYS:
        c[f"{k}_s"] = c[k] if scored else 0
    return c


def _caught(rows: list[dict], key: str) -> int:
    return sum(len(r[key]["score"]["caught"]) for r in rows)


def prompt_note(prompt: str) -> str:
    """One paragraph that names the overwatch prompt and says what it tells the model."""
    where = f"`{PROMPT_DIR}/{prompt_file(prompt)}`"
    if prompt == "no_hints":
        return (
            f"Overwatch prompt: no_hints ({where}): the default prompt with its fault signatures "
            "section removed. The default prompt lists fault signatures that resemble how the "
            "hard cases were built. This one keeps the rules, the fault kinds, and the reply "
            "format, but describes no signature."
        )
    return (
        f"Overwatch prompt: {prompt} ({where}). This prompt lists fault signatures, and some of "
        "them resemble how the hard cases were built: a drift that leaves the plate flags quiet, "
        "and a small gap between the two NMR product signals. So a gain on the hard cases may come "
        "from those hints. `labauto eval-reviewer --prompt no_hints` runs the same prompt without "
        "that section."
    )


def _sweep_title(n: int) -> str:
    return f"Baselines across {n} extra seed{'s' if n != 1 else ''} per case"


STATS_CONSTANTS = (
    ("STATS_MIN_N", STATS_MIN_N),
    ("DRIFT_LOW_SHARE", DRIFT_LOW_SHARE),
    ("DRIFT_MEDIAN_MAX", DRIFT_MEDIAN_MAX),
    ("IMPURITY_FLOOR", IMPURITY_FLOOR),
    ("IMPURITY_SPREADS", IMPURITY_SPREADS),
)


def how_to_read(rows: list[dict], sweep: dict | None = None) -> list[str]:
    """The "How to read this" section: the columns, the gap the hard cases sit in, the size.

    Three paragraphs: the two baselines, the overwatch and the pooled columns,
    then what the numbers on this page show.
    """
    constants = ", ".join(f"{name} ({value:g})" for name, value in STATS_CONSTANTS)
    if sweep:
        on_seeds = (
            f"The section {_sweep_title(sweep['n'])} shows how both baselines do on seeds the "
            "case table never used."
        )
    else:
        on_seeds = (
            "This run skipped the baseline seed sweep (--baseline-seeds 0), so this page does "
            "not show how the baselines do on other seeds."
        )
    baselines = (
        f"{QC} is fixed code that maps quality flags to verdicts and fault kinds. {STATS} is "
        "the same code plus two plate statistics: a drift rule (LC/MS below NMR on at least "
        f"{DRIFT_LOW_SHARE:.0%} of the conditions with {STATS_MIN_N} or more wells a side, with a "
        f"median difference of {DRIFT_MEDIAN_MAX:g} points or lower) and an impurity rule (a "
        f"well's NMR singlet above its doublet by at least {IMPURITY_FLOOR:g} points and by "
        f"{IMPURITY_SPREADS:g} times the plate's own spread of those gaps). Neither uses a model. "
        f"The rules' constants are fixed in review.py: {constants}. {on_seeds}"
    )
    columns = (
        f"Overwatch alone is the model's own review, with the prompt named above. {QC_OR} pools "
        f"the faults {QC} and the overwatch name. {STATS_OR} pools the faults {STATS} and the "
        "overwatch name, so it shows whether the model adds anything on top of the best fixed "
        f"rules here. When {STATS} catch every true fault, {STATS_OR} cannot catch more, so a "
        "model can only show a gain there in well recall. Pooling can only add false alarms, "
        f"so a model that raises fewer shows it only in Overwatch alone against {STATS}. Both "
        "prompts name the fault kinds, since a reply must use them. So the no_hints prompt has "
        "the fewest hints the reply format allows; it is not free of hints."
    )
    text = ""
    hard = [r for r in rows if r["case"].get("hard")]
    if hard:
        n = sum(len(r["truth"]) for r in hard)
        qc, st = _caught(hard, "qc"), _caught(hard, "qc_stats")
        if qc == n:
            verdict = "so here the plain rules leave no gap"
        elif st == n:
            verdict = "so simple statistics close that gap here"
        elif st > qc:
            verdict = "so simple statistics close part of that gap here"
        else:
            verdict = "so here the two statistics do not close it"
        text += (
            "The hard cases were built to sit in a gap of the plain QC rules: on their case "
            f"seeds no flag fires. On the hard cases, {QC} catch {_of(qc, n)} true faults and {STATS} catch "
            f"{_of(st, n)}, {verdict}. So an overwatch that beats {QC} has not yet shown it "
            f"beats fixed rules; compare it with {STATS} too. The constants were set with the "
            "hard-case recipes known, so read that column as what simple rules do once someone "
            "knows what to look for. "
        )
    n_true = sum(len(r["truth"]) for r in rows)
    qc_all, st_all = _caught(rows, "qc"), _caught(rows, "qc_stats")
    qc_false = sum(len(r["qc"]["score"]["false_alarms"]) for r in rows)
    st_false = sum(len(r["qc_stats"]["score"]["false_alarms"]) for r in rows)
    text += (
        f"Over all cases, {STATS} catch {_of(st_all, n_true)} true faults and raise {st_false} "
        f"false alarm{'s' if st_false != 1 else ''}; {QC} catch {_of(qc_all, n_true)} and raise "
        f"{qc_false}."
    )
    if n_true and st_all == n_true:
        text += (
            f" So on these case plates {STATS} leave no fault for a model to add; a gain there "
            "can only be in well recall or fewer false alarms."
        )
    if sweep:
        plates = [p for e in sweep["cases"] for p in e["plates"]]
        n_s = sum(len(p["truth"]) for p in plates)
        st_s = sum(len(p["qc_stats"]["score"]["caught"]) for p in plates)
        qc_s = sum(len(p["qc"]["score"]["caught"]) for p in plates)
        text += (
            f" On the {len(plates)} sweep plate{'s' if len(plates) != 1 else ''}, {STATS} catch "
            f"{_of(st_s, n_s)} true faults and {QC} catch {_of(qc_s, n_s)}."
        )
        hard_entries = [e for e in sweep["cases"] if e["case"].get("hard")]
        if hard_entries:
            h: dict[str, int] = defaultdict(int)
            for e in hard_entries:
                for k, v in _sweep_counts(e).items():
                    h[k] += v
            text += (
                f" On the {h['plates']} hard sweep plates alone, {STATS} catch "
                f"{_of(h['qc_stats_caught'], h['true'])} true faults and find "
                f"{_of(h['qc_stats_hits'], h['true_wells'])} of the faulty wells, so on seeds "
                "other than the case seeds the fixed rules do leave room for a model."
            )
    n_cases = len(rows)
    text += (
        f" {n_cases} case{'s' if n_cases != 1 else ''} with {n_true} true "
        f"fault{'s' if n_true != 1 else ''} is a small set: one case more or less moves every "
        "share by a lot."
    )
    return ["## How to read this", "", baselines, "", columns, "", text, ""]


def render(
    rows: list[dict],
    trials: int,
    command: str,
    runs: str,
    prompt: str = DEFAULT_PROMPT,
    sweep: dict | None = None,
) -> str:
    """The evaluation page as Markdown. sweep is baseline_sweep's summary, or None."""
    models = model_names(rows)
    evaluators = sorted({r["evaluator"] for r in rows if r["evaluator"]})
    total_trials = sum(len(r["trials"]) for r in rows)
    outages = sum(1 for r in rows for t in r["trials"] if t["outage"])
    lines = ["# Reviewer evaluation", ""]
    if "scripted" in models:
        lines += [SCRIPTED_BANNER, ""]
    elif any(m in NOT_A_MODEL for m in models):
        lines += [UNNAMED_BANNER, ""]
    if outages:
        lines += [
            f"**{_of(outages, total_trials)} overwatch trials ended because the endpoint stopped "
            "answering. They are counted in the last column and left out of the overwatch "
            "columns and the two pooled columns. The Total rows also give each baseline over the "
            "scored cases only "
            "(cases with at least one scored trial). The command exits with code 1.**",
            "",
        ]
    lines += [
        f"Generated by `{command}`. Every number below comes from the case folders under `{runs}`.",
        "",
        "All data here are synthetic. A made up yield surface is read by a simulated LC/MS and a "
        "simulated 1H NMR, and the faults are injected by those simulators. The true fault log "
        "comes from the simulators' own records. No reviewer sees it.",
        "",
        prompt_note(prompt),
        "",
        *how_to_read(rows, sweep),
        "Scoring. Entries of the same kind are merged first, so grouping cannot change a score. "
        "A true fault is caught when a review names its kind and, for injection_failure or "
        "nmr_impurity, at least one of its wells. For those two kinds, each named well the fault "
        "log does not list under that kind is one false alarm. A plate-wide kind or other that "
        "matches no true fault is one false alarm, however often it is named. "
        "lcms_nmr_disagreement is not a false alarm when the plate holds a true fault of any kind.",
        "",
        f"The overwatch ran {trials} time{'s' if trials != 1 else ''} per case; its columns add "
        f"up over trials. No valid review means no reply passed code's checks in {ATTEMPTS} "
        f"attempts (up to {ATTEMPTS * CALLS_PER_ATTEMPT} replies, since each attempt may retry "
        "once on a reply that is not readable JSON). Such a trial counts as naming no fault, and "
        "a person would take over. A trial that ended on an endpoint outage is not scored.",
        "",
    ]
    if any(r["case"].get("hard") for r in rows):
        lines += [
            "Cases marked (hard) are made to slip past the fixed QC flags on their case seeds.",
            "",
        ]
    lines += [
        f"| Case | Faults injected | True faults | {QC}: caught | {QC}: false alarms "
        f"| {STATS}: caught | {STATS}: false alarms "
        "| Overwatch alone: caught | Overwatch alone: false alarms "
        f"| {QC_OR}: caught | {QC_OR}: false alarms "
        f"| {STATS_OR}: caught | {STATS_OR}: false alarms "
        "| No valid review | Ended on an outage |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    tot: dict[str, int] = defaultdict(int)
    well_lines = []
    for r in rows:
        case = r["case"]
        injected = [*case.get("lcms_faults", []), *case.get("nmr_faults", [])]
        counts = _counts(r)
        for k, v in counts.items():
            tot[k] += v
        name = case["id"] + (" (hard)" if case.get("hard") else "")
        lines.append(_fault_row(name, _cell(", ".join(injected) or "none"), counts))
        well_lines.append(_well_row(name, counts))
    lines += [
        _fault_row("**Total**", "", tot, scored_too=bool(outages)),
        "",
        "Well level, for injection_failure and nmr_impurity. Well precision: of the wells a "
        "review named under one of those kinds, the share the fault log lists under that kind. "
        "Well recall: of the wells the fault log lists, the share a review named under the right "
        "kind. The overwatch columns and the two pooled columns add up over scored trials.",
        "",
        f"| Case | True wells | {QC}: well precision | {QC}: well recall "
        f"| {STATS}: well precision | {STATS}: well recall "
        "| Overwatch alone: well precision | Overwatch alone: well recall "
        f"| {QC_OR}: well precision | {QC_OR}: well recall "
        f"| {STATS_OR}: well precision | {STATS_OR}: well recall |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
        *well_lines,
        _well_row("**Total**", tot, scored_too=bool(outages)),
        "",
        "Overwatch model, as reported by the endpoint: "
        + (", ".join(m or "unknown (no name reported)" for m in models) or "none recorded")
        + "  ",
        f"Overwatch prompt: {prompt} ({prompt_file(prompt)})  ",
        f"NMR evaluator: {', '.join(evaluators) or 'not named in the evidence'}  ",
        f"Date: {datetime.now(timezone.utc).date().isoformat()}  ",
        f"Git commit: {git_commit()}  ",
        f"Command: `{command}`",
        "",
        *(sweep_section(sweep, runs) if sweep else []),
        "## Cases",
        "",
    ]
    for r in rows:
        case = r["case"]
        lines += [f"### {case['id']}", ""]
        lines.append(
            f"- Injected: LC/MS {', '.join(case.get('lcms_faults', [])) or 'none'}; "
            f"NMR {', '.join(case.get('nmr_faults', [])) or 'none'}; seed {case.get('seed', 'n/a')}"
        )
        if case.get("hard"):
            lines.append(
                "- Hard case, made to slip past the fixed flags"
                + (f": {_cell(case['note'])}" if case.get("note") else ".")
            )
        lines.append(
            "- True faults (simulator log): "
            + ("; ".join(fault_text(f) for f in r["truth"]) or "none")
        )
        for who, key in ((QC, "qc"), (STATS, "qc_stats")):
            b = r[key]
            lines.append(
                f"- {who}: round verdict {b['review']['round_verdict']}. " + _named(b["score"])
            )
        for t in r["trials"]:
            head = f"- Overwatch alone, trial {t['trial']}: "
            if t["outage"]:
                lines.append(
                    head + "ended on an endpoint outage, not scored. " + _cell(t["info"]["note"])
                )
                continue
            if t["review"] is None:
                lines.append(head + _cell(t["info"]["note"] or "no valid review"))
                continue
            lines.append(
                head + f"round verdict {t['review']['round_verdict']}. " + _named(t["score"])
            )
            if t["review"]["summary"].strip():
                lines.append(
                    "  Summary, written by the reviewer (not by code) and checked by code: "
                    f'"{_short(t["review"]["summary"])}"'
                )
        lines.append("")
    lines += ["## Failure notes", ""]
    lines += failure_notes(rows)
    return "\n".join(lines) + "\n"


def _sweep_counts(entry: dict) -> dict:
    """One case's sweep numbers: plates, skipped seeds, true faults and wells, and per baseline
    the faults caught, the well hits, and the plates with at least one false alarm."""
    plates = entry["plates"]
    c = {
        "plates": len(plates),
        "skipped": len(entry["skipped"]),
        "true": sum(len(p["truth"]) for p in plates),
        "true_wells": sum(p["qc"]["score"]["wells"]["true"] for p in plates),
    }
    for key in ("qc", "qc_stats"):
        scores = [p[key]["score"] for p in plates]
        c[f"{key}_caught"] = _sum(scores, "caught")
        c[f"{key}_hits"] = _wells(scores, "hits")
        c[f"{key}_alarm_plates"] = sum(1 for sc in scores if sc["false_alarms"])
    return c


def _sweep_row(name: str, c: dict) -> str:
    cells = [name, c["plates"], c["skipped"]]
    for key in ("qc", "qc_stats"):
        cells += [
            _of(c[f"{key}_caught"], c["true"]),
            _ratio(c[f"{key}_hits"], c["true_wells"]),
            _of(c[f"{key}_alarm_plates"], c["plates"]),
        ]
    return _row(cells)


def _alarm_kinds(plates: list[dict], key: str, kinds: list[str]) -> str:
    """For each kind, the plates on which a baseline raised a false alarm of it.

    Counted apart on plates with no true fault of that kind and, for a well
    kind, on plates with one (extra wells), when there are any.
    """
    parts = []
    for kind in kinds:
        has = [kind in {t["kind"] for t in p["truth"]} for p in plates]
        hit = [any(f["kind"] == kind for f in p[key]["score"]["false_alarms"]) for p in plates]
        without = sum(1 for h, x in zip(has, hit, strict=True) if x and not h)
        within = sum(1 for h, x in zip(has, hit, strict=True) if x and h)
        text = f"{kind} on {_of(without, has.count(False))} plates with no true {kind} fault"
        if within:
            text += f", and extra wells on {_of(within, has.count(True))} plates with one"
        parts.append(text)
    return "; ".join(parts)


def sweep_section(sweep: dict, runs: str) -> list[str]:
    """The section on the baseline sweep: both baselines on extra seeds per case, no model."""
    n = sweep["n"]
    lines = [
        f"## {_sweep_title(n)}",
        "",
        "No model runs in this section, so it costs no model calls. Each case above ran on one "
        f"seed. Here code builds each case again on {n} other seed{'s' if n != 1 else ''} (seed "
        f"k is the case seed x {SEED_STRIDE} + k, never the case seed itself) and runs only the "
        "two fixed baselines. A seed on which the case's fault did not happen is skipped and the "
        f"next one is used, up to {SEED_TRIES} x {n} seeds per case. Faults caught: true faults "
        "caught, of all true faults on the case's plates. Well recall: as in the well table. "
        "Plates with a false alarm: plates on which the baseline raised at least one false "
        f"alarm, of the plates built. Every number comes from the folders under `{runs}/"
        f"{SWEEP_DIR}`, which list the seeds used in sweep.json. Raw spectra are not kept there.",
        "",
        f"| Case | Plates | Seeds skipped | {QC}: faults caught | {QC}: well recall "
        f"| {QC}: plates with a false alarm | {STATS}: faults caught | {STATS}: well recall "
        f"| {STATS}: plates with a false alarm |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    tot: dict[str, int] = defaultdict(int)
    for entry in sweep["cases"]:
        case = entry["case"]
        c = _sweep_counts(entry)
        for k, v in c.items():
            tot[k] += v
        lines.append(_sweep_row(case["id"] + (" (hard)" if case.get("hard") else ""), c))
    lines += [_sweep_row("**Total**", tot), ""]
    plates = [p for e in sweep["cases"] for p in e["plates"]]
    seen = {
        f["kind"] for p in plates for k in ("qc", "qc_stats") for f in p[k]["score"]["false_alarms"]
    }
    kinds = sorted(seen | {"nmr_impurity"})
    lines += [
        "False alarms by kind, counted in plates. "
        f"{QC}: {_alarm_kinds(plates, 'qc', kinds)}. "
        f"{STATS}: {_alarm_kinds(plates, 'qc_stats', kinds)}.",
        "",
    ]
    broken = [
        (e["case"]["id"], p["seed"], who, p[key]["problems"])
        for e in sweep["cases"]
        for p in e["plates"]
        for who, key in ((QC, "qc"), (STATS, "qc_stats"))
        if p[key]["problems"]
    ]
    if broken:
        lines += [
            f"A baseline review failed code's own checks on {len(broken)} sweep "
            f"plate{'s' if len(broken) != 1 else ''}. That is a bug in the baseline:",
            "",
            *(
                f"- {who}, {cid}, seed {seed}: {_short(' '.join(msgs))}"
                for cid, seed, who, msgs in broken
            ),
            "",
        ]
    return lines


def _row(cells: list) -> str:
    """One table row. An empty cell stays a single space wide."""
    return "| " + " | ".join(str(x) for x in cells).replace("|  |", "| |") + " |"


def _also(text: str, scored: str, scored_too: bool) -> str:
    """A Total cell, followed by the same number over scored cases when trials were cut."""
    return f"{text}; {scored}{_SCORED}" if scored_too else text


def _fault_row(name: str, injected: str, c: dict, scored_too: bool = False) -> str:
    cells = [name, injected, _also(str(c["true"]), str(c["true_s"]), scored_too)]
    for key in ("qc", "st"):
        cells += [
            _also(
                _of(c[f"{key}_caught"], c["true"]),
                _of(c[f"{key}_caught_s"], c["true_s"]),
                scored_too,
            ),
            _also(str(c[f"{key}_false"]), str(c[f"{key}_false_s"]), scored_too),
        ]
    cells += [
        _of(c["ow_caught"], c["ow_target"]),
        c["ow_false"],
        _of(c["ei_caught"], c["ow_target"]),
        c["ei_false"],
        _of(c["es_caught"], c["ow_target"]),
        c["es_false"],
        _of(c["invalid"], c["scored"]),
        _of(c["outage"], c["trials"]),
    ]
    return _row(cells)


def _well_row(name: str, c: dict, scored_too: bool = False) -> str:
    cells = [name, _also(str(c["true_wells"]), str(c["true_wells_s"]), scored_too)]
    for key in ("qc", "st"):
        hits, named, hits_s, named_s = (
            c[f"{key}_hits"],
            c[f"{key}_named"],
            c[f"{key}_hits_s"],
            c[f"{key}_named_s"],
        )
        cells += [
            _also(_ratio(hits, named), _ratio(hits_s, named_s), scored_too),
            _also(_ratio(hits, c["true_wells"]), _ratio(hits_s, c["true_wells_s"]), scored_too),
        ]
    cells += [
        _ratio(c["ow_hits"], c["ow_named"]),
        _ratio(c["ow_hits"], c["ow_true_wells"]),
        _ratio(c["ei_hits"], c["ei_named"]),
        _ratio(c["ei_hits"], c["ei_true_wells"]),
        _ratio(c["es_hits"], c["es_named"]),
        _ratio(c["es_hits"], c["es_true_wells"]),
    ]
    return _row(cells)


def _named(sc: dict) -> str:
    """Each named fault kind, merged, with how it scored."""
    if not sc["named"]:
        return "Named no fault."
    parts = []
    for e in sc["named"]:
        text = f"{fault_text(e)}: {e['outcome']}"
        bits = []
        if e["extra"]:
            bits.append(f"{len(e['extra'])} extra well{'s' if len(e['extra']) != 1 else ''}")
        if e["missed"]:
            bits.append(
                f"{len(e['missed'])} true well{'s' if len(e['missed']) != 1 else ''} missed"
            )
        if bits:
            text += f" ({', '.join(bits)})"
        parts.append(text)
    return "Named: " + "; ".join(parts) + "."


def _counted(cases: list[str]) -> str:
    """Case ids in first-seen order, each with its count when above one."""
    return ", ".join(f"{c} ({n})" if n > 1 else c for c, n in Counter(cases).items())


def failure_notes(rows: list[dict]) -> list[str]:
    """What went wrong, grouped by kind, each with one real example from the replies."""
    problems: dict[str, list[tuple[str, int, int, str]]] = defaultdict(list)
    whos = (QC, STATS, "Overwatch alone")
    missed: dict[str, dict[str, list[str]]] = {who: defaultdict(list) for who in whos}
    alarms: dict[str, dict[str, list[str]]] = {who: defaultdict(list) for who in whos}
    broken = []
    no_review = []
    outages = []
    for r in rows:
        cid = r["case"]["id"]
        for who, key in ((QC, "qc"), (STATS, "qc_stats")):
            for f in r[key]["score"]["missed"]:
                missed[who][f["kind"]].append(cid)
            for f in r[key]["score"]["false_alarms"]:
                alarms[who][f["kind"]].append(cid)
            if r[key].get("problems"):
                broken.append((who, cid, r[key]["problems"]))
        for t in r["trials"]:
            for a in t["info"]["attempts"]:
                for p in a.get("problems") or []:
                    problems[p["kind"]].append((cid, t["trial"], a["attempt"], p["message"]))
            if t["outage"]:
                outages.append((cid, t["trial"], t["info"]["error"] or ""))
                continue
            if t["review"] is None:
                no_review.append((cid, t["trial"], t["info"]["note"]))
                continue
            for f in t["score"]["missed"]:
                missed["Overwatch alone"][f["kind"]].append(cid)
            for f in t["score"]["false_alarms"]:
                alarms["Overwatch alone"][f["kind"]].append(cid)
    out = []
    if broken:
        out += ["A baseline review failed code's own checks. That is a bug in the baseline:", ""]
        for who, cid, msgs in broken:
            out.append(f"- {who}, {cid}: {_short(' '.join(msgs))}")
        out.append("")
    if problems:
        out += [
            "Replies that code rejected, by problem. Each example is quoted from a real reply's check.",
            "",
        ]
        for kind, items in sorted(problems.items(), key=lambda kv: -len(kv[1])):
            cid, trial, attempt, msg = items[0]
            cases = ", ".join(dict.fromkeys(i[0] for i in items))
            out.append(
                f"- {kind}: {len(items)} time{'s' if len(items) != 1 else ''}, in {cases}. "
                f"Example from {cid}, trial {trial}, reply {attempt}: {_short(msg)}"
            )
        out.append("")
    if no_review:
        out.append(
            f"No valid review in {len(no_review)} trial{'s' if len(no_review) != 1 else ''}:"
        )
        out.append("")
        for cid, trial, note in no_review:
            out.append(f"- {cid}, trial {trial}: {_short(note)}")
        out.append("")
    if outages:
        out.append(
            f"Ended on an endpoint outage in {len(outages)} trial{'s' if len(outages) != 1 else ''}:"
        )
        out.append("")
        for cid, trial, error in outages:
            out.append(f"- {cid}, trial {trial}: {_short(error)}")
        out.append("")
    for who in whos:
        for title, table in (("Missed", missed[who]), ("False alarms", alarms[who])):
            if table:
                listed = "; ".join(
                    f"{kind} in {_counted(cases)}" for kind, cases in sorted(table.items())
                )
                out.append(f"- {who}, {title.lower()}: {listed}.")
    if not out:
        out.append("No reply was rejected, no fault was missed, and no false alarm was raised.")
    return out
