"""How many rounds does each strategy need to recommend a near optimal condition?

After each round, a strategy's recommendation is the tested condition with the
highest measured mean. It is scored by its true yield on the noise free
surface, which is what a lab would act on. Every seed is a different surface
(surface seed = seed), and round 1 is the same Latin hypercube for every
strategy on a seed.

Fast path: the plan's volumes are taken as the ledger, and compile and
simulate are skipped. The full path is what `labauto loop` runs.

Model runs are recorded. Each model seed gets its own transcript,
model/seed_NN.jsonl in the run folder. Running again with the same folder
replays the calls recorded there and asks the model only for the rest.
result.json in the same folder holds everything the page is built from.
"""

from __future__ import annotations

import hashlib
import json
import os
import statistics
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from labauto.agents.llm import Transcript
from labauto.config import repo_root
from labauto.decide import model_decider
from labauto.decide.base import build_history, recommendation
from labauto.decide.optimizer import OptimizerDecider, round_one
from labauto.decide.random_decider import RandomDecider
from labauto.execute import design_for_round, ledger_from_plan, measure_and_analyze, result_rows
from labauto.expand import expand
from labauto.models import Assay, Decision
from labauto.virtual.ground_truth import grid_optimum, reference_expected, surface_for, true_yield

NEAR = 3.0
MIN_MODEL_SEEDS = 3


class BenchmarkError(ValueError):
    """A run folder that cannot be resumed with these settings."""


def _turns(messages: list[dict] | None) -> list[tuple]:
    """The messages a call sends, without the system message.

    A client may add the reply schema to the system message before it records
    it, so the system prompt is checked by its hash in model/settings.json instead.
    """
    return [(m.get("role"), m.get("content")) for m in messages or [] if m.get("role") != "system"]


class CountingTranscript(Transcript):
    """A transcript that also counts the calls it served from lines recorded earlier.

    A recorded reply is replayed only when it was asked with the same messages
    this run would send. Otherwise the code or the assay changed since it was
    recorded, and the run stops with a BenchmarkError.
    """

    def __init__(self, path: Path | None):
        super().__init__(path)
        self.recorded_before = len(self.records)
        self.replayed = 0

    def lookup(self, meta: dict, call: int, messages: list[dict] | None = None) -> dict | None:
        rec = super().lookup(meta, call, messages)
        if rec is None:
            return None
        if messages is not None and _turns(rec.get("messages")) != _turns(messages):
            where = "this run" if self.path is None else shown_path(self.path)
            raise BenchmarkError(
                f"{where} holds a reply to {meta.get('scope')} turn {meta.get('attempt')} "
                f"call {call} that was asked with other messages than this run would send. "
                "The decider's code or the assay changed since it was recorded. "
                "Use another --run-dir."
            )
        self.replayed += 1
        return rec

    @property
    def fresh(self) -> int:
        """Calls sent to the model in this run. Each one added a line."""
        return len(self.records) - self.recorded_before


def transcript_path(run_dir: Path, seed: int) -> Path:
    """Where one model seed's calls are recorded: model/seed_NN.jsonl."""
    return Path(run_dir) / "model" / f"seed_{seed:02d}.jsonl"


def shown_path(path: str | Path) -> str:
    """A folder as the page shows it: relative to the repository, or only its name when outside.

    The page may be committed, so it never carries a home folder or other local path.
    """
    p = Path(path).resolve()
    try:
        return p.relative_to(repo_root().resolve()).as_posix()
    except ValueError:
        return f"<outside the repository>/{p.name}"


def decider_prompt_sha256() -> str:
    """sha256 of the model decider's system prompt, as the decider loads it."""
    return hashlib.sha256(
        model_decider.load_prompt("decider_system.md").encode("utf-8")
    ).hexdigest()


