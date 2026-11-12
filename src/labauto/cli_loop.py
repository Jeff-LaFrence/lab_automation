"""Closed loop commands: loop, report, benchmark, and the demo's loop part."""

from __future__ import annotations

import argparse
from pathlib import Path

from labauto.config import DEFAULT_ASSAY, DEFAULT_WORKCELL, load_assay, load_workcell, resolve
from labauto.record import portable

EXIT_DONE, EXIT_WAITING, EXIT_FAILED = 0, 1, 2


def is_campaign(path: str | Path) -> bool:
    return (Path(path) / "campaign.json").exists()


def _exit(state: str) -> int:
    return {"DONE": EXIT_DONE, "AWAITING_APPROVAL": EXIT_WAITING, "NEEDS_HUMAN": EXIT_WAITING}.get(
        state, EXIT_FAILED
    )


def cmd_loop(args: argparse.Namespace) -> int:
    from labauto.campaign import campaign_status, load_cstate, new_campaign_id, start_campaign
    from labauto.cli_plan import ConfigError, make_clients, settings_for
    from labauto.models import CampaignConfig

    assay = load_assay(args.assay)
    workcell = load_workcell(args.workcell)
    clients = None
    if args.strategy == "model":
        try:
            settings = settings_for(args.client)
            clients = make_clients(args.client, settings)
        except ConfigError as exc:
            print(f"error: {exc}")
            print(
                "The model decider needs a client. Live results are pending until an endpoint is configured."
            )
            return EXIT_FAILED
    faults = ["injection_failure"] + list(args.fault or [])
    try:
        cfg = CampaignConfig(
            campaign_id=args.campaign_id or new_campaign_id(args.strategy),
            brief=args.brief or "",
            assay_id=assay.assay_id,
            workcell_id=workcell.workcell_id,
            strategy=args.strategy,
            rounds=args.rounds,
            conditions_per_round=args.conditions,
            replicates=args.replicates,
            seed=args.seed,
            surface_seed=args.surface_seed,
            approval_policy=args.approval,
            faults=faults,
            assay=portable(args.assay),
            workcell=portable(args.workcell),
            client=clients.spec if clients is not None else "",
        )
    except ValueError as exc:
        print(
            f"error: {str(exc).splitlines()[-1] if 'unknown fault' not in str(exc) else _first_reason(exc)}"
        )
        return EXIT_FAILED
    cdir = start_campaign(cfg, clients, None, args.campaigns)
    print(campaign_status(cdir))
    return _exit(load_cstate(cdir).state)


def _first_reason(exc: Exception) -> str:
    for line in str(exc).splitlines():
        if "unknown fault" in line:
            return line.strip().removeprefix("Value error, ")
    return str(exc).splitlines()[0]


def resume_campaign_cmd(path: Path, client: str | None) -> int:
    from labauto.campaign import advance_campaign, campaign_status, load_config
    from labauto.cli_plan import ConfigError, make_clients, settings_for

    cfg = load_config(path)
    clients = None
    if cfg.strategy == "model":
        spec = client or cfg.client or "live"
        try:
            clients = make_clients(spec, settings_for(spec))
        except ConfigError as exc:
            print(f"error: {exc}")
            return EXIT_FAILED
        _skip_recorded(path, clients)
    st = advance_campaign(path, clients, None)
    print(campaign_status(path))
    return _exit(st.state)


def _skip_recorded(cdir: Path, clients) -> None:
    """Replay and scripted clients serve replies in order: move past the ones already recorded."""
    from labauto.agents.llm import Transcript

    if not hasattr(clients.local, "skip"):
        return
    for t in sorted((cdir / "rounds").glob("*/transcript.jsonl")):
        clients.local.skip(len(Transcript(t).records))


def approve_campaign_cmd(path: Path, by: str | None, comment: str) -> int:
    from labauto.campaign import CampaignError, approve_waiting_round

    try:
        approval = approve_waiting_round(path, by=by, comment=comment)
    except CampaignError as exc:
        print(f"Not approved: {exc}")
        return EXIT_WAITING
    print(f"Approved by {approval.by}. Continue with: labauto resume {path}")
    return EXIT_DONE


