"""One test per plan layer error code, each with a minimal bad input."""

from __future__ import annotations

from labauto.expand import expand
from labauto.models import Plan, PlanWell
from labauto.validate import validate_plan

GOOD = {"solvent": 98, "substrate": 40, "cosolvent": 40, "base": 20, "catalyst": 2}


def one_well_plan(well="E1", role="sample", **add) -> Plan:
    vols = dict(GOOD)
    vols.update(add)
    return Plan(
        plan_id="p-test",
        assay_id="generic_reaction_screen_v1",
        wells=[PlanWell(well=well, role=role, condition_index=0, add_ul=vols)],
    )


def codes(errors):
    return [e.code for e in errors]


def test_expanded_plan_passes(assay, good_design):
    assert validate_plan(expand(good_design, assay), assay) == []


def test_unknown_reagent(assay):
    errors = validate_plan(one_well_plan(water=0), assay)
    assert "UNKNOWN_REAGENT" in codes(errors)


def test_bad_well_name(assay):
    assert codes(validate_plan(one_well_plan(well="Z13"), assay)) == ["BAD_WELL_NAME"]


def test_duplicate_well(assay):
    plan = one_well_plan()
    plan.wells.append(plan.wells[0].model_copy())
    assert codes(validate_plan(plan, assay)) == ["DUPLICATE_WELL"]


def test_negative_volume_including_fill(assay):
    errors = validate_plan(one_well_plan(solvent=-10, base=60, catalyst=20, cosolvent=80), assay)
    assert "NEGATIVE_VOLUME" in codes(errors)
    assert "fill" in errors[0].message


def test_well_overfill_reported_first(assay):
    errors = validate_plan(one_well_plan(solvent=498), assay)
    assert codes(errors) == ["WELL_OVERFILL", "WELL_TOTAL_MISMATCH"]
    assert errors[0].brief() == "WELL_OVERFILL plate:E1 600 uL, limit 250"


def test_overfill_against_labware_capacity_when_assay_allows_more(assay):
    roomy = assay.model_copy(update={"max_well_volume_ul": 1000})
    errors = validate_plan(one_well_plan(solvent=398), roomy)
    assert "WELL_OVERFILL" in codes(errors)
    assert "limit 360" in errors[0].short


def test_well_total_mismatch(assay):
    assert codes(validate_plan(one_well_plan(solvent=90), assay)) == ["WELL_TOTAL_MISMATCH"]


def test_control_wells_are_not_held_to_the_total(assay):
    plan = one_well_plan(well="A1", role="negative_control", solvent=50)
    assert validate_plan(plan, assay) == []
