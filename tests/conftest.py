"""Shared fixtures. Tests never touch the network or a GPU."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("OT_API_CONFIG_DIR", str(ROOT / ".labauto" / "opentrons"))
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / ".labauto" / "mpl"))
(ROOT / ".labauto" / "opentrons").mkdir(parents=True, exist_ok=True)
(ROOT / ".labauto" / "mpl").mkdir(parents=True, exist_ok=True)

from labauto.config import load_assay, load_workcell  # noqa: E402
from labauto.models import Design  # noqa: E402


@pytest.fixture
def root() -> Path:
    return ROOT


@pytest.fixture
def assay():
    return load_assay(ROOT / "configs" / "assay_reaction_screen.json")


@pytest.fixture
def workcell():
    return load_workcell(ROOT / "configs" / "workcell_ot2.json")


@pytest.fixture
def good_design_data() -> dict:
    return json.loads((ROOT / "examples" / "designs" / "good_factorial.json").read_text())


@pytest.fixture
def good_design(good_design_data) -> Design:
    return Design.model_validate(good_design_data)


def make_design(**fields) -> Design:
    """A small valid design, with any field replaced."""
    base = {
        "design_id": "d-test",
        "title": "test",
        "grid": {"catalyst_mol_pct": [1, 2], "base_equiv": [1.0], "cosolvent_pct": [20]},
        "replicates": 2,
        "include_controls": True,
        "layout": {"strategy": "by_column", "seed": 0},
    }
    base.update(fields)
    return Design.model_validate(base)
