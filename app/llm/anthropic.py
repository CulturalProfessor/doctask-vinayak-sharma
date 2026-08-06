"""The live provider. Only reachable when LLM_PROVIDER=anthropic is set
deliberately; the default everywhere else is the recorded fake."""
from __future__ import annotations

from app.llm.base import Completion, Provider, ProviderError, Usage
from app.llm.fake import record
from app.settings import settings

# USD per million tokens. Used for the per-stage cost report (behaviour 10).
PRICING = {
    "claude-opus-5": (15.0, 75.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}


def _cost(model: str, tokens_in: int, tokens_out: int) -> float:
    rate_in, rate_out = PRICING.get(model, (0.0, 0.0))
    return round((tokens_in * rate_in + tokens_out * rate_out) / 1_000_000, 6)


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self) -> None:
        if not settings.anthropic_api_key:
            raise ProviderError(
                "LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is empty. "
                "Use LLM_PROVIDER=fake to run offline against recorded responses."
            )
        try:
            from anthropic import Anthropic
        except ImportError as exc:  # optional dependency, on purpose
            raise ProviderError(
                "the anthropic package is not installed; `pip install anthropic`, "
                "or use LLM_PROVIDER=fake"
            ) from exc
        self._client = Anthropic(api_key=settings.anthropic_api_key)

    def complete(self, *, purpose: str, system: str, user: str,
                 max_tokens: int = 2048) -> Completion:
        response = self._client.messages.create(
            model=settings.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        usage = Usage(
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
            cost_usd=_cost(settings.model, response.usage.input_tokens,
                           response.usage.output_tokens),
            model=settings.model,
        )
        completion = Completion(text=text, usage=usage)
        if settings.record_fixtures:
            record(purpose, system, user, completion)
        return completion
