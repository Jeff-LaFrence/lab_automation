"""The model decider: a model picks the next round, using the optimizer as a tool.

The tool loop is plain JSON, so it does not depend on server side tool calling.
Each turn the model returns one object: a tool call or its final picks. At
most 6 tool calls per round. A final answer is checked by code. An invalid
answer gets 2 retries with the errors; after that the round falls back to the
optimizer's suggest(n), and the fallback is recorded.
"""

from __future__ import annotations

import json
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from labauto.agents.llm import Transcript, chat
from labauto.agents.planner import load_prompt
from labauto.config import allowed_values, fmt
from labauto.decide.base import key, on_grid
from labauto.decide.optimizer import Optimizer, round_seed
from labauto.models import Assay, Decision, History, Pick, ToolCall

MAX_TOOL_CALLS = 6
MAX_RETRIES = 2
MAX_REASON_WORDS = 20
MAX_REPEATS = 2
SOURCES = ("optimizer", "own", "confirm")


class ToolError(ValueError):
    """A tool call the decider made with arguments the tool cannot use."""


def _whole(name: str, value: Any, low: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or int(value) != value
        or value < low
    ):
        raise ToolError(
            f"'{name}' must be a whole number of at least {low}, not {json.dumps(value)}"
        )
    return int(value)


class PickIn(BaseModel):
    conditions: dict[str, float]
    source: str
    reason: str = ""


class DeciderTurn(BaseModel):
    """One reply from the model decider: a tool call or the final picks."""

    action: Literal["tool", "final"]
    tool: Literal["suggest", "predict", "results"] | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    picks: list[PickIn] | None = None
    round_note: str = ""


def factor_table(assay: Assay) -> str:
    lines = []
    for name, spec in assay.factors.items():
        values = allowed_values(spec)
        step = values[1] - values[0] if len(values) > 1 else 0
        lines.append(
            f"- {name} ({spec.unit}): {fmt(values[0])} to {fmt(values[-1])} in steps of {fmt(step)}"
        )
    return "\n".join(lines)


def digest(history: History) -> str:
    """Top 5 conditions so far, last round's picks with outcomes, and quality flags."""
    measured = sorted((s for s in history.stats if s.mean is not None), key=lambda s: -s.mean)[:5]
    top = [
        {
            "conditions": s.conditions,
            "mean": round(s.mean, 1),
            "sd": None if s.sd is None else round(s.sd, 1),
            "n": s.n,
            "flags": s.flags,
        }
        for s in measured
    ]
    return (
        "Top 5 conditions so far:\n"
        + json.dumps(top)
        + "\nLast round's picks and what they gave:\n"
        + json.dumps(history.last_picks)
        + "\nQuality flags:\n"
        + (json.dumps(history.qc_events) if history.qc_events else "none")
    )


def validate_final(turn: DeciderTurn, n: int, history: History, assay: Assay) -> list[str]:
    """Code checks every final answer. Returns the problems found, empty when valid."""
    names = list(assay.factors)
    picks = turn.picks or []
    errors = []
    if len(picks) != n:
        errors.append(f"Give exactly {n} picks. You gave {len(picks)}.")
    tested = {key(s.conditions, names) for s in history.stats}
    seen: set[tuple] = set()
    repeats = 0
    for i, p in enumerate(picks):
        if not on_grid(p.conditions, assay):
            errors.append(f"Pick {i + 1} is not on the allowed grid: {json.dumps(p.conditions)}.")
            continue
        k = key(p.conditions, names)
        if k in seen:
            errors.append(f"Pick {i + 1} repeats another pick in this round.")
        seen.add(k)
        if k in tested:
            repeats += 1
        if not p.reason.strip():
            errors.append(
                f"Pick {i + 1} has no reason. Give one of {MAX_REASON_WORDS} words or fewer."
            )
        elif len(p.reason.split()) > MAX_REASON_WORDS:
            errors.append(
                f"Pick {i + 1} has a reason of {len(p.reason.split())} words. The limit is {MAX_REASON_WORDS}."
            )
        if p.source not in SOURCES:
            errors.append(f"Pick {i + 1} has source '{p.source}'. Use one of {', '.join(SOURCES)}.")
    if repeats > MAX_REPEATS:
        errors.append(
            f"{repeats} picks repeat conditions already tested. At most {MAX_REPEATS} may."
        )
    return errors


