# Decisions

One line per decision where SPEC.md was silent, where it was wrong about a
library, or where a fallback was taken. Open problems are at the end.

## Environment and process

- The build machine runs Python 3.12. SPEC.md was checked on 3.13. Everything installs and runs on 3.12, which is also the CI version.
- The first commit on `main` holds only SPEC.md, CLAUDE.md, and `.gitignore`, so it predates `labauto demo`. Every merge after it passes `pytest -q` and `labauto demo`.
- The whole build ran unattended. No question was put to the owner, so each choice below was taken without review.
- ruff ignores E501 (line length): the formatter already wraps code, and the remaining long lines are message strings.
- ruff skips Markdown files. ruff 0.17 formats Python code blocks inside Markdown, and once rewrote two code blocks in SPEC.md. The file was restored from git.

## Stage 1

- `OT_API_CONFIG_DIR` works as SPEC.md says. Setting it to `.labauto/opentrons` before the first import keeps the library's config files out of the home folder.
- In process simulation does not leak state between runs in the test suite (`test_repeated_simulations_do_not_leak_state`), so there is no subprocess per simulation.
- The simulator's exception text carries Python tracebacks with absolute paths. The message kept in a `SIM_*` error is the simulator's top level `detail` text, with any absolute path replaced by `<path>`.
- Error mapping checks substrings in a fixed order. The unknown labware message also contains `KeyError`, so `FileNotFoundError` is checked before the `SIM_OTHER` fallback.
- `needs_human` for simulator and ledger errors: true for `SIM_DECK_CONFLICT`, `SIM_LABWARE_NOT_FOUND`, `SIM_OTHER`, and `LEDGER_MISMATCH` (no design change fixes them), false for `SIM_OUT_OF_TIPS` and `SIM_BAD_VOLUME`.
- Added fields: `PlanError.short` (a compact detail for tables), `LedgerEntry.dest_labware`, `CheckResult.input_kind`, `CheckResult.instrument`, `CheckResult.layer_status`, and `Worklist.workcell_id`, `Worklist.plan_id`, `Worklist.tips_needed`.
- Added error code `UNKNOWN_INSTRUMENT` (workcell layer, `needs_human` true) for a workcell whose instrument string has no backend.
- A pipette model the backend cannot map, a tip rack or plate missing from the deck, and an inventory well that its labware does not have are all reported as `UNKNOWN_LABWARE`.
- Both `grid` and `conditions` set fails parsing with `SCHEMA_INVALID`. Neither set is `EMPTY_DESIGN`.
- The Design model forbids unknown fields, so a misspelled field such as `replicate` fails with `SCHEMA_INVALID` instead of quietly taking the default.
- A design whose `assay_id` names a different assay fails with `SCHEMA_INVALID` at `assay_id`. A design's `workcell_id` is a note only, so one design file can run on any workcell.
- The same value listed twice in one grid list is reported as `DUPLICATE_CONDITION`.
- `VOLUME_OUT_OF_RANGE` groups rows by reagent and volume and lists the wells, to keep the error list short.
- A plan written by hand may leave out `totals_ul`. Code then adds it from the wells. A `totals_ul` that disagrees with the wells is a plan error (`TOTALS_MISMATCH`), not a compiler fault at the ledger.
- A design with no id gets `d-` plus the first 12 hex characters of its hash. A plan id follows its design id (`d-...` becomes `p-...`), so the same design gives the same plan on every workcell.
- A grid expands in the assay's factor order, so condition 0 takes the first listed value of every factor.
- The negative control has `conditions: null` in the plan, as in the SPEC.md example. The reference control carries its conditions.
- `summary.md` uses Markdown headings for its sections, lists reagent use with the most used reagent (as a share of usable volume) first, and prints both hashes in full.
- Run folder names end in four random hex characters. This is a name, not an experimental choice, so it is not seeded.
- `labauto demo` writes its run folders under `runs/`, which is gitignored.
- Added plan layer codes: `EMPTY_PLAN` (a plan with no wells) and `TOTALS_MISMATCH`. A plan whose `assay_id` names another assay fails with `SCHEMA_INVALID`, as a design does.
- Added workcell layer codes, both with `needs_human` true: `DUPLICATE_NAME` (two deck entries, pipettes, or mounts with one name) and `SOURCE_CONFLICT` (two reagents listed in one source well).
- The workcell rules check that the plan's plate is the assay's plate labware. Without this, a hand written plan could dispense into the reagent reservoir and pass every layer.
- One snapping rule for factor values. A value that passes the grid check is snapped to its exact step volume, and the plan stores the snapped value. Duplicate conditions are found by comparing volumes, so 2.0 and 2.0000001 count as the same.
- Protocol code writes volumes at full precision. Display text still rounds. Ledger messages show six decimals so a small difference is visible.
- Factor values in a design must be finite numbers: `true`, `"2"`, and NaN fail parsing. Volumes in a plan must be finite. Inventory volumes must be zero or more, and the dead volume fraction must be at least 0 and below 1. A workcell file that breaks these fails to load (exit code 2).
- Unknown fields are refused in nested design and plan parts too (`layout`, `created_by`, `deviations`, plan wells), so a typo such as `layout.sed` fails instead of silently changing the layout.
- `summary.md` counts tips per pipette from the worklist rows. If two pipettes share a rack, it reports per rack instead.
- Run records name the input file relative to the working folder, or by file name alone if it lies outside, so no home path lands in a record.
- pytest's temporary folders go to `.labauto/pytest` (set in `tests/conftest.py`), so the suite writes nothing outside the repository. `test_tmp_path_is_inside_the_repository` pins this.

