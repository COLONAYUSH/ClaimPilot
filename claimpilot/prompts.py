"""Every prompt and JSON schema, versioned in one place.

Design rules the prompts enforce:
  - every extracted field is a {value, quote} pair; the quote must be verbatim
    (the grounding gate re-checks it mechanically against the source),
  - absence is null, never a guess,
  - vision sources produce a full transcript first; field quotes then cite the
    transcript, which is stored as OCR-derived text with its own caveat.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

SYSTEM_EXTRACT = """You are the information-extraction engine inside a freight-claims copilot.
Non-negotiable rules:
1. Output ONLY one JSON object matching the schema you were given. No prose, no markdown fences.
2. Every extracted value must be supported by a VERBATIM quote copied character-for-character from the source text (>= 4 consecutive words where available; keep original casing and punctuation).
3. If the source does not state something, use null. Never guess, infer across documents, or use outside knowledge.
4. In "value" fields: numbers are plain JSON numbers (no $ or thousands separators); dates are ISO 8601. The "quote" keeps the source's original wording.
5. If a value is approximate or ambiguous in the source, extract it and explain in the nearest "note" field."""


def fv(*types: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Schema fragment for one grounded field: {value, quote}."""
    node: Dict[str, Any] = {
        "type": "object",
        "required": ["value", "quote"],
        "properties": {
            "value": {"type": list(types) + ["null"]},
            "quote": {"type": ["string", "null"]},
        },
    }
    if extra:
        node["properties"].update(extra)
    return node


def obj(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "object", "required": required or list(properties.keys()),
            "properties": properties}


def arr(items: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "array", "items": items}


def extraction_prompt(title: str, source_text: str, task: str, schema: Dict[str, Any]) -> str:
    import json
    return (
        "SOURCE DOCUMENT: {title}\n"
        "--- BEGIN SOURCE TEXT ---\n{text}\n--- END SOURCE TEXT ---\n\n"
        "TASK: {task}\n\n"
        "Return ONLY a JSON object matching this schema (quotes verbatim from the source text):\n"
        "{schema}"
    ).format(title=title, text=source_text, task=task,
             schema=json.dumps(schema, indent=1))


# ------------------------------------------------------------------ invoice

INVOICE_SCHEMA = obj({
    "invoice_no": fv("string"),
    "sales_order": fv("string"),
    "seller": fv("string"),
    "bill_to": fv("string"),
    "invoice_date": fv("string"),
    "qty_units": fv("integer"),
    "unit_price_usd": fv("number"),
    "extended_value_usd": fv("number"),
    "invoice_total_usd": fv("number"),
    "cartons": fv("integer"),
    "units_per_carton": fv("integer"),
    "unit_weight_lb": fv("number", extra={"note": {"type": ["string", "null"]}}),
    "freight_note": fv("string"),
})

INVOICE_TASK = ("Extract the commercial invoice fields, including the packing note "
                "(cartons, units per carton) and the approximate unit weight.")

# ---------------------------------------------------------------------- BOL

BOL_SCHEMA = obj({
    "bol_number": fv("string"),
    "pro_number": fv("string"),
    "shipper": fv("string"),
    "consignee": fv("string"),
    "pickup_date": fv("string"),
    "carrier": fv("string"),
    "service_description": fv("string"),
    "pallets": fv("integer"),
    "cartons": fv("integer"),
    "weight_lb": fv("number"),
    "freight_class": fv("string"),
    "declared_value_note": fv("string"),
    "requested_delivery": fv("string"),
    "promotion_note": fv("string"),
    "guarantee_note": fv("string"),
    "packaging_note": fv("string"),
    "shipper_signature": fv("string"),
    "carrier_signature": fv("string"),
})

BOL_TASK = ("Extract the bill of lading fields. Capture exactly what it says about "
            "declared value, the requested delivery date, and whether the requested "
            "date is a guaranteed-service accessorial.")

# ---------------------------------------------------------------------- POD

POD_SCHEMA = obj({
    "pro_number": fv("string"),
    "bol_number": fv("string"),
    "consignee": fv("string"),
    "carrier": fv("string"),
    "delivered_at": fv("string"),
    "tendered_cartons": fv("integer"),
    "received_cartons": fv("integer"),
    "short_cartons": fv("integer"),
    "damaged_cartons": fv("integer"),
    "exception_text": fv("string"),
    "consignee_signature": fv("string"),
    "signed_at": fv("string"),
    "driver_note": fv("string"),
})

POD_TASK = ("Extract the proof-of-delivery fields, especially the consignee's "
            "written exceptions and the counts (tendered/received/short/damaged).")

# ----------------------------------------------------------------- overview

