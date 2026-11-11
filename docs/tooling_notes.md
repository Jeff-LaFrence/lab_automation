# Tooling notes

What each simulator checks, what it misses, and the test that pins each point.
If a library upgrade changes any of these, the named test fails and this file
must be updated.

## Opentrons simulator (opentrons 9.0.0, apiLevel 2.20)

The OT-2 backend writes a Python protocol and runs it with
`opentrons.simulate.simulate` in the same process. The simulator is the
vendor's own code, so it is an independent second opinion on the protocol this
project generates. It is not a model of liquids.

Version note: `opentrons` 10.1.0 refuses OT-2 protocols, so the version is
pinned to 9.0.0.

### What it catches

| Problem | Error code here | Test |
|---|---|---|
| Running out of tips | `SIM_OUT_OF_TIPS` | `test_simulator_catches_tip_shortfall_when_rules_are_skipped` |
| Two labware in one slot | `SIM_DECK_CONFLICT` | `test_simulator_catches_deck_conflict_when_rules_are_skipped` |
| A slot number that does not exist (13) | `SIM_DECK_CONFLICT` | `test_simulator_rejects_invalid_slot` |
| Aspirating more than the tip holds | `SIM_BAD_VOLUME` | `test_simulator_error_mapping_for_bad_volume_and_unknown_labware` |
| Dispensing more than was aspirated | `SIM_BAD_VOLUME` | `test_simulator_rejects_dispense_above_aspirated` |
| An unknown labware name | `SIM_LABWARE_NOT_FOUND` | `test_simulator_error_mapping_for_bad_volume_and_unknown_labware` |
| Aspirating with no tip attached | `SIM_OTHER` | `test_simulator_error_mapping_for_bad_volume_and_unknown_labware` |

Every failure arrives as one exception type,
`opentrons.util.entrypoint_util.ProtocolEngineExecuteError`. The code here is
chosen by looking for a fixed list of substrings in its text, in order. The
message for an unknown labware name also contains the word `KeyError`, so the
`FileNotFoundError` check runs before anything falls through to `SIM_OTHER`.

The raw exception text includes Python tracebacks with absolute file paths.
The message saved in `errors.json` keeps the simulator's own top level error
text and replaces any absolute path with `<path>`.

### What it misses

These blind spots are the reason the plan and workcell rules exist. Each test
asserts that the simulator accepts the bad protocol.

| Blind spot | Test that pins it | Rule here that covers it |
|---|---|---|
| A well filled past its capacity (600 uL into a 360 uL well) | `test_simulator_accepts_overfill`, `test_simulator_accepts_overfill_through_the_pipeline` | `WELL_OVERFILL` (`test_well_overfill_reported_first`) |
| A volume below the pipette's minimum (5 uL on the 20 to 300 uL pipette) | `test_simulator_accepts_below_minimum_volume` | `VOLUME_OUT_OF_RANGE` (`test_volume_out_of_range`) |
| Drawing more from a source well than it holds (18 mL from a 15 mL well) | `test_simulator_does_not_track_source_volumes` | `SOURCE_SHORT` (`test_source_short_carries_numbers`) |

It also does not model liquid behavior, timing, evaporation, or physical
collisions beyond deck slot occupancy.

### Run log facts this project depends on

The ledger (what was dispensed into each well) is read from the run log.
`test_run_log_strings_are_stable` guards the text prefixes `Picking up tip`,
`Aspirating`, and `Dispensing`. `test_ledger_equals_plan` checks that the
ledger built from the log equals the plan for every well and reagent.

`transfer()` with a volume above the pipette's maximum is not an error in the
simulator: it splits the volume into several aspirations. This project writes
one explicit aspirate and one dispense per worklist row instead, so the run
log lines up with the worklist.

### Side effects

The library writes configuration files on import. Setting the environment
variable `OT_API_CONFIG_DIR` to `.labauto/opentrons` before the import keeps
them inside this project. The library also prints notices about missing robot
settings and calibration files. They are captured and kept out of the CLI
output.

Repeated simulations in one process give the same step log
(`test_repeated_simulations_do_not_leak_state`), so the backend does not need a
subprocess per simulation.

## PyLabRobot chatterbox backend for the Tecan EVO (pylabrobot 0.2.2)

