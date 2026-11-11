"""Chat clients behind one interface, defensive parsing, and the transcript.

chat() sends messages to a client, pulls one JSON object out of the reply,
validates it with a pydantic model, and retries once with the parse error if
that fails. Every call is appended to the run's transcript.jsonl. Endpoint
labels (local-main, local-small, hosted) are recorded, never a URL or a key.

Client types:
- LiveClient: an OpenAI-compatible server over HTTP.
- HostedClient: the hosted provider's official SDK.
- ScriptedClient: canned replies in order. For tests and scripted sessions.
- ReplayClient: replies served from a recorded transcript, in order.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from labauto.config import Endpoint

TIMEOUT_SECONDS = 600


class RawReply(BaseModel):
    """What a client returns for one call, before any parsing."""

    text: str
    reasoning: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""


class ChatResult(BaseModel):
    """The outcome of chat(): the parsed object, or an error saying why there is none."""

    text: str = ""
    parsed: dict | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    endpoint_label: str = ""
    model: str = ""
    calls: int = 0
    error: str | None = None


class EndpointUnavailable(RuntimeError):
    """The model endpoint did not answer."""


class ScriptExhausted(RuntimeError):
    """A scripted or replayed client was asked for more replies than it holds."""


class ChatClient(Protocol):
    """Anything that can answer one chat call."""

    def complete(
        self,
        label: str,
        messages: list[dict],
        schema: dict | None,
        temperature: float,
        max_tokens: int,
    ) -> RawReply: ...


# ---------------------------------------------------------------- parsing

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.S)


def extract_json(text: str) -> dict:
    """Pull the first complete JSON object out of a model reply.

    Strips a <think>...</think> block, then code fences, then takes the first
    '{' that starts a complete JSON object. Raises ValueError if there is none.
    """
    cleaned = _THINK.sub("", text)
    if "</think>" in cleaned:  # an opening tag the server already removed
        cleaned = cleaned.split("</think>", 1)[1]
    fenced = _FENCE.findall(cleaned)
    candidates = [*fenced, cleaned] if fenced else [cleaned]
    decoder = json.JSONDecoder()
    for chunk in candidates:
        for match in re.finditer(r"\{", chunk):
            try:
                obj, _ = decoder.raw_decode(chunk[match.start() :])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    raise ValueError("the reply holds no complete JSON object")


def parse_reply(text: str, schema: type[BaseModel] | None) -> tuple[dict | None, str | None]:
    """Extract and validate. Returns (parsed, None) or (None, error text)."""
    try:
        obj = extract_json(text)
    except ValueError as exc:
        return None, str(exc)
    if schema is None:
        return obj, None
    try:
        return schema.model_validate(obj).model_dump(mode="json"), None
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '(top level)'}: {e['msg']}"
            for e in exc.errors()
        )
        return None, f"the JSON does not match the reply format: {problems}"


# ---------------------------------------------------------------- transcript


class Transcript:
    """The run's transcript.jsonl: one line per model call.

    Lines are keyed by (attempt, call). On resume, a call whose reply is
    already recorded is served from here and the model is not asked again.
    """

    def __init__(self, path: Path | None):
        self.path = path
        self.records: list[dict] = []
        self._cache: dict[tuple, dict] = {}
        if path is not None and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self.records.append(rec)
                    self._cache[self._key(rec)] = rec

    @staticmethod
    def _key(rec: dict) -> tuple:
        return (rec.get("scope", ""), rec.get("attempt"), rec.get("call"))

    def lookup(self, meta: dict, call: int) -> dict | None:
        return self._cache.get((meta.get("scope", ""), meta.get("attempt"), call))

    def append(self, rec: dict) -> None:
        self.records.append(rec)
        self._cache[self._key(rec)] = rec
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------- chat


RETRY_TEXT = (
    "Your reply could not be used: {error}. Reply again with exactly one JSON object "
    "in the reply format and nothing else."
)


def chat(
    client: ChatClient,
    label: str,
    messages: list[dict],
    schema: type[BaseModel] | None = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    *,
    transcript: Transcript | None = None,
    role: str = "planner",
    meta: dict | None = None,
    max_calls: int = 2,
) -> ChatResult:
    """Ask for one JSON object. Retries once with the parse error appended.

    A second failure returns a ChatResult with parsed None and an error that
    starts with SCHEMA_INVALID. max_calls caps the calls (1 means no retry).
    """
    meta = dict(meta or {})
    json_schema = schema.model_json_schema() if schema is not None else None
    convo = list(messages)
    total = ChatResult(endpoint_label=label)
    for call in range(1, max(1, max_calls) + 1):
        recorded = transcript.lookup(meta, call) if transcript else None
        if recorded is not None:
            raw = RawReply(
                text=recorded["raw"],
                reasoning=recorded.get("reasoning", ""),
                prompt_tokens=recorded.get("prompt_tokens", 0),
                completion_tokens=recorded.get("completion_tokens", 0),
                model=recorded.get("model", ""),
            )
            seconds = recorded.get("seconds", 0.0)
        else:
            start = time.perf_counter()
            raw = client.complete(label, convo, json_schema, temperature, max_tokens)
            seconds = round(time.perf_counter() - start, 3)
        parsed, error = parse_reply(raw.text, schema)
        if recorded is None and transcript is not None:
            transcript.append(
                {
                    **meta,
                    "call": call,
                    "role": role,
                    "endpoint_label": label,
                    "model": raw.model,
                    "temperature": temperature,
                    "messages": convo,
                    "raw": raw.text,
                    "reasoning": raw.reasoning,
                    "parsed": parsed,
                    "parse_error": error,
                    "prompt_tokens": raw.prompt_tokens,
                    "completion_tokens": raw.completion_tokens,
                    "seconds": seconds,
                }
            )
        total.text = raw.text
        total.model = raw.model or total.model
        total.prompt_tokens += raw.prompt_tokens
        total.completion_tokens += raw.completion_tokens
        total.seconds = round(total.seconds + seconds, 3)
        total.calls = call
        if parsed is not None:
            total.parsed = parsed
            total.error = None
            return total
        total.error = f"SCHEMA_INVALID: {error}"
        convo = [
            *convo,
            {"role": "assistant", "content": raw.text},
            {"role": "user", "content": RETRY_TEXT.format(error=error)},
        ]
    return total


# ---------------------------------------------------------------- clients


def schema_instruction(schema: dict) -> str:
    """Text that carries the reply schema when the server cannot enforce it."""
    return (
        "Reply with one JSON object that matches this JSON schema, and nothing else:\n"
        + json.dumps(schema)
    )


def with_schema_in_system(messages: list[dict], schema: dict) -> list[dict]:
    """Add the schema text to the leading system message (or add one at the front).

    Some local chat templates refuse a system message after a user message.
    """
    out = [dict(m) for m in messages]
    if out and out[0]["role"] == "system":
        out[0]["content"] = out[0]["content"] + "\n\n" + schema_instruction(schema)
    else:
        out.insert(0, {"role": "system", "content": schema_instruction(schema)})
    return out


class LiveClient:
    """An OpenAI-compatible chat endpoint, called with httpx.

    On first use it probes whether the server accepts a JSON schema in
    response_format, and caches the answer for that endpoint. If not, the
    schema goes into the prompt instead.
    """

    _structured: dict[str, bool] = {}

    def __init__(self, endpoints: dict[str, Endpoint], transport: Any = None):
        self.endpoints = endpoints
        self.transport = transport

    def _endpoint(self, label: str) -> Endpoint:
        ep = self.endpoints.get(label)
        if ep is None:
            raise EndpointUnavailable(f"no endpoint configured for {label}")
        return ep

    def _http(self):
        import httpx

        return httpx.Client(timeout=TIMEOUT_SECONDS, transport=self.transport)

    def _headers(self, ep: Endpoint) -> dict:
        headers = {"Content-Type": "application/json"}
        if ep.api_key:
            headers["Authorization"] = f"Bearer {ep.api_key}"
        return headers

    def _key(self, ep: Endpoint) -> str:
        return hashlib.sha256(f"{ep.base_url}|{ep.model}".encode()).hexdigest()[:16]

    def check_server(self, label: str) -> dict:
        """GET /models, then one tiny structured request. Reports what works, without hostnames."""
        ep = self._endpoint(label)
        report: dict = {"label": label, "models_endpoint": False, "structured_output": False}
        try:
            with self._http() as http:
                resp = http.get(f"{ep.base_url.rstrip('/')}/models", headers=self._headers(ep))
                report["models_endpoint"] = resp.status_code == 200
                if resp.status_code == 200:
                    data = resp.json().get("data", [])
                    report["models"] = [m.get("id", "") for m in data][:5]
        except Exception as exc:
            report["error"] = type(exc).__name__
            return report
        report["structured_output"] = self.supports_structured(label)
        return report

    def supports_structured(self, label: str) -> bool:
        ep = self._endpoint(label)
        key = self._key(ep)
        if key not in self._structured:
            schema = {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            }
            body = {
                "model": ep.model,
                "messages": [{"role": "user", "content": 'Reply with {"ok": true}.'}],
                "max_tokens": 32,
                "temperature": 0,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "probe", "schema": schema},
                },
            }
            try:
                with self._http() as http:
                    resp = http.post(
                        f"{ep.base_url.rstrip('/')}/chat/completions",
                        json=body,
                        headers=self._headers(ep),
                    )
                self._structured[key] = resp.status_code == 200
            except Exception:
                self._structured[key] = False
        return self._structured[key]

    def complete(self, label, messages, schema, temperature, max_tokens) -> RawReply:
        ep = self._endpoint(label)
        body: dict = {
            "model": ep.model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if schema is not None:
            if self.supports_structured(label):
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "reply", "schema": schema},
                }
            else:
                body["messages"] = with_schema_in_system(messages, schema)
        try:
            with self._http() as http:
                resp = http.post(
                    f"{ep.base_url.rstrip('/')}/chat/completions",
                    json=body,
                    headers=self._headers(ep),
                )
        except Exception as exc:
            raise EndpointUnavailable(f"{label} did not answer ({type(exc).__name__})") from exc
        if resp.status_code != 200:
            raise EndpointUnavailable(f"{label} answered with HTTP {resp.status_code}")
        data = resp.json()
        msg = (data.get("choices") or [{}])[0].get("message", {})
        usage = data.get("usage") or {}
        return RawReply(
            text=msg.get("content") or "",
            reasoning=msg.get("reasoning_content") or "",
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            model=data.get("model") or ep.model,
        )


class HostedClient:
    """The hosted rung. Uses the provider's official SDK; the model name comes from the environment.

    Provider "anthropic" uses the anthropic package (install the [hosted] extra).
    Provider "openai_compatible" uses LiveClient against the hosted endpoint.
    Current hosted models reject a temperature setting, so none is sent, and
    the schema travels in the prompt.
    """

    def __init__(self, endpoint: Endpoint):
        self.endpoint = endpoint

    def complete(self, label, messages, schema, temperature, max_tokens) -> RawReply:
        ep = self.endpoint
        if ep.provider == "openai_compatible":
            return LiveClient({label: ep}).complete(label, messages, schema, temperature, max_tokens)
        if ep.provider != "anthropic":
            raise EndpointUnavailable(f"unknown hosted provider '{ep.provider}'")
        try:
            import anthropic
        except ImportError as exc:
            raise EndpointUnavailable("the [hosted] extra is not installed") from exc
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        if schema is not None:
            system = (system + "\n\n" + schema_instruction(schema)).strip()
        turns = [m for m in messages if m["role"] != "system"]
        client = anthropic.Anthropic(api_key=ep.api_key or None, timeout=TIMEOUT_SECONDS)
        try:
            resp = client.messages.create(
                model=ep.model,
                max_tokens=max(max_tokens, 16000),
                system=system,
                messages=turns,
            )
        except anthropic.APIStatusError as exc:
            raise EndpointUnavailable(f"hosted answered with HTTP {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise EndpointUnavailable("hosted did not answer") from exc
        if resp.stop_reason == "refusal":
            text = "(the hosted model declined to answer)"
        else:
            text = "".join(b.text for b in resp.content if b.type == "text")
        return RawReply(
            text=text,
            prompt_tokens=resp.usage.input_tokens,
            completion_tokens=resp.usage.output_tokens,
            model=resp.model,
        )


class ScriptedClient:
    """Returns canned replies in order. Replies may be strings or JSON-able objects.

    Pass a list to serve every label from one queue, or a dict of lists keyed
    by endpoint label. The model name recorded is "scripted".
    """

    def __init__(self, replies: list | dict[str, list], model: str = "scripted"):
        self.model = model
        if isinstance(replies, dict):
            self.queues = {k: list(v) for k, v in replies.items()}
        else:
            self.queues = {"*": list(replies)}
        self.calls: list[tuple[str, list[dict]]] = []

    @classmethod
    def from_file(cls, path: str | Path) -> ScriptedClient:
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def skip(self, n: int, label: str = "*") -> None:
        """Drop the first n replies for a label: they were used before a resume."""
        queue = self.queues.get(label, self.queues.get("*", []))
        del queue[:n]

    def complete(self, label, messages, schema, temperature, max_tokens) -> RawReply:
        self.calls.append((label, list(messages)))
        queue = self.queues.get(label, self.queues.get("*"))
        if not queue:
            raise ScriptExhausted(f"no scripted reply left for {label}")
        reply = queue.pop(0)
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return RawReply(text=text, model=self.model)


class ReplayClient:
    """Serves replies from a recorded transcript.jsonl, in order."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.records = [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.position = 0

    def skip(self, n: int, label: str = "*") -> None:
        """Move past the first n replies: they were used before a resume."""
        self.position += n

    def complete(self, label, messages, schema, temperature, max_tokens) -> RawReply:
        if self.position >= len(self.records):
            raise ScriptExhausted(f"the recording {self.path.name} has no more replies")
        rec = self.records[self.position]
        self.position += 1
        return RawReply(
            text=rec["raw"],
            reasoning=rec.get("reasoning", ""),
            prompt_tokens=rec.get("prompt_tokens", 0),
            completion_tokens=rec.get("completion_tokens", 0),
            model=rec.get("model", ""),
        )
