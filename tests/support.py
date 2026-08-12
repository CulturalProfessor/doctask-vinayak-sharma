"""A provider that returns exactly what a test tells it to.

Distinct from `FakeProvider`, which replays recordings of real model output for
end-to-end runs. This one exists to drive the *stage* through model responses
that are awkward to obtain on demand: malformed JSON, a quote that is not in the
document, a type outside the taxonomy, low confidence. Those are the branches
that matter, and they are branches in our code, not in the model's.
"""

from __future__ import annotations

import json
from typing import Any

from app.llm.base import Completion, Usage


class ScriptedProvider:
    name = "scripted"

    def __init__(self, *responses: Any) -> None:
        self.queue: list[Any] = list(responses)
        self.calls: list[tuple[str, str, str]] = []

    def complete(
        self, *, purpose: str, system: str, user: str, max_tokens: int = 2048
    ) -> Completion:
        self.calls.append((purpose, system, user))
        if not self.queue:
            raise AssertionError(
                f"stage made an unscripted model call (purpose={purpose!r}); "
                f"{len(self.calls)} call(s) so far"
            )
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        text = item if isinstance(item, str) else json.dumps(item)
        return Completion(
            text, Usage(tokens_in=100, tokens_out=40, cost_usd=0.0012, model="scripted")
        )

    @property
    def purposes(self) -> list[str]:
        return [call[0] for call in self.calls]


def classification(doc_type: str | None, confidence: float, *, instructions: bool = False) -> dict:
    return {
        "doc_type": doc_type,
        "confidence": confidence,
        "reasoning": "scripted",
        "contains_instructions_to_the_reader": instructions,
    }


def extraction(**fields: tuple[str, str]) -> dict:
    """extraction(hourly_rate=("USD 120", "USD 120 per hour")) -> payload."""
    return {
        "fields": {
            name: {"value": value, "quote": quote, "confidence": 0.95}
            for name, (value, quote) in fields.items()
        }
    }
