"""Run configuration and the declarative source manifest.

The manifest encodes what the data dictionary says about each source - in
particular its trust tier, which the reconciliation engine uses for
authority rulings (a signed POD outranks a carrier EDI event for the
receiving count). Nothing downstream hard-codes filenames.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .models import Trust


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    filename: str
    kind: str
    trust_tier: str
    description: str
    required: bool = True


MANIFEST: List[SourceSpec] = [
    SourceSpec("case_overview", "01_case_overview.pdf", "PDF_TEXT", Trust.CONVENIENCE,
               "Claimant-authored case summary; convenience only, must reconcile to primaries"),
    SourceSpec("email_thread", "02_claim_email_thread.eml", "EMAIL", Trust.CORRESPONDENCE,
               "Negotiation history between shipper and carrier"),
    SourceSpec("claim_snapshot", "03_claim_snapshot.json", "JSON", Trust.OPERATIONAL,
               "Internal claim-system record"),
    SourceSpec("tms_shipment", "04_tms_shipment.json", "JSON", Trust.OPERATIONAL,
               "TMS shipment record with EDI events (carrier-reported counts)"),
    SourceSpec("erp_order_invoice", "05_erp_order_invoice.csv", "CSV", Trust.OPERATIONAL,
               "ERP order/invoice line"),
    SourceSpec("commercial_invoice", "06_commercial_invoice.pdf", "PDF_TEXT", Trust.PRIMARY_RECORD,
               "Commercial invoice (proof of value)"),
    SourceSpec("bill_of_lading", "07_bill_of_lading.pdf", "PDF_TEXT", Trust.PRIMARY_RECORD,
               "Signed bill of lading (tender record)"),
    SourceSpec("proof_of_delivery", "08_proof_of_delivery.pdf", "PDF_TEXT", Trust.PRIMARY_RECORD,
               "Signed POD with consignee exceptions (receiving record of authority)"),
    SourceSpec("inspection_report", "09_damage_inspection_report_scanned.pdf", "PDF_SCAN",
               Trust.PRIMARY_RECORD,
               "Independent inspection report; image-only scan, vision-transcribed"),
    SourceSpec("carrier_agreement", "10_carrier_service_agreement.pdf", "PDF_TEXT",
               Trust.PRIMARY_RECORD, "Master transportation services agreement (governing terms)"),
    SourceSpec("historical_claims_xlsx", "11_historical_claims.xlsx", "XLSX", Trust.OPERATIONAL,
               "Historical claims (xlsx twin; cross-checked against CSV)", required=False),
    SourceSpec("historical_claims", "12_historical_claims.csv", "CSV", Trust.OPERATIONAL,
               "Historical claims and settlements"),
    SourceSpec("damage_photo_1", "13_damage_photo_1.png", "IMAGE", Trust.EVIDENCE_MEDIA,
               "Warehouse damage photo"),
    SourceSpec("damage_photo_2", "14_damage_photo_2.png", "IMAGE", Trust.EVIDENCE_MEDIA,
               "Warehouse damage photo"),
    SourceSpec("data_dictionary", "15_data_dictionary.md", "TEXT", Trust.OPERATIONAL,
               "Field semantics and source-authority notes for the pack"),
]


def _default_datum_python() -> Optional[str]:
    env = os.environ.get("DATUM_PYTHON")
    if env:
        return env
    candidates = [
        Path.home() / "Downloads" / "Reimagining-RAG" / "datum" / ".venv" / "bin" / "python",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


# Calibrated on the gold query set (evals/abstention_sweep.py): at 0.50 datum
# refuses 2/3 deliberately-unanswerable probes at the cost of one answerable
# abstention; datum's own default refuses none on this corpus. Override with
# CLAIMPILOT_ABSTAIN_FLOOR ("" disables, using datum's default).
CALIBRATED_ABSTAIN_FLOOR = 0.50


def _default_abstain_floor() -> Optional[float]:
    raw = os.environ.get("CLAIMPILOT_ABSTAIN_FLOOR")
    if raw is not None:
        return float(raw) if raw else None
    return CALIBRATED_ABSTAIN_FLOOR


@dataclass
class DatumConfig:
    python: Optional[str] = field(default_factory=_default_datum_python)
    dsn: str = field(default_factory=lambda: os.environ.get(
        "DATUM_PG_DSN", "postgresql://localhost/datum_claims_fcc"))
    namespace: str = "tenant:northstar"
    principal_id: str = "claimpilot"
    startup_timeout_s: int = 240      # first open loads embedder+reranker on CPU
    request_timeout_s: int = 120
    # Evidence-sufficiency floor for typed abstention; None = datum's default.
    # The calibrated value for this corpus comes from evals/abstention_sweep.py.
    abstain_floor: Optional[float] = field(default_factory=_default_abstain_floor)


def _default_provider() -> str:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if shutil.which("claude"):
        return "cli"
    return "replay"


@dataclass
class RunConfig:
    pack_dir: str
    out_dir: str = "out"
    cache_dir: str = ".cache/llm"
    provider: str = field(default_factory=_default_provider)   # anthropic | cli | replay
    model: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5"))
    retrieval_backend: str = "auto"    # auto | datum | fts5
    datum: DatumConfig = field(default_factory=DatumConfig)
    vision_verify: bool = True         # second-pass confirmation of vision-extracted fields
    max_repairs: int = 2               # schema/grounding repair loops per LLM call
    fuzzy_threshold: float = 0.90
    ablate: List[str] = field(default_factory=list)   # source_ids to hide (failure-handling evals)

    def resolve_paths(self) -> None:
        self.pack_dir = str(Path(self.pack_dir).resolve())
        out = Path(self.out_dir)
        cache = Path(self.cache_dir)
        base = Path(__file__).resolve().parent.parent
        self.out_dir = str(out if out.is_absolute() else base / out)
        self.cache_dir = str(cache if cache.is_absolute() else base / cache)
        Path(self.out_dir).mkdir(parents=True, exist_ok=True)
        Path(self.cache_dir).mkdir(parents=True, exist_ok=True)
