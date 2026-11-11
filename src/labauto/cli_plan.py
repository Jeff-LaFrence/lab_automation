"""Planner commands: plan, status, approve, reject, resume, eval-planner, and the demo's planner part."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from labauto.agents.llm import HostedClient, LiveClient, ReplayClient, ScriptedClient
from labauto.approval import ApprovalError, approve, reject, status_text, verify
from labauto.config import (
    DEFAULT_ASSAY,
    DEFAULT_WORKCELL,
    Endpoint,
    Settings,
    load_settings,
    resolve,
)
from labauto.orchestrator import RESTING, Clients, load_state, resume, run_summary_dict, start_run
from labauto.record import portable

EXIT_OK, EXIT_HUMAN, EXIT_INTERNAL = 0, 1, 2


def is_campaign(path: str | Path) -> bool:
    """A campaign folder holds campaign.json. A planned run holds request.txt."""
    return (Path(path) / "campaign.json").exists()


DEMO_SCENARIOS = ["01_simple_factorial", "02_too_big", "03_out_of_range"]
NO_ENDPOINT = (
    "No model endpoint is configured. Copy .env.example to .env and set LABAUTO_MAIN_BASE_URL, "
    "or use --client replay:<file> or --client scripted:<file>."
)


class ConfigError(RuntimeError):
    """The command needs something that is not configured."""


def make_clients(spec: str, settings: Settings) -> Clients:
    """Build the chat clients from a --client value: live, replay:<file>, or scripted:<file>."""
    if spec == "live":
        if settings.main is None:
            raise ConfigError(NO_ENDPOINT)
        endpoints = {"local-main": settings.main}
        if settings.small is not None:
            endpoints["local-small"] = settings.small
        hosted = HostedClient(settings.hosted) if settings.hosted is not None else None
        return Clients(local=LiveClient(endpoints), hosted=hosted, spec=spec)
    kind, _, path = spec.partition(":")
    if kind == "replay" and path:
        client = ReplayClient(resolve(path))
    elif kind == "scripted" and path:
        client = ScriptedClient.from_file(resolve(path))
    else:
        raise ConfigError(f"--client must be live, replay:<file>, or scripted:<file>, not '{spec}'")
    return Clients(local=client, hosted=client, spec=f"{kind}:{portable(resolve(path))}")


def settings_for(spec: str) -> Settings:
    """Live runs read .env. Replayed and scripted runs use no real endpoints, so they repeat exactly.

    If a recording (or a scripted file keyed by label) holds hosted replies, the
    hosted rung is switched on with a placeholder endpoint, so the replay walks
    the same ladder the session did.
    """
    if spec == "live":
        return load_settings()
    kind, _, path = spec.partition(":")
    hosted_calls = 0
    try:
        if kind == "replay":
            for line in resolve(path).read_text(encoding="utf-8").splitlines():
                if line.strip() and json.loads(line).get("rung") == "hosted":
                    hosted_calls += 1
        elif kind == "scripted":
            data = json.loads(resolve(path).read_text(encoding="utf-8"))
            if isinstance(data, dict):
                hosted_calls = len(data.get("hosted", []))
    except (OSError, ValueError):
        return Settings()
    if not hosted_calls:
        return Settings()
    return Settings(
        hosted=Endpoint(label="hosted", provider=kind, model=kind),
        hosted_max_calls=max(2, hosted_calls),
    )


def _exit_for(state: str) -> int:
    if state in ("AWAITING_APPROVAL", "APPROVED"):
        return EXIT_OK
    if state in ("NEEDS_HUMAN", "REJECTED"):
        return EXIT_HUMAN
    return EXIT_INTERNAL


def cmd_plan(args: argparse.Namespace) -> int:
    if args.request_file:
        request = resolve(args.request_file).read_text(encoding="utf-8")
    elif args.request:
        request = args.request
    else:
        print("error: give a request in quotes or --request-file F")
        return EXIT_INTERNAL
    try:
        settings = settings_for(args.client)
        clients = make_clients(args.client, settings)
    except ConfigError as exc:
        print(f"error: {exc}")
        return EXIT_INTERNAL
    run_dir = start_run(request, args.assay, args.workcell, clients, settings, args.runs, args.seed)
    print(status_text(run_dir))
    return _exit_for(load_state(run_dir).state)


def cmd_status(args: argparse.Namespace) -> int:
    run_dir = Path(args.run)
    if is_campaign(run_dir):
        from labauto.campaign import campaign_status

        print(campaign_status(run_dir))
        return EXIT_OK
    if load_state(run_dir).state == "APPROVED":
        verify(run_dir)
        if load_state(run_dir).state != "APPROVED":
            print("The approval was voided: the files changed after approval.")
    print(status_text(run_dir))
    return EXIT_OK


def cmd_approve(args: argparse.Namespace) -> int:
    if is_campaign(args.run):
        from labauto.cli_loop import approve_campaign_cmd

        return approve_campaign_cmd(Path(args.run), args.by, args.comment or "")
    try:
        approval = approve(Path(args.run), by=args.by, comment=args.comment or "")
    except ApprovalError as exc:
        print(f"Not approved: {exc}")
        return EXIT_HUMAN
    print(f"Approved by {approval.by}. Design hash {approval.design_hash}")
    print(f"Protocol hash {approval.protocol_hash}")
    return EXIT_OK


def cmd_reject(args: argparse.Namespace) -> int:
    try:
        reject(Path(args.run), args.reason, by=args.by)
    except ApprovalError as exc:
        print(f"Not rejected: {exc}")
        return EXIT_HUMAN
    print(f"Rejected: {args.reason}")
    return EXIT_OK


def cmd_resume(args: argparse.Namespace) -> int:
    run_dir = Path(args.run)
    if is_campaign(run_dir):
        from labauto.cli_loop import resume_campaign_cmd

        return resume_campaign_cmd(run_dir, args.client)
    st = load_state(run_dir)
    if st.state in RESTING:
        resume(run_dir, Clients(local=None), Settings())  # rewrites summary.md if a crash lost it
        print(status_text(run_dir))
        return _exit_for(st.state)
    spec = args.client or st.client or "live"
    try:
        settings = settings_for(spec)
        clients = make_clients(spec, settings)
    except ConfigError as exc:
        print(f"error: {exc}")
        return EXIT_INTERNAL
    st = resume(run_dir, clients, settings)
    print(status_text(run_dir))
    return _exit_for(st.state)


def cmd_eval(args: argparse.Namespace) -> int:
    from labauto.planner_eval import NoModelAnswered, evaluate

    settings = load_settings()
    try:
        make_clients("live", settings)
    except ConfigError as exc:
        print(f"error: {exc}")
        print(
            "docs/planner_eval.md was not written. The README status table says live results are pending."
        )
        return EXIT_HUMAN
    try:
        path = evaluate(
            lambda: make_clients("live", settings),
            settings,
            trials=args.trials,
            runs_dir=args.runs,
            out_path=Path(args.out),
        )
    except NoModelAnswered as exc:
        print(f"error: {exc}")
        print(f"{args.out} was not written.")
        return EXIT_HUMAN
    print(f"Wrote {path}")
    return EXIT_OK


# ---------------------------------------------------------------- demo


SCRIPTED_LABEL = "scripted session (hand-written replies, not a model)"


def recording_source(path: Path) -> str:
    """Say what a recording holds, from its records, never from its file name."""
    models = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            models.add(json.loads(line).get("model") or "unknown")
    if not models or "scripted" in models or "unknown" in models:
        return SCRIPTED_LABEL
    return "recorded session of " + ", ".join(sorted(models))


def recording_for(scenario: str) -> tuple[Path | None, str]:
    """A recording of a model session if one exists, else the scripted session's recording."""
    folder = resolve("examples/recordings")
    for path in (folder / f"{scenario}.jsonl", folder / f"{scenario}.scripted.jsonl"):
        if path.exists():
            return path, recording_source(path)
    return None, "no recording"


