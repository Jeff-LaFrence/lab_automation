# Rules for coding agents working in this repository

SPEC.md is the source of truth. Read it in full before changing anything. Re-read the section for the current stage before starting it, and again after any context reset.

- Build in stage order: 1, 1b, 2, 3. Do not build ahead. Finish a stage's Core before any Extension. Build Extensions and Stage 4 items only when the owner asks.
- `main` always passes `pytest -q` and `labauto demo`. Work on `stage-N` branches. Merge and tag only when the stage's Done checklist is true.
- Python packages go in `.venv` only. No sudo. No system changes.
- No force push, no history rewriting, no `git reset --hard`, no `git clean`. Do not push unless asked. Never bypass a commit hook. If a hook blocks a commit, fix the content.
- This repository is public. No secrets, keys, hostnames, user names, or home directory paths in committed files. No names of people. Nothing copied from outside this repository.
- Never fake results. Numbers in the README and docs come from files this code generated. If something is not built, the status table says so.
- When stuck after three approaches, log it in `docs/decisions.md`, take the fallback SPEC.md names, and move on.
- Prose style: plain words, short sentences, no em dashes, no hype.
- If `CLAUDE.local.md` exists, it holds local rules for this machine. Follow them. Nothing from local notes is ever copied into committed files.

Commands

- Setup: `python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"`
- Tests: `pytest -q`
- Lint: `ruff check . && ruff format --check .`
- Demo: `labauto demo`
