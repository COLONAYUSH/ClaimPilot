"""Stage 6 - historical comparables.

Pure arithmetic over the historical claims table: a transparent similarity
score (carrier, issue types, service level, evidence profile), the top-k
comparable claims, and cohort settlement statistics that the position stage
may cite. The data dictionary's caveat travels with the numbers: a historical
settlement percentage is an outcome, not an entitlement.
"""

from __future__ import annotations

import logging
import statistics
from decimal import Decimal
from typing import Dict, List, Optional, Set, Tuple

from .models import (Citation, CohortStat, Comparable, FactLedger, Kind, Method,
                     SourceDoc)
from .util import D, dec2

log = logging.getLogger("claimpilot.benchmark")

WEIGHTS = {"carrier": 0.35, "issues": 0.30, "service": 0.15, "evidence": 0.20}

_EVIDENCE_TOKENS = {"pod": "pod", "photo": "photos", "photos": "photos",
                    "inspection": "inspection", "invoice": "invoice", "tms": "tms"}


def _issue_set(raw: str) -> Set[str]:
    return {t.strip().upper() for t in raw.split("+") if t.strip()}


def _evidence_set(raw: str) -> Set[str]:
    out: Set[str] = set()
    for token in raw.replace("+", " ").split():
        key = token.strip().lower()
        for needle, norm in _EVIDENCE_TOKENS.items():
            if needle in key:
                out.add(norm)
    return out


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def score_row(row: Dict[str, str], carrier: str, issues: Set[str], service: str,
              evidence: Set[str]) -> float:
    s = 0.0
    if row.get("carrier", "").strip().lower() == carrier.lower():
        s += WEIGHTS["carrier"]
    s += WEIGHTS["issues"] * _jaccard(_issue_set(row.get("issue_type", "")), issues)
    if row.get("service_level", "").strip().lower() == service.lower():
        s += WEIGHTS["service"]
    s += WEIGHTS["evidence"] * _jaccard(_evidence_set(row.get("evidence", "")), evidence)
    return round(s, 4)


def _to_comparable(row: Dict[str, str], score: float, locator: str,
                   basis: str = "structural") -> Comparable:
    return Comparable(
        claim_id=row.get("claim_id", ""), score=score,
        carrier=row.get("carrier", ""), issue_type=row.get("issue_type", ""),
        service_level=row.get("service_level", ""),
        claimed=dec2(row.get("claimed_usd", 0)), settled=dec2(row.get("settled_usd", 0)),
        settlement_pct=dec2(D(row.get("settlement_pct", 0)) * 100),
        days_to_settle=int(float(row.get("days_to_settle", 0) or 0)),
        evidence=row.get("evidence", ""),
        negotiation_summary=row.get("negotiation_summary", ""),
        notes=row.get("notes", ""), locator=locator, match_basis=basis)


_STOPWORDS = {"the", "and", "with", "that", "this", "have", "from", "were", "will",
              "after", "very", "similar", "profile", "evidence", "claims", "claim"}


def _tokens(text: str) -> Set[str]:
    import re
    return {t for t in re.findall(r"[a-z]{4,}", str(text).lower())} - _STOPWORDS


def _context_terms(ledger: FactLedger) -> Set[str]:
    """The case's live dispute vocabulary, derived from the ledger: what the
    carrier disputes, which documents are missing/partial, what the parties
    keep asserting about. Deterministic and case-specific by construction."""
    terms: Set[str] = set()
    for reason in ledger.get("email.offer_dispute_reasons", []) or []:
        terms |= _tokens(reason)
    flags = ledger.get("snapshot.doc_flags", {}) or {}
    for key, status in flags.items():
        if status in ("MISSING", "PARTIAL"):
            terms |= _tokens(key.replace("_", " "))
    for fact in ledger:
        if fact.key.startswith("email.assert."):
            terms |= _tokens(fact.key.split("email.assert.")[1].replace("_", " "))
    terms |= _tokens(" ".join(ledger.get("snapshot.claim_types", []) or []))
    return terms


def _context_overlap(case_terms: Set[str], row: Dict[str, str]) -> Set[str]:
    """Prefix-tolerant intersection ('specification' matches 'spec')."""
    row_terms = _tokens("{} {}".format(row.get("negotiation_summary", ""),
                                       row.get("notes", "")))
    shared: Set[str] = set()
    for a in case_terms:
        for b in row_terms:
            if a.startswith(b[:4]) and (a.startswith(b) or b.startswith(a)):
                shared.add(min(a, b, key=len))
                break
    return shared


def _pct_stats(rows: List[Dict[str, str]]) -> Tuple[Decimal, Decimal, Decimal]:
    pcts = [float(r.get("settlement_pct", 0)) * 100 for r in rows]
    return (dec2(statistics.median(pcts)), dec2(min(pcts)), dec2(max(pcts)))


