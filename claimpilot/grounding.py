"""Grounding: the two mechanical gates between the LLM and the user.

1. Quote verification - every LLM-extracted fact carries a verbatim quote;
   the quote must actually occur in its source (normalized, with a bounded
   fuzzy fallback for PDF-extraction artifacts). A fact whose quotes all fail
   is quarantined: it stays in the ledger for audit but is excluded from all
   downstream reasoning and reported in the QA section.

2. NumberGuard - the generated brief and draft reply may only contain
   numbers, dates and times that exist in the verified fact ledger (or are
   computed by the deterministic engine and therefore *in* the ledger).
   The same tokenizer builds the allow-list from fact values and scans the
   generated text, so the two sides cannot drift apart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .models import Fact, FactLedger, SourceDoc
from .util import D, find_quote

QUARANTINE_NOTE = "QUARANTINED: quote not found in source"

# ------------------------------------------------------- quote verification


@dataclass
class QuoteQA:
    total: int = 0
    exact: int = 0
    fuzzy: int = 0
    failed: int = 0
    quarantined_fact_ids: List[str] = field(default_factory=list)
    failures: List[Dict[str, str]] = field(default_factory=list)

    @property
    def validity_rate(self) -> float:
        return round((self.exact + self.fuzzy) / self.total, 4) if self.total else 1.0


def verify_fact_citations(facts: Iterable[Fact], registry: Dict[str, SourceDoc],
                          fuzzy_threshold: float = 0.90) -> QuoteQA:
    qa = QuoteQA()
    for fact in facts:
        cited = [c for c in fact.citations if c.quote]
        if not cited:
            continue
        any_ok = False
        for cit in cited:
            doc = registry.get(cit.source_id)
            text = doc.citable_text if doc else ""
            ok, ratio = find_quote(cit.quote, text, fuzzy_threshold) if text else (False, 0.0)
            cit.verified, cit.match_ratio = ok, ratio
            qa.total += 1
            if ok and ratio >= 0.999:
                qa.exact += 1
            elif ok:
                qa.fuzzy += 1
            else:
                qa.failed += 1
                qa.failures.append({"fact_id": fact.fact_id, "key": fact.key,
                                    "source_id": cit.source_id, "quote": cit.quote[:160]})
            any_ok = any_ok or ok
        if not any_ok:
            fact.confidence = 0.0
            fact.note = (fact.note + " " if fact.note else "") + QUARANTINE_NOTE
            qa.quarantined_fact_ids.append(fact.fact_id)
    return qa


def is_quarantined(fact: Fact) -> bool:
    return QUARANTINE_NOTE in fact.note


# --------------------------------------------------------------- NumberGuard

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}

# Identifier-ish tokens (claim ids, PRO/BOL/SKU numbers, fact refs) are
# stripped before scanning so their digits are never mistaken for figures.
_ID_TOKEN = re.compile(r"\[[A-Z]{1,3}-\d+\]|\b[A-Z]{1,4}\d*(?:-[A-Z0-9]+)+\b")
_MONEY = re.compile(r"\$\s?\d[\d,]*(?:\.\d{1,2})?")
_PCT = re.compile(r"\b\d{1,3}(?:\.\d{1,2})?\s?%")
_ISO_TS = re.compile(r"(20\d{2}-\d{2}-\d{2})T(\d{1,2}:\d{2})")
_ISO_DATE = re.compile(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)")
_US_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b")
_PROSE_DATE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{1,2})(?:,\s*(20\d{2}))?\b", re.IGNORECASE)
_TIME = re.compile(r"\b\d{1,2}:\d{2}\b")
_NUM = re.compile(r"\b\d[\d,]*(?:\.\d+)?\b")


def _canon_num(raw: str) -> Optional[str]:
    cleaned = raw.replace("$", "").replace(",", "").replace("%", "").strip()
    try:
        d = D(cleaned)
    except (InvalidOperation, ValueError):
        return None
    if d == d.to_integral_value():
        return str(int(d))
    return str(d.normalize().to_eng_string())


def collect_tokens(text: str) -> Set[Tuple[str, str]]:
    """Tokenize numbers/dates/times/percents into (kind, canonical) pairs.
    Used identically for building the allow-list and scanning output."""
    tokens: Set[Tuple[str, str]] = set()
    work = _ID_TOKEN.sub(" ", text)
    work = _ISO_TS.sub(r"\1 \2", work)   # split ISO timestamps so both parts tokenize

    for m in _ISO_DATE.finditer(work):
        tokens.add(("date", "{}-{}-{}".format(m.group(1), m.group(2), m.group(3))))
    for m in _US_DATE.finditer(work):
        tokens.add(("date", "{}-{:02d}-{:02d}".format(
            m.group(3), int(m.group(1)), int(m.group(2)))))
    for m in _PROSE_DATE.finditer(work):
        month = _MONTHS[m.group(1).lower()]
        day = int(m.group(2))
        if m.group(3):
            tokens.add(("date", "{}-{:02d}-{:02d}".format(m.group(3), month, day)))
        tokens.add(("date", "{:02d}-{:02d}".format(month, day)))
    work = _ISO_DATE.sub(" ", work)
    work = _US_DATE.sub(" ", work)
    work = _PROSE_DATE.sub(" ", work)

    for m in _TIME.finditer(work):
        h, mm = m.group(0).split(":")
        tokens.add(("time", "{}:{}".format(int(h), mm)))
    work = _TIME.sub(" ", work)

    for m in _PCT.finditer(work):
        canon = _canon_num(m.group(0))
        if canon is not None:
            tokens.add(("pct", canon))
    work = _PCT.sub(" ", work)

    for m in _MONEY.finditer(work):
        canon = _canon_num(m.group(0))
        if canon is not None:
            tokens.add(("num", canon))
    work = _MONEY.sub(" ", work)

    for m in _NUM.finditer(work):
        canon = _canon_num(m.group(0))
        if canon is not None:
            tokens.add(("num", canon))
    return tokens


def _widen(tokens: Set[Tuple[str, str]]) -> Set[Tuple[str, str]]:
    """Add benign renderings of allowed values: date parts without year,
    percent roundings, integer form of x.00 money."""
    out = set(tokens)
    for kind, canon in tokens:
        if kind == "date" and len(canon) == 10:
            out.add(("date", canon[5:]))
        if kind == "pct":
            try:
                d = D(canon)
                out.add(("pct", _canon_num(str(round(float(d))))))
                out.add(("pct", _canon_num(str(round(float(d), 1)))))
            except (InvalidOperation, ValueError):
                pass
        if kind == "num" and "." in canon:
            try:
                d = D(canon)
                if d == d.to_integral_value():
                    out.add(("num", str(int(d))))
                else:
                    # a grounded decimal may be rendered as a percentage
                    # (81.28 -> "81.28%", "81.3%", "81%"); money stays strict
                    out.add(("pct", canon))
                    out.add(("pct", _canon_num(str(round(float(d), 1)))))
                    out.add(("pct", _canon_num(str(round(float(d))))))
            except (InvalidOperation, ValueError):
                pass
    return {t for t in out if t[1] is not None}


def build_allowed_tokens(ledger: FactLedger, extras: Iterable[Any] = ()) -> Set[Tuple[str, str]]:
    """The allow-list: every token that can be derived from a non-quarantined
    fact value (values are stringified and run through the same tokenizer),
    plus tokens from any extra values (entitlement bounds, comp stats...)."""
    corpus: List[str] = []
    for fact in ledger:
        if is_quarantined(fact):
            continue
        corpus.append(_stringify(fact.value))
    for value in extras:
        corpus.append(_stringify(value))
    tokens: Set[Tuple[str, str]] = set()
    for chunk in corpus:
        tokens |= collect_tokens(chunk)
    for fact in ledger:
        if not is_quarantined(fact) and isinstance(fact.value, (int, float, Decimal)):
            canon = _canon_num(str(fact.value))
            if canon is not None:
                tokens.add(("num", canon))
                tokens.add(("pct", canon))   # a ledger number quoted as a percent is fine
    return _widen(tokens)


def _stringify(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return " ".join(_stringify(v) for v in value)
    if isinstance(value, dict):
        return " ".join(_stringify(v) for v in value.values())
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


@dataclass
class GuardViolation:
    kind: str
    token: str
    context: str


def scan_generated_text(text: str, allowed: Set[Tuple[str, str]],
                        small_int_ceiling: int = 10) -> List[GuardViolation]:
    """Return every numeric/date/time token in `text` that is not derivable
    from the allow-list. Integers <= small_int_ceiling pass unconditionally
    (prose counts, section numbers, list ordinals)."""
    violations: List[GuardViolation] = []
    stripped = _ID_TOKEN.sub(" ", text)
    for kind, canon in sorted(collect_tokens(text)):
        if (kind, canon) in allowed:
            continue
        if kind == "num":
            try:
                if abs(D(canon)) <= small_int_ceiling and D(canon) == D(canon).to_integral_value():
                    continue
            except (InvalidOperation, ValueError):
                pass
        if kind == "date" and len(canon) == 5 and ("date", canon) in allowed:
            continue
        idx = stripped.find(canon.replace("-", "-"))
        context = ""
        if idx >= 0:
            context = stripped[max(0, idx - 40):idx + 40].replace("\n", " ")
        violations.append(GuardViolation(kind=kind, token=canon, context=context))
    return violations


_FACT_REF = re.compile(r"\[((?:F|D|G|E|CT)-\d+)\]")


def check_fact_refs(text: str, valid_ids: Set[str]) -> Tuple[List[str], List[str]]:
    """Returns (refs_used, invalid_refs)."""
    used = _FACT_REF.findall(text)
    invalid = sorted({u for u in used if u not in valid_ids})
    return used, invalid
