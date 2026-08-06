"""Turning raw extracted text into values that can be compared.

Reconciliation decides that two documents disagree. It can only do that once
both values have been reduced to the same shape, so everything about
"USD 19,200" versus "$19,200.00", or "thirty (30) days" versus "30 days", has
to be settled here rather than guessed at comparison time.

Two refusals are deliberate:

  Currencies are never converted. Comparing USD to EUR without a rate and a
  date produces a confident wrong answer, so a mixed-currency group is left for
  a human to resolve.

  Ambiguous numeric dates are flagged, not guessed. 03/04/2026 is March in one
  country and April in another, and picking one silently makes a termination
  date wrong by a month.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

# Contracts write numbers twice: "thirty (30) days", "twelve (12) months".
# The parenthetical numeral is the authoritative one and the easiest to parse.
_PAREN_NUMBER = re.compile(r"\((\d[\d,]*(?:\.\d+)?)\)")
_BARE_NUMBER = re.compile(r"(?<![\d.])(\d[\d,]*(?:\.\d+)?)")
_CURRENCY_CODE = re.compile(r"\b(USD|EUR|GBP|INR|CAD|AUD|JPY)\b", re.I)

_WORD_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45,
    "fifty": 50, "sixty": 60, "ninety": 90, "one hundred": 100,
}


class NormalisationError(ValueError):
    pass


@dataclass(frozen=True)
class NormalisedValue:
    """A raw value reduced to something comparable.

    `canonical` is what reconciliation groups on. `ambiguous` marks a value we
    parsed but do not fully trust -- it still flows through, carrying its doubt,
    rather than being dropped or silently resolved.
    """
    raw: str
    canonical: str
    value_type: str
    number: Decimal | None = None
    unit: str | None = None
    currency: str | None = None
    as_date: date | None = None
    ambiguous: bool = False
    note: str | None = None
    # What a person should see. Text values casefold for comparison, and
    # rendering the casefolded form would put "acme fabrication services llc"
    # into a register a human reads. Machines group on `canonical`; humans read
    # `display`.
    _display: str | None = None

    @property
    def display(self) -> str:
        return self._display or self.canonical


def plain(value: Decimal) -> str:
    """Format a Decimal without scientific notation.

    `Decimal("160").normalize()` is `1.6E+2`, which would put exponents into
    register cells for ordinary round numbers and make two spellings of the same
    quantity compare unequal as strings.
    """
    normalised = value.normalize()
    if normalised.as_tuple().exponent > 0:
        normalised = normalised.quantize(Decimal(1))
    return format(normalised, "f")


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None


def parse_number(raw: str) -> Decimal | None:
    """Prefer the parenthetical numeral, then a bare one, then a spelled word."""
    if raw is None:
        return None
    text = str(raw).strip()
    match = _PAREN_NUMBER.search(text)
    if match:
        return _decimal(match.group(1))
    match = _BARE_NUMBER.search(text)
    if match:
        return _decimal(match.group(1))
    lowered = text.lower()
    for word, value in sorted(_WORD_NUMBERS.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return Decimal(value)
    return None


def parse_money(raw: str, cfg: dict[str, Any]) -> NormalisedValue | None:
    text = str(raw or "").strip()
    if not text:
        return None
    currency_cfg = cfg.get("currency", {})
    symbols: dict[str, str] = currency_cfg.get("symbols", {})

    currency = None
    code = _CURRENCY_CODE.search(text)
    if code:
        currency = code.group(1).upper()
    else:
        # Longest symbol first, so "US$" wins over "$".
        for symbol in sorted(symbols, key=len, reverse=True):
            if symbol in text:
                currency = symbols[symbol]
                break
    currency = currency or currency_cfg.get("default", "USD")

    amount = parse_number(text)
    if amount is None:
        return None
    quantised = amount.quantize(Decimal("0.01"))
    return NormalisedValue(
        raw=text, canonical=f"{currency} {quantised}", value_type="money",
        number=quantised, currency=currency,
    )


def parse_duration(raw: str, cfg: dict[str, Any]) -> NormalisedValue | None:
    text = str(raw or "").strip()
    if not text:
        return None
    duration_cfg = cfg.get("duration", {})
    units: dict[str, str] = duration_cfg.get("units", {})
    to_days: dict[str, float] = duration_cfg.get("to_days", {})

    amount = parse_number(text)
    if amount is None:
        return None

    lowered = text.lower()
    unit = None
    for phrase in sorted(units, key=len, reverse=True):
        if re.search(rf"\b{re.escape(phrase)}\b", lowered):
            unit = units[phrase]
            break
    if unit is None:
        return NormalisedValue(raw=text, canonical=plain(amount), value_type="number",
                               number=amount)

    days = Decimal(str(to_days.get(unit, 1))) * amount
    return NormalisedValue(
        raw=text, canonical=f"{plain(amount)} {unit}", value_type="duration",
        number=amount, unit=unit,
        note=f"{plain(days)} days equivalent" if unit != "days" else None,
    )


def parse_date(raw: str, cfg: dict[str, Any]) -> NormalisedValue | None:
    text = str(raw or "").strip()
    if not text:
        return None
    date_cfg = cfg.get("date", {})
    formats: list[str] = date_cfg.get("formats", ["%Y-%m-%d"])

    # An all-numeric date with a day part under 13 could be either convention.
    numeric = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", text)
    ambiguous = bool(numeric and int(numeric.group(1)) <= 12 and int(numeric.group(2)) <= 12)

    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        note = None
        if ambiguous and date_cfg.get("ambiguous_numeric_dates") == "flag":
            note = (f"{text!r} is ambiguous: day/month order cannot be determined "
                    f"from the document; read as {fmt}")
        return NormalisedValue(raw=text, canonical=parsed.isoformat(), value_type="date",
                               as_date=parsed, ambiguous=ambiguous, note=note)
    return None


def normalise(value_type: str, raw: str, cfg: dict[str, Any]) -> NormalisedValue | None:
    """Reduce a raw value according to its declared type.

    Returns None when the value cannot be parsed. The caller records that as a
    gap; it does not get to fall back to the raw string and pretend the value
    is comparable.
    """
    if raw is None or str(raw).strip() == "":
        return None
    handlers = {
        "money": parse_money,
        "date": parse_date,
        "duration": parse_duration,
        "number": lambda r, c: _number_value(r),
        "text": lambda r, c: _text_value(r),
    }
    handler = handlers.get(value_type)
    if handler is None:
        raise NormalisationError(f"no handler for value type {value_type!r}")
    return handler(raw, cfg)


def _number_value(raw: str) -> NormalisedValue | None:
    amount = parse_number(str(raw))
    if amount is None:
        return None
    return NormalisedValue(raw=str(raw), canonical=plain(amount),
                           value_type="number", number=amount)


def _text_value(raw: str) -> NormalisedValue:
    collapsed = " ".join(str(raw).split())
    return NormalisedValue(raw=str(raw), canonical=collapsed.casefold(),
                           value_type="text", _display=collapsed)


def values_agree(a: NormalisedValue, b: NormalisedValue, cfg: dict[str, Any]) -> bool:
    """Whether two normalised values should be treated as the same value.

    Tolerance exists because a rounded total on an invoice is not a
    disagreement. Cross-currency comparison returns False rather than
    converting -- the group becomes a conflict a human resolves.
    """
    if a.value_type != b.value_type:
        return False
    if a.value_type == "money":
        if a.currency != b.currency:
            return False
        return _within(a.number, b.number, cfg.get("tolerance", {}).get("money", {}))
    if a.value_type == "duration":
        if a.unit != b.unit:
            return a.canonical == b.canonical
        return _within(a.number, b.number,
                       {"absolute": cfg.get("tolerance", {})
                        .get("duration_days", {}).get("absolute", 0)})
    if a.value_type == "number":
        return _within(a.number, b.number, cfg.get("tolerance", {}).get("number", {}))
    return a.canonical == b.canonical


def _within(a: Decimal | None, b: Decimal | None, tolerance: dict[str, Any]) -> bool:
    if a is None or b is None:
        return False
    if a == b:
        return True
    absolute = tolerance.get("absolute")
    if absolute is not None and abs(a - b) <= Decimal(str(absolute)):
        return True
    relative = tolerance.get("relative")
    if relative is not None:
        scale = max(abs(a), abs(b))
        if scale and abs(a - b) / scale <= Decimal(str(relative)):
            return True
    return False
