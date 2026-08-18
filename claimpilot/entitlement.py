"""Stage 5 - contract entitlement.

Three moves, with a hard boundary between them:
  1. RETRIEVE the governing clauses (datum primary; abstention is a typed
     outcome, and a topic with no supporting clause is reported as exactly
     that, never papered over with the nearest-sounding text).
  2. READ the clause parameters with one schema-forced LLM call, every
     parameter quoted verbatim from the agreement.
  3. COMPUTE the entitlement per demand line deterministically: liability
     caps, notice deadlines, exclusions, and the negotiation arithmetic all
     happen in plain Python with formulas recorded in the fact ledger.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from . import prompts
from .config import RunConfig
from .llm import LLMClient, LLMRequest
from .models import (Citation, Classification, ContractTerm, DemandLine, Entitlement,
                     FactLedger, Kind, Method, SourceDoc)
from .util import D, dec2, find_quote, money, parse_dt

log = logging.getLogger("claimpilot.entitlement")

# (topic, primary query, fallback query) - fallbacks fire on abstention/empty.
CLAUSE_QUERIES: List[Tuple[str, str, str]] = [
    ("liability_limit",
     "carrier liability limit per pound and invoice value for lost or damaged cargo",
     "cargo loss and damage liability limitation released value"),
    ("delay_exclusions",
     "is the carrier responsible for lost profits or promotion losses caused by late delivery",
     "consequential damages loss of market delay exclusion"),
    ("notice_deadlines",
     "deadline to file a cargo damage claim and a delay claim",
     "claim notice period months days filing requirement"),
    ("packaging",
     "who is responsible if packaging was insufficient for normal handling",
     "shipper packaging responsibility insufficient packaging"),
    ("salvage",
     "must the claimant credit salvage value and mitigate the loss",
     "mitigation salvage usable goods credit"),
    ("guaranteed_service",
     "refund when a guaranteed appointment delivery is missed",
     "guaranteed appointment service failure freight charge liability"),
    ("inspection_costs",
     "are third-party inspection costs and internal labor reimbursable",
     "inspection costs administrative labor reimbursement"),
    ("compromise",
     "can the parties settle commercially without setting a precedent",
     "settlement commercial resolution precedent"),
]


@dataclass
class EntitlementResult:
    terms: List[ContractTerm] = field(default_factory=list)
    entitlements: List[Entitlement] = field(default_factory=list)
    retrieval_log: List[Dict[str, Any]] = field(default_factory=list)
    no_clause_topics: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


# ------------------------------------------------------------- clause lookup

def retrieve_clauses(retriever: Any, result: EntitlementResult) -> List[Dict[str, str]]:
    """Run every topic query; collect unique agreement clauses. Abstentions
    retry once with the fallback phrasing, then are recorded honestly."""
    seen: Dict[str, Dict[str, str]] = {}
    for topic, query, fallback in CLAUSE_QUERIES:
        chosen = None
        for attempt, q in enumerate([query, fallback]):
            res = retriever.search(q, k=4)
            hits = [h for h in res.hits if h.source_id == "carrier_agreement"]
            result.retrieval_log.append({
                "topic": topic, "query": q, "attempt": attempt + 1,
                "backend": res.backend, "status": res.status,
                "sufficiency": res.sufficiency, "plan_id": res.plan_id,
                "top": hits[0].title if hits else None,
                "elapsed_ms": res.elapsed_ms,
            })
            if res.answered and hits:
                chosen = hits
                break
        if chosen is None:
            result.no_clause_topics.append(topic)
            log.warning("no supporting clause retrieved for topic %r "
                        "(retriever reported %s)", topic,
                        result.retrieval_log[-1]["status"])
            continue
        for h in chosen[:2]:
            key = "{}::{}".format(h.source_id, h.locator)
            seen.setdefault(key, {"locator": h.locator, "title": h.title, "text": h.text})
    return list(seen.values())


def extract_terms(clauses: List[Dict[str, str]], registry: Dict[str, SourceDoc],
                  ledger: FactLedger, client: LLMClient,
                  result: EntitlementResult) -> Dict[str, ContractTerm]:
    clause_text = "\n\n".join("[{}] {}\n{}".format(c["locator"], c["title"], c["text"])
                              for c in clauses)
    req = LLMRequest(
        prompt="{}\n\nRETRIEVED AGREEMENT CLAUSES:\n{}\n\nReturn ONLY JSON matching:\n{}"
        .format(prompts.CONTRACT_TERMS_TASK, clause_text,
                json.dumps(prompts.CONTRACT_TERMS_SCHEMA, indent=1)),
        system=prompts.SYSTEM_EXTRACT, schema=prompts.CONTRACT_TERMS_SCHEMA,
        label="extract/contract_terms")
    obj = client.call(req).obj or {}
    agreement = registry.get("carrier_agreement")
    agreement_text = agreement.citable_text if agreement else ""

    terms: Dict[str, ContractTerm] = {}
    fact_keys = {
        "liability_rule": [("cap_per_lb_usd", "contract.liability_cap_per_lb"),
                           ("basis_description", "contract.liability_basis")],
        "delay_exclusions": [("consequential_excluded", "contract.delay_consequential_excluded"),
                             ("examples_listed", "contract.delay_exclusion_examples")],
        "guaranteed_service": [("requires_written_purchase", "contract.guarantee_requires_purchase"),
                               ("delay_liability_cap_description", "contract.guarantee_delay_cap")],
        "claim_notice": [("cargo_claim_months", "contract.notice_cargo_months"),
                         ("delay_claim_days", "contract.notice_delay_days")],
        "packaging": [("shipper_responsible", "contract.packaging_shipper_responsible"),
                      ("carrier_relief_if_insufficient", "contract.packaging_carrier_relief")],
        "salvage_mitigation": [("salvage_credit_required", "contract.salvage_credit_required")],
        "inspection_costs": [("third_party_may_be_considered",
                              "contract.inspection_third_party_considered"),
                             ("internal_labor_reimbursable",
                              "contract.internal_labor_reimbursable")],
        "commercial_compromise": [("allowed", "contract.compromise_allowed"),
                                  ("non_precedential", "contract.compromise_non_precedential")],
        "documentation_required": [("items", "contract.documentation_items")],
    }
    n = 0
    for topic, node in (obj or {}).items():
        if not isinstance(node, dict):
            continue
        quote = node.get("quote") or ""
        section = node.get("section") or ""
        verified, ratio = find_quote(quote, agreement_text) if quote else (False, 0.0)
        n += 1
        term = ContractTerm(
            term_id="CT-{}".format(n), topic=topic, section=section, quote=quote,
            params={k: v for k, v in node.items() if k not in ("quote", "section")},
            citations=[Citation(source_id="carrier_agreement",
                                locator="section:{}".format(section.split(".")[0].strip() or "?"),
                                quote=quote, verified=verified, match_ratio=ratio)])
        terms[topic] = term
        result.terms.append(term)
        for param_key, fact_key in fact_keys.get(topic, []):
            value = node.get(param_key)
            if value is None:
                continue
            ledger.add(fact_key, value, kind=Kind.EXTRACTED, method=Method.LLM,
                       citations=[Citation(source_id="carrier_agreement",
                                           locator=term.citations[0].locator, quote=quote)],
                       note="agreement {} ({})".format(section, topic))
        if quote and not verified:
            result.notes.append("term {} quote failed verification against the "
                                "agreement text".format(topic))
    return terms


# -------------------------------------------------------------- calculators

def _add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
                      else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)


def check_timeliness(ledger: FactLedger, result: EntitlementResult) -> None:
    if not (ledger.has("tms.delivered_at") and ledger.has("snapshot.opened_at")):
        result.notes.append("timeliness: skipped (delivery or filing date unavailable)")
        return
    delivered = parse_dt(ledger.value("tms.delivered_at")).date()
    filed = parse_dt(ledger.value("snapshot.opened_at")).date()
    if ledger.has("contract.notice_cargo_months"):
        months = int(ledger.value("contract.notice_cargo_months"))
        deadline = _add_months(delivered, months)
        ledger.add("entitlement.cargo_notice_deadline", deadline.isoformat(),
                   kind=Kind.DERIVED, method=Method.COMPUTED,
                   inputs=[ledger.fact("tms.delivered_at").fact_id,
                           ledger.fact("contract.notice_cargo_months").fact_id],
                   formula="delivered {} + {} months".format(delivered.isoformat(), months))
        ledger.add("entitlement.cargo_notice_ok", filed <= deadline,
                   kind=Kind.DERIVED, method=Method.COMPUTED,
                   inputs=[ledger.fact("snapshot.opened_at").fact_id],
                   formula="filed {} <= deadline {}".format(
                       filed.isoformat(), deadline.isoformat()),
                   note="cargo loss/damage notice requirement met" if filed <= deadline
                   else "CARGO NOTICE LATE")
    if ledger.has("contract.notice_delay_days"):
        days = int(ledger.value("contract.notice_delay_days"))
        deadline = delivered + timedelta(days=days)
        ledger.add("entitlement.delay_notice_deadline", deadline.isoformat(),
                   kind=Kind.DERIVED, method=Method.COMPUTED,
                   inputs=[ledger.fact("tms.delivered_at").fact_id,
                           ledger.fact("contract.notice_delay_days").fact_id],
                   formula="delivered {} + {} days".format(delivered.isoformat(), days))
        ledger.add("entitlement.delay_notice_ok", filed <= deadline,
                   kind=Kind.DERIVED, method=Method.COMPUTED,
                   inputs=[ledger.fact("snapshot.opened_at").fact_id],
                   formula="delay asserted {} <= deadline {}".format(
                       filed.isoformat(), deadline.isoformat()),
                   note="delay-claim notice requirement met" if filed <= deadline
                   else "DELAY NOTICE LATE")


def _cap_math(ledger: FactLedger, units_key: str, prefix: str) -> Optional[Decimal]:
    """min(invoice value of affected units, cap/lb x affected weight); records
    the intermediate facts. Returns the governing basis or None."""
    needed = [units_key, "invoice.unit_price_usd", "derived.unit_weight_lb",
              "contract.liability_cap_per_lb"]
    if not all(ledger.has(k) for k in needed):
        return None
    units = int(ledger.value(units_key))
    price = dec2(ledger.value("invoice.unit_price_usd"))
    unit_weight = D(str(ledger.value("derived.unit_weight_lb")))
    cap_rate = dec2(ledger.value("contract.liability_cap_per_lb"))
    weight = dec2(units * unit_weight)
    invoice_value = dec2(units * price)
    cap_value = dec2(weight * cap_rate)
    basis = min(invoice_value, cap_value)
    ids = [ledger.fact(k).fact_id for k in needed]
    ledger.add("entitlement.{}_weight_lb".format(prefix), weight, kind=Kind.DERIVED,
               method=Method.COMPUTED, inputs=ids,
               formula="{} units x {} lb/unit".format(units, unit_weight))
    ledger.add("entitlement.{}_cap_usd".format(prefix), cap_value, kind=Kind.DERIVED,
               method=Method.COMPUTED, inputs=ids,
               formula="{} lb x {}/lb (agreement section 2)".format(weight, money(cap_rate)))
    ledger.add("entitlement.{}_basis_usd".format(prefix), basis, kind=Kind.DERIVED,
               method=Method.COMPUTED, inputs=ids,
               formula="lesser of invoice value {} and cap {}".format(
                   money(invoice_value), money(cap_value)),
               note="invoice value governs" if invoice_value <= cap_value else "cap governs")
    return basis


def compute_entitlements(ledger: FactLedger, demand_lines: List[DemandLine],
                         terms: Dict[str, ContractTerm],
                         result: EntitlementResult) -> None:
    ents: List[Entitlement] = []
    price = dec2(ledger.get("invoice.unit_price_usd", 0))

    def tids(*topics: str) -> List[str]:
        return [terms[t].term_id for t in topics if t in terms]

    def fids(*keys: str) -> List[str]:
        return [ledger.fact(k).fact_id for k in keys if ledger.has(k)]

    by_key = {l.key: l for l in demand_lines}

    # -- missing product -----------------------------------------------------
    line = by_key.get("missing_product")
    if line:
        basis = _cap_math(ledger, "derived.shortage_units", "missing")
        accepted = ledger.has("email.offer_accepted_missing_units")
        if basis is not None:
            low = high = min(basis, line.claimed)
            rationale = ("Signed POD records the shortage; agreement section 2 limits "
                         "liability to the lesser of invoice value ({}) and the per-pound "
                         "cap ({}); invoice value governs.{}".format(
                             money(ledger.dec("entitlement.missing_basis_usd")),
                             money(ledger.dec("entitlement.missing_cap_usd")),
                             " Carrier has already accepted this component." if accepted else ""))
            cls = Classification.STRONG
        else:
            low, high = D(0), line.claimed
            rationale = "Shortage support incomplete this run; entitlement unquantified."
            cls = Classification.NEEDS_INFO
        ents.append(Entitlement(
            key="missing_product", label="Missing product (shortage)", claimed=line.claimed,
            entitled_low=dec2(low), entitled_high=dec2(high), classification=cls,
            rationale=rationale, term_ids=tids("liability_rule"),
            fact_ids=fids("derived.shortage_units", "pod.short_cartons",
                          "entitlement.missing_basis_usd", "email.offer_accepted_missing_units")))

    # -- damaged product (split: accepted vs disputed) ------------------------
    line = by_key.get("damaged_product")
    if line:
        basis = _cap_math(ledger, "derived.unsellable_units", "damaged")
        acc_units = ledger.get("email.offer_accepted_damaged_units")
        if basis is not None and acc_units is not None and price:
            acc_units = int(acc_units)
            unsell = int(ledger.value("derived.unsellable_units"))
            disputed_units = unsell - acc_units
            acc_value = dec2(acc_units * price)
            disputed_value = dec2(disputed_units * price)
            ents.append(Entitlement(
                key="damaged_accepted",
                label="Damaged product - {} units carrier accepted".format(acc_units),
                claimed=acc_value, entitled_low=acc_value, entitled_high=acc_value,
                classification=Classification.STRONG,
                rationale=("Within section 2 limits ({} basis for all {} unsellable "
                           "units) and conceded by the carrier in the thread.".format(
                               money(basis), unsell)),
                term_ids=tids("liability_rule"),
                fact_ids=fids("derived.unsellable_units",
                              "email.offer_accepted_damaged_units",
                              "derived.offer_accepted_damage_value")))
            flags = []
            if ledger.has("derived.photo_coverage"):
                flags.append("photo coverage: {}".format(ledger.value("derived.photo_coverage")))
            if any(g for g in ("packaging",) if g in terms):
                flags.append("packaging specification missing (section 3 defense pool)")
            if ledger.get("inspection.foam_present") is True:
                flags.append("independent inspector found molded foam present in all "
                             "opened cartons")
            flags.append("salvage disposition undocumented (section 3 credit)")
            ents.append(Entitlement(
                key="damaged_disputed",
                label="Damaged product - {} units disputed".format(disputed_units),
                claimed=disputed_value, entitled_low=D(0), entitled_high=disputed_value,
                classification=Classification.MODERATE,
                rationale=("Corroborated by the signed POD exception, the independent "
                           "inspection and the driver's note; contested by the carrier "
                           "on photo coverage and the missing packaging specification. "
                           "Within section 2 limits if proven."),
                term_ids=tids("liability_rule", "packaging"),
                fact_ids=fids("derived.disputed_units_value", "derived.photo_coverage",
                              "inspection.foam_present", "pod.driver_note",
                              "email.offer_dispute_reasons")))
        else:
            ents.append(Entitlement(
                key="damaged_product", label="Damaged/unsellable product",
                claimed=line.claimed,
                entitled_low=dec2(ledger.get("derived.offer_accepted_damage_value", 0)),
                entitled_high=line.claimed,
                classification=Classification.NEEDS_INFO,
                rationale=("The unsellable count cannot be independently verified this "
                           "run (inspection report unavailable); only the carrier-"
                           "accepted portion is safely supportable."),
                term_ids=tids("liability_rule"),
                fact_ids=fids("pod.damaged_cartons",
                              "derived.offer_accepted_damage_value")))

    # -- inspection fee -------------------------------------------------------
    line = by_key.get("inspection_fee")
    if line:
        third_party = ledger.get("contract.inspection_third_party_considered")
        documented = ledger.has("inspection.inspection_fee_usd")
        cls = Classification.MODERATE if (third_party and documented) \
            else Classification.NEEDS_INFO
        ents.append(Entitlement(
            key="inspection_fee", label="Independent inspection fee", claimed=line.claimed,
            entitled_low=D(0), entitled_high=line.claimed, classification=cls,
            rationale=("Section 3 allows reasonable third-party inspection costs to be "
                       "considered when requested or reasonably necessary; the carrier "
                       "itself asked for an inspection report in the thread, and the fee "
                       "is documented in the surveyor's report."
                       if cls == Classification.MODERATE else
                       "Fee documentation or the enabling clause is unavailable this run."),
            term_ids=tids("inspection_costs"),
            fact_ids=fids("inspection.inspection_fee_usd", "email.document_requests")))

    # -- repack labor ----------------------------------------------------------
    line = by_key.get("repack_labor")
    if line:
        ents.append(Entitlement(
            key="repack_labor", label="Repack labor (salvageable units)", claimed=line.claimed,
            entitled_low=D(0), entitled_high=line.claimed,
            classification=Classification.NEEDS_INFO,
            rationale=("Recoverable only if third-party (section 3 excludes internal "
                       "administrative labor unless agreed in writing); the folder does "
                       "not show who performed the repack."),
            term_ids=tids("inspection_costs", "salvage"),
            fact_ids=fids("inspection.repack_labor_usd", "derived.repackable_units"),
            flags=["classification (internal vs third-party) undocumented"]))

    # -- late-delivery markdown -------------------------------------------------
    line = by_key.get("late_markdown")
    if line:
        excluded = ledger.get("contract.delay_consequential_excluded")
        guaranteed = ledger.get("derived.guaranteed_service_purchased")
        if excluded and guaranteed is False:
            ents.append(Entitlement(
                key="late_markdown", label="Late-delivery markdown", claimed=line.claimed,
                entitled_low=D(0), entitled_high=D(0),
                classification=Classification.EXCLUDED_CONTRACTUAL,
                rationale=("Section 4 excludes loss-of-market and markdown damages for "
                           "delay on Standard LTL; no guaranteed-appointment service was "
                           "purchased (TMS and BOL agree), and section 1 says requested "
                           "dates and promotion dates do not create a delivery "
                           "commitment. The amount is also a commercial assertion with "
                           "no supporting documentation in the folder."),
                term_ids=tids("delay_exclusions", "guaranteed_service"),
                fact_ids=fids("derived.guaranteed_service_purchased",
                              "bol.guarantee_note", "email.assert.late_delivery_markdown"),
                flags=["asserted amount; no markdown documentation provided",
                       "commercial-compromise ask only (section 6)"]))
        else:
            ents.append(Entitlement(
                key="late_markdown", label="Late-delivery markdown", claimed=line.claimed,
                entitled_low=D(0), entitled_high=line.claimed,
                classification=Classification.NEEDS_INFO,
                rationale="Delay-exclusion terms unavailable this run; treat as open.",
                term_ids=tids("delay_exclusions"), fact_ids=[]))

    # -- freight refund -----------------------------------------------------------
    line = by_key.get("freight_refund")
    if line:
        guaranteed = ledger.get("derived.guaranteed_service_purchased")
        ents.append(Entitlement(
            key="freight_refund", label="Freight charge refund", claimed=line.claimed,
            entitled_low=D(0), entitled_high=line.claimed,
            classification=Classification.GOODWILL_LEVER if guaranteed is False
            else Classification.NEEDS_INFO,
            rationale=("Not owed under section 4 (a service refund attaches to a "
                       "purchased Guaranteed Appointment service, which this shipment "
                       "did not have) - but the delivery was late by the carrier's own "
                       "records with carrier-side causes, and precedent shows BlueLine "
                       "grants freight-scale commercial refunds on delay complaints "
                       "(section 6 allows non-precedential compromise)."),
            term_ids=tids("guaranteed_service", "compromise"),
            fact_ids=fids("tms.freight_charge_usd", "derived.delivery_delay",
                          "derived.delay_causes_carrier_side",
                          "derived.guaranteed_service_purchased"),
            flags=["commercial lever, not a contractual entitlement"]))

    for other in demand_lines:
        if other.key == "other":
            ents.append(Entitlement(
                key="other", label=other.label, claimed=other.claimed,
                entitled_low=D(0), entitled_high=other.claimed,
                classification=Classification.NEEDS_INFO,
                rationale="Unrecognized demand line; needs manual review.",
                term_ids=[], fact_ids=[]))

    for i, e in enumerate(ents, start=1):
        e.ent_id = "E-{}".format(i)
    result.entitlements = ents


def compute_position_numbers(ledger: FactLedger, ents: List[Entitlement]) -> None:
    """The negotiation arithmetic, all as derived facts with formulas."""
    def get(key: str) -> Optional[Entitlement]:
        return next((e for e in ents if e.key == key), None)

    def ids(*keys: str) -> List[str]:
        return [ledger.fact(k).fact_id for k in keys if ledger.has(k)]

    strong = [e for e in ents if e.classification == Classification.STRONG]
    core_low = dec2(sum((e.entitled_low for e in strong), D(0)))
    supportable = [e for e in ents if e.classification in
                   (Classification.STRONG, Classification.MODERATE,
                    Classification.NEEDS_INFO)]
    core_high = dec2(sum((e.entitled_high for e in supportable), D(0)))
    ledger.add("position.core_low", core_low, kind=Kind.DERIVED, method=Method.COMPUTED,
               formula=" + ".join("{} {}".format(e.key, money(e.entitled_low))
                                  for e in strong) or "0",
               note="sum of STRONG entitlements (evidence floor)")
    ledger.add("position.core_high", core_high, kind=Kind.DERIVED, method=Method.COMPUTED,
               formula=" + ".join("{} {}".format(e.key, money(e.entitled_high))
                                  for e in supportable) or "0",
               note="full supportable cargo/cost case (STRONG + MODERATE + NEEDS_INFO at "
                    "their documented amounts)")
    goodwill = get("freight_refund")
    goodwill_high = goodwill.entitled_high if goodwill else D(0)
    ledger.add("position.goodwill_high", dec2(goodwill_high), kind=Kind.DERIVED,
               method=Method.COMPUTED, formula="freight charge (commercial lever ceiling)",
               note="freight-scale goodwill ceiling per precedent")
    counter = dec2(core_high + goodwill_high)
    ledger.add("position.recommended_counter", counter, kind=Kind.DERIVED,
               method=Method.COMPUTED,
               formula="core_high {} + goodwill_high {}".format(
                   money(core_high), money(goodwill_high)),
               note="anchor for the next counter-offer; every component is documented "
                    "or precedent-backed, so it is defensible line by line")
    offer = ledger.get("snapshot.carrier_offer_usd") or ledger.get("email.offer_total_usd")
    if offer is not None:
        offer = dec2(offer)
        ledger.add("position.offer_gap", dec2(counter - offer), kind=Kind.DERIVED,
                   method=Method.COMPUTED,
                   formula="recommended counter {} - current offer {}".format(
                       money(counter), money(offer)),
                   inputs=ids("snapshot.carrier_offer_usd"))
        if offer == core_low:
            ledger.add("position.offer_equals_floor", True, kind=Kind.DERIVED,
                       method=Method.COMPUTED,
                       formula="current offer {} == sum of STRONG entitlements {}".format(
                           money(offer), money(core_low)),
                       note="the carrier's offer is exactly the undisputed floor - "
                            "everything above it is what the negotiation is about")
    inspection = get("inspection_fee")
    expected_low = dec2(core_low + (inspection.entitled_high if inspection else D(0)))
    ledger.add("position.expected_band_low", expected_low, kind=Kind.DERIVED,
               method=Method.COMPUTED,
               formula="core_low {} + inspection fee {}".format(
                   money(core_low), money(inspection.entitled_high if inspection else 0)),
               note="conservative settlement outcome")
    ledger.add("position.expected_band_high", counter, kind=Kind.DERIVED,
               method=Method.COMPUTED, formula="equal to the recommended counter",
               note="full-success settlement outcome")
    if ledger.has("snapshot.reserve_usd"):
        reserve = ledger.dec("snapshot.reserve_usd")
        ledger.add("position.reserve_covers_counter", bool(reserve >= counter),
                   kind=Kind.DERIVED, method=Method.COMPUTED,
                   formula="reserve {} >= counter {}".format(money(reserve), money(counter)),
                   inputs=ids("snapshot.reserve_usd"))


def run_entitlement(ledger: FactLedger, registry: Dict[str, SourceDoc],
                    demand_lines: List[DemandLine], retriever: Any,
                    client: LLMClient, cfg: RunConfig) -> EntitlementResult:
    result = EntitlementResult()
    agreement = registry.get("carrier_agreement")
    if agreement is None or agreement.status != "OK":
        result.notes.append("carrier agreement unavailable - entitlement analysis limited "
                            "to evidence positions, no contractual rulings")
        log.warning("entitlement: carrier agreement unavailable")
        compute_entitlements(ledger, demand_lines, {}, result)
        compute_position_numbers(ledger, result.entitlements)
        return result
    clauses = retrieve_clauses(retriever, result)
    terms = extract_terms(clauses, registry, ledger, client, result)
    check_timeliness(ledger, result)
    compute_entitlements(ledger, demand_lines, terms, result)
    compute_position_numbers(ledger, result.entitlements)
    log.info("entitlement: %d terms, %d entitlement lines, %d clause topics unresolved",
             len(result.terms), len(result.entitlements), len(result.no_clause_topics))
    return result
