"""A deterministic provider that replays recorded responses.

This is what makes the claim "real tests exist and run without a live key"
true. It is not a mock that returns whatever the test wants: it replays what a
real model actually said, recorded once against the same prompt. A test that
passes here is a test against real model output, replayed.

If a call has no recording, it fails loudly with the exact filename to create.
Silently inventing a plausible answer would make the suite prove nothing.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.llm.base import Completion, Provider, ProviderError, Usage, call_key
from app.settings import settings


class MissingFixture(ProviderError):
    pass


class FakeProvider(Provider):
    name = "fake"

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or settings.fixture_root
        self.calls: list[str] = []

    def path_for(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def complete(self, *, purpose: str, system: str, user: str,
                 max_tokens: int = 2048) -> Completion:
        key = call_key(purpose, system, user)
        self.calls.append(key)
        path = self.path_for(key)
        if not path.exists():
            raise MissingFixture(
                f"no recorded response for {purpose!r}.\n"
                f"  expected: {path}\n"
                f"  record it with: RECORD_FIXTURES=1 LLM_PROVIDER=anthropic <your command>\n"
                f"  prompt begins: {user[:200]!r}"
            )
        data = json.loads(path.read_text())
        u = data.get("usage", {})
        return Completion(
            text=data["text"],
            usage=Usage(
                tokens_in=u.get("tokens_in", 0),
                tokens_out=u.get("tokens_out", 0),
                cost_usd=u.get("cost_usd", 0.0),
                model=u.get("model", "recorded"),
            ),
        )


def record(purpose: str, system: str, user: str, completion: Completion,
           root: Path | None = None) -> Path:
    """Persist a live response so the fake can replay it offline."""
    root = root or settings.fixture_root
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{call_key(purpose, system, user)}.json"
    path.write_text(json.dumps(
        {
            "purpose": purpose,
            "prompt_preview": user[:500],
            "text": completion.text,
            "usage": {
                "tokens_in": completion.usage.tokens_in,
                "tokens_out": completion.usage.tokens_out,
                "cost_usd": completion.usage.cost_usd,
                "model": completion.usage.model,
            },
        },
        indent=2,
    ))
    return path
