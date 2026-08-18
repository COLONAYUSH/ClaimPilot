"""Domain model.

The central object is the FactLedger: every statement the system makes is a
Fact with provenance. Three kinds are kept strictly apart (a scoring criterion
of the exercise, but also just good practice):

  EXTRACTED  - read from a source; must carry at least one verified citation.
  ASSERTED   - a party's claim (e.g. the shipper's markdown figure). Real as a
               statement, unestablished as a fact; never silently promoted.
  DERIVED    - computed by this system from other facts; carries the formula
               and the input fact ids, so any number is auditable end to end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .util import dec2


# --- constant vocabularies (plain strings keep JSON round-trips trivial) ---

class Kind:
    EXTRACTED = "EXTRACTED"
    ASSERTED = "ASSERTED"
    DERIVED = "DERIVED"


class Method:
    DETERMINISTIC = "DETERMINISTIC"
    LLM = "LLM"
    LLM_VISION = "LLM_VISION"
    COMPUTED = "COMPUTED"


class Trust:
    PRIMARY_RECORD = "PRIMARY_RECORD"      # signed/issued documents (POD, BOL, invoice, MSA, inspection)
    OPERATIONAL = "OPERATIONAL"            # system records (TMS, ERP, claim system)
    CORRESPONDENCE = "CORRESPONDENCE"      # email thread (statements by parties)
    CONVENIENCE = "CONVENIENCE"            # summaries; must reconcile to primaries
    EVIDENCE_MEDIA = "EVIDENCE_MEDIA"      # photos


class Severity:
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class Classification:
    STRONG = "STRONG"                          # contractually owed, evidence complete
    MODERATE = "MODERATE"                      # contractually owed, evidence contested/partial
    NEEDS_INFO = "NEEDS_INFO"                  # recoverability turns on a missing fact
    EXCLUDED_CONTRACTUAL = "EXCLUDED_CONTRACTUAL"
    GOODWILL_LEVER = "GOODWILL_LEVER"          # not owed, but precedent shows carriers pay


@dataclass
class Citation:
    source_id: str
    locator: str = ""            # e.g. "page:1", "message:4", "row:3", "$.events[6].pieces"
    quote: str = ""              # verbatim snippet from the source
    verified: Optional[bool] = None
    match_ratio: float = 0.0


@dataclass
class Fact:
    fact_id: str
    key: str                     # dotted, namespaced: "pod.received_cartons"
    value: Any
    kind: str = Kind.EXTRACTED
    method: str = Method.DETERMINISTIC
    citations: List[Citation] = field(default_factory=list)
    confidence: float = 1.0
    inputs: List[str] = field(default_factory=list)   # fact_ids (DERIVED only)
    formula: str = ""                                  # human-readable (DERIVED only)
    note: str = ""


class FactLedger:
    """Ordered fact store with stable ids and a by-key index."""

    def __init__(self) -> None:
        self.facts: List[Fact] = []
        self._by_key: Dict[str, Fact] = {}
        self._n = 0

    def add(self, key: str, value: Any, **kwargs: Any) -> Fact:
        self._n += 1
        fact = Fact(fact_id="F-{:03d}".format(self._n), key=key, value=value, **kwargs)
        self.facts.append(fact)
        self._by_key[key] = fact
        return fact

    def fact(self, key: str) -> Fact:
        if key not in self._by_key:
            raise KeyError("no fact with key {!r}".format(key))
        return self._by_key[key]

    def get(self, key: str, default: Any = None) -> Any:
        f = self._by_key.get(key)
        return f.value if f is not None else default

    def has(self, key: str) -> bool:
        return key in self._by_key

    def value(self, key: str) -> Any:
        return self.fact(key).value

    def dec(self, key: str) -> Decimal:
        return dec2(self.fact(key).value)

    def by_id(self, fact_id: str) -> Optional[Fact]:
        for f in self.facts:
            if f.fact_id == fact_id:
                return f
        return None

    def __len__(self) -> int:
        return len(self.facts)

    def __iter__(self):
        return iter(self.facts)


@dataclass
class Discrepancy:
    disc_id: str
    severity: str
    category: str            # COUNT_CONFLICT | EVIDENCE_COVERAGE | MISSING_DOCUMENT |
                             # CONTRACT_TENSION | DATA_QUALITY | VERIFIED_CONSISTENT
    title: str
    description: str
    fact_ids: List[str] = field(default_factory=list)
    authority_note: str = ""  # which source governs, and why (deterministic ruling)
    status: str = "OPEN"      # OPEN | EXPLAINED | VERIFIED_CONSISTENT


@dataclass
class EvidenceGap:
    gap_id: str
    item: str
    why_needed: str
    requested_by: str = ""    # who asked for it / which rule raised it
    impact: str = ""
    fact_ids: List[str] = field(default_factory=list)


@dataclass
class DemandLine:
    key: str
    label: str
    claimed: Decimal
    basis: str
    asserted_only: bool = False   # True when the amount is a party assertion (markdown)
    fact_ids: List[str] = field(default_factory=list)


@dataclass
class ContractTerm:
    term_id: str
    topic: str
    section: str                 # e.g. "2. Cargo Loss and Damage Liability"
    quote: str
    params: Dict[str, Any] = field(default_factory=dict)
    citations: List[Citation] = field(default_factory=list)
    retrieval: Dict[str, Any] = field(default_factory=dict)  # backend, plan_id, sufficiency, status


@dataclass
class Entitlement:
    key: str
    label: str
    claimed: Decimal
    entitled_low: Decimal
    entitled_high: Decimal
    classification: str
    rationale: str               # deterministic, template-built - not LLM prose
    term_ids: List[str] = field(default_factory=list)
    fact_ids: List[str] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)
    ent_id: str = ""


@dataclass
class Comparable:
    claim_id: str
    score: float
    carrier: str
    issue_type: str
    service_level: str
    claimed: Decimal
    settled: Decimal
    settlement_pct: Decimal
    days_to_settle: int
    evidence: str
    negotiation_summary: str
    notes: str
    locator: str = ""
    match_basis: str = "structural"


@dataclass
class CohortStat:
    name: str
    description: str
    n: int
    median_pct: Decimal
    min_pct: Decimal
    max_pct: Decimal
    member_ids: List[str] = field(default_factory=list)


@dataclass
class Segment:
    locator: str
    text: str
    title: str = ""


@dataclass
class SourceDoc:
    source_id: str
    filename: str
    path: str
    kind: str                    # EMAIL | JSON | CSV | XLSX | PDF_TEXT | PDF_SCAN | IMAGE
    trust_tier: str
    description: str
    status: str = "OK"           # OK | MISSING | UNREADABLE | OCR_DERIVED
    sha256: str = ""
    text: str = ""               # canonical citable text (native extraction)
    derived_text: str = ""       # vision transcript for PDF_SCAN / IMAGE sources
    segments: List[Segment] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def citable_text(self) -> str:
        return self.text if self.text else self.derived_text