Install with `pip install -e ".[tecan]"`. Tests that need it skip without it,
and the demo row prints `SKIPPED (pylabrobot not installed)`.

The Tecan backend writes `protocol_tecan.json`, a list of calls, and
`protocol_tecan.py`, a PyLabRobot script that makes the same calls and runs on
its own (`test_readable_script_runs_on_its_own`). `simulate` replays the JSON
calls against `LiquidHandlerChatterboxBackend` on an `EVO150Deck`, one channel
at a time, with tip tracking and volume tracking on. The source wells start at
the volumes in the workcell inventory, set with `set_volume`.

This is bookkeeping only. It checks tips and volumes. It does not check
collisions, calibration, liquid classes, or timing. It has never driven a real
instrument.

### Parts used

| Neutral key | PyLabRobot resource | Notes |
|---|---|---|
| `tips_200ul` | `DiTi_200ul_LiHa` | 220 uL per tip. Used for bulk transfers. |
| `tips_50ul` | `DiTi_50ul_LiHa` | 55 uL per tip. Used for small transfers. |
| `plate_96_flat_360ul` | `cor_96_wellplate_360uL_Fb` | 360 uL wells, so the tracker sees an overfill. |
| `reservoir_12_15ml` | `celltreat_12_troughplate_15mL_Vb` | 12 troughs. |
| carriers | `DiTi_3Pos`, `MP_3Pos` | Positions are `{"carrier", "rails", "site"}`. |

Library facts found while building, which differ from SPEC.md Appendix A:

- `Microplate_96_Well` holds 1437 uL per well. With it, a 600 uL overfill would pass the tracker. The Corning 360 uL plate definition is used instead.
- `CellTreat_12_troughplate_15000ul_Vb` is deprecated in 0.2.2. The lower case `celltreat_12_troughplate_15mL_Vb` replaces it.
- `EVO150Deck` has no trash, so `discard_tips` fails. A `Trash` resource is placed at a fixed coordinate. It cannot go on the rails, because the EVO deck only places Tecan resources on rails.
- The `DiTi_*_LiHa` tip definitions print a warning that the total tip length is zero or less. It does not affect tip or volume bookkeeping. It is captured and kept out of the CLI output.
- The chatterbox backend prints a table per call. The step log in `sim_log.txt` is written by this project instead: one plain line for each call the simulator accepted.

### What it catches

| Problem | Error code here | Test |
|---|---|---|
| A well filled past its capacity | `SIM_BAD_VOLUME` (`TooLittleVolumeError`) | `test_overfill_is_caught_by_the_tracker` |
| Drawing more from a source well than it holds | `SIM_BAD_VOLUME` (`TooLittleLiquidError`) | `test_overdrawn_source_is_caught_by_the_tracker` |
| Aspirating more than the tip holds | `SIM_BAD_VOLUME` | `test_aspirate_above_tip_capacity_is_caught` |
| Picking a tip from an empty spot (rack used up) | `SIM_OUT_OF_TIPS` | `test_tip_shortfall_is_caught_by_the_tracker` |
| Two labware on one carrier site | `SIM_DECK_CONFLICT` | `test_deck_conflict_is_caught_by_the_simulator` |
| Carriers that overlap on the rails | `SIM_DECK_CONFLICT` | `test_overlapping_carriers_pass_the_rules_but_not_the_simulator` |

So the tracker covers two of the Opentrons simulator's blind spots: overfilled
wells and source wells drawn dry. It also catches overlapping carriers, which
the workcell rules do not (they only compare positions for equality).

When a tip rack runs out, the compiled calls wrap back to the first tip spot,
as a naive program would. The tip tracker then reports the empty spot.

### What it misses

| Blind spot | Test that pins it | Rule here that covers it |
|---|---|---|
| A volume below the channel's minimum (5 uL on the 20 to 200 uL channel) | `test_simulator_accepts_below_minimum_volume` (Tecan) | `VOLUME_OUT_OF_RANGE` |

It also does not model the dispense height (`above_liquid` or `in_liquid`),
liquid classes, collisions of the arm with labware, or timing.

### Ledger

Each dispense is recorded as it is issued, with the reagent of that channel's
last aspirate. After the run, the plate's tracked volume in every well is
carried back in `SimResult.well_volumes`, and the ledger check confirms it
equals the plan (`test_ledger_and_tracked_volumes_match_the_plan`).
