"""Turn a Plan into a Worklist: the ordered transfers on one workcell.

Order: all bulk transfers first, reagent by reagent in the assay's addition
order, wells in column order. Then all small transfers, in the same order.

A transfer is small when its volume is below the workcell's
contact_dispense_below_ul. Small transfers get a fresh tip for every well and
are dispensed into the liquid already in the well. Bulk transfers use one tip
per reagent per pipette and are dispensed from above the liquid, so the tip
never touches a well's contents.
"""

from __future__ import annotations

from collections import defaultdict

from labauto import labware
from labauto.config import TOLERANCE
from labauto.models import Assay, PipetteSpec, Plan, Transfer, Workcell, Worklist

NO_PIPETTE = ""


def bulk_pipette(volume: float, pipettes: list[PipetteSpec]) -> str:
    """For a bulk transfer: the pipette with the largest range that contains the volume."""
    fits = [p for p in pipettes if p.min_ul - TOLERANCE <= volume <= p.max_ul + TOLERANCE]
    if not fits:
        return NO_PIPETTE
    return max(fits, key=lambda p: (p.max_ul - p.min_ul, p.max_ul)).name


def small_pipette(volume: float, pipettes: list[PipetteSpec]) -> str:
    """For a small transfer: the pipette with the smallest maximum that contains the volume."""
    fits = [p for p in pipettes if p.min_ul - TOLERANCE <= volume <= p.max_ul + TOLERANCE]
    if not fits:
        return NO_PIPETTE
    return min(fits, key=lambda p: (p.max_ul, p.max_ul - p.min_ul)).name


def schedule(plan: Plan, workcell: Workcell, assay: Assay) -> Worklist:
    """Build the worklist for a plan. Never raises on a bad workcell: the rules catch it."""
    threshold = workcell.liquid_handling.contact_dispense_below_ul
    order = list(assay.addition_order)
    for w in plan.wells:
        for r in w.add_ul:
            if r not in order:
                order.append(r)
    wells = sorted(plan.wells, key=lambda w: labware.column_order_key(w.well))

    bulk: list[dict] = []
    small: list[dict] = []
    for reagent in order:
        inv = workcell.inventory.get(reagent)
        for w in wells:
            vol = w.add_ul.get(reagent, 0.0)
            if vol <= 0:
                continue
            row = {
                "reagent": reagent,
                "source_labware": inv.labware if inv else "",
                "source_well": inv.well if inv else "",
                "dest_labware": plan.plate,
                "dest_well": w.well,
                "volume_ul": vol,
            }
            if vol >= threshold - TOLERANCE:
                row.update(pipette=bulk_pipette(vol, workcell.pipettes), dispense="above_liquid")
                bulk.append(row)
            else:
                row.update(pipette=small_pipette(vol, workcell.pipettes), dispense="in_liquid")
                small.append(row)

    rows: list[Transfer] = []
    started: set[tuple[str, str]] = set()
    for row in bulk:
        group = (row["reagent"], row["pipette"])
        tip = "keep" if group in started else "new"
        started.add(group)
        rows.append(Transfer(step=len(rows) + 1, tip=tip, **row))
    for row in small:
        rows.append(Transfer(step=len(rows) + 1, tip="new", **row))

    return Worklist(
        workcell_id=workcell.workcell_id,
        plan_id=plan.plan_id,
        rows=rows,
        tips_needed=count_tips(rows, workcell),
    )


def count_tips(rows: list[Transfer], workcell: Workcell) -> dict[str, int]:
    """Tips needed from each tip rack: one for every row that takes a new tip."""
    out: dict[str, int] = defaultdict(int)
    for row in rows:
        pip = workcell.pipette(row.pipette)
        if pip is not None and row.tip == "new":
            out[pip.tips] += 1
    return dict(out)


CSV_COLUMNS = [
    "step",
    "reagent",
    "source_labware",
    "source_well",
    "dest_labware",
    "dest_well",
    "volume_ul",
    "pipette",
    "tip",
    "dispense",
]
