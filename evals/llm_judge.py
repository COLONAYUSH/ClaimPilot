"""LLM-as-judge for the composed sections of the brief.

The golden set stays deterministic because its questions have objective
answers. The judge covers what no string or number check can: whether the
written analysis uses its cited facts truthfully, whether the draft reply
stays inside the record, and whether the prose agrees with the computed
position. The known gap it closes: NumberGuard proves a figure exists in the
ledger, not that the sentence around it says something true.

Design notes:
  * The judge sees only case-file data (the sections plus the facts they
    reference), never the raw sources, so it verifies against a fixed ledger
    instead of re-deciding the case.
  * Verdicts are binary per criterion with quoted evidence, temperature 0,
    schema-forced, and served through the same content-addressed cache as
    every other call, so eval reruns are deterministic and free.
  * Same-family circularity is real. Verification against a provided ledger
    is a much easier task than composition, which blunts most of it; for the
    rest, CLAIMPILOT_JUDGE_MODEL lets you judge with a different model when
    you have API access to one.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

JUDGE_SYSTEM = """You are an adversarial reviewer of an AI-drafted freight-claim negotiation brief.
You receive the brief's written sections and the verified case data they were built
from. Your job is to catch misuse, not to admire prose.

Rules:
- Judge ONLY against the provided case data. No outside knowledge, no re-deciding
  the claim on the merits.
- For each criterion return pass=true or pass=false with one or two sentences of
  evidence. When failing, quote the offending sentence verbatim.
- Fail on genuine violations. Style preferences, tone and phrasing are not
  violations.
- Output only the JSON object."""

CRITERIA = [
    ("faithful_citation_use",
     "Every factual sentence in executive_summary and negotiation_analysis says the "
     "same thing the case data says. Fail if any sentence misstates a value, direction "
     "or relationship, or asserts something nothing in the provided data supports (a "
     "real number used in a wrong claim still fails). A claim that is supported "
     "elsewhere in the provided data but cited imprecisely passes; note it in the "
     "evidence."),
    ("no_overstatement_in_reply",
     "The draft reply asserts nothing beyond the record: missing documents are "
     "described as currently unavailable rather than impossible, disputed or open "
     "items are not presented as resolved, and no commitment appears that the case "
     "data does not support."),
    ("position_consistency",
     "The counter amount, the concessions and the holds in the prose match the "
     "computed position_numbers and entitlements exactly (counter value, markdown "
     "conceded, disputed units and fees held, freight framed as goodwill)."),
    ("register_separation",
     "Facts are cited with references, recommendations read as recommendations, and "
     "open questions stay open. Fail if speculation is presented as established fact."),
    ("material_completeness",
     "The analysis addresses the material items: the disputed damaged units, the "
     "markdown exclusion, the EDI versus POD count conflict, the freight-refund "
     "goodwill angle, and the evidence gaps."),
]

JUDGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["criteria"],
    "properties": {
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "pass", "evidence"],
                "properties": {
                    "id": {"type": "string",
                           "enum": [cid for cid, _ in CRITERIA]},
                    "pass": {"type": "boolean"},
                    "evidence": {"type": "string"},
                },
            },
        },
    },
}


def _materials(case: Dict[str, Any]) -> Dict[str, Any]:
    # The judge gets the complete ledger, not just the facts the analysis
    # cites. The draft reply carries no bracketed references by design, so a
    # subset starves the judge and produces false "unsupported" verdicts
    # (learned in calibration: the reply's "four days late" is backed by a
    # ledger fact the referenced-only view never included).
    facts = [
        {"id": f["fact_id"], "key": f["key"], "value": f["value"],
         **({"formula": f["formula"]} if f.get("formula") else {}),
         **({"note": f["note"]} if f.get("note") else {})}
        for f in case["facts"]
        if "QUARANTINED" not in (f.get("note") or "")
    ]
    return {
        "sections": case["position"]["data"],
        "position_numbers": case["position_numbers"],
        "entitlements": case["entitlements"],
        "discrepancies": [{"id": d["disc_id"], "severity": d["severity"],
                           "title": d["title"], "status": d["status"],
                           "description": d["description"],
                           **({"authority_note": d["authority_note"]}
                              if d.get("authority_note") else {})}
                          for d in case["discrepancies"]],
        "gaps": [{"id": g["gap_id"], "item": g["item"]} for g in case["gaps"]],
        "comparables": case["comparables"],
        "cohorts": case["cohorts"],
        "all_facts": facts,
    }


def judge_section(case: Dict[str, Any], cfg):
    """Run the judge over the composed sections; returns a run_evals Section."""
    from evals.run_evals import Section
    from claimpilot.llm import LLMClient, LLMRequest

    s = Section("LLM-as-judge (composed sections)")
    if not case["position"].get("ok"):
        s.check("composed sections available to judge", False,
                "position failed closed; nothing to judge")
        return s

    model = os.environ.get("CLAIMPILOT_JUDGE_MODEL", cfg.model)
    client = LLMClient(cfg.provider, model, cfg.cache_dir, cfg.max_repairs)
    criteria_text = "\n".join("- {}: {}".format(cid, desc) for cid, desc in CRITERIA)
    req = LLMRequest(
        prompt=("Evaluate the brief against these criteria:\n{}\n\n"
                "CASE DATA AND SECTIONS (JSON):\n{}\n\n"
                "Return ONLY JSON matching:\n{}").format(
                    criteria_text,
                    json.dumps(_materials(case), ensure_ascii=False, indent=1),
                    json.dumps(JUDGE_SCHEMA, indent=1)),
        system=JUDGE_SYSTEM, schema=JUDGE_SCHEMA, max_tokens=4000,
        label="eval/llm-judge")
    verdicts = {c["id"]: c for c in (client.call(req).obj or {}).get("criteria", [])}
    for cid, _ in CRITERIA:
        v = verdicts.get(cid)
        if v is None:
            s.check(cid, False, "judge returned no verdict for this criterion")
        else:
            s.check(cid, bool(v["pass"]), str(v.get("evidence", ""))[:220])
    return s