def planner_row(name: str, state: str, attempts: str, detail: str) -> str:
    return f"{name:<29}{state:<20}{attempts:<10}{detail}".rstrip()


def demo_planner(runs: str, live: bool = False) -> bool:
    """Run scenarios 01 to 03 from recordings, or live. Returns True if each rests as expected."""
    print()
    if live:
        print("Stage 2: planner scenarios, live against the configured endpoint")
        settings = load_settings()
        try:
            make_clients("live", settings)
        except ConfigError as exc:
            print(f"  {exc}")
            return False
    else:
        print("Stage 2: planner scenarios, replayed from recorded sessions")
        settings = Settings()
    print()
    print(planner_row("scenario", "state", "attempts", "detail"))
    all_ok = True
    sources = {}
    for scenario in DEMO_SCENARIOS:
        request = resolve(f"examples/requests/{scenario}.txt").read_text(encoding="utf-8")
        if live:
            clients = make_clients("live", settings)
        else:
            path, source = recording_for(scenario)
            if path is None:
                print(planner_row(scenario, "SKIPPED", "", "(no recording)"))
                continue
            sources[scenario] = source
            spec = f"replay:{portable(path)}"
            clients = make_clients(spec, Settings())
            settings = settings_for(spec)
        run_dir = start_run(request, DEFAULT_ASSAY, DEFAULT_WORKCELL, clients, settings, runs)
        d = run_summary_dict(run_dir)
        repaired = [c for codes in d["codes"][:-1] for c in codes]
        if d["state"] == "AWAITING_APPROVAL":
            detail = f"{d['wells']} wells"
            if repaired:
                detail += f", repaired {', '.join(dict.fromkeys(repaired))}"
            n = d["deviations"]
            detail += f", {n} deviation{'s' if n != 1 else ''} declared" if n else ", no deviations"
        elif d["state"] == "NEEDS_HUMAN" and live:
            detail = d["needs_human"] or "see summary.md"
        else:
            detail = d["needs_human"] or "see summary.md"
            all_ok = False
        if scenario in sources and sources[scenario] == SCRIPTED_LABEL:
            detail += " (scripted)"
        print(planner_row(scenario, d["state"], str(d["attempts"]), detail))
    if sources:
        print()
        for scenario, source in sources.items():
            print(f"Source of {scenario}: {source}.")
    print("A person approves with labauto approve <run>. Nothing here was approved.")
    return all_ok


