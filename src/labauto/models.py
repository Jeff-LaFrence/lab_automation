"""All data structures that cross a module boundary.

Every file this project reads or writes is described here as a pydantic model,
so that a bad file fails loudly with a list of the fields that are wrong.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, Strict, model_validator


def _volume_out(value: float) -> float | int:
    """Write whole microliter volumes as integers, so files read like the spec."""
    return int(value) if float(value).is_integer() else value


Volume = Annotated[float, Field(allow_inf_nan=False), PlainSerializer(_volume_out, return_type=Any)]

# A factor value written in a design: a finite number. true, "2" and NaN are refused.
FactorValue = Annotated[float, Strict(), Field(allow_inf_nan=False)]


class Strict_(BaseModel):
    """Base for models read from files people or models write: unknown fields are errors."""

    model_config = ConfigDict(extra="forbid")


Layer = Literal["design", "plan", "workcell", "simulator", "ledger", "qc"]


# ---------------------------------------------------------------- assay


class FactorSpec(BaseModel):
    """One thing a design may vary, and how a value turns into a dispense volume."""

    unit: str
    value_per_ul: float
    min_ul: Volume
    max_ul: Volume
    step_ul: Volume


class ReagentSpec(BaseModel):
    """How one reagent is dosed: a fixed volume, set by a factor, or the top-up fill."""

    role: Literal["fixed", "factor", "fill"]
    volume_ul: Volume | None = None
    factor: str | None = None


class ControlSpec(BaseModel):
    """A control placed in fixed wells on every plate, with what its result should be."""

    wells: list[str]
    volumes_ul: dict[str, Volume] | None = None
    conditions: dict[str, float] | None = None
    expect: dict[str, float] = Field(default_factory=dict)


class AnalysisSpec(BaseModel):
    """Response factors that turn peak areas into yield and conversion."""

    k_product: float
    k_substrate: float
    internal_standard_area: float


class QcSpec(BaseModel):
    """Limits used by the quality checks on each plate."""

    internal_standard_tolerance: float
    replicate_spread_max: float


class Assay(BaseModel):
    """An assay definition: reagents, factors, controls, and quality rules."""

    assay_id: str
    description: str = ""
    plate_labware: str
    total_volume_ul: Volume
    max_well_volume_ul: Volume
    default_replicates: int
    addition_order: list[str]
    reagents: dict[str, ReagentSpec]
    factors: dict[str, FactorSpec]
    controls: dict[str, ControlSpec] = Field(default_factory=dict)
    analysis: AnalysisSpec
    qc: QcSpec


# ---------------------------------------------------------------- workcell


class DeckEntry(BaseModel):
    """One piece of labware on the deck, under a neutral name."""

    name: str
    labware: str
    position: dict[str, Any]


class PipetteSpec(BaseModel):
    """One pipette: its neutral name, model key, mount, volume range and tip rack."""

    name: str
    model: str
    mount: str
    min_ul: Volume
    max_ul: Volume
    tips: str


class InventoryEntry(BaseModel):
    """Where a reagent is loaded on the deck and how much was loaded."""

    labware: str
    well: str
    volume_ul: Annotated[Volume, Field(ge=0)]


class LiquidHandling(BaseModel):
    """The single liquid handling rule: transfers below this volume touch the liquid."""

    contact_dispense_below_ul: Volume


class Workcell(BaseModel):
    """A workcell: one instrument, its deck, pipettes, tips, and loaded reagents."""

    workcell_id: str
    instrument: str
    deck: list[DeckEntry]
    pipettes: list[PipetteSpec]
    liquid_handling: LiquidHandling
    inventory: dict[str, InventoryEntry]
    dead_volume_fraction: float = Field(default=0.0, ge=0.0, lt=1.0)

    def deck_entry(self, name: str) -> DeckEntry | None:
        """Find a deck entry by its neutral name."""
        for entry in self.deck:
            if entry.name == name:
                return entry
        return None

    def pipette(self, name: str) -> PipetteSpec | None:
        """Find a pipette by its neutral name."""
        for pip in self.pipettes:
            if pip.name == name:
                return pip
        return None


# ---------------------------------------------------------------- design


class Deviation(Strict_):
    """A declared difference between what was asked and what is planned."""

    requested: str
    planned: str
    reason: str


class CreatedBy(Strict_):
    """Who or what wrote a design."""

    role: Literal["person", "model", "optimizer", "random", "code"] = "person"
    model: str | None = None
    rung: str | None = None


class Layout(Strict_):
    """How samples are placed in wells: a seeded shuffle, or in column order."""

    strategy: Literal["randomized", "by_column"] = "randomized"
    seed: int = 0


class Design(BaseModel):
    """An experiment design: conditions, replicates, and controls.

    This is the only thing a model writes. Exactly one of grid and conditions
    may be set. Code fills the ids, the layout, and created_by.
    """

    model_config = ConfigDict(extra="forbid")

    design_id: str = ""
    assay_id: str = ""
    workcell_id: str = ""
    title: str = ""
    grid: dict[str, list[FactorValue]] | None = None
    conditions: list[dict[str, FactorValue]] | None = None
    replicates: int = 3
    include_controls: bool = True
    layout: Layout = Field(default_factory=Layout)
    deviations: list[Deviation] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    notes: str = ""
    created_by: CreatedBy = Field(default_factory=CreatedBy)

    @model_validator(mode="after")
    def _not_both(self) -> Design:
        if self.grid is not None and self.conditions is not None:
            raise ValueError("set exactly one of 'grid' and 'conditions', not both")
        return self


# ---------------------------------------------------------------- plan


class PlanWell(Strict_):
    """What goes into one well, in microliters per reagent."""

    well: str
    role: Literal["sample", "negative_control", "reference_control"]
    condition_index: int | None = None
    replicate: int = 1
    conditions: dict[str, float] | None = None
    add_ul: dict[str, Volume]

    def total_ul(self) -> float:
        """Total liquid in the well once every addition is made."""
        return float(sum(self.add_ul.values()))


class Plan(BaseModel):
    """The volume of every reagent in every well. No vendor terms."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    design_id: str | None = None
    assay_id: str
    plate: str = "plate"
    wells: list[PlanWell]
    totals_ul: dict[str, Volume] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _fill_totals(self) -> Plan:
        """A plan written by hand may leave totals_ul out. Code adds it from the wells."""
        if not self.totals_ul:
            totals: dict[str, float] = {}
            for w in self.wells:
                for r, v in w.add_ul.items():
                    totals[r] = totals.get(r, 0.0) + v
            self.totals_ul = totals
        return self


