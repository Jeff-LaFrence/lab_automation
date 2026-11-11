"""One test per plan layer error code, each with a minimal bad input."""

from __future__ import annotations

from labauto.expand import expand
from labauto.models import Plan, PlanWell
from labauto.validate import parse_plan, validate_plan

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
    well = one_well_plan().wells[0]
    plan = Plan(plan_id="p-test", assay_id=assay.assay_id, wells=[well, well.model_copy()])
    assert codes(validate_plan(plan, assay)) == ["DUPLICATE_WELL"]


def test_negative_fill_when_other_additions_exceed_the_target(assay):
    errors = validate_plan(
        one_well_plan(solvent=-20, substrate=60, base=60, catalyst=20, cosolvent=80), assay
    )
    assert codes(errors)[0] == "NEGATIVE_VOLUME"
    assert errors[0].message == (
        "The solvent fill in E1 is -20 uL because the other additions total 220 uL, "
        "above the 200 uL target. Volumes cannot be below zero."
    )


def test_negative_fill_that_should_be_zero_says_so(assay):
    errors = validate_plan(one_well_plan(solvent=-10, base=60, catalyst=20, cosolvent=80), assay)
    assert codes(errors)[0] == "NEGATIVE_VOLUME"
    assert "the fill should be 0 uL" in errors[0].message
    assert "above" not in errors[0].message


def test_negative_non_fill_volume(assay):
    errors = validate_plan(one_well_plan(base=-5, solvent=123), assay)
    assert codes(errors)[0] == "NEGATIVE_VOLUME"
    assert errors[0].where == "plate:E1:base"


def test_empty_plan(assay):
    plan = Plan(plan_id="p-empty", assay_id=assay.assay_id, wells=[])
    assert codes(validate_plan(plan, assay)) == ["EMPTY_PLAN"]


def test_plan_for_another_assay(assay):
    plan = one_well_plan()
    plan = plan.model_copy(update={"assay_id": "other_assay"})
    errors = validate_plan(plan, assay)
    assert codes(errors) == ["SCHEMA_INVALID"]
    assert errors[0].needs_human


def test_missing_totals_are_filled_from_the_wells(assay):
    plan = one_well_plan()
    assert plan.totals_ul == GOOD
    assert validate_plan(plan, assay) == []


def test_stale_totals_are_caught_at_the_plan_layer(assay):
    data = one_well_plan().model_dump(mode="json")
    data["totals_ul"]["solvent"] = 50
    errors = validate_plan(Plan.model_validate(data), assay)
    assert codes(errors) == ["TOTALS_MISMATCH"]
    assert "lists 50 uL of solvent" in errors[0].message and "98 uL" in errors[0].message


def test_nan_volume_fails_parsing():
    data = {
        "plan_id": "p",
        "assay_id": "a",
        "wells": [{"well": "E1", "role": "sample", "add_ul": {"solvent": float("nan")}}],
    }
    plan, errors = parse_plan(data)
    assert plan is None
    assert codes(errors) == ["SCHEMA_INVALID"]


def test_unknown_field_in_a_plan_well_fails_parsing():
    data = {
        "plan_id": "p",
        "assay_id": "a",
        "wells": [{"well": "E1", "role": "sample", "add_ul": {}, "volume": 3}],
    }
    assert codes(parse_plan(data)[1]) == ["SCHEMA_INVALID"]


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
