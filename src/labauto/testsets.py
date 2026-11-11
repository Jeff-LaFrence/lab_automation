"""Test sets for the planner evaluation, and the faithful check.

A test set is a folder under examples/testsets/ that holds manifest.json and
the request files it names. For each request, the manifest says what a
faithful answer looks like: rules on the final design, written by a person
from the request, with the arithmetic behind them.

Passing every check is not enough. A design that passes can still drop the
point of the request, for example by lowering the catalyst when the scientist
asked for 10 mol % everywhere. faithful_failures says, in plain words, which
rules the final design breaks.

Values in the rules are compared the way the design rules compare them: by
the volume step they give on the assay's grid. So 1 and 1.0 match, and 1.2
does not match 1.0.
"""

from __future__ import annotations

import itertools
import json
import os
import re
from pathlib import Path
from typing import Annotated

from pydantic import (
    BaseModel,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    ValidationError,
    model_validator,
)

from labauto.config import (
    condition_key,
    fmt,
    in_range,
    load_assay,
    load_workcell,
    on_grid,
    resolve,
)
from labauto.models import Assay, Design, FactorValue, Strict_
from labauto.record import portable
from labauto.validate import design_conditions

TESTSETS_DIR = "examples/testsets"
MANIFEST = "manifest.json"
CORE_SET = "core"
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"

Name = Annotated[str, StringConstraints(pattern=NAME_PATTERN)]
Text = Annotated[str, StringConstraints(min_length=1)]
Count = Annotated[StrictInt, Field(ge=0)]


class SetError(ValueError):
    """A test set that cannot be used: a bad manifest, a missing file, or a broken reference."""


class UnknownSet(SetError):
    """No test set has this name."""

    def __init__(self, name: str, known: list[str]):
        self.name = name
        self.known = known
        listed = ", ".join(known) if known else "none"
        super().__init__(f"unknown test set '{name}'. Known sets: {listed}")


# ---------------------------------------------------------------- the manifest


class FaithfulRules(Strict_):
    """Rules on the final design. All are optional. Every rule given must hold."""

    grid: dict[str, list[FactorValue]] | None = None
    conditions: list[dict[str, FactorValue]] | None = None
    fixed: dict[str, FactorValue] | None = None
    allowed: dict[str, list[FactorValue]] | None = None
    required_values: dict[str, list[FactorValue]] | None = None
    required_any: dict[str, list[FactorValue]] | None = None
    replicates: list[Annotated[StrictInt, Field(ge=1)]] | None = None
    include_controls: StrictBool | None = None
    min_conditions: Count | None = None
    max_conditions: Count | None = None
    min_sample_wells: Count | None = None
    max_sample_wells: Count | None = None

    @model_validator(mode="after")
    def _bounds_in_order(self) -> FaithfulRules:
        for low, high in (
            ("min_conditions", "max_conditions"),
            ("min_sample_wells", "max_sample_wells"),
        ):
            a, b = getattr(self, low), getattr(self, high)
            if a is not None and b is not None and a > b:
                raise ValueError(f"{low} ({a}) is larger than {high} ({b})")
        return self


class Expect(Strict_):
    """What should happen for one request."""

    as_asked_valid: StrictBool | None
    deviation_required: StrictBool
    assumption_required: StrictBool
    faithful: FaithfulRules = Field(default_factory=FaithfulRules)


class RequestEntry(Strict_):
    """One request in a manifest, as written."""

    id: Name
    file: Text
    intent: Text
    arithmetic: Text
    assay: Text | None = None
    workcell: Text | None = None
    expect_from: Text | None = None
    expect: Expect | None = None

    @model_validator(mode="after")
    def _one_expect(self) -> RequestEntry:
        if (self.expect is None) == (self.expect_from is None):
            raise ValueError("give exactly one of 'expect' and 'expect_from'")
        return self


class Manifest(Strict_):
    """A test set's manifest.json, as written."""

    set: Name
    description: Text
    assay: Text
    workcell: Text
    requests: list[RequestEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> Manifest:
        seen: set[str] = set()
        for r in self.requests:
            if r.id in seen:
                raise ValueError(f"request id '{r.id}' appears more than once")
            seen.add(r.id)
        return self


# ---------------------------------------------------------------- a loaded set


class SetRequest(BaseModel):
    """One request of a loaded test set, with its file read and its expectations resolved."""

    id: str
    file: Path
    text: str
    assay: str
    workcell: str
    intent: str
    arithmetic: str
    expect: Expect
    expect_source: str  # "<set>/<id>" of the entry the expectations came from


class LoadedSet(BaseModel):
    """A test set ready to run."""

    name: str
    description: str
    manifest_path: Path
    requests: list[SetRequest]

    def ids(self) -> list[str]:
        return [r.id for r in self.requests]

    def get(self, request_id: str) -> SetRequest:
        for r in self.requests:
            if r.id == request_id:
                return r
        raise SetError(
            f"set '{self.name}' has no request '{request_id}'. Its requests: {', '.join(self.ids())}"
        )


def testsets_root() -> Path:
    """The folder that holds the committed test sets."""
    return resolve(TESTSETS_DIR)


def list_sets() -> list[str]:
    """Names of the committed test sets: every folder under examples/testsets with a manifest."""
    root = testsets_root()
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if (d / MANIFEST).is_file())