# ---------------------------------------------------------------- worklist


class Transfer(BaseModel):
    """One row of a worklist: a single aspirate and dispense."""

    step: int
    reagent: str
    source_labware: str
    source_well: str
    dest_labware: str
    dest_well: str
    volume_ul: Volume
    pipette: str
    tip: Literal["new", "keep"]
    dispense: Literal["above_liquid", "in_liquid"]


class Worklist(BaseModel):
    """The ordered transfers that make a plan on one workcell."""

    workcell_id: str
    plan_id: str
    rows: list[Transfer]
    tips_needed: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------- errors and results


class PlanError(BaseModel):
    """One structured failure, specific enough for a model or a person to act on."""

    code: str
    layer: Layer
    where: str
    message: str
    fix_hint: str = ""
    options: list[str] = Field(default_factory=list)
    needs_human: bool = False
    short: str = ""

    def brief(self) -> str:
        """A one line form for tables, for example 'SOURCE_SHORT catalyst 1328 uL needed'."""
        return f"{self.code} {self.short}".strip()


class LedgerEntry(BaseModel):
    """What the simulator says was dispensed of one reagent into one well."""

    dest_labware: str = "plate"
    dest_well: str
    reagent: str
    volume_ul: Volume


class SimResult(BaseModel):
    """The outcome of running a protocol in an instrument simulator."""

    ok: bool
    instrument: str
    steps: list[str] = Field(default_factory=list)
    ledger: list[LedgerEntry] = Field(default_factory=list)
    tips_used: dict[str, int] = Field(default_factory=dict)
    error: PlanError | None = None
    seconds: float = 0.0
    well_volumes: dict[str, Volume] | None = (
        None  # tracked plate volumes, when the simulator keeps them
    )


