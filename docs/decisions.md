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
- With hosted provider `anthropic`, the client sends no temperature (current hosted models reject sampling settings) and carries the reply schema in the system prompt rather than the provider's structured output feature, because strict schemas need fixed keys and the design's `grid` has free factor names. A refusal counts as a failed attempt: the transcript keeps the model's real text (often empty) and marks the refusal in `stop_reason`, set by code. With provider `openai_compatible`, the hosted rung behaves exactly like the local client. The model name comes only from `LABAUTO_HOSTED_MODEL`.
- When a local server does not accept a JSON schema in `response_format`, the schema text is appended to the leading system message, not sent as a later system message. Some local chat templates refuse a system message after a user message.
- The model's reply is validated against `PlannerOutput`, which forbids unknown fields and requires `deviations` and `assumptions` to be present. A missing `deviations` list is not read as "no deviations" (P5).
- A planner design id is `d-<run id>-<attempt number>`, so every design names its run. Comparisons across runs ignore the id.
- `transcript.jsonl` lines carry `scope`, `attempt`, and `call` (1 for the first call, 2 for the parse retry). On resume, a call already recorded is served from the transcript and the model is not asked again. Scripted and replay clients are moved past the replies already used.
- Added fields to `state.json` beyond SPEC 8.6: `assay`, `workcell`, `seed`, `client`, `hosted_calls`, `final_attempt`, `design_hash`, `protocol_hash`, and `notes`. They let `labauto resume` rebuild the run with no other input.
- Each attempt folder also holds `attempt.json` (written last, so it marks a finished attempt) and `check.json` (the full check result). `final/` is a copy of the passing attempt's folder.
- An internal error (including a model endpoint that stops answering) moves the run to FAILED with the error and the state it happened in, and writes `error.txt` with home folder paths removed. `labauto resume` puts a FAILED run back into the state it failed from.
- `LABAUTO_HOSTED_MAX_CALLS_PER_RUN` counts calls, including the parse retry. With a cap of 1 the hosted attempt gets no retry. With a cap of 0 the hosted rung is skipped, and the record says so. Hosted calls are counted from the transcript, so a resume never counts a recorded call twice.
- No progress is judged on one rung: an attempt whose errors (codes and places) equal the previous attempt's on the same rung ends that rung.
- `labauto approve` without `--by` records `unnamed person`. User names from the system are never read.
- Approval is refused, not just voided, if the files in `final/` changed between the check and the approval.
- `NEEDS_HUMAN` uses kind `handoff` in the core build. Kind `question` belongs to Extension A, which is not built.
- `labauto plan` exit codes: 0 when the run awaits approval, 1 when it needs a person, 2 on an internal error or missing configuration.
- The planner commands live in `cli_plan.py` and the evaluation in `planner_eval.py`, to keep `cli.py` short. The evaluation's failure notes are built by code from the attempts: each error code, how often it appeared, and one real message as the example.
- "Valid after local repairs" counts trials that passed on the local rung, including those that passed on the first try. "Median seconds" is the model time summed over a trial's attempts.
- The small endpoint has no key setting of its own in `.env.example`, so it uses `LABAUTO_MAIN_API_KEY`.
- The planner's assay summary also says how many uL of each reagent one unit of its factor needs, so a model can reason about reagent limits.
- Each transcript line records the request actually sent: the messages after the schema text was added, and the temperature (empty when none was sent).
- The structured output probe caches only a real answer: yes on HTTP 200, no on a 4xx refusal. A probe that gets no answer raises "endpoint unavailable" and is tried again next time.
- Reply parsing removes reasoning first: closed `<think>` blocks, anything before a lone `</think>`, and an unclosed `<think>` to the end of the text (the model never finished, so nothing in it is an answer). It then lists every top level JSON object, fenced ones first, and takes the first that validates.
- A transcript line cut off by a crash is skipped on load, and that call is made again. `state.json` is written to a temporary file and renamed, so it is never half written.
- Replaying or scripting a session that reached the hosted rung switches the hosted rung on with a placeholder endpoint, so the replay walks the same ladder. The demo labels each recording from the model names inside it, never from its file name, and marks scripted rows on the row itself.
- `labauto eval-planner` refuses to write its page when no model replied in any trial. The page counts trials that ended FAILED in their own column and quotes the reason, and says in bold when the numbers come from a scripted client.
- Run records keep home folder paths out: `check.json` names the protocol by file name, `state.json` stores paths relative to the working folder or the repository when it can, and `summary.md` shows the run folder relative to the working folder.
- Resuming a run that already rests (awaiting approval, needing a person, approved, rejected) rewrites its `summary.md`, in case a crash came between the state change and the summary.
- The planner's workcell summary lists tips per rack, so a rack shared by two pipettes is counted once.

## Stage 3