def display_path(path: Path) -> str:
    """A path safe to print in a committed page: relative to here or the repository, never a home path."""
    shown = portable(path)
    if not Path(shown).is_absolute():
        return shown
    # Outside the working folder and the repository: only the last name, so no user or home
    # folder name can reach a page that gets committed.
    return "<outside the repository>/" + Path(path).name


def find_manifest(name_or_path: str | Path) -> Path:
    """The manifest file for a set name (a folder under examples/testsets) or a path.

    A bare name is looked up under examples/testsets. Anything with a folder
    in it, or ending in .json, is read as a path to a set folder or a manifest.
    """
    text = str(name_or_path)
    p = Path(text)
    if len(p.parts) > 1 or p.is_absolute() or p.suffix == ".json":
        path = resolve(p)
        if path.is_dir():
            path = path / MANIFEST
        if not path.is_file():
            raise SetError(f"no test set manifest at {text}")
        return path
    path = testsets_root() / text / MANIFEST
    if not re.match(NAME_PATTERN, text) or not path.is_file():
        raise UnknownSet(text, list_sets())
    return path


def read_manifest(path: Path) -> Manifest:
    """Read and check one manifest. Unknown keys are errors."""
    shown = display_path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SetError(f"{shown}: cannot be read as JSON: {exc}") from exc
    try:
        manifest = Manifest.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(x) for x in e['loc']) or 'top level'}: {e['msg']}" for e in exc.errors()
        )
        raise SetError(f"{shown}: {problems}") from exc
    if manifest.set != path.parent.name:
        raise SetError(
            f"{shown}: 'set' is '{manifest.set}' but the folder is '{path.parent.name}'. They must match."
        )
    return manifest


def _other_manifest(from_manifest: Path, set_name: str) -> Path | None:
    """Where a set named in expect_from lives: next to this set, or under examples/testsets."""
    if not re.match(NAME_PATTERN, set_name):
        return None
    for candidate in (
        from_manifest.parent.parent / set_name / MANIFEST,
        testsets_root() / set_name / MANIFEST,
    ):
        if candidate.is_file():
            return candidate
    return None


def resolve_expect(
    manifest_path: Path, manifest: Manifest, entry: RequestEntry
) -> tuple[Expect, str]:
    """Follow expect_from until an entry gives its own expectations.

    Returns the expectations and "<set>/<id>" of the entry that gave them. A
    missing target or a cycle is an error that names the chain.
    """
    start = f"{manifest.set}/{entry.id}"
    chain = [start]
    path = manifest_path
    while entry.expect is None:
        target = entry.expect_from or ""
        set_name, sep, request_id = target.partition("/")
        if not sep or not set_name or not request_id:
            raise SetError(f"{start}: expect_from must look like '<set>/<id>', not '{target}'")
        if target in chain:
            raise SetError(f"{start}: expect_from makes a cycle: {' -> '.join(chain + [target])}")
        chain.append(target)
        other = _other_manifest(path, set_name)
        if other is None:
            raise SetError(
                f"{start}: expect_from names '{target}', but there is no test set '{set_name}'"
            )
        path = other
        manifest = read_manifest(path)
        matches = [r for r in manifest.requests if r.id == request_id]
        if not matches:
            raise SetError(
                f"{start}: expect_from names '{target}', but set '{set_name}' has no request '{request_id}'"
            )
        entry = matches[0]
    return entry.expect, f"{manifest.set}/{entry.id}"