class CheckResult(BaseModel):
    """The outcome of one pass through the check pipeline."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ok: bool
    failed_layer: str | None = None
    errors: list[PlanError] = Field(default_factory=list)
    input_kind: Literal["design", "plan"] = "design"
    instrument: str = ""
    layer_status: dict[str, str] = Field(default_factory=dict)
    design: Design | None = None
    plan: Plan | None = None
    worklist: Worklist | None = None
    artifact_path: str | None = None
    sim: SimResult | None = None
    design_hash: str | None = None
    protocol_hash: str | None = None


# ---------------------------------------------------------------- planner and runs (Stage 2)


class ModelDesign(Strict_):
    """The design fields a model writes. Code fills the rest (ids, layout, created_by)."""

    title: str
    grid: dict[str, list[FactorValue]] | None = None
    conditions: list[dict[str, FactorValue]] | None = None
    replicates: int
    include_controls: bool = True
    deviations: list[Deviation]
    assumptions: list[str]
    notes: str = ""

    @model_validator(mode="after")
    def _not_both(self) -> ModelDesign:
        if self.grid is not None and self.conditions is not None:
            raise ValueError("set exactly one of 'grid' and 'conditions', not both")
        return self


class PlannerOutput(Strict_):
    """One planner reply."""

    action: Literal["submit_design"]
    design: ModelDesign


class AttemptRecord(BaseModel):
    """One planner attempt: who wrote it, what it was, and what the checks said."""

    number: int
    rung: Literal["local", "hosted"]
    endpoint_label: str
    model: str = ""
    design: dict | None = None
    errors: list[PlanError] = Field(default_factory=list)
    ok: bool = False
    failed_layer: str | None = None
    reply_error: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    calls: int = 0
    design_hash: str | None = None
    protocol_hash: str | None = None

    def signature(self) -> list[tuple[str, str]]:
        """The errors as (code, where) pairs, used to spot an attempt that made no progress."""
        return sorted({(e.code, e.where) for e in self.errors})


class NeedsHuman(BaseModel):
    """Why a run is waiting for a person."""

    kind: Literal["handoff", "question", "approval"]
    text: str


class Transition(BaseModel):
    """One state change, with when and why."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    from_: str = Field(alias="from")
    to: str
    at: str
    reason: str = ""


class RunState(BaseModel):
    """state.json for a planned run."""

    run_id: str
    state: str
    updated_at: str
    rung: str | None = None
    attempt: int = 0
    needs_human: NeedsHuman | None = None
    history: list[Transition] = Field(default_factory=list)
    assay: str = ""
    workcell: str = ""
    seed: int = 0
    client: str = ""
    hosted_calls: int = 0
    final_attempt: int | None = None
    design_hash: str | None = None
    protocol_hash: str | None = None
    notes: list[str] = Field(default_factory=list)


class Approval(BaseModel):
    """approval.json: the decision, who made it, and the hashes it is tied to."""

    decision: Literal["approved", "rejected", "voided"]
    mode: Literal["human", "envelope", "demo_auto"]
    by: str
    at: str
    comment: str = ""
    design_hash: str | None = None
    protocol_hash: str | None = None
    envelope_ref: str | None = None
    void_reason: str | None = None


# ---------------------------------------------------------------- closed loop (Stage 3)