OVERVIEW_SCHEMA = obj({
    "claim_id": fv("string"),
    "claimant": fv("string"),
    "respondent_carrier": fv("string"),
    "service": fv("string"),
    "ship_date": fv("string"),
    "expected_delivery": fv("string"),
    "actual_delivery": fv("string"),
    "contents_units": fv("integer"),
    "contents_cartons": fv("integer"),
    "units_per_carton": fv("integer"),
    "invoice_value_usd": fv("number"),
    "demand_lines": arr(obj({
        "label": {"type": "string"},
        "basis": {"type": ["string", "null"]},
        "amount_usd": {"type": "number"},
        "quote": {"type": "string"},
    })),
    "total_demand_usd": fv("number"),
    "carrier_offer_usd": fv("number"),
    "trust_caveat": fv("string"),
})

OVERVIEW_TASK = ("Extract the case-overview fields including every claimed-loss line "
                 "(label, basis, amount) and the document's own caveat about whether "
                 "it is a source of truth.")

# -------------------------------------------------------------------- email

EMAIL_SCHEMA = obj({
    "messages": arr(obj({
        "index": {"type": "integer"},
        "role": {"type": "string", "enum": ["shipper", "carrier"]},
        "sent_at": {"type": ["string", "null"]},
        "summary": {"type": "string"},
    })),
    "demand": obj({
        "initial_total_usd": fv("number"),
        "components_mentioned": arr(obj({
            "label": {"type": "string"},
            "amount_usd": {"type": ["number", "null"]},
            "message_index": {"type": "integer"},
            "quote": {"type": "string"},
        })),
    }),
    "carrier_offer": obj({
        "total_usd": fv("number"),
        "components": arr(obj({
            "label": {"type": "string"},
            "amount_usd": {"type": "number"},
            "quote": {"type": "string"},
        })),
        "accepted_missing_units": fv("integer"),
        "accepted_damaged_units": fv("integer"),
        "disputed_damaged_units": fv("integer"),
        "dispute_reasons": arr(obj({
            "reason": {"type": "string"},
            "quote": {"type": "string"},
        })),
        "explicitly_excluded": arr(obj({
            "item": {"type": "string"},
            "quote": {"type": "string"},
        })),
        "message_index": {"type": "integer"},
    }),
    "document_requests": arr(obj({
        "item": {"type": "string"},
        "requested_by": {"type": "string", "enum": ["shipper", "carrier"]},
        "message_index": {"type": "integer"},
        "status_per_thread": {"type": ["string", "null"]},
        "quote": {"type": "string"},
    })),
    "key_assertions": arr(obj({
        "topic": {"type": "string"},
        "by": {"type": "string", "enum": ["shipper", "carrier"]},
        "message_index": {"type": "integer"},
        "value": {"type": ["string", "number", "null"]},
        "quote": {"type": "string"},
    })),
    "thread_state": obj({
        "last_message_index": {"type": "integer"},
        "last_message_role": {"type": "string"},
        "open_points": {"type": "array", "items": {"type": "string"}},
    }),
})

EMAIL_TASK = """Extract the negotiation state from this claim email thread.
Cover at minimum these topics in key_assertions (one entry each, where stated):
arrival date vs expected date; missing cartons; damaged cartons; units per carton;
invoice units and unit price; direct shortage/damage subtotal; EDI piece count vs
POD carton count; inspection findings (units unsellable / repackable); internal
foam presence; packaging specification availability; late-delivery markdown amount
and the promotion it relates to; freight charge refund request; the carrier's
service-level position. Record every document request and whether the thread shows
it fulfilled. Quote message text verbatim; message_index refers to the numbered
[MESSAGE n] markers."""

# ---------------------------------------------------------- contract terms

CONTRACT_TERMS_SCHEMA = obj({
    "liability_rule": obj({
        "cap_per_lb_usd": {"type": ["number", "null"]},
        "basis_description": {"type": "string"},
        "applies_to": {"type": "string"},
        "section": {"type": "string"},
        "quote": {"type": "string"},
    }),
    "delay_exclusions": obj({
        "consequential_excluded": {"type": "boolean"},
        "examples_listed": {"type": "array", "items": {"type": "string"}},
        "section": {"type": "string"},
        "quote": {"type": "string"},
    }),
    "guaranteed_service": obj({
        "requires_written_purchase": {"type": "boolean"},
        "delay_liability_cap_description": {"type": "string"},
        "section": {"type": "string"},
        "quote": {"type": "string"},
    }),
    "claim_notice": obj({
        "cargo_claim_months": {"type": ["number", "null"]},
        "delay_claim_days": {"type": ["number", "null"]},
        "section": {"type": "string"},
        "quote": {"type": "string"},
    }),
    "packaging": obj({
        "shipper_responsible": {"type": "boolean"},
        "carrier_relief_if_insufficient": {"type": "boolean"},
        "section": {"type": "string"},
        "quote": {"type": "string"},
    }),
    "salvage_mitigation": obj({
        "salvage_credit_required": {"type": "boolean"},
        "section": {"type": "string"},
        "quote": {"type": "string"},
    }),
    "inspection_costs": obj({
        "third_party_may_be_considered": {"type": "boolean"},
        "internal_labor_reimbursable": {"type": "boolean"},
        "condition": {"type": ["string", "null"]},
        "section": {"type": "string"},
        "quote": {"type": "string"},
    }),
    "commercial_compromise": obj({
        "allowed": {"type": "boolean"},
        "non_precedential": {"type": "boolean"},
        "section": {"type": "string"},
        "quote": {"type": "string"},
    }),
    "documentation_required": obj({
        "items": {"type": "array", "items": {"type": "string"}},
        "section": {"type": "string"},
        "quote": {"type": "string"},
    }),
})

