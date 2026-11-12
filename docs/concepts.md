# Concepts

## Vocabulary

| Term | Meaning here |
|---|---|
| Request | Plain text from a scientist saying what to test, given to `labauto plan` in quotes or in a file. The planner turns it into a design. |
| Design | The short JSON that says what to test: the conditions (a `grid` of values per factor, every combination used, or a list), the replicates, whether controls are on, and any deviations and assumptions. A model or a person writes it. When the planner writes it, code fills in the ids, the plate layout, and who wrote it. In the closed loop, code builds it from the decider's picks. |
| Condition | One combination of factor values, such as catalyst 2.0 mol %, base 1.5 equiv, and co-solvent 20 %. Each condition fills its replicate wells, and results are reported per condition. |
| Factor | Something a design varies: `catalyst_mol_pct`, `base_equiv`, or `cosolvent_pct` in the shipped assay. Each factor sets one reagent's volume, so its allowed values are volume steps: catalyst runs from 0.5 to 10 mol % in steps of 0.5, at 0.5 mol % per uL. |
| Assay | A config file that defines one kind of experiment: the reagents, the factors and their allowed values, the controls, how peak areas become yields, and the quality limits. The shipped one, `generic_reaction_screen_v1`, is a synthetic reaction screen with placeholder reagents. |
| Plan | The volume of every reagent in every well, worked out from the design by fixed arithmetic. It has no vendor terms, so one design gives the same plan on every workcell. |
| Workcell | A config file for one liquid handler: the instrument, the labware on its deck, the pipettes and tip racks, and how much of each reagent is loaded where. The shipped workcells are for an Opentrons OT-2 and a Tecan EVO. |
| Worklist | The ordered transfers that make a plan on one workcell, one aspirate and dispense per row, each with its pipette and tip choice. Bulk additions come first, then small additions with a fresh tip for each well. |
| Protocol | Code for one instrument, generated from the worklist: a Python protocol for the OT-2, or a list of calls and a matching PyLabRobot script for the Tecan EVO. It is checked in a simulator (the vendor's for the OT-2, PyLabRobot's for the Tecan EVO) and has never run on a robot. |
| Ledger | What the simulator says was dispensed of each reagent into each well: the as-run record. The ledger check compares it with the plan, and the simulated LC/MS reads the ledger, not the plan. |
| Check pipeline | The layers a design or plan goes through, in order: design rules, plan rules, workcell rules, the simulator, and the ledger check. The first layer with errors stops it, and each error comes back as data: a code, where it is, a message with the numbers, and a hint for the fix. |
| Slot | A part that sits behind a small interface so it can be swapped: the planner, the instrument, the store, and the decider, plus the spectrum evaluator behind the NMR readout. |
| Run | One design or plan checked by `labauto check`, or one request planned by `labauto plan`, with its folder of records under `runs/`. A planned run keeps every attempt, and `labauto status`, `approve`, `reject`, and `resume` take its folder. |
| Deviation | A declared difference between what the request asked and what the design plans, with the reason, such as cutting triplicate to 2 replicates so the plate fits. Every planner reply must carry a `deviations` list, and its entries are shown in the approval summary right after the request, under "Changes from what was asked". |
| Assumption | Something the request left out that the planner filled in, usually with the assay's default, written in plain words. Every reply must carry an `assumptions` list, and it is shown in the approval summary below the deviations. |
| Rung, ladder | A rung is one level of the escalation ladder: the local model (a first try and up to 2 repairs), then a hosted model (1 attempt, only if one is configured), then a person. A planned run climbs it while its designs fail, leaves a rung early when an attempt repeats the last attempt's errors, and goes straight to a person on an error no design change can fix. |
| Approval | A recorded yes or no on a run or round that passed every check, kept in `approval.json` with who decided, when, and the design and protocol hashes; a later change to either file voids it. The mode is human (`labauto approve <run> --by NAME`), envelope, or demo_auto, the label written when the `--approval auto_demo` policy approves with no person. |
| Round | One plate in a campaign: a decider picks conditions, code builds the design, the plate goes through the check pipeline and approval, and the simulated LC/MS reads it. Round 1 is the same Latin hypercube for every strategy. |
| Campaign | A series of rounds toward one goal, started by `labauto loop` with a decider strategy, a seed, a surface seed, and an approval policy. Its folder holds the settings (`campaign.json`), each round's files, `results_all.csv`, and `report.md`. |
| Decider | Whatever picks the next round's conditions from the measured results so far: random, the optimizer (a Gaussian process), or a model that uses the optimizer as a tool. It never sees the hidden surface. A model decider that gives 3 invalid answers in a round falls back to the optimizer, and the fallback is recorded. |
| Surface | The made up yield function behind the simulated LC/MS, with an interior optimum, hidden from every decider. Surface seed 0 is the surface in SPEC.md Appendix C. Any other seed varies its parameters, so each benchmark seed is a different surface. |
| Approval envelope | The limits inside which later rounds of a campaign are approved without a person, set when a person approves round 1 under the `envelope` policy and kept in `envelope.json`. A round goes back to a person when its assay or workcell differs or its file changed, it has more wells, a check failed, the previous round had an anomaly such as a failed plate or a decider fallback, or the 5 automatic rounds are used up. |
| Negative control | Wells with no catalyst (A1 and B1 in the shipped assay), on every plate with controls on, which should show almost no product. If their mean yield is not below 5 %, the plate fails its quality checks. |
| Reference control | Wells (C1 and D1) that run one fixed condition, catalyst 2.0 mol %, base 1.5 equiv, and co-solvent 20 %, with a known expected yield. If their mean is more than 8 points off, as when the `plate_drift@R` fault makes every product area on round R's plate read 25 % low, the plate fails (`PLATE_QC_FAIL`) and every well on it is excluded. |
| Internal standard | A compound at the same known amount in every well, so yield is read as a ratio to it: in the LC/MS, the product's peak area over the standard's, divided by a response factor. A well whose standard area is more than 25 % from the plate median, as after a failed injection (the `injection_failure` fault, on by default in `labauto loop`), is flagged `IS_OUT_OF_RANGE` and excluded. |
| Recommendation | After each round, the tested condition with the highest measured mean over valid wells. The benchmark scores it by its true yield on the hidden surface, because measured means include instrument noise. |
| Evidence pack | `evidence.json` for one round: the LC/MS and NMR numbers per condition and per well, side by side, with the flags that fixed code raised, and a plate block with the plate flags and control means. It holds no hidden truth (no true yields, no fault log), and it is all the overwatch sees. |
| Overwatch | The data reviewer: the main model reads one round's evidence pack, gives each condition a verdict (accept, flag, or rerun), and names the faults it suspects, citing paths in the pack. Code checks every reply against fixed rules, for example that each number is found in the part of the pack the check allows, and sends a failing reply back at most twice before a person takes over. |
| True fault log | `fault_log.json`: the faults the simulators really applied to a plate in the reviewer evaluation, taken from their own records and never from a quality flag. No reviewer sees it, and `labauto eval-reviewer` scores each reviewer against it. |
| Test set | A folder under `examples/testsets/` with request files and a `manifest.json` that says, for each request, whether a deviation or an assumption is needed and what a faithful final design looks like. The sets are `core` (the five SPEC scenarios), `paraphrase` (the same five reworded), and `held_out` (eleven new kinds of request). |
| Valid, faithful | In the planner evaluation, a design is valid when it passed every layer of the check pipeline. A valid final design is also faithful when it meets the rules written beforehand for that request: the values, the controls, the replicates, and the number of conditions or wells. So a design can be valid and still not faithful. Whether a needed change was declared is a separate column. |

