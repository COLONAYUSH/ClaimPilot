"""Pipeline orchestrator: ingest -> extract -> ground -> scan -> reconcile ->
retrieve/entitle -> benchmark -> compose -> render. The adversarial-input scan
(security.py) runs over the ingested sources and vision transcripts before any
reasoning, and its findings ride into the brief's security panel. Deterministic
stages never depend on LLM output succeeding; a failed composition stage
degrades to a deterministic-only brief rather than aborting the run."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from . import PROMPTS_VERSION
from .benchmark import run_benchmark
from .config import RunConfig
from .entitlement import run_entitlement
from .extract import run_extraction
from .grounding import is_quarantined, verify_fact_citations
from .ingest import load_registry
from .llm import LLMClient
from .models import FactLedger
from .position import build_case_input, compose_position
from .reconcile import run_reconciliation
from .retrieval import build_chunks, make_retriever
from .security import SCAN_CLASSES, scan_registry
from .util import to_jsonable

log = logging.getLogger("claimpilot.pipeline")


def run_pipeline(cfg: RunConfig) -> Dict[str, Any]:
    started = time.time()
    cfg.resolve_paths()
    registry = load_registry(cfg)
    ledger = FactLedger()
    client = LLMClient(cfg.provider, cfg.model, cfg.cache_dir, cfg.max_repairs)

    extraction = run_extraction(registry, ledger, client, cfg)

    # Adversarial-input scan: covers native text, vision transcripts, and any
    # text layer found where none should exist. Findings surface in the brief;
    # the money conclusions are computed and cannot be moved by injected text.
    security_findings = scan_registry(registry)
    if security_findings:
        log.warning("SECURITY: %d adversarial-input indicator(s): %s",
                    len(security_findings),
                    ", ".join(sorted({f.kind for f in security_findings})))

    recon = run_reconciliation(ledger, registry)

    chunks = build_chunks(registry)   # includes vision transcripts by now
    retriever, retrieval_note = make_retriever(cfg, chunks)
    try:
        ent = run_entitlement(ledger, registry, recon.demand_lines, retriever, client, cfg)
        comparables, cohorts = run_benchmark(ledger, registry)

        qa = verify_fact_citations(ledger, registry, cfg.fuzzy_threshold)

        case_input = build_case_input(
            ledger, recon.discrepancies, recon.gaps, recon.demand_lines,
            ent.entitlements, ent.terms, comparables, cohorts,
            extra_notes=(extraction.notes + recon.notes + ent.notes))
        position = compose_position(
            ledger, case_input, ent.entitlements, recon.discrepancies, recon.gaps,
            ent.terms, comparables, cohorts, recon.demand_lines, client, cfg)

        explain_sample = ""
        if getattr(retriever, "name", "") == "datum" and ent.retrieval_log:
            plan_id = next((r["plan_id"] for r in ent.retrieval_log if r.get("plan_id")), "")
            if plan_id:
                try:
                    explain_sample = retriever.explain(plan_id)
                except Exception as exc:  # audit nicety, never fatal
                    explain_sample = "explain unavailable: {}".format(exc)
    finally:
        try:
            retriever.close()
        except Exception:
            pass

    case: Dict[str, Any] = {
        "claim": case_input["claim"],
        "run": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "provider": cfg.provider,
            "model": cfg.model,
            "retrieval_backend": getattr(retriever, "name", "?"),
            "retrieval_fallback_note": retrieval_note,
            "prompts_version": PROMPTS_VERSION,
            "llm_cost_usd": client.total_cost,
            "llm_calls": client.calls,
            "ablated_sources": cfg.ablate,
            "skipped_sources": extraction.skipped_sources,
            "elapsed_s": round(time.time() - started, 1),
        },
        "sources": [{
            "source_id": d.source_id, "filename": d.filename, "kind": d.kind,
            "trust_tier": d.trust_tier, "status": d.status, "sha256": d.sha256,
            "description": d.description,
        } for d in registry.values()],
        "facts": [to_jsonable(f) for f in ledger],
        "discrepancies": [to_jsonable(d) for d in recon.discrepancies],
        "gaps": [to_jsonable(g) for g in recon.gaps],
        "demand_lines": [to_jsonable(l) for l in recon.demand_lines],
        "entitlements": [to_jsonable(e) for e in ent.entitlements],
        "contract_terms": [to_jsonable(t) for t in ent.terms],
        "comparables": [to_jsonable(c) for c in comparables],
        "cohorts": [to_jsonable(c) for c in cohorts],
        "position_numbers": case_input["position_numbers"],
        "position": {
            "ok": position.ok, "data": position.data, "violations": position.violations,
            "attempts": position.attempts, "refs_used": sorted(set(position.refs_used)),
        },
        "retrieval": {
            "log": ent.retrieval_log,
            "no_clause_topics": ent.no_clause_topics,
            "explain_sample": explain_sample,
        },
        "security": {
            "findings": [to_jsonable(f) for f in security_findings],
            "sources_scanned": sum(1 for d in registry.values()
                                   if d.status not in ("MISSING", "UNREADABLE")),
            "classes": SCAN_CLASSES,
        },
        "qa": {
            "quotes_total": qa.total, "quotes_exact": qa.exact, "quotes_fuzzy": qa.fuzzy,
            "quotes_failed": qa.failed, "citation_validity_rate": qa.validity_rate,
            "quote_failures": qa.failures,
            "quarantined_facts": qa.quarantined_fact_ids,
            "vision_verify": extraction.vision_verify,
            "notes": extraction.notes + recon.notes + ent.notes,
        },
    }
    n_quarantined = len(qa.quarantined_fact_ids)
    log.info("pipeline done in %.1fs: %d facts (%d quarantined), citation validity %.1f%%, "
             "position %s", case["run"]["elapsed_s"], len(ledger), n_quarantined,
             qa.validity_rate * 100, "OK" if position.ok else "FAILED CLOSED")
    return case


def ask_question(cfg: RunConfig, question: str, k: int = 5) -> Dict[str, Any]:
    """Grounded Q&A over the claim folder: retrieve, answer from passages
    only, honest 'insufficient evidence' pass-through."""
    from . import prompts
    from .llm import LLMRequest

    cfg.resolve_paths()
    registry = load_registry(cfg)
    chunks = build_chunks(registry)
    retriever, note = make_retriever(cfg, chunks)
    try:
        res = retriever.search(question, k=k)
        out: Dict[str, Any] = {
            "question": question, "backend": res.backend, "status": res.status,
            "sufficiency": res.sufficiency, "plan_id": res.plan_id,
            "passages": [{"source_id": h.source_id, "locator": h.locator,
                          "title": h.title, "text": h.text[:400]} for h in res.hits],
            "fallback_note": note,
        }
        if not res.answered:
            out["answer"] = ("The indexed claim sources do not contain enough evidence to "
                             "answer this (retriever returned: {}). Consider rephrasing or "
                             "adding the missing document.".format(res.status))
            return out
        passages = "\n\n".join("[{}] ({}) {}\n{}".format(
            h.source_id, h.locator, h.title, h.text) for h in res.hits)
        client = LLMClient(cfg.provider, cfg.model, cfg.cache_dir, cfg.max_repairs)
        answer = client.call(LLMRequest(
            prompt="QUESTION: {}\n\nRETRIEVED PASSAGES:\n{}".format(question, passages),
            system=prompts.ASK_SYSTEM, label="ask")).text
        out["answer"] = answer
        out["cost_usd"] = client.total_cost
        return out
    finally:
        try:
            retriever.close()
        except Exception:
            pass
