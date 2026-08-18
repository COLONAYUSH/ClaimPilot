"""Golden-set evaluation for the Freight Claim Copilot.

What is measured, against hand-labeled ground truth (golden.json):
  1. Fact extraction accuracy (deterministic + LLM + vision fields).
  2. Discrepancy detection recall over the pack's planted conflicts.
  3. Evidence-gap detection recall.
  4. Entitlement classification and bounds (the money logic).
  5. Grounding health: citation validity, quarantine count, NumberGuard.
  6. Ablation: rerun without the scanned inspection report - the pipeline
     must degrade explicitly (downgrade the damage line, raise a gap, use no
     inspection-derived numbers) instead of inventing facts.

Thanks to the content-addressed LLM cache, re-running this suite is
deterministic and free once a live run has populated the cache.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Tuple

HERE = Path(__file__).resolve().parent


def _eq(expected: Any, actual: Any) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return bool(expected) == bool(actual)
    try:
        return Decimal(str(expected)) == Decimal(str(actual))
    except (InvalidOperation, ValueError, TypeError):
        return str(expected).strip() == str(actual).strip()


class Section:
    def __init__(self, name: str) -> None:
        self.name = name
        self.rows: List[Tuple[str, bool, str]] = []

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        self.rows.append((label, bool(ok), detail))

    @property
    def passed(self) -> int:
        return sum(1 for _, ok, _ in self.rows if ok)

    def render(self) -> List[str]:
        out = ["## {}  ({}/{})".format(self.name, self.passed, len(self.rows)), ""]
        for label, ok, detail in self.rows:
            mark = "PASS" if ok else "FAIL"
            out.append("- [{}] {}{}".format(mark, label,
                                            " - {}".format(detail) if detail else ""))
        out.append("")
        return out


def evaluate_case(case: Dict[str, Any], golden: Dict[str, Any]) -> List[Section]:
    facts_by_key = {f["key"]: f for f in case["facts"]}
    sections: List[Section] = []

    s = Section("Fact extraction accuracy")
    for key, expected in golden["facts"].items():
        f = facts_by_key.get(key)
        if f is None:
            s.check(key, False, "fact missing")
        else:
            s.check(key, _eq(expected, f["value"]),
                    "" if _eq(expected, f["value"])
                    else "expected {!r}, got {!r}".format(expected, f["value"]))
    for key, expected_set in golden.get("fact_sets", {}).items():
        f = facts_by_key.get(key)
        actual = set(f["value"]) if f and isinstance(f["value"], list) else set()
        s.check(key, actual == set(expected_set),
                "" if actual == set(expected_set)
                else "expected {}, got {}".format(sorted(expected_set), sorted(actual)))
    sections.append(s)

    s = Section("Discrepancy detection (planted conflicts)")
    for spec in golden["discrepancies"]:
        matched = None
        for d in case["discrepancies"]:
            if d["category"] != spec["category"]:
                continue
            if not any(t.lower() in d["title"].lower()
                       for t in spec["title_contains_any"]):
                continue
            if spec.get("severity") and d["severity"] != spec["severity"]:
                continue
            matched = d
            break
        s.check(spec["name"], matched is not None,
                matched["disc_id"] if matched else "no matching finding")
    sections.append(s)

    s = Section("Evidence-gap detection")
    for spec in golden["gaps"]:
        needle = spec["item_contains"].lower()
        hit = next((g for g in case["gaps"] if needle in g["item"].lower()), None)
        s.check(spec["item_contains"], hit is not None,
                hit["gap_id"] if hit else "not raised")
    sections.append(s)

    s = Section("Entitlement classification & bounds")
    ents = {e["key"]: e for e in case["entitlements"]}
    for key, spec in golden["entitlements"].items():
        e = ents.get(key)
        if e is None:
            s.check(key, False, "entitlement line missing")
            continue
        ok = (e["classification"] == spec["classification"]
              and _eq(spec["low"], e["entitled_low"])
              and _eq(spec["high"], e["entitled_high"]))
        s.check(key, ok, "" if ok else "{} {}-{} (expected {} {}-{})".format(
            e["classification"], e["entitled_low"], e["entitled_high"],
            spec["classification"], spec["low"], spec["high"]))
    sections.append(s)

    s = Section("Historical comparables")
    got = {c["claim_id"] for c in case["comparables"]}
    for cid in golden["comparables_must_include"]:
        s.check("top-5 includes {}".format(cid), cid in got,
                "" if cid in got else "top-5 was {}".format(sorted(got)))
    sections.append(s)

    s = Section("Grounding & guardrails")
    qa, thr = case["qa"], golden["qa_thresholds"]
    s.check("citation validity >= {}".format(thr["citation_validity_min"]),
            qa["citation_validity_rate"] >= thr["citation_validity_min"],
            "{:.1%} ({} exact, {} fuzzy, {} failed)".format(
                qa["citation_validity_rate"], qa["quotes_exact"], qa["quotes_fuzzy"],
                qa["quotes_failed"]))
    s.check("quarantined facts <= {}".format(thr["max_quarantined"]),
            len(qa["quarantined_facts"]) <= thr["max_quarantined"],
            str(qa["quarantined_facts"]))
    if thr["position_must_compose"]:
        s.check("position composed & NumberGuard clean", case["position"]["ok"],
                "attempts={} violations={}".format(
                    case["position"]["attempts"],
                    len(case["position"]["violations"])))
    sections.append(s)
    return sections


def judged_sections(case: Dict[str, Any], cfg) -> List[Section]:
    """The qualitative layer: deterministic checks stop where objective answers
    stop; an adversarial LLM judge covers the composed prose (see llm_judge.py)."""
    try:
        from evals.llm_judge import judge_section
        return [judge_section(case, cfg)]
    except Exception as exc:
        s = Section("LLM-as-judge (composed sections)")
        s.check("judge ran", False, "{}: {}".format(type(exc).__name__, exc))
        return [s]


def evaluate_ablation(case: Dict[str, Any], golden: Dict[str, Any]) -> Section:
    spec = golden["ablation_inspection"]
    s = Section("Ablation: inspection report removed (graceful degradation)")
    s.check("pipeline completed", True)
    prefix = spec["forbidden_fact_prefix"]
    leaked = [f["key"] for f in case["facts"] if f["key"].startswith(prefix)]
    s.check("no {}* facts invented".format(prefix), not leaked, ", ".join(leaked[:5]))
    ents = {e["key"]: e for e in case["entitlements"]}
    damaged = ents.get("damaged_product") or ents.get("damaged_disputed")
    s.check("damage line downgraded to {}".format(spec["damaged_line_classification"]),
            damaged is not None and
            damaged["classification"] == spec["damaged_line_classification"],
            damaged["classification"] if damaged else "line missing")
    hit = next((g for g in case["gaps"]
                if spec["gap_item_contains"].lower() in g["item"].lower()), None)
    s.check("gap raised for the missing report", hit is not None,
            hit["gap_id"] if hit else "not raised")
    if case["position"]["ok"]:
        texts = json.dumps(case["position"]["data"])
        for needle in spec["report_only_strings"]:
            s.check("report-only detail {!r} absent from brief".format(needle),
                    needle not in texts)
    facts_text = json.dumps([f for f in case["facts"]
                             if not f["key"].startswith("history.")])
    for needle in spec["report_only_strings"]:
        s.check("report-only detail {!r} absent from fact ledger".format(needle),
                needle not in facts_text)
    return s


def main(cfg) -> int:
    from claimpilot.pipeline import run_pipeline

    golden = json.loads((HERE / "golden.json").read_text())

    print("== full-evidence run ==")
    case = run_pipeline(cfg)
    sections = evaluate_case(case, golden)
    sections.extend(judged_sections(case, cfg))

    print("== ablation run (inspection report removed) ==")
    cfg2 = copy.deepcopy(cfg)
    cfg2.ablate = ["inspection_report"]
    cfg2.out_dir = str(Path(cfg.out_dir) / "ablation")
    ablation_case = run_pipeline(cfg2)
    from claimpilot.report import write_outputs
    write_outputs(ablation_case, cfg2.out_dir)
    sections.append(evaluate_ablation(ablation_case, golden))

    total = sum(len(s.rows) for s in sections)
    passed = sum(s.passed for s in sections)
    lines = ["# Evaluation report - Freight Claim Copilot", "",
             "Overall: **{}/{} checks passed ({:.1f}%)**".format(
                 passed, total, passed / total * 100 if total else 0.0),
             "Run: provider={} model={} retrieval={} | LLM cost this eval: ${:.2f}".format(
                 case["run"]["provider"], case["run"]["model"],
                 case["run"]["retrieval_backend"],
                 case["run"]["llm_cost_usd"] + ablation_case["run"]["llm_cost_usd"]),
             ""]
    for s in sections:
        lines.extend(s.render())
    report = "\n".join(lines)
    out = HERE / "eval_report.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    print("written:", out)
    return 0 if passed == total else 1
