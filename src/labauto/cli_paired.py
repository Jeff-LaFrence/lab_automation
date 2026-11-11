"""Paired LC/MS and NMR commands: nmr (run the NMR readout on a campaign round and pair it with LC/MS).

labauto nmr <campaign> [--round N] [--fault NAME ...]

The command reads the round the same way the campaign loop does: the plan and
the simulator's ledger from check.json, and the LC/MS peak table from
raw/lcms_peaks.csv. It runs the simulated NMR on the same ledger, evaluates
every spectrum, pairs the two readouts, and writes rounds/NN/nmr/,
rounds/NN/review/evidence.json, and rounds/NN/paired.md. It changes no file
the campaign wrote. Data are synthetic.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

EXIT_OK, EXIT_ERROR = 0, 2
NMR_FAULTS = ("nmr_broad_lines", "nmr_phase_error", "nmr_impurity")


class RoundError(RuntimeError):
    """The round cannot be paired as asked."""


def parse_nmr_faults(names: list[str], round_number: int) -> list[str]:
    """Check fault names and tag each with the round it applies to.

    A fault given without @R applies to the round being read. A fault given
    with @R keeps its tag, and R must be that round: the command reads one
    round, so a fault for another round would never be applied.
    """
    out = []
    for name in names:
        base, sep, at = name.partition("@")
        if base not in NMR_FAULTS or (sep and not at.isdigit()):
            raise RoundError(
                f"unknown NMR fault '{name}': use {', '.join(NMR_FAULTS)}, each with an optional @R"
            )
        if sep and int(at) != round_number:
            raise RoundError(
                f"NMR fault '{name}' names round {int(at)}, but this command reads round "
                f"{round_number}. Use {base}@{round_number} or leave out @R."
            )
        out.append(f"{base}@{round_number}")
    return list(dict.fromkeys(out))


def latest_read_round(cdir: Path) -> int | None:
    """The highest round with an LC/MS peak table."""
    found = [
        int(p.parent.parent.name)
        for p in (cdir / "rounds").glob("*/raw/lcms_peaks.csv")
        if p.parent.parent.name.isdigit()
    ]
    return max(found) if found else None


def read_ledger_csv(path: Path):
    """ledger.csv back as ledger entries."""
    from labauto.models import LedgerEntry

    with open(path, encoding="utf-8") as fh:
        return [
            LedgerEntry(
                dest_labware=row["dest_labware"],
                dest_well=row["dest_well"],
                reagent=row["reagent"],
                volume_ul=float(row["volume_ul"]),
            )
            for row in csv.DictReader(fh)
        ]


def round_plan_and_ledger(rdir: Path):
    """The round's plan and as-run ledger, as stored by the campaign.

    check.json holds both (the loop reads them from there). ledger.csv is the
    second source. Neither is rebuilt from the plan: a ledger made from planned
    volumes would hide any difference between what was planned and what was run.
    """
    from labauto.campaign import load_round_check

    res = load_round_check(rdir)
    if res is None or res.plan is None:
        raise RoundError(f"{rdir.name}: no check.json with a plan")
    if res.sim is not None and res.sim.ledger:
        return res.plan, res.sim.ledger, "check.json"
    if (rdir / "ledger.csv").exists():
        return res.plan, read_ledger_csv(rdir / "ledger.csv"), "ledger.csv"
    raise RoundError(f"round {rdir.name} stores no ledger (no simulator ledger, no ledger.csv)")


def pair_campaign_round(cdir: Path, round_number: int | None, faults: list[str]):
    """Run the NMR readout on one campaign round, pair it with LC/MS, and write the files."""
    from labauto.analysis import analyze_plate
    from labauto.campaign import load_config, round_dir
    from labauto.config import load_assay
    from labauto.execute import lcms_seed, volumes_by_well
    from labauto.paired import nmr_and_pair
    from labauto.virtual.ground_truth import reference_expected, surface_for
    from labauto.virtual.lcms import load_peaks

    if not (cdir / "campaign.json").exists():
        raise RoundError(f"{cdir.name}: not a campaign folder (no campaign.json)")
    cfg = load_config(cdir)
    r = round_number if round_number is not None else latest_read_round(cdir)
    if r is None:
        raise RoundError("no round has been read by the LC/MS yet")
    rdir = round_dir(cdir, r)
    peaks_path = rdir / "raw" / "lcms_peaks.csv"
    if not peaks_path.exists():
        raise RoundError(f"round {r} has no LC/MS reading yet (no raw/lcms_peaks.csv)")
    nmr_faults = parse_nmr_faults(faults, r)
    assay = load_assay(cfg.assay)
    plan, ledger, source = round_plan_and_ledger(rdir)
    expected = reference_expected(assay, cfg.surface_seed)
    # The same analysis the loop ran, from the same peak table, so the LC/MS numbers match results.csv.
    lcms_wells, qc = analyze_plate(
        load_peaks(peaks_path), plan, volumes_by_well(ledger, plan.plate), assay, expected, r
    )
    evidence, _, _ = nmr_and_pair(
        rdir,
        plan=plan,
        ledger=ledger,
        assay=assay,
        surface=surface_for(cfg.surface_seed),
        plate_seed=lcms_seed(cfg.seed, r),
        round_number=r,
        nmr_faults=nmr_faults,
        lcms_wells=lcms_wells,
        lcms_qc=qc,
        reference_expected=expected,
        campaign_id=cfg.campaign_id,
        title=f"Paired LC/MS and NMR readout: campaign {cfg.campaign_id}, round {r}",
        lcms_faults=list(cfg.faults),
    )
    return r, rdir, evidence, source


def cmd_nmr(args: argparse.Namespace) -> int:
    cdir = Path(args.campaign)
    try:
        r, rdir, evidence, source = pair_campaign_round(cdir, args.round, list(args.fault or []))
    except RoundError as exc:
        print(f"error: {exc}")
        return EXIT_ERROR
    counts = {"agree": 0, "disagree": 0, "insufficient": 0}
    for c in evidence.conditions:
        counts[c.agreement] += 1
    print(f"Round {r}: simulated NMR read {len(evidence.wells)} wells (ledger from {source}).")
    print(
        f"{len(evidence.conditions)} conditions: {counts['agree']} agree, "
        f"{counts['disagree']} disagree, {counts['insufficient']} insufficient."
    )
    print(f"LC/MS plate flags: {', '.join(evidence.plate.lcms_flags) or 'none'}")
    print(f"NMR plate flags: {', '.join(evidence.plate.nmr_flags) or 'none'}")
    print(f"Wrote {rdir / 'paired.md'} and {rdir / 'review' / 'evidence.json'}")
    print("Synthetic data: both readouts are simulated.")
    return EXIT_OK


def add_parsers(sub) -> None:
    """Register this module's commands on the main parser."""
    p = sub.add_parser(
        "nmr", help="run the simulated NMR on a campaign round and pair it with LC/MS"
    )
    p.add_argument("campaign", help="campaign folder")
    p.add_argument("--round", type=int, default=None, help="round number (default: latest read)")
    p.add_argument(
        "--fault",
        action="append",
        help="nmr_broad_lines, nmr_phase_error, or nmr_impurity, each with an optional @R "
        "for the round read (repeatable)",
    )
    p.set_defaults(func=cmd_nmr)
