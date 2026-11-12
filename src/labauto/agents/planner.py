"""The planner role: build the context a model sees, and turn its reply into a Design.

The context is written by code from the assay and workcell files, so the model
works from the same numbers the checks use. The model writes only the short
design fields. Code fills ids, layout, and who wrote it (principle P1).
"""

from __future__ import annotations

import copy
import json
from importlib import resources

from labauto import labware
from labauto.config import allowed_values, fmt, fmt_ul, sample_wells
from labauto.models import Assay, AttemptRecord, CreatedBy, Design, Layout, Workcell


def load_prompt(name: str) -> str:
    """Read a prompt file from labauto/agents/prompts/."""
    return resources.files("labauto.agents.prompts").joinpath(name).read_text(encoding="utf-8")


def assay_summary(assay: Assay) -> str:
    """Factors with units and allowed values, defaults, controls, and wells available."""
    lines = [f"Assay {assay.assay_id}. {assay.description}".strip(), ""]
    lines.append("Factors you may vary, with every allowed value:")
    for name, spec in assay.factors.items():
        values = ", ".join(fmt(v) for v in allowed_values(spec))
        per_ul = fmt(spec.value_per_ul)
        reagent = next((r for r, s in assay.reagents.items() if s.factor == name), name)
        lines.append(f"- {name} ({spec.unit}): {values}")
        lines.append(f"  Each 1 uL of {reagent} per well gives {per_ul} {spec.unit}.")
    fixed = [
        f"{r} {fmt_ul(s.volume_ul or 0)} uL" for r, s in assay.reagents.items() if s.role == "fixed"
    ]
    fill = [r for r, s in assay.reagents.items() if s.role == "fill"]
    lines.append("")
    lines.append(
        f"Every sample well holds {fmt_ul(assay.total_volume_ul)} uL: "
        f"{', '.join(fixed) or 'no fixed reagents'}, the factor reagents, "
        f"and {', '.join(fill) or 'no fill'} to top up."
    )
    lines.append(f"Default replicates: {assay.default_replicates}.")
    if assay.controls:
        lines.append("Controls, placed on every plate when include_controls is true:")
        for name, ctrl in assay.controls.items():
            if ctrl.conditions:
                what = ", ".join(f"{k} {fmt(v)}" for k, v in ctrl.conditions.items())
            else:
                what = (
                    "no catalyst"
                    if (ctrl.volumes_ul or {}).get("catalyst") == 0
                    else "fixed volumes"
                )
            lines.append(f"- {name}: wells {', '.join(ctrl.wells)}, {what}")
    on = len(sample_wells(assay, True))
    off = len(sample_wells(assay, False))
    lines.append(
        f"Sample wells available: {on} with controls on, {off} with controls off. "
        "Conditions times replicates must not exceed this."
    )
    return "\n".join(lines)


def workcell_summary(workcell: Workcell, assay: Assay) -> str:
    """Usable reagent volumes, tips per type, and the small transfer rule."""
    lines = [f"Workcell {workcell.workcell_id} ({workcell.instrument})."]
    dead = workcell.dead_volume_fraction
    lines.append(f"Usable volume per reagent ({dead * 100:g}% of each source is held back):")
    for reagent, inv in workcell.inventory.items():
        lines.append(f"- {reagent}: {fmt_ul(inv.volume_ul * (1 - dead))} uL")
    lines.append("Tips, per rack:")
    for rack in dict.fromkeys(p.tips for p in workcell.pipettes):
        entry = workcell.deck_entry(rack)
        spec = labware.get(entry.labware) if entry else None
        count = spec.count if spec else 0
        users = [p for p in workcell.pipettes if p.tips == rack]
        who = " and ".join(
            f"the {p.name} pipette ({fmt_ul(p.min_ul)} to {fmt_ul(p.max_ul)} uL)" for p in users
        )
        lines.append(f"- {rack}: {count} tips, used by {who}")
    threshold = fmt_ul(workcell.liquid_handling.contact_dispense_below_ul)
    lines.append(
        f"Small transfer rule: every addition below {threshold} uL uses a fresh tip, "
        "so each such addition in each well uses one tip."
    )
    return "\n".join(lines)