CONTRACT_TERMS_TASK = """From the retrieved agreement clauses below, extract the contract
parameters. Quote each clause verbatim (the exact governing sentence or phrase).
Use the section headings as shown (e.g. "2. Cargo Loss and Damage Liability").
If a parameter is not addressed in the provided clauses, use null/false and quote
nothing rather than inventing.
Parameter definitions - read carefully:
- guaranteed_service.requires_written_purchase: does the delay/service-refund right
  exist ONLY when a written guaranteed/appointment service was purchased? If the
  clauses describe such a purchased service and tie the refund to it, this is true;
  quote that clause and describe its liability cap in
  delay_liability_cap_description.
- inspection_costs.third_party_may_be_considered: may reasonable third-party
  inspection costs be considered or reimbursed under ANY stated circumstances, even
  conditionally (e.g. "may be considered when requested or reasonably necessary")?
  If yes: true, quote that sentence, and put the proviso in "condition".
- inspection_costs.internal_labor_reimbursable: is internal/administrative labor
  reimbursable WITHOUT a separate written agreement?
- All other booleans read the same way: does the agreement permit/require it at all,
  even conditionally?
Quote the complete sentence (or complete phrase) that grants or denies each
parameter - never cut a word in half. Use null/"" only when the provided clauses
genuinely do not touch the topic."""

# --------------------------------------------------------- vision: inspection

INSPECTION_VISION_SCHEMA = obj({
    "transcript": {"type": "string"},
    "report_no": fv("string"),
    "claim_id": fv("string"),
    "pro_number": fv("string"),
    "bol_number": fv("string"),
    "inspection_date": fv("string"),
    "location": fv("string"),
    "inspector": fv("string"),
    "carton_rows": arr(obj({
        "carton_id": {"type": "string"},
        "units": {"type": "integer"},
        "unsellable": {"type": "integer"},
        "repackable": {"type": "integer"},
        "observation": {"type": "string"},
    })),
    "total_examined": fv("integer"),
    "total_unsellable": fv("integer"),
    "total_repackable": fv("integer"),
    "cartons_received_note": fv("string"),
    "foam_present": fv("boolean"),
    "no_damage_in_repackable": fv("boolean"),
    "photos_provided_note": fv("string"),
    "inspection_fee_usd": fv("number"),
    "repack_labor_usd": fv("number"),
    "conclusion": fv("string"),
    "packaging_spec_note": fv("string"),
    "signed_by": fv("string"),
    "signed_date": fv("string"),
    "legibility": {"type": "number"},
})

INSPECTION_VISION_TASK = """This is a scanned (image-only) independent cargo inspection report.
1. First produce "transcript": a complete, faithful, line-by-line transcription of ALL
   text visible in the document, preserving the table structure as readable lines.
   Transcribe exactly what is written; mark anything unreadable as [illegible].
2. Then extract the fields. Every "quote" must be copied verbatim FROM YOUR TRANSCRIPT.
3. Set "legibility" between 0 and 1 for how confidently the scan could be read overall."""

# -------------------------------------------------------------- vision: photo

PHOTO_VISION_SCHEMA = obj({
    "header_text": fv("string"),
    "footer_text": fv("string"),
    "timestamp_text": fv("string"),
    "location_text": fv("string"),
    "carton_labels_visible": {"type": "array", "items": {"type": "string"}},
    "cartons_shown_damaged": {"type": "array", "items": {"type": "string"}},
    "damage_observations": {"type": "array", "items": {"type": "string"}},
    "synthetic_watermark": fv("boolean"),
    "description": {"type": "string"},
})

