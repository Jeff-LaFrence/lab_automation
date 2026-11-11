"""Expansion from design to plan, and scheduling from plan to worklist."""

from __future__ import annotations

import pytest
from conftest import make_design

from labauto.expand import DesignInvalid, expand
from labauto.models import Layout
from labauto.schedule import schedule

# ---------------------------------------------------------------- expand


def test_grid_expands_to_the_right_count(assay, good_design):
    plan = expand(good_design, assay)
    samples = [w for w in plan.wells if w.role == "sample"]
    assert len(samples) == 36
    assert len({w.condition_index for w in samples}) == 12
    assert len(plan.wells) == 40


def test_fill_tops_up_to_total(assay, good_design):
    plan = expand(good_design, assay)
    for w in plan.wells:
        assert w.total_ul() == 200
    w = next(
        w
        for w in plan.wells
        if w.conditions == {"catalyst_mol_pct": 1.0, "base_equiv": 1.0, "cosolvent_pct": 20.0}
    )
    assert w.add_ul == {"solvent": 98, "substrate": 40, "cosolvent": 40, "base": 20, "catalyst": 2}


def test_controls_land_in_their_wells(assay, good_design):
    plan = expand(good_design, assay)
    by_well = {w.well: w for w in plan.wells}
    for well in ("A1", "B1"):
        assert by_well[well].role == "negative_control"
        assert by_well[well].add_ul["catalyst"] == 0
        assert by_well[well].add_ul["solvent"] == 90
    for well in ("C1", "D1"):
        assert by_well[well].role == "reference_control"
        assert by_well[well].add_ul["catalyst"] == 4
        assert by_well[well].add_ul["base"] == 30
    samples = sorted((w for w in plan.wells if w.role == "sample"), key=lambda w: w.well)
    assert "E1" in {w.well for w in samples}
    assert not {"A1", "B1", "C1", "D1"} & {w.well for w in samples}


def test_samples_fill_wells_in_column_order(assay):
    plan = expand(make_design(replicates=3), assay)
    wells = [w.well for w in plan.wells if w.role == "sample"]
    assert wells == ["E1", "F1", "G1", "H1", "A2", "B2"]


def test_by_column_layout_is_in_order(assay):
    plan = expand(make_design(replicates=3), assay)
    pairs = [(w.condition_index, w.replicate) for w in plan.wells if w.role == "sample"]
    assert pairs == [(0, 1), (0, 2), (0, 3), (1, 1), (1, 2), (1, 3)]


def test_randomized_layout_is_stable_for_a_seed_and_differs_across_seeds(assay, good_design):
    def layout(seed):
        d = good_design.model_copy(update={"layout": Layout(strategy="randomized", seed=seed)})
        return [(w.well, w.condition_index, w.replicate) for w in expand(d, assay).wells]

    assert layout(7) == layout(7)
    assert layout(7) != layout(8)


def test_totals(assay, good_design):
    plan = expand(good_design, assay)
    assert plan.totals_ul["catalyst"] == 278
    assert sum(plan.totals_ul.values()) == 40 * 200


def test_expand_refuses_a_bad_design(assay):
    with pytest.raises(DesignInvalid) as info:
        expand(make_design(replicates=0), assay)
    assert info.value.errors[0].code == "BAD_REPLICATES"


def test_controls_off(assay):
    plan = expand(make_design(include_controls=False, replicates=1), assay)
    assert [w.well for w in plan.wells] == ["A1", "B1"]


# ---------------------------------------------------------------- schedule


def test_order_bulk_then_small_in_addition_order(assay, workcell, good_design):
    plan = expand(good_design, assay)
    rows = schedule(plan, workcell, assay).rows
    assert [r.step for r in rows] == list(range(1, len(rows) + 1))
    kinds = [r.dispense for r in rows]
    first_small = kinds.index("in_liquid")
    assert all(k == "above_liquid" for k in kinds[:first_small])
    assert all(k == "in_liquid" for k in kinds[first_small:])
    bulk_reagents = []
    for r in rows[:first_small]:
        if not bulk_reagents or bulk_reagents[-1] != r.reagent:
            bulk_reagents.append(r.reagent)
    assert bulk_reagents == ["solvent", "substrate", "cosolvent", "base"]
    solvent_wells = [r.dest_well for r in rows if r.reagent == "solvent"]
    assert solvent_wells[:6] == ["A1", "B1", "C1", "D1", "E1", "F1"]


def test_zero_volumes_produce_no_row(assay, workcell, good_design):
    rows = schedule(expand(good_design, assay), workcell, assay).rows
    assert not [r for r in rows if r.reagent == "catalyst" and r.dest_well in ("A1", "B1")]
    assert all(r.volume_ul > 0 for r in rows)


@pytest.mark.parametrize(
    "volume, pipette, dispense",
    [(19, "small", "in_liquid"), (20, "large", "above_liquid"), (21, "large", "above_liquid")],
)
def test_pipette_choice_at_19_20_21(assay, workcell, volume, pipette, dispense):
    from labauto.models import Plan, PlanWell

    plan = Plan(
        plan_id="p-t",
        assay_id=assay.assay_id,
        wells=[PlanWell(well="E1", role="sample", add_ul={"catalyst": volume})],
    )
    rows = schedule(plan, workcell, assay).rows
    assert len(rows) == 1
    assert rows[0].pipette == pipette
    assert rows[0].dispense == dispense


def test_tip_counts(assay, workcell, good_design):
    wl = schedule(expand(good_design, assay), workcell, assay)
    assert wl.tips_needed == {"tips_300": 4, "tips_20": 38}
    bulk = [r for r in wl.rows if r.dispense == "above_liquid"]
    assert sum(r.tip == "new" for r in bulk) == 4
    small = [r for r in wl.rows if r.dispense == "in_liquid"]
    assert all(r.tip == "new" for r in small)


def test_no_pipette_leaves_pipette_blank(assay, workcell):
    from labauto.models import Plan, PlanWell

    plan = Plan(
        plan_id="p-t",
        assay_id=assay.assay_id,
        wells=[PlanWell(well="E1", role="sample", add_ul={"solvent": 400})],
    )
    assert schedule(plan, workcell, assay).rows[0].pipette == ""


def test_schedule_is_pure(assay, workcell, good_design):
    plan = expand(good_design, assay)
    assert schedule(plan, workcell, assay) == schedule(plan, workcell, assay)
    assert expand(good_design, assay) == plan