class ModelDecider:
    """Picks with a model that may call the optimizer's tools."""

    name = "model"

    def __init__(
        self, assay: Assay, client, label: str = "local-main", transcript: Transcript | None = None
    ):
        self.assay = assay
        self.client = client
        self.label = label
        self.transcript = transcript

    def _tool(self, opt: Optimizer, turn: DeciderTurn) -> Any:
        """Run one tool. Raises ToolError, with a message for the model, on bad arguments."""
        args = dict(turn.args or {})
        if turn.tool == "suggest":
            return opt.suggest(_whole("n", args.get("n", 8), low=1))
        if turn.tool == "predict":
            conds = args.get("conditions")
            if not isinstance(conds, list) or not conds:
                raise ToolError("predict needs 'conditions': a list of condition objects")
            for i, c in enumerate(conds[:10]):
                if (
                    not isinstance(c, dict)
                    or set(c) != set(self.assay.factors)
                    or not all(
                        isinstance(v, (int, float)) and not isinstance(v, bool) for v in c.values()
                    )
                ):
                    raise ToolError(
                        f"predict condition {i + 1} must give a number for each of: {', '.join(self.assay.factors)}"
                    )
            return opt.predict(conds)
        top_k = args.get("top_k")
        rnd = args.get("round")
        return opt.results(
            None if top_k is None else _whole("top_k", top_k, low=1),
            None if rnd is None else _whole("round", rnd, low=1),
        )

    def decide(self, history: History, n: int, brief: str, seed: int) -> Decision:
        start = time.perf_counter()
        round_number = history.next_round
        opt = Optimizer(self.assay, history, round_seed(seed, round_number))
        messages = [
            {"role": "system", "content": load_prompt("decider_system.md")},
            {
                "role": "user",
                "content": (
                    f"CAMPAIGN BRIEF\n{brief or '(no brief given)'}\n\n"
                    f"ROUND {round_number}. Pick N = {n} conditions.\n\n"
                    f"FACTORS (only these values are allowed)\n{factor_table(self.assay)}\n\n"
                    f"DIGEST\n{digest(history)}"
                ),
            },
        ]
        tool_calls: list[ToolCall] = []
        invalid = 0
        tokens = 0
        model = None
        turn_no = 0
        reason = ""
        while invalid <= MAX_RETRIES:
            turn_no += 1
            result = chat(
                self.client,
                self.label,
                messages,
                DeciderTurn,
                temperature=0.3,
                transcript=self.transcript,
                role="decider",
                meta={"scope": f"decider-r{round_number:02d}", "attempt": turn_no},
            )
            tokens += result.prompt_tokens + result.completion_tokens
            model = result.model or model
            if result.parsed is None:
                invalid += 1
                reason = result.error or "unreadable reply"
                messages += [
                    {"role": "assistant", "content": result.text},
                    {
                        "role": "user",
                        "content": f"That reply could not be used: {reason}. Reply with one JSON object.",
                    },
                ]
                continue
            turn = DeciderTurn.model_validate(result.parsed)
            messages.append({"role": "assistant", "content": result.text})
            if turn.action == "tool":
                if len(tool_calls) >= MAX_TOOL_CALLS:
                    invalid += 1
                    reason = f"asked for a tool after the limit of {MAX_TOOL_CALLS} tool calls"
                    messages.append(
                        {
                            "role": "user",
                            "content": f"You have used all {MAX_TOOL_CALLS} tool calls. Reply now with your final picks.",
                        }
                    )
                    continue
                if turn.tool is None:
                    invalid += 1
                    reason = "a tool call without a tool name"
                    messages.append(
                        {"role": "user", "content": "Name the tool: suggest, predict, or results."}
                    )
                    continue
                try:
                    output = self._tool(opt, turn)
                except ToolError as exc:
                    invalid += 1
                    reason = f"a {turn.tool} call with bad arguments: {exc}"
                    tool_calls.append(
                        ToolCall(tool=turn.tool, args=turn.args, result={"error": str(exc)})
                    )
                    messages.append(
                        {"role": "user", "content": f"That {turn.tool} call failed: {exc}."}
                    )
                    continue
                tool_calls.append(ToolCall(tool=turn.tool, args=turn.args, result=output))
                left = MAX_TOOL_CALLS - len(tool_calls)
                messages.append(
                    {
                        "role": "user",
                        "content": f"Result of {turn.tool}:\n{json.dumps(output)}\nTool calls left this round: {left}.",
                    }
                )
                continue
            errors = validate_final(turn, n, history, self.assay)
            if not errors:
                return Decision(
                    round=round_number,
                    strategy=self.name,
                    model=model,
                    picks=[
                        Pick(conditions=p.conditions, source=p.source, reason=p.reason)
                        for p in turn.picks or []
                    ],
                    tool_calls=tool_calls,
                    round_note=turn.round_note,
                    seconds=round(time.perf_counter() - start, 3),
                    tokens=tokens,
                )
            invalid += 1
            reason = "invalid final answer: " + " ".join(errors)
            messages.append(
                {
                    "role": "user",
                    "content": "Your final answer was not valid:\n- "
                    + "\n- ".join(errors)
                    + "\nReply with corrected final picks.",
                }
            )
        suggestions = opt.suggest(n)
        return Decision(
            round=round_number,
            strategy=self.name,
            model=model,
            picks=[
                Pick(
                    conditions=s["conditions"],
                    source="optimizer",
                    reason="fallback to the optimizer's suggestion",
                )
                for s in suggestions
            ],
            tool_calls=tool_calls,
            fallback=f"after {MAX_RETRIES} retries: {reason}"[:500],
            round_note="",
            seconds=round(time.perf_counter() - start, 3),
            tokens=tokens,
        )
