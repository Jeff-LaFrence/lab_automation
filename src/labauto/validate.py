"""Rules for designs, plans, and worklists.

Each function returns every error it finds in its layer, not only the first.
Every error carries the numbers involved and a hint, so a person or a model
can fix the input without guessing (principle P3).
"""

from __future__ import annotations

import json
import math
from collections import defaultdict

from pydantic import ValidationError

from labauto import labware
from labauto.config import (
    TOLERANCE,
    allowed_values,
    condition_key,
    fmt,
    fmt_ul,
    in_range,
    nearest_allowed,
    on_grid,
    sample_wells,
)
from labauto.models import Assay, Design, FactorSpec, Plan, PlanError, Workcell, Worklist

DESIGN_ORDER = [
    "SCHEMA_INVALID",
    "EMPTY_DESIGN",
    "UNKNOWN_FACTOR",
    "MISSING_FACTOR",
    "FACTOR_OUT_OF_RANGE",
    "FACTOR_OFF_GRID",
    "DUPLICATE_CONDITION",
    "BAD_REPLICATES",
    "TOO_MANY_WELLS",
]
PLAN_ORDER = [
    "SCHEMA_INVALID",
    "EMPTY_PLAN",
    "UNKNOWN_REAGENT",
    "BAD_WELL_NAME",
    "DUPLICATE_WELL",
    "NEGATIVE_VOLUME",
    "WELL_OVERFILL",
    "WELL_TOTAL_MISMATCH",
    "TOTALS_MISMATCH",
]
WORKCELL_ORDER = [
    "UNKNOWN_INSTRUMENT",
    "DUPLICATE_NAME",
    "DECK_CONFLICT",
    "BAD_SLOT",
    "UNKNOWN_LABWARE",
    "MISSING_INVENTORY",
    "SOURCE_CONFLICT",
    "SOURCE_OVER_CAPACITY",
    "SOURCE_SHORT",
    "VOLUME_OUT_OF_RANGE",
    "TIPS_SHORT",
]


def _ordered(errors: list[PlanError], order: list[str]) -> list[PlanError]:
    rank = {code: i for i, code in enumerate(order)}
    return sorted(errors, key=lambda e: rank.get(e.code, len(order)))


def _schema_error(layer: str, kind: str, exc: ValidationError) -> PlanError:
    problems = []
    fields = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(top level)"
        fields.append(loc)
        problems.append(f"{loc}: {err['msg']}")
    return PlanError(
        code="SCHEMA_INVALID",
        layer=layer,  # type: ignore[arg-type]
        where=", ".join(fields),
        message=f"The {kind} does not match the expected format. Problems: " + "; ".join(problems),
        fix_hint=f"Write the {kind} as one JSON object with only the documented fields.",
        options=["fix_format"],
        short=", ".join(fields),
    )


def parse_design(data: object) -> tuple[Design | None, list[PlanError]]:
    """Turn raw JSON into a Design, or a SCHEMA_INVALID error naming the bad fields."""
    try:
        return Design.model_validate(data), []
    except ValidationError as exc:
        return None, [_schema_error("design", "design", exc)]


def parse_plan(data: object) -> tuple[Plan | None, list[PlanError]]:
    """Turn raw JSON into a Plan, or a SCHEMA_INVALID error naming the bad fields."""
    try:
        return Plan.model_validate(data), []
    except ValidationError as exc:
        return None, [_schema_error("plan", "plan", exc)]


# ---------------------------------------------------------------- design rules


def _check_value(factor: str, value: float, spec: FactorSpec, where: str) -> list[PlanError]:
    values = allowed_values(spec)
    lo, hi = values[0], values[-1]
    step = spec.step_ul * spec.value_per_ul
    range_text = f"{fmt(lo)} to {fmt(hi)} {spec.unit} in steps of {fmt(step)}"
    if math.isnan(value) or not in_range(value, spec):
        return [
            PlanError(
                code="FACTOR_OUT_OF_RANGE",
                layer="design",
                where=where,
                message=f"{factor}={fmt(value)} {spec.unit} is outside the allowed range.",
                fix_hint=f"Allowed {factor} values run from {range_text}.",
                options=["clamp_to_range", "drop_value"],
                short=f"{factor}={fmt(value)} (allowed {fmt(lo)} to {fmt(hi)})",
            )
        ]
    if not on_grid(value, spec):
        near = nearest_allowed(value, spec)
        near_text = " and ".join(f"{fmt(v)}" for v in near)
        return [
            PlanError(
                code="FACTOR_OFF_GRID",
                layer="design",
                where=where,
                message=(
                    f"{factor}={fmt(value)} {spec.unit} is inside the range "
                    f"but is not an allowed step ({range_text})."
                ),
                fix_hint=f"The nearest allowed values are {near_text} {spec.unit}.",
                options=["round_to_nearest"],
                short=f"{factor}={fmt(value)} (nearest {', '.join(fmt(v) for v in near)})",
            )
        ]
    return []


