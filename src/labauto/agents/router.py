"""Pick an endpoint for each model call, and log why.

report_note goes to the small endpoint when one is configured. Every other
role goes to main. data_reviewer, the overwatch, always stays on main: it is
the generalist main model by design, so a large prompt never moves it to the
small endpoint. For the other roles, if the prompt looks too big for the
chosen endpoint (over 80% of its context), the endpoint with the larger
context is used. If none fits, the caller trims the attempt history to the
latest attempt.

The token estimate is characters divided by 4.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from labauto.config import Endpoint, Settings

ROLES = ("planner", "repair", "reviewer", "decider", "report_note", "data_reviewer")
# Roles that may run on the small endpoint. Others always start on main.
SMALL_ROLES = ("report_note",)
# Roles that never leave main, whatever the prompt size: the overwatch is the main model.
MAIN_ONLY_ROLES = ("data_reviewer",)
FILL_LIMIT = 0.8
DEFAULT_CONTEXT = 16000


class Route(BaseModel):
    """One routing decision. The endpoint itself is kept out of logs."""

    role: str
    label: str
    context_tokens: int
    est_prompt_tokens: int
    reason: str
    trim: bool = False


def estimate_tokens(messages: list[dict] | str) -> int:
    """Characters divided by 4."""
    if isinstance(messages, str):
        return len(messages) // 4
    return sum(len(m.get("content") or "") for m in messages) // 4


def _local(settings: Settings | None) -> list[tuple[str, int]]:
    """The local endpoints as (label, context size). Main is assumed when none is set."""
    out = []
    main = settings.main if settings else None
    out.append(("local-main", main.context_tokens if main else DEFAULT_CONTEXT))
    if settings and settings.small is not None:
        out.append(("local-small", settings.small.context_tokens))
    return out


def route(
    role: str,
    est_prompt_tokens: int,
    settings: Settings | None = None,
    log_path: Path | None = None,
    extra: dict | None = None,
) -> Route:
    """Choose the endpoint label for a role and a prompt size. Logs the decision."""
    if role not in ROLES:
        raise ValueError(f"unknown role '{role}'")
    endpoints = dict(_local(settings))
    if role in MAIN_ONLY_ROLES:
        endpoints = {"local-main": endpoints["local-main"]}
    if role in SMALL_ROLES and "local-small" in endpoints:
        label, reason = "local-small", f"{role} goes to the small endpoint"
    else:
        label = "local-main"
        reason = f"{role} goes to the main endpoint"
        if role in SMALL_ROLES:
            reason = f"{role} goes to main: no small endpoint configured"
        elif role in MAIN_ONLY_ROLES:
            reason = f"{role} goes to the main endpoint: the overwatch is the main model"
    trim = False
    if est_prompt_tokens > FILL_LIMIT * endpoints[label]:
        biggest = max(endpoints.items(), key=lambda kv: kv[1])
        if est_prompt_tokens <= FILL_LIMIT * biggest[1] and biggest[0] != label:
            reason += (
                f"; estimate {est_prompt_tokens} is above 80% of {label} context "
                f"{endpoints[label]}, moved to {biggest[0]} ({biggest[1]})"
            )
            label = biggest[0]
        else:
            label = biggest[0]
            trim = True
            reason += (
                f"; estimate {est_prompt_tokens} is above 80% of every context, "
                f"largest is {label} ({biggest[1]}); attempt history trimmed"
            )
    decision = Route(
        role=role,
        label=label,
        context_tokens=endpoints[label],
        est_prompt_tokens=est_prompt_tokens,
        reason=reason,
        trim=trim,
    )
    log_route(decision, log_path, extra)
    return decision


def hosted_route(
    est_prompt_tokens: int, settings: Settings, log_path: Path | None, extra: dict | None = None
) -> Route:
    """The hosted rung is chosen by the ladder, not the router. Logged the same way."""
    hosted = settings.hosted
    decision = Route(
        role="repair",
        label="hosted",
        context_tokens=hosted.context_tokens if hosted else 0,
        est_prompt_tokens=est_prompt_tokens,
        reason="escalation ladder: local rung finished, hosted rung gets one attempt",
    )
    log_route(decision, log_path, extra)
    return decision


def log_route(decision: Route, log_path: Path | None, extra: dict | None = None) -> None:
    """Append one line to model_calls.jsonl."""
    if log_path is None:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({**(extra or {}), **decision.model_dump()}) + "\n")


def endpoint_for(label: str, settings: Settings | None) -> Endpoint | None:
    """The configured endpoint behind a label, or None."""
    if settings is None:
        return None
    return {
        "local-main": settings.main,
        "local-small": settings.small,
        "hosted": settings.hosted,
    }.get(label)
