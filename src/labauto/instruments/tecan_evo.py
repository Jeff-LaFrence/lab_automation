"""Tecan EVO backend, simulated with PyLabRobot's chatterbox backend.

compile writes protocol_tecan.json, a list of calls to make, and
protocol_tecan.py, a readable PyLabRobot script that makes the same calls.
simulate replays the JSON calls against a simulated EVO 150 deck with tip
tracking and volume tracking on.

This is bookkeeping only. It checks tips and volumes. It does not check
collisions, calibration, liquid classes, or timing, and it has never driven a
real instrument.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
import warnings
from collections import defaultdict
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from labauto import labware
from labauto.config import fmt_code
from labauto.instruments.base import scrub_paths
from labauto.models import LedgerEntry, Plan, PlanError, SimResult, Workcell, Worklist

FORMAT = "labauto-tecan-calls-v1"

LABWARE_MAP = {
    "tips_200ul": "DiTi_200ul_LiHa",
    "tips_50ul": "DiTi_50ul_LiHa",
    "plate_96_flat_360ul": "cor_96_wellplate_360uL_Fb",
    "reservoir_12_15ml": "celltreat_12_troughplate_15mL_Vb",
}
PIPETTE_MAP = {
    "liha_200ul": "LiHa channel with 200 uL disposable tips",
    "liha_50ul": "LiHa channel with 50 uL disposable tips",
}
CARRIERS = {"DiTi_3Pos": 3, "MP_3Pos": 3}
NUM_RAILS = 45  # EVO 150
NUM_CHANNELS = 8

# Error text checked in this order. The first match picks the code.
ERROR_CODES = [
    ("TooLittleVolumeError", "SIM_BAD_VOLUME"),
    ("TooLittleLiquidError", "SIM_BAD_VOLUME"),
    ("already occupied", "SIM_DECK_CONFLICT"),
    ("already has a resource", "SIM_DECK_CONFLICT"),
    ("Rails must be between", "SIM_DECK_CONFLICT"),
    ("not a known carrier", "SIM_DECK_CONFLICT"),
    ("not a known labware", "SIM_LABWARE_NOT_FOUND"),
]
HINTS = {
    "SIM_OUT_OF_TIPS": (
        "Use fewer wells with small additions, or ask for another tip rack.",
        False,
    ),
    "SIM_DECK_CONFLICT": ("Fix the deck layout in the workcell file.", True),
    "SIM_BAD_VOLUME": ("Change the values so every transfer fits its tip and every source.", False),
    "SIM_LABWARE_NOT_FOUND": (
        "Fix the labware names in the workcell file or the backend map.",
        True,
    ),
    "SIM_OTHER": ("This points to a fault in the protocol generator. Keep the run folder.", True),
}


def channel_of(mount: str) -> int:
    """A Tecan pipette's mount is its LiHa channel, written channel_0 to channel_7."""
    prefix = "channel_"
    if not mount.startswith(prefix) or not mount[len(prefix) :].isdigit():
        raise ValueError(f"mount '{mount}' is not a channel such as channel_0")
    ch = int(mount[len(prefix) :])
    if not 0 <= ch < NUM_CHANNELS:
        raise ValueError(f"mount '{mount}' is outside channels 0 to {NUM_CHANNELS - 1}")
    return ch


