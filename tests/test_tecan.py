"""Tecan EVO backend through PyLabRobot's chatterbox simulator. Skipped without the [tecan] extra."""

from __future__ import annotations

import subprocess
import sys

import pytest
from conftest import ROOT, make_design

pytest.importorskip("pylabrobot")

from labauto.config import load_workcell  # noqa: E402
from labauto.instruments.tecan_evo import TecanEVO  # noqa: E402
from labauto.models import Plan, PlanWell  # noqa: E402
from labauto.pipeline import check  # noqa: E402


@pytest.fixture
def tecan():
    return load_workcell(ROOT / "configs" / "workcell_tecan_evo.json")


def edit(workcell, fn):
    data = workcell.model_dump(mode="json")
    fn(data)
    return type(workcell).model_validate(data)


def test_good_design_passes(assay, tecan, good_design, tmp_path):
    result = check(good_design, assay, tecan, out_dir=tmp_path)
    assert result.ok, result.errors
    assert result.sim.tips_used == {"large": 4, "small": 38}
    assert (tmp_path / "protocol_tecan.json").exists()
    assert (tmp_path / "protocol_tecan.py").exists()


def test_ledger_and_tracked_volumes_match_the_plan(assay, tecan, good_design, tmp_path):
    result = check(good_design, assay, tecan, out_dir=tmp_path)
    got = {(e.dest_well, e.reagent): e.volume_ul for e in result.sim.ledger}
    want = {(w.well, r): v for w in result.plan.wells for r, v in w.add_ul.items() if v > 0}
    assert got == want
    assert result.sim.well_volumes == {w.well: w.total_ul() for w in result.plan.wells}


def test_same_design_gives_identical_plans_on_both_workcells(
    assay, workcell, tecan, good_design, tmp_path
):
    ot2 = check(good_design, assay, workcell, out_dir=tmp_path / "ot2")
    evo = check(good_design, assay, tecan, out_dir=tmp_path / "evo")
    assert ot2.ok and evo.ok
    assert ot2.plan == evo.plan
    assert ot2.design_hash == evo.design_hash


def test_overdrawn_source_is_caught_by_the_tracker(assay, tecan, tmp_path):
    design = make_design(
        grid={
            "catalyst_mol_pct": [10],
            "base_equiv": [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0],
            "cosolvent_pct": [0, 20, 40],
        },
        replicates=2,
    )
    ruled = check(design, assay, tecan, out_dir=tmp_path / "a")
    assert ruled.failed_layer == "workcell" and ruled.errors[0].code == "SOURCE_SHORT"
    skipped = check(design, assay, tecan, out_dir=tmp_path / "b", skip_layers={"workcell"})
    assert skipped.failed_layer == "simulator"
    assert skipped.errors[0].code == "SIM_BAD_VOLUME"
    assert "TooLittleLiquidError" in skipped.errors[0].message


def test_overfill_is_caught_by_the_tracker(assay, tecan, tmp_path):
    """The Opentrons simulator misses this. PyLabRobot's volume tracker does not."""
    plan = Plan(
        plan_id="p-overfill",
        assay_id=assay.assay_id,
        wells=[PlanWell(well="E1", role="sample", add_ul={"solvent": 190, "substrate": 190})],
    )
    result = check(plan, assay, tecan, out_dir=tmp_path, skip_layers={"plan"})
    assert result.failed_layer == "simulator"
    assert result.errors[0].code == "SIM_BAD_VOLUME"
    assert "TooLittleVolumeError" in result.errors[0].message


def test_tip_shortfall_is_caught_by_the_tracker(assay, tecan, tmp_path):
    design = make_design(
        grid={
            "catalyst_mol_pct": [1, 2, 3, 4, 5, 6, 7, 8, 9],
            "base_equiv": [0.5, 0.75],
            "cosolvent_pct": [20],
        },
        replicates=5,
    )
    result = check(design, assay, tecan, out_dir=tmp_path, skip_layers={"workcell"})
    assert result.failed_layer == "simulator"
    assert result.errors[0].code == "SIM_OUT_OF_TIPS"


def test_deck_conflict_is_caught_by_the_simulator(assay, tecan, good_design, tmp_path):
    def same_site(d):
        d["deck"][3]["position"] = dict(d["deck"][2]["position"])

    result = check(
        good_design, assay, edit(tecan, same_site), out_dir=tmp_path, skip_layers={"workcell"}
    )
    assert result.failed_layer == "simulator"
    assert result.errors[0].code == "SIM_DECK_CONFLICT"


def test_overlapping_carriers_pass_the_rules_but_not_the_simulator(
    assay, tecan, good_design, tmp_path
):
    """Equal positions are a rule. Carriers that overlap on the rails are only caught by PyLabRobot."""

    def overlap(d):
        for e in d["deck"][2:]:
            e["position"]["rails"] = 12

    result = check(good_design, assay, edit(tecan, overlap), out_dir=tmp_path)
    assert result.layer_status["workcell"] == "pass"
    assert result.failed_layer == "simulator"
    assert result.errors[0].code == "SIM_DECK_CONFLICT"


def test_simulator_accepts_below_minimum_volume(tecan, tmp_path):
    """Like the Opentrons simulator, the tracker accepts 5 uL on the 20 to 200 uL channel."""
    from labauto.models import Transfer, Worklist

    row = Transfer(
        step=1,
        reagent="solvent",
        source_labware="reservoir",
        source_well="A5",
        dest_labware="plate",
        dest_well="E1",
        volume_ul=5,
        pipette="large",
        tip="new",
        dispense="in_liquid",
    )
    plan = Plan(
        plan_id="p-small",
        assay_id="a",
        wells=[PlanWell(well="E1", role="sample", add_ul={"solvent": 5})],
    )
    evo = TecanEVO()
    artifact = evo.compile(
        Worklist(workcell_id="w", plan_id="p", rows=[row]), tecan, plan, tmp_path
    )
    assert evo.simulate(artifact, tecan).ok