def run_benchmark(ledger: FactLedger, registry: Dict[str, SourceDoc],
                  top_k: int = 5) -> Tuple[List[Comparable], List[CohortStat]]:
    doc = registry.get("historical_claims")
    if doc is None or doc.status != "OK" or not doc.meta.get("rows"):
        log.warning("benchmark: historical claims unavailable")
        return [], []
    rows = doc.meta["rows"]
    carrier = str(ledger.get("snapshot.carrier", ""))
    issues = set(ledger.get("snapshot.claim_types", []) or [])
    service = "Standard LTL" if ledger.get("derived.guaranteed_service_purchased") is False \
        else str(ledger.get("tms.service_name", "Standard LTL"))
    evidence: Set[str] = {"pod", "invoice"}
    if ledger.has("derived.photo_cartons_covered"):
        evidence.add("photos")
    if ledger.has("inspection.total_examined"):
        evidence.add("inspection")

    scored = []
    for i, row in enumerate(rows, start=1):
        scored.append((score_row(row, carrier, issues, service, evidence), i, row))
    scored.sort(key=lambda t: (-t[0], t[1]))
    comparables = [_to_comparable(row, s, "row:{}".format(i)) for s, i, row in scored[:top_k]]

    # Second lens: dispute-pattern twins - past claims whose settlement notes
    # share this case's live dispute vocabulary (packaging spec, photo
    # coverage, markdown/promotion...), regardless of structural shape.
    case_terms = _context_terms(ledger)
    seen = {c.claim_id for c in comparables}
    ctx_scored = []
    for i, row in enumerate(rows, start=1):
        if row.get("carrier") != carrier:
            continue
        shared = _context_overlap(case_terms, row)
        if len(shared) >= 2:
            ctx_scored.append((len(shared), i, row, shared))
    ctx_scored.sort(key=lambda t: (-t[0], t[1]))
    for n_shared, i, row, shared in ctx_scored[:3]:
        basis = "dispute-pattern: {}".format(", ".join(sorted(shared)))
        if row.get("claim_id") in seen:
            for c in comparables:
                if c.claim_id == row.get("claim_id"):
                    c.match_basis = "structural + " + basis
            continue
        structural = score_row(row, carrier, issues, service, evidence)
        comparables.append(_to_comparable(row, structural, "row:{}".format(i), basis))

    def rows_where(pred) -> List[Tuple[int, Dict[str, str]]]:
        return [(i, r) for i, r in enumerate(rows, start=1) if pred(r)]

    cohort_specs = [
        ("damage_with_inspection",
         "{} DAMAGE claims on Standard LTL with inspection evidence".format(carrier),
         lambda r: (r.get("carrier") == carrier and
                    "DAMAGE" in _issue_set(r.get("issue_type", "")) and
                    r.get("service_level") == "Standard LTL" and
                    "inspection" in _evidence_set(r.get("evidence", "")))),
        ("delay_only",
         "{} DELAY-only claims".format(carrier),
         lambda r: (r.get("carrier") == carrier and
                    _issue_set(r.get("issue_type", "")) == {"DELAY"})),
        ("damage_plus_delay",
         "{} combined DAMAGE+DELAY claims".format(carrier),
         lambda r: (r.get("carrier") == carrier and
                    {"DAMAGE", "DELAY"} <= _issue_set(r.get("issue_type", "")))),
    ]
    cohorts: List[CohortStat] = []
    for key, description, pred in cohort_specs:
        members = rows_where(pred)
        if not members:
            continue
        med, lo, hi = _pct_stats([r for _, r in members])
        member_ids = [r.get("claim_id", "") for _, r in members]
        cohorts.append(CohortStat(name=key, description=description, n=len(members),
                                  median_pct=med, min_pct=lo, max_pct=hi,
                                  member_ids=member_ids))
        ledger.add("comps.{}_median_pct".format(key), med, kind=Kind.DERIVED,
                   method=Method.COMPUTED,
                   formula="median settlement pct of {} rows: {}".format(
                       len(members), ", ".join(member_ids)),
                   citations=[Citation(source_id="historical_claims",
                                       locator="row:{}".format(i), quote=r.get("claim_id", ""))
                              for i, r in members[:3]],
                   note=description)

    delay_pattern = [r for _, r in rows_where(
        lambda r: r.get("carrier") == carrier and
        "DELAY" in _issue_set(r.get("issue_type", "")))]
    denied = sum(1 for r in delay_pattern
                 if "denied" in r.get("negotiation_summary", "").lower()
                 or "excluded" in r.get("negotiation_summary", "").lower())
    if delay_pattern:
        ledger.add("comps.delay_component_pattern",
                   "{} of {} {} claims involving DELAY show the delay/commercial component "
                   "denied or excluded in the settlement summary".format(
                       denied, len(delay_pattern), carrier),
                   kind=Kind.DERIVED, method=Method.COMPUTED,
                   formula="string scan of negotiation_summary over DELAY rows",
                   citations=[Citation(source_id="historical_claims", locator="rows",
                                       quote="")])
    dd = registry.get("data_dictionary")
    caveat = ("Historical settlement_pct is settlement divided by claimed amount; "
              "it should not be treated as a contractual entitlement.")
    citations = []
    if dd is not None and dd.status == "OK" and "settlement divided by" in dd.text:
        citations = [Citation(source_id="data_dictionary", locator="document",
                              quote="Historical `settlement_pct` is settlement divided by "
                                    "claimed amount; it should not be treated as a "
                                    "contractual entitlement.")]
    ledger.add("comps.caveat", caveat, kind=Kind.EXTRACTED if citations else Kind.DERIVED,
               method=Method.DETERMINISTIC, citations=citations)
    log.info("benchmark: %d comparables, %d cohorts", len(comparables), len(cohorts))
    return comparables, cohorts
