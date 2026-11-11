"""Neutral facts about labware: well names, well count, and how much each well holds.

These keys are vendor neutral. Each instrument backend maps them to its own names.
"""

from __future__ import annotations

from pydantic import BaseModel


class LabwareSpec(BaseModel):
    """Shape and capacity of one kind of labware."""

    kind: str
    rows: int
    columns: int
    capacity_ul: float

    @property
    def count(self) -> int:
        """Number of wells, or tips for a tip rack."""
        return self.rows * self.columns


LABWARE: dict[str, LabwareSpec] = {
    "plate_96_flat_360ul": LabwareSpec(kind="plate", rows=8, columns=12, capacity_ul=360),
    "reservoir_12_15ml": LabwareSpec(kind="reservoir", rows=1, columns=12, capacity_ul=15000),
    "tips_20ul": LabwareSpec(kind="tips", rows=8, columns=12, capacity_ul=20),
    "tips_50ul": LabwareSpec(kind="tips", rows=8, columns=12, capacity_ul=50),
    "tips_200ul": LabwareSpec(kind="tips", rows=8, columns=12, capacity_ul=200),
    "tips_300ul": LabwareSpec(kind="tips", rows=8, columns=12, capacity_ul=300),
    "tips_1000ul": LabwareSpec(kind="tips", rows=8, columns=12, capacity_ul=1000),
}

ROW_LETTERS = "ABCDEFGHIJKLMNOP"


def get(key: str) -> LabwareSpec | None:
    """Look up a labware key. Returns None for a key this project does not know."""
    return LABWARE.get(key)


def well_names(key: str) -> list[str]:
    """All well names of a labware in column order: A1, B1, ..., H1, A2, ..."""
    spec = LABWARE[key]
    return [f"{ROW_LETTERS[r]}{c + 1}" for c in range(spec.columns) for r in range(spec.rows)]


def column_order_key(well: str) -> tuple[int, int]:
    """Sort key that puts well names in column order (A1, B1, ..., A2, ...)."""
    row = ROW_LETTERS.find(well[:1].upper())
    try:
        col = int(well[1:])
    except ValueError:
        col = 10_000
    return (col, row if row >= 0 else 10_000)
