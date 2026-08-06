"""Shared shapes that cross stage boundaries."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.stages.extract import ExtractedFact


@dataclass(frozen=True)
class SourcedFact:
    """An extracted fact plus which document it came from.

    Extraction produces facts about a document; reconciliation compares facts
    *across* documents. That comparison needs to know the source's type, because
    an amendment and an invoice disagreeing means something quite different from
    two invoices disagreeing.
    """
    document: str
    doc_type: str
    entity_key: str
    fact: ExtractedFact
    effective_date: date | None = None

    @property
    def field(self) -> str:
        return self.fact.field_name

    @property
    def canonical(self) -> str:
        """The comparison form. Reconciliation groups on this."""
        return self.fact.normalised.canonical if self.fact.normalised else self.fact.value_raw

    @property
    def display(self) -> str:
        """The form a person reads in the register."""
        return self.fact.normalised.display if self.fact.normalised else self.fact.value_raw

    @property
    def citation(self) -> str:
        span = self.fact.span
        return f"{self.document} [{span.char_start}-{span.char_end}]"