def check_model_settings(run_dir: Path, settings: dict) -> None:
    """Refuse to replay calls that were made for another surface, model, or decider prompt.

    The first model run in a folder writes model/settings.json. A later run in
    the same folder must match it, or its replayed replies would answer
    questions that were never asked.
    """
    path = Path(run_dir) / "model" / "settings.json"
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        for name, value in settings.items():
            if name in old and old[name] != value:
                raise BenchmarkError(
                    f"{shown_path(run_dir)} holds model calls made with {name} "
                    f"{json.dumps(old[name])}, but this run has {json.dumps(value)}. "
                    "Use another --run-dir."
                )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def fast_campaign(
    assay: Assay,
    decide: Callable[[object, int, int], Decision],
    strategy: str,
    seed: int,
    rounds: int = 6,
    n: int = 8,
    replicates: int = 3,
    faults: list[str] | None = None,
    surface_seed: int | None = None,
    stats: dict | None = None,
) -> list[float]:
    """Run one campaign on the fast path. Returns the true yield of each round's recommendation.

    The surface seed equals the seed unless surface_seed is given (Appendix C's
    sanity check holds the surface at seed 0 and varies only the campaign seed).
    """
    faults = ["injection_failure"] if faults is None else faults
    sseed = seed if surface_seed is None else surface_seed
    surface = surface_for(sseed)
    expected = reference_expected(assay, sseed)
    rows: list[dict] = []
    scores = []
    fallbacks = 0
    for r in range(1, rounds + 1):
        history = build_history(rows, assay, r)
        decision = round_one(assay, n, seed) if r == 1 else decide(history, n, seed)
        fallbacks += 1 if decision.fallback else 0
        design = design_for_round(
            decision, assay, "fast_path", f"bench-{strategy}-{seed}", r, replicates, seed
        )
        plan = expand(design, assay)
        _, results, _ = measure_and_analyze(
            plan, ledger_from_plan(plan), assay, surface, seed, r, faults, expected
        )
        rows += result_rows(
            results,
            assay,
            campaign_id=f"bench-{seed}",
            round_number=r,
            attempt=1,
            run_id="fast",
            design_hash=None,
            protocol_hash=None,
            decided_by=strategy,
            timestamp="",
        )
        rec = recommendation(build_history(rows, assay, r + 1))
        scores.append(true_yield(rec["conditions"], surface) if rec else 0.0)
    if stats is not None:
        stats["fallbacks"] = stats.get("fallbacks", 0) + fallbacks
        stats["decided"] = stats.get("decided", 0) + rounds - 1
    return scores


def decider_for(strategy: str, assay: Assay, model_factory=None, transcript=None):
    """A decide(history, n, seed) function for a strategy name.

    For the model, model_factory(transcript) builds the decider, which records
    its calls in that transcript. None when there is no model_factory.
    """
    if strategy == "random":
        d = RandomDecider(assay)
    elif strategy == "optimizer":
        d = OptimizerDecider(assay)
    elif strategy == "model":
        if model_factory is None:
            return None
        d = model_factory(transcript)
    else:
        raise ValueError(f"unknown strategy '{strategy}'")
    return lambda history, n, seed: d.decide(history, n, "", seed)


