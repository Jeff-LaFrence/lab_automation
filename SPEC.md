# lab_automation build specification

Version 1.0

This document is the single source of truth for building this repository. It is written for a coding agent that works unattended for long stretches, and for a human reviewer who wants to know why the system looks the way it does.

Contents

0. Rules for the builder
1. What this project is
2. Vocabulary
3. Architecture
4. Repository, environment, conventions
5. Data contracts
6. Stage 1: translation layer (no model)
7. Stage 1b: second instrument
8. Stage 2: planner agent
9. Stage 3: closed loop
10. Stage 4: stretch items
11. README and docs
12. Appendix A: verified library facts
13. Appendix B: prompts
14. Appendix C: reference numbers

---

## 0. Rules for the builder

1. Read this whole document once before writing code. Re-read the section for the current stage before you start it, and again after any context reset.
2. Build in order: Stage 1, Stage 1b, Stage 2, Stage 3. Build Stage 4 items only when the owner asks for them.
3. Stages 2 and 3 have a Core and Extensions. Finish and tag Core first. Extensions are optional. Build them only when asked, in the order listed.
4. Stage gate. A stage is done when every line of its Done checklist is true. Work on a branch named `stage-1`, `stage-1b`, `stage-2`, `stage-3`. Merge to `main` only when the stage is done, then tag it. `main` must always pass `pytest` and `labauto demo`.
5. Dependencies. Stage 1 is required for everything. Stage 1b is optional: if it hits its effort cap, leave the branch unmerged and go on. If Stage 2 Core cannot be finished, Stage 3 may still be built with the `random` and `optimizer` deciders, and the model decider is marked not built.
6. When this spec is silent, pick the simplest option that satisfies the Done checklist and add one line to `docs/decisions.md`.
7. When this spec is wrong about a library, trust the installed library, fix the approach, and log it in `docs/decisions.md`.
8. Stuck rule. After three failed approaches to the same problem, write the problem and what you tried under "Open problems" in `docs/decisions.md`, take the fallback this spec names (or leave the feature out and mark it not built), and move on. Do not wait for input unless a section says to stop.
9. Never fake anything. No numbers typed by hand into docs. No stub presented as working. No test weakened, skipped, or deleted to get a pass. If something is not built, the status table says so.
10. System safety. Python packages go in the project virtual environment only. No sudo. No system package installs or upgrades. No driver, kernel, or container daemon changes. No global git config changes. Do not stop or restart any service you did not start.
11. Git safety. No force push, no history rewriting, no `git reset --hard`, no `git clean`. Do not push unless the owner says so. Never bypass commit hooks. If a hook blocks a commit, fix the content.
12. Public repository hygiene. This repository is public. Committed files contain no secrets, API keys, tokens, hostnames, user names, or home directory paths. They name no people, and no organizations other than the instrument vendors and open-source projects named in this document. Nothing is copied from any file outside this repository. If a `CLAUDE.local.md` file exists, follow it, and never copy anything from local notes into committed files.
13. Writing style for all committed prose (README, docs, code comments, CLI output, commit messages). Plain words. Short sentences. Define a term the first time it appears. No em dashes, and no hyphens used as dashes. Do not use these words: leverage, robust, seamless, spearhead, cutting-edge, revolutionary, powerful, genuinely. No claims of production use. No claims about hardware that was not run.
14. Keep modules small. A person should be able to read any one module in ten minutes and explain it. Prefer plain functions and pydantic models to class hierarchies.

---

## 1. What this project is

Language models can now propose experiments. A proposal is not something a liquid handling robot can run, and it is not something a lab should trust without checks. This repository is the layer in between. It turns an experiment design into a checked, simulated liquid handler protocol, keeps a person at the approval step, and then closes the loop with a simulated analytical instrument so that a model can choose the next round from the data.

The structure is the same as a compiler:

| Level | Name | Written by | Example |
|---|---|---|---|
| 0 | Request | a scientist, in plain language | "Screen catalyst loading at 1, 2, 4 and 8 mol % against base at 1.0, 1.5 and 2.0 equivalents, in triplicate." |
| 1 | Design | a model or a person | 12 conditions, 3 replicates, controls on |
| 2 | Plan | code | what goes into each well, in microliters. No vendor terms. |
| 3 | Worklist | code | the ordered list of transfers, with pipette and tip choices |
| 4 | Protocol | code | instrument code for one specific liquid handler |
| 5 | Simulation | the vendor's simulator | a step log and a record of what was dispensed |

### Design principles

These are referenced by number in the rest of the document. The README states them in one sentence each.

- P1. The model never writes robot code. It writes a short design. Fixed code does everything after that.
- P2. Check before you run: rules first, then the simulator, then a person.
- P3. Errors are data. Every failure is structured, specific, and carries a hint, so a model can repair its own design.
- P4. Escalate, do not loop. Retries are bounded. After that a larger model gets one try. After that a person.
- P5. Nothing changes silently. Any difference between what was asked and what is planned is listed at the approval step.
- P6. Record everything. What was designed, what was run, who or what decided, which model, and a hash that ties approval to the exact protocol.
- P7. Numbers come from code. Words may come from a model. Reports mark which is which.
- P8. Parts are swappable. Planner, Instrument, Decider, and Store sit behind small interfaces.
- P9. It runs anywhere. The full demo runs on a laptop with no GPU and no API key, by replaying recorded model sessions.
- P10. Honest scope. Everything here is simulation. A status table says what is built, what is simulated, and what is not built.

### What this is not

Not a scheduler. Not a LIMS. Not validated chemistry: the reaction data is synthetic. No physical run has been made with this code.

### Capability map

This table tells a reader where to look. The README carries a short form of it.

| Need in an agent driven lab | Where it is handled here |
|---|---|
| Translation between planning logic and instrument control | Stage 1: Design to Plan to Worklist to Protocol |
| More than one liquid handler vendor | Instrument slot. Opentrons OT-2 in Stage 1, Tecan EVO (simulated) in Stage 1b |
| Agents with tool use, state, and error recovery | Stage 2 planner with repair loop and escalation ladder. Stage 3 decider with tools |
| Human in the loop | Approval gate, deviations list, approval envelope |
| Analytical readout to traceable, model-ready data | Stage 3: raw peak table, analysis, quality checks, results table with as-designed and as-run values |
| Closed loop design, make, test, analyze | Stage 3 campaign loop and benchmark |
| Judging open-source tools for fit | `docs/tooling_notes.md`: what each simulator checks and what it misses, pinned by tests |
| Service and container packaging, CI | CI in Stage 1. HTTP service and Dockerfile in Stage 4 |
| Not covered | Cluster orchestration, real LIMS or ELN, real instruments, vendor analytical software, model training |

---

## 2. Vocabulary

| Term | Meaning here |
|---|---|
| Request | Plain language text from a scientist. |
| Assay definition | A config file: the reagents, the factors a design may vary, their allowed values, the controls, and the quality rules. |
| Factor | Something a design varies, such as catalyst loading. Each factor maps to a dispense volume. |
| Condition | One combination of factor values. |
| Design | Conditions, replicates, and controls. Short. This is the only thing a model writes. |
| Plan | The volume of every reagent in every well. Vendor neutral. |
| Worklist | The ordered transfers that realize a plan on one workcell. |
| Workcell | A config file: one instrument, its deck layout, pipettes, tips, and the reagents loaded. |
| Protocol | Instrument code generated from a worklist. |
| Ledger | What the simulator says was dispensed into each well. The "as-run" record. |
| Check pipeline | Design rules, expansion, plan rules, worklist rules, compile, simulate, ledger comparison, in that order. |
| Run | One pass of a request or design through the check pipeline, with its folder of records. |
| Rung | One level of the escalation ladder: local model, hosted model, person. |
| Deviation | A declared difference between the request and the design. |
| Approval envelope | The limits inside which later rounds may be approved without a person. |
| Round | One plate in a campaign. |
| Campaign | A series of rounds chasing a goal. |
| Decider | Whatever picks the next round's conditions: random, optimizer, or a model using the optimizer as a tool. |

---

## 3. Architecture

### Pipeline

```
Request (text)
   |  Planner [model]                         Stage 2
   v
Design  --design rules-->  errors
   |  expand (code)
   v
Plan    --plan rules-->    errors
   |  schedule (code) + Workcell
   v
Worklist --workcell rules--> errors
   |  Instrument.compile (code)
   v
Protocol
   |  Instrument.simulate (vendor simulator)
   v
Step log + Ledger --ledger check--> errors
   |
   v
Approval gate [person or envelope]            Stage 2
   |
   v
Virtual instrument reads the Ledger           Stage 3
   |
   v
Raw peak table -> analysis + quality checks -> Results (Store)
   |
   v
Decider [random | optimizer | model with tools] -> next Design
```

Errors at any layer go back to the planner as structured data (P3). Layers run in order, and a failing layer stops the pipeline: later layers cannot run on a broken input.

### Which layer catches what

| Layer | Catches | Why it exists |
|---|---|---|
| Design rules | Unknown factors, values off the allowed list, too many wells, duplicates | Fast, specific feedback in the planner's own terms |
| Plan rules | Overfilled wells, wrong totals, bad well names | The vendor simulator accepts overfilled wells (Appendix A) |
| Workcell rules | Deck conflicts, reagent shortfalls, volumes no pipette can do, tip shortfalls | The vendor simulator accepts volumes below a pipette's minimum (Appendix A) |
| Simulator | Anything the instrument's own software rejects | Independent second opinion from the vendor's code |
| Ledger check | Any difference between what was planned and what the simulator dispensed | Guards against bugs in this repo's own compiler |
| Approval | Whether this should be run at all | Judgment stays with a person |

### Slots

| Slot | Interface | Built | Possible later |
|---|---|---|---|
| Planner | `propose(context) -> PlannerOutput` | scripted, replay, local model, hosted model | any chat model |
| Instrument | `compile(worklist, workcell) -> artifact`, `simulate(artifact) -> SimResult` | Opentrons OT-2, Tecan EVO (bookkeeping simulation) | Hamilton, real hardware drivers |
| Decider | `decide(history, n) -> Decision` | random, optimizer, model with tools | other optimizers |
| Store | `append_results`, `load_results`, `save_record` | local files | LIMS or ELN adapter |

### Roles and control

| Role | Filled by | Stage |
|---|---|---|
| Orchestrator | code (a state machine) | 2, 3 |
| Planner | model | 2 |
| Reviewer | model (extension) | 2 |
| Decider | model, optimizer, or random | 3 |
| Report note writer | model | 3 |
| Approver | person, or the envelope | 2, 3 |