class TecanEVO:
    """Backend for a Tecan EVO 150, checked with PyLabRobot's bookkeeping simulator."""

    name = "tecan_evo"

    def labware_map(self) -> dict[str, str]:
        return dict(LABWARE_MAP)

    def pipette_map(self) -> dict[str, str]:
        return dict(PIPETTE_MAP)

    def valid_position(self, position: dict) -> bool:
        if set(position) != {"carrier", "rails", "site"}:
            return False
        carrier, rails, site = position["carrier"], position["rails"], position["site"]
        return (
            carrier in CARRIERS
            and type(rails) is int
            and 1 <= rails <= NUM_RAILS
            and type(site) is int
            and 0 <= site < CARRIERS[carrier]
        )

    # ------------------------------------------------------------ compile

    def calls(self, worklist: Worklist, workcell: Workcell, plan: Plan) -> dict:
        """The calls to make, as plain data. One channel at a time."""
        deck = [
            {
                "name": e.name,
                "resource": LABWARE_MAP.get(e.labware, e.labware),
                "carrier": e.position.get("carrier"),
                "rails": e.position.get("rails"),
                "site": e.position.get("site"),
            }
            for e in workcell.deck
        ]
        next_tip: dict[str, int] = defaultdict(int)
        held: dict[int, bool] = defaultdict(bool)
        out: list[dict] = []
        rows = worklist.rows
        section = None
        for i, row in enumerate(rows):
            pip = workcell.pipette(row.pipette)
            if pip is None:
                raise ValueError(f"worklist step {row.step} has no pipette that covers the volume")
            ch = channel_of(pip.mount)
            bulk = row.dispense == "above_liquid"
            if (bulk, row.reagent) != section:
                section = (bulk, row.reagent)
                text = (
                    f"{row.reagent}: bulk additions, one tip, dispensed from above the liquid"
                    if bulk
                    else f"{row.reagent}: small additions, fresh tip per well, dispensed into the liquid"
                )
                out.append({"op": "comment", "text": text})
            if row.tip == "new":
                if held[ch]:
                    out.append({"op": "drop_tip", "channel": ch})
                rack = pip.tips
                entry = workcell.deck_entry(rack)
                spec = labware.get(entry.labware) if entry else None
                spots = labware.well_names(entry.labware) if spec else ["A1"]
                # A rack that runs out wraps to its first spot, as a naive
                # program would. The tip tracker then reports the empty spot.
                spot = spots[next_tip[rack] % len(spots)]
                next_tip[rack] += 1
                out.append({"op": "pick_up_tip", "channel": ch, "rack": rack, "spot": spot})
                held[ch] = True
            out.append(
                {
                    "op": "aspirate",
                    "channel": ch,
                    "labware": row.source_labware,
                    "well": row.source_well,
                    "volume_ul": row.volume_ul,
                }
            )
            out.append(
                {
                    "op": "dispense",
                    "channel": ch,
                    "labware": row.dest_labware,
                    "well": row.dest_well,
                    "volume_ul": row.volume_ul,
                    "mode": row.dispense,
                }
            )
            nxt = next((r for r in rows[i + 1 :] if r.pipette == row.pipette), None)
            if nxt is None or nxt.tip == "new":
                out.append({"op": "drop_tip", "channel": ch})
                held[ch] = False
        return {
            "format": FORMAT,
            "plan_id": plan.plan_id,
            "design_id": plan.design_id,
            "workcell_id": workcell.workcell_id,
            "deck": deck,
            "sources": {
                reagent: {"labware": inv.labware, "well": inv.well, "volume_ul": inv.volume_ul}
                for reagent, inv in workcell.inventory.items()
            },
            "calls": out,
        }

    def render(self, data: dict) -> str:
        """protocol_tecan.py: a readable PyLabRobot script that makes the same calls."""
        lines = [
            f"# Generated by labauto from plan {data['plan_id']} (design {data['design_id']}). "
            "Do not edit by hand.",
            "# Runs the calls in protocol_tecan.json against PyLabRobot's chatterbox backend.",
            "# Bookkeeping only: tips and volumes are tracked, nothing else is simulated.",
            "import asyncio",
            "",
            "from pylabrobot.liquid_handling import LiquidHandler",
            "from pylabrobot.liquid_handling.backends import LiquidHandlerChatterboxBackend",
            "from pylabrobot import resources as R",
            "",
            "",
            "async def run():",
            "    R.set_tip_tracking(True)",
            "    R.set_volume_tracking(True)",
            "    deck = R.EVO150Deck()",
            f"    lh = LiquidHandler(backend=LiquidHandlerChatterboxBackend(num_channels={NUM_CHANNELS}), deck=deck)",
            "    await lh.setup()",
            '    deck.assign_child_resource(R.Trash(name="trash", size_x=100, size_y=100, size_z=10),',
            "                               location=R.Coordinate(x=900, y=100, z=0))",
        ]
        carriers: dict[tuple, str] = {}
        for d in data["deck"]:
            key = (d["carrier"], d["rails"])
            if key not in carriers:
                var = f"carrier_{len(carriers) + 1}"
                carriers[key] = var
                lines.append(f'    {var} = R.{d["carrier"]}(name="{var}")')
                lines.append(f"    deck.assign_child_resource({var}, rails={d['rails']})")
            lines.append(f'    {d["name"]} = R.{d["resource"]}(name="{d["name"]}")')
            lines.append(f"    {carriers[key]}[{d['site']}] = {d['name']}")
        names = ", ".join(f'"{d["name"]}": {d["name"]}' for d in data["deck"])
        lines.append(f"    labware = {{{names}}}")
        for reagent, src in data.get("sources", {}).items():
            lines.append(
                f'    labware["{src["labware"]}"].get_well("{src["well"]}").set_volume('
                f"{fmt_code(src['volume_ul'])})  # {reagent}"
            )
        for c in data["calls"]:
            op = c["op"]
            if op == "comment":
                lines.append("")
                lines.append(f"    # {c['text']}")
            elif op == "pick_up_tip":
                lines.append(
                    f'    await lh.pick_up_tips([{c["rack"]}.get_item("{c["spot"]}")], use_channels=[{c["channel"]}])'
                )
            elif op == "aspirate":
                lines.append(
                    f'    await lh.aspirate([{c["labware"]}.get_well("{c["well"]}")], '
                    f"vols=[{fmt_code(c['volume_ul'])}], use_channels=[{c['channel']}])"
                )
            elif op == "dispense":
                lines.append(
                    f'    await lh.dispense([{c["labware"]}.get_well("{c["well"]}")], '
                    f"vols=[{fmt_code(c['volume_ul'])}], use_channels=[{c['channel']}])"
                )
            elif op == "drop_tip":
                lines.append(f"    await lh.discard_tips(use_channels=[{c['channel']}])")
        lines += [
            "",
            "",
            'if __name__ == "__main__":',
            "    asyncio.run(run())",
        ]
        return "\n".join(lines) + "\n"

    def compile(self, worklist: Worklist, workcell: Workcell, plan: Plan, out_dir: Path) -> Path:
        """Write protocol_tecan.json and protocol_tecan.py. Returns the JSON path."""
        out_dir.mkdir(parents=True, exist_ok=True)
        data = self.calls(worklist, workcell, plan)
        path = out_dir / "protocol_tecan.json"
        path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        (out_dir / "protocol_tecan.py").write_text(self.render(data), encoding="utf-8")
        return path

    # ------------------------------------------------------------ simulate

    def simulate(self, artifact: Path, workcell: Workcell) -> SimResult:
        """Replay protocol_tecan.json against the chatterbox backend with tracking on."""
        data = json.loads(Path(artifact).read_text(encoding="utf-8"))
        start = time.perf_counter()
        state = _RunState()
        chatter = io.StringIO()
        try:
            with redirect_stdout(chatter), redirect_stderr(chatter), warnings.catch_warnings():
                warnings.simplefilter("ignore")
                asyncio.run(_execute(data, workcell, state))
        except Exception as exc:  # the tracker reports every problem as an exception
            return SimResult(
                ok=False,
                instrument=self.name,
                steps=state.steps,
                error=map_error(exc, state.current),
                seconds=round(time.perf_counter() - start, 3),
            )
        return SimResult(
            ok=True,
            instrument=self.name,
            steps=state.steps,
            ledger=[
                LedgerEntry(dest_labware=lw, dest_well=w, reagent=r, volume_ul=round(v, 6))
                for (lw, w, r), v in state.ledger.items()
            ],
            tips_used=dict(state.tips),
            well_volumes={w: round(v, 6) for w, v in state.well_volumes.items()},
            seconds=round(time.perf_counter() - start, 3),
        )


