"""The overwatch on the baseline sweep's plates, next to the fixed rules on the same plates.

labauto eval-reviewer runs the overwatch once per fault case, so its case
table has 12 plates. Its baseline sweep builds each case again on extra seeds
(120 plates by default) but runs only the two fixed baselines there, with no
model. This module runs the overwatch on those sweep plates too, one trial per
plate, so the five reviewers of the reviewer evaluation (QC rules, QC plus
statistics, Overwatch alone, and the two pooled columns) can be compared on
the same plates.

It never builds a plate. The source is an eval-reviewer run folder that holds
baseline_seeds/sweep.json. Each plate's evidence pack, true fault log, and
baseline reviews are read from its folder there, and nothing is written
there. A trial runs as reviewer_eval.run_case runs one: route, review_round
with the chosen prompt, then review.score for Overwatch alone and for each
baseline pooled with the overwatch. The baselines are scored again from their
stored reviews, so every column uses the same scoring code.

Output: runs/eval_reviewer_sweep/<UTC stamp>/run.json, and per plate
<case>/seed_<seed>/ with trial_01.json, transcript.jsonl, and
model_calls.jsonl. A run can be resumed. A plate whose trial ended normally is
not asked again; a plate whose trial ended on an outage is. After STOP_AFTER
outages in a row the endpoint is taken to be down: the run stops and still
writes its page from the plates done. The page is built by code from the
output folder alone, so a resumed run rewrites it from every plate done.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from labauto.agents.llm import LiveClient, Transcript
from labauto.agents.router import estimate_tokens, route
from labauto.config import Settings, repo_root
from labauto.planner_eval import git_commit
from labauto.record import utc_now, utc_stamp
from labauto.review import (
    ATTEMPTS,
    CALLS_PER_ATTEMPT,
    DEFAULT_PROMPT,
    NOT_A_MODEL,
    ROLE,
    DataReview,
    check_review,
    prompt_file,
    review_messages,
    review_round,
    score,
)
from labauto.reviewer_eval import (
    DEFAULT_OUTS,
    PROMPT_DIR,
    QC,
    QC_OR,
    SCRIPTED_BANNER,
    STATS,
    STATS_CONSTANTS,
    STATS_OR,
    SWEEP_DIR,
    UNNAMED_BANNER,
    _counted,
    _of,
    _ratio,
    _row,
    _short,
    _write,
    default_page,
    shown_path,
)

COMMAND = "labauto eval-reviewer-sweep"
# The page each prompt writes by default. Only a live client may write these.
SWEEP_OUTS = {"default": "docs/reviewer_sweep.md", "no_hints": "docs/reviewer_sweep_no_hints.md"}
RUNS_DIR = "runs/eval_reviewer_sweep"
TRIAL_FILE = "trial_01.json"
STOP_AFTER = 3  # outages in a row: the endpoint is taken to be down, and the run stops
LIST_MAX = 10  # plates listed by name in the failure notes, per list
OW = "Overwatch alone"
# The five reviewers in column order, with the key a trial file stores each score under.
REVIEWERS = (
    (QC, "qc"),
    (STATS, "qc_stats"),
    (OW, "score"),
    (QC_OR, "either"),
    (STATS_OR, "either_stats"),
)
BASELINES = {"qc": "qc_only.json", "qc_stats": "qc_stats.json"}
PLATE_FILES = ("evidence.json", "true_faults.json", *BASELINES.values())
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


class SweepResult(NamedTuple):
    """What a sweep review did.

    path is the page, or None when no plate has been reviewed yet. plates
    counts the plates in the run's cases, reviewed those with a scored trial,
    outages those whose last trial ended on an outage, and asked the plates
    sent to the model in this session. stopped is True when the run stopped
    after STOP_AFTER outages in a row.
    """

    path: Path | None
    run_dir: Path
    plates: int
    reviewed: int
    outages: int
    stopped: bool
    asked: int


def sweep_page(path: str | Path) -> str | None:
    """The sweep's default page a path resolves to, by any spelling, or None."""
    return default_page(path, SWEEP_OUTS.values())


