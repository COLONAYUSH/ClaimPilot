"""Stage 2 - fact extraction.

Structured sources (JSON/CSV/XLSX) never touch the LLM: their facts are read
natively with JSONPath/row locators and raw-line quotes. Unstructured text
documents go through schema-forced LLM extraction where every field carries a
verbatim quote. Image-only sources go through vision: a full transcript first
(which becomes the source's citable derived text), then fields quoted from
that transcript, then an independent second-pass verification of the key
fields against the image.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

from . import prompts
from .config import RunConfig
from .llm import LLMClient, LLMRequest
from .models import Citation, FactLedger, Kind, Method, SourceDoc
from .util import D, dec2

log = logging.getLogger("claimpilot.extract")


@dataclass
class ExtractionReport:
    skipped_sources: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    vision_verify: Dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------- helpers

def _json_line_quote(raw: str, json_key: str) -> str:
    m = re.search(r'^\s*("{}"\s*:.*?)\s*,?\s*$'.format(re.escape(json_key)),
                  raw, re.MULTILINE)
    return m.group(1) if m else ""


def _cit(source_id: str, locator: str, quote: str) -> Citation:
    return Citation(source_id=source_id, locator=locator, quote=quote)


def _as_value(value: Any, key: str) -> Any:
    if value is None:
        return None
    if key.endswith("_usd") and isinstance(value, (int, float, Decimal)):
        return dec2(value)
    return value


class FactWriter:
    """Small convenience wrapper so every add is uniform."""

    def __init__(self, ledger: FactLedger) -> None:
        self.ledger = ledger

    def det(self, key: str, value: Any, source_id: str, locator: str, quote: str,
            kind: str = Kind.EXTRACTED, note: str = "") -> None:
        if value is None:
            return
        self.ledger.add(key, _as_value(value, key), kind=kind, method=Method.DETERMINISTIC,
                        citations=[_cit(source_id, locator, quote)], note=note)

    def fv(self, key: str, node: Optional[Dict[str, Any]], source_id: str,
           locator: str = "document", kind: str = Kind.EXTRACTED,
           method: str = Method.LLM, confidence: float = 1.0) -> None:
        """Add a {value, quote} field extracted by the LLM."""
        if not isinstance(node, dict) or node.get("value") is None:
            return
        quote = node.get("quote") or ""
        note = node.get("note") or ""
        conf = confidence if quote else min(confidence, 0.5)
        if not quote:
            note = (note + " " if note else "") + "no supporting quote provided"
        self.ledger.add(key, _as_value(node["value"], key), kind=kind, method=method,
                        citations=[_cit(source_id, locator, quote)] if quote else [],
                        confidence=conf, note=note)


# ------------------------------------------------- deterministic extractors

def extract_snapshot(doc: SourceDoc, w: FactWriter) -> None:
    data, raw = doc.meta["data"], doc.text
    sid = doc.source_id

    def q(key: str) -> str:
        return _json_line_quote(raw, key)

    w.det("snapshot.claim_id", data.get("claim_id"), sid, "$.claim_id", q("claim_id"))
    w.det("snapshot.carrier", data.get("carrier"), sid, "$.carrier", q("carrier"))
    w.det("snapshot.pro_number", data.get("pro_number"), sid, "$.pro_number", q("pro_number"))
    w.det("snapshot.claim_types", data.get("claim_type_codes"), sid,
          "$.claim_type_codes", '"claim_type_codes"')
    w.det("snapshot.status", data.get("status"), sid, "$.status", q("status"))
    w.det("snapshot.owner", data.get("owner"), sid, "$.owner", q("owner"))
    w.det("snapshot.opened_at", data.get("opened_at"), sid, "$.opened_at", q("opened_at"))
    w.det("snapshot.claim_amount_usd", data.get("claim_amount_usd"), sid,
          "$.claim_amount_usd", q("claim_amount_usd"))
    w.det("snapshot.reserve_usd", data.get("reserve_usd"), sid, "$.reserve_usd", q("reserve_usd"))
    w.det("snapshot.carrier_offer_usd", data.get("carrier_offer_usd"), sid,
          "$.carrier_offer_usd", q("carrier_offer_usd"))
    w.det("snapshot.last_activity_at", data.get("last_activity_at"), sid,
          "$.last_activity_at", q("last_activity_at"))
    w.det("snapshot.doc_flags", data.get("required_document_flags"), sid,
          "$.required_document_flags", '"required_document_flags"')
    w.det("snapshot.analyst_note", data.get("analyst_note"), sid,
          "$.analyst_note", q("analyst_note"),
          kind=Kind.ASSERTED, note="internal analyst commentary, not an established fact")


def extract_tms(doc: SourceDoc, w: FactWriter) -> None:
    data, raw, sid = doc.meta["data"], doc.text, doc.source_id

    def q(key: str) -> str:
        return _json_line_quote(raw, key)

    w.det("tms.pro_number", data.get("pro_number"), sid, "$.pro_number", q("pro_number"))
    w.det("tms.bol_number", data.get("bol_number"), sid, "$.bol_number", q("bol_number"))
    svc = data.get("service", {})
    w.det("tms.service_code", svc.get("code"), sid, "$.service.code", q("code"))
    w.det("tms.service_name", svc.get("name"), sid, "$.service.name", q("name"))
    w.det("tms.service_guaranteed", svc.get("guaranteed"), sid,
          "$.service.guaranteed", q("guaranteed"),
          note="no guaranteed-service accessorial recorded in TMS")
    shp = data.get("shipment", {})
    w.det("tms.pickup_date", shp.get("pickup_date"), sid, "$.shipment.pickup_date",
          q("pickup_date"))
    w.det("tms.pieces_tendered", shp.get("pieces_tendered"), sid,
          "$.shipment.pieces_tendered", q("pieces_tendered"))
    w.det("tms.pallets", shp.get("pallets"), sid, "$.shipment.pallets", q("pallets"))
    w.det("tms.weight_lb", shp.get("weight_lb"), sid, "$.shipment.weight_lb", q("weight_lb"))
    w.det("tms.freight_class", shp.get("freight_class"), sid,
          "$.shipment.freight_class", q("freight_class"))
    w.det("tms.freight_charge_usd", shp.get("freight_charge_usd"), sid,
          "$.shipment.freight_charge_usd", q("freight_charge_usd"))
    if "declared_value_usd" in shp:
        w.det("tms.declared_value_usd", "null (none declared)" if shp["declared_value_usd"]
              is None else shp["declared_value_usd"], sid,
              "$.shipment.declared_value_usd", q("declared_value_usd"))
    w.det("tms.customer_requested_delivery", data.get("customer_requested_delivery"), sid,
          "$.customer_requested_delivery", q("customer_requested_delivery"))
    w.det("tms.carrier_estimated_delivery", data.get("carrier_estimated_delivery"), sid,
          "$.carrier_estimated_delivery", q("carrier_estimated_delivery"))
    events = data.get("events", [])
    for i, ev in enumerate(events):
        if ev.get("code") == "DELIVERED":
            w.det("tms.delivered_at", ev.get("timestamp"), sid,
                  "$.events[{}].timestamp".format(i), str(ev.get("timestamp")))
            w.det("tms.edi_delivered_pieces", ev.get("pieces"), sid,
                  "$.events[{}].pieces".format(i), '"pieces": {}'.format(ev.get("pieces")),
                  note="carrier-reported EDI 214 count; not a consignee-signed receiving record")
    exceptions = [{"timestamp": e.get("timestamp"), "detail": e.get("detail")}
                  for e in events if e.get("code") == "EXCEPTION"]
    if exceptions:
        w.det("tms.exception_events", exceptions, sid, "$.events[*]", '"code": "EXCEPTION"')
    w.det("tms.exception_notes", data.get("exception_notes"), sid,
          "$.exception_notes", '"exception_notes"')


def extract_erp(doc: SourceDoc, w: FactWriter) -> None:
    rows = doc.meta.get("rows", [])
    if not rows:
        return
    row, sid = rows[0], doc.source_id
    line_quote = doc.segments[0].text if doc.segments else ""

    def add(key: str, col: str, cast=None) -> None:
        value = row.get(col)
        if value in (None, ""):
            return
        if cast is not None:
            value = cast(value)
        w.det("erp.{}".format(key), value, sid, "row:1", line_quote)

    add("sales_order", "sales_order")
    add("invoice", "invoice")
    add("customer", "customer")
    add("customer_po", "customer_po")
    add("sku", "sku")
    add("qty_ordered", "qty_ordered", int)
    add("qty_shipped", "qty_shipped", int)
    add("unit_price_usd", "unit_price_usd", dec2)
    add("extended_value_usd", "extended_value_usd", dec2)
    add("ship_date", "ship_date")
    add("promotion_launch_date", "promotion_launch_date")
    add("promotion_end_date", "promotion_end_date")
    add("incoterm", "incoterm")


def crosscheck_history_xlsx(csv_doc: SourceDoc, xlsx_doc: SourceDoc, w: FactWriter) -> None:
    """Same dataset, two systems: verify the xlsx twin agrees with the CSV.
    (The xlsx stores settlement_pct as formulas; data_only reads cached
    values, so numeric comparison uses a small tolerance.)"""
    csv_rows = csv_doc.meta.get("rows", [])
    w.det("history.row_count", len(csv_rows), csv_doc.source_id, "rows", "",
          kind=Kind.DERIVED)
    xlsx_rows = xlsx_doc.meta.get("rows") if xlsx_doc.status == "OK" else None
    if not xlsx_rows:
        w.det("history.xlsx_consistent", "not checked ({})".format(xlsx_doc.status),
              csv_doc.source_id, "rows", "", kind=Kind.DERIVED)
        return
    mismatches: List[str] = []
    if len(xlsx_rows) != len(csv_rows):
        mismatches.append("row count {} vs {}".format(len(xlsx_rows), len(csv_rows)))
    for i, (a, b) in enumerate(zip(csv_rows, xlsx_rows), start=1):
        for col, cv in a.items():
            xv = b.get(col)
            try:
                if abs(float(cv) - float(xv)) > 0.005:
                    mismatches.append("row {} col {}: {} vs {}".format(i, col, cv, xv))
            except (TypeError, ValueError):
                if str(cv).strip() != str(xv).strip():
                    mismatches.append("row {} col {}: {!r} vs {!r}".format(i, col, cv, xv))
    w.det("history.xlsx_consistent", not mismatches, csv_doc.source_id, "rows",
          "", kind=Kind.DERIVED,
          note="; ".join(mismatches[:5]) if mismatches else
          "xlsx twin matches CSV (formula column recomputed)")


_DD_ANCHORS = [
    ("dd.pod_authority", "signed POD is the consignee's documented receiving exception"),
    ("dd.edi_semantics", "final EDI event is not a consignee-signed receiving record"),
    ("dd.claim_amount_semantics", "shipper's demand, not an adjudicated recoverable amount"),
    ("dd.service_guaranteed_semantics", "no guaranteed-service accessorial is recorded"),
    ("dd.settlement_pct_semantics", "should not be treated as a contractual entitlement"),
]


def extract_data_dictionary(doc: SourceDoc, w: FactWriter) -> None:
    """Semantics the pack's data dictionary defines - captured as citable
    facts so authority rulings (signed POD over EDI, etc.) carry quotes."""
    for key, anchor in _DD_ANCHORS:
        for line in doc.text.splitlines():
            if anchor in line:
                w.det(key, line.strip("- ").strip(), doc.source_id, "document",
                      line.strip())
                break


# --------------------------------------------------------- LLM doc extractors

def _run_doc_extraction(client: LLMClient, doc: SourceDoc, task: str,
                        schema: Dict[str, Any]) -> Dict[str, Any]:
    req = LLMRequest(
        prompt=prompts.extraction_prompt(doc.filename, doc.text, task, schema),
        system=prompts.SYSTEM_EXTRACT, schema=schema, label="extract/{}".format(doc.source_id))
    return client.call(req).obj or {}


def extract_invoice(doc: SourceDoc, w: FactWriter, client: LLMClient) -> None:
    obj = _run_doc_extraction(client, doc, prompts.INVOICE_TASK, prompts.INVOICE_SCHEMA)
    sid = doc.source_id
    for key in ["invoice_no", "sales_order", "seller", "bill_to", "invoice_date", "qty_units",
                "unit_price_usd", "extended_value_usd", "invoice_total_usd", "cartons",
                "units_per_carton", "unit_weight_lb", "freight_note"]:
        w.fv("invoice.{}".format(key), obj.get(key), sid, "page:1")


def extract_bol(doc: SourceDoc, w: FactWriter, client: LLMClient) -> None:
    obj = _run_doc_extraction(client, doc, prompts.BOL_TASK, prompts.BOL_SCHEMA)
    sid = doc.source_id
    for key in ["bol_number", "pro_number", "shipper", "consignee", "pickup_date", "carrier",
                "service_description", "pallets", "cartons", "weight_lb", "freight_class",
                "declared_value_note", "requested_delivery", "promotion_note", "guarantee_note",
                "packaging_note", "shipper_signature", "carrier_signature"]:
        w.fv("bol.{}".format(key), obj.get(key), sid, "page:1")


def extract_pod(doc: SourceDoc, w: FactWriter, client: LLMClient) -> None:
    obj = _run_doc_extraction(client, doc, prompts.POD_TASK, prompts.POD_SCHEMA)
    sid = doc.source_id
    for key in ["pro_number", "bol_number", "consignee", "carrier", "delivered_at",
                "tendered_cartons", "received_cartons", "short_cartons", "damaged_cartons",
                "exception_text", "consignee_signature", "signed_at", "driver_note"]:
        w.fv("pod.{}".format(key), obj.get(key), sid, "page:1")


def extract_overview(doc: SourceDoc, w: FactWriter, client: LLMClient) -> None:
    obj = _run_doc_extraction(client, doc, prompts.OVERVIEW_TASK, prompts.OVERVIEW_SCHEMA)
    sid = doc.source_id
    for key in ["claim_id", "claimant", "respondent_carrier", "service", "ship_date",
                "expected_delivery", "actual_delivery", "contents_units", "contents_cartons",
                "units_per_carton", "invoice_value_usd", "total_demand_usd",
                "carrier_offer_usd", "trust_caveat"]:
        w.fv("overview.{}".format(key), obj.get(key), sid, "page:1")
    lines = obj.get("demand_lines") or []
    if lines:
        cits = [_cit(sid, "page:1", ln.get("quote", "")) for ln in lines if ln.get("quote")]
        w.ledger.add("overview.demand_lines",
                     [{"label": ln.get("label"), "basis": ln.get("basis"),
                       "amount_usd": dec2(ln.get("amount_usd", 0))} for ln in lines],
                     kind=Kind.EXTRACTED, method=Method.LLM, citations=cits,
                     note="claimant-authored summary; each line re-verified deterministically")


def extract_email(doc: SourceDoc, w: FactWriter, client: LLMClient) -> None:
    annotated = []
    for i, m in enumerate(doc.meta.get("messages", []), start=1):
        annotated.append("[MESSAGE {}] role={} sent={} from={}\n{}".format(
            i, m["role"], m["sent_at"], m["headers"].get("from", "?"), m["body"]))
    text = "\n\n".join(annotated)
    schema = prompts.EMAIL_SCHEMA
    req = LLMRequest(prompt=prompts.extraction_prompt(doc.filename, text,
                                                      prompts.EMAIL_TASK, schema),
                     system=prompts.SYSTEM_EXTRACT, schema=schema,
                     label="extract/{}".format(doc.source_id), max_tokens=12000)
    obj = client.call(req).obj or {}
    sid = doc.source_id

    def loc(node: Any, default: str = "document") -> str:
        if isinstance(node, dict) and isinstance(node.get("message_index"), int):
            return "message:{}".format(node["message_index"])
        return default

    demand = obj.get("demand") or {}
    w.fv("email.initial_demand_usd", demand.get("initial_total_usd"), sid, "message:1")
    comps = demand.get("components_mentioned") or []
    if comps:
        w.ledger.add("email.demand_components_mentioned",
                     [{"label": c.get("label"), "amount_usd": c.get("amount_usd")}
                      for c in comps],
                     kind=Kind.ASSERTED, method=Method.LLM,
                     citations=[_cit(sid, loc(c), c.get("quote", "")) for c in comps],
                     note="components as described in correspondence")
    offer = obj.get("carrier_offer") or {}
    offer_loc = loc(offer, "message:6")
    w.fv("email.offer_total_usd", offer.get("total_usd"), sid, offer_loc)
    oc = offer.get("components") or []
    if oc:
        w.ledger.add("email.offer_components",
                     [{"label": c.get("label"), "amount_usd": dec2(c.get("amount_usd", 0))}
                      for c in oc],
                     kind=Kind.EXTRACTED, method=Method.LLM,
                     citations=[_cit(sid, offer_loc, c.get("quote", "")) for c in oc])
    w.fv("email.offer_accepted_missing_units", offer.get("accepted_missing_units"), sid, offer_loc)
    w.fv("email.offer_accepted_damaged_units", offer.get("accepted_damaged_units"), sid, offer_loc)
    w.fv("email.offer_disputed_damaged_units", offer.get("disputed_damaged_units"), sid, offer_loc)
    reasons = offer.get("dispute_reasons") or []
    if reasons:
        w.ledger.add("email.offer_dispute_reasons", [r.get("reason") for r in reasons],
                     kind=Kind.EXTRACTED, method=Method.LLM,
                     citations=[_cit(sid, offer_loc, r.get("quote", "")) for r in reasons])
    excluded = offer.get("explicitly_excluded") or []
    if excluded:
        w.ledger.add("email.offer_exclusions", [e.get("item") for e in excluded],
                     kind=Kind.EXTRACTED, method=Method.LLM,
                     citations=[_cit(sid, offer_loc, e.get("quote", "")) for e in excluded])
    requests = obj.get("document_requests") or []
    if requests:
        w.ledger.add("email.document_requests",
                     [{"item": r.get("item"), "requested_by": r.get("requested_by"),
                       "message": r.get("message_index"),
                       "status": r.get("status_per_thread")} for r in requests],
                     kind=Kind.EXTRACTED, method=Method.LLM,
                     citations=[_cit(sid, loc(r), r.get("quote", "")) for r in requests])
    for a in obj.get("key_assertions") or []:
        topic = re.sub(r"\W+", "_", str(a.get("topic", ""))).strip("_").lower()[:48]
        if not topic:
            continue
        w.ledger.add("email.assert.{}".format(topic),
                     a.get("value") if a.get("value") is not None else a.get("quote", ""),
                     kind=Kind.ASSERTED, method=Method.LLM,
                     citations=[_cit(sid, loc(a), a.get("quote", ""))],
                     note="asserted by {} in correspondence".format(a.get("by")))
    state = obj.get("thread_state") or {}
    if state:
        w.ledger.add("email.thread_state", state, kind=Kind.DERIVED, method=Method.LLM,
                     citations=[], note="negotiation state as read from the thread")


# ------------------------------------------------------------ vision sources

_INSPECTION_FIELDS = [
    "report_no", "claim_id", "pro_number", "bol_number", "inspection_date", "location",
    "inspector", "total_examined", "total_unsellable", "total_repackable",
    "cartons_received_note", "foam_present", "no_damage_in_repackable",
    "photos_provided_note", "inspection_fee_usd", "repack_labor_usd", "conclusion",
    "packaging_spec_note", "signed_by", "signed_date",
]


def extract_inspection(doc: SourceDoc, w: FactWriter, client: LLMClient,
                       cfg: RunConfig, report: ExtractionReport) -> None:
    import json as _json
    schema = prompts.INSPECTION_VISION_SCHEMA
    req = LLMRequest(
        prompt="{}\n\nReturn ONLY a JSON object matching this schema:\n{}".format(
            prompts.INSPECTION_VISION_TASK, _json.dumps(schema, indent=1)),
        system=prompts.SYSTEM_EXTRACT, attachments=[doc.path], schema=schema,
        label="vision/{}".format(doc.source_id), max_tokens=12000)
    obj = client.call(req).obj or {}
    transcript = obj.get("transcript") or ""
    doc.derived_text = transcript
    doc.status = "OCR_DERIVED"
    legibility = float(obj.get("legibility") or 0.8)
    conf = max(0.5, min(1.0, legibility))
    sid = doc.source_id
    for key in _INSPECTION_FIELDS:
        w.fv("inspection.{}".format(key), obj.get(key), sid, "transcript",
             method=Method.LLM_VISION, confidence=conf)
    rows = obj.get("carton_rows") or []
    if rows:
        w.ledger.add("inspection.carton_rows", rows, kind=Kind.EXTRACTED,
                     method=Method.LLM_VISION, confidence=conf,
                     citations=[_cit(sid, "transcript", "Unit disposition")],
                     note="per-carton disposition table transcribed from scan")
    w.ledger.add("inspection.legibility", legibility, kind=Kind.DERIVED,
                 method=Method.LLM_VISION, citations=[],
                 note="model-reported read confidence for the scan")

    if cfg.vision_verify:
        expected = {"report_no": obj.get("report_no", {}).get("value"),
                    "inspection_date": obj.get("inspection_date", {}).get("value"),
                    "total_examined": obj.get("total_examined", {}).get("value"),
                    "total_unsellable": obj.get("total_unsellable", {}).get("value"),
                    "total_repackable": obj.get("total_repackable", {}).get("value"),
                    "inspection_fee_usd": obj.get("inspection_fee_usd", {}).get("value"),
                    "repack_labor_usd": obj.get("repack_labor_usd", {}).get("value"),
                    "carton_ids": sorted({r.get("carton_id", "") for r in rows})}
        vreq = LLMRequest(
            prompt="{}\n\nExpected values:\n{}\n\nReturn ONLY JSON matching:\n{}".format(
                prompts.VISION_VERIFY_TASK, _json.dumps(expected, indent=1),
                _json.dumps(prompts.VISION_VERIFY_SCHEMA, indent=1)),
            attachments=[doc.path], schema=prompts.VISION_VERIFY_SCHEMA,
            label="vision-verify/{}".format(doc.source_id))
        vobj = client.call(vreq).obj or {}
        checks = vobj.get("checks") or []
        mismatched = [c for c in checks if not c.get("matches")]
        report.vision_verify[sid] = {"checks": len(checks), "mismatches": mismatched}
        doc.meta["vision_verify"] = report.vision_verify[sid]
        for c in mismatched:
            key = "inspection.{}".format(c.get("field"))
            if w.ledger.has(key):
                fact = w.ledger.fact(key)
                fact.confidence = min(fact.confidence, 0.4)
                fact.note = (fact.note + " " if fact.note else "") + \
                    "vision verify disagreed: {!r}".format(c.get("actual_if_different"))


def extract_photo(doc: SourceDoc, w: FactWriter, client: LLMClient) -> None:
    import json as _json
    schema = prompts.PHOTO_VISION_SCHEMA
    req = LLMRequest(
        prompt="{}\n\nReturn ONLY a JSON object matching this schema:\n{}".format(
            prompts.PHOTO_VISION_TASK, _json.dumps(schema, indent=1)),
        system=prompts.SYSTEM_EXTRACT, attachments=[doc.path], schema=schema,
        label="vision/{}".format(doc.source_id))
    obj = client.call(req).obj or {}
    parts = []
    for key in ["header_text", "footer_text", "timestamp_text", "location_text"]:
        node = obj.get(key) or {}
        if node.get("value"):
            parts.append("{}: {}".format(key, node["value"]))
    labels = obj.get("carton_labels_visible") or []
    damaged = obj.get("cartons_shown_damaged") or []
    observations = obj.get("damage_observations") or []
    parts.append("carton labels visible: {}".format(", ".join(labels) or "none"))
    parts.append("cartons shown damaged: {}".format(", ".join(damaged) or "none"))
    parts.append("damage observations: {}".format("; ".join(observations) or "none"))
    if obj.get("description"):
        parts.append("description: {}".format(obj["description"]))
    doc.derived_text = "\n".join(parts)
    doc.status = "OCR_DERIVED"
    sid = doc.source_id
    for key in ["header_text", "footer_text", "timestamp_text"]:
        w.fv("{}.{}".format(sid, key), obj.get(key), sid, "image",
             method=Method.LLM_VISION, confidence=0.9)
    w.ledger.add("{}.carton_labels".format(sid), sorted(set(labels)),
                 kind=Kind.EXTRACTED, method=Method.LLM_VISION, confidence=0.9,
                 citations=[_cit(sid, "image", "carton labels visible: {}".format(
                     ", ".join(labels) or "none"))])
    w.ledger.add("{}.cartons_shown_damaged".format(sid), sorted(set(damaged)),
                 kind=Kind.EXTRACTED, method=Method.LLM_VISION, confidence=0.9,
                 citations=[_cit(sid, "image", "cartons shown damaged: {}".format(
                     ", ".join(damaged) or "none"))],
                 note="cartons whose damage this photo documents; intact cartons "
                      "visible in frame are excluded")
    w.ledger.add("{}.damage_observations".format(sid), observations,
                 kind=Kind.EXTRACTED, method=Method.LLM_VISION, confidence=0.85,
                 citations=[_cit(sid, "image", "damage observations: {}".format(
                     "; ".join(observations) or "none"))])


# --------------------------------------------------------------- entry point

def run_extraction(registry: Dict[str, SourceDoc], ledger: FactLedger,
                   client: LLMClient, cfg: RunConfig) -> ExtractionReport:
    report = ExtractionReport()
    w = FactWriter(ledger)

    deterministic = {
        "claim_snapshot": extract_snapshot,
        "tms_shipment": extract_tms,
        "erp_order_invoice": extract_erp,
        "data_dictionary": extract_data_dictionary,
    }
    llm_docs = {
        "commercial_invoice": extract_invoice,
        "bill_of_lading": extract_bol,
        "proof_of_delivery": extract_pod,
        "case_overview": extract_overview,
        "email_thread": extract_email,
    }

    for sid, fn in deterministic.items():
        doc = registry[sid]
        if doc.status != "OK":
            report.skipped_sources.append(sid)
            continue
        fn(doc, w)

    crosscheck_history_xlsx(registry["historical_claims"], registry["historical_claims_xlsx"], w)

    for sid, fn in llm_docs.items():
        doc = registry[sid]
        if doc.status != "OK":
            report.skipped_sources.append(sid)
            continue
        fn(doc, w, client)

    if registry["inspection_report"].status in ("OK", "OCR_DERIVED"):
        extract_inspection(registry["inspection_report"], w, client, cfg, report)
    else:
        report.skipped_sources.append("inspection_report")

    for sid in ("damage_photo_1", "damage_photo_2"):
        if registry[sid].status in ("OK", "OCR_DERIVED"):
            extract_photo(registry[sid], w, client)
        else:
            report.skipped_sources.append(sid)

    if report.skipped_sources:
        report.notes.append("sources unavailable this run: {}".format(
            ", ".join(report.skipped_sources)))
        log.warning("extraction skipped unavailable sources: %s", report.skipped_sources)
    log.info("extraction complete: %d facts", len(ledger))
    return report