The orchestrator is ordinary code. Models fill roles inside states. No model decides control flow.

### Run states

```
RECEIVED -> PLANNING -> CHECKING -> AWAITING_APPROVAL -> APPROVED
                ^           |
                |           v
                +------ REPAIRING            (bounded, see the ladder)

Any state -> NEEDS_HUMAN   (a question, or a handoff after the ladder is exhausted)
AWAITING_APPROVAL -> REJECTED
Any state -> FAILED        (internal error; the record says where)
```

Stage 3 adds, per round: `APPROVED -> EXECUTING -> ANALYZING -> DECIDING -> (next round)` and a campaign level `DONE`.

State is written to disk after every transition, so a run can be resumed after a crash.

---

## 4. Repository, environment, conventions

### Layout

```
lab_automation/
  README.md
  SPEC.md
  CLAUDE.md
  LICENSE                      MIT unless the owner says otherwise
  pyproject.toml
  .gitignore
  .env.example
  .github/workflows/ci.yml
  configs/
    assay_reaction_screen.json
    workcell_ot2.json
    workcell_ot2_conflict.json     deliberately broken, for the demo
    workcell_tecan_evo.json        Stage 1b
  examples/
    designs/                       good_factorial.json, bad_off_grid.json,
                                   bad_too_many_wells.json, bad_reagent_short.json
    plans/                         bad_overfill.json
    requests/                      01_simple_factorial.txt ... 05_reagent_limit.txt
    recordings/                    recorded model sessions for replay (Stage 2 on)
    sample_run/                    one committed run folder, so readers can browse without running
  src/labauto/
    models.py                      all pydantic models
    config.py                      load assay, workcell, environment
    labware.py                     neutral labware table (well names, capacities)
    expand.py                      Design -> Plan
    validate.py                    design rules, plan rules, workcell rules
    schedule.py                    Plan + Workcell -> Worklist
    instruments/
      base.py
      opentrons_ot2.py
      tecan_evo.py                 Stage 1b
    pipeline.py                    check(design or plan) -> CheckResult
    record.py                      run folders, summary.md
    cli.py
    agents/                        Stage 2
      llm.py                       chat client, structured output, record and replay
      router.py
      planner.py
      prompts/                     prompt text as .md files
    orchestrator.py                Stage 2 state machine
    approval.py
    virtual/                       Stage 3
      ground_truth.py
      lcms.py
    analysis.py
    store.py
    decide/
      base.py  random_decider.py  optimizer.py  model_decider.py
    campaign.py
    benchmark.py
    report.py
  tests/
  docs/
    concepts.md  walkthrough.md  tooling_notes.md  decisions.md
    planner_eval.md  benchmark.md  img/
  runs/                            gitignored
  campaigns/                       gitignored
  .labauto/                        gitignored scratch (simulator config, caches)
```

### Environment

- Python 3.10 or newer. Checked on 3.13. Use a virtual environment at `.venv`.
- `opentrons==9.0.0`, pinned. Newer releases refuse OT-2 protocols (Appendix A). Protocols use `apiLevel` 2.20.
- Core dependencies: `pydantic>=2.6,<3`, `scikit-learn>=1.4`, `httpx>=0.27`, `python-dotenv>=1.0`, `matplotlib>=3.8`. Do not pin numpy: opentrons 9.0.0 selects a 1.26 release and scikit-learn works with it. This full set installs together with no conflicts.
- Extras: `[dev]` is `pytest` and `ruff`. `[tecan]` is `pylabrobot==0.2.2` (installs alongside the core set with no conflicts). `[hosted]` is the SDK for the hosted model provider. `[service]` is `fastapi` and `uvicorn`. `[memory]` is `lancedb`.
- CLI entry point: `labauto = labauto.cli:main`. Use `argparse`.
- Package version: 0.1.0 at Stage 1, 0.2.0 at Stage 2, 0.3.0 at Stage 3.

### Configuration

Machine specific values live in `.env`, which is gitignored. Commit `.env.example`:

```
# Main model. Any OpenAI-compatible chat endpoint, for example a local llama.cpp server.
LABAUTO_MAIN_BASE_URL=http://127.0.0.1:8080/v1
LABAUTO_MAIN_MODEL=local-model
LABAUTO_MAIN_API_KEY=not-needed
LABAUTO_MAIN_CONTEXT_TOKENS=16000

# Optional small model for short summaries.
LABAUTO_SMALL_BASE_URL=
LABAUTO_SMALL_MODEL=
LABAUTO_SMALL_CONTEXT_TOKENS=8000

# Optional hosted model: second rung of the escalation ladder.
# Provider is "anthropic", "openai_compatible", or empty to turn the rung off.
LABAUTO_HOSTED_PROVIDER=
LABAUTO_HOSTED_MODEL=
LABAUTO_HOSTED_BASE_URL=
LABAUTO_HOSTED_API_KEY=
LABAUTO_HOSTED_MAX_CALLS_PER_RUN=2

# Optional embeddings endpoint for the skill library (Stage 4).
LABAUTO_EMBED_BASE_URL=
LABAUTO_EMBED_MODEL=
```

Never hard code a model name, a port, or a key. Never print a key.

### Code conventions

- Type hints everywhere. Pydantic v2 models for every structure that crosses a module boundary.
- `expand`, `validate`, and `schedule` are pure functions: same input, same output, no I/O.
- Every random choice takes a seed. Seeds are recorded.
- Docstrings say what the function is for in words a bench scientist would follow.
- Tests never touch the network or a GPU. Model paths are tested with scripted clients.
- `ruff check` and `ruff format` clean.
- All volumes are in microliters and are whole numbers in the shipped assay.

### Git and CI

- Small commits with short imperative messages.
- Tags: `v0.1-translation-layer`, `v0.1b-second-instrument`, `v0.2-planner-agent`, `v0.3-closed-loop`.
- `.github/workflows/ci.yml`: on push and pull request, Ubuntu, Python 3.12, `pip install -e ".[dev]"`, `ruff check`, `pytest -q`, `labauto demo`. Tests that need `pylabrobot` use `pytest.importorskip`.
- `.gitignore` covers `.venv/`, `.env`, `.claude/`, `CLAUDE.local.md`, `runs/`, `campaigns/`, `.labauto/`, caches. Keep whatever entries already exist in the file.

---

## 5. Data contracts

Field names below are binding. Add fields when needed, and log additions in `docs/decisions.md`.

### 5.1 Assay definition

`configs/assay_reaction_screen.json`:

```json
{
  "assay_id": "generic_reaction_screen_v1",
  "description": "Synthetic small molecule reaction condition screen. Reagent names are placeholders and no real chemistry is modeled.",
  "plate_labware": "plate_96_flat_360ul",
  "total_volume_ul": 200,
  "max_well_volume_ul": 250,
  "default_replicates": 3,
  "addition_order": ["solvent", "substrate", "cosolvent", "base", "catalyst"],
  "reagents": {
    "substrate": {"role": "fixed", "volume_ul": 40},
    "catalyst":  {"role": "factor", "factor": "catalyst_mol_pct"},
    "base":      {"role": "factor", "factor": "base_equiv"},
    "cosolvent": {"role": "factor", "factor": "cosolvent_pct"},
    "solvent":   {"role": "fill"}
  },
  "factors": {
    "catalyst_mol_pct": {"unit": "mol %", "value_per_ul": 0.5,  "min_ul": 1,  "max_ul": 20, "step_ul": 1},
    "base_equiv":       {"unit": "equiv", "value_per_ul": 0.05, "min_ul": 10, "max_ul": 60, "step_ul": 5},
    "cosolvent_pct":    {"unit": "% v/v", "value_per_ul": 0.5,  "min_ul": 0,  "max_ul": 80, "step_ul": 10}
  },
  "controls": {
    "negative":  {"wells": ["A1", "B1"],
                  "volumes_ul": {"substrate": 40, "catalyst": 0, "base": 30, "cosolvent": 40},
                  "expect": {"yield_max": 5}},
    "reference": {"wells": ["C1", "D1"],
                  "conditions": {"catalyst_mol_pct": 2.0, "base_equiv": 1.5, "cosolvent_pct": 20},
                  "expect": {"yield_center": 50, "yield_tolerance": 8}}
  },
  "analysis": {"k_product": 1.20, "k_substrate": 0.85, "internal_standard_area": 1000000},
  "qc": {"internal_standard_tolerance": 0.25, "replicate_spread_max": 15}
}
```

How to read it:

- A factor value maps to a volume: `volume_ul = value / value_per_ul`. The allowed values are the ones whose volume is a whole multiple of `step_ul` between `min_ul` and `max_ul`.
  - Catalyst: 0.5 to 10 mol % in 0.5 steps (1 to 20 uL). 20 levels.
  - Base: 0.5 to 3.0 equivalents in 0.25 steps (10 to 60 uL). 11 levels.
  - Co-solvent: 0 to 40 % in 5 % steps (0 to 80 uL). 9 levels.
  - 1,980 possible conditions.
- `fixed` reagents get the same volume in every sample well. The `fill` reagent tops each well up to `total_volume_ul`.
- Controls sit in fixed wells. The negative control has no catalyst. The reference control is a known mid range condition. Control volumes are exempt from the factor range rules.
- Samples fill the remaining wells in column order (E1, F1, G1, H1, A2, B2, ...). With controls on, 92 wells are available for samples.

### 5.2 Workcell

`configs/workcell_ot2.json`:

```json
{
  "workcell_id": "ot2_bench",
  "instrument": "opentrons_ot2",
  "deck": [
    {"name": "tips_300",  "labware": "tips_300ul",          "position": {"slot": 1}},
    {"name": "tips_20",   "labware": "tips_20ul",           "position": {"slot": 4}},
    {"name": "plate",     "labware": "plate_96_flat_360ul", "position": {"slot": 2}},
    {"name": "reservoir", "labware": "reservoir_12_15ml",   "position": {"slot": 3}}
  ],
  "pipettes": [
    {"name": "small", "model": "p20_single",  "mount": "left",  "min_ul": 1,  "max_ul": 20,  "tips": "tips_20"},
    {"name": "large", "model": "p300_single", "mount": "right", "min_ul": 20, "max_ul": 300, "tips": "tips_300"}
  ],
  "liquid_handling": {"contact_dispense_below_ul": 20},
  "inventory": {
    "substrate": {"labware": "reservoir", "well": "A1", "volume_ul": 6000},
    "catalyst":  {"labware": "reservoir", "well": "A2", "volume_ul": 1200},
    "base":      {"labware": "reservoir", "well": "A3", "volume_ul": 8000},
    "cosolvent": {"labware": "reservoir", "well": "A4", "volume_ul": 10000},
    "solvent":   {"labware": "reservoir", "well": "A5", "volume_ul": 14000}
  },
  "dead_volume_fraction": 0.10
}
```