def cmd_report(args: argparse.Namespace) -> int:
    from labauto.report import build_report, model_note_writer

    writer = None
    if args.note:
        from labauto.cli_plan import ConfigError, make_clients, settings_for

        try:
            writer = model_note_writer(
                make_clients(args.client, settings_for(args.client)), Path(args.campaign)
            )
        except ConfigError as exc:
            print(f"error: {exc}")
            return EXIT_FAILED
    print(f"Wrote {build_report(Path(args.campaign), note_writer=writer)}")
    return EXIT_DONE


def cmd_benchmark(args: argparse.Namespace) -> int:
    from labauto.agents.llm import EndpointUnavailable
    from labauto.benchmark import BenchmarkError, run_benchmark, shown_path, write_outputs
    from labauto.cli_plan import ConfigError, make_clients
    from labauto.config import load_settings
    from labauto.planner_eval import git_commit
    from labauto.record import utc_stamp

    assay = load_assay(DEFAULT_ASSAY)
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    model_factory = None
    model_name = None
    if "model" in strategies:
        settings = load_settings()
        try:
            clients = make_clients("live", settings)
        except ConfigError:
            print(
                "No model endpoint is configured, so the model strategy is skipped. Live results are pending."
            )
        else:
            from labauto.decide.model_decider import ModelDecider

            model_name = settings.main.model if settings.main else None

            def model_factory(transcript):
                return ModelDecider(assay, clients.local, transcript=transcript)

    run_dir = Path(args.run_dir) if args.run_dir else Path("runs") / "benchmark" / utc_stamp()
    print(f"Run folder: {run_dir}")
    if run_dir.exists() and model_factory is not None:
        print(
            "That folder exists, so this run resumes it: model calls recorded there are replayed,"
            " and only the rest go to the model."
        )
    print(f"To resume after a stop, run the same command with --run-dir {run_dir}")
    print(
        "Benchmark on the fast path: plan volumes stand in for the ledger; compile and simulate are skipped."
    )
    command = "labauto benchmark " + " ".join(
        [
            f"--strategies {args.strategies}",
            f"--seeds {args.seeds}",
            f"--model-seeds {args.model_seeds}",
            f"--rounds {args.rounds}",
        ]
        + ([f"--surface-seed {args.surface_seed}"] if args.surface_seed is not None else [])
        # Paths go through shown_path: the page and result.json never carry a local folder.
        + ([f"--out {shown_path(args.out)}"] if args.out != "docs/benchmark.md" else [])
        + ([f"--png {shown_path(args.png)}"] if args.png != "docs/img/benchmark.png" else [])
        + ([f"--run-dir {shown_path(run_dir)}"] if args.run_dir else [])
    )
    try:
        result = run_benchmark(
            assay,
            strategies,
            seeds=args.seeds,
            model_seeds=args.model_seeds,
            rounds=args.rounds,
            model_factory=model_factory,
            progress=print if args.verbose else None,
            surface_seed=args.surface_seed,
            run_dir=run_dir,
            command=command,
            model_name=model_name,
        )
    except BenchmarkError as exc:
        print(f"error: {exc}")
        return EXIT_FAILED
    except EndpointUnavailable as exc:
        print(f"error: {exc}")
        print(f"The model calls made so far are saved in {run_dir / 'model'}.")
        print(f"To go on from there, run the same command with --run-dir {run_dir}")
        return EXIT_FAILED
    write_outputs(
        result, args.rounds, command, Path(args.out), Path(args.png), model_name, git_commit()
    )
    print(f"Wrote {args.out} and {args.png}, from {run_dir / 'result.json'}")
    return EXIT_DONE


# ---------------------------------------------------------------- demo