class Pick(BaseModel):
    """One condition chosen for the next plate, with where it came from and why."""

    conditions: dict[str, float]
    source: str
    reason: str = ""


class ToolCall(BaseModel):
    """One tool call the model decider made, with a short view of the result."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    result: Any = None


class Decision(BaseModel):
    """What a decider chose for one round, and how."""

    round: int
    strategy: str
    model: str | None = None
    picks: list[Pick]
    tool_calls: list[ToolCall] = Field(default_factory=list)
    fallback: str | None = None
    round_note: str = ""
    seconds: float = 0.0
    tokens: int = 0


class ConditionStat(BaseModel):
    """Measured results for one condition over the valid wells of every plate so far."""

    conditions: dict[str, float]
    mean: float | None = None
    sd: float | None = None
    n: int = 0
    flags: list[str] = Field(default_factory=list)
    rounds: list[int] = Field(default_factory=list)


class History(BaseModel):
    """What a decider may see: measured results only, never the hidden surface."""

    next_round: int
    stats: list[ConditionStat] = Field(default_factory=list)
    last_picks: list[dict[str, Any]] = Field(default_factory=list)
    qc_events: list[str] = Field(default_factory=list)


class QcEvent(BaseModel):
    """One quality check finding on a plate."""

    code: Literal["IS_OUT_OF_RANGE", "REPLICATE_SPREAD", "LOW_REPLICATES", "PLATE_QC_FAIL"]
    where: str
    message: str


class QcReport(BaseModel):
    """qc.json for one plate."""

    round: int
    attempt: int = 1
    plate_ok: bool
    negative_mean: float | None = None
    negative_limit: float | None = None
    reference_mean: float | None = None
    reference_expected: float | None = None
    reference_tolerance: float | None = None
    is_median: float | None = None
    excluded_wells: list[str] = Field(default_factory=list)
    events: list[QcEvent] = Field(default_factory=list)


class CampaignConfig(BaseModel):
    """campaign.json: the settings a campaign was started with."""

    model_config = ConfigDict(extra="forbid")

    campaign_id: str
    brief: str = ""
    assay_id: str
    workcell_id: str
    strategy: Literal["random", "optimizer", "model"]
    rounds: int = Field(default=6, ge=1)
    conditions_per_round: int = Field(default=8, ge=1)
    replicates: int = Field(default=3, ge=1)
    seed: int = 1
    surface_seed: int = 0
    approval_policy: Literal["always", "envelope", "auto_demo"] = "always"
    faults: list[str] = Field(default_factory=lambda: ["injection_failure"])
    assay: str = ""
    workcell: str = ""
    client: str = ""

    @model_validator(mode="after")
    def _known_faults(self) -> CampaignConfig:
        for f in self.faults:
            name, _, at = f.partition("@")
            if f == "injection_failure":
                continue
            if name == "plate_drift" and at.isdigit() and 1 <= int(at) <= self.rounds:
                continue
            raise ValueError(
                f"unknown fault '{f}': use injection_failure or plate_drift@R with R from 1 to {self.rounds}"
            )
        return self


class Envelope(BaseModel):
    """envelope.json: the limits inside which later rounds may be approved without a person."""

    assay_id: str
    workcell_id: str
    wells_approved: int
    approved_by: str
    approved_at: str
    approval_ref: str
    assay_sha256: str = ""
    workcell_sha256: str = ""
    max_auto_rounds: int = 5
    auto_rounds_used: int = 0


class CampaignState(BaseModel):
    """state.json for a campaign."""

    campaign_id: str
    state: str
    updated_at: str
    round: int = 1
    round_state: str = "DECIDING"
    needs_human: str | None = None
    appended_rounds: list[int] = Field(default_factory=list)
    anomalies: dict[str, str] = Field(default_factory=dict)
    history: list[dict[str, str]] = Field(default_factory=list)