def design_conditions(design: Design, assay: Assay) -> list[dict[str, float]]:
    """The design's conditions as a list. A grid becomes its full factorial.

    The grid is expanded in the assay's factor order, so condition 0 takes the
    first listed value of every factor.
    """
    if design.conditions is not None:
        return [dict(c) for c in design.conditions]
    if not design.grid:
        return []
    factors = [f for f in assay.factors if f in design.grid]
    factors += [f for f in design.grid if f not in assay.factors]
    out: list[dict[str, float]] = [{}]
    for f in factors:
        out = [{**c, f: float(v)} for c in out for v in design.grid[f]]
    return out


def validate_design(design: Design, assay: Assay) -> list[PlanError]:
    """Design rules: known factors, allowed values, no duplicates, and the wells fit."""
    errors: list[PlanError] = []
    factors = list(assay.factors)

    if design.assay_id and design.assay_id != assay.assay_id:
        errors.append(
            PlanError(
                code="SCHEMA_INVALID",
                layer="design",
                where="assay_id",
                message=(
                    f"The design was written for assay '{design.assay_id}', "
                    f"but the assay loaded is '{assay.assay_id}'."
                ),
                fix_hint="Check the design against the assay it was written for.",
                options=["fix_format"],
                needs_human=True,
                short=f"assay_id {design.assay_id}",
            )
        )

    if design.replicates < 1:
        errors.append(
            PlanError(
                code="BAD_REPLICATES",
                layer="design",
                where="replicates",
                message=f"replicates is {design.replicates}. It must be 1 or more.",
                fix_hint=f"Use at least 1 replicate. The assay default is {assay.default_replicates}.",
                options=["set_replicates"],
                short=f"replicates={design.replicates}",
            )
        )

    n_conditions = 0
    if design.grid is not None:
        if not design.grid:
            errors.append(_empty("grid", "The grid lists no factors."))
        for key, values in design.grid.items():
            if key not in assay.factors:
                errors.append(_unknown_factor(key, f"grid.{key}", factors))
                continue
            if not values:
                errors.append(_empty(f"grid.{key}", f"The grid lists no values for {key}."))
            seen: dict[tuple, int] = {}
            for i, v in enumerate(values):
                where = f"grid.{key}[{i}]"
                errors.extend(_check_value(key, v, assay.factors[key], where))
                k = condition_key({key: v}, {key: assay.factors[key]})
                if k in seen:
                    errors.append(
                        PlanError(
                            code="DUPLICATE_CONDITION",
                            layer="design",
                            where=where,
                            message=(
                                f"The grid lists {key}={fmt(v)} twice "
                                f"(positions {seen[k]} and {i}), so every condition "
                                "with it would appear twice."
                            ),
                            fix_hint=f"List each {key} value once.",
                            options=["remove_duplicate"],
                            short=f"{key}={fmt(v)} listed twice",
                        )
                    )
                else:
                    seen[k] = i
        for f in factors:
            if f not in design.grid:
                errors.append(_missing_factor(f, "grid", assay))
        known = [v for k, v in design.grid.items() if k in assay.factors]
        if known and all(known):
            n_conditions = math.prod(len(v) for v in known)
    elif design.conditions is not None:
        if not design.conditions:
            errors.append(_empty("conditions", "The conditions list is empty."))
        first_seen: dict[tuple, int] = {}
        for i, cond in enumerate(design.conditions):
            for key in cond:
                if key not in assay.factors:
                    errors.append(_unknown_factor(key, f"conditions[{i}].{key}", factors))
            for f in factors:
                if f not in cond:
                    errors.append(_missing_factor(f, f"conditions[{i}]", assay))
                else:
                    errors.extend(
                        _check_value(f, cond[f], assay.factors[f], f"conditions[{i}].{f}")
                    )
            key = condition_key(cond, assay.factors)
            if key in first_seen:
                text = ", ".join(f"{f}={fmt(cond[f])}" for f in factors if f in cond)
                errors.append(
                    PlanError(
                        code="DUPLICATE_CONDITION",
                        layer="design",
                        where=f"conditions[{i}]",
                        message=(
                            f"conditions[{i}] ({text}) is the same as "
                            f"conditions[{first_seen[key]}]."
                        ),
                        fix_hint=(
                            "List each condition once. Use replicates to repeat a condition, "
                            "or replace the duplicate with a different allowed condition."
                        ),
                        options=["remove_duplicate", "replace_condition"],
                        short=f"conditions[{i}] repeats conditions[{first_seen[key]}]",
                    )
                )
            else:
                first_seen[key] = i
        n_conditions = len(design.conditions)
    else:
        errors.append(_empty("grid", "The design sets neither grid nor conditions."))

    if n_conditions > 0 and design.replicates >= 1:
        available = len(sample_wells(assay, design.include_controls))
        needed = n_conditions * design.replicates
        if needed > available:
            fit = available // n_conditions
            if fit >= 1:
                fit_text = f"At most {fit} replicate{'s' if fit != 1 else ''} of {n_conditions} conditions fit."
                hint = f"Use {fit} replicate{'s' if fit != 1 else ''}, or fewer conditions."
            else:
                fit_text = f"Not even 1 replicate of {n_conditions} conditions fits."
                hint = f"Use at most {available} conditions."
            controls = " with controls on" if design.include_controls else ""
            errors.append(
                PlanError(
                    code="TOO_MANY_WELLS",
                    layer="design",
                    where="conditions x replicates",
                    message=(
                        f"The design needs {needed} sample wells ({n_conditions} conditions x "
                        f"{design.replicates} replicates). {available} sample wells are "
                        f"available{controls}. {fit_text}"
                    ),
                    fix_hint=hint,
                    options=["reduce_replicates", "reduce_conditions"],
                    short=f"{needed} needed, {available} available",
                )
            )
    return _ordered(errors, DESIGN_ORDER)