def check_rules(rules: FaithfulRules, assay: Assay, where: str) -> None:
    """Refuse rules that no valid design could meet: unknown factors and values the assay does not allow."""
    factors = assay.factors

    def check_value(rule: str, factor: str, value: float) -> None:
        if factor not in factors:
            raise SetError(
                f"{where}: faithful.{rule} names factor '{factor}', which the assay does not have "
                f"(it has {', '.join(factors)})"
            )
        spec = factors[factor]
        if not (in_range(value, spec) and on_grid(value, spec)):
            raise SetError(
                f"{where}: faithful.{rule} gives {factor} {fmt(value)}, which is not an allowed value, "
                "so no design that passes the checks can use it"
            )

    for f, v in (rules.fixed or {}).items():
        check_value("fixed", f, v)
    for rule in ("grid", "allowed", "required_values", "required_any"):
        for f, values in (getattr(rules, rule) or {}).items():
            if not values:
                raise SetError(f"{where}: faithful.{rule} gives no values for {f}")
            for v in values:
                check_value(rule, f, v)
    for i, cond in enumerate(rules.conditions or []):
        for f, v in cond.items():
            check_value(f"conditions[{i}]", f, v)
    for rule, conds in (
        ("grid", [rules.grid] if rules.grid else []),
        ("conditions", rules.conditions or []),
    ):
        for cond in conds:
            left_out = [f for f in factors if f not in cond]
            if left_out:
                raise SetError(
                    f"{where}: faithful.{rule} must name every factor; it leaves out {', '.join(left_out)}"
                )


def load_set(name_or_path: str | Path) -> LoadedSet:
    """Read a test set by name or path, with every file, path, and expect_from resolved and checked."""
    path = find_manifest(name_or_path)
    manifest = read_manifest(path)
    assays: dict[str, Assay] = {}
    requests = []
    for entry in manifest.requests:
        where = f"{manifest.set}/{entry.id}"
        text_path = Path(os.path.normpath(path.parent / entry.file))
        if not text_path.is_file():
            raise SetError(f"{where}: request file {entry.file} does not exist")
        text = text_path.read_text(encoding="utf-8").strip()
        if not text:
            raise SetError(f"{where}: request file {entry.file} is empty")
        assay_path = entry.assay or manifest.assay
        workcell_path = entry.workcell or manifest.workcell
        for kind, config in (("assay", assay_path), ("workcell", workcell_path)):
            if not resolve(config).is_file():
                raise SetError(f"{where}: {kind} file {config} does not exist")
        expect, source = resolve_expect(path, manifest, entry)
        if assay_path not in assays:
            try:
                assays[assay_path] = load_assay(assay_path)
                load_workcell(workcell_path)
            except Exception as exc:  # a config that is not an assay or workcell
                raise SetError(
                    f"{where}: cannot load {assay_path} or {workcell_path}: {exc}"
                ) from exc
        check_rules(expect.faithful, assays[assay_path], where)
        requests.append(
            SetRequest(
                id=entry.id,
                file=text_path,
                text=text,
                assay=assay_path,
                workcell=workcell_path,
                intent=entry.intent,
                arithmetic=entry.arithmetic,
                expect=expect,
                expect_source=source,
            )
        )
    return LoadedSet(
        name=manifest.set,
        description=manifest.description,
        manifest_path=path,
        requests=requests,
    )


def set_label(tset: LoadedSet) -> str:
    """How to name the set on the command line: its name if it is committed, else its folder."""
    try:
        if find_manifest(tset.name).resolve() == tset.manifest_path.resolve():
            return tset.name
    except SetError:
        pass
    return display_path(tset.manifest_path.parent)


# ---------------------------------------------------------------- the faithful check


def _cond_text(cond: dict[str, float], assay: Assay) -> str:
    """A condition in words, in the assay's factor order: catalyst_mol_pct 2.0, base_equiv 1.5."""
    order = [f for f in assay.factors if f in cond] + [f for f in cond if f not in assay.factors]
    return ", ".join(f"{f} {fmt(cond[f])}" for f in order)


def _values_text(values: list[float]) -> str:
    return ", ".join(fmt(v) for v in values)


def _set_mismatch(
    rule: str, wanted: list[dict[str, float]], have: list[dict[str, float]], assay: Assay
) -> str | None:
    """Compare two condition sets on the volume grid. None when they are the same set."""
    want = {condition_key(c, assay.factors): c for c in wanted}
    got = {condition_key(c, assay.factors): c for c in have}
    missing = [c for k, c in want.items() if k not in got]
    extra = [c for k, c in got.items() if k not in want]
    if not missing and not extra:
        return None
    parts = []
    if missing:
        parts.append(
            f"{len(missing)} asked for are missing, for example {_cond_text(missing[0], assay)}"
        )
    if extra:
        parts.append(f"{len(extra)} were not asked for, for example {_cond_text(extra[0], assay)}")
    return (
        f"{rule}: {len(want)} conditions were asked for and the design has {len(got)}; "
        + "; ".join(parts)
    )


