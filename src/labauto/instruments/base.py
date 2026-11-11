"""The Instrument slot: what every liquid handler backend must provide.

A backend maps neutral labware and pipette keys to its own names, writes a
protocol from a worklist, and runs that protocol in the vendor's simulator.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from labauto.models import Plan, SimResult, Workcell, Worklist


class Instrument(Protocol):
    """Interface for one liquid handler backend."""

    name: str

    def labware_map(self) -> dict[str, str]:
        """Neutral labware key to the backend's own labware name."""
        ...

    def pipette_map(self) -> dict[str, str]:
        """Neutral pipette model key to the backend's own pipette name."""
        ...

    def valid_position(self, position: dict) -> bool:
        """True when a deck position is valid on this instrument."""
        ...

    def valid_mount(self, mount: str) -> bool:
        """True when a pipette mount is valid on this instrument."""
        ...

    def compile(self, worklist: Worklist, workcell: Workcell, plan: Plan, out_dir: Path) -> Path:
        """Write the protocol for a worklist into out_dir and return its path."""
        ...

    def simulate(self, artifact: Path, workcell: Workcell) -> SimResult:
        """Run a compiled protocol in the simulator and report what it dispensed."""
        ...


_HOME = re.compile(r"(/home/|/Users/|/root/|[A-Za-z]:\\Users\\)[^\s'\"]*")


def scrub_paths(text: str) -> str:
    """Remove absolute file paths from simulator messages before they are saved."""
    return _HOME.sub("<path>", text)