def _empty(where: str, text: str) -> PlanError:
    return PlanError(
        code="EMPTY_DESIGN",
        layer="design",
        where=where,
        message=text + " A design needs at least one condition.",
        fix_hint="Give a grid with at least one value per factor, or a list of conditions.",
        options=["add_conditions"],
        short=where,
    )


def _unknown_factor(key: str, where: str, factors: list[str]) -> PlanError:
    return PlanError(
        code="UNKNOWN_FACTOR",
        layer="design",
        where=where,
        message=f"'{key}' is not a factor in this assay.",
        fix_hint=f"Use only these factors: {', '.join(factors)}.",
        options=["remove_factor", "rename_factor"],
        short=key,
    )


def _missing_factor(factor: str, where: str, assay: Assay) -> PlanError:
    spec = assay.factors[factor]
    values = allowed_values(spec)
    return PlanError(
        code="MISSING_FACTOR",
        layer="design",
        where=where,
        message=f"{where} does not give a value for {factor}. Every condition must set every factor.",
        fix_hint=(
            f"Add {factor} with a value from {fmt(values[0])} to {fmt(values[-1])} {spec.unit}."
        ),
        options=["add_factor"],
        short=f"{factor} at {where}",
    )


# ---------------------------------------------------------------- plan rules


def validate_plan(plan: Plan, assay: Assay) -> list[PlanError]:
    """Plan rules: real wells, known reagents, no negative volumes, wells not overfilled."""
    errors: list[PlanError] = []
    spec = labware.get(assay.plate_labware)
    valid = set(labware.well_names(assay.plate_labware)) if spec else set()
    capacity = spec.capacity_ul if spec else float("inf")
    limit = min(assay.max_well_volume_ul, capacity)
    fill = [r for r, s in assay.reagents.items() if s.role == "fill"]
    if plan.assay_id != assay.assay_id:
        errors.append(
            PlanError(
                code="SCHEMA_INVALID",
                layer="plan",
                where="assay_id",
                message=(
                    f"The plan was written for assay '{plan.assay_id}', "
                    f"but the assay loaded is '{assay.assay_id}'."
                ),
                fix_hint="Check the plan against the assay it was written for.",
                options=["fix_format"],
                needs_human=True,
                short=f"assay_id {plan.assay_id}",
            )
        )
    if not plan.wells:
        errors.append(
            PlanError(
                code="EMPTY_PLAN",
                layer="plan",
                where="wells",
                message="The plan lists no wells, so there is nothing to run.",
                fix_hint="Add at least one well, or start from a design.",
                options=["add_wells"],
                short="no wells",
            )
        )
    seen: set[str] = set()
    for w in plan.wells:
        place = f"{plan.plate}:{w.well}"
        for reagent in w.add_ul:
            if reagent not in assay.reagents:
                errors.append(
                    PlanError(
                        code="UNKNOWN_REAGENT",
                        layer="plan",
                        where=place,
                        message=f"Well {w.well} adds '{reagent}', which is not a reagent in this assay.",
                        fix_hint=f"Use only these reagents: {', '.join(assay.reagents)}.",
                        options=["remove_reagent"],
                        short=f"{reagent} in {w.well}",
                    )
                )
        if w.well not in valid:
            errors.append(
                PlanError(
                    code="BAD_WELL_NAME",
                    layer="plan",
                    where=place,
                    message=f"'{w.well}' is not a well on {assay.plate_labware}.",
                    fix_hint="Use well names from A1 to H12.",
                    options=["rename_well"],
                    short=place,
                )
            )
        if w.well in seen:
            errors.append(
                PlanError(
                    code="DUPLICATE_WELL",
                    layer="plan",
                    where=place,
                    message=f"Well {w.well} appears more than once in the plan.",
                    fix_hint="List each well once.",
                    options=["remove_duplicate"],
                    short=place,
                )
            )
        seen.add(w.well)
        others = sum(v for r, v in w.add_ul.items() if r not in fill)
        target = assay.total_volume_ul
        for reagent, vol in w.add_ul.items():
            if vol < 0:
                if reagent in fill and others > target + TOLERANCE:
                    text = (
                        f"The {reagent} fill in {w.well} is {fmt_ul(vol)} uL because the other "
                        f"additions total {fmt_ul(others)} uL, above the {fmt_ul(target)} uL target."
                    )
                    hint = "Lower the other additions so they fit inside the well total."
                elif reagent in fill:
                    text = (
                        f"The {reagent} fill in {w.well} is {fmt_ul(vol)} uL. The other additions "
                        f"total {fmt_ul(others)} uL against a {fmt_ul(target)} uL target, so the "
                        f"fill should be {fmt_ul(target - others)} uL."
                    )
                    hint = f"Set the {reagent} fill to {fmt_ul(target - others)} uL."
                else:
                    text = f"Well {w.well} adds {fmt_ul(vol)} uL of {reagent}."
                    hint = f"Use a {reagent} volume of zero or more."
                errors.append(
                    PlanError(
                        code="NEGATIVE_VOLUME",
                        layer="plan",
                        where=f"{place}:{reagent}",
                        message=text + " Volumes cannot be below zero.",
                        fix_hint=hint,
                        options=["lower_values"],
                        short=f"{place} {reagent} {fmt_ul(vol)} uL",
                    )
                )
        total = w.total_ul()
        if total > limit + TOLERANCE:
            errors.append(
                PlanError(
                    code="WELL_OVERFILL",
                    layer="plan",
                    where=place,
                    message=(
                        f"Well {w.well} would hold {fmt_ul(total)} uL. The limit is "
                        f"{fmt_ul(limit)} uL (the assay allows {fmt_ul(assay.max_well_volume_ul)} "
                        f"uL per well and the plate holds {fmt_ul(capacity)} uL)."
                    ),
                    fix_hint=f"Bring the well total to {fmt_ul(assay.total_volume_ul)} uL.",
                    options=["lower_values"],
                    short=f"{place} {fmt_ul(total)} uL, limit {fmt_ul(limit)}",
                )
            )
        if w.role == "sample" and abs(total - assay.total_volume_ul) > TOLERANCE:
            errors.append(
                PlanError(
                    code="WELL_TOTAL_MISMATCH",
                    layer="plan",
                    where=place,
                    message=(
                        f"Sample well {w.well} totals {fmt_ul(total)} uL. Every sample well "
                        f"must total {fmt_ul(assay.total_volume_ul)} uL."
                    ),
                    fix_hint="Adjust the fill so the total matches.",
                    options=["fix_fill"],
                    short=f"{place} {fmt_ul(total)} uL, expected {fmt_ul(assay.total_volume_ul)}",
                )
            )
    sums: dict[str, float] = {}
    for w in plan.wells:
        for r, v in w.add_ul.items():
            sums[r] = sums.get(r, 0.0) + v
    for reagent in sorted(set(sums) | set(plan.totals_ul)):
        listed, actual = plan.totals_ul.get(reagent, 0.0), sums.get(reagent, 0.0)
        if abs(listed - actual) > TOLERANCE:
            errors.append(
                PlanError(
                    code="TOTALS_MISMATCH",
                    layer="plan",
                    where=f"totals_ul:{reagent}",
                    message=(
                        f"totals_ul lists {fmt_ul(listed)} uL of {reagent}, "
                        f"but the wells add up to {fmt_ul(actual)} uL."
                    ),
                    fix_hint="Recompute totals_ul from the wells, or leave it out and code will add it.",
                    options=["fix_totals"],
                    short=f"{reagent} listed {fmt_ul(listed)}, wells {fmt_ul(actual)}",
                )
            )
    return _ordered(errors, PLAN_ORDER)


