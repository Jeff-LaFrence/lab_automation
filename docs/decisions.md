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