class _RunState:
    """What the replay has done so far: steps, ledger, tips, and the call being run."""

    def __init__(self) -> None:
        self.steps: list[str] = []
        self.ledger: dict[tuple[str, str, str], float] = defaultdict(float)
        self.tips: dict[str, int] = defaultdict(int)
        self.well_volumes: dict[str, float] = {}
        self.current: dict | None = None


async def _execute(data: dict, workcell: Workcell, state: _RunState) -> None:
    from pylabrobot import resources as R
    from pylabrobot.liquid_handling import LiquidHandler
    from pylabrobot.liquid_handling.backends import LiquidHandlerChatterboxBackend

    R.set_tip_tracking(True)
    R.set_volume_tracking(True)
    deck = R.EVO150Deck()
    lh = LiquidHandler(backend=LiquidHandlerChatterboxBackend(num_channels=NUM_CHANNELS), deck=deck)
    await lh.setup()
    deck.assign_child_resource(
        R.Trash(name="trash", size_x=100, size_y=100, size_z=10),
        location=R.Coordinate(x=900, y=100, z=0),
    )

    resources: dict = {}
    carriers: dict[tuple, object] = {}
    for d in data["deck"]:
        state.current = {"op": "load", **d}
        factory = getattr(R, d["carrier"] or "", None)
        if d["carrier"] not in CARRIERS or factory is None:
            raise ValueError(f"'{d['carrier']}' is not a known carrier")
        key = (d["carrier"], d["rails"])
        if key not in carriers:
            carrier = factory(name=f"{d['carrier']}_rails_{d['rails']}")
            deck.assign_child_resource(carrier, rails=d["rails"])
            carriers[key] = carrier
        make = getattr(R, d["resource"], None)
        if make is None:
            raise ValueError(f"'{d['resource']}' is not a known labware in PyLabRobot")
        res = make(name=d["name"])
        carriers[key][d["site"]] = res
        resources[d["name"]] = res
        state.steps.append(
            f"Loaded {d['resource']} as {d['name']} on {d['carrier']} at rails {d['rails']} site {d['site']}"
        )

    sources: dict[tuple[str, str], str] = {}
    for reagent, inv in workcell.inventory.items():
        state.current = {"op": "fill_source", "reagent": reagent}
        if inv.labware in resources:
            resources[inv.labware].get_well(inv.well).set_volume(inv.volume_ul)
            sources[(inv.labware, inv.well)] = reagent
            state.steps.append(
                f"Set {inv.labware} {inv.well} to {fmt_code(inv.volume_ul)} uL of {reagent}"
            )

    by_channel = {channel_of(p.mount): p.name for p in workcell.pipettes}
    holding: dict[int, str] = {}
    plate_names = {
        e.name
        for e in workcell.deck
        if labware.get(e.labware) and labware.get(e.labware).kind == "plate"
    }
    for call in data["calls"]:
        state.current = call
        op = call["op"]
        if op == "comment":
            state.steps.append(call["text"])
            continue
        ch = call["channel"]
        if op == "pick_up_tip":
            spot = resources[call["rack"]].get_item(call["spot"])
            await lh.pick_up_tips([spot], use_channels=[ch])
            state.tips[by_channel.get(ch, f"channel_{ch}")] += 1
            state.steps.append(f"Picking up tip from {call['rack']} {call['spot']} on channel {ch}")
        elif op == "aspirate":
            well = resources[call["labware"]].get_well(call["well"])
            await lh.aspirate([well], vols=[call["volume_ul"]], use_channels=[ch])
            holding[ch] = sources.get(
                (call["labware"], call["well"]), f"unknown:{call['labware']}:{call['well']}"
            )
            state.steps.append(
                f"Aspirating {fmt_code(call['volume_ul'])} uL from {call['labware']} {call['well']} on channel {ch}"
            )
        elif op == "dispense":
            well = resources[call["labware"]].get_well(call["well"])
            await lh.dispense([well], vols=[call["volume_ul"]], use_channels=[ch])
            state.ledger[(call["labware"], call["well"], holding.get(ch, "unknown"))] += float(
                call["volume_ul"]
            )
            state.steps.append(
                f"Dispensing {fmt_code(call['volume_ul'])} uL into {call['labware']} {call['well']} on channel {ch}"
            )
        elif op == "drop_tip":
            await lh.discard_tips(use_channels=[ch])
            state.steps.append(f"Dropping tip from channel {ch} into the trash")
        else:
            raise ValueError(f"unknown call '{op}'")
    state.current = None

    for name in plate_names:
        plate = resources.get(name)
        if plate is None:
            continue
        for well in plate.children:
            used = well.tracker.get_used_volume()
            if used:
                short = well.name.rsplit("_", 1)[-1]
                state.well_volumes[short] = float(used)


