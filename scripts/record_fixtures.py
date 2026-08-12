"""Record real model responses so the suite can replay them offline forever.

Run once against a live provider. After that `LLM_PROVIDER=fake` reproduces the
same run with no key and no network, which is what graded behaviour 7 asks for.

    LLM_PROVIDER=openrouter RECORD_FIXTURES=1 python -m scripts.record_fixtures

The default model is priced at zero, so this costs nothing. Free tiers rate
limit rather than bill, so 429s are expected and retried with backoff rather
than treated as failures.
"""

from __future__ import annotations

import argparse
import sys
import time

from app.domain.config import load_domain
from app.ingest.formats import UnsupportedFormat, detect_format, extract_pages
from app.llm.base import ProviderError, get_provider
from app.settings import REPO_ROOT, settings
from app.stages.classify import classify_document
from app.stages.extract import extract_document

MAX_RETRIES = 6

# What "the free tier is full" looks like, which is not the same as "the request
# was wrong". A shared zero-cost model answers a 429 sometimes and a 502 saying
# ResourceExhausted other times, and the second is what actually happened here:
# two documents came back unclassified with `escalate` and a 502 in the note,
# and the recording completed looking successful with two fixtures missing.
# Retrying only on 429 meant capacity failures were silently written into the
# corpus as escalations.
_RETRYABLE = (
    "rate limit",
    "429",
    "resourceexhausted",
    "resource exhausted",
    "temporarily unavailable",
    "502",
    "503",
    "overloaded",
    "capacity",
)


def with_backoff(label: str, fn, *args, **kwargs):
    """Retry capacity failures, but never retry a real error into silence."""
    delay = 8.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = fn(*args, **kwargs)
        except ProviderError as exc:
            message = str(exc)
        else:
            # classify_document catches provider errors and turns them into an
            # escalation rather than raising, which is correct in a run and
            # wrong here: a fixture that records "the provider was busy" is a
            # fixture that makes the offline suite replay an outage.
            note = getattr(result, "note", None) or ""
            if not any(word in note.lower() for word in _RETRYABLE):
                return result
            message = note

        if not any(word in message.lower() for word in _RETRYABLE) or attempt == MAX_RETRIES:
            raise ProviderError(f"{label}: {message}")
        print(
            f"      provider busy on {label}, waiting {delay:.0f}s "
            f"(attempt {attempt}/{MAX_RETRIES})"
        )
        time.sleep(delay)
        delay *= 1.8
    raise ProviderError(f"gave up on {label}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pile", default="pile_acme")
    parser.add_argument("--domain", default="vendor_contracts")
    args = parser.parse_args()

    if settings.llm_provider == "fake":
        print("LLM_PROVIDER=fake -- nothing to record. Set LLM_PROVIDER=openrouter.")
        return 1
    if not settings.record_fixtures:
        print("RECORD_FIXTURES is not set; responses would not be written. Set it to 1.")
        return 1

    cfg = load_domain(args.domain)
    provider = get_provider()
    directory = REPO_ROOT / "corpora" / args.pile
    paths = sorted(p for p in directory.glob("*") if p.is_file())

    print(f"recording against {getattr(provider, 'model', provider.name)}")
    print(f"corpus: {directory} ({len(paths)} documents)\n")

    totals = {
        "classified": 0,
        "escalate": 0,
        "quarantine": 0,
        "unsupported": 0,
        "facts": 0,
        "gaps": 0,
    }
    cost = 0.0

    for path in paths:
        data = path.read_bytes()
        print(f"  {path.name}")

        try:
            fmt = detect_format(path, data)
        except UnsupportedFormat as exc:
            # A corpus is allowed to contain a document the system refuses. That
            # is a gap, which is output, and it needs no recording -- ingest
            # rejects the file before any stage asks a model about it. Crashing
            # here made a deliberately realistic corpus impossible to record.
            print(f"      unsupported -> no model call needed ({exc})")
            totals["unsupported"] += 1
            continue

        text = extract_pages(data, fmt)[0].text

        result = with_backoff(
            f"classify {path.name}", classify_document, provider, cfg, path.name, text
        )
        cost += result.usage.cost_usd
        totals[result.path if result.path in totals else "classified"] += 1
        print(
            f"      classify -> {result.path}: {result.doc_type} "
            f"(confidence {result.confidence:.2f})"
        )

        if result.path != "classified":
            if result.note:
                print(f"      note: {result.note[:150]}")
            continue

        extraction = with_backoff(
            f"extract {path.name}",
            extract_document,
            provider,
            cfg,
            result.doc_type,
            path.name,
            text,
        )
        cost += extraction.usage.cost_usd
        totals["facts"] += len(extraction.facts)
        totals["gaps"] += len(extraction.gaps)
        print(
            f"      extract  -> {len(extraction.facts)} facts, "
            f"{len(extraction.gaps)} gaps, entity={extraction.entity_key}"
        )
        for fact in extraction.facts:
            shown = fact.normalised.canonical if fact.normalised else fact.value_raw
            print(f"          {fact.field_name:24} {shown:22} [{fact.span.method}]")
        for gap in extraction.gaps:
            print(f"          GAP {gap.field_name:20} {gap.reason}")

    print(f"\nfixtures written to {settings.fixture_root}")
    print(f"totals: {totals}")
    print(f"cost this run: ${cost:.4f}")
    print("\nreplay offline with: LLM_PROVIDER=fake")
    return 0


if __name__ == "__main__":
    sys.exit(main())