# ---------------------------------------------------------------- workcell rules


def position_label(position: dict) -> str:
    """Readable name for a deck position, for example 'slot 2'."""
    return ", ".join(f"{k} {v}" for k, v in position.items())


def validate_worklist(
    worklist: Worklist, plan: Plan, workcell: Workcell, assay: Assay | None = None
) -> list[PlanError]:
    """Workcell rules: the deck is sound, reagents and tips suffice, pipettes reach the volumes."""
    from labauto.instruments import get_instrument

    errors: list[PlanError] = []
    try:
        inst = get_instrument(workcell.instrument)
    except KeyError:
        return [
            PlanError(
                code="UNKNOWN_INSTRUMENT",
                layer="workcell",
                where="instrument",
                message=f"The workcell names instrument '{workcell.instrument}', which has no backend.",
                fix_hint="Use a workcell for an instrument this project supports.",
                options=["ask_human_to_fix_workcell"],
                needs_human=True,
                short=workcell.instrument,
            )
        ]

    by_position: dict[str, list[str]] = defaultdict(list)
    labels: dict[str, str] = {}
    for entry in workcell.deck:
        key = json.dumps(entry.position, sort_keys=True)
        by_position[key].append(entry.name)
        labels[key] = position_label(entry.position)
    for key, names in by_position.items():
        if len(names) > 1:
            errors.append(
                PlanError(
                    code="DECK_CONFLICT",
                    layer="workcell",
                    where=f"deck:{labels[key]}",
                    message=f"{len(names)} pieces of labware are placed at {labels[key]}: {', '.join(names)}.",
                    fix_hint="Move one of them in the workcell file. No change to the design can fix this.",
                    options=["ask_human_to_fix_workcell"],
                    needs_human=True,
                    short=f"{labels[key]}: {', '.join(names)}",
                )
            )

    for kind, names in (
        ("deck entry name", [e.name for e in workcell.deck]),
        ("pipette name", [p.name for p in workcell.pipettes]),
        ("pipette mount", [p.mount for p in workcell.pipettes]),
    ):
        for name in sorted({n for n in names if names.count(n) > 1}):
            errors.append(
                PlanError(
                    code="DUPLICATE_NAME",
                    layer="workcell",
                    where=f"{kind.split()[0]}:{name}",
                    message=f"The {kind} '{name}' is used {names.count(name)} times in the workcell.",
                    fix_hint="Give each one its own name in the workcell file.",
                    options=["ask_human_to_fix_workcell"],
                    needs_human=True,
                    short=f"{kind} {name}",
                )
            )

    lmap = inst.labware_map()
    for entry in workcell.deck:
        if not inst.valid_position(entry.position):
            errors.append(
                PlanError(
                    code="BAD_SLOT",
                    layer="workcell",
                    where=f"deck:{entry.name}",
                    message=(
                        f"{entry.name} is placed at {position_label(entry.position)}, "
                        f"which is not a valid position on {inst.name}."
                    ),
                    fix_hint="Fix the position in the workcell file.",
                    options=["ask_human_to_fix_workcell"],
                    needs_human=True,
                    short=f"{entry.name} at {position_label(entry.position)}",
                )
            )
        if entry.labware not in lmap or labware.get(entry.labware) is None:
            errors.append(
                _unknown_labware(f"deck:{entry.name}", f"labware '{entry.labware}'", inst.name)
            )

    pmap = inst.pipette_map()
    for pip in workcell.pipettes:
        if pip.model not in pmap:
            errors.append(
                _unknown_labware(f"pipette:{pip.name}", f"pipette model '{pip.model}'", inst.name)
            )
        if workcell.deck_entry(pip.tips) is None:
            errors.append(
                _unknown_labware(
                    f"pipette:{pip.name}", f"tip rack '{pip.tips}' (not on the deck)", inst.name
                )
            )
    plate_entry = workcell.deck_entry(plan.plate)
    if plate_entry is None:
        errors.append(
            _unknown_labware(
                f"plate:{plan.plate}", f"plate '{plan.plate}' (not on the deck)", inst.name
            )
        )
    elif assay is not None and plate_entry.labware != assay.plate_labware:
        errors.append(
            PlanError(
                code="UNKNOWN_LABWARE",
                layer="workcell",
                where=f"plate:{plan.plate}",
                message=(
                    f"The plan dispenses into '{plan.plate}', which is {plate_entry.labware} on "
                    f"this deck. The assay runs on {assay.plate_labware}."
                ),
                fix_hint="Point the plan at the assay plate, or fix the workcell file.",
                options=["ask_human_to_fix_workcell"],
                needs_human=True,
                short=f"plate:{plan.plate} is {plate_entry.labware}",
            )
        )

    by_source: dict[tuple[str, str], list[str]] = defaultdict(list)
    for reagent, inv in workcell.inventory.items():
        by_source[(inv.labware, inv.well)].append(reagent)
    for (lw, well), reagents in by_source.items():
        if len(reagents) > 1:
            errors.append(
                PlanError(
                    code="SOURCE_CONFLICT",
                    layer="workcell",
                    where=f"source:{lw}:{well}",
                    message=f"{', '.join(reagents)} are all listed in {lw} {well}. Each source well holds one reagent.",
                    fix_hint="Give each reagent its own source well in the workcell file.",
                    options=["ask_human_to_fix_workcell"],
                    needs_human=True,
                    short=f"{lw}:{well} {', '.join(reagents)}",
                )
            )

    drawn: dict[str, float] = defaultdict(float)
    for row in worklist.rows:
        drawn[row.reagent] += row.volume_ul

    for reagent in drawn:
        if reagent not in workcell.inventory:
            errors.append(
                PlanError(
                    code="MISSING_INVENTORY",
                    layer="workcell",
                    where=f"inventory:{reagent}",
                    message=f"The plan needs {fmt_ul(drawn[reagent])} uL of {reagent}, but none is loaded.",
                    fix_hint=f"Load {reagent} on the deck and list it in the workcell inventory.",
                    options=["ask_human_to_load"],
                    needs_human=True,
                    short=reagent,
                )
            )

    for reagent, inv in workcell.inventory.items():
        src = workcell.deck_entry(inv.labware)
        if src is None:
            errors.append(
                _unknown_labware(
                    f"inventory:{reagent}",
                    f"source labware '{inv.labware}' (not on the deck)",
                    inst.name,
                )
            )
            continue
        spec = labware.get(src.labware)
        if spec is None:
            continue
        if inv.well not in labware.well_names(src.labware):
            errors.append(
                _unknown_labware(
                    f"inventory:{reagent}", f"well '{inv.well}' on {inv.labware}", inst.name
                )
            )
        if inv.volume_ul > spec.capacity_ul + TOLERANCE:
            errors.append(
                PlanError(
                    code="SOURCE_OVER_CAPACITY",
                    layer="workcell",
                    where=f"inventory:{reagent}",
                    message=(
                        f"{fmt_ul(inv.volume_ul)} uL of {reagent} is listed in {inv.labware} "
                        f"{inv.well}, which holds {fmt_ul(spec.capacity_ul)} uL."
                    ),
                    fix_hint="Fix the loaded volume in the workcell file.",
                    options=["ask_human_to_fix_workcell"],
                    needs_human=True,
                    short=f"{reagent} {fmt_ul(inv.volume_ul)} uL, holds {fmt_ul(spec.capacity_ul)}",
                )
            )

    dead = workcell.dead_volume_fraction
    for reagent, need in drawn.items():
        inv = workcell.inventory.get(reagent)
        if inv is None:
            continue
        usable = inv.volume_ul * (1 - dead)
        if need > usable + TOLERANCE:
            is_factor = bool(
                assay and assay.reagents.get(reagent) and assay.reagents[reagent].role == "factor"
            )
            if is_factor:
                hint = f"Use fewer wells or lower {reagent} amounts, or ask for more {reagent} to be loaded."
                options = ["reduce_wells", "lower_values", "ask_human_to_load_more"]
            else:
                hint = f"Use fewer wells, or ask for more {reagent} to be loaded."
                options = ["reduce_wells", "ask_human_to_load_more"]
            errors.append(
                PlanError(
                    code="SOURCE_SHORT",
                    layer="workcell",
                    where=f"inventory:{reagent}",
                    message=(
                        f"The design needs {fmt_ul(need)} uL of {reagent}. {fmt_ul(usable)} uL is usable "
                        f"({fmt_ul(inv.volume_ul)} uL loaded, {dead * 100:g}% held back as dead volume)."
                    ),
                    fix_hint=hint,
                    options=options,
                    short=f"{reagent} {fmt_ul(need)} uL needed, {fmt_ul(usable)} usable",
                )
            )

    ranges = ", ".join(
        f"{p.name} {fmt_ul(p.min_ul)} to {fmt_ul(p.max_ul)} uL" for p in workcell.pipettes
    )
    bad: dict[tuple[str, float], list[str]] = defaultdict(list)
    for row in worklist.rows:
        pip = workcell.pipette(row.pipette)
        if pip is None or not (pip.min_ul - TOLERANCE <= row.volume_ul <= pip.max_ul + TOLERANCE):
            bad[(row.reagent, row.volume_ul)].append(row.dest_well)
    for (reagent, vol), wells in bad.items():
        shown = ", ".join(wells[:6]) + (", ..." if len(wells) > 6 else "")
        errors.append(
            PlanError(
                code="VOLUME_OUT_OF_RANGE",
                layer="workcell",
                where=f"worklist:{reagent}:{fmt_ul(vol)}",
                message=(
                    f"{fmt_ul(vol)} uL of {reagent} into {len(wells)} well(s) ({shown}): no pipette "
                    f"on this workcell covers that volume. Pipette ranges: {ranges}."
                ),
                fix_hint="Change the values so every addition falls inside a pipette's range.",
                options=["change_values"],
                short=f"{reagent} {fmt_ul(vol)} uL",
            )
        )

    needed_tips: dict[str, int] = defaultdict(int)
    for row in worklist.rows:
        pip = workcell.pipette(row.pipette)
        if pip is not None and row.tip == "new":
            needed_tips[pip.tips] += 1
    for rack, need in needed_tips.items():
        entry = workcell.deck_entry(rack)
        spec = labware.get(entry.labware) if entry else None
        if spec is None:
            continue  # already reported as UNKNOWN_LABWARE
        have = spec.count
        if need > have:
            errors.append(
                PlanError(
                    code="TIPS_SHORT",
                    layer="workcell",
                    where=f"tips:{rack}",
                    message=f"The protocol needs {need} tips from {rack}. {have} are on the deck.",
                    fix_hint="Use fewer wells with small additions, or ask for another tip rack.",
                    options=["reduce_wells", "ask_human_to_add_tips"],
                    short=f"{rack} {need} needed, {have} on deck",
                )
            )
    return _ordered(errors, WORKCELL_ORDER)


def _unknown_labware(where: str, what: str, instrument: str) -> PlanError:
    return PlanError(
        code="UNKNOWN_LABWARE",
        layer="workcell",
        where=where,
        message=f"The {instrument} backend cannot map {what}.",
        fix_hint="Fix the workcell file. No change to the design can fix this.",
        options=["ask_human_to_fix_workcell"],
        needs_human=True,
        short=f"{where} {what}",
    )
