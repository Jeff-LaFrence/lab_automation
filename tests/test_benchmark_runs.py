"""The benchmark's model runs: recorded per seed, resumable from a run folder, and compared
with the other strategies on the same seeds. All model calls here are scripted."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from conftest import ROOT

from labauto.agents.llm import EndpointUnavailable, ScriptedClient, Transcript
from labauto.benchmark import (
    BenchmarkError,
    decider_prompt_sha256,
    line_labels,
    load_result,
    run_benchmark,
    shown_path,
    summarize,
    write_outputs,
)
from labauto.cli import main
from labauto.decide.base import grid_conditions, key
from labauto.decide.model_decider import ModelDecider
from labauto.decide.optimizer import round_one

ROUNDS = 3
SEEDS = 4  # random runs on more seeds than the model
MODEL_SEEDS = 3
CALLS_PER_SEED = 2 * (ROUNDS - 1)  # a tool call and a final answer in each decided round


def final(picks, note="ok"):
    return {"action": "final", "picks": picks, "round_note": note}


def scripted_replies(assay, seeds, rounds=ROUNDS) -> list:
    """For each seed in turn: per decided round, a results call, then 8 conditions not yet tested."""
    names = list(assay.factors)
    replies: list = []
    for seed in seeds:
        tested = {key(p.conditions, names) for p in round_one(assay, 8, seed).picks}
        pool = [c for c in grid_conditions(assay) if key(c, names) not in tested]
        pool = pool[seed * 37 :]  # a different start on each seed, so the transcripts differ
        for _ in range(2, rounds + 1):
            picks, pool = pool[:8], pool[8:]
            replies += [
                {"action": "tool", "tool": "results", "args": {"top_k": 3}},
                final(
                    [
                        {"conditions": c, "source": "own", "reason": "spread the search"}
                        for c in picks
                    ]
                ),
            ]
    return replies


def factory_for(assay, client):
    return lambda transcript: ModelDecider(assay, client, transcript=transcript)


def bench(assay, run_dir, client, **kw):
    args = dict(
        seeds=SEEDS,
        model_seeds=MODEL_SEEDS,
        rounds=ROUNDS,
        model_factory=factory_for(assay, client),
        run_dir=run_dir,
        command="labauto benchmark --strategies random,model --seeds 4 --model-seeds 3 --rounds 3",
        model_name="scripted",
    )
    args.update(kw)
    return run_benchmark(assay, ["random", "model"], **args)


@pytest.fixture
def first_run(tmp_path, assay):
    run_dir = tmp_path / "run"
    client = ScriptedClient(scripted_replies(assay, range(1, MODEL_SEEDS + 1)))
    result = bench(assay, run_dir, client)
    return run_dir, result, client


def page(result, tmp_path, name="benchmark.md") -> str:
    md = tmp_path / "page" / name
    write_outputs(
        result, ROUNDS, result["command"], md, tmp_path / "page" / "b.png", "scripted", "abc"
    )
    return md.read_text(encoding="utf-8")


def test_model_on_three_seeds_writes_one_transcript_per_seed(first_run):
    run_dir, result, client = first_run
    files = sorted(p.name for p in (run_dir / "model").glob("*.jsonl"))
    assert files == ["seed_01.jsonl", "seed_02.jsonl", "seed_03.jsonl"]
    picks_by_seed = []
    for name in files:
        records = Transcript(run_dir / "model" / name).records
        assert len(records) == CALLS_PER_SEED
        assert [r["scope"] for r in records] == [
            "decider-r02",
            "decider-r02",
            "decider-r03",
            "decider-r03",
        ]
        picks_by_seed.append(json.dumps(records[1]["parsed"]["picks"]))
    assert len(set(picks_by_seed)) == MODEL_SEEDS  # each seed's file holds that seed's replies
    assert client.queues["*"] == []  # every scripted reply was asked for once
    assert result["fallbacks"]["model"] == (0, MODEL_SEEDS * (ROUNDS - 1))
    calls = result["model_calls"]
    assert calls["fresh"] == MODEL_SEEDS * CALLS_PER_SEED and calls["replayed"] == 0
    assert calls["by_seed"] == {s: {"fresh": CALLS_PER_SEED, "replayed": 0} for s in (1, 2, 3)}
    assert set(result["scores"]["model"]) == {1, 2, 3}
    assert set(result["scores"]["random"]) == {1, 2, 3, 4}
    assert (run_dir / "result.json").exists()


def test_rerun_with_the_same_folder_replays_every_call(first_run, tmp_path, assay):
    run_dir, first, _ = first_run
    files = {p.name: p.read_text() for p in (run_dir / "model").glob("*.jsonl")}
    empty = ScriptedClient([])  # any call sent to it raises ScriptExhausted
    second = bench(assay, run_dir, empty)
    assert empty.calls == []
    assert second["scores"] == first["scores"]
    assert second["model_calls"]["fresh"] == 0
    assert second["model_calls"]["replayed"] == MODEL_SEEDS * CALLS_PER_SEED
    assert {p.name: p.read_text() for p in (run_dir / "model").glob("*.jsonl")} == files

    first_text = page(first, tmp_path, "first.md")
    assert (
        "Model calls: 12 made in this run, 0 replayed from an earlier run in that folder."
        in first_text
    )
    assert "were replayed, not sent" not in first_text
    text = page(second, tmp_path, "second.md")
    assert (
        "Model calls: 0 made in this run, 12 replayed from an earlier run in that folder." in text
    )
    assert "12 of the 12 model calls were replayed, not sent to the model in this run." in text


def test_resume_makes_fresh_calls_only_where_none_is_recorded(first_run, assay):
    run_dir, first, _ = first_run
    (run_dir / "model" / "seed_02.jsonl").unlink()
    client = ScriptedClient(scripted_replies(assay, [2]))
    second = bench(assay, run_dir, client)
    assert client.queues["*"] == []
    assert second["model_calls"]["by_seed"] == {
        1: {"fresh": 0, "replayed": CALLS_PER_SEED},
        2: {"fresh": CALLS_PER_SEED, "replayed": 0},
        3: {"fresh": 0, "replayed": CALLS_PER_SEED},
    }
    assert second["scores"] == first["scores"]


def test_a_folder_made_for_another_surface_is_refused(first_run, assay):
    run_dir, _, _ = first_run
    client = ScriptedClient([])
    with pytest.raises(BenchmarkError, match="surface_seed"):
        bench(assay, run_dir, client, surface_seed=0)
    with pytest.raises(BenchmarkError, match="model"):
        bench(assay, run_dir, client, model_name="another-model")
    assert client.calls == []


def test_same_seed_tables_match_a_hand_computation(first_run, tmp_path):
    run_dir, result, _ = first_run
    text = page(result, tmp_path)
    main_part, section = text.split("## On the model's seeds", 1)
    assert "| random | 4 |" in main_part and "| model | 3 |" in main_part
    assert 'See "On the model\'s seeds" below.' in main_part
    assert "compare the model with the others here and not in the tables above" in section
    yield_part, share_part = section.split("### Share of seeds within 3 points of the optimum", 1)
    assert "### Mean true yield of the recommendation (%)" in yield_part

    raw = json.loads((run_dir / "result.json").read_text())
    seeds = ["1", "2", "3"]
    for strategy in ("random", "model"):
        scores = raw["scores"][strategy]
        means = [sum(scores[s][r] for s in seeds) / len(seeds) for r in range(ROUNDS)]
        shares = [
            sum(scores[s][r] >= raw["optimum"][s] - 3 for s in seeds) / len(seeds)
            for r in range(ROUNDS)
        ]
        assert f"| {strategy} | 3 | " + " | ".join(f"{m:.1f}" for m in means) + " |" in yield_part
        assert f"| {strategy} | 3 | " + " | ".join(f"{x:.2f}" for x in shares) + " |" in share_part
    optimum = sum(raw["optimum"][s] for s in seeds) / len(seeds)
    assert f"Mean true optimum over these 3 surfaces: {optimum:.1f}%." in section
    assert "Mean true optimum over the 4 surfaces:" in main_part


def test_no_same_seed_section_when_the_model_ran_on_every_seed(tmp_path):
    result = {
        "scores": {
            "random": {1: [50.0], 2: [60.0], 3: [70.0]},
            "model": {1: [55.0], 2: [65.0], 3: [75.0]},
        },
        "optimum": {1: 80.0, 2: 80.0, 3: 80.0},
        "seconds": {"random": 1.0, "model": 2.0},
        "skipped": {},
        "fallbacks": {"model": (0, 0)},
        "surface_seed": None,
    }
    md = tmp_path / "b.md"
    write_outputs(result, 1, "cmd", md, tmp_path / "b.png", "scripted", "abc")
    text = md.read_text()
    assert "On the model's seeds" not in text and "Model calls" not in text


def test_result_json_round_trips(first_run, tmp_path):
    run_dir, result, _ = first_run
    raw = json.loads((run_dir / "result.json").read_text())
    for name in (
        "scores",
        "optimum",
        "seconds",
        "skipped",
        "fallbacks",
        "surface_seed",
        "rounds",
        "seeds",
        "model_seeds",
        "command",
        "model_name",
        "model_calls",
        "run_dir",
    ):
        assert name in raw
    assert set(raw["scores"]["model"]) == {"1", "2", "3"}  # JSON keys are text
    loaded = load_result(run_dir / "result.json")
    assert loaded == result
    assert set(loaded["scores"]["model"]) == {1, 2, 3} and set(loaded["optimum"]) == {1, 2, 3, 4}
    assert page(loaded, tmp_path) == page(result, tmp_path)


def test_page_names_the_run_folder_without_an_absolute_path(first_run, tmp_path):
    run_dir, result, _ = first_run
    text = page(result, tmp_path)
    shown = result["run_dir"]
    assert shown == shown_path(run_dir) and not Path(shown).is_absolute()
    assert f"`{shown}/model/`, one file per seed" in text
    stored = (run_dir / "result.json").read_text()
    for local in (str(ROOT), str(tmp_path), str(Path.home())):
        assert local not in text and local not in stored
    assert shown_path(Path("/elsewhere/bench_run")) == "<outside the repository>/bench_run"


def test_plot_labels_name_each_line_seed_count(first_run):
    _, result, _ = first_run
    labels = line_labels(summarize(result, ROUNDS))
    assert labels == {"random": "random (4 seeds)", "model": "model (3 seeds)"}


def test_shared_seed_tables_when_the_model_ran_on_more_seeds(tmp_path):
    result = {
        "scores": {
            "random": {1: [50.0, 60.0], 2: [70.0, 71.0]},
            "model": {1: [55.0, 79.0], 2: [65.0, 75.0], 3: [40.0, 90.0]},
        },
        "optimum": {1: 80.0, 2: 80.0, 3: 90.0},
        "seconds": {"random": 1.0, "model": 2.0},
        "skipped": {},
        "fallbacks": {"model": (0, 3)},
        "surface_seed": None,
    }
    md = tmp_path / "b.md"
    write_outputs(result, 2, "cmd", md, tmp_path / "b.png", "scripted", "abc")
    main_part, section = md.read_text().split("## On the shared seeds", 1)
    assert (
        "| random | 2 | 60.0 | 65.5 |" in main_part and "| model | 3 | 53.3 | 81.3 |" in main_part
    )
    assert (
        "The model ran on 3 seeds and the others on 2, so the tables above do not compare them "
        'on the same seeds. See "On the shared seeds" below.'
    ) in main_part
    assert "On the model's seeds" not in main_part
    assert "seeds 1 to 2, the only seeds the others ran on" in section
    # Seeds 1 and 2 only: model means (55+65)/2 and (79+75)/2; within 3 points of 80 in round 2.
    assert "| random | 2 | 60.0 | 65.5 |" in section and "| model | 2 | 60.0 | 77.0 |" in section
    assert "| random | 2 | 0.00 | 0.00 |" in section and "| model | 2 | 0.00 | 0.50 |" in section
    assert "Mean true optimum over these 2 surfaces: 80.0%." in section


def test_model_seeds_is_zero_when_the_model_did_not_run(assay):
    alone = run_benchmark(assay, ["random"], seeds=1, rounds=2)
    skipped = run_benchmark(assay, ["random", "model"], seeds=1, rounds=2)
    assert alone["model_seeds"] == 0 and skipped["model_seeds"] == 0
    assert "model" in skipped["skipped"] and "model" not in skipped["scores"]


def test_first_run_records_the_decider_prompt_hash(first_run):
    run_dir, result, _ = first_run
    settings = json.loads((run_dir / "model" / "settings.json").read_text())
    assert settings["decider_prompt_sha256"] == decider_prompt_sha256()
    assert result["model_seeds"] == MODEL_SEEDS


def test_a_changed_decider_prompt_is_refused(first_run, assay, monkeypatch):
    import labauto.decide.model_decider as decider_mod

    run_dir, _, _ = first_run
    saved = (run_dir / "result.json").read_text()
    monkeypatch.setattr(decider_mod, "load_prompt", lambda name: "Another decider prompt.")
    client = ScriptedClient([])
    with pytest.raises(BenchmarkError, match="decider_prompt_sha256"):
        bench(assay, run_dir, client)
    assert client.calls == [] and (run_dir / "result.json").read_text() == saved


def test_a_reply_recorded_for_other_messages_is_refused(first_run, assay):
    run_dir, _, _ = first_run
    saved = (run_dir / "result.json").read_text()
    path = run_dir / "model" / "seed_02.jsonl"
    lines = path.read_text().splitlines()
    rec = json.loads(lines[2])  # round 3, turn 1: asked with round 2's results in the digest
    assert rec["scope"] == "decider-r03" and rec["messages"][1]["role"] == "user"
    rec["messages"][1]["content"] = rec["messages"][1]["content"].replace("ROUND 3", "ROUND 9")
    lines[2] = json.dumps(rec)
    path.write_text("\n".join(lines) + "\n")
    client = ScriptedClient([])
    with pytest.raises(BenchmarkError, match="asked with other messages") as caught:
        bench(assay, run_dir, client)
    assert "seed_02.jsonl" in str(caught.value) and "decider-r03 turn 1 call 1" in str(caught.value)
    assert client.calls == [] and (run_dir / "result.json").read_text() == saved


def test_a_folder_with_recorded_calls_is_not_run_without_the_model(first_run, assay):
    run_dir, _, _ = first_run
    saved = (run_dir / "result.json").read_text()
    with pytest.raises(BenchmarkError, match="holds recorded model calls"):
        bench(assay, run_dir, ScriptedClient([]), model_factory=None)
    assert (run_dir / "result.json").read_text() == saved


# ---------------------------------------------------------------- command line


@pytest.fixture
def fake_run(monkeypatch):
    """Stand in for run_benchmark and write_outputs, so the command runs no campaign."""
    import labauto.benchmark as bench_mod

    seen: dict = {}

    def fake(assay, strategies, **kw):
        seen.update(kw, strategies=strategies)
        if seen.get("raise"):
            raise seen["raise"]
        return {"scores": {}, "optimum": {}, "seconds": {}, "skipped": {}, "fallbacks": {}}

    monkeypatch.setattr(bench_mod, "run_benchmark", fake)
    monkeypatch.setattr(bench_mod, "write_outputs", lambda *a: seen.update(written=a))
    return seen


def test_benchmark_prints_a_new_run_folder_by_default(fake_run, capsys):
    assert main(["benchmark", "--strategies", "random"]) == 0
    run_dir = fake_run["run_dir"]
    assert re.fullmatch(r"runs/benchmark/\d{8}T\d{6}Z", run_dir.as_posix())
    assert not run_dir.exists()  # nothing is written by the command itself
    out = capsys.readouterr().out
    assert f"Run folder: {run_dir}" in out and f"--run-dir {run_dir}" in out
    assert "--run-dir" not in fake_run["command"]


def test_benchmark_resumes_a_given_run_folder(fake_run, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("LABAUTO_MAIN_BASE_URL", "http://endpoint.invalid/v1")  # never called
    run_dir = tmp_path / "bench"
    run_dir.mkdir()
    args = ["benchmark", "--strategies", "random,model", "--run-dir", str(run_dir)]
    assert main(args) == 0
    assert fake_run["run_dir"] == run_dir
    assert "this run resumes it" in capsys.readouterr().out
    assert fake_run["command"].endswith(f"--run-dir {shown_path(run_dir)}")
    assert str(tmp_path) not in fake_run["command"]


def test_benchmark_says_how_to_go_on_when_the_endpoint_stops(fake_run, tmp_path, capsys):
    fake_run["raise"] = EndpointUnavailable("local-main did not answer")
    run_dir = tmp_path / "bench"
    assert main(["benchmark", "--strategies", "random", "--run-dir", str(run_dir)]) == 2
    out = capsys.readouterr().out
    assert "local-main did not answer" in out and f"--run-dir {run_dir}" in out
    assert "written" not in fake_run


def test_benchmark_without_an_endpoint_keeps_a_folder_with_recorded_calls(
    first_run, tmp_path, capsys, monkeypatch
):
    import labauto.cli_plan as cli_plan

    def no_endpoint(spec, settings):
        raise cli_plan.ConfigError("no endpoint")

    monkeypatch.setattr(cli_plan, "make_clients", no_endpoint)
    run_dir, _, _ = first_run
    saved = (run_dir / "result.json").read_text()
    page_md, page_png = tmp_path / "out" / "b.md", tmp_path / "out" / "b.png"
    args = ["benchmark", "--strategies", "random,model", "--seeds", "1", "--rounds", "3"]
    args += ["--run-dir", str(run_dir), "--out", str(page_md), "--png", str(page_png)]
    assert main(args) == 2
    assert "holds recorded model calls" in capsys.readouterr().out
    assert (run_dir / "result.json").read_text() == saved and not page_md.exists()


def test_benchmark_builds_one_model_decider_per_transcript(fake_run, tmp_path, monkeypatch):
    monkeypatch.setenv("LABAUTO_MAIN_BASE_URL", "http://endpoint.invalid/v1")
    monkeypatch.setenv("LABAUTO_MAIN_MODEL", "model-from-env")
    assert main(["benchmark", "--strategies", "model", "--run-dir", str(tmp_path / "b")]) == 0
    assert fake_run["model_name"] == "model-from-env"
    first, second = Transcript(None), Transcript(None)
    a, b = fake_run["model_factory"](first), fake_run["model_factory"](second)
    assert isinstance(a, ModelDecider) and a.transcript is first and b.transcript is second
