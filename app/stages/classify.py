"""Working out what each document is.

Two decisions here change the path the run takes, which is what separates a
graph from a script with stage labels:

  Low confidence escalates to a person instead of guessing. Everything
  downstream extracts against the chosen type's schema, so a confident wrong
  classification poisons every fact that follows it. A shrug is cheap; a
  confident mistake is not.

  A document that tries to instruct the reader is quarantined. It leaves the
  extraction path entirely and becomes a finding.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.config import DomainConfig
from app.llm.base import Completion, Provider, ProviderError, ProviderUnavailable, Usage
from app.stages.prompts import classify_prompt
from app.stages.screen import ScreenVerdict, screen_text

DEFAULT_MIN_CONFIDENCE = 0.70


@dataclass
class Classification:
    doc_type: str | None
    confidence: float
    reasoning: str
    # The branch conditions. Exactly one path is taken, and which one is
    # recorded on the stage_event so the run log shows why.
    escalate: bool = False
    quarantine: bool = False
    screen: ScreenVerdict | None = None
    model_flagged_instructions: bool = False
    usage: Usage = field(default_factory=Usage)
    note: str | None = None

    @property
    def path(self) -> str:
        if self.quarantine:
            return "quarantine"
        if self.escalate:
            return "escalate"
        return "classified"


def classify_document(
    provider: Provider,
    cfg: DomainConfig,
    filename: str,
    text: str,
    min_confidence: float | None = None,
) -> Classification:
    """Classify one document, screening it for injected instructions first.

    The screen runs before the model call, and a hit short-circuits it. There is
    no reason to spend a model call interpreting a document we already know is
    trying to manipulate the interpreter -- and doing so would mean the
    manipulation had at least one chance to work.
    """
    threshold = min_confidence if min_confidence is not None else DEFAULT_MIN_CONFIDENCE
    verdict = screen_text(text, cfg.reconciliation.get("extra_injection_patterns"))
    if verdict.suspicious:
        return Classification(
            doc_type=None,
            confidence=0.0,
            reasoning="not classified: document contains directives aimed at the reader",
            quarantine=True,
            screen=verdict,
            note=verdict.statement(filename),
        )

    system, user = classify_prompt(cfg.doc_types, filename, text)
    try:
        completion: Completion = provider.complete(
            purpose="classify", system=system, user=user, max_tokens=512
        )
        payload = completion.json()
    except ProviderUnavailable:
        # The deployment is broken, not the document. Escalating here would
        # report a healthy-looking run that understood nothing.
        raise
    except ProviderError as exc:
        # An unreadable answer is not a classification. Escalating is the honest
        # outcome; picking the most common type would be a guess wearing a
        # confidence score.
        return Classification(
            None, 0.0, f"model response unusable: {exc}", escalate=True, note=str(exc)[:300]
        )

    doc_type = payload.get("doc_type")
    if doc_type is not None and doc_type not in cfg.doc_types:
        return Classification(
            None,
            0.0,
            f"model proposed type {doc_type!r}, which is not in the configured taxonomy",
            escalate=True,
            usage=completion.usage,
            note=f"unknown type {doc_type!r}",
        )

    confidence = _as_float(payload.get("confidence"))
    reasoning = str(payload.get("reasoning", ""))[:1000]
    model_flag = bool(payload.get("contains_instructions_to_the_reader"))

    # The model gets a vote on injection even though the rail already passed.
    # Either layer firing is enough -- they fail in different ways, which is the
    # point of having both.
    if model_flag:
        return Classification(
            doc_type=doc_type,
            confidence=confidence,
            reasoning=reasoning,
            quarantine=True,
            model_flagged_instructions=True,
            usage=completion.usage,
            note=(
                f"{filename}: the deterministic screen found nothing, but the model "
                f"judged the document to contain instructions aimed at its reader"
            ),
        )

    if doc_type is None or confidence < threshold:
        return Classification(
            doc_type=doc_type,
            confidence=confidence,
            reasoning=reasoning,
            escalate=True,
            usage=completion.usage,
            note=(
                f"confidence {confidence:.2f} below threshold {threshold:.2f}"
                if doc_type
                else "model could not place the document in the taxonomy"
            ),
        )

    return Classification(doc_type, confidence, reasoning, usage=completion.usage)


def _as_float(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))
