# Concepts

## Vocabulary

| Term | Meaning here |
|

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
| 5 | Simulation | the vendor's simulator | a step log and a record of what was dispensed |

A model writes only level 1. Everything below it is fixed code, so a mistake at
a lower level is a bug in this repository, not a choice a model made.

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
simulator is the vendor's code and checks things the rules do not: on the
Tecan EVO, carriers that overlap on the rails pass the workcell rules and are
caught only by PyLabRobot
(`test_overlapping_carriers_pass_the_rules_but_not_the_simulator`). It also
proves that the generated protocol loads and runs: the labware names resolve,
the tips last, every aspirate has a tip. See
[tooling_notes.md](tooling_notes.md) for the full list of what each simulator
checks and misses.

### What does the ledger check add?

It checks the compiler. The ledger is what the simulator says was dispensed,
read back from its run log. The check compares it with the plan for every well
and reagent, and with the plan's totals. A compiler bug that sent liquid to the
wrong well, or the wrong volume, would pass the rules and the simulator but
fail here (`test_pipeline_stops_at_the_ledger_layer`). The ledger also matters
downstream: in the closed loop the simulated LC/MS reads the ledger, not the
plan, so the data follow what was dispensed
(`test_measured_yield_follows_what_was_dispensed`).

### What does the escalation ladder buy over more retries?

Three things. A rung is left as soon as an attempt repeats the previous
attempt's errors, so a model stuck on one mistake does not burn its retries
(`test_no_progress_leaves_the_rung_early`). An error that no design change can
fix, such as a deck conflict in the workcell file, goes straight to a person
(`test_needs_human_error_skips_to_the_person`). And the total number of model
calls per request is bounded, so a run always ends: approved, rejected, or
waiting for a person with its full history. Whether the hosted rung rescues
designs the local model cannot fix is a question for live data, which is
pending.

### What stops a model from quietly changing the experiment?

The reply format requires a `deviations` list and an `assumptions` list; a
reply without them fails parsing. Every declared change is shown at the top of
the approval summary, before anything else. Approval records the design and
protocol hashes, and any later change to either voids it
(`test_changed_protocol_voids_the_approval`). What this does not stop is a
model that changes something and does not declare it. The person reading the
summary is the check for that. The reviewer role that would compare the
request with the design is an extension and is not built.

### Why controls on every plate?

Because a plate can be wrong as a whole. With the `plate_drift` fault, every
product area on one plate reads 25% low. The reference control falls outside
its tolerance, the plate fails its quality checks, every well on it is excluded,
and the next round needs a person
(`test_plate_drift_fails_the_reference_control`,
`test_envelope_approves_a_normal_round_then_stops_after_an_anomaly`). Without
controls those low yields would go into the optimizer as real data.

### What would change on real hardware?

The parts behind the slots. The ledger would come from the instrument's own
logs and liquid level detection, not a simulator. Volumes would need calibrated
liquid classes, deck positions taught on the real deck, and handling for failed
tip pickups and clots. The LC/MS would give real chromatograms that need peak
integration and calibration curves. Results would go to a LIMS or ELN through
the Store slot. The design, plan, and worklist layers and the approval gate
would stay as they are.

### Did the model decider beat the optimizer, and what does that say?

It has not been measured. No model endpoint was available when this was built,
so the benchmark ran only the random and optimizer strategies
([benchmark.md](benchmark.md)). Over 30 synthetic surfaces, the optimizer's
recommendation after 6 rounds averaged 81.8% true yield against a mean
optimum of 82.3%, and was within 3 points of the optimum on a share of
1.00 of the surfaces. Random search reached 76.9% and a share of
0.37. Those are the numbers a model decider has to beat. Until it is
run, the only claim is that the tool loop, its checks, and its fallback work,
which the tests show with scripted replies.