- Labware and pipette names are neutral keys. Each instrument backend maps them to its own names. `labware.py` holds the neutral facts: well names, well count, capacity per well (plate 360 uL, reservoir 15,000 uL, tip racks 96 tips).
- `position` is free form per instrument. Two deck entries with equal positions are a conflict.
- `inventory` is what is loaded on the deck. Usable volume is `volume_ul * (1 - dead_volume_fraction)`.
- `contact_dispense_below_ul` is the one liquid handling rule in this project. Transfers below it are small: fresh tip for every well, dispensed into the liquid already in the well. Transfers at or above it are bulk: one tip per reagent, dispensed from above the liquid with a blow out, so the tip never touches a well.

`configs/workcell_ot2_conflict.json` is a copy with the reservoir also placed in slot 2.

### 5.3 Design

```json
{
  "design_id": "d-20260101-120000-a1b2",
  "assay_id": "generic_reaction_screen_v1",
  "workcell_id": "ot2_bench",
  "title": "Catalyst loading by base equivalents at 20% co-solvent",
  "grid": {"catalyst_mol_pct": [1, 2, 4, 8], "base_equiv": [1.0, 1.5, 2.0], "cosolvent_pct": [20]},
  "conditions": null,
  "replicates": 3,
  "include_controls": true,
  "layout": {"strategy": "randomized", "seed": 7},
  "deviations": [],
  "assumptions": [],
  "notes": "",
  "created_by": {"role": "person", "model": null, "rung": null}
}
```

- Exactly one of `grid` and `conditions` is set. `grid` is a full factorial and expands to conditions. `conditions` is an explicit list of `{factor: value}` objects, each naming every factor.
- A model writes only: `title`, `grid` or `conditions`, `replicates`, `include_controls`, `deviations`, `assumptions`, `notes`. Code fills the rest.
- `deviations`: list of `{"requested": str, "planned": str, "reason": str}`. An empty list is a claim that the design is exactly what was asked (P5).
- `assumptions`: list of strings for things the request did not specify, such as the replicate count.
- Layout strategies: `randomized` (seeded shuffle of which sample goes in which of the sample wells used) and `by_column` (in order).

### 5.4 Plan

```json
{
  "plan_id": "p-...",
  "design_id": "d-...",
  "assay_id": "generic_reaction_screen_v1",
  "plate": "plate",
  "wells": [
    {"well": "A1", "role": "negative_control", "condition_index": null, "replicate": 1,
     "conditions": null,
     "add_ul": {"solvent": 90, "substrate": 40, "cosolvent": 40, "base": 30, "catalyst": 0}},
    {"well": "E1", "role": "sample", "condition_index": 0, "replicate": 1,
     "conditions": {"catalyst_mol_pct": 1.0, "base_equiv": 1.0, "cosolvent_pct": 20},
     "add_ul": {"solvent": 98, "substrate": 40, "cosolvent": 40, "base": 20, "catalyst": 2}}
  ],
  "totals_ul": {"solvent": 0, "substrate": 0, "cosolvent": 0, "base": 0, "catalyst": 0}
}
```

A plan has no slots, pipettes, or vendor names. A plan can also be written by hand, which is how the overfill demo case is made.

### 5.5 Worklist

One row per transfer. Saved as `worklist.csv`.

| Column | Meaning |
|---|---|
| step | 1, 2, 3, ... |
| reagent | reagent name |
| source_labware, source_well | where it comes from |
| dest_labware, dest_well | where it goes |
| volume_ul | amount |
| pipette | neutral pipette name from the workcell |
| tip | `new` or `keep` |
| dispense | `above_liquid` or `in_liquid` |

Order: all bulk transfers first, reagent by reagent in `addition_order`, wells in column order. Then all small transfers, reagent by reagent in the same order. Zero volumes produce no row.

Pipette choice: a bulk transfer uses the pipette with the largest range that contains the volume. A small transfer uses the pipette with the smallest maximum that contains the volume.

Tip use: bulk transfers use one tip per reagent per pipette (`new` on the first row of the group, `keep` after). Small transfers use a new tip every row.

### 5.6 PlanError

```json
{
  "code": "SOURCE_SHORT",
  "layer": "workcell",
  "where": "inventory:catalyst",
  "message": "The design needs 1328 uL of catalyst. 1080 uL is usable (1200 uL loaded, 10% held back as dead volume).",
  "fix_hint": "Use fewer wells or lower catalyst loadings, or ask for more catalyst to be loaded.",
  "options": ["reduce_wells", "lower_values", "ask_human_to_load_more"],
  "needs_human": false
}
```

`layer` is one of `design`, `plan`, `workcell`, `simulator`, `ledger`, `qc`. `needs_human` is true when no change to the design can fix it, for example a deck conflict in the workcell file.

Messages always carry the numbers: what was needed, what is available.

### 5.7 Error codes

| Layer | Code | Rule |
|---|---|---|
| design | `SCHEMA_INVALID` | Fails model parsing. Message lists the fields. |
| design | `UNKNOWN_FACTOR` | A factor not in the assay. |
| design | `MISSING_FACTOR` | A condition leaves a factor out. |
| design | `FACTOR_OUT_OF_RANGE` | A value outside the allowed range. Hint gives the range. |
| design | `FACTOR_OFF_GRID` | In range but not an allowed step. Hint gives the two nearest allowed values. |
| design | `DUPLICATE_CONDITION` | Two identical conditions. |
| design | `BAD_REPLICATES` | Fewer than 1. |
| design | `EMPTY_DESIGN` | No conditions. |
| design | `TOO_MANY_WELLS` | Conditions times replicates exceeds the sample wells available. Message gives both numbers and the largest replicate count that fits. |
| plan | `UNKNOWN_REAGENT` | A well adds a reagent the assay does not have. |
| plan | `BAD_WELL_NAME` | Not a well on the plate. |
| plan | `DUPLICATE_WELL` | Same well twice. |
| plan | `NEGATIVE_VOLUME` | Any volume below zero, including a fill that would go negative. |
| plan | `WELL_OVERFILL` | Total above `max_well_volume_ul` or above the labware capacity. |
| plan | `WELL_TOTAL_MISMATCH` | A sample well's total differs from `total_volume_ul`. |
| workcell | `DECK_CONFLICT` | Two labware at one position. `needs_human` true. |
| workcell | `BAD_SLOT` | Position not valid for the instrument. `needs_human` true. |
| workcell | `UNKNOWN_LABWARE` | Labware key the backend cannot map. `needs_human` true. |
| workcell | `MISSING_INVENTORY` | A reagent the plan needs is not loaded. `needs_human` true. |
| workcell | `SOURCE_OVER_CAPACITY` | Loaded volume above the source well's capacity. `needs_human` true. |
| workcell | `SOURCE_SHORT` | Total drawn exceeds usable volume. |
| workcell | `VOLUME_OUT_OF_RANGE` | No pipette's range contains the volume. |
| workcell | `TIPS_SHORT` | Tips needed exceed tips on deck, per tip type. Message gives both numbers. |
| simulator | `SIM_OUT_OF_TIPS`, `SIM_DECK_CONFLICT`, `SIM_BAD_VOLUME`, `SIM_LABWARE_NOT_FOUND`, `SIM_OTHER` | Mapped from the simulator's error text (Appendix A). The original text is kept in the message. |
| ledger | `LEDGER_MISMATCH` | What the simulator dispensed differs from the plan, for any well and reagent. |
| qc | `IS_OUT_OF_RANGE`, `REPLICATE_SPREAD`, `LOW_REPLICATES`, `PLATE_QC_FAIL` | Stage 3. |

Validators return every error in the failing layer, not only the first.

### 5.8 SimResult and CheckResult

```
SimResult:
  ok: bool
  instrument: str
  steps: list[str]            plain English step log from the simulator
  ledger: list[{dest_well, reagent, volume_ul}]
  tips_used: {pipette: int}
  error: PlanError | None
  seconds: float

CheckResult:
  ok: bool
  failed_layer: str | None
  errors: list[PlanError]
  design, plan, worklist, artifact_path, sim: each present once its layer has run
  design_hash, protocol_hash: sha256 hex
```

---

## 6. Stage 1: translation layer (no model)

### What this stage shows

An experiment design can be checked, compiled, and simulated by fixed code, with every failure caught at a named layer and explained in plain words. Nothing here needs a model.

### 6.1 Expand: Design to Plan

1. Run the design rules. Stop on errors.
2. Expand `grid` to conditions if present.
3. For each condition, compute each factor reagent's volume from `value_per_ul`. Add fixed reagents. Compute the fill.
4. Place controls in their fixed wells when `include_controls` is true.
5. List the sample wells in column order after the control wells. Take as many as needed. Assign each (condition, replicate) to a well: shuffled with the layout seed for `randomized`, in order for `by_column`.
6. Compute `totals_ul`.

### 6.2 Validate

Three functions, one per layer, each returning `list[PlanError]`: `validate_design(design, assay)`, `validate_plan(plan, assay)`, `validate_worklist(worklist, plan, workcell)`. The plan rules run even on plans produced by `expand`. The plan validator does not trust the expander.

### 6.3 Schedule: Plan to Worklist

Apply the ordering, pipette choice, and tip rules from section 5.5. Count tips per tip type.

### 6.4 Instrument interface

```python
class Instrument(Protocol):
    name: str
    def labware_map(self) -> dict[str, str]: ...
    def pipette_map(self) -> dict[str, str]: ...
    def valid_position(self, position: dict) -> bool: ...
    def compile(self, worklist: Worklist, workcell: Workcell, plan: Plan, out_dir: Path) -> Path: ...
    def simulate(self, artifact: Path, workcell: Workcell) -> SimResult: ...
```

A registry maps the workcell's `instrument` string to a backend.

### 6.5 Opentrons OT-2 backend

Name maps: `tips_300ul` to `opentrons_96_tiprack_300ul`, `tips_20ul` to `opentrons_96_tiprack_20ul`, `plate_96_flat_360ul` to `corning_96_wellplate_360ul_flat`, `reservoir_12_15ml` to `nest_12_reservoir_15ml`, `p20_single` to `p20_single_gen2`, `p300_single` to `p300_single_gen2`. Valid positions are slots 1 to 11.

