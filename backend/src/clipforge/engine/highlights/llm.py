"""LLM backends behind one small interface (ADR-0005).

- `BridgeClient`  -> host-side `scripts/llm_bridge.py`, which runs Claude Code headless (`claude -p`)
                     signed in with the owner's plan. No API key, no per-token billing.
- `OllamaClient`  -> local model on the GPU (free fallback).
- `FakeClient`    -> tests.
No CLI/API structured-output guarantee here, so `call_structured` parses JSON, validates with Pydantic,
re-asks once with the validation error, then gives up.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from clipforge.errors import ClipForgeError


class LlmError(ClipForgeError):
    retryable = True  # network / rate limit / bridge busy


class LlmOutputInvalid(ClipForgeError):
    """The model kept returning output that doesn't match the schema."""


class LlmLimitReached(ClipForgeError):
    """Plan usage limit reached; retry later (not an error in the content)."""

    retryable = True


@dataclass
class LlmResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None  # notional (plan) cost reported by Claude Code; not billed
    latency_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class LlmClient(Protocol):
    backend: str

    def complete(self, system: str, user: str, *, max_tokens: int = 4000) -> LlmResponse: ...


# --- backends --------------------------------------------------------------------------------------------


class BridgeClient:
    """Calls the host bridge (Claude Code headless with the owner's plan login)."""

    backend = "claude_code"

    def __init__(self, url: str, token: str, model: str, timeout_s: float = 600) -> None:
        self.url, self.token, self.model, self.timeout_s = url.rstrip("/"), token, model, timeout_s

    def complete(self, system: str, user: str, *, max_tokens: int = 4000) -> LlmResponse:
        t0 = time.monotonic()
        try:
            r = httpx.post(
                f"{self.url}/v1/complete",
                headers={"X-Bridge-Token": self.token},
                json={"system": system, "user": user, "model": self.model},
                timeout=self.timeout_s,
            )
        except httpx.HTTPError as exc:
            raise LlmError(
                f"LLM bridge unreachable at {self.url}: {type(exc).__name__}. Is "
                "scripts/llm_bridge.py running on the host?"
            ) from exc
        if r.status_code == 429:
            raise LlmLimitReached(
                "Claude plan usage limit reached; will retry later.", details={"bridge": r.text[:300]}
            )
        if r.status_code >= 500:
            raise LlmError(f"LLM bridge error {r.status_code}", details={"bridge": r.text[:300]})
        if r.status_code != 200:
            raise ClipForgeError(
                f"LLM bridge rejected the request ({r.status_code})", details={"bridge": r.text[:300]}
            )
        d = r.json()
        u = d.get("usage") or {}
        return LlmResponse(
            text=str(d.get("text", "")),
            model=str(d.get("model") or self.model),
            input_tokens=int(u.get("input_tokens", 0)),
            output_tokens=int(u.get("output_tokens", 0)),
            cache_read_tokens=int(u.get("cache_read_input_tokens", 0)),
            cache_write_tokens=int(u.get("cache_creation_input_tokens", 0)),
            cost_usd=d.get("cost_usd"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            raw=d,
        )


class OllamaClient:
    backend = "ollama"

    def __init__(self, url: str, model: str, timeout_s: float = 900, num_ctx: int = 16384) -> None:
        self.url, self.model, self.timeout_s, self.num_ctx = url.rstrip("/"), model, timeout_s, num_ctx

    def complete(self, system: str, user: str, *, max_tokens: int = 4000) -> LlmResponse:
        t0 = time.monotonic()
        body = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "think": False,
            "keep_alive": "2m",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": 0.2, "num_ctx": self.num_ctx, "num_predict": max_tokens},
        }
        try:
            r = httpx.post(f"{self.url}/api/chat", json=body, timeout=self.timeout_s)
        except httpx.HTTPError as exc:
            raise LlmError(f"Ollama unreachable at {self.url}: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise LlmError(f"Ollama error {r.status_code}", details={"body": r.text[:300]})
        d = r.json()
        return LlmResponse(
            text=str((d.get("message") or {}).get("content", "")),
            model=self.model,
            input_tokens=int(d.get("prompt_eval_count", 0)),
            output_tokens=int(d.get("eval_count", 0)),
            latency_ms=int((time.monotonic() - t0) * 1000),
            raw={k: v for k, v in d.items() if k != "message"},
        )


class FakeClient:
    """Returns canned responses chosen by a function of (system, user)."""

    backend = "fake"

    def __init__(self, responder: Callable[[str, str], str]) -> None:
        self.responder = responder
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, *, max_tokens: int = 4000) -> LlmResponse:
        self.calls.append((system, user))
        text = self.responder(system, user)
        return LlmResponse(
            text=text, model="fake", input_tokens=len(system + user) // 4, output_tokens=len(text) // 4
        )


# --- structured calls -------------------------------------------------------------------------------------

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
T = TypeVar("T", bound=BaseModel)


def extract_json_object(text: str) -> Any:
    """Tolerates code fences and prose around the object (CLI output isn't schema-constrained)."""
    cleaned = _FENCE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(cleaned[start : end + 1])


def call_structured[T: BaseModel](
    client: LlmClient, system: str, user: str, model_cls: type[T], *, max_tokens: int = 4000, retries: int = 1
) -> tuple[T, list[LlmResponse]]:
    responses: list[LlmResponse] = []
    prompt = user
    last_error = ""
    for _ in range(retries + 1):
        resp = client.complete(system, prompt, max_tokens=max_tokens)
        responses.append(resp)
        try:
            return model_cls.model_validate(extract_json_object(resp.text)), responses
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = str(exc)[:1500]
            resp.raw["validation_error"] = last_error  # kept on the llm_calls trail for prompt debugging
            prompt = (
                f"{user}\n\n---\nYour previous answer was not valid for the required JSON format:\n"
                f"{last_error}\nReturn ONLY the corrected JSON object."
            )
    raise LlmOutputInvalid(
        "The model did not return valid JSON for the required schema.",
        details={"error": last_error, "responses": len(responses)},
    )


CHARS_PER_TOKEN = 3.2  # rendered transcript lines are dense (IDs, timestamps); measured on real calls
PER_CALL_OVERHEAD_TOKENS = 950  # Claude Code's own scaffolding per headless call (measured: ~930)


def estimate_tokens(texts: Sequence[str]) -> int:
    """Rough token count for dry-run estimates (checked against actual usage by the eval harness)."""
    return int(sum(len(t) for t in texts) / CHARS_PER_TOKEN)
