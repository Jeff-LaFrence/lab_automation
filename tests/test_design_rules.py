"""One test per design layer error code, each with a minimal bad input."""

from __future__ import annotations

from conftest import make_design

from labauto.validate import parse_design, validate_design


def codes(errors):
    return [e.code for e in errors]


def test_good_design_has_no_errors(assay, good_design):
    assert validate_design(good_design, assay) == []


def test_schema_invalid_lists_fields():
    design, errors = parse_design({"title": "x", "grid": {}, "replicates": "three", "colour": 1})
    assert design is None
    assert codes(errors) == ["SCHEMA_INVALID"]
    assert "replicates" in errors[0].where
    assert "colour" in errors[0].where


def test_schema_invalid_when_grid_and_conditions_both_set():
    _, errors = parse_design(
        {"grid": {"catalyst_mol_pct": [1]}, "conditions": [{"catalyst_mol_pct": 1}]}
    )
    assert codes(errors) == ["SCHEMA_INVALID"]


def test_unknown_factor(assay):
    design = make_design(
        grid={"catalyst_mol_pct": [1], "base_equiv": [1.0], "cosolvent_pct": [20], "temp_c": [40]}
    )
    errors = validate_design(design, assay)
    assert codes(errors) == ["UNKNOWN_FACTOR"]
    assert errors[0].where == "grid.temp_c"


def test_missing_factor(assay):
    design = make_design(
        grid=None,
        conditions=[{"catalyst_mol_pct": 1, "base_equiv": 1.0}],
    )
    errors = validate_design(design, assay)
    assert codes(errors) == ["MISSING_FACTOR"]
    assert "cosolvent_pct" in errors[0].message


def test_factor_out_of_range_gives_range(assay):
    design = make_design(
        grid={"catalyst_mol_pct": [15], "base_equiv": [1.0], "cosolvent_pct": [20]}
    )
    errors = validate_design(design, assay)
    assert codes(errors) == ["FACTOR_OUT_OF_RANGE"]
    assert "0.5 to 10.0 mol %" in errors[0].fix_hint


def test_factor_off_grid_gives_two_nearest(assay):
    design = make_design(
        grid={"catalyst_mol_pct": [2.2], "base_equiv": [1.0], "cosolvent_pct": [20]}
    )
    errors = validate_design(design, assay)
    assert codes(errors) == ["FACTOR_OFF_GRID"]
    assert "2.0 and 2.5" in errors[0].fix_hint
    assert errors[0].brief() == "FACTOR_OFF_GRID catalyst_mol_pct=2.2 (nearest 2.0, 2.5)"


def test_float_noise_is_not_off_grid(assay):
    design = make_design(
        grid={"catalyst_mol_pct": [1], "base_equiv": [1.15 + 0.1], "cosolvent_pct": [20]}
    )
    assert validate_design(design, assay) == []


def test_duplicate_condition(assay):
    cond = {"catalyst_mol_pct": 2, "base_equiv": 1.5, "cosolvent_pct": 20}
    design = make_design(grid=None, conditions=[cond, dict(cond)])
    errors = validate_design(design, assay)
    assert codes(errors) == ["DUPLICATE_CONDITION"]
    assert errors[0].where == "conditions[1]"


def test_duplicate_value_in_grid(assay):
    design = make_design(
        grid={"catalyst_mol_pct": [2, 2], "base_equiv": [1.0], "cosolvent_pct": [20]}
    )
    assert codes(validate_design(design, assay)) == ["DUPLICATE_CONDITION"]


def test_bad_replicates(assay):
    assert codes(validate_design(make_design(replicates=0), assay)) == ["BAD_REPLICATES"]


def test_empty_design(assay):
    assert codes(validate_design(make_design(grid=None, conditions=[]), assay)) == ["EMPTY_DESIGN"]
    assert codes(validate_design(make_design(grid=None), assay)) == ["EMPTY_DESIGN"]


def test_too_many_wells_gives_numbers_and_largest_replicates(assay):
    design = make_design(
        grid={
            "catalyst_mol_pct": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
            "base_equiv": [1.0, 1.5, 2.0, 2.5],
            "cosolvent_pct": [20],
        },
        replicates=3,
    )
    errors = validate_design(design, assay)
    assert codes(errors) == ["TOO_MANY_WELLS"]
    assert "120" in errors[0].message and "92" in errors[0].message
    assert "At most 2 replicates" in errors[0].message


def test_controls_off_frees_four_wells(assay):
    conds = [
        {"catalyst_mol_pct": c / 2, "base_equiv": 1.0, "cosolvent_pct": 20} for c in range(1, 21)
    ]
    on = make_design(grid=None, conditions=conds[:12], replicates=8)
    assert codes(validate_design(on, assay)) == ["TOO_MANY_WELLS"]
    off = make_design(grid=None, conditions=conds[:12], replicates=8, include_controls=False)
    assert validate_design(off, assay) == []


def test_every_error_is_returned(assay):
    design = make_design(
        grid={"catalyst_mol_pct": [2.2, 15], "base_equiv": [1.0], "cosolvent_pct": [20]},
        replicates=0,
    )
    assert codes(validate_design(design, assay)) == [
        "FACTOR_OUT_OF_RANGE",
        "FACTOR_OFF_GRID",
        "BAD_REPLICATES",
    ]


def test_duplicate_found_even_when_both_conditions_miss_a_factor(assay):
    cond = {"catalyst_mol_pct": 1, "base_equiv": 1.0}
    design = make_design(grid=None, conditions=[cond, dict(cond)])
    assert codes(validate_design(design, assay)) == [
        "MISSING_FACTOR",
        "MISSING_FACTOR",
        "DUPLICATE_CONDITION",
    ]


def test_values_that_give_the_same_volume_are_duplicates(assay):
    a = {"catalyst_mol_pct": 2.0, "base_equiv": 1.5, "cosolvent_pct": 20}
    b = {"catalyst_mol_pct": 2.0000001, "base_equiv": 1.5, "cosolvent_pct": 20}
    design = make_design(grid=None, conditions=[a, b])
    assert codes(validate_design(design, assay)) == ["DUPLICATE_CONDITION"]


def test_typo_inside_layout_fails_parsing():
    _, errors = parse_design({"grid": {}, "layout": {"strategy": "randomized", "sed": 7}})
    assert codes(errors) == ["SCHEMA_INVALID"]
    assert "layout.sed" in errors[0].where


def test_boolean_or_text_factor_value_fails_parsing():
    for bad in (True, "2"):
        _, errors = parse_design({"grid": {"catalyst_mol_pct": [bad]}})
        assert codes(errors) == ["SCHEMA_INVALID"]


def test_unknown_grid_factor_is_not_counted_in_too_many_wells(assay):
    design = make_design(
        grid={
            "catalyst_mol_pct": [1, 2],
            "base_equiv": [1.0],
            "cosolvent_pct": [20],
            "temp_c": list(range(60)),
        },
        replicates=2,
    )
    assert codes(validate_design(design, assay)) == ["UNKNOWN_FACTOR"]