`compile` writes a standalone `protocol.py` that a person could load on an OT-2. It must be readable. This shape simulates cleanly:

```python
# Generated by labauto from plan p-... (design d-...). Do not edit by hand.
from opentrons import protocol_api

metadata = {"protocolName": "labauto p-...", "author": "labauto"}
requirements = {"robotType": "OT-2", "apiLevel": "2.20"}


def run(protocol: protocol_api.ProtocolContext):
    tips_300 = protocol.load_labware("opentrons_96_tiprack_300ul", 1)
    tips_20 = protocol.load_labware("opentrons_96_tiprack_20ul", 4)
    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", 2)
    reservoir = protocol.load_labware("nest_12_reservoir_15ml", 3)
    small = protocol.load_instrument("p20_single_gen2", "left", tip_racks=[tips_20])
    large = protocol.load_instrument("p300_single_gen2", "right", tip_racks=[tips_300])

    protocol.comment("solvent: bulk additions, one tip, dispensed from above the liquid")
    large.pick_up_tip()
    large.aspirate(98, reservoir["A5"])
    large.dispense(98, plate["E1"].top(-2))
    large.blow_out(plate["E1"].top(-2))
    # ... one aspirate, dispense, blow out per row ...
    large.drop_tip()

    protocol.comment("catalyst: small additions, fresh tip per well, dispensed into the liquid")
    small.pick_up_tip()
    small.aspirate(2, reservoir["A2"])
    small.dispense(2, plate["E1"])
    small.drop_tip()
```

Put `apiLevel` in `requirements` only, not also in `metadata`. One aspirate per dispense, so the ledger is one to one with the worklist.

`simulate`:

- Call `opentrons.simulate.simulate(io.StringIO(source), file_name="protocol.py")` in process. It returns `(run_log, bundle)`. A 28 well, 5 reagent protocol simulates in about half a second.
- Before importing opentrons, set the `OT_API_CONFIG_DIR` environment variable to `.labauto/opentrons` so the library does not write config into the home directory. If that has no effect, leave it and note it in decisions.
- The library prints setup notices. Capture stdout and stderr so they stay out of the CLI output.
- `steps`: `opentrons.simulate.format_runlog(run_log)`, split into lines.
- `ledger`: walk the run log. See Appendix A for the entry structure. Keep, per pipette, the reagent of its last aspirate (found from the source well through the workcell inventory). Each dispense adds its volume to `(dest_well, reagent)`.
- `tips_used`: count pick up entries per pipette.
- Errors: catch the exception, map its text to a `SIM_*` code (Appendix A), keep the original text in the message.

If in process simulation leaks state between runs in the test suite, switch to a subprocess per simulation and log the decision.

### 6.6 Check pipeline

`pipeline.check(input, assay, workcell) -> CheckResult` accepts a Design or a Plan. Order: design rules, expand, plan rules, schedule, workcell rules, compile, simulate, ledger check. A Plan input starts at plan rules. The ledger check compares the simulator's ledger with `add_ul` for every well and reagent, and with `totals_ul` per source.

`design_hash` is the sha256 of the canonical JSON of the design. `protocol_hash` is the sha256 of the compiled artifact.

A `skip_layers` argument exists for tests only. It lets a test send a bad plan straight to the simulator.

### 6.7 Run record

`labauto check` writes `runs/<UTC timestamp>_<short id>/`:

```
input.json            the design or plan as given
design.json           if the input was a design
plan.json
worklist.csv
protocol.py           or the backend's artifact
sim_log.txt           the plain English step log
ledger.csv            as-run volumes per well and reagent
well_map.csv          well, role, condition values, each reagent volume (as designed)
errors.json           empty list on a pass
summary.md
```

`summary.md` is generated by code, one screen, readable by someone who does not program:

```
# Run 20260101T120000Z_a1b2: PASS

Input: examples/designs/good_factorial.json
Assay: generic_reaction_screen_v1    Workcell: ot2_bench (opentrons_ot2)

What would be run
- 12 conditions x 3 replicates + 4 controls = 40 wells, 200 uL each
- Reagent use: catalyst 278 of 1080 uL usable, base ..., ...
- Tips: small 38 of 96, large 4 of 96

Checks
- Design rules: pass
- Plan rules: pass
- Workcell rules: pass
- Simulator (opentrons_ot2): pass, 598 steps
- Ledger: simulated dispenses match the plan for all 40 wells

Files: plan.json, worklist.csv, protocol.py, sim_log.txt, ledger.csv
Design hash: 3f9a...   Protocol hash: 77c1...
```

On a failure the title says `CAUGHT at <layer>`, and each error is listed with its message and hint.

Numbers in this example are illustrative. The real file carries the computed values.

### 6.8 CLI

- `labauto check <file> [--assay PATH] [--workcell PATH]`. Detects design or plan from the JSON. Exit code 0 on pass, 1 on a caught error, 2 on an internal error.
- `labauto demo`. Runs the cases below and prints one table. Must finish in under a minute with no network.

### 6.9 Demo cases

| # | Input | Workcell | Expected |
|---|---|---|---|
| 1 | `designs/good_factorial.json` (the 12 condition example, triplicate) | `workcell_ot2.json` | PASS |
| 2 | `designs/bad_off_grid.json` (catalyst 2.2 mol %) | same | CAUGHT at design: `FACTOR_OFF_GRID` |
| 3 | `designs/bad_too_many_wells.json` (40 conditions, triplicate) | same | CAUGHT at design: `TOO_MANY_WELLS` |
| 4 | `designs/bad_reagent_short.json` (33 conditions at 10 mol % catalyst, duplicate) | same | CAUGHT at workcell: `SOURCE_SHORT` |
| 5 | `plans/bad_overfill.json` (a hand edited plan with one well at 600 uL) | same | CAUGHT at plan: `WELL_OVERFILL` |
| 6 | `designs/good_factorial.json` | `workcell_ot2_conflict.json` | CAUGHT at workcell: `DECK_CONFLICT` |

Printed table:

```
case                     result   layer      detail
good_factorial           PASS                40 wells, 598 steps simulated, ledger matches
bad_off_grid             CAUGHT   design     FACTOR_OFF_GRID catalyst_mol_pct=2.2 (nearest 2.0, 2.5)
bad_too_many_wells       CAUGHT   design     TOO_MANY_WELLS 120 needed, 92 available
bad_reagent_short        CAUGHT   workcell   SOURCE_SHORT catalyst 1328 uL needed, 1080 usable
bad_overfill             CAUGHT   plan       WELL_OVERFILL plate:E1 600 uL, limit 250
deck_conflict            CAUGHT   workcell   DECK_CONFLICT slot 2: plate, reservoir
```

Step counts in this table are illustrative. Case 5 may also report `WELL_TOTAL_MISMATCH` for the same well. Show `WELL_OVERFILL` first.

### 6.10 Tests

- One test per error code in the design, plan, and workcell layers, each with a minimal bad input.
- Expansion: grid expands to the right count, fill is correct, controls land in their wells, randomized layout is stable for a seed and differs across seeds.
- Scheduling: order, pipette choice at 19, 20, and 21 uL, tip counts.
- OT-2: the good design compiles and simulates, the ledger equals the plan, the generated `protocol.py` also passes when simulated on its own as a file.
- Simulator layer works alone: with `skip_layers`, a deck conflict and a tip shortfall are caught by the simulator and mapped to `SIM_DECK_CONFLICT` and `SIM_OUT_OF_TIPS`.
- Blind spots, pinned on purpose. `test_simulator_accepts_overfill`: a 600 uL total into a 360 uL well passes the vendor simulator. `test_simulator_accepts_below_minimum_volume`: a 5 uL aspirate on the 20 to 300 uL pipette passes. These tests document why the plan and workcell rules exist. If either starts failing after a library change, update `docs/tooling_notes.md`.
- Run record: every file is present, `summary.md` contains the result line.
- CLI exit codes.

### 6.11 Docs written in this stage

- `README.md` first version (section 11).
- `docs/tooling_notes.md`: what the Opentrons simulator checks, what it does not, with the test names that prove each point. Add the PyLabRobot notes in Stage 1b.
- `docs/decisions.md`.
- `examples/sample_run/`: copy of a real run folder for demo case 1.

### 6.12 Done checklist

- [ ] On a clean clone: `python -m venv .venv`, `pip install -e ".[dev]"`, `labauto demo` prints the six rows with the expected results.
- [ ] `pytest -q` is green. `ruff check` is clean.
- [ ] CI workflow file is present.
- [ ] README has the diagram, the demo output pasted from a real run, the status table, and the limits section.
- [ ] `examples/sample_run/` is committed.
- [ ] Merged to `main`, tagged `v0.1-translation-layer`.

---

## 7. Stage 1b: second instrument

### What this stage shows

The same plan compiles for a second vendor through the Instrument slot, with no change to anything upstream.

Effort cap: stop after three failed approaches or about two hours of work, whichever comes first. If capped, leave the branch unmerged, record why, and go to Stage 2.

### Build

- `[tecan]` extra, `src/labauto/instruments/tecan_evo.py`, `configs/workcell_tecan_evo.json`.
- Use PyLabRobot's `LiquidHandlerChatterboxBackend` with the Tecan EVO deck. Known working pieces are in Appendix A. The API is async: run it with `asyncio.run` inside `simulate`.
- Deck positions for this backend are `{"carrier": ..., "rails": ..., "site": ...}`.
- For the reagent source, look for a trough plate resource (for example a 12 channel trough plate) in `pylabrobot.resources`. For tips, list the `DiTi_*_LiHa` resources and choose a small tip for small transfers and a larger tip for bulk. If only a 1000 uL tip is usable, use it for everything and say so in `docs/tooling_notes.md`.
- Use one channel at a time. The artifact written by `compile` is a JSON file of the calls to make, plus a readable `protocol_tecan.py` rendering of the same calls. `simulate` executes the calls against the chatterbox backend.
- Turn on tip tracking and volume tracking. Set starting volumes on the source wells from the workcell inventory with `set_volume`. Map the tracker errors (not enough space in a container, not enough liquid) to `SIM_BAD_VOLUME`.
- Ledger: record each dispense as it is issued, then confirm the plate's tracked volumes equal the plan.

### Limits to state

This backend checks tips and volumes only. It does not check collisions, calibration, liquid classes, or timing. It has never driven a real instrument. Say this in the README limits and in `docs/tooling_notes.md`.

### Demo and tests

