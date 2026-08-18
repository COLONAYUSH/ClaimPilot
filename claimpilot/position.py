"""Stage 7 - position composition.

The LLM writes the narrative brief and the draft reply from the assembled
case file - nothing else. Two mechanical gates stand between its output and
the user:

  * NumberGuard: every number/date/time in the output must be derivable from
    the verified fact ledger (which includes every deterministically computed
    figure, with its formula).
  * Reference check: every [F-x]/[D-x]/[G-x]/[E-x]/[CT-x] must resolve; the
    draft reply must read as a real email (no bracketed ids), and the
    analysis must actually cite its claims.

Violations are fed back for a bounded repair; if the output still fails, the
stage fails CLOSED: the brief renders the deterministic sections only and the
failure is reported, rather than shipping unguarded prose.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from . import prompts
from .config import RunConfig
from .grounding import (build_allowed_tokens, check_fact_refs, is_quarantined,
                        scan_generated_text)
from .llm import LLMClient, LLMError, LLMRequest
from .models import (CohortStat, Comparable, ContractTerm, DemandLine, Discrepancy,
                     Entitlement, EvidenceGap, FactLedger, Kind)
from .util import to_jsonable

log = logging.getLogger("claimpilot.position")

_POSITION_KEYS = [
    "position.core_low", "position.core_high", "position.goodwill_high",
    "position.recommended_counter", "position.offer_gap", "position.offer_equals_floor",
    "position.expected_band_low", "position.expected_band_high",
    "position.reserve_covers_counter",
    "entitlement.cargo_notice_deadline", "entitlement.cargo_notice_ok",
    "entitlement.delay_notice_deadline", "entitlement.delay_notice_ok",
]


@dataclass
class PositionResult:
    ok: bool
    data: Dict[str, Any] = field(default_factory=dict)
    violations: List[str] = field(default_factory=list)
    attempts: int = 0
    refs_used: List[str] = field(default_factory=list)


def build_case_input(ledger: FactLedger, discrepancies: List[Discrepancy],
                     gaps: List[EvidenceGap], demand_lines: List[DemandLine],
                     entitlements: List[Entitlement], terms: List[ContractTerm],
                     comparables: List[Comparable], cohorts: List[CohortStat],
                     extra_notes: List[str]) -> Dict[str, Any]:
    facts = []
    for f in ledger:
        if is_quarantined(f):
            continue
        entry: Dict[str, Any] = {"id": f.fact_id, "key": f.key, "value": to_jsonable(f.value)}
        if f.kind != Kind.EXTRACTED:
            entry["kind"] = f.kind
        if f.formula:
            entry["formula"] = f.formula
        if f.note:
            entry["note"] = f.note
        if f.confidence < 1.0:
            entry["confidence"] = f.confidence
        facts.append(entry)
    return {
        "claim": {
            "claim_id": ledger.get("snapshot.claim_id"),
            "claimant": ledger.get("overview.claimant", "Northstar Retail Equipment LLC"),
            "carrier": ledger.get("snapshot.carrier"),
            "status": ledger.get("snapshot.status"),
            "owner": ledger.get("snapshot.owner"),
            "demand_usd": to_jsonable(ledger.get("snapshot.claim_amount_usd")),
            "carrier_offer_usd": to_jsonable(ledger.get("snapshot.carrier_offer_usd")),
            "reserve_usd": to_jsonable(ledger.get("snapshot.reserve_usd")),
        },
        "verified_facts": facts,
        "discrepancies": [to_jsonable(d) for d in discrepancies],
        "evidence_gaps": [to_jsonable(g) for g in gaps],
        "demand_lines": [to_jsonable(l) for l in demand_lines],
        "entitlements": [to_jsonable(e) for e in entitlements],
        "contract_terms": [
            {"id": t.term_id, "topic": t.topic, "section": t.section, "quote": t.quote,
             "params": to_jsonable(t.params)} for t in terms],
        "comparables": [to_jsonable(c) for c in comparables],
        "cohorts": [to_jsonable(c) for c in cohorts],
        "position_numbers": {
            k: {"id": ledger.fact(k).fact_id, "value": to_jsonable(ledger.value(k)),
                "formula": ledger.fact(k).formula, "note": ledger.fact(k).note}
            for k in _POSITION_KEYS if ledger.has(k)},
        "notes": extra_notes,
        "thread_state": to_jsonable(ledger.get("email.thread_state", {})),
    }


def _texts_to_check(data: Dict[str, Any]) -> List[str]:
    texts = [data.get("executive_summary", "")]
    texts += list(data.get("negotiation_analysis", []) or [])
    for step in data.get("recommended_next_steps", []) or []:
        texts += [step.get("action", ""), step.get("rationale", "")]
    texts += list(data.get("risks_and_watchouts", []) or [])
    draft = data.get("draft_reply", {}) or {}
    texts += [draft.get("subject", ""), draft.get("body", "")]
    return [t for t in texts if t]


def validate_position(data: Dict[str, Any], allowed: Set, valid_ids: Set[str]) -> List[str]:
    violations: List[str] = []
    for text in _texts_to_check(data):
        for v in scan_generated_text(text, allowed):
            violations.append(
                "ungrounded {} {!r} near: ...{}...".format(v.kind, v.token, v.context))
    joined = " ".join(_texts_to_check(data))
    _, invalid = check_fact_refs(joined, valid_ids)
    for ref in invalid:
        violations.append("reference [{}] does not exist in the case file".format(ref))
    analysis = " ".join(data.get("negotiation_analysis", []) or [])
    refs_in_analysis, _ = check_fact_refs(analysis, valid_ids)
    if len(refs_in_analysis) < 6:
        violations.append(
            "negotiation_analysis cites only {} fact references; every factual claim "
            "must carry its [F-x]/[E-x]/[D-x]/[CT-x] reference".format(len(refs_in_analysis)))
    body = (data.get("draft_reply", {}) or {}).get("body", "")
    if re.search(r"\[(?:F|D|G|E|CT)-\d+\]", body):
        violations.append("draft_reply.body must not contain bracketed reference ids")
    return violations


def compose_position(ledger: FactLedger, case_input: Dict[str, Any],
                     entitlements: List[Entitlement], discrepancies: List[Discrepancy],
                     gaps: List[EvidenceGap], terms: List[ContractTerm],
                     comparables: List[Comparable], cohorts: List[CohortStat],
                     demand_lines: List[DemandLine], client: LLMClient,
                     cfg: RunConfig) -> PositionResult:
    allowed = build_allowed_tokens(ledger, extras=[
        to_jsonable(entitlements), to_jsonable(comparables), to_jsonable(cohorts),
        to_jsonable(demand_lines)])
    valid_ids: Set[str] = {f.fact_id for f in ledger if not is_quarantined(f)}
    valid_ids |= {d.disc_id for d in discrepancies}
    valid_ids |= {g.gap_id for g in gaps}
    valid_ids |= {e.ent_id for e in entitlements}
    valid_ids |= {t.term_id for t in terms}

    base_prompt = "{}\n\nCASE FILE (JSON):\n{}".format(
        prompts.POSITION_TASK, json.dumps(case_input, indent=1, ensure_ascii=False))
    prompt = base_prompt
    violations: List[str] = []
    data: Dict[str, Any] = {}
    attempts = 0
    for attempt in range(cfg.max_repairs + 1):
        attempts = attempt + 1
        try:
            res = client.call(LLMRequest(
                prompt=prompt, system=prompts.POSITION_SYSTEM,
                schema=prompts.POSITION_SCHEMA, max_tokens=16000,
                label="position/brief" if attempt == 0
                else "position/brief-guardfix{}".format(attempt)))
        except LLMError as exc:
            return PositionResult(ok=False, violations=["LLM failure: {}".format(exc)],
                                  attempts=attempts)
        data = res.obj or {}
        violations = validate_position(data, allowed, valid_ids)
        if not violations:
            refs, _ = check_fact_refs(" ".join(_texts_to_check(data)), valid_ids)
            log.info("position composed: %d refs, %d attempt(s)", len(refs), attempts)
            return PositionResult(ok=True, data=data, attempts=attempts, refs_used=refs)
        log.warning("position guard round %d: %d violations", attempt + 1, len(violations))
        prompt = (
            "{}\n\nYOUR PREVIOUS OUTPUT (as JSON):\n{}\n\n"
            "It failed the grounding guard with these violations:\n{}\n\n"
            "Rewrite the affected sentences so that every number, date and time comes "
            "from the case file exactly as stated there, every bracketed reference "
            "exists, factual sentences in the analysis carry references, and the draft "
            "reply body contains no bracketed ids. Keep everything that was already "
            "compliant. Output ONLY the corrected JSON object."
        ).format(base_prompt, json.dumps(data, ensure_ascii=False)[:14000],
                 "\n".join("- " + v for v in violations[:25]))
    log.error("position failed guard after %d attempts; failing closed", attempts)
    return PositionResult(ok=False, data=data, violations=violations, attempts=attempts)