def check_page(path: str | Path, client) -> None:
    """Refuse a page this run may not write, before anything is written.

    Only a live client writes a sweep default page. No client writes an
    eval-reviewer page from here.
    """
    page = sweep_page(path)
    if page is not None and not isinstance(client, LiveClient):
        raise ValueError(f"only a live client writes {page}; give another out_path")
    other = default_page(path, DEFAULT_OUTS.values())
    if other is not None:
        raise ValueError(f"{other} is the eval-reviewer page; give another out_path")


def _read(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_run(run_dir: str | Path) -> dict:
    """A sweep review run's run.json. A folder without one is not a run to resume."""
    path = Path(run_dir) / "run.json"
    if not path.is_file():
        raise ValueError(f"no run.json in {shown_path(run_dir)}: not a sweep review run to resume")
    run = _read(path)
    if "sweep_cases" not in run:
        raise ValueError(f"{shown_path(path)} is not from {COMMAND}")
    return run


def run_prompt(resume: str | Path | None, prompt: str | None) -> str:
    """The prompt a run uses: the recorded one on a resume, else the one given, else default.

    A resume that names another prompt than the one recorded is refused.
    """
    if resume is None:
        prompt = prompt or DEFAULT_PROMPT
    else:
        recorded = load_run(resume)["prompt"]
        if prompt is not None and prompt != recorded:
            raise ValueError(
                f"the run in {shown_path(resume)} uses the {recorded} prompt, not {prompt}"
            )
        prompt = recorded
    prompt_file(prompt)  # an unknown prompt fails here
    return prompt


def load_sweep(source: str | Path) -> list[dict]:
    """The sweep's cases in its order, each with its plates' seeds. Skipped seeds are left out."""
    path = Path(source) / SWEEP_DIR / "sweep.json"
    if not path.is_file():
        raise ValueError(
            f"no baseline sweep at {shown_path(path)}: give an eval-reviewer run folder "
            f"that holds {SWEEP_DIR}/sweep.json"
        )
    out = []
    for entry in _read(path)["cases"]:
        cid = str(entry["case"]["id"])
        if not _NAME.match(cid):
            raise ValueError(f"the sweep names a case '{cid}' that is not a plain folder name")
        out.append({"case": entry["case"], "seeds": [int(p["seed"]) for p in entry["plates"]]})
    return out


def choose_cases(known: list[str], wanted: list[str] | None) -> list[str]:
    """The cases to run, in order: the ones asked for, or every case in the sweep's order."""
    if not wanted:
        return list(known)
    unknown = [c for c in wanted if c not in known]
    if unknown:
        raise ValueError(
            f"unknown case {', '.join(unknown)}; the sweep holds {', '.join(known) or 'no case'}"
        )
    twice = [c for c, n in Counter(wanted).items() if n > 1]
    if twice:
        raise ValueError(f"case {', '.join(twice)} is named more than once")
    return list(wanted)


def plate_source(source: str | Path, case_id: str, seed: int) -> Path:
    """Where a sweep plate's evidence pack, true faults, and baseline reviews are."""
    return Path(source) / SWEEP_DIR / case_id / f"seed_{seed}" / "reviewer"


def plate_out(run_dir: str | Path, case_id: str, seed: int) -> Path:
    """Where this run keeps a plate's trial, transcript, and route log."""
    return Path(run_dir) / case_id / f"seed_{seed}"


def _inside(path: Path, folder: Path) -> bool:
    p, f = Path(path).resolve(), Path(folder).resolve()
    return p == f or p.is_relative_to(f)


def _same_place(recorded: str, source: Path) -> bool:
    """Whether a source recorded in run.json (as shown_path wrote it) is this source."""
    if recorded == shown_path(source):
        return True
    target = Path(source).resolve()
    return any((base / recorded).resolve() == target for base in (Path.cwd(), repo_root()))


def review_plate(
    source: Path,
    case_id: str,
    seed: int,
    out_dir: Path,
    client,
    settings: Settings | None,
    prompt: str,
) -> dict:
    """Review one sweep plate with the overwatch, once, and score it next to both baselines.

    Reads the plate's files from the source and writes trial_01.json,
    transcript.jsonl, and model_calls.jsonl under out_dir. A transcript left
    by an earlier try is reused, so a reply already recorded is not asked for
    again. A trial that ended on an outage keeps its record but has no score.
    """
    rdir = plate_source(source, case_id, seed)
    pack = _read(rdir / "evidence.json")
    truth = _read(rdir / "true_faults.json")
    trial: dict = {"case": case_id, "seed": seed, "trial": 1, "plate": shown_path(rdir)}
    trial["truth"] = truth
    named_by = {}
    for key, name in BASELINES.items():
        review = DataReview.model_validate(_read(rdir / name)["review"])
        named_by[key] = review.suspected_faults
        trial[key] = {
            "score": score(truth, review.suspected_faults),
            "problems": check_review(review, pack),
        }
    decision = route(
        ROLE,
        estimate_tokens(review_messages(pack, prompt)),
        settings,
        out_dir / "model_calls.jsonl",
        {"case": case_id, "seed": seed, "trial": 1, "prompt": prompt},
    )
    review, info = review_round(
        pack,
        client,
        decision.label,
        Transcript(out_dir / "transcript.jsonl"),
        scope="trial-01",
        prompt=prompt,
    )
    outage = bool(info.get("outage"))
    named = review.suspected_faults if review else []
    trial.update(
        {
            "review": review.model_dump(mode="json") if review else None,
            "info": info,
            "outage": outage,
            "score": None if outage else score(truth, named),
            "either": None if outage else score(truth, [*named_by["qc"], *named]),
            "either_stats": None if outage else score(truth, [*named_by["qc_stats"], *named]),
        }
    )
    _write(out_dir / TRIAL_FILE, trial)
    return trial


def read_trial(out_dir: Path) -> dict | None:
    """A plate's trial file, or None when there is none or it was cut off by a crash."""
    path = Path(out_dir) / TRIAL_FILE
    if not path.is_file():
        return None
    try:
        return _read(path)
    except json.JSONDecodeError:
        return None


def _new_run_dir(runs_dir: str | Path) -> Path:
    base = Path(runs_dir) / utc_stamp()
    n = 1
    while base.exists():
        n += 1
        base = Path(runs_dir) / f"{utc_stamp()}_{n}"
    return base


def review_sweep(
    source: str | Path,
    client,
    settings: Settings | None = None,
    *,
    prompt: str | None = None,
    cases: list[str] | None = None,
    runs_dir: str | Path = RUNS_DIR,
    resume: str | Path | None = None,
    out_path: str | Path | None = None,
    command: str = COMMAND,
) -> SweepResult:
    """Run the overwatch on every plate of a baseline sweep, once per plate, and write the page.

    cases selects and orders the cases to run (default: all, in the sweep's
    order). resume continues the run in that folder: its prompt and source are
    kept, plates done are not asked again, and cases given here are added to
    the run's cases. The page goes to out_path, or to the prompt's default
    page, which only a live client may write. Every check runs before
    anything is written, and nothing is ever written under source. The page
    is written when at least one plate has been reviewed.
    """
    source = Path(source)
    prompt = run_prompt(resume, prompt)
    out_path = Path(out_path) if out_path is not None else Path(SWEEP_OUTS[prompt])
    check_page(out_path, client)
    entries = load_sweep(source)
    seeds = {e["case"]["id"]: e["seeds"] for e in entries}
    order = choose_cases(list(seeds), cases)
    run_dir = Path(resume) if resume is not None else _new_run_dir(runs_dir)
    for path in (run_dir, out_path):
        if _inside(path, source):
            raise ValueError(
                f"{shown_path(path)} is inside the source {shown_path(source)}, "
                "which this command only reads"
            )
    session = {"command": command, "commit": git_commit(), "started": utc_now()}
    if resume is None:
        run = {
            "synthetic": True,
            "source": shown_path(source),
            "prompt": prompt,
            "prompt_file": prompt_file(prompt),
            "cases": order,
            "command": command,
            "trials_per_plate": 1,
            "sweep_cases": entries,
            "sessions": [session],
        }
    else:
        run = load_run(run_dir)
        if not _same_place(run["source"], source):
            raise ValueError(
                f"the run in {shown_path(run_dir)} reviews the sweep of {run['source']}, "
                f"not of {shown_path(source)}"
            )
        if run["sweep_cases"] != entries:
            raise ValueError(f"the sweep in {run['source']} changed since this run started")
        order = order if cases else list(run["cases"])
        run["cases"] = [*run["cases"], *(c for c in order if c not in run["cases"])]
        run["sessions"].append(session)
    plates = [(cid, seed) for cid in order for seed in seeds[cid]]
    for cid, seed in plates:
        rdir = plate_source(source, cid, seed)
        missing = [name for name in PLATE_FILES if not (rdir / name).is_file()]
        if missing:
            raise ValueError(f"{shown_path(rdir)} lacks {', '.join(missing)}")
    _write(run_dir / "run.json", run)
    in_a_row = asked = 0
    stopped = False
    for cid, seed in plates:
        pdir = plate_out(run_dir, cid, seed)
        done = read_trial(pdir)
        if done is not None and not done["outage"]:
            continue
        trial = review_plate(source, cid, seed, pdir, client, settings, prompt)
        asked += 1
        in_a_row = in_a_row + 1 if trial["outage"] else 0
        if in_a_row >= STOP_AFTER:
            stopped = True
            break
    trials = [t for ts in collect(run_dir, run).values() for t in ts]
    reviewed = sum(1 for t in trials if not t["outage"])
    page = None
    if reviewed:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(render(run_dir), encoding="utf-8")
        page = out_path
    total = sum(len(seeds[c]) for c in run["cases"])
    outages = sum(1 for t in trials if t["outage"])
    return SweepResult(page, run_dir, total, reviewed, outages, stopped, asked)


def collect(run_dir: Path, run: dict) -> dict[str, list[dict]]:
    """Each run case's trial files, in the sweep's case order and seed order."""
    out: dict[str, list[dict]] = {}
    for entry in run["sweep_cases"]:
        cid = entry["case"]["id"]
        if cid not in run["cases"]:
            continue
        trials = [read_trial(plate_out(run_dir, cid, seed)) for seed in entry["seeds"]]
        out[cid] = [t for t in trials if t is not None]
    return out


# ---------------------------------------------------------------- the page


def _score(t: dict, key: str) -> dict:
    return t[key]["score"] if key in BASELINES else t[key]


def _counts(trials: list[dict]) -> dict:
    """Numbers for one table row. Columns count reviewed plates only; outages are counted apart."""
    c: dict = defaultdict(int)
    for t in trials:
        if t["outage"]:
            c["outage"] += 1
            continue
        c["reviewed"] += 1
        c["true"] += len(t["truth"])
        c["true_wells"] += t["qc"]["score"]["wells"]["true"]
        c["invalid"] += int(t["review"] is None)
        for _, key in REVIEWERS:
            sc = _score(t, key)
            c[f"{key}_caught"] += len(sc["caught"])
            c[f"{key}_hits"] += sc["wells"]["hits"]
            c[f"{key}_alarm"] += int(bool(sc["false_alarms"]))
    return c


def _table_row(name: str, c: dict, plates: int) -> str:
    cells = [name, _of(c["reviewed"], plates), c["true"]]
    for _, key in REVIEWERS:
        cells += [
            _of(c[f"{key}_caught"], c["true"]),
            _ratio(c[f"{key}_hits"], c["true_wells"]),
            _of(c[f"{key}_alarm"], c["reviewed"]),
        ]
    cells += [_of(c["invalid"], c["reviewed"]), c["outage"]]
    return _row(cells)


def _n(k: int, word: str) -> str:
    return f"{k} {word}{'s' if k != 1 else ''}"


def pooled_sentence(c: dict, where: str) -> str:
    """One sentence, from the counts, on what pooling the overwatch with QC plus statistics did."""
    n, true, wells = c["reviewed"], c["true"], c["true_wells"]
    st_c, es_c = c["qc_stats_caught"], c["either_stats_caught"]
    st_h, es_h = c["qc_stats_hits"], c["either_stats_hits"]
    st_a, es_a = c["qc_stats_alarm"], c["either_stats_alarm"]
    lead = f"On {where}, pooling the overwatch with {STATS}"
    if es_c == st_c and es_h == st_h:
        text = (
            f"{lead} added nothing: it caught no more true faults ({_of(st_c, true)}) and named "
            f"no more faulty wells ({_of(st_h, wells)})"
        )
        if es_a == st_a:
            return text + ", and it added no plate with a false alarm."
        return (
            text + f". It only added a false alarm on {_n(es_a - st_a, 'more plate')} "
            f"({st_a} to {_of(es_a, n)})."
        )
    caught = (
        f"caught {_n(es_c - st_c, 'more true fault')} ({st_c} to {_of(es_c, true)})"
        if es_c > st_c
        else f"caught no more true faults ({_of(st_c, true)})"
    )
    named = (
        f"named {_n(es_h - st_h, 'more faulty well')} ({st_h} to {_of(es_h, wells)})"
        if es_h > st_h
        else f"named no more faulty wells ({_of(st_h, wells)})"
    )
    alarms = (
        f"added a false alarm on {_n(es_a - st_a, 'more plate')} ({st_a} to {_of(es_a, n)})"
        if es_a > st_a
        else "added no plate with a false alarm"
    )
    return f"{lead} {caught}, {named}, and {alarms}."


def prompt_note(prompt: str) -> str:
    """One paragraph that names the overwatch prompt and what it tells the model."""
    where = f"`{PROMPT_DIR}/{prompt_file(prompt)}`"
    if prompt == "no_hints":
        return (
            f"Overwatch prompt: no_hints ({where}): the default prompt with its fault signatures "
            "section removed. It keeps the rules, the fault kinds, and the reply format, but "
            "describes no signature."
        )
    return (
        f"Overwatch prompt: {prompt} ({where}). This prompt lists fault signatures, and some of "
        "them resemble how the hard cases were built. So a gain on the hard plates may come from "
        f"those hints. `{COMMAND} <source> --prompt no_hints` reviews the same plates with the "
        "prompt that drops them."
    )


def _models(trials: list[dict]) -> list[str]:
    return sorted({t["info"].get("model") or "" for t in trials if t["info"]["replies"]})


def how_to_read() -> list[str]:
    """The "How to read this" section."""
    constants = ", ".join(f"{name} ({value:g})" for name, value in STATS_CONSTANTS)
    return [
        "## How to read this",
        "",
        "- One trial per plate. The overwatch reviewed each plate once, so its columns add up "
        "over plates. Another run could get other replies on some plates.",
        f"- {QC} is fixed code that maps quality flags to verdicts and fault kinds. {STATS} adds "
        f"two plate statistics, a drift rule and an impurity rule, with constants fixed in "
        f"review.py: {constants}. Those constants were set with the hard-case recipes known, so "
        "read that column as what simple rules do once someone knows what to look for.",
        "- Pooling can only add false alarms. A pooled column names every fault the baseline or "
        "the overwatch names, so it catches at least what each catches alone, and it has a false "
        "alarm on every plate where either has one. A model that raises fewer false alarms shows "
        f"it only in {OW} against {STATS}.",
        "- Scoring is the same as on the reviewer evaluation page. A true fault is caught when a "
        "review names its kind and, for injection_failure or nmr_impurity, at least one of its "
        "wells. For those two kinds, each named well the fault log does not list under that kind "
        "is a false alarm. A plate-wide kind or other that matches no true fault is one false "
        "alarm. lcms_nmr_disagreement is not a false alarm on a plate with a true fault.",
        f"- No valid review means no reply passed code's checks in {ATTEMPTS} attempts (up to "
        f"{ATTEMPTS * CALLS_PER_ATTEMPT} replies). Such a plate counts as the overwatch naming no "
        "fault, and a person would take over. A plate whose trial ended on an endpoint outage is "
        "not scored and is left out of every column; a resumed run tries it again.",
        "",
    ]


def render(run_dir: str | Path) -> str:
    """The sweep review page as Markdown, built from the run folder alone."""
    run_dir = Path(run_dir)
    run = load_run(run_dir)
    by_case = collect(run_dir, run)
    cases = {e["case"]["id"]: e for e in run["sweep_cases"]}
    trials = [t for ts in by_case.values() for t in ts]
    source, shown_run = run["source"], shown_path(run_dir)
    models = _models(trials)
    sweep_total = sum(len(e["seeds"]) for e in run["sweep_cases"])
    in_run = sum(len(cases[c]["seeds"]) for c in by_case)
    tot = _counts(trials)
    hard = [t for cid, ts in by_case.items() if cases[cid]["case"].get("hard") for t in ts]
    hard_plates = sum(len(cases[c]["seeds"]) for c in by_case if cases[c]["case"].get("hard"))
    reviewed, outages = tot["reviewed"], tot["outage"]
    untried = in_run - reviewed - outages
    lines = ["# Overwatch on the baseline sweep", ""]
    if "scripted" in models:
        lines += [SCRIPTED_BANNER, ""]
    elif any(m in NOT_A_MODEL for m in models):
        lines += [UNNAMED_BANNER, ""]
    if reviewed < in_run:
        lines += [
            f"**This run is not finished: the overwatch reviewed {_of(reviewed, in_run)} plates "
            f"in its cases. {outages} ended on an endpoint outage and {untried} "
            f"{'was' if untried == 1 else 'were'} not tried yet. Every column counts only the "
            f"reviewed plates. `{COMMAND} {source} --resume {shown_run}` continues it.**",
            "",
        ]
    resumed = [s["command"] for s in run["sessions"][1:]]
    generated = f"Generated by `{run['command']}`"
    if resumed:
        generated += ", resumed with " + ", then ".join(f"`{c}`" for c in resumed)
    scope = f"The overwatch reviewed {_of(reviewed, sweep_total)} plates in the sweep."
    if in_run < sweep_total:
        scope += (
            f" This run covers {len(by_case)} of the sweep's {len(cases)} cases "
            f"({', '.join(by_case)}), with {in_run} plates."
        )
    lines += [
        f"{generated}. Every number below comes from the plate folders under `{shown_run}`.",
        "",
        "## What this is",
        "",
        f"These are the plates of the baseline sweep of `{source}`, now also reviewed by the "
        "overwatch. That sweep built each fault case again on other seeds and ran only the two "
        "fixed baselines on them, with no model. This run builds no plate: it reads each plate's "
        f"files from `{source}/{SWEEP_DIR}` and writes nothing there. Seeds the sweep skipped, "
        "because the case's fault did not happen on them, are not here.",
        "",
        "All data are synthetic. The plates and their true fault logs come from the simulators: "
        "a simulated LC/MS and a simulated 1H NMR read a made up yield surface, and the "
        "simulators inject the faults and log them. The overwatch sees only each plate's evidence "
        "pack, never the fault log. Both baselines are scored again here from their stored "
        "reviews, with the same scoring code as the overwatch.",
        "",
        f"{scope} Every column below is computed on exactly the reviewed plates, so all five "
        "reviewers are compared on the same plates.",
        "",
        prompt_note(run["prompt"]),
        "",
        "## What pooling added",
        "",
    ]
    pooled = [pooled_sentence(tot, f"the {_n(reviewed, 'reviewed plate')}")]
    h = _counts(hard)
    if h["reviewed"]:
        pooled.append(pooled_sentence(h, f"the {_n(h['reviewed'], 'reviewed hard plate')}"))
    elif hard_plates:
        pooled.append("No hard plate has been reviewed yet.")
    lines += [" ".join(pooled), "", "## Per case", ""]
    lines += [
        "Plates reviewed: plates with a scored trial, of the case's plates in the sweep. Faults "
        "caught: true faults caught, of all true faults on the reviewed plates. Well recall: of "
        "the wells the fault logs list under injection_failure or nmr_impurity, the share a "
        "review named under the right kind. Plates with a false alarm: reviewed plates on which "
        "the reviewer raised at least one false alarm. Cases marked (hard) are made to slip past "
        "the fixed QC flags on their case seeds.",
        "",
        "| Case | Plates reviewed | True faults | "
        + " | ".join(
            f"{who}: {col}"
            for who, _ in REVIEWERS
            for col in ("faults caught", "well recall", "plates with a false alarm")
        )
        + " | No valid review | Ended on an outage |",
        "|" + "---|" * (5 + 3 * len(REVIEWERS)),
    ]
    for cid, ts in by_case.items():
        case = cases[cid]["case"]
        name = cid + (" (hard)" if case.get("hard") else "")
        lines.append(_table_row(name, _counts(ts), len(cases[cid]["seeds"])))
    lines.append(_table_row("**Total**", tot, in_run))
    if hard_plates:
        lines.append(_table_row("**Hard cases**", h, hard_plates))
    commits = list(dict.fromkeys(s["commit"] for s in run["sessions"]))
    lines += [
        "",
        f"No valid review: {_of(tot['invalid'], reviewed)} reviewed plates. Ended on an "
        f"endpoint outage and not scored: {_n(outages, 'plate')}.  ",
        "Overwatch model, as reported by the endpoint: "
        + (", ".join(m or "unknown (no name reported)" for m in models) or "none recorded")
        + "  ",
        f"Overwatch prompt: {run['prompt']} ({prompt_file(run['prompt'])})  ",
        f"Model replies: {sum(t['info']['replies'] for t in trials)}  ",
        f"Source: `{source}`, baseline sweep in `{source}/{SWEEP_DIR}`  ",
        f"Date: {datetime.now(timezone.utc).date().isoformat()}  ",
        ("Git commit: " if len(commits) == 1 else "Git commits, by session: ")
        + ", ".join(commits)
        + "  ",
        f"Command: `{run['command']}`" + "".join(f"  \nResumed with: `{c}`" for c in resumed),
        "",
        *how_to_read(),
        "## Failure notes",
        "",
        *failure_notes(trials),
    ]
    return "\n".join(lines) + "\n"


def _plates(items: list[tuple[str, int, str]], head: str) -> list[str]:
    """A heading, then plates with a note each, LIST_MAX at most."""
    out = [head, ""]
    out += [f"- {cid}, seed {seed}: {_short(text)}" for cid, seed, text in items[:LIST_MAX]]
    if len(items) > LIST_MAX:
        out.append(f"- and {len(items) - LIST_MAX} more, in the trial files")
    return [*out, ""]


def failure_notes(trials: list[dict]) -> list[str]:
    """What went wrong, grouped by kind, each with one real example from the replies.

    Missed faults and false alarms are counted in reviewed plates, per case.
    """
    problems: dict[str, list[tuple[str, int, int, str]]] = defaultdict(list)
    whos = ((QC, "qc"), (STATS, "qc_stats"), (OW, "score"))
    missed: dict[str, dict[str, list[str]]] = {who: defaultdict(list) for who, _ in whos}
    alarms: dict[str, dict[str, list[str]]] = {who: defaultdict(list) for who, _ in whos}
    broken, no_review, outages = [], [], []
    for t in trials:
        cid, seed = t["case"], t["seed"]
        for a in t["info"]["attempts"]:
            for p in a.get("problems") or []:
                problems[p["kind"]].append((cid, seed, a["attempt"], p["message"]))
        if t["outage"]:
            outages.append((cid, seed, t["info"]["error"] or ""))
            continue
        if t["review"] is None:
            no_review.append((cid, seed, t["info"]["note"]))
        for who, key in whos:
            sc = _score(t, key)
            for kind in {f["kind"] for f in sc["missed"]}:
                missed[who][kind].append(cid)
            for kind in {f["kind"] for f in sc["false_alarms"]}:
                alarms[who][kind].append(cid)
        for who, key in whos[:2]:
            if t[key]["problems"]:
                broken.append((cid, seed, f"{who}: " + " ".join(t[key]["problems"])))
    out = []
    if broken:
        out += _plates(
            broken,
            f"A baseline review failed code's own checks {_n(len(broken), 'time')}. "
            "That is a bug in the baseline:",
        )
    if problems:
        out += [
            "Replies that code rejected, by problem. Each example is quoted from a real reply's check.",
            "",
        ]
        for kind, items in sorted(problems.items(), key=lambda kv: -len(kv[1])):
            cid, seed, attempt, msg = items[0]
            plates = len({(i[0], i[1]) for i in items})
            cases = ", ".join(dict.fromkeys(i[0] for i in items))
            out.append(
                f"- {kind}: {_n(len(items), 'time')}, on {_n(plates, 'plate')} in {cases}. "
                f"Example from {cid}, seed {seed}, reply {attempt}: {_short(msg)}"
            )
        out.append("")
    if no_review:
        out += _plates(no_review, f"No valid review on {_n(len(no_review), 'plate')}:")
    if outages:
        out += _plates(outages, f"Ended on an endpoint outage on {_n(len(outages), 'plate')}:")
    listed = []
    for who, _ in whos:
        for title, table in (("missed", missed[who]), ("false alarms", alarms[who])):
            if table:
                text = "; ".join(f"{k} in {_counted(v)}" for k, v in sorted(table.items()))
                listed.append(f"- {who}, {title}: {text}.")
    if listed:
        out += ["Missed faults and false alarms, counted in reviewed plates per case:", "", *listed]
    if not out:
        out.append("No reply was rejected, no fault was missed, and no false alarm was raised.")
    return out