def attempt_text(a: AttemptRecord) -> str:
    """One earlier attempt: its design and its errors as structured data."""
    head = f"Attempt {a.number} ({a.endpoint_label}, {a.rung} rung):"
    if a.design is None:
        body = f"Your reply could not be read. {a.reply_error or ''}".strip()
    else:
        body = "Design:\n" + json.dumps(a.design)
    errors = "\n".join(
        json.dumps(
            e.model_dump(
                include={"code", "layer", "where", "message", "fix_hint", "options", "needs_human"}
            )
        )
        for e in a.errors
    )
    return f"{head}\n{body}\nErrors:\n{errors or '(none)'}"


def build_messages(
    assay: Assay,
    workcell: Workcell,
    request: str,
    attempts: list[AttemptRecord],
    answers: list[str] | None = None,
    trim: bool = False,
) -> list[dict]:
    """The full message list for one planner call."""
    parts = [
        "ASSAY\n" + assay_summary(assay),
        "WORKCELL\n" + workcell_summary(workcell, assay),
        "REQUEST\n" + request.strip(),
    ]
    shown = list(reversed(attempts))
    if trim and shown:
        shown = shown[:1]
    if shown:
        label = "EARLIER ATTEMPTS, newest first"
        if trim and len(attempts) > 1:
            label += f" (trimmed to the latest of {len(attempts)} to fit the context)"
        parts.append(label + "\n" + "\n\n".join(attempt_text(a) for a in shown))
        parts.append(
            "Write a new design that fixes these errors. Change only what the errors require."
        )
    if answers:
        parts.append("ANSWERS FROM A PERSON\n" + "\n".join(f"- {x}" for x in answers))
    return [
        {"role": "system", "content": load_prompt("planner_system.md")},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def to_design(
    parsed: dict,
    assay: Assay,
    workcell: Workcell,
    design_id: str,
    created_by: CreatedBy,
    seed: int = 0,
) -> Design:
    """Wrap the model's design fields with the fields code owns."""
    fields = dict(parsed["design"])
    return Design.model_validate(
        {
            **fields,
            "design_id": design_id,
            "assay_id": assay.assay_id,
            "workcell_id": workcell.workcell_id,
            "layout": Layout(strategy="randomized", seed=seed).model_dump(),
            "created_by": created_by.model_dump(),
        }
    )


def propose(
    client,
    label: str,
    messages: list[dict],
    *,
    first: bool = True,
    transcript=None,
    meta: dict | None = None,
    max_calls: int = 2,
    assay: Assay | None = None,
):
    """The planner slot: ask a model for one design. Returns a ChatResult whose
    parsed field is a PlannerOutput as a dict, or None with the reason in error.

    Temperature is 0.3 for a first attempt and 0.1 for repairs. With an assay,
    the schema sent to the server names its factors (reply_schema).
    """
    from labauto.agents.llm import chat
    from labauto.models import PlannerOutput

    return chat(
        client,
        label,
        messages,
        PlannerOutput,
        temperature=0.3 if first else 0.1,
        transcript=transcript,
        role="planner" if first else "repair",
        meta=meta,
        max_calls=max_calls,
        wire=reply_schema(assay) if assay is not None else None,
    )


def reply_schema(assay: Assay) -> dict:
    """The planner's reply schema as sent to a server, with the assay's factor names.

    The generic schema lets "grid" and each condition hold any key. A server
    that turns the schema into a grammar then lets a model write a wrong
    factor name: a live model's reasoning named base_equiv, but its JSON said
    "encoding". Here "grid" and each condition list exactly the assay's
    factors, each one required, and no other key. Values stay plain numbers:
    whether a value is allowed is for the design rules to say, with a reason
    the model can repair from. Replies are still validated against
    PlannerOutput, so no rule changes.
    """
    from labauto.agents.llm import wire_schema
    from labauto.models import PlannerOutput

    names = list(assay.factors)
    schema = PlannerOutput.model_json_schema()
    design = schema["$defs"]["ModelDesign"]["properties"]

    def named(value: dict) -> dict:
        return {
            "type": "object",
            "properties": {name: copy.deepcopy(value) for name in names},
            "required": list(names),
            "additionalProperties": False,
        }

    def branch(prop: str, kind: str) -> dict:
        found = [b for b in design[prop].get("anyOf", []) if b.get("type") == kind]
        if len(found) != 1:
            raise ValueError(f"the {prop} schema changed shape; reply_schema needs updating")
        return found[0]

    grid = branch("grid", "object")
    value = grid.pop("additionalProperties")
    grid.update(named(value))
    items = branch("conditions", "array")["items"]
    items.update(named(items.pop("additionalProperties")))
    return wire_schema(schema)