## Stage 1b

- The Tecan plate is `cor_96_wellplate_360uL_Fb`, not `Microplate_96_Well` as Appendix A suggests. The latter holds 1437 uL per well, so the tracker would not catch an overfill.
- Tips: `DiTi_200ul_LiHa` for bulk and `DiTi_50ul_LiHa` for small transfers, mapped from the neutral keys `tips_200ul` and `tips_50ul` (already in `labware.py` since Stage 1).
- Tecan pipettes are LiHa channels. The workcell's `mount` names the channel (`channel_0`, `channel_1`). The small pipette covers 1 to 50 uL, the large 20 to 200 uL.
- The trash is placed at a fixed coordinate, because `EVO150Deck` has none and the deck only places Tecan resources on rails.
- Added field `SimResult.well_volumes`: the plate volumes kept by the simulator's tracker, when it keeps them. The ledger check compares them with the plan's well totals. This is how "confirm the plate's tracked volumes equal the plan" is done.
- The Tecan step log is one line per call the simulator accepted, written by this project. The chatterbox output is a table per call.
- When a tip rack runs out, the compiled calls wrap to the first spot, so the tip tracker catches it when the workcell rules are skipped.
- The demo table's first column is 29 characters wide, so `good_factorial (tecan_evo)` fits.
- The Tecan row in the demo also checks that its plan equals the OT-2 plan for the same design. If they differ, the row says so and the demo fails.
- The Instrument interface gained `valid_mount(mount)`. A pipette on a mount the instrument does not have is a `BAD_SLOT` workcell error with `needs_human` true. OT-2 mounts are `left` and `right`. Tecan mounts are `channel_0` to `channel_7`, written without leading zeros.
- A design with no id gets one from its content without the workcell id, so the same design file gives the same plan on every workcell.
- The Tecan artifact names the plan's plate, and only that plate's tracked volumes are read back. Another plate on the deck (a stock plate) is not part of the ledger.
- `protocol_tecan.py` uses deck names only as dictionary keys, so any name in a workcell file gives a valid script.
- If compiling fails, the pipeline reports `SIM_OTHER` at the simulator layer with `needs_human` true, instead of crashing.
- PyLabRobot's "does not fit at rails" error maps to `SIM_DECK_CONFLICT`. `HasTipError` (a channel that already holds a tip) maps to `SIM_OTHER`, because it means the generator forgot to drop a tip, not that a rack is empty.