- `labauto check examples/designs/good_factorial.json --workcell configs/workcell_tecan_evo.json` passes.
- `labauto demo` gains one row: `good_factorial (tecan_evo)   PASS   same plan, second instrument`. The row prints `SKIPPED (pylabrobot not installed)` when the extra is missing.
- Tests use `pytest.importorskip("pylabrobot")`: the good design passes, the ledger matches, an overdrawn source is caught by the tracker when the workcell rules are skipped.

### Done checklist

- [ ] The same design file passes on both workcells. The two plans are identical.
- [ ] README status table and limits updated. `docs/tooling_notes.md` updated.
- [ ] Merged to `main`, tagged `v0.1b-second-instrument`.

---

## 8. Stage 2: planner agent

### What this stage shows

A model can sit in the planner seat without being trusted. It writes a short design. The Stage 1 pipeline checks it. Structured errors go back for repair. Retries are bounded, then a larger model gets one try, then a person. Nothing reaches "approved" without a person seeing what will be run and how it differs from what was asked.

### 8.1 Core scope

Chat client with record and replay. Router. Planner. Repair loop with the escalation ladder. Orchestrator with saved state and resume. Approval gate with deviations and hashes. Planner evaluation. Replay based demo.

### 8.2 Chat client (`agents/llm.py`)

- One function: `chat(endpoint, messages, schema=None, temperature, max_tokens) -> ChatResult` with `text`, `parsed`, `prompt_tokens`, `completion_tokens`, `seconds`, `endpoint_label`, `model`.
- OpenAI-compatible `POST {base_url}/chat/completions` through `httpx`. Timeout 600 seconds.
- Structured output. On first use, probe whether the server accepts a JSON schema in `response_format`. Cache the answer. If not, send the schema in the prompt and parse the reply.
- Parsing is defensive. Strip any `<think>...</think>` block. Use a separate `reasoning_content` field if the server returns one. Strip code fences. Take the first complete JSON object. Validate with pydantic. On failure, retry once with the parse error appended. A second failure counts as a failed attempt with code `SCHEMA_INVALID`.
- Check the running server before assuming anything: `GET {base_url}/models`, then one tiny structured request. Record what works in `docs/decisions.md` without hostnames.
- Client types behind the same interface:
  - `LiveClient`: the above.
  - `HostedClient`: the hosted provider's official SDK, model name from the environment.
  - `ScriptedClient`: returns canned replies in order. For tests.
  - `ReplayClient`: serves replies from a recording file, in order.
- Recording. Every call appends one line to the run's `transcript.jsonl`: role, messages, raw reply, parsed reply, token counts, seconds, endpoint label, model name. Labels are `local-main`, `local-small`, `hosted`. Never a URL, never a key.
- If the main endpoint does not answer, retry three times over five minutes. If it still fails, build and test this stage with `ScriptedClient`, mark live results as pending in the README status table, and continue.

### 8.3 Router (`agents/router.py`)

`route(role, est_prompt_tokens) -> endpoint`. Roles: `planner`, `repair`, `reviewer`, `decider`, `report_note`.

- `report_note` goes to the small endpoint if one is configured, otherwise main. All other roles go to main.
- Token estimate is characters divided by 4. If the estimate is above 80% of the chosen endpoint's context size, use the endpoint with the larger context. If none fits, trim the attempt history to the latest attempt and its errors, and log that it was trimmed.
- Every routing decision is written to `model_calls.jsonl` with its reason.

### 8.4 Planner (`agents/planner.py`)

Input context, built by code:

- Assay summary generated from the assay file: each factor with its unit and allowed values, default replicates, controls, sample wells available.
- Workcell summary generated from the workcell file: usable volume per reagent, tips per type, the small transfer rule in one sentence.
- The request text.
- For repairs: each earlier attempt with its design and errors, newest first.
- Any answers a person gave.

Output, one JSON object:

```json
{"action": "submit_design",
 "design": {"title": "...", "grid": {...}, "conditions": null, "replicates": 3,
            "include_controls": true, "deviations": [], "assumptions": [], "notes": ""}}
```

The system prompt is in Appendix B. Keep prompt text in `agents/prompts/` as files. Temperature 0.3 for a first attempt, 0.1 for repairs.

### 8.5 Repair loop and escalation ladder

```
rungs = [local: up to 3 attempts (1 first try + 2 repairs),
         hosted: 1 attempt, sees the full history,
         person: handoff]

for each rung:
    for each attempt allowed on the rung:
        output = planner on this rung
        result = pipeline.check(output.design)
        record the attempt (rung, model, design, errors, tokens, seconds)
        if result.ok: go to AWAITING_APPROVAL
        if the errors match the previous attempt's errors (same codes, same places):
            leave this rung early            # no progress
state = NEEDS_HUMAN with the full attempt history
```

- If the hosted rung is not configured, skip it and record "hosted rung not configured".
- The hosted rung makes at most `LABAUTO_HOSTED_MAX_CALLS_PER_RUN` calls.
- An error with `needs_human` true skips straight to the person, because no design change can fix it.
- Each attempt's files are kept in `attempts/NN_<rung>/`.

### 8.6 Orchestrator and state (`orchestrator.py`)

- `state.json`: `run_id`, `state`, `updated_at`, `rung`, `attempt`, `needs_human` (kind and text, or null), `history` (list of from, to, at, reason).
- Written after every transition. `labauto resume <run>` continues from the saved state. Resuming never repeats a model call whose reply is already in the transcript.

Run folder for a planned run:

```
request.txt
state.json
attempts/01_local/  design.json errors.json plan.json worklist.csv protocol.py sim_log.txt
attempts/02_local/  ...
final/              the passing attempt's files, plus ledger.csv and well_map.csv
approval.json
transcript.jsonl
model_calls.jsonl
summary.md
```

### 8.7 Approval gate (`approval.py`)

`summary.md` in `AWAITING_APPROVAL` adds, above the Stage 1 content:

```
Request
  "<the request text>"

Changes from what was asked            (from the planner's deviations list)
  - asked: catalyst 15 mol %   planned: catalyst 10 mol %   why: 10 mol % is the highest the loaded stock allows
Assumptions
  - 3 replicates (assay default)

How we got here
  attempt 1  local-main   failed  TOO_MANY_WELLS
  attempt 2  local-main   passed

To approve:  labauto approve <run>      To reject:  labauto reject <run> --reason "..."
```

- `approval.json`: `decision`, `mode` (`human`, `envelope`, `demo_auto`), `by`, `at`, `comment`, `design_hash`, `protocol_hash`.
- Approval is tied to the hashes. Before anything uses an approved run, recompute both hashes. A mismatch voids the approval and returns the run to `AWAITING_APPROVAL`.
- `demo_auto` exists so the demo can run unattended. It is always labelled as such in every record and summary.

### 8.8 CLI

- `labauto plan "<request>"` or `labauto plan --request-file F`. Options: `--client live|replay:<file>|scripted:<file>`, `--assay`, `--workcell`.
- `labauto status <run>`, `labauto approve <run> [--by NAME] [--comment TEXT]`, `labauto reject <run> --reason TEXT`, `labauto resume <run>`.
- `labauto eval-planner [--trials 5]`.
- `labauto demo` now also runs the planner scenarios from recordings. `labauto demo --live` uses the configured endpoint.

### 8.9 Scenarios

`examples/requests/`:

| File | Request | What should happen |
|---|---|---|
| `01_simple_factorial.txt` | "Screen catalyst loading at 1, 2, 4 and 8 mol % against base at 1.0, 1.5 and 2.0 equivalents, with 20% co-solvent, in triplicate." | Passes on the first try. 40 wells. No deviations. |
| `02_too_big.txt` | "Test every catalyst loading from 1 to 10 mol % in 1 % steps against base at 1.0, 1.5, 2.0 and 2.5 equivalents, 20% co-solvent, in triplicate." | 120 sample wells against 92. Repair by cutting replicates or conditions. The cut is declared as a deviation. |
| `03_out_of_range.txt` | "Compare catalyst at 5, 10 and 15 mol % with base at 1.5 and 3.5 equivalents, 10% co-solvent, four replicates." | 15 mol % and 3.5 equivalents are outside the allowed ranges. Drop or clamp, and declare it. Clamping 15 to 10 creates a duplicate condition, which is a second error to repair. |
| `04_ambiguous.txt` | "Run a quick screen around our usual conditions to see if more base helps." | Core: plans around the reference control conditions and lists what it assumed. Extension A: asks a question instead. |
| `05_reagent_limit.txt` | "Full plate: catalyst at 10 mol % everywhere, base from 0.5 to 3.0 equivalents in 0.25 steps, co-solvent at 0, 20 and 40 %, in triplicate." | 99 sample wells against 92, then a catalyst shortfall once the well count fits. Two rounds of repair, or a request to load more catalyst. |

`labauto demo` shows scenarios 01, 02, and 03 from recordings.

### 8.10 Recordings and replay

After live runs, copy each scenario's `transcript.jsonl` to `examples/recordings/<scenario>.jsonl`. Before committing, check each file: endpoint labels only, no URLs, no keys, no absolute paths, no host or user names. Keep the model name. The README says the default demo is a replay of recorded sessions from a local model, and gives the command to run live.

### 8.11 Planner evaluation

`labauto eval-planner --trials 5` runs each scenario that many times on the live main model, local rung only plus the ladder as configured, and writes `docs/planner_eval.md`:

| Scenario | Trials | Valid first try | Valid after local repairs | Went to hosted | Handed to person | Deviations declared when needed | Median seconds |
|---|---|---|---|---|---|---|---|

- "Deviations declared when needed" applies to scenarios 02, 03, and 05: the final design differs from the request, so the deviations list must not be empty.
- Below the table: model name as reported by the endpoint, date, git commit, and a short "Failure notes" section listing the kinds of mistakes seen, each with one real example from the transcripts. Write the notes from the transcripts. Do not smooth them over.
- Publish the numbers whatever they are.

### 8.12 Tests

All with `ScriptedClient`, no network:

- Clean first try reaches `AWAITING_APPROVAL`.
- Bad then good: error goes back, second attempt passes, both attempts recorded.
- Malformed JSON, a reply wrapped in think tags, a reply in a code fence.
- No progress: identical errors twice leaves the rung early.
- Ladder: local exhausted, hosted (scripted) succeeds. Local and hosted exhausted ends in `NEEDS_HUMAN`. Hosted not configured is skipped and recorded.
- `needs_human` error skips to the person.
- Approve, reject, and the hash mismatch case.
- Resume from each state without repeating recorded calls.
- Router choices and the context trim.
- Replay of a committed recording reproduces the same final design.