## The compiler analogy

The structure is the same as a compiler. Each level is checked before the next
one is built from it:

| Level | Name | Written by | Example |
|---|---|---|---|
| 0 | Request | a scientist, in plain language | "Screen catalyst loading at 1, 2, 4 and 8 mol % against base at 1.0, 1.5 and 2.0 equivalents, in triplicate." |
| 1 | Design | a model or a person | 12 conditions, 3 replicates, controls on |
| 2 | Plan | code | what goes into each well, in microliters. No vendor terms. |
| 3 | Worklist | code | the ordered list of transfers, with pipette and tip choices |
| 4 | Protocol | code | instrument code for one specific liquid handler |
| 5 | Simulation | the vendor's simulator (OT-2) or PyLabRobot (Tecan EVO) | a step log and a record of what was dispensed |

Of these levels, a model writes only level 1. Everything below it is fixed
code, so a mistake at a lower level is a bug in this repository, not a choice a
model made. Models also pick conditions in the closed loop (code then builds the
design), write a short report note, and review paired data. Code checks each of
those too.

## Short answers

### Why not let the model write the protocol directly?

Because the instrument's own simulator accepts protocols that are wrong. The
Opentrons simulator passes a 600 uL fill of a 360 uL well
(`test_simulator_accepts_overfill`), a 5 uL aspirate on the 20 to 300 uL pipette
(`test_simulator_accepts_below_minimum_volume`), and 18 mL drawn from a 15 mL
source well (`test_simulator_does_not_track_source_volumes`). A protocol written
by a model would have to avoid all of these by itself, with no check behind it.
Here the model writes a design of a few lines, and the rules in
`validate.py` catch those three cases as `WELL_OVERFILL`,
`VOLUME_OUT_OF_RANGE`, and `SOURCE_SHORT` before any protocol exists. The design
is also short enough for a person to read in full.

### Why keep a simulator when the rules already passed?