## Stage 2

- Model server check (SPEC 8.2): there was no `.env`, and nothing answered at the address in `.env.example`. Three tries of `GET /v1/models`, two and a half minutes apart, all failed to connect. The owner had also said no model server would be available. Fallback taken as SPEC 8.2 names it: the stage is built and tested with `ScriptedClient`, and the README status table says live results are pending.
- Because no live session could be recorded, `examples/recordings/` holds transcripts of scripted sessions, named `<scenario>.scripted.jsonl`. Each one is the real `transcript.jsonl` of a `labauto plan` run whose planner replies were written by hand (`examples/scripted/<scenario>.json`). Their model field says `scripted`. The demo prefers a live recording `<scenario>.jsonl` when one exists, and prints which kind it replayed.
- `docs/planner_eval.md` is not written. `labauto eval-planner` refuses to run without a configured endpoint, so no numbers exist to publish.
- The hosted client sends no temperature: current hosted models reject sampling settings with an error. It carries the reply schema in the system prompt rather than the provider's structured output feature, because strict schemas need fixed keys and the design's `grid` has free factor names. A refusal from the hosted model counts as a failed attempt. The model name comes only from `LABAUTO_HOSTED_MODEL`.
- When a local server does not accept a JSON schema in `response_format`, the schema text is appended to the leading system message, not sent as a later system message. Some local chat templates refuse a system message after a user message.
- The model's reply is validated against `PlannerOutput`, which forbids unknown fields and requires `deviations` and `assumptions` to be present. A missing `deviations` list is not read as "no deviations" (P5).
- A planner design id is `d-<run id>-<attempt number>`, so every design names its run. Comparisons across runs ignore the id.
- `transcript.jsonl` lines carry `scope`, `attempt`, and `call` (1 for the first call, 2 for the parse retry). On resume, a call already recorded is served from the transcript and the model is not asked again. Scripted and replay clients are moved past the replies already used.
- Added fields to `state.json` beyond SPEC 8.6: `assay`, `workcell`, `seed`, `client`, `hosted_calls`, `final_attempt`, `design_hash`, `protocol_hash`, and `notes`. They let `labauto resume` rebuild the run with no other input.
- Each attempt folder also holds `attempt.json` (written last, so it marks a finished attempt) and `check.json` (the full check result). `final/` is a copy of the passing attempt's folder.
- An internal error (including a model endpoint that stops answering) moves the run to FAILED with the error and the state it happened in, and writes `error.txt` with path names removed. `labauto resume` puts a FAILED run back into the state it failed from.
- `LABAUTO_HOSTED_MAX_CALLS_PER_RUN` counts calls, including the parse retry. With a cap of 1 the hosted attempt gets no retry.
- No progress is judged on one rung: an attempt whose errors (codes and places) equal the previous attempt's on the same rung ends that rung.
- `labauto approve` without `--by` records `unnamed person`. User names from the system are never read.
- Approval is refused, not just voided, if the files in `final/` changed between the check and the approval.
- `NEEDS_HUMAN` uses kind `handoff` in the core build. Kind `question` belongs to Extension A, which is not built.
- `labauto plan` exit codes: 0 when the run awaits approval, 1 when it needs a person, 2 on an internal error or missing configuration.
- The planner commands live in `cli_plan.py` and the evaluation in `planner_eval.py`, to keep `cli.py` short. The evaluation's failure notes are built by code from the attempts: each error code, how often it appeared, and one real message as the example.
- "Valid after local repairs" counts trials that passed on the local rung, including those that passed on the first try. "Median seconds" is the model time summed over a trial's attempts.
- The small endpoint has no key setting of its own in `.env.example`, so it uses `LABAUTO_MAIN_API_KEY`.
- The planner's assay summary also says how many uL of each reagent one unit of its factor needs, so a model can reason about reagent limits.