### 8.13 Extensions (only when asked, in this order)

A. Ask a person. The planner may return `{"action": "ask_human", "question": "...", "reason": "..."}`. State becomes `NEEDS_HUMAN` with kind `question`. `labauto answer <run> "text"` stores the answer and returns to `PLANNING` with the answer in context. At most two questions per run. Scenario 04 is the test.

B. Reviewer. After a design passes, a second model call compares the request with the design and lists differences that are not in `deviations` or `assumptions`. Output `{"undeclared": [{"requested": "...", "planned": "..."}]}`. Shown at approval under "Found by reviewer". The reviewer never edits the design. Note in the README that the reviewer shares a model with the planner unless a second endpoint is configured.

### 8.14 Done checklist

- [ ] `labauto demo` runs Stage 1 cases and the three replayed scenarios with no network, in under two minutes.
- [ ] `labauto plan` works live against the configured endpoint, or the status table says live results are pending and why.
- [ ] `docs/planner_eval.md` holds real numbers from a live run, or is absent with the status table saying so.
- [ ] `pytest -q` green, `ruff check` clean.
- [ ] README updated: the ladder, the approval summary pasted from a real run, the evaluation table, status table.
- [ ] Merged to `main`, tagged `v0.2-planner-agent`.

---

## 9. Stage 3: closed loop

### What this stage shows

The same checked path supports a closed loop. Each round a decider picks conditions, the plate goes through the full check pipeline, a simulated instrument reads what was dispensed, the data passes quality checks, and the next round is chosen from results that can be traced back to the exact protocol.

### 9.1 Core scope

Virtual LC/MS with noise and a raw peak table. Analysis and quality checks with controls on every plate. Results store. Optimizer. Three deciders. Campaign loop with the approval envelope. Benchmark. Campaign report.

### 9.2 Ground truth (`virtual/ground_truth.py`)

A synthetic yield surface, hidden from the decider. It has an interior optimum and an interaction between two factors, so it is not trivial. It models no real chemistry, and the docs say so.

With `c` = catalyst mol %, `b` = base equivalents, `s` = co-solvent fraction (percent divided by 100):

```
f_cat  = (1 - exp(-c / 2.0)) * (1 - 0.02 * max(0, c - 6))
b_opt  = 1.6 + 1.0 * s
f_base = exp(-((b - b_opt)^2) / (2 * 0.6^2))
f_cos  = exp(-((s - 0.22)^2) / (2 * 0.12^2))
yield  = 92 * f_cat * f_base * f_cos          (percent)
```

On the allowed grid the maximum is 86.0 % at 6.5 mol %, 1.75 equivalents, 20 % co-solvent. Thirteen of the 1,980 conditions are within 3 points of it. The reference control is 50.6 %. The negative control is 0.

`surface_seed`: 0 gives exactly the parameters above. Any other value jitters them with that seed: co-solvent optimum 0.22 plus or minus up to 0.08, base intercept 1.6 plus or minus up to 0.4, base slope 1.0 plus or minus up to 0.5, catalyst scale 2.0 times 0.7 to 1.5, penalty onset 6 plus or minus up to 1.5, ceiling 92 times 0.9 to 1.0. When the surface is jittered, the reference control's expected value is recomputed from the surface and passed to the quality checks, standing in for a lab's historical control chart.

### 9.3 Virtual LC/MS (`virtual/lcms.py`)

Input: the run's ledger (as-run volumes per well), the assay, a seed, and any faults.

1. Compute each well's as-run factor values from its ledger volumes and `value_per_ul`. The instrument reads what was dispensed, not what was intended.
2. True yield from the ground truth. Observed yield is the true yield plus normal noise with standard deviation 2.5, clipped to 0 to 100.
3. Conversion is observed yield plus a uniform 2 to 8, capped at 100.
4. Internal standard area is `internal_standard_area` times (1 plus normal noise with standard deviation 0.03).
5. Product area is `k_product * (yield / 100) * IS area`. Substrate area is `k_substrate * (1 - conversion / 100) * IS area`.
6. Faults:
   - `injection_failure`: each well independently with probability 0.02. The internal standard area is multiplied by a uniform 0.2 to 0.5, and the noise on that well's product ratio is tripled. On by default.
   - `plate_drift@R`: in round R, every product area on the plate is multiplied by 0.75. Off unless requested. Used by Extension A.
7. Write `raw/lcms_peaks.csv`: `well, analyte, rt_min, area`, three rows per well (`substrate` near 1.87 min, `product` near 2.41 min, `internal_standard` near 3.05 min, each with small jitter).

### 9.4 Analysis and quality checks (`analysis.py`)

Per well:

- `yield_pct = 100 * (product area / IS area) / k_product`
- `conversion_pct = 100 * (1 - (substrate area / IS area) / k_substrate)`
- `IS_OUT_OF_RANGE` if the IS area is more than 25% from the plate median. The well is excluded.

Per condition:

- Mean, standard deviation, and count over wells that are not excluded.
- `REPLICATE_SPREAD` if the range across replicates is above 15 points. Kept, flagged.
- `LOW_REPLICATES` if fewer than 2 wells remain. Kept, flagged.

Per plate:

- Negative control mean must be below `yield_max`.
- Reference control mean must be within `yield_tolerance` of its expected value.
- If either fails: `PLATE_QC_FAIL`. Every well on the plate is marked excluded with that reason. Core behavior is to record this, flag the round, and require a person's approval for the next round. Extension A adds an automatic repeat.

Outputs per round: `results.csv` and `qc.json`.

### 9.5 Results and Store (`store.py`)

One row per well in `results.csv`, appended to the campaign's `results_all.csv`:

`campaign_id, round, attempt, run_id, well, role, condition_index, replicate, as-designed factor values, as-run factor values, each reagent volume, the three peak areas, yield_pct, conversion_pct, qc_flags, excluded, exclude_reason, design_hash, protocol_hash, decided_by, timestamp`

`Store` is an interface with one implementation, `LocalStore`, which writes CSV and JSON under `campaigns/`. The README names a LIMS or ELN adapter as the open slot and says it is not built.

### 9.6 Optimizer (`decide/optimizer.py`)

- Candidates are the 1,980 grid conditions, with tested conditions removed.
- Inputs scaled to 0 to 1 per factor. Target is the condition mean over valid wells. Plates that failed quality checks are left out.
- Model: scikit-learn `GaussianProcessRegressor`, kernel `ConstantKernel * Matern(nu=2.5, one length scale per factor, bounds 0.05 to 3.0) + WhiteKernel`, `normalize_y=True`, `n_restarts_optimizer=1`.
- Batch choice by Thompson sampling: draw `n` samples of the model over all candidates with `sample_y`, and take each sample's best candidate, skipping repeats. This takes a few seconds per round.
- Round 1 has no data. Use a Latin hypercube over the grid: split each factor's levels into `n` bins, pick one level per bin, shuffle each factor's picks independently. Seeded. Every strategy starts from the same round 1 design for a given seed, so comparisons are fair.
- Functions, also exposed to the model decider as tools:
  - `suggest(n)`: up to 16 candidates, each with predicted mean and standard deviation.
  - `predict(conditions)`: predicted mean and standard deviation for up to 10 conditions.
  - `results(top_k=None, round=None)`: measured condition means with count, standard deviation, and flags.

### 9.7 Deciders (`decide/`)

```python
class Decider(Protocol):
    name: str
    def decide(self, history: History, n: int, brief: str, seed: int) -> Decision: ...
```

`Decision`: `round`, `strategy`, `model`, `picks` (list of `{conditions, source, reason}`), `tool_calls`, `fallback` (null or a reason), `round_note`, `seconds`, `tokens`.

- `RandomDecider`: seeded uniform picks from untested conditions.
- `OptimizerDecider`: `suggest(n)`.
- `ModelDecider`: a tool loop, written here in plain JSON so it does not depend on server side tool calling.
  - Up front it is given the campaign brief, the round number, the factor table, and a digest: the top 5 conditions so far, last round's picks with their outcomes, and any quality flags.
  - Each turn it returns one JSON object: `{"action": "tool", "tool": "suggest", "args": {"n": 12}}` or `{"action": "final", "picks": [...], "round_note": "..."}`.
  - At most 6 tool calls per round. Then it must finalize.
  - A final answer is validated: exactly `n` picks, all on the grid, no duplicates within the round, at most 2 repeats of conditions already tested, a reason of 20 words or fewer per pick, `source` one of `optimizer`, `own`, `confirm`.
  - An invalid final answer gets 2 retries with the validation errors. After that, fall back to `suggest(n)` and record `fallback` with the reason.
  - The system prompt is in Appendix B. Temperature 0.3.

### 9.8 Campaign loop (`campaign.py`)

`campaign.json`: `campaign_id`, `brief`, `assay_id`, `workcell_id`, `strategy`, `rounds` (default 6), `conditions_per_round` (default 8), `replicates` (default 3), `seed`, `surface_seed`, `approval_policy`, `faults`.

Each round:

1. Decide. Round 1 uses the shared Latin hypercube design.
2. Code builds a Design from the picks: explicit `conditions`, replicates, controls on.
3. `pipeline.check`. The full Stage 1 path, including compile, simulate, and the ledger check. 28 wells per round with the defaults.
   - If it fails with the model decider, fall back to the optimizer's picks once and record it. If that fails too, `NEEDS_HUMAN`.
4. Approve, by policy (below).
5. Execute: the virtual LC/MS reads the ledger and writes the raw peak table.
6. Analyze, run quality checks, append to the Store.
7. Save `decision.json` for the next round and update `state.json`.

Assumption, stated in the README: the plate and tips are fresh each round and reagents are topped up between rounds.

Approval policies:

- `always`: a person approves every round.
- `envelope`: a person approves round 1. That approval creates `envelope.json`: assay, workcell, the well count approved, who approved it and when, and `max_auto_rounds` (5). A later round is approved by the envelope when all of these hold: same assay and workcell, well count no larger than approved, every check passed, and the previous round had no anomaly. An anomaly is a plate quality failure, a decider fallback, or a pipeline failure. After an anomaly, or when `max_auto_rounds` is used up, the next round goes to a person. Envelope approvals are recorded with mode `envelope` and a pointer to the person's original approval.
- `auto_demo`: a scripted approver stands in wherever a person is required. Every such approval is labelled `demo_auto`.

Stop when `rounds` is reached.

Campaign folder:

