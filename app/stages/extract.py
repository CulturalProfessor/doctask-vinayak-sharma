"""Pulling facts out of a document, each bound to the text it came from.

The rule that shapes this stage: **a fact whose quote cannot be found in the
document is discarded.** Not kept with a weaker citation, not kept pointing at
the whole page -- discarded, and recorded as a gap.

That is a deliberately expensive rule. It throws away values that are probably
correct, because a value that is probably correct and definitely uncitable is
exactly what an ungrounded register is made of. The system is allowed to know
less; it is not allowed to claim a source it cannot show.

Malformed model output retries once under a stricter instruction, then gives up
and records the whole document as a gap. Both branches are path changes, and
both are recorded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.domain.config import DomainConfig
from app.domain.normalize import NormalisedValue, normalise
from app.domain.spans import SpanMatch, find_span
from app.llm.base import Provider, ProviderError, ProviderUnavailable, Usage
from app.stages.prompts import extract_prompt

MAX_ATTEMPTS = 2
_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass
class ExtractedFact:
    field_name: str
    value_raw: str
    quote: str
    span: SpanMatch
    normalised: NormalisedValue | None
    value_type: str
    unit: str | None
    confidence: float


@dataclass
class Gap:
    """Something the system could not establish, and why.

    Gaps are output, not error handling. They populate the register's "what
    could not be established" section, which is the section that makes the rest
    of it trustworthy.
    """
    field_name: str
    reason: str
    detail: str | None = None


@dataclass
class ExtractionResult:
    facts: list[ExtractedFact] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    attempts: int = 0
    path: str = "extracted"

    @property
    def entity_key(self) -> str | None:
        return self._entity_key

    _entity_key: str | None = None


def slugify(value: str) -> str:
    return _SLUG.sub("-", (value or "").strip().casefold()).strip("-") or "unknown"


def extract_document(provider: Provider, cfg: DomainConfig, doc_type: str, filename: str,
                     page_text: str) -> ExtractionResult:
    spec = cfg.doc_types[doc_type]
    schema = cfg.extraction[spec.extraction]
    fields: dict[str, dict] = schema.get("fields", {})

    result = ExtractionResult()
    payload = None
    system, user = extract_prompt(schema, doc_type, filename, page_text)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        result.attempts = attempt
        try:
            completion = provider.complete(
                purpose="extract" if attempt == 1 else "extract_retry",
                system=system, user=user, max_tokens=3000,
            )
            result.usage = result.usage + completion.usage
            payload = completion.json()
            break
        except ProviderUnavailable:
            # Retrying a broken deployment just wastes the retry budget.
            raise
        except ProviderError as exc:
            if attempt >= MAX_ATTEMPTS:
                # Path change: give up on this document rather than accept
                # whatever partial text came back.
                result.path = "skipped_unparseable"
                result.gaps.append(Gap("*", "model output could not be parsed",
                                       str(exc)[:300]))
                return result
            result.path = "retried"
            user = user + (
                "\n\nYour previous response was not valid JSON. Respond with the JSON "
                "object alone: no prose before it, no code fence around it."
            )

    reported = (payload or {}).get("fields") or {}
    if not isinstance(reported, dict):
        result.path = "skipped_unparseable"
        result.gaps.append(Gap("*", "model returned 'fields' that was not an object"))
        return result

    for name, spec_field in fields.items():
        entry = reported.get(name)
        if not isinstance(entry, dict) or not str(entry.get("value", "")).strip():
            if spec_field.get("required"):
                result.gaps.append(Gap(name, "required field not stated in the document"))
            continue

        value_raw = str(entry["value"]).strip()
        quote = str(entry.get("quote", "")).strip()
        if not quote:
            result.gaps.append(Gap(name, "no quote supplied", f"value was {value_raw!r}"))
            continue

        span = find_span(page_text, quote)
        if span is None:
            # The load-bearing discard. The value may well be right; without a
            # locatable source it cannot enter the register.
            result.gaps.append(Gap(
                name, "quote not found in the document",
                f"value {value_raw!r} discarded; quote was {quote[:120]!r}",
            ))
            continue

        value_type = spec_field.get("type", "text")
        normalised = normalise(value_type, value_raw, cfg.normalization)
        if normalised is None:
            result.gaps.append(Gap(
                name, f"value could not be read as {value_type}",
                f"raw value was {value_raw!r}",
            ))
            continue

        result.facts.append(ExtractedFact(
            field_name=name, value_raw=value_raw, quote=quote, span=span,
            normalised=normalised, value_type=value_type,
            unit=spec_field.get("unit"),
            confidence=_as_float(entry.get("confidence"), default=1.0),
        ))

    result._entity_key = _entity_key_for(schema, result)
    return result


def _entity_key_for(schema: dict, result: ExtractionResult) -> str | None:
    """Build the key that puts facts from different documents into one group.

    Without it, an amendment's rate and the MSA's rate never meet and the
    disagreement the system exists to find is invisible.
    """
    template = schema.get("entity_key")
    if not template:
        return None
    counterparty = next(
        (f.value_raw for f in result.facts if f.field_name == "counterparty"), None
    )
    if counterparty is None:
        return None
    return template.replace("{counterparty_slug}", slugify(counterparty))


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