def faithful_failures(design: Design, assay: Assay, rules: FaithfulRules) -> list[str]:
    """Every rule the final design breaks, one plain sentence per rule. Empty means faithful.

    Each reason starts with the rule's name and a colon, for example
    "fixed: ...", so reasons can be grouped by rule.
    """
    conds = design_conditions(design, assay)
    out: list[str] = []

    def step(factor: str, value: float) -> object:
        """The volume step a value gives, the same key condition_key uses."""
        spec = assay.factors.get(factor)
        if spec is None:
            return round(float(value), 9)
        return condition_key({factor: value}, {factor: spec})[0]

    def used(factor: str) -> list[float]:
        """The distinct values the design uses for a factor, in first-seen order."""
        seen: dict[object, float] = {}
        for c in conds:
            if factor in c:
                seen.setdefault(step(factor, c[factor]), c[factor])
        return list(seen.values())

    def missing_factor(factor: str) -> int:
        return sum(1 for c in conds if factor not in c)

    if rules.grid is not None:
        names = list(rules.grid)
        wanted = [
            dict(zip(names, combo, strict=True))
            for combo in itertools.product(*(rules.grid[f] for f in names))
        ]
        reason = _set_mismatch("grid", wanted, conds, assay)
        if reason:
            out.append(reason)

    if rules.conditions is not None:
        reason = _set_mismatch("conditions", [dict(c) for c in rules.conditions], conds, assay)
        if reason:
            out.append(reason)

    if rules.fixed:
        parts = []
        for f, v in rules.fixed.items():
            off = [c for c in conds if f not in c or step(f, c[f]) != step(f, v)]
            if off:
                others = [x for x in used(f) if step(f, x) != step(f, v)]
                how = [f"use {_values_text(others)}"] if others else []
                if missing_factor(f):
                    how.append("leave it out")
                parts.append(
                    f"{f} should be {fmt(v)} in every condition, but {len(off)} of {len(conds)} "
                    f"conditions {' or '.join(how)}"
                )
        if parts:
            out.append("fixed: " + "; ".join(parts))

    if rules.allowed:
        parts = []
        for f, values in rules.allowed.items():
            ok = {step(f, v) for v in values}
            extra = [x for x in used(f) if step(f, x) not in ok]
            if extra:
                parts.append(
                    f"{f} may only be {_values_text(values)}, but the design also uses {_values_text(extra)}"
                )
        if parts:
            out.append("allowed: " + "; ".join(parts))

    if rules.required_values:
        parts = []
        for f, values in rules.required_values.items():
            have = {step(f, x) for x in used(f)}
            parts += [f"no condition has {f} {fmt(v)}" for v in values if step(f, v) not in have]
        if parts:
            out.append("required_values: " + "; ".join(parts))

    if rules.required_any:
        parts = []
        for f, values in rules.required_any.items():
            have = {step(f, x) for x in used(f)}
            if not any(step(f, v) in have for v in values):
                parts.append(f"no condition has {f} at any of {_values_text(values)}")
        if parts:
            out.append("required_any: " + "; ".join(parts))

    if rules.replicates is not None and design.replicates not in rules.replicates:
        wanted_text = (
            str(rules.replicates[0])
            if len(rules.replicates) == 1
            else "one of " + ", ".join(str(r) for r in rules.replicates)
        )
        out.append(f"replicates: the design has {design.replicates}; expected {wanted_text}")

    if rules.include_controls is not None and design.include_controls != rules.include_controls:
        out.append(
            f"include_controls: controls are {'on' if design.include_controls else 'off'}; "
            f"they should be {'on' if rules.include_controls else 'off'}"
        )

    n = len(conds)
    if rules.min_conditions is not None and n < rules.min_conditions:
        out.append(
            f"min_conditions: the design has {n} conditions; at least {rules.min_conditions} were expected"
        )
    if rules.max_conditions is not None and n > rules.max_conditions:
        out.append(
            f"max_conditions: the design has {n} conditions; at most {rules.max_conditions} were expected"
        )

    wells = n * design.replicates
    sized = f"{wells} sample wells ({n} conditions x {design.replicates} replicates)"
    if rules.min_sample_wells is not None and wells < rules.min_sample_wells:
        out.append(
            f"min_sample_wells: the design has {sized}; at least {rules.min_sample_wells} were expected"
        )
    if rules.max_sample_wells is not None and wells > rules.max_sample_wells:
        out.append(
            f"max_sample_wells: the design has {sized}; at most {rules.max_sample_wells} were expected"
        )
    return out