```
campaigns/<campaign_id>/
  campaign.json  state.json  envelope.json
  rounds/01/   a run folder, plus raw/lcms_peaks.csv results.csv qc.json decision.json
  rounds/02/ ...
  results_all.csv
  report.md  progress.png
```

### 9.9 CLI

- `labauto loop --strategy random|optimizer|model [--rounds 6] [--seed 1] [--surface-seed 0] [--approval always|envelope|auto_demo] [--fault NAME@ROUND] [--client live|replay:<file>] [--brief TEXT]`
- `labauto report <campaign>`
- `labauto benchmark [--strategies random,optimizer,model] [--seeds 30] [--model-seeds 3] [--rounds 6]`
- `labauto demo` gains a loop part: 3 rounds with the optimizer strategy and `auto_demo` approval, and, when a recording exists, 2 rounds of the model decider from replay.

### 9.10 Benchmark (`benchmark.py`)

Question: how many rounds does each strategy need to recommend a near optimal condition?

- After each round, a strategy's recommendation is the tested condition with the highest measured mean. Score it by its true yield on the noise free surface. This is what a lab would actually act on.
- Per seed, the surface seed equals the seed, so every seed is a different surface. Round 1 is shared across strategies for a seed.
- The benchmark uses a fast path: plan volumes are taken as the ledger, and compile and simulate are skipped. Say so in the output. The full path is what `labauto loop` runs.
- Seeds: 30 for `random` and `optimizer`. The model strategy runs on the first `--model-seeds` seeds, minimum 3, because each round takes model time. State the count next to every model number.
- Outputs, generated by code and committed:
  - `docs/img/benchmark.png`: x is the round, y is the true yield of the recommendation, one line per strategy with a band of plus or minus one standard deviation, and a dashed line for the mean true optimum.
  - `docs/benchmark.md`: a table of mean true yield of the recommendation by round, the share of seeds within 3 points of the optimum by round, the number of seeds per strategy, model name, date, git commit, and the exact command.
- Appendix C gives reference numbers for checking the implementation.
- Report what happens. If the model decider does not beat the optimizer alone, the write-up says so in one plain sentence and notes what the model added or failed to add. Do not tune the surface or the prompt to change the ranking.

### 9.11 Campaign report (`report.py`)

`report.md`, built by code:

- Campaign settings and the brief.
- Rounds table: best measured mean so far, quality events, approval mode, fallbacks, model used.
- Top 5 conditions with mean, standard deviation, count, flags.
- `progress.png`: best measured mean by round.
- Recommendation: the condition with the highest measured mean.
- Traceability: for the recommended condition, the run folders, well names, and hashes behind its data.
- "Model note", at most 120 words, written by the model from the tables and labelled as model written. Before it is included, code extracts every number in the note and confirms each one appears in the report's tables after rounding. If any number does not match, the note is dropped and replaced with the line "Model note rejected: it contained numbers that are not in the data." (P7)

### 9.12 Tests

- Ground truth: the stated maximum, location, and control values for surface seed 0.
- LC/MS and analysis: with noise set to zero, analysis recovers the true yield exactly. Seeded runs are reproducible.
- Quality checks: a forced injection failure is excluded. A forced plate drift fails the reference control.
- As-run matters: alter one ledger volume and confirm the measured yield follows the altered value, not the designed one.
- Optimizer: on surface seed 0 with a fixed seed, the recommendation after 6 rounds is within 3 points of the optimum. Mark the test slow if needed.
- Model decider with `ScriptedClient`: a tool call then a valid final answer, an invalid final answer then a valid one, repeated invalid answers leading to fallback, the tool call cap.
- Envelope: approves a normal round, refuses after an anomaly, refuses after `max_auto_rounds`.
- Campaign: three rounds end to end with the optimizer and `auto_demo`. Resume in the middle of a campaign.
- Report: the number check accepts a correct note and rejects one with an invented number.

### 9.13 Extensions (only when asked, in this order)

A. Experiment level recovery. With `--fault plate_drift@R`, round R fails plate quality checks. The loop repeats the same design as a new plate (round R, attempt 2), keeps the failed plate's rows in the results marked excluded, and goes on. A second failure ends in `NEEDS_HUMAN`. The demo gains a short run that shows the catch and the repeat. This is the experiment level twin of the Stage 2 repair loop.

B. Soft constraints from the brief. The brief may carry a preference in words, for example "Catalyst is the expensive reagent. Prefer lower loading when yield is within 3 points of the best." The model decider is told to respect it, and at the end of the campaign it names a final recommendation with a reason. The report shows the code's pick (highest mean) and the model's pick side by side when they differ, and the benchmark table adds the mean catalyst loading of each strategy's final recommendation.

### 9.14 Done checklist

- [ ] `labauto loop --strategy optimizer --approval auto_demo` completes 6 rounds, with every round passing through compile, simulate, and the ledger check.
- [ ] `labauto loop --strategy model` works live, or the status table says live results are pending and why.
- [ ] `docs/benchmark.md` and `docs/img/benchmark.png` are generated from a real run. Model results are included if the endpoint was available, with the seed count stated.
- [ ] `labauto demo` covers all three stages with no network, in under two minutes.
- [ ] `pytest -q` green, `ruff check` clean.
- [ ] README updated: loop diagram, benchmark plot, one campaign report excerpt, status table, limits.
- [ ] Merged to `main`, tagged `v0.3-closed-loop`.

---

## 10. Stage 4: stretch items

Build only when asked. Each is independent.

A. HTTP service and container. `[service]` extra, `labauto/service.py` with FastAPI: `POST /runs` (request text, assay, workcell) returns a run id, `GET /runs/{id}` returns state and summary, `GET /runs/{id}/files` lists the record, `POST /runs/{id}/approve`, `POST /runs/{id}/reject`, `POST /runs/{id}/answer`, `POST /campaigns`, `GET /campaigns/{id}`, `GET /healthz`. Work runs in a background thread. No queue and no database, and the README says so. A `Dockerfile` on a slim Python image that installs the package and starts the service with the replay client by default. Tests with FastAPI's test client. Write the Dockerfile even if no container runtime is available to build it, and say whether it was built.

B. Skill library. After a person approves a run, save the request, the final design, the assay, and the workcell as a "skill". At planning time, retrieve the three most similar approved requests and give them to the planner as examples. Similarity uses the embeddings endpoint and a LanceDB table under `.labauto/` when configured, and falls back to word overlap with a JSON file store so it also works with no GPU. Measure it: rerun the planner evaluation with the library on and off and add both rows to `docs/planner_eval.md`. The idea of saving validated procedures for reuse follows the Self-Building Labs work cited in section 11.

C. Small model role. If a small endpoint is configured, route `report_note` and a new per round digest to it, and report the token split by endpoint in the campaign report.

D. Inventory across rounds. Carry reagent volumes from round to round. When a plan fails `SOURCE_SHORT`, the campaign pauses in `NEEDS_HUMAN` asking for a refill, and `labauto resume --refill` continues.

E. Third instrument. Hamilton STAR through PyLabRobot's chatterbox backend, same approach and same limits as Stage 1b.

---

## 11. README and docs

### Readers

Two kinds of reader, and the README serves both:

1. A scientist who does not program and wants to know what this does and whether it can be trusted.
2. An automation or software engineer who has built real workcells, has five minutes, and will notice any claim that outruns the evidence.

Most readers stop after the first screen. The first screen must carry: what it is in three sentences, the diagram, real demo output, and a link to the limits.

### README outline

1. Title and one line: "A checked path from a model's experiment idea to a liquid handler protocol, with a simulated closed loop."
2. What this is. Three sentences from section 1.
3. The idea in one picture. A Mermaid diagram of the pipeline with the four slots marked. GitHub renders Mermaid.
4. See it run. Three commands, then the demo table pasted from a real run.
5. How a bad plan gets caught. One example, start to finish: the request, the error as structured data, the repair, the approval summary. Pasted from a real run.
6. Why it is built this way. The principles, one sentence each.
7. What is real and what is simulated. The status table:

   | Part | Status |
   |---|---|
   | Design, plan, worklist, validators | built and tested |
   | Opentrons OT-2 protocol generation | built, checked in the vendor's simulator, never run on a robot |
   | Tecan EVO backend | built, bookkeeping simulation only |
   | Planner agent, repair, escalation, approval | built, with the model named in the evaluation |
   | LC/MS readout | simulated, synthetic data |
   | Closed loop | built, simulated |
   | Service API, skill library | built or not built, as true |
   | Real hardware, LIMS or ELN, cluster deployment | not built |

   Use exact words: built and tested, simulated, live results pending, not built. Keep it true at every tag.
8. Results. The planner evaluation table and the benchmark plot, each with the command that regenerates it and one honest sentence on what it shows.
9. Plug in your own. The four slots, and two short examples: pointing the planner at another endpoint through `.env`, and the outline of a new instrument backend.
10. Limits. At least these:
    - Simulation only. No physical run has been made.
    - The reaction data is synthetic. The yield surface is a made-up function, chosen to have an interior optimum.
    - An OT-2 with organic solvents is a poor match in practice: volatile liquids drip and some plastics do not hold up. Real reaction screening runs on other hardware. That is the reason the instrument is a slot.
    - One liquid handling rule stands in for liquid classes. Real methods need calibrated liquid classes, labware definitions taught on the actual deck, and mixing steps.
    - The Tecan backend checks volumes and tips, not collisions or timing.
    - Plates and tips are assumed fresh each round, and reagents topped up.
    - No scheduler, no multi-instrument coordination, no sample tracking system.
    - Results depend on the planner model. The evaluation table is for the model named there.
11. What real hardware would add. A short list: deck teaching and calibration, liquid classes and liquid level detection, hardware error handling such as failed tip pickups and clots, the vendor's own control software and drivers, scheduling across instruments, write back to a LIMS or ELN, safety interlocks. Keep it to general statements. The repository owner will edit this section.
12. How this was built. "Designed and directed by the repository owner. The code was written with an AI coding assistant from a written specification, SPEC.md in this repository. The planner and decider run on a local open-weight model. Everything is tested in simulation only."
13. Background. Links, each checked at build time and dropped if it does not load:
    - Opentrons Python Protocol API: https://docs.opentrons.com/v2/
    - PyLabRobot: https://docs.pylabrobot.org
    - Self-Building Labs: https://self-building-labs.github.io/ (arXiv 2610.23826). That work builds lab capabilities in simulation before physical trials and saves validated skills for reuse. This repository borrows the simulate first habit and, in Stage 4, the skill library idea.
14. License.

### Other docs