def add_parsers(sub) -> None:
    """Register the planner commands on the main parser."""
    p = sub.add_parser("plan", help="plan a request with the planner agent")
    p.add_argument("request", nargs="?", help="the request, in quotes")
    p.add_argument("--request-file", help="a file holding the request")
    p.add_argument("--client", default="live", help="live, replay:<file>, or scripted:<file>")
    p.add_argument("--assay", default=DEFAULT_ASSAY)
    p.add_argument("--workcell", default=DEFAULT_WORKCELL)
    p.add_argument("--runs", default="runs")
    p.add_argument("--seed", type=int, default=0, help="seed for the plate layout")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("status", help="show a run's state")
    p.add_argument("run")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("approve", help="approve a run that is awaiting approval")
    p.add_argument("run")
    p.add_argument("--by", default=None, help="who approves")
    p.add_argument("--comment", default="")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("reject", help="reject a run that is awaiting approval")
    p.add_argument("run")
    p.add_argument("--reason", required=True)
    p.add_argument("--by", default=None)
    p.set_defaults(func=cmd_reject)

    p = sub.add_parser("resume", help="continue a run from its saved state")
    p.add_argument("run")
    p.add_argument("--client", default=None, help="override the client the run started with")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("eval-planner", help="evaluate the planner on the live model")
    p.add_argument("--trials", type=int, default=5)
    p.add_argument("--runs", default="runs/eval")
    p.add_argument("--out", default="docs/planner_eval.md")
    p.set_defaults(func=cmd_eval)
