"""One test per workcell layer error code, each with a minimal bad input."""

from __future__ import annotations

from conftest import make_design

from labauto.expand import expand
from labauto.schedule import schedule
from labauto.validate import validate_worklist


def run_rules(design, assay, workcell):
    plan = expand(design, assay)
    worklist = schedule(plan, workcell, assay)
    return validate_worklist(worklist, plan, workcell, assay)


def codes(errors):
    return [e.code for e in errors]


def edit(workcell, fn):
    data = workcell.model_dump(mode="json")
    fn(data)
    return type(workcell).model_validate(data)


def test_good_design_passes(assay, workcell, good_design):
    assert run_rules(good_design, assay, workcell) == []


def test_deck_conflict_needs_human(assay, workcell, good_design):
    def move(d):
        d["deck"][3]["position"] = {"slot": 2}

    errors = run_rules(good_design, assay, edit(workcell, move))
    assert codes(errors) == ["DECK_CONFLICT"]
    assert errors[0].needs_human
    assert errors[0].brief() == "DECK_CONFLICT slot 2: plate, reservoir"


def test_bad_slot(assay, workcell, good_design):
    def move(d):
        d["deck"][3]["position"] = {"slot": 12}

    errors = run_rules(good_design, assay, edit(workcell, move))
    assert codes(errors) == ["BAD_SLOT"]
    assert errors[0].needs_human


def test_unknown_labware(assay, workcell, good_design):
    def rename(d):
        d["deck"][3]["labware"] = "reservoir_mystery"

    errors = run_rules(good_design, assay, edit(workcell, rename))
    assert "UNKNOWN_LABWARE" in codes(errors)
    assert all(e.needs_human for e in errors)


def test_missing_inventory(assay, workcell, good_design):
    errors = run_rules(good_design, assay, edit(workcell, lambda d: d["inventory"].pop("base")))
    assert codes(errors) == ["MISSING_INVENTORY"]
    assert errors[0].needs_human


def test_source_over_capacity(assay, workcell, good_design):
    def overload(d):
        d["inventory"]["solvent"]["volume_ul"] = 20000

    errors = run_rules(good_design, assay, edit(workcell, overload))
    assert codes(errors) == ["SOURCE_OVER_CAPACITY"]
    assert errors[0].needs_human


def test_source_short_carries_numbers(assay, workcell):
    design = make_design(
        grid={
            "catalyst_mol_pct": [10],
            "base_equiv": [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0],
            "cosolvent_pct": [0, 20, 40],
        },
        replicates=2,
    )
    errors = run_rules(design, assay, workcell)
    assert codes(errors) == ["SOURCE_SHORT"]
    e = errors[0]
    assert not e.needs_human
    assert e.message == (
        "The design needs 1328 uL of catalyst. 1080 uL is usable "
        "(1200 uL loaded, 10% held back as dead volume)."
    )
    assert e.options == ["reduce_wells", "lower_values", "ask_human_to_load_more"]


def test_volume_out_of_range(assay, workcell, good_design):
    def narrow(d):
        d["pipettes"][0]["min_ul"] = 5

    errors = run_rules(good_design, assay, edit(workcell, narrow))
    assert codes(errors) == ["VOLUME_OUT_OF_RANGE", "VOLUME_OUT_OF_RANGE"]
    assert {e.where for e in errors} == {"worklist:catalyst:2", "worklist:catalyst:4"}


def test_tips_short_gives_both_numbers(assay, workcell):
    design = make_design(
        grid={
            "catalyst_mol_pct": [1, 2, 3, 4, 5, 6, 7, 8, 9],
            "base_equiv": [0.5, 0.75],
            "cosolvent_pct": [20],
        },
        replicates=5,
    )
    errors = run_rules(design, assay, workcell)
    assert codes(errors) == ["TIPS_SHORT"]
    assert "182" in errors[0].message and "96" in errors[0].message


def test_unknown_instrument(assay, workcell, good_design):
    errors = run_rules(good_design, assay, edit(workcell, lambda d: d.update(instrument="robot_x")))
    assert codes(errors) == ["UNKNOWN_INSTRUMENT"]


def test_duplicate_deck_name(assay, workcell, good_design):
    def dup(d):
        d["deck"].append(
            {"name": "reservoir", "labware": "plate_96_flat_360ul", "position": {"slot": 5}}
        )

    errors = run_rules(good_design, assay, edit(workcell, dup))
    assert "DUPLICATE_NAME" in codes(errors)
    assert errors[0].needs_human


def test_two_pipettes_on_one_mount(assay, workcell, good_design):
    def same_mount(d):
        d["pipettes"][1]["mount"] = "left"

    assert codes(run_rules(good_design, assay, edit(workcell, same_mount))) == ["DUPLICATE_NAME"]


def test_two_reagents_in_one_source_well(assay, workcell, good_design):
    def share(d):
        d["inventory"]["catalyst"]["well"] = "A1"

    errors = run_rules(good_design, assay, edit(workcell, share))
    assert codes(errors) == ["SOURCE_CONFLICT"]
    assert "substrate, catalyst" in errors[0].message or "catalyst, substrate" in errors[0].message


def test_plan_plate_must_be_the_assay_plate(assay, workcell, good_design):
    plan = expand(good_design, assay).model_copy(update={"plate": "reservoir"})
    worklist = schedule(plan, workcell, assay)
    errors = validate_worklist(worklist, plan, workcell, assay)
    assert codes(errors) == ["UNKNOWN_LABWARE"]
    assert "reservoir_12_15ml" in errors[0].message


def test_boolean_slot_is_a_bad_slot(assay, workcell, good_design):
    def boolean(d):
        d["deck"][1]["position"] = {"slot": True}

    assert "BAD_SLOT" in codes(run_rules(good_design, assay, edit(workcell, boolean)))


def test_missing_tip_rack_is_not_also_tips_short(assay, workcell, good_design):
    def no_rack(d):
        d["pipettes"][0]["tips"] = "tips_none"

    assert codes(run_rules(good_design, assay, edit(workcell, no_rack))) == ["UNKNOWN_LABWARE"]


def test_bad_inventory_numbers_fail_when_the_file_loads(workcell):
    import pydantic
    import pytest

    for change in (
        lambda d: d["inventory"]["catalyst"].update(volume_ul=-5),
        lambda d: d.update(dead_volume_fraction=1.5),
    ):
        with pytest.raises(pydantic.ValidationError):
            edit(workcell, change)