- `docs/concepts.md`: the vocabulary table, the compiler analogy, and short answers to these questions, written from the actual code and results:
  - Why not let the model write the protocol directly?
  - Why keep a simulator when the rules already passed?
  - What does the ledger check add?
  - What does the escalation ladder buy over more retries?
  - What stops a model from quietly changing the experiment?
  - Why controls on every plate?
  - What would change on real hardware?
  - Did the model decider beat the optimizer, and what does that say?
- `docs/walkthrough.md`: one request followed from text to an approved protocol, with the real files from `examples/sample_run/`, and a "break it yourself" section: three small edits a reader can make to an example file and the error each one produces.
- `docs/tooling_notes.md`: what each simulator checks and misses, with test names.
- `docs/decisions.md`: decisions and open problems.
- `docs/planner_eval.md`, `docs/benchmark.md`: generated.

All numbers in the README and docs come from files this repository's code generated. Give the command next to each.

---

## 12. Appendix A: verified library facts

Checked in a clean environment before the build. If the installed library disagrees, the library wins (rule 7).

Opentrons

- `opentrons` 10.1.0 refuses OT-2 protocols. `opentrons==9.0.0` simulates them. Tested with `apiLevel` 2.20 on Python 3.13.
- `opentrons.simulate.simulate(io.StringIO(source), file_name="protocol.py")` works in process and returns `(run_log, bundle)`.
- Each run log entry is a dict with keys `level`, `logs`, `payload`. `payload["text"]` is always present.
- Aspirate and dispense entries also carry `volume` (number), `location`, `instrument`, and `rate`. Tell them apart by the text prefix: `Aspirating` or `Dispensing`. The pin keeps these strings stable. A test should guard them.
- For those entries: `well = payload["location"].labware.as_well()`, then `well.well_name`, `well.parent.load_name`, and `str(well.parent.parent)` for the slot. `payload["instrument"].name` is the pipette model, for example `p20_single_gen2`, and `.mount` is the mount.
- Pick up entries have text starting `Picking up tip`.
- Every failure surfaces as `opentrons.util.entrypoint_util.ProtocolEngineExecuteError`. The message contains a `detail` string that names the underlying error. Map on these substrings:

  | Substring in the message | Code |
  |---|---|
  | `OutOfTipsError` | `SIM_OUT_OF_TIPS` |
  | `LocationIsOccupiedError` | `SIM_DECK_CONFLICT` |
  | `is not a valid deck slot` | `SIM_DECK_CONFLICT` |
  | `InvalidAspirateVolumeError`, `InvalidDispenseVolumeError` | `SIM_BAD_VOLUME` |
  | `FileNotFoundError` or a labware definition that cannot be found | `SIM_LABWARE_NOT_FOUND` |
  | anything else (`TipNotAttachedError`, `KeyError` for a bad well, `Instrument already present`) | `SIM_OTHER` |

- The simulator does not catch an overfilled well. A total of 600 uL dispensed into a 360 uL well passes, with `transfer` and with explicit dispenses.
- The simulator does not catch a volume below a pipette's minimum. A 5 uL aspirate on `p300_single_gen2` passes.
- It does catch a volume above the tip's capacity, a dispense larger than what was aspirated, an aspirate with no tip, running out of tips, two labware in one slot, and an unknown labware name.
- `transfer()` with a volume above the pipette's maximum is not an error. It splits into several aspirations. This project uses explicit aspirate and dispense calls instead.
- The library prints setup notices about missing robot settings and calibration files. They are harmless. Capture them.
- Labware and pipette names that load: `opentrons_96_tiprack_300ul`, `opentrons_96_tiprack_20ul`, `corning_96_wellplate_360ul_flat`, `nest_12_reservoir_15ml`, `p300_single_gen2`, `p20_single_gen2`.

PyLabRobot 0.2.2

- Installs next to `opentrons==9.0.0` with no dependency conflicts.
- Backends present: `LiquidHandlerChatterboxBackend` (simulation), `EVOBackend` (Tecan), `STARBackend` and `VantageBackend` (Hamilton), `OpentronsOT2Backend`.
- A Tecan EVO 150 deck simulates with the chatterbox backend. Pieces that worked: `EVO150Deck`, a `DiTi_3Pos` carrier with `DiTi_1000ul_LiHa` tips at `rails=10`, an `MP_3Pos` carrier with `Microplate_96_Well` plates at `rails=16`, 8 channels.
- `pylabrobot.resources` includes trough plates (for example `CellTreat_12_troughplate_15000ul_Vb`) and a family of `DiTi_*_LiHa` tips.
- With `set_tip_tracking(True)` and `set_volume_tracking(True)` it raises `TooLittleVolumeError` on an overfill and `TooLittleLiquidError` on drawing from an empty well. This covers the Opentrons simulator's overfill blind spot.
- The API is async: `await lh.setup()`, `lh.pick_up_tips`, `lh.aspirate`, `lh.dispense`. `set_liquids` is deprecated. Use `set_volume`.
- The chatterbox backend is bookkeeping only. It proves nothing about collisions, calibration, or liquid behavior.

Model server

- Nothing about the model server is verified here. Probe it (section 8.2) and record what works.

---

## 13. Appendix B: prompts

Store these as files under `src/labauto/agents/prompts/`. Small wording fixes are fine and are logged in decisions. Do not add examples that contain the scenario answers.

### Planner system prompt

```
You plan plate based experiments for a liquid handling robot. You do not write robot code and you do not calculate volumes. You write a short experiment design as JSON. Other software turns it into a protocol and checks it.

You are given: the assay (the factors you may vary and their allowed values, the controls, the wells available), the workcell (how much of each reagent is loaded, how many tips), the request from a scientist, and, when this is a repair, your earlier designs with the errors they produced.

Rules
1. Use only the listed factors and only their allowed values.
2. Match the request as closely as the limits allow.
3. If you must change anything the scientist asked for (a value, the number of replicates, the number of conditions), make the smallest change that works and list it in "deviations" with what was asked, what you planned, and why.
4. Never change something silently. An empty "deviations" list means the design is exactly what was asked.
5. If the request does not specify something and the assay gives a default, use the default and list it in "assumptions".
6. When repairing, change only what the errors require. Read the fix hints. Do not undo a fix you already made.
7. Use "grid" for a full factorial and "conditions" for anything else. Never both.
8. Reply with one JSON object and nothing else.

Reply format
{"action": "submit_design",
 "design": {"title": string,
            "grid": {factor: [values]} or null,
            "conditions": [{factor: value, ...}] or null,
            "replicates": integer,
            "include_controls": true,
            "deviations": [{"requested": string, "planned": string, "reason": string}],
            "assumptions": [string],
            "notes": string}}
```

With Extension A, add rule: "If the request leaves out something you need and the assay gives no default, ask one clear question instead of guessing: {"action": "ask_human", "question": string, "reason": string}."

### Reviewer system prompt (Extension B)

```
You check an experiment design against the request it came from. List every difference in what will actually be run that is not already stated in the design's "deviations" or "assumptions". Do not judge whether the design is good. Do not suggest changes.

Reply with one JSON object and nothing else:
{"undeclared": [{"requested": string, "planned": string}]}
An empty list means you found no undeclared differences.
```

### Model decider system prompt

```
You choose the next experiments in an optimization campaign. A scientist wrote the campaign brief. You do not write robot code or volumes. You pick conditions, and other software plans and checks the plate.

Each round you pick exactly N distinct conditions from the allowed values.

Tools
- suggest {"n": integer}: the statistical optimizer proposes n conditions with predicted yield and uncertainty.
- predict {"conditions": [...]}: the optimizer's model predicts yield and uncertainty for conditions you are considering.
- results {"top_k": integer or null, "round": integer or null}: measured results, with quality flags.

Rules
1. Treat the optimizer as an advisor. You may take its suggestions, replace some with your own, or repeat earlier conditions to confirm them (at most 2 repeats per round).
2. Follow the brief. If it states a preference, let it shape your picks and say so in the reason.
3. Do not build on flagged data. If a result is flagged or its replicates disagree, prefer to repeat it.
4. Give every pick a reason of 20 words or fewer, and a source: "optimizer", "own", or "confirm".
5. One JSON object per reply. Either a tool call or your final picks. At most 6 tool calls per round.

Reply formats
{"action": "tool", "tool": "suggest" | "predict" | "results", "args": {...}}
{"action": "final",
 "picks": [{"conditions": {factor: value, ...}, "source": string, "reason": string}],
 "round_note": string of 40 words or fewer}
```

### Report note prompt

```
You are given the tables from a campaign report. Write a note of at most 120 words for a scientist: what the campaign found, how confident the data makes you, and one thing worth checking next. Use only numbers that appear in the tables, copied exactly. Do not invent any number.
```

---

## 14. Appendix C: reference numbers

Use these to check the implementation. Published numbers always come from the repository's own runs.

Ground truth, surface seed 0

- Maximum 86.04 % at catalyst 6.5 mol %, base 1.75 equivalents, co-solvent 20 %.
- 13 of 1,980 grid conditions are within 3 points of the maximum. 29 are within 5 points.
- Reference control 50.61 %. Negative control 0.

Stage 2 scenario arithmetic

- Scenario 01: 12 conditions x 3 + 4 controls = 40 wells. Catalyst use 278 uL of 1,080 usable. Small tips 38, large tips 4.
- Scenario 02: 40 conditions x 3 = 120 sample wells against 92. In duplicate, 80 sample wells + 4 controls = 84 wells, catalyst 888 uL, small tips 74.
- Scenario 05: 33 conditions x 3 = 99 sample wells against 92. In duplicate, 66 sample wells, catalyst 1,328 uL against 1,080 usable.

Optimizer sanity check (surface seed 0, 8 conditions x 3 replicates per round, 6 rounds, noise 2.5, recommendation scored by true yield)

| | Round 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|
| Optimizer, mean true yield of recommendation (12 seeds) | 64.9 | 72.6 | 80.6 | 84.0 | 85.1 | 85.4 |
| Optimizer, share of seeds within 3 points | 0.00 | 0.17 | 0.58 | 0.75 | 0.92 | 1.00 |
| Random, mean true yield of recommendation (40 seeds) | 61.4 | 69.7 | 73.1 | 76.1 | 77.9 | 79.2 |
| Random, share of seeds within 3 points | 0.00 | 0.02 | 0.10 | 0.15 | 0.20 | 0.25 |

An implementation should land in the same neighborhood. Exact values will differ with random number details. Each optimizer round took about 3 seconds in this check.