def run_benchmark(
    assay: Assay,
    strategies: list[str],
    seeds: int = 30,
    model_seeds: int = 3,
    rounds: int = 6,
    model_factory=None,
    progress: Callable[[str], None] | None = None,
    surface_seed: int | None = None,
    run_dir: Path | None = None,
    command: str = "",
    model_name: str | None = None,
) -> dict:
    """Score every strategy on every seed. Returns the raw scores and the optimum per seed.

    surface_seed holds the surface fixed for every seed, as in Appendix C's check.
    model_factory(transcript) builds one model decider per seed, each with its
    own transcript. With run_dir, those are run_dir/model/seed_NN.jsonl: calls
    already recorded there are replayed, and only the rest go to the model.
    The result is then also saved as run_dir/result.json.
    """
    run_model = "model" in strategies and model_factory is not None
    model_count = max(MIN_MODEL_SEEDS, model_seeds)
    out: dict = {
        "scores": {},
        "optimum": {},
        "seconds": {},
        "skipped": {},
        "fallbacks": {},
        "surface_seed": surface_seed,
        "rounds": rounds,
        "seeds": seeds,
        "model_seeds": model_count if run_model else 0,
        "command": command,
        "model_name": model_name,
        "run_dir": None if run_dir is None else shown_path(run_dir),
    }
    recorded = run_dir is not None and any((Path(run_dir) / "model").glob("seed_*.jsonl"))
    if "model" in strategies and not run_model and recorded:
        # Skipping the model here would save a result.json without the model's scores.
        raise BenchmarkError(
            f"{shown_path(run_dir)} holds recorded model calls, but no model endpoint is "
            "configured, so this run would drop the model's results from that folder. "
            "Configure the endpoint to resume it, or use another --run-dir."
        )
    if run_model and run_dir is not None:
        check_model_settings(
            run_dir,
            {
                "surface_seed": surface_seed,
                "model": model_name,
                "decider_prompt_sha256": decider_prompt_sha256(),
            },
        )
    for seed in range(1, max(seeds, model_count if run_model else 0) + 1):
        out["optimum"][seed] = grid_optimum(
            assay, surface_for(seed if surface_seed is None else surface_seed)
        )[0]
    calls: dict[int, dict] = {}
    for strategy in strategies:
        decide = None if strategy == "model" else decider_for(strategy, assay)
        if strategy == "model" and not run_model:
            out["skipped"][strategy] = "no model endpoint configured; live results pending"
            continue
        count = model_count if strategy == "model" else seeds
        start = time.perf_counter()
        out["scores"][strategy] = {}
        stats: dict = {}
        for seed in range(1, count + 1):
            transcript = None
            if strategy == "model":
                transcript = CountingTranscript(
                    None if run_dir is None else transcript_path(run_dir, seed)
                )
                decide = decider_for(strategy, assay, model_factory, transcript)
            out["scores"][strategy][seed] = fast_campaign(
                assay, decide, strategy, seed, rounds, surface_seed=surface_seed, stats=stats
            )
            note = ""
            if transcript is not None:
                calls[seed] = {"fresh": transcript.fresh, "replayed": transcript.replayed}
                note = f" ({transcript.fresh} model calls made, {transcript.replayed} replayed)"
            if progress:
                progress(f"{strategy} seed {seed}: {out['scores'][strategy][seed][-1]:.1f}{note}")
        out["seconds"][strategy] = round(time.perf_counter() - start, 1)
        out["fallbacks"][strategy] = (stats.get("fallbacks", 0), stats.get("decided", 0))
    if "model" in out["scores"]:
        out["model_calls"] = {
            "fresh": sum(c["fresh"] for c in calls.values()),
            "replayed": sum(c["replayed"] for c in calls.values()),
            "by_seed": calls,
        }
    if run_dir is not None:
        save_result(out, Path(run_dir) / "result.json")
    return out


def save_result(result: dict, path: Path) -> None:
    """Write result.json. JSON keys are text, so seeds become strings here."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def load_result(path: Path) -> dict:
    """Read result.json back, with seeds as whole numbers again, as run_benchmark returns them."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data["scores"] = {
        strategy: {int(seed): scores for seed, scores in by_seed.items()}
        for strategy, by_seed in data["scores"].items()
    }
    data["optimum"] = {int(seed): value for seed, value in data["optimum"].items()}
    data["fallbacks"] = {s: tuple(pair) for s, pair in data.get("fallbacks", {}).items()}
    if data.get("model_calls"):
        by_seed = data["model_calls"].get("by_seed", {})
        data["model_calls"]["by_seed"] = {int(seed): c for seed, c in by_seed.items()}
    return data


def summarize(result: dict, rounds: int, only: set[int] | None = None) -> dict:
    """Mean and sd of the recommendation's true yield, and the share within 3 points, per round.

    With only, each strategy is scored on those seeds alone.
    """
    table = {}
    for strategy, by_seed in result["scores"].items():
        if only is not None:
            by_seed = {seed: scores for seed, scores in by_seed.items() if seed in only}
        if not by_seed:
            continue
        rows = []
        for r in range(rounds):
            vals = [scores[r] for scores in by_seed.values()]
            near = [scores[r] >= result["optimum"][seed] - NEAR for seed, scores in by_seed.items()]
            rows.append(
                {
                    "mean": statistics.fmean(vals),
                    "sd": statistics.stdev(vals) if len(vals) > 1 else 0.0,
                    "share": sum(near) / len(near),
                }
            )
        table[strategy] = {"seeds": len(by_seed), "rounds": rows}
    return table