def map_error(exc: BaseException, call: dict | None) -> PlanError:
    """Turn a tracker or deck exception into a SIM_* error, keeping the original text."""
    kind = type(exc).__name__
    raw = f"{kind}: {exc}"
    code = "SIM_OTHER"
    for needle, mapped in ERROR_CODES:
        if needle in raw:
            code = mapped
            break
    op = (call or {}).get("op")
    if kind in ("NoTipError", "HasTipError") and op == "pick_up_tip":
        code = "SIM_OUT_OF_TIPS"
    if kind == "KeyError" and op == "load":
        code = "SIM_DECK_CONFLICT"
    where = "protocol_tecan.json"
    if call:
        where += f" {op}" + (
            f" {call.get('labware') or call.get('rack') or call.get('name') or ''}".rstrip()
        )
        if call.get("well") or call.get("spot"):
            where += f" {call.get('well') or call.get('spot')}"
    hint, needs_human = HINTS[code]
    return PlanError(
        code=code,
        layer="simulator",
        where=where,
        message=f"The PyLabRobot simulator stopped at {where}. Original error: {scrub_paths(raw)[:500]}",
        fix_hint=hint,
        options=["ask_human"] if needs_human else ["reduce_wells", "change_values"],
        needs_human=needs_human,
        short=where,
    )