- The ground truth, checked by `test_ground_truth_matches_the_published_numbers`, reproduces every Appendix C number: maximum 86.04% at 6.5 mol %, 1.75 equivalents, 20% co-solvent; 13 conditions within 3 points and 29 within 5; reference control 50.61%; negative control 0.
- Appendix C's optimizer and random reference numbers hold the surface at seed 0 and vary only the campaign seed. The benchmark protocol (SPEC 9.10) gives each seed its own surface. The two are different experiments. `labauto benchmark --surface-seed 0` runs the Appendix C version, and its output is in `docs/benchmark_check.md`.
- For surface seed 0 the reference control's expected value is the assay's `yield_center` (50). For any other surface it is recomputed from the surface, as SPEC 9.2 says.
- A condition leaves the candidate list once it has at least one valid measurement. Conditions whose only plate failed quality checks stay candidates, because nothing valid is known about them.
- The model decider counts any condition already put on a plate (valid or not) toward its 2-repeat limit.
- A model decider turn that asks for a tool after the 6-call limit counts as an invalid answer. So does an unreadable reply. Each round therefore ends after at most 6 tool calls and 3 invalid answers.
- The model decider's prompt names N in the user message ("Pick N = 8 conditions"). The system prompt is Appendix B's text unchanged.
- Peak areas are written with 4 decimals, so the analysis reads back the raw peak table with no loss that matters (zero noise recovers the true yield to 1e-6).
- The campaign's analysis step reads `raw/lcms_peaks.csv` back from disk, so the results come from the raw data file, not from values held in memory.
- `results_all.csv` holds as-designed (`designed_<factor>`) and as-run (`asrun_<factor>`) values side by side. `decided_by` is `lhs` for round 1, else the strategy, plus the model name and any fallback.
- A round's approval is checked again just before the plate is read: both hashes are recomputed from the round's files, and a mismatch voids the approval and waits for a person.
- `labauto approve <campaign>` approves the round the campaign waits on, and `labauto resume <campaign>` continues it. Under the envelope policy, the person's approval of round 1 creates `envelope.json`. A later person approval keeps the existing envelope.
- Under `auto_demo` every round is approved by the scripted approver, labelled `demo_auto` in `approval.json`, the round summary, and the report.
- The benchmark's fast path keeps the same decider, LC/MS, analysis, and quality checks as the full loop. Only compile, simulate, and the ledger check are skipped.
- The model strategy in the benchmark needs a live endpoint. None was available, so it was not run, and `docs/benchmark.md` says so.
- The demo's model decider rows replay a scripted session (`examples/recordings/decider.scripted.jsonl`, made from `examples/scripted/decider.json`). Its picks are the corners of two fixed coarse grids, chosen without looking at any data or at the hidden surface. Every pick says "scripted pick" or "scripted repeat", and the demo, the round summaries, and the report say it is scripted. A first draft of these picks clustered near the known optimum and was replaced, because a scripted stand in must not use knowledge the model would not have.
- Stage 3 data contracts (Decision, History, QcReport, CampaignConfig, Envelope, CampaignState) live in `models.py`. Models used only inside one module (LC/MS settings, readings, the decider's reply shape) stay in that module.
- The optimizer's `results` tool takes an argument named `round`, as the model sees it. Inside that function the builtin `round` is reached through `builtins`.
- The approval envelope compares what a round will really run with what the person approved: the assay and workcell ids in the round's own design, and sha256 hashes of the assay and workcell files taken at approval. A swapped or edited workcell file sends the round to a person.
- The envelope's automatic round is charged before the round's approval is written. If a crash falls between the two, the next round goes to a person one round early; that errs toward a person. A round whose approval is already on file, with hashes that still match, is not decided or charged again on resume.
- Rows for a round are appended to `results_all.csv` only if none for that round are there yet, and `state.json` is saved right after, so a crash cannot duplicate a round.
- A model decider tool call with unusable arguments (a missing factor, a word where a number goes) is sent back to the model as an error and counts as an invalid answer, so the 2 retries and the fallback still apply. A pick with an empty reason is invalid.
- With fewer than 2 conditions that have valid measurements, the optimizer has no model. `suggest` then draws a Latin hypercube from untested conditions and returns no prediction, rather than a made up 0 +/- 1. With no untested conditions left, `suggest` returns nothing.
- Round 1's decision is recorded with strategy `lhs` and its design with `created_by.role` `code`, whatever the campaign's strategy.
- Round summaries and the report say who decided and who wrote any note: code, a scripted client, or a named model. "Model written" is used only for a real model.
- The model note is requested at the end of a campaign only when a live client is configured, and on demand with `labauto report <campaign> --note --client ...`. The note goes through the router's `report_note` role and is recorded in the campaign's `transcript.jsonl`. No endpoint was available, so no campaign here carries a model note.
- The note number check counts every run of digits, with a leading dot or inside a word, and scientific notation as one number, so a number cannot pass by its spelling. An empty note gets its own rejection line.
- `--fault` accepts only `injection_failure` and `plate_drift@R` with R inside the campaign; anything else is refused before the campaign starts.
- The benchmark counts model decider rounds that fell back to the optimizer and prints the count beside the model row. Its plot link is relative to the page, and the exact command names `--out` and `--png` whenever they differ from the defaults.
- `MemoryStore` was removed. The benchmark keeps its rows in a plain list, and `LocalStore` is the one Store implementation, as SPEC 9.5 says.

## Open problems

- No model has run. Every model path (planner, hosted rung, model decider, report note) is built and tested with scripted clients only. Live results are pending: the planner evaluation (`docs/planner_eval.md`), recorded model sessions in `examples/recordings/`, the model decider row of the benchmark, and a model note in any campaign report. Each needs `labauto eval-planner`, `labauto plan`, `labauto benchmark`, or `labauto loop --strategy model` run against a configured endpoint.
- The model decider's prompt and the 120 word report note limit have never met a real model, so how often real replies need the retries and the fallback is unknown.
- PyLabRobot's `DiTi_*_LiHa` tip definitions warn that their total tip length is zero or less. This does not affect the bookkeeping here, but a backend driving real hardware would need correct tip definitions.
- No stage hit the stuck rule (three failed approaches to one problem). Stage 1b finished well inside its effort cap.
