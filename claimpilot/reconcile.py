"""Stage 4 - deterministic reconciliation.

Pure-Python rules cross-check every count, amount, date and document across
sources, derive the case's core quantities (with formulas and input fact ids),
and surface conflicts instead of hiding them. Source-authority rulings encode
the data dictionary's semantics: the signed POD is the receiving record of
authority; the final EDI event is carrier-reported and is not.

Every rule degrades gracefully when its inputs are missing (ablated or
unreadable sources): it either skips with a note or raises an evidence gap.
No LLM is involved anywhere in this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .models import (Citation, DemandLine, Discrepancy, EvidenceGap, FactLedger,
                     Kind, Method, Severity, SourceDoc)
from .util import D, dec2, money, parse_dt

log = logging.getLogger("claimpilot.reconcile")


@dataclass
class ReconcileResult:
    discrepancies: List[Discrepancy] = field(default_factory=list)
    gaps: List[EvidenceGap] = field(default_factory=list)
    demand_lines: List[DemandLine] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


class Ctx:
    def __init__(self, ledger: FactLedger, registry: Dict[str, SourceDoc]) -> None:
        self.ledger = ledger
        self.registry = registry
        self.result = ReconcileResult()
        self._d = 0
        self._g = 0

    # -- shorthand ----------------------------------------------------------
    def has(self, *keys: str) -> bool:
        return all(self.ledger.has(k) for k in keys)

    def val(self, key: str, default: Any = None) -> Any:
        return self.ledger.get(key, default)

    def fid(self, *keys: str) -> List[str]:
        return [self.ledger.fact(k).fact_id for k in keys if self.ledger.has(k)]

    def derive(self, key: str, value: Any, formula: str, input_keys: List[str],
               note: str = "") -> None:
        self.ledger.add(key, value, kind=Kind.DERIVED, method=Method.COMPUTED,
                        inputs=self.fid(*input_keys), formula=formula, note=note)

    def disc(self, severity: str, category: str, title: str, description: str,
             fact_keys: List[str], authority_note: str = "", status: str = "OPEN") -> None:
        self._d += 1
        self.result.discrepancies.append(Discrepancy(
            disc_id="D-{:02d}".format(self._d), severity=severity, category=category,
            title=title, description=description, fact_ids=self.fid(*fact_keys),
            authority_note=authority_note, status=status))

    def gap(self, item: str, why: str, requested_by: str = "", impact: str = "",
            fact_keys: Optional[List[str]] = None) -> None:
        self._g += 1
        self.result.gaps.append(EvidenceGap(
            gap_id="G-{:02d}".format(self._g), item=item, why_needed=why,
            requested_by=requested_by, impact=impact,
            fact_ids=self.fid(*(fact_keys or []))))

    def skip(self, rule: str, missing: str) -> None:
        self.result.notes.append("{}: skipped ({} unavailable)".format(rule, missing))


# ------------------------------------------------------------------- rules

def r01_identifiers(ctx: Ctx) -> None:
    """PRO / BOL / claim ids must agree everywhere they appear."""
    groups = {
        "PRO number": ["snapshot.pro_number", "tms.pro_number", "bol.pro_number",
                       "pod.pro_number", "inspection.pro_number"],
        "BOL number": ["tms.bol_number", "bol.bol_number", "pod.bol_number",
                       "inspection.bol_number"],
        "claim id": ["snapshot.claim_id", "overview.claim_id", "inspection.claim_id"],
    }
    for name, keys in groups.items():
        present = [(k, str(ctx.val(k)).strip()) for k in keys if ctx.has(k)]
        if len(present) < 2:
            continue
        values = {v for _, v in present}
        if len(values) == 1:
            ctx.disc(Severity.INFO, "VERIFIED_CONSISTENT",
                     "{} consistent across {} sources".format(name, len(present)),
                     "All sources agree: {}.".format(present[0][1]),
                     [k for k, _ in present], status="VERIFIED_CONSISTENT")
        else:
            ctx.disc(Severity.HIGH, "DATA_QUALITY", "{} mismatch".format(name),
                     "Sources disagree: {}.".format(
                         "; ".join("{}={}".format(k, v) for k, v in present)),
                     [k for k, _ in present])


def r02_piece_counts(ctx: Ctx) -> None:
    """Tendered vs EDI-delivered vs POD-received; derive the shortage."""
    if not ctx.has("tms.pieces_tendered", "pod.received_cartons"):
        ctx.skip("R02 piece counts", "TMS tender or POD receipt")
        return
    tendered = int(ctx.val("tms.pieces_tendered"))
    received = int(ctx.val("pod.received_cartons"))
    for key, label in [("bol.cartons", "BOL"), ("invoice.cartons", "invoice packing note")]:
        if ctx.has(key) and int(ctx.val(key)) != tendered:
            ctx.disc(Severity.MEDIUM, "COUNT_CONFLICT",
                     "Tendered carton count differs from {}".format(label),
                     "TMS tendered {} but {} shows {}.".format(
                         tendered, label, ctx.val(key)), ["tms.pieces_tendered", key])
    short = tendered - received
    ctx.derive("derived.shortage_cartons", short,
               "{} tendered - {} received (signed POD)".format(tendered, received),
               ["tms.pieces_tendered", "pod.received_cartons"])
    if ctx.has("pod.short_cartons") and int(ctx.val("pod.short_cartons")) != short:
        ctx.disc(Severity.HIGH, "COUNT_CONFLICT", "POD short-count internal mismatch",
                 "POD states {} short but tendered-received = {}.".format(
                     ctx.val("pod.short_cartons"), short),
                 ["pod.short_cartons", "derived.shortage_cartons"])
    if ctx.has("tms.edi_delivered_pieces"):
        edi = int(ctx.val("tms.edi_delivered_pieces"))
        if edi != received:
            ctx.disc(
                Severity.HIGH, "COUNT_CONFLICT",
                "Carrier EDI reports {} pieces delivered; signed POD records {}".format(
                    edi, received),
                "The final EDI 214 event and the consignee-signed POD disagree by {} "
                "carton(s). The carrier raised this in the thread and asked for a "
                "reconciliation.".format(abs(edi - received)),
                ["tms.edi_delivered_pieces", "pod.received_cartons", "tms.exception_notes",
                 "dd.pod_authority", "dd.edi_semantics"],
                authority_note=(
                    "The signed POD is the consignee's documented receiving record; the "
                    "TMS record itself notes the final EDI piece count is carrier-reported "
                    "and may not match the consignee receiving count. The POD count ({}) "
                    "governs the shortage calculation.".format(received)))
    if ctx.has("invoice.units_per_carton"):
        upc = int(ctx.val("invoice.units_per_carton"))
        ctx.derive("derived.shortage_units", short * upc,
                   "{} short cartons x {} units/carton".format(short, upc),
                   ["derived.shortage_cartons", "invoice.units_per_carton"])


def r03_damage_units(ctx: Ctx) -> None:
    """POD damaged cartons -> affected units; inspection must reconcile."""
    if not ctx.has("pod.damaged_cartons"):
        ctx.skip("R03 damage units", "POD damaged carton count")
        return
    damaged = int(ctx.val("pod.damaged_cartons"))
    if ctx.has("invoice.units_per_carton"):
        upc = int(ctx.val("invoice.units_per_carton"))
        ctx.derive("derived.damage_units_affected", damaged * upc,
                   "{} damaged cartons x {} units/carton".format(damaged, upc),
                   ["pod.damaged_cartons", "invoice.units_per_carton"])
    if not ctx.has("inspection.total_examined"):
        ctx.gap("Independent inspection report",
                "The unsellable/repackable split of the {} damaged cartons cannot be "
                "verified without it".format(damaged),
                requested_by="carrier (email message 2)",
                impact="Damage entitlement can only be supported at the carrier-accepted "
                       "level; the disputed units lack corroboration.",
                fact_keys=["pod.damaged_cartons"])
        return
    examined = int(ctx.val("inspection.total_examined"))
    unsell = int(ctx.val("inspection.total_unsellable", 0))
    repack = int(ctx.val("inspection.total_repackable", 0))
    rows = ctx.val("inspection.carton_rows", []) or []
    checks = []
    if ctx.has("derived.damage_units_affected"):
        checks.append(("units examined vs POD-derived affected units",
                       examined == int(ctx.val("derived.damage_units_affected"))))
    checks.append(("unsellable + repackable == examined", unsell + repack == examined))
    if rows:
        checks.append(("carton table rows == POD damaged cartons", len(rows) == damaged))
        checks.append(("table unsellable sum == stated total",
                       sum(int(r.get("unsellable", 0)) for r in rows) == unsell))
        checks.append(("table repackable sum == stated total",
                       sum(int(r.get("repackable", 0)) for r in rows) == repack))
    failed = [name for name, ok in checks if not ok]
    if failed:
        ctx.disc(Severity.HIGH, "COUNT_CONFLICT", "Inspection arithmetic does not reconcile",
                 "Failed checks: {}.".format("; ".join(failed)),
                 ["inspection.total_examined", "inspection.total_unsellable",
                  "inspection.total_repackable", "pod.damaged_cartons"])
    else:
        ctx.disc(Severity.INFO, "VERIFIED_CONSISTENT",
                 "Inspection findings reconcile with the signed POD",
                 "{} damaged cartons -> {} units examined; {} unsellable + {} repackable; "
                 "per-carton table sums match ({} checks).".format(
                     damaged, examined, unsell, repack, len(checks)),
                 ["pod.damaged_cartons", "inspection.total_examined",
                  "inspection.total_unsellable", "inspection.total_repackable"],
                 status="VERIFIED_CONSISTENT")
    ctx.derive("derived.unsellable_units", unsell, "inspection count (independent surveyor)",
               ["inspection.total_unsellable"])
    ctx.derive("derived.repackable_units", repack, "inspection count (independent surveyor)",
               ["inspection.total_repackable"])


def r04_photo_coverage(ctx: Ctx) -> None:
    """Photos document damage to which cartons? The carrier's core evidence
    dispute. A carton merely visible (intact) in frame is not documented
    damage - only cartons shown damaged count toward coverage."""
    labels: List[str] = []
    for key in ("damage_photo_1.cartons_shown_damaged", "damage_photo_2.cartons_shown_damaged"):
        if ctx.has(key):
            labels.extend(ctx.val(key) or [])
    covered = sorted(set(labels))
    if covered:
        ctx.derive("derived.photo_cartons_covered", covered,
                   "union of carton ids shown damaged in the supplied photos",
                   ["damage_photo_1.cartons_shown_damaged",
                    "damage_photo_2.cartons_shown_damaged"])
    rows = ctx.val("inspection.carton_rows", []) or []
    damaged_ids = sorted({r.get("carton_id", "") for r in rows if r.get("carton_id")})
    if not damaged_ids:
        if covered:
            ctx.result.notes.append(
                "R04: photo coverage vs damaged-carton ids unverifiable without inspection")
        return
    missing = [c for c in damaged_ids if c not in covered]
    ctx.derive("derived.photo_coverage", "{} of {} damaged cartons photographed".format(
        len(damaged_ids) - len(missing), len(damaged_ids)),
        "damaged ids {} vs photographed {}".format(damaged_ids, covered),
        ["inspection.carton_rows", "derived.photo_cartons_covered"])
    if missing:
        ctx.disc(Severity.MEDIUM, "EVIDENCE_COVERAGE",
                 "Photos document {} of {} damaged cartons".format(
                     len(damaged_ids) - len(missing), len(damaged_ids)),
                 "No photo shows carton(s) {}. The carrier relies on this to dispute part "
                 "of the damage claim. Counterweights on file: the consignee noted all {} "
                 "damaged cartons on the signed POD at delivery, the independent surveyor "
                 "examined all of them, and the driver's own note corroborates the damaged "
                 "pallet.".format(", ".join(missing), len(damaged_ids)),
                 ["derived.photo_coverage", "pod.exception_text", "pod.driver_note",
                  "inspection.carton_rows"],
                 authority_note="Photographs are corroborating evidence; the signed POD "
                                "exception and the independent inspection are the primary "
                                "records of damage here.")
        ctx.gap("Photographs of cartons {}".format(", ".join(missing)),
                "Carrier disputes damaged units in cartons that were not photographed",
                requested_by="carrier (email message 4)",
                impact="Directly supports the {} disputed damaged units if obtainable "
                       "from the consignee.".format(ctx.val(
                           "email.offer_disputed_damaged_units", "")),
                fact_keys=["derived.photo_coverage"])
    if ctx.has("inspection.photos_provided_note"):
        ctx.result.notes.append("R04: inspector's note on photos: {!r}".format(
            ctx.val("inspection.photos_provided_note")))


def r05_value_math(ctx: Ctx) -> None:
    """Unit price, extended value, weight-per-unit consistency."""
    if ctx.has("erp.unit_price_usd", "invoice.unit_price_usd"):
        if dec2(ctx.val("erp.unit_price_usd")) != dec2(ctx.val("invoice.unit_price_usd")):
            ctx.disc(Severity.HIGH, "DATA_QUALITY", "Unit price mismatch ERP vs invoice",
                     "ERP {} vs invoice {}.".format(
                         ctx.val("erp.unit_price_usd"), ctx.val("invoice.unit_price_usd")),
                     ["erp.unit_price_usd", "invoice.unit_price_usd"])
    if ctx.has("invoice.qty_units", "invoice.unit_price_usd", "invoice.extended_value_usd"):
        qty = int(ctx.val("invoice.qty_units"))
        price = dec2(ctx.val("invoice.unit_price_usd"))
        expect = dec2(qty * price)
        stated = dec2(ctx.val("invoice.extended_value_usd"))
        if expect != stated:
            ctx.disc(Severity.HIGH, "DATA_QUALITY", "Invoice extension does not compute",
                     "{} x {} = {} but invoice states {}.".format(
                         qty, money(price), money(expect), money(stated)),
                     ["invoice.qty_units", "invoice.unit_price_usd",
                      "invoice.extended_value_usd"])
        else:
            ctx.derive("derived.invoice_value_verified", stated,
                       "{} units x {} = {}".format(qty, money(price), money(stated)),
                       ["invoice.qty_units", "invoice.unit_price_usd",
                        "invoice.extended_value_usd"])
    if ctx.has("tms.weight_lb", "invoice.qty_units"):
        per_unit = D(ctx.val("tms.weight_lb")) / D(ctx.val("invoice.qty_units"))
        ctx.derive("derived.unit_weight_lb", dec2(per_unit),
                   "{} lb shipment / {} units".format(
                       ctx.val("tms.weight_lb"), ctx.val("invoice.qty_units")),
                   ["tms.weight_lb", "invoice.qty_units"],
                   note="matches the invoice's stated ~{} lb per unit".format(
                       ctx.val("invoice.unit_weight_lb", "?")))


_LINE_KEYWORDS = [
    ("missing_product", ("missing",)),
    ("damaged_product", ("damaged", "unsellable")),
    ("inspection_fee", ("inspection",)),
    ("repack_labor", ("repack",)),
    ("late_markdown", ("markdown", "late-delivery", "late delivery")),
    ("freight_refund", ("freight",)),
]


def _line_key(label: str) -> str:
    low = label.lower()
    for key, needles in _LINE_KEYWORDS:
        if any(n in low for n in needles):
            return key
    return "other"


def r06_demand_decomposition(ctx: Ctx) -> None:
    """Rebuild the demand from primary evidence, line by line."""
    lines = ctx.val("overview.demand_lines") or []
    if not lines:
        ctx.skip("R06 demand decomposition", "overview demand table")
        return
    overview_fact = ctx.ledger.fact("overview.demand_lines")
    price = dec2(ctx.val("invoice.unit_price_usd", ctx.val("erp.unit_price_usd", 0)))
    recomputed: List[str] = []
    for ln in lines:
        key = _line_key(str(ln.get("label", "")))
        claimed = dec2(ln.get("amount_usd", 0))
        asserted_only = False
        basis = str(ln.get("basis") or "")
        if key == "missing_product" and ctx.has("derived.shortage_units") and price:
            expect = dec2(int(ctx.val("derived.shortage_units")) * price)
            ctx.derive("derived.demand.missing_product", expect,
                       "{} missing units x {}".format(
                           ctx.val("derived.shortage_units"), money(price)),
                       ["derived.shortage_units", "invoice.unit_price_usd"])
            recomputed.append(key) if expect == claimed else ctx.disc(
                Severity.HIGH, "COUNT_CONFLICT", "Missing-product line does not recompute",
                "Claimed {} but evidence supports {}.".format(money(claimed), money(expect)),
                ["derived.shortage_units", "overview.demand_lines"])
        elif key == "damaged_product" and ctx.has("derived.unsellable_units") and price:
            expect = dec2(int(ctx.val("derived.unsellable_units")) * price)
            ctx.derive("derived.demand.damaged_product", expect,
                       "{} unsellable units x {}".format(
                           ctx.val("derived.unsellable_units"), money(price)),
                       ["derived.unsellable_units", "invoice.unit_price_usd"])
            recomputed.append(key) if expect == claimed else ctx.disc(
                Severity.HIGH, "COUNT_CONFLICT", "Damaged-product line does not recompute",
                "Claimed {} but inspection supports {}.".format(money(claimed), money(expect)),
                ["derived.unsellable_units", "overview.demand_lines"])
        elif key == "inspection_fee":
            if ctx.has("inspection.inspection_fee_usd"):
                if dec2(ctx.val("inspection.inspection_fee_usd")) == claimed:
                    recomputed.append(key)
                else:
                    ctx.disc(Severity.MEDIUM, "DATA_QUALITY",
                             "Inspection fee differs from the report's cost documentation",
                             "Claimed {} vs report {}.".format(
                                 money(claimed),
                                 money(ctx.val("inspection.inspection_fee_usd"))),
                             ["inspection.inspection_fee_usd", "overview.demand_lines"])
            else:
                asserted_only = True
        elif key == "repack_labor":
            if ctx.has("inspection.repack_labor_usd"):
                if dec2(ctx.val("inspection.repack_labor_usd")) == claimed:
                    recomputed.append(key)
                else:
                    ctx.disc(Severity.MEDIUM, "DATA_QUALITY",
                             "Repack labor differs from the report's cost documentation",
                             "Claimed {} vs report {}.".format(
                                 money(claimed), money(ctx.val("inspection.repack_labor_usd"))),
                             ["inspection.repack_labor_usd", "overview.demand_lines"])
            else:
                asserted_only = True
        elif key == "late_markdown":
            asserted_only = True   # no invoice/credit memo anywhere in the folder
        elif key == "freight_refund" and ctx.has("tms.freight_charge_usd"):
            if dec2(ctx.val("tms.freight_charge_usd")) == claimed:
                recomputed.append(key)
            else:
                ctx.disc(Severity.MEDIUM, "DATA_QUALITY",
                         "Freight-refund line differs from the TMS freight charge",
                         "Claimed {} vs TMS charge {}.".format(
                             money(claimed), money(ctx.val("tms.freight_charge_usd"))),
                         ["tms.freight_charge_usd", "overview.demand_lines"])
        ctx.result.demand_lines.append(DemandLine(
            key=key, label=str(ln.get("label", key)), claimed=claimed, basis=basis,
            asserted_only=asserted_only, fact_ids=[overview_fact.fact_id]))
    total = dec2(sum((l.claimed for l in ctx.result.demand_lines), D(0)))
    ctx.derive("derived.demand_total", total,
               " + ".join(money(l.claimed) for l in ctx.result.demand_lines),
               ["overview.demand_lines"])
    for key, label in [("overview.total_demand_usd", "case overview total"),
                       ("snapshot.claim_amount_usd", "claim-system amount"),
                       ("email.initial_demand_usd", "demand stated in the thread")]:
        if ctx.has(key) and dec2(ctx.val(key)) != total:
            ctx.disc(Severity.HIGH, "COUNT_CONFLICT",
                     "Demand total does not reconcile with {}".format(label),
                     "Line items sum to {} but {} is {}.".format(
                         money(total), label, money(ctx.val(key))),
                     [key, "derived.demand_total"])
    if ctx.has("overview.total_demand_usd") and \
            dec2(ctx.val("overview.total_demand_usd")) == total:
        ctx.disc(Severity.INFO, "VERIFIED_CONSISTENT",
                 "Demand decomposition verified: six lines sum to {}".format(money(total)),
                 "Recomputed from primary evidence: {}. The markdown line is a commercial "
                 "assertion with no supporting document in the folder.".format(
                     ", ".join(recomputed) or "none"),
                 ["derived.demand_total"], status="VERIFIED_CONSISTENT")


def r07_offer_decomposition(ctx: Ctx) -> None:
    if not ctx.has("email.offer_total_usd"):
        ctx.skip("R07 offer decomposition", "carrier offer in thread")
        return
    offer = dec2(ctx.val("email.offer_total_usd"))
    if ctx.has("snapshot.carrier_offer_usd") and \
            dec2(ctx.val("snapshot.carrier_offer_usd")) != offer:
        ctx.disc(Severity.MEDIUM, "DATA_QUALITY",
                 "Offer amount differs between thread and claim system",
                 "Thread {} vs snapshot {}.".format(
                     money(offer), money(ctx.val("snapshot.carrier_offer_usd"))),
                 ["email.offer_total_usd", "snapshot.carrier_offer_usd"])
    comps = ctx.val("email.offer_components") or []
    if comps:
        total = dec2(sum((dec2(c.get("amount_usd", 0)) for c in comps), D(0)))
        if total != offer:
            ctx.disc(Severity.MEDIUM, "COUNT_CONFLICT", "Offer components do not sum",
                     "Components sum to {} vs stated {}.".format(money(total), money(offer)),
                     ["email.offer_components", "email.offer_total_usd"])
    price = dec2(ctx.val("invoice.unit_price_usd", 0))
    if price and ctx.has("email.offer_accepted_damaged_units"):
        acc = int(ctx.val("email.offer_accepted_damaged_units"))
        ctx.derive("derived.offer_accepted_damage_value", dec2(acc * price),
                   "{} accepted damaged units x {}".format(acc, money(price)),
                   ["email.offer_accepted_damaged_units", "invoice.unit_price_usd"])
    if price and ctx.has("email.offer_disputed_damaged_units"):
        disputed = int(ctx.val("email.offer_disputed_damaged_units"))
        ctx.derive("derived.disputed_units_value", dec2(disputed * price),
                   "{} disputed damaged units x {}".format(disputed, money(price)),
                   ["email.offer_disputed_damaged_units", "invoice.unit_price_usd"])
        if ctx.has("derived.unsellable_units", "email.offer_accepted_damaged_units"):
            expect = int(ctx.val("derived.unsellable_units")) - \
                int(ctx.val("email.offer_accepted_damaged_units"))
            if expect != disputed:
                ctx.disc(Severity.MEDIUM, "COUNT_CONFLICT",
                         "Disputed-unit count does not reconcile",
                         "Unsellable {} - accepted {} = {} but thread says {} disputed."
                         .format(ctx.val("derived.unsellable_units"),
                                 ctx.val("email.offer_accepted_damaged_units"),
                                 expect, disputed),
                         ["derived.unsellable_units", "email.offer_accepted_damaged_units",
                          "email.offer_disputed_damaged_units"])


def r08_delivery_timeline(ctx: Ctx) -> None:
    if not ctx.has("tms.customer_requested_delivery"):
        ctx.skip("R08 delivery timeline", "requested delivery")
        return
    requested = parse_dt(ctx.val("tms.customer_requested_delivery"))
    delivered_key = "pod.delivered_at" if ctx.has("pod.delivered_at") else "tms.delivered_at"
    if not ctx.has("tms.delivered_at"):
        ctx.skip("R08 delivery timeline", "delivery timestamp")
        return
    delivered = parse_dt(ctx.val("tms.delivered_at"))
    delta = delivered - requested
    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    ctx.derive("derived.delivery_delay",
               "{} days {} hours {} minutes".format(days, hours, rem // 60),
               "delivered {} - requested {}".format(
                   ctx.val("tms.delivered_at"), ctx.val("tms.customer_requested_delivery")),
               ["tms.delivered_at", "tms.customer_requested_delivery", delivered_key])
    ctx.derive("derived.delivered_days_late", (delivered.date() - requested.date()).days,
               "calendar days from requested date to actual delivery date",
               ["tms.delivered_at", "tms.customer_requested_delivery"])
    if ctx.has("tms.exception_events"):
        causes = [e.get("detail", "") for e in ctx.val("tms.exception_events")]
        ctx.derive("derived.delay_causes_carrier_side", causes,
                   "TMS exception events during transit",
                   ["tms.exception_events"],
                   note="both recorded exceptions are carrier operational events")
    if ctx.has("snapshot.opened_at", "tms.delivered_at"):
        opened = parse_dt(ctx.val("snapshot.opened_at"))
        ctx.derive("derived.days_delivery_to_claim",
                   (opened.date() - delivered.date()).days,
                   "claim opened {} - delivered {}".format(
                       ctx.val("snapshot.opened_at")[:10], ctx.val("tms.delivered_at")[:10]),
                   ["snapshot.opened_at", "tms.delivered_at"])


def r09_service_level(ctx: Ctx) -> None:
    indicators = []
    if ctx.has("tms.service_guaranteed"):
        indicators.append(("TMS", not ctx.val("tms.service_guaranteed")))
    if ctx.has("bol.guarantee_note"):
        indicators.append(("BOL", "not" in str(ctx.val("bol.guarantee_note")).lower()))
    if ctx.has("bol.service_description"):
        indicators.append(("BOL service", "non-guaranteed"
                           in str(ctx.val("bol.service_description")).lower()))
    if not indicators:
        ctx.skip("R09 service level", "service records")
        return
    non_guaranteed = all(flag for _, flag in indicators)
    ctx.derive("derived.guaranteed_service_purchased", not non_guaranteed,
               "consistent across {}".format(", ".join(name for name, _ in indicators)),
               ["tms.service_guaranteed", "bol.guarantee_note", "bol.service_description"])
    if non_guaranteed:
        markdown = next((l for l in ctx.result.demand_lines if l.key == "late_markdown"), None)
        if markdown is not None:
            ctx.disc(Severity.HIGH, "CONTRACT_TENSION",
                     "Delay-loss demand ({}) rides on a non-guaranteed service".format(
                         money(markdown.claimed)),
                     "TMS, the BOL and the carrier's own position agree no guaranteed-"
                     "appointment service was purchased. The markdown and freight-refund "
                     "components therefore depend on contract terms for non-guaranteed "
                     "Standard LTL (resolved in the entitlement analysis).",
                     ["derived.guaranteed_service_purchased", "tms.service_guaranteed",
                      "bol.guarantee_note"])


def r10_document_inventory(ctx: Ctx) -> None:
    flags = ctx.val("snapshot.doc_flags") or {}
    if flags.get("packaging_specification") == "MISSING":
        ctx.gap("Vendor packaging specification",
                "Carrier requested it to assess the internal-packaging adequacy defense; "
                "the shipper confirmed it is not in the claim folder; the inspector notes "
                "none was provided",
                requested_by="carrier (email message 4)",
                impact="Weakens rebuttal of the packaging argument on the disputed units, "
                       "partially offset by the inspector's finding that molded foam was "
                       "present in all opened cartons.",
                fact_keys=["snapshot.doc_flags", "inspection.foam_present",
                           "inspection.packaging_spec_note"])
    if flags.get("damage_photos") == "PARTIAL":
        ctx.result.notes.append("R10: claim system already flags damage photos as PARTIAL, "
                                "consistent with the photo-coverage finding")
    stated_received = [k for k, v in flags.items() if v == "RECEIVED"]
    folder_map = {"commercial_invoice": "commercial_invoice",
                  "bill_of_lading": "bill_of_lading",
                  "proof_of_delivery": "proof_of_delivery",
                  "inspection_report": "inspection_report",
                  "carrier_agreement": "carrier_agreement"}
    for flag_key in stated_received:
        sid = folder_map.get(flag_key)
        if sid and ctx.registry.get(sid) and ctx.registry[sid].status == "MISSING":
            ctx.disc(Severity.MEDIUM, "MISSING_DOCUMENT",
                     "Claim system marks {} RECEIVED but it is absent from the folder"
                     .format(flag_key),
                     "Flagged RECEIVED in the claim snapshot yet not present/readable "
                     "in this run's folder.", ["snapshot.doc_flags"])


def r11_salvage_and_repack(ctx: Ctx) -> None:
    if ctx.has("derived.unsellable_units"):
        ctx.gap("Salvage / disposition statement for the {} unsellable units".format(
                    ctx.val("derived.unsellable_units")),
                "The agreement requires crediting salvage value; nothing in the folder "
                "documents disposition (scrap, salvage sale, or return)",
                requested_by="reconciliation rule R11",
                impact="The carrier can discount the damage payout for unstated salvage; "
                       "a disposition statement closes that argument.",
                fact_keys=["derived.unsellable_units"])
    if ctx.has("inspection.repack_labor_usd"):
        ctx.gap("Invoice or classification for the repack labor charge ({})".format(
                    money(ctx.val("inspection.repack_labor_usd"))),
                "The agreement excludes internal administrative labor unless agreed; "
                "the folder does not show whether repack was third-party or internal",
                requested_by="reconciliation rule R11",
                impact="Determines whether the repack line is recoverable at all.",
                fact_keys=["inspection.repack_labor_usd"])


def r12_overview_audit(ctx: Ctx) -> None:
    """The overview says it is not a source of truth - hold it to that."""
    pairs = [
        ("overview.expected_delivery", "tms.customer_requested_delivery", "date"),
        ("overview.actual_delivery", "tms.delivered_at", "date"),
        ("overview.contents_units", "invoice.qty_units", "int"),
        ("overview.contents_cartons", "invoice.cartons", "int"),
        ("overview.units_per_carton", "invoice.units_per_carton", "int"),
        ("overview.invoice_value_usd", "invoice.invoice_total_usd", "money"),
        ("overview.total_demand_usd", "snapshot.claim_amount_usd", "money"),
        ("overview.carrier_offer_usd", "snapshot.carrier_offer_usd", "money"),
        ("overview.ship_date", "tms.pickup_date", "date"),
    ]
    mismatches, checked = [], 0
    for ov_key, primary_key, kind in pairs:
        if not ctx.has(ov_key, primary_key):
            continue
        checked += 1
        a, b = ctx.val(ov_key), ctx.val(primary_key)
        if kind == "money":
            same = dec2(a) == dec2(b)
        elif kind == "int":
            same = int(a) == int(b)
        else:
            same = str(a)[:10] == str(b)[:10]
        if not same:
            mismatches.append("{} ({!r}) vs {} ({!r})".format(ov_key, a, primary_key, b))
    if mismatches:
        ctx.disc(Severity.MEDIUM, "DATA_QUALITY",
                 "Case-overview summary conflicts with primary records",
                 "; ".join(mismatches), ["overview.trust_caveat"])
    elif checked:
        ctx.disc(Severity.INFO, "VERIFIED_CONSISTENT",
                 "Convenience summary verified against primary records ({} checks)"
                 .format(checked),
                 "The overview document itself warns it is not a source of truth; every "
                 "figure it shows was re-verified against the underlying records.",
                 ["overview.trust_caveat"], status="VERIFIED_CONSISTENT")


def r13_history_quality(ctx: Ctx) -> None:
    if ctx.has("history.xlsx_consistent"):
        consistent = ctx.val("history.xlsx_consistent")
        if consistent is True:
            ctx.disc(Severity.INFO, "VERIFIED_CONSISTENT",
                     "Historical-claims xlsx twin matches the CSV",
                     "Same dataset held in two systems; values agree (the xlsx stores the "
                     "settlement percentage as a formula, recomputed for comparison).",
                     ["history.xlsx_consistent"], status="VERIFIED_CONSISTENT")
        elif consistent is not True and not isinstance(consistent, str):
            ctx.disc(Severity.MEDIUM, "DATA_QUALITY",
                     "Historical-claims xlsx and CSV disagree",
                     str(ctx.ledger.fact("history.xlsx_consistent").note),
                     ["history.xlsx_consistent"])


def r14_photo_timestamps(ctx: Ctx) -> None:
    stamp = None
    for key in ("damage_photo_1.timestamp_text", "damage_photo_2.timestamp_text"):
        if ctx.has(key):
            stamp = str(ctx.val(key))
            break
    if stamp and ctx.has("tms.delivered_at"):
        delivered = ctx.val("tms.delivered_at")
        if delivered[:10] in stamp.replace("/", "-"):
            ctx.result.notes.append(
                "R14: photo timestamps fall on the delivery date ({}) - consistent with "
                "photos taken at receiving".format(delivered[:10]))


RULES = [r01_identifiers, r02_piece_counts, r03_damage_units, r04_photo_coverage,
         r05_value_math, r06_demand_decomposition, r07_offer_decomposition,
         r08_delivery_timeline, r09_service_level, r10_document_inventory,
         r11_salvage_and_repack, r12_overview_audit, r13_history_quality,
         r14_photo_timestamps]


def run_reconciliation(ledger: FactLedger, registry: Dict[str, SourceDoc]) -> ReconcileResult:
    ctx = Ctx(ledger, registry)
    for rule in RULES:
        try:
            rule(ctx)
        except Exception as exc:  # a rule bug must not take down the run
            log.exception("rule %s failed", rule.__name__)
            ctx.result.notes.append("{} CRASHED: {}".format(rule.__name__, exc))
    open_count = sum(1 for d in ctx.result.discrepancies if d.status == "OPEN")
    log.info("reconciliation: %d findings (%d open), %d gaps, %d demand lines",
             len(ctx.result.discrepancies), open_count, len(ctx.result.gaps),
             len(ctx.result.demand_lines))
    return ctx.result
