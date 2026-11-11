"""How many rounds does each strategy need to recommend a near optimal condition?

After each round, a strategy's recommendation is the tested condition with the
highest measured mean. It is scored by its true yield on the noise free
surface, which is what a lab would act on. Every seed is a different surface
(surface seed = seed), and round 1 is the same Latin hypercube for every
strategy on a seed.

Fast path: the plan's volumes are taken as the ledger, and compile and
simulate are skipped. The full path is what `labauto loop` runs.
"""

from __future__ import annotations

import os
import statistics
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from labauto.config import repo_root
from labauto.decide.base import build_history, recommendation
from labauto.decide.optimizer import OptimizerDecider, round_one
from labauto.decide.random_decider import RandomDecider
from labauto.execute import design_for_round, ledger_from_plan, measure_and_analyze, result_rows
from labauto.expand import expand
from labauto.models import Assay, Decision
from labauto.virtual.ground_truth import grid_optimum, reference_expected, surface_for, true_yield

NEAR = 3.0


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
    for r in range(1, rounds + 1):
        history = build_history(rows, assay, r)
        decision = round_one(assay, n, seed, strategy) if r == 1 else decide(history, n, seed)
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
    return scores


def decider_for(strategy: str, assay: Assay, model_factory=None):
    """A decide(history, n, seed) function for a strategy name."""
    if strategy == "random":
        d = RandomDecider(assay)
    elif strategy == "optimizer":
        d = OptimizerDecider(assay)
    elif strategy == "model":
        if model_factory is None:
            return None
        d = model_factory()
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
) -> dict:
    """Score every strategy on every seed. Returns the raw scores and the optimum per seed.

    surface_seed holds the surface fixed for every seed, as in Appendix C's check.
    """
    out: dict = {
        "scores": {},
        "optimum": {},
        "seconds": {},
        "skipped": {},
        "surface_seed": surface_seed,
    }
    for seed in range(1, seeds + 1):
        out["optimum"][seed] = grid_optimum(
            assay, surface_for(seed if surface_seed is None else surface_seed)
        )[0]
    for strategy in strategies:
        decide = decider_for(strategy, assay, model_factory)
        if decide is None:
            out["skipped"][strategy] = "no model endpoint configured; live results pending"
            continue
        count = max(3, model_seeds) if strategy == "model" else seeds
        start = time.perf_counter()
        out["scores"][strategy] = {}
        for seed in range(1, count + 1):
            out["scores"][strategy][seed] = fast_campaign(
                assay, decide, strategy, seed, rounds, surface_seed=surface_seed
            )
            if progress:
                progress(f"{strategy} seed {seed}: {out['scores'][strategy][seed][-1]:.1f}")
        out["seconds"][strategy] = round(time.perf_counter() - start, 1)
    return out


def summarize(result: dict, rounds: int) -> dict:
    """Mean and sd of the recommendation's true yield, and the share within 3 points, per round."""
    table = {}
    for strategy, by_seed in result["scores"].items():
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

    head = "| Strategy | Seeds | " + " | ".join(f"Round {r}" for r in range(1, rounds + 1)) + " |"
    sep = "|---|---|" + "---|" * rounds
    lines = [
        "# Closed loop benchmark",
        "",
        "Question: how many rounds does each strategy need to recommend a near optimal condition?",
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
        "## Mean true yield of the recommendation (%)",
        "",
        head,
        sep,
    ]
    for strategy, t in table.items():
        lines.append(
            f"| {strategy} | {t['seeds']} | "
            + " | ".join(f"{x['mean']:.1f}" for x in t["rounds"])
            + " |"
        )
    lines += ["", f"## Share of seeds within {NEAR:g} points of the optimum", "", head, sep]
    for strategy, t in table.items():
        lines.append(
            f"| {strategy} | {t['seeds']} | "
            + " | ".join(f"{x['share']:.2f}" for x in t["rounds"])
            + " |"
        )
    lines += [
        "",
        f"Mean true optimum over the {len(seeds_in_plot)} surfaces: {mean_opt:.1f}%.",
        "",
        "![Benchmark](img/benchmark.png)",
        "",
    ]
    for strategy, why in result["skipped"].items():
        lines.append(f"- {strategy}: not run, {why}.")
    for strategy, secs in result["seconds"].items():
        lines.append(f"- {strategy}: {secs:.0f} s for {table[strategy]['seeds']} seeds.")
    lines += [
        "",
        f"Model: {model_name or 'none (no model strategy was run)'}  ",
        f"Date: {datetime.now(timezone.utc).date().isoformat()}  ",
        f"Git commit: {commit}",
        "",
    ]
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(lines), encoding="utf-8")


def plot(table: dict, rounds: int, mean_opt: float, path: Path) -> None:
    """Round on x, true yield of the recommendation on y, a band of one sd, the mean optimum dashed."""
    os.environ.setdefault("MPLCONFIGDIR", str(repo_root() / ".labauto" / "mpl"))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    x = list(range(1, rounds + 1))
    fig, ax = plt.subplots(figsize=(7, 4.2), dpi=120)
    colors = {"random": "#7a7a7a", "optimizer": "#1f6fb4", "model": "#c4572a"}
    for strategy, t in table.items():
        mean = [r["mean"] for r in t["rounds"]]
        sd = [r["sd"] for r in t["rounds"]]
        color = colors.get(strategy, "#333333")
        ax.plot(x, mean, marker="o", color=color, label=f"{strategy} ({t['seeds']} seeds)")
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