The rules are this repository's code, so they can share its blind spots. The
simulator is outside code (the vendor's own for the OT-2, the open-source
PyLabRobot for the Tecan EVO) and checks things the rules do not: on the
Tecan EVO, carriers that overlap on the rails pass the workcell rules and are
caught only by PyLabRobot
(`test_overlapping_carriers_pass_the_rules_but_not_the_simulator`). It also
proves that the generated protocol loads and runs: the labware names resolve,
the tips last, every aspirate has a tip. See
[tooling_notes.md](tooling_notes.md) for the full list of what each simulator
checks and misses.

### What does the ledger check add?

It checks the compiler. The ledger is what the simulator says was dispensed:
read back from the run log on the OT-2, and built from the calls the simulator
accepted on the Tecan EVO, whose tracked well volumes are also checked. The
check compares it with the plan for every well and reagent, and with the plan's
totals. A compiler bug that sent liquid to the wrong well, or the wrong volume,
would pass the rules and the simulator but fail here
(`test_pipeline_stops_at_the_ledger_layer`,
`test_ledger_check_catches_changed_missing_and_misplaced_volumes`). The ledger
also matters downstream: in the closed loop the simulated LC/MS reads the
ledger, not the plan, so the data follow what was dispensed
(`test_measured_yield_follows_what_was_dispensed`).

### What does the escalation ladder buy over more retries?

Three things. A rung is left as soon as an attempt repeats the previous
attempt's errors, so a model stuck on one mistake does not burn its retries
(`test_no_progress_leaves_the_rung_early`). An error that no design change can
fix, such as a deck conflict in the workcell file, goes straight to a person
(`test_needs_human_error_skips_to_the_person`). And the total number of model
calls per request is bounded, so a run always stops: awaiting approval, or
needing a person with its full history. An endpoint that stops answering moves
the run to FAILED, and `labauto resume` picks it up again.

Live, on a local model, the repair rounds did most of the work. In the latest
core run on qwen3.8:27b, 23 of 25 trials were valid on the first try and 25 of
25 after local repairs ([planner_eval.md](planner_eval.md)). In the earlier
qwen3.8:27b runs, 4 of 130 trials went to a person, each after a repair
repeated the same errors. The hosted rung has not run on a model, because no
hosted model is configured, so whether it rescues designs the local model
cannot fix is still open.

### What stops a model from quietly changing the experiment?

The reply format requires a `deviations` list and an `assumptions` list; a
reply without them fails parsing. Every declared change is shown in the
approval summary right after the request, before the attempts and the plan.
Approval records the design and protocol hashes, and any later change to either
voids it (`test_changed_protocol_voids_the_approval`). What this does not stop is a
model that changes something and does not declare it. The person reading the
summary is the check for that. A second model call that would compare the
request with the design (SPEC.md 8.13, Extension B) is not built. The overwatch
is a different reviewer: it judges measured data, not designs. The planner
evaluations check part of this on test requests: their Faithful column counts
passing designs that also meet rules written beforehand for what was asked
([planner_eval.md](planner_eval.md)).

### Why controls on every plate?

Because a plate can be wrong as a whole. With the `plate_drift@R` fault, every
product area on round R's plate reads 25% low. The reference control falls
outside its tolerance, the plate fails its quality checks, every well on it is
excluded, and under the envelope approval policy the next round goes to a person
(`test_plate_drift_fails_the_reference_control`,
`test_envelope_approves_a_normal_round_then_stops_after_an_anomaly`). Without
controls those low yields would go into the optimizer as real data.

### What would change on real hardware?

The parts behind the slots. The ledger would come from the instrument's own
logs and liquid level detection, not a simulator. Volumes would need calibrated
liquid classes, deck positions taught on the real deck, and handling for failed
tip pickups and clots. The LC/MS would give real chromatograms that need peak
integration and calibration curves. Real NMR spectra would carry effects the
simulator leaves out, listed in [nmr.md](nmr.md), so the fitter's errors would
be larger. Results would go to a LIMS or ELN through the Store slot. The
design, plan, and worklist layers and the approval gate would stay as they are.

### Did the model decider beat the optimizer, and what does that say?

Not across many surfaces yet. The benchmark ([benchmark.md](benchmark.md))
has so far run the random and optimizer strategies; its model row is pending.
Over 30 synthetic surfaces, the optimizer's recommendation after 6 rounds
averaged 81.8% true yield against a mean optimum of 82.3%, and was within 3
points of the optimum on a share of 1.00 of the surfaces. Random search reached
76.9% and a share of 0.37. Those are the numbers a model decider has to beat.

The model decider has run live once, on qwen3.8:27b through Ollama: one 6-round
campaign on surface seed 0. It chose rounds 2 to 6 with no fallback. In round
6, code refused its first two final answers because they repeated too many
tested conditions, and the third passed. It recommended catalyst 6.5 mol %,
base 1.75 equiv, co-solvent 20 %, measured at 86.6% over 9 wells, which is the
surface's grid optimum. One campaign on one surface is an example, not a
comparison. Its calls are in `examples/recordings/decider.jsonl`, and the demo
replays them. The fallback to the optimizer has been shown only by tests with
scripted replies.