def _table_lines(table: dict, rounds: int, level: str) -> list[str]:
    """The two tables: mean true yield of the recommendation, and the share near the optimum."""
    head = "| Strategy | Seeds | " + " | ".join(f"Round {r}" for r in range(1, rounds + 1)) + " |"
    sep = "|---|---|" + "---|" * rounds
    lines = [f"{level} Mean true yield of the recommendation (%)", "", head, sep]
    for strategy, t in table.items():
        lines.append(
            f"| {strategy} | {t['seeds']} | "
            + " | ".join(f"{x['mean']:.1f}" for x in t["rounds"])
            + " |"
        )
    lines += ["", f"{level} Share of seeds within {NEAR:g} points of the optimum", "", head, sep]
    for strategy, t in table.items():
        lines.append(
            f"| {strategy} | {t['seeds']} | "
            + " | ".join(f"{x['share']:.2f}" for x in t["rounds"])
            + " |"
        )
    return lines


def _optimum_line(result: dict, count: int, mean_opt: float, these: str = "the") -> str:
    if result.get("surface_seed") is None:
        return f"Mean true optimum over {these} {count} surfaces: {mean_opt:.1f}%."
    return f"True optimum of surface seed {result['surface_seed']}: {mean_opt:.1f}%."


def _shared_seeds(result: dict) -> list[int]:
    """The seeds every strategy ran on, when the model ran on other seeds than the rest.

    Empty when every strategy ran on the same seeds, or the model did not run.
    """
    model = set(result["scores"].get("model", {}))
    others = [set(b) for s, b in result["scores"].items() if s != "model" and b]
    if not model or not others or all(o == model for o in others):
        return []
    return sorted(model.intersection(*others))


def _model_call_lines(result: dict) -> list[str]:
    """Where the model's calls are, and how many were made in this run or replayed."""
    calls = result.get("model_calls")
    if not calls:
        return []
    fresh, replayed = calls["fresh"], calls["replayed"]
    counts = f"Model calls: {fresh} made in this run, {replayed} replayed from an earlier run"
    if result.get("run_dir"):
        lines = [
            f"- model: its calls are recorded in `{result['run_dir']}/model/`, one file per seed. "
            f"{counts} in that folder."
        ]
    else:
        lines = [f"- model: its calls were not saved, because no run folder was given. {counts}."]
    if replayed:
        lines.append(
            f"- model: {replayed} of the {fresh + replayed} model calls were replayed, not sent "
            "to the model in this run. Their replies were read from the files an earlier run "
            "wrote, and the time above leaves them out."
        )
    return lines