PHOTO_VISION_TASK = """This is a warehouse evidence photo from a freight damage claim.
Report exactly what is visible: any header/footer/caption text (verbatim), any carton
identifiers, and the visible damage indicators (tears, punctures, crush, stains).
carton_labels_visible: every carton id readable anywhere in the image (once per
distinct physical carton). cartons_shown_damaged: ONLY the carton ids whose cartons
visibly exhibit damage in this photo - a carton that merely appears intact in the
background does not count as documented damage. Do not speculate beyond the image."""

# ------------------------------------------------------------- vision verify

VISION_VERIFY_SCHEMA = obj({
    "checks": arr(obj({
        "field": {"type": "string"},
        "expected": {"type": ["string", "number", "boolean", "array", "null"]},
        "matches": {"type": "boolean"},
        "actual_if_different": {"type": ["string", "number", "boolean", "array", "null"]},
    })),
})

VISION_VERIFY_TASK = """Re-read the attached document image carefully. For each expected
field value below, confirm whether the document actually shows that value.
Mark matches=false ONLY if the document clearly shows something different, and then
report what it shows. Judge from the image alone."""

# ----------------------------------------------------------------- position

POSITION_SCHEMA = obj({
    "executive_summary": {"type": "string"},
    "negotiation_analysis": {"type": "array", "items": {"type": "string"}},
    "recommended_next_steps": arr(obj({
        "action": {"type": "string"},
        "rationale": {"type": "string"},
    })),
    "risks_and_watchouts": {"type": "array", "items": {"type": "string"}},
    "draft_reply": obj({
        "subject": {"type": "string"},
        "body": {"type": "string"},
    }),
})

POSITION_SYSTEM = """You are the drafting engine of a freight-claims copilot, writing for a
senior claims specialist. You will receive a structured case file: verified facts (each
with an id like F-012), detected discrepancies, evidence gaps, a contract-entitlement
table computed deterministically by the system, and historical comparables.

Hard rules:
1. Use ONLY the numbers, dates and counts present in the case file. Do not compute new
   figures, do not round differently, do not introduce outside knowledge.
2. Every sentence that states a fact or figure must end with its fact reference(s) in
   square brackets, e.g. "The signed POD records 58 cartons received [F-014]." Use entitlement
   ids [E-3], discrepancy ids [D-2], term ids [CT-4] and comparable claim ids (HC-...) the same way.
3. Keep the three registers separate and never blur them: facts (cited), the system's
   computed entitlement positions (cite the E-ids), and your recommended negotiation
   judgment (label it as recommendation).
4. The draft reply is a professional email from Maya Chen (Northstar) to Daniel Ruiz
   (BlueLine). Concede what the contract genuinely excludes, hold what the evidence
   supports, and propose the computed counter. Reference documents by name, not by
   fact ids, inside the draft reply body - the reply must read as a real business email
   (no bracketed ids inside draft_reply.body; still no invented numbers).
5. Do not assert that a disputed or missing item is resolved. Where the case file marks
   something as a gap or open question, treat it that way.
6. In the draft reply, never overstate the record: a document not in the folder is
   "not currently available", not impossible to produce; an open question stays open.
   Concessions and commitments must match the recommended position exactly. When one
   sentence bundles several evidence items, claim from each only what it individually
   shows (a driver's note about a torn pallet wrap corroborates damage at delivery,
   not a specific carton count). Recommended next steps are things to do, not things
   done: in the reply, write them as commitments ("we will ask", "we are checking"),
   never as completed actions.
7. Historical claims: report each row's recorded outcome as recorded, attributed to its
   claim id. State a cross-claim pattern ONLY if the case file contains it as a computed
   fact (a cohort median, a pattern count), cited by that fact id - never generalize
   beyond what a cited fact literally says."""

POSITION_TASK = """Compose the negotiation position brief sections and the draft reply email.
negotiation_analysis: 4-6 tight paragraphs walking the money: what is contractually owed
vs offered, the disputed 5 units, the excluded components and why, what precedent suggests,
and the recommended counter with its logic. recommended_next_steps: 4-6 concrete actions.
risks_and_watchouts: 3-5 items.

Your output MUST be exactly this JSON shape (note: every next step is an OBJECT with both
keys; the top-level "executive_summary" key is required):
{
 "executive_summary": "...",
 "negotiation_analysis": ["paragraph 1 ...", "paragraph 2 ..."],
 "recommended_next_steps": [{"action": "...", "rationale": "..."}],
 "risks_and_watchouts": ["..."],
 "draft_reply": {"subject": "...", "body": "..."}
}

The case file follows."""

ASK_SYSTEM = """You answer questions about one freight claim using ONLY the retrieved
passages provided. Rules: quote your supporting evidence verbatim; cite each answer
sentence with the passage's source id in brackets, e.g. [proof_of_delivery]; if the
passages do not contain the answer, say exactly that and name what is missing. Output
plain text (no JSON)."""