def demo_loop(runs: str) -> bool:
    """3 rounds with the optimizer and auto_demo approval, full path. Then the model decider from a recording."""
    from labauto.campaign import load_config, load_cstate, new_campaign_id, start_campaign
    from labauto.cli_plan import recording_for
    from labauto.models import CampaignConfig

    assay = load_assay(DEFAULT_ASSAY)
    workcell = load_workcell(DEFAULT_WORKCELL)
    base = Path(runs) / "campaigns"
    print()
    print("Stage 3: closed loop, full path (compile, simulate, ledger check, simulated LC/MS)")
    print()
    ok = True
    runs_to_show = [("optimizer", None, 3)]
    rec, source = recording_for("decider")
    if rec is not None:
        runs_to_show.append(("model", rec, 3))
    for strategy, recording, rounds in runs_to_show:
        clients = None
        if recording is not None:
            from labauto.agents.llm import ReplayClient
            from labauto.orchestrator import Clients

            clients = Clients(local=ReplayClient(recording), spec=f"replay:{recording.name}")
        cfg = CampaignConfig(
            campaign_id=new_campaign_id(strategy),
            brief="Find the condition with the highest yield.",
            assay_id=assay.assay_id,
            workcell_id=workcell.workcell_id,
            strategy=strategy,
            rounds=rounds,
            approval_policy="auto_demo",
            seed=1,
            surface_seed=0,
            assay=DEFAULT_ASSAY,
            workcell=DEFAULT_WORKCELL,
            client=f"replay:{portable(recording)}" if recording else "",
        )
        cdir = start_campaign(cfg, clients, None, base)
        st = load_cstate(cdir)
        label = strategy if recording is None else f"model decider, {source}"
        print(
            f"{label}: {st.state} after {load_config(cdir).rounds} rounds. Every round approved by demo_auto (no person)."
        )
        from labauto.report import build_report

        report = build_report(cdir).read_text(encoding="utf-8")
        table = report.split("## Rounds", 1)[1].split("![Progress]", 1)[0].strip()
        print(table)
        rec_line = report.split("## Recommendation", 1)[1].strip().splitlines()[0]
        print(rec_line)
        print(f"Report: {cdir / 'report.md'}")
        print()
        ok = ok and st.state == "DONE"
    if rec is None:
        print("Model decider: no recording yet; live results pending.")
    return ok


def add_parsers(sub) -> None:
    p = sub.add_parser("loop", help="run a closed loop campaign")
    p.add_argument("--strategy", choices=["random", "optimizer", "model"], required=True)
    p.add_argument("--rounds", type=int, default=6)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--surface-seed", type=int, default=0)
    p.add_argument("--approval", choices=["always", "envelope", "auto_demo"], default="always")
    p.add_argument("--fault", action="append", help="for example plate_drift@3")
    p.add_argument(
        "--client", default="live", help="live, replay:<file>, or scripted:<file> (model strategy)"
    )
    p.add_argument("--brief", default="")
    p.add_argument("--conditions", type=int, default=8, help="conditions per round")
    p.add_argument("--replicates", type=int, default=3)
    p.add_argument("--assay", default=DEFAULT_ASSAY)
    p.add_argument("--workcell", default=DEFAULT_WORKCELL)
    p.add_argument("--campaigns", default="campaigns")
    p.add_argument("--campaign-id", default=None)
    p.set_defaults(func=cmd_loop)

    p = sub.add_parser("report", help="rebuild a campaign's report")
    p.add_argument("campaign")
    p.add_argument("--note", action="store_true", help="ask a model for a short note on the tables")
    p.add_argument("--client", default="live", help="live, replay:<file>, or scripted:<file>")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("benchmark", help="compare strategies over many seeds (fast path)")
    p.add_argument("--strategies", default="random,optimizer,model")
    p.add_argument("--seeds", type=int, default=30)
    p.add_argument("--model-seeds", type=int, default=3)
    p.add_argument("--rounds", type=int, default=6)
    p.add_argument(
        "--surface-seed", type=int, default=None, help="hold the surface fixed (Appendix C check)"
    )
    p.add_argument("--out", default="docs/benchmark.md")
    p.add_argument("--png", default="docs/img/benchmark.png")
    p.add_argument(
        "--run-dir",
        default=None,
        help="folder for result.json and the model's calls (default runs/benchmark/<UTC time>); "
        "an existing folder is resumed",
    )
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_benchmark)


def resolve_path(p: str) -> Path:
    return resolve(p)
