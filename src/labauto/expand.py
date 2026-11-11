"""Turn a Design into a Plan: the volume of every reagent in every well.

This is pure arithmetic. A factor value becomes a volume through value_per_ul,
fixed reagents get their set volume, and the fill reagent tops each well up to
the assay's total volume.
"""

from __future__ import annotations

import random

from labauto import labware
from labauto.config import factor_volume, sample_wells, snap_value
from labauto.models import Assay, Design, Plan, PlanError, PlanWell
from labauto.validate import design_conditions, validate_design


class DesignInvalid(ValueError):
    """Raised when expand is given a design that breaks the design rules."""

    def __init__(self, errors: list[PlanError]):
        super().__init__("; ".join(e.message for e in errors))
        self.errors = errors


def plan_id_for(design: Design) -> str:
    """The plan id that goes with a design id: d-... becomes p-..."""
    if design.design_id.startswith("d-"):
        return "p-" + design.design_id[2:]
    return "p-" + (design.design_id or "unnamed")


def well_volumes(assay: Assay, factor_values: dict[str, float]) -> dict[str, float]:
    """Volumes for one well, in addition order, from its factor values."""
    vols: dict[str, float] = {}
    for reagent, spec in assay.reagents.items():
        if spec.role == "fixed":
            vols[reagent] = float(spec.volume_ul or 0)
        elif spec.role == "factor":
            vols[reagent] = factor_volume(factor_values[spec.factor], assay.factors[spec.factor])
    return _with_fill(assay, vols)


def _with_fill(assay: Assay, vols: dict[str, float]) -> dict[str, float]:
    fill = [r for r, s in assay.reagents.items() if s.role == "fill"]
    out = dict(vols)
    for r in fill:
        out[r] = float(assay.total_volume_ul) - sum(v for k, v in vols.items() if k not in fill)
    order = assay.addition_order + [r for r in out if r not in assay.addition_order]
    return {r: out[r] for r in order if r in out}


def control_wells(assay: Assay) -> list[PlanWell]:
    """The control wells every plate carries, with their volumes."""
    out: list[PlanWell] = []
    for name, ctrl in assay.controls.items():
        role = "negative_control" if name == "negative" else "reference_control"
        if ctrl.conditions is not None:
            vols = well_volumes(assay, ctrl.conditions)
            conds = {k: snap_value(v, assay.factors[k]) for k, v in ctrl.conditions.items()}
        else:
            vols = _with_fill(assay, {k: float(v) for k, v in (ctrl.volumes_ul or {}).items()})
            conds = None
        for i, well in enumerate(ctrl.wells):
            out.append(
                PlanWell(
                    well=well,
                    role=role,
                    condition_index=None,
                    replicate=i + 1,
                    conditions=conds,
                    add_ul=vols,
                )
            )
    return out


def expand(design: Design, assay: Assay, check: bool = True) -> Plan:
    """Turn a design into a plan. Raises DesignInvalid if the design breaks a design rule."""
    if check:
        errors = validate_design(design, assay)
        if errors:
            raise DesignInvalid(errors)

    conditions = design_conditions(design, assay)
    wells: list[PlanWell] = []
    if design.include_controls:
        wells.extend(control_wells(assay))

    items = [(ci, rep) for ci in range(len(conditions)) for rep in range(1, design.replicates + 1)]
    free = sample_wells(assay, design.include_controls)[: len(items)]
    if design.layout.strategy == "randomized":
        random.Random(design.layout.seed).shuffle(items)

    for well, (ci, rep) in zip(free, items, strict=False):
        cond = {f: snap_value(conditions[ci][f], spec) for f, spec in assay.factors.items()}
        wells.append(
            PlanWell(
                well=well,
                role="sample",
                condition_index=ci,
                replicate=rep,
                conditions=cond,
                add_ul=well_volumes(assay, cond),
            )
        )

    wells.sort(key=lambda w: labware.column_order_key(w.well))
    return Plan(
        plan_id=plan_id_for(design),
        design_id=design.design_id or None,
        assay_id=assay.assay_id,
        plate="plate",
        wells=wells,
        totals_ul=plan_totals(wells, assay),
    )


def plan_totals(wells: list[PlanWell], assay: Assay) -> dict[str, float]:
    """Total volume of each reagent across the plate, in addition order."""
    totals: dict[str, float] = {r: 0.0 for r in assay.addition_order}
    for w in wells:
        for r, v in w.add_ul.items():
            totals[r] = totals.get(r, 0.0) + v
    return totals