def write_outputs(
    result: dict,
    rounds: int,
    command: str,
    md_path: Path,
    png_path: Path,
    model_name: str | None,
    commit: str,
) -> None:
    """docs/benchmark.md and docs/img/benchmark.png, both from the same result."""
    table = summarize(result, rounds)
    seeds_in_plot = sorted({s for by_seed in result["scores"].values() for s in by_seed})
    mean_opt = (
        statistics.fmean(result["optimum"][s] for s in seeds_in_plot) if seeds_in_plot else 0.0
    )
    plot(table, rounds, mean_opt, png_path)
    shared = _shared_seeds(result)
    model_fewer = shared == sorted(result["scores"].get("model", {}))
    section = "On the model's seeds" if model_fewer else "On the shared seeds"

    lines = [
        "# Closed loop benchmark",
        "",
        "Question: how many rounds does each strategy need to recommend a near optimal condition? "
        "The surfaces and the LC/MS readings are synthetic.",
        "",
        "After each round, the recommendation is the tested condition with the highest measured mean. "
        "It is scored by its true yield on the noise free surface. "
        + (
            "Each seed is a different synthetic surface (surface seed = seed). "
            if result.get("surface_seed") is None
            else f"Every seed uses the same surface (surface seed {result['surface_seed']}); only the campaign seed changes. "
        )
        + "Round 1 is the same Latin hypercube for every strategy on a seed. "
        "8 conditions x 3 replicates per round, noise sd 2.5, injection failures on.",
        "",
        "This run used the fast path: the plan's volumes stand in for the simulator's ledger, and compile "
        "and simulate are skipped. `labauto loop` runs the full path.",
        "",
        f"Command: `{command}`",
        "",
    ]
    lines += _table_lines(table, rounds, "##")
    lines += ["", _optimum_line(result, len(seeds_in_plot), mean_opt), ""]
    if shared:
        others = "more" if model_fewer else str(len(shared))
        lines += [
            f"The model ran on {len(result['scores']['model'])} seeds and the others on {others}, "
            f'so the tables above do not compare them on the same seeds. See "{section}" below.',
            "",
        ]
    lines += [f"![Benchmark]({os.path.relpath(png_path, md_path.parent)})", ""]
    for strategy, why in result["skipped"].items():
        lines.append(f"- {strategy}: not run, {why}.")
    for strategy, (fell, decided) in result.get("fallbacks", {}).items():
        if strategy == "model":
            lines.append(
                f"- model: {fell} of {decided} decided rounds fell back to the optimizer's picks"
                + (
                    "; the model row then shows the optimizer, not the model."
                    if decided and fell == decided
                    else "."
                )
            )
    for strategy, secs in result["seconds"].items():
        lines.append(f"- {strategy}: {secs:.0f} s for {table[strategy]['seeds']} seeds.")
    lines += _model_call_lines(result)
    if shared:
        same = summarize(result, rounds, only=set(shared))
        same_opt = statistics.fmean(result["optimum"][s] for s in shared)
        lines += [
            "",
            f"## {section}",
            "",
            f"These tables score every strategy on seeds {shared[0]} to {shared[-1]}, "
            f"the only seeds the {'model' if model_fewer else 'others'} ran on, so compare the "
            "model with the others here and not in the tables above.",
            "",
        ]
        lines += _table_lines(same, rounds, "###")
        lines += ["", _optimum_line(result, len(shared), same_opt, "these")]
    lines += [
        "",
        f"Model: {model_name or 'none (no model strategy was run)'}  ",
        f"Date: {datetime.now(timezone.utc).date().isoformat()}  ",
        f"Git commit: {commit}",
        "",
    ]
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(lines), encoding="utf-8")


def line_labels(table: dict) -> dict[str, str]:
    """The legend label of each line, with its seed count.

    A model line on fewer seeds than the others reads, for example,
    "model (10 seeds)" next to "optimizer (30 seeds)".
    """
    return {strategy: f"{strategy} ({t['seeds']} seeds)" for strategy, t in table.items()}


def plot(table: dict, rounds: int, mean_opt: float, path: Path) -> None:
    """Round on x, true yield of the recommendation on y, a band of one sd, the mean optimum dashed."""
    os.environ.setdefault("MPLCONFIGDIR", str(repo_root() / ".labauto" / "mpl"))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    x = list(range(1, rounds + 1))
    fig, ax = plt.subplots(figsize=(7, 4.2), dpi=120)
    colors = {"random": "#7a7a7a", "optimizer": "#1f6fb4", "model": "#c4572a"}
    labels = line_labels(table)
    for strategy, t in table.items():
        mean = [r["mean"] for r in t["rounds"]]
        sd = [r["sd"] for r in t["rounds"]]
        color = colors.get(strategy, "#333333")
        ax.plot(x, mean, marker="o", color=color, label=labels[strategy])
        ax.fill_between(
            x,
            [m - s for m, s in zip(mean, sd, strict=True)],
            [m + s for m, s in zip(mean, sd, strict=True)],
            color=color,
            alpha=0.15,
        )
    ax.axhline(
        mean_opt,
        linestyle="--",
        color="#222222",
        linewidth=1,
        label=f"mean true optimum ({mean_opt:.1f}%)",
    )
    ax.set_xlabel("Round")
    ax.set_ylabel("True yield of the recommendation (%)")
    ax.set_xticks(x)
    ax.set_title("Recommendation quality by round (synthetic surfaces)")
    ax.legend(loc="lower right", fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)
