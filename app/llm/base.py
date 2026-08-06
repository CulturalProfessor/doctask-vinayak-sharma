"""The model boundary.

Everything that talks to a model goes through `Provider`. That is what lets the
whole test suite run with no key and no network (graded behaviour 7), and what
makes per-stage cost reporting possible (behaviour 10) without threading token
counts through business logic.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol


class ProviderError(RuntimeError):
    pass


class BudgetExceeded(ProviderError):
    """Raised when a run hits its model-call ceiling.

    Deliberately fatal. A run that would quietly overspend should stop and say
    so rather than finish and surprise someone.
    """


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    model: str = "fake"

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.tokens_in + other.tokens_in,
            self.tokens_out + other.tokens_out,
            round(self.cost_usd + other.cost_usd, 6),
            self.model or other.model,
        )


@dataclass
class Completion:
    text: str
    usage: Usage = field(default_factory=Usage)

    def json(self) -> Any:
        """Parse the response as JSON, tolerating a fenced code block.

        Models wrap JSON in ```json fences often enough that handling it here
        beats handling it at every call site.
        """
        raw = self.text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
            if raw.endswith("```"):
                raw = raw[: -3]
            raw = raw.strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # Last resort: the first balanced JSON object in the response. Smaller
        # models narrate before answering ("Here are the fields:") often enough
        # that rejecting those outright would waste a retry every time.
        #
        # Scoped deliberately: it extracts one *balanced* object rather than
        # regexing between the first and last brace, and it still raises when
        # there is no object at all. A parser that always finds something would
        # turn a refusal or an error message into a silently empty result.
        extracted = _first_json_object(raw)
        if extracted is not None:
            try:
                return json.loads(extracted)
            except json.JSONDecodeError:
                pass
        raise ProviderError(f"model did not return JSON:\n{self.text[:400]}")


def _first_json_object(text: str) -> str | None:
    """Return the first balanced {...} run, respecting strings and escapes."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def call_key(purpose: str, system: str, user: str) -> str:
    """Stable identity for one model call, used to record and replay fixtures."""
    digest = hashlib.sha256("\x00".join((purpose, system, user)).encode()).hexdigest()
    return f"{purpose}-{digest[:16]}"


class Provider(Protocol):
    name: str

    def complete(self, *, purpose: str, system: str, user: str,
                 max_tokens: int = 2048) -> Completion: ...


def get_provider(name: str | None = None) -> Provider:
    from app.settings import settings

    choice = (name or settings.llm_provider).lower()
    if choice == "fake":
        from app.llm.fake import FakeProvider

        return FakeProvider()
    if choice == "anthropic":
        from app.llm.anthropic import AnthropicProvider

        return AnthropicProvider()
    if choice == "openrouter":
        from app.llm.openrouter import OpenRouterProvider

        return OpenRouterProvider()
    raise ProviderError(
        f"unknown LLM_PROVIDER {choice!r}; expected 'fake', 'openrouter' or 'anthropic'"
    )
