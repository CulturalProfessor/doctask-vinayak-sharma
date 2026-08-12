"""OpenRouter provider, defaulting to a zero-cost model.

Why this exists: recording the fixtures that `FakeProvider` replays needs real
model output, but the project should not require anyone to hold a paid API key
to reproduce it. OpenRouter carries models priced at zero, and recording is a
one-time cost -- once the fixtures exist, the suite and the demo run offline
forever.

Deliberately dependency-free (urllib, not httpx or the openai SDK). The whole
model boundary is one small interface, and adding an HTTP client plus an SDK to
cross it would be more moving parts than the crossing is worth.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from app.llm.base import Completion, Provider, ProviderError, ProviderUnavailable, Usage
from app.llm.fake import record
from app.settings import settings

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

# Free-tier models are rate limited rather than billed, so a slow record run is
# expected and a 429 is not an error worth crashing over.
RETRYABLE = {408, 429, 500, 502, 503, 504}


class OpenRouterProvider(Provider):
    name = "openrouter"

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        self.api_key = api_key or settings.openrouter_key
        if not self.api_key:
            raise ProviderUnavailable(
                "LLM_PROVIDER=openrouter but OPEN_ROUTER_KEY is empty. "
                "Use LLM_PROVIDER=fake to run offline against recorded responses."
            )
        self.model = model or settings.openrouter_model

    def complete(
        self, *, purpose: str, system: str, user: str, max_tokens: int = 2048
    ) -> Completion:
        body = json.dumps(
            {
                "model": self.model,
                "max_tokens": max_tokens,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
        ).encode()
        request = urllib.request.Request(
            ENDPOINT,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                # OpenRouter asks callers to identify themselves; neither header
                # carries anything private.
                "X-Title": "doctask",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:400]
            hint = " (free-tier rate limit; wait and retry)" if exc.code in RETRYABLE else ""
            raise ProviderUnavailable(f"OpenRouter HTTP {exc.code}{hint}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ProviderUnavailable(f"OpenRouter unreachable: {exc.reason}") from exc

        if "error" in payload and not payload.get("choices"):
            raise ProviderError(f"OpenRouter error: {str(payload['error'])[:300]}")

        choices = payload.get("choices") or []
        if not choices:
            raise ProviderError(f"OpenRouter returned no choices: {str(payload)[:300]}")
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        if not text.strip():
            # Some reasoning models put everything in a separate field and leave
            # content empty. Say so plainly rather than returning "".
            raise ProviderError(
                f"model {self.model!r} returned empty content "
                f"(finish_reason={choices[0].get('finish_reason')!r})"
            )

        usage_data = payload.get("usage") or {}
        usage = Usage(
            tokens_in=usage_data.get("prompt_tokens", 0),
            tokens_out=usage_data.get("completion_tokens", 0),
            # Zero-cost models really are zero. Anything else is reported by
            # OpenRouter in the `cost` field when it is present.
            cost_usd=float(usage_data.get("cost", 0.0) or 0.0),
            model=payload.get("model", self.model),
        )
        completion = Completion(text=text, usage=usage)
        if settings.record_fixtures:
            record(purpose, system, user, completion)
        return completion