def test_valid_positions():
    evo = TecanEVO()
    assert evo.valid_position({"carrier": "MP_3Pos", "rails": 16, "site": 0})
    assert not evo.valid_position({"carrier": "MP_3Pos", "rails": 16, "site": 3})
    assert not evo.valid_position({"carrier": "MP_3Pos", "rails": 46, "site": 0})
    assert not evo.valid_position({"carrier": "Nope", "rails": 16, "site": 0})
    assert not evo.valid_position({"slot": 2})


def test_readable_script_runs_on_its_own(assay, tecan, good_design, tmp_path):
    check(good_design, assay, tecan, out_dir=tmp_path)
    proc = subprocess.run(
        [sys.executable, str(tmp_path / "protocol_tecan.py")],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]


def test_demo_row_skips_without_pylabrobot(monkeypatch, assay, tmp_path):
    import importlib.util

    from labauto import cli

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a: None if name == "pylabrobot" else real(name, *a),
    )
    row, ok = cli.tecan_row(assay, str(tmp_path), None)
    assert ok
    assert "SKIPPED (pylabrobot not installed)" in " ".join(row.split())


def test_aspirate_above_tip_capacity_is_caught(tecan, tmp_path):
    from labauto.models import Transfer, Worklist

    row = Transfer(
        step=1,
        reagent="solvent",
        source_labware="reservoir",
        source_well="A5",
        dest_labware="plate",
        dest_well="E1",
        volume_ul=60,
        pipette="small",
        tip="new",
        dispense="in_liquid",
    )
    plan = Plan(
        plan_id="p",
        assay_id="a",
        wells=[PlanWell(well="E1", role="sample", add_ul={"solvent": 60})],
    )
    evo = TecanEVO()
    artifact = evo.compile(
        Worklist(workcell_id="w", plan_id="p", rows=[row]), tecan, plan, tmp_path
    )
    err = evo.simulate(artifact, tecan).error
    assert err.code == "SIM_BAD_VOLUME"
    assert "55" in err.message


# ---------------------------------------------------------------- review fixes


def test_a_second_plate_on_the_deck_is_not_read_into_the_ledger(
    assay, tecan, good_design, tmp_path
):
    def stock(d):
        d["deck"].append(
            {
                "name": "stock",
                "labware": "plate_96_flat_360ul",
                "position": {"carrier": "MP_3Pos", "rails": 16, "site": 2},
            }
        )
        d["inventory"]["catalyst"] = {"labware": "stock", "well": "H12", "volume_ul": 340}

    result = check(good_design, assay, edit(tecan, stock), out_dir=tmp_path)
    assert result.ok, result.errors
    assert "H12" not in result.sim.well_volumes


def test_a_mount_the_instrument_lacks_is_a_bad_slot(assay, tecan, good_design, tmp_path):
    for mount in ("left", "channel_00", "channel_9"):
        wc = edit(tecan, lambda d, m=mount: d["pipettes"][1].update(mount=m))
        result = check(good_design, assay, wc, out_dir=tmp_path / mount)
        assert result.failed_layer == "workcell"
        assert result.errors[0].code == "BAD_SLOT" and result.errors[0].needs_human


def test_free_text_deck_names_give_a_valid_script(assay, tecan, good_design, tmp_path):
    def hyphen(d):
        d["deck"][0]["name"] = "tips-200"
        d["pipettes"][1]["tips"] = "tips-200"

    result = check(good_design, assay, edit(tecan, hyphen), out_dir=tmp_path)
    assert result.ok, result.errors
    proc = subprocess.run(
        [sys.executable, str(tmp_path / "protocol_tecan.py")],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]


def test_a_design_without_ids_gives_identical_plans(
    assay, workcell, tecan, good_design_data, tmp_path
):
    from labauto.models import Design

    data = {k: v for k, v in good_design_data.items() if k not in ("design_id", "workcell_id")}
    design = Design.model_validate(data)
    ot2 = check(design, assay, workcell, out_dir=tmp_path / "ot2")
    evo = check(design, assay, tecan, out_dir=tmp_path / "evo")
    assert ot2.ok and evo.ok
    assert ot2.plan == evo.plan


def test_carrier_past_the_deck_edge_is_a_deck_conflict(assay, tecan, good_design, tmp_path):
    def edge(d):
        for e in d["deck"][2:]:
            e["position"]["rails"] = 45

    result = check(good_design, assay, edit(tecan, edge), out_dir=tmp_path)
    assert result.failed_layer == "simulator"
    assert result.errors[0].code == "SIM_DECK_CONFLICT"


def test_a_tip_already_held_is_a_generator_fault(assay, tecan, good_design, tmp_path):
    import json

    check(good_design, assay, tecan, out_dir=tmp_path)
    artifact = tmp_path / "protocol_tecan.json"
    data = json.loads(artifact.read_text())
    first_drop = next(i for i, c in enumerate(data["calls"]) if c["op"] == "drop_tip")
    del data["calls"][first_drop]
    artifact.write_text(json.dumps(data))
    err = TecanEVO().simulate(artifact, tecan).error
    assert err.code == "SIM_OTHER" and err.needs_human
