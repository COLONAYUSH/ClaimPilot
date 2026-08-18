"""Retrieval backend head-to-head: datum vs SQLite FTS5.

Both backends index the identical chunk set. The gold set (queries.json)
spans verbatim, paraphrase, semantic-no-overlap, entity, cross-document and
deliberately unanswerable queries.

Metrics
  hit@1 / hit@3 / MRR@5   on answerable queries (a hit = an acceptable
                          (source, section) target in the ranked list)
  false-answer rate       on unanswerable queries: returning ranked hits for
                          a question the corpus cannot answer. This is the
                          capability gap the benchmark exists to expose -
                          FTS5 has no notion of sufficiency; datum can return
                          a typed `insufficient_evidence` instead.
  latency                 per query, warm.

The report states results per kind so the trade-off is visible rather than
averaged away.
"""

from __future__ import annotations

import copy
import json
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent


def _match(hit, expects) -> bool:
    for exp in expects:
        if hit.source_id != exp["source_id"]:
            continue
        frag = exp.get("title_contains")
        if frag is None or frag.lower() in (hit.title or "").lower() \
                or frag.lower() in (hit.locator or "").lower():
            return True
    return False


def run_backend(name: str, retriever, queries: List[Dict[str, Any]]) -> Dict[str, Any]:
    per_query = []
    for q in queries:
        res = retriever.search(q["query"], k=5)
        entry: Dict[str, Any] = {
            "id": q["id"], "kind": q["kind"], "query": q["query"],
            "status": res.status, "elapsed_ms": res.elapsed_ms,
            "answered": res.answered, "sufficiency": res.sufficiency,
            "top": "{}::{}".format(res.hits[0].source_id, res.hits[0].title)
            if res.hits else None,
        }
        if q["kind"] == "unanswerable":
            entry["correct_refusal"] = not res.answered
        else:
            rank = None
            for i, h in enumerate(res.hits, start=1):
                if _match(h, q["expect"]):
                    rank = i
                    break
            entry["rank"] = rank
        per_query.append(entry)

    answerable = [e for e in per_query if e["kind"] != "unanswerable"]
    unanswerable = [e for e in per_query if e["kind"] == "unanswerable"]
    ranks = [e["rank"] for e in answerable]

    def rate(pred) -> float:
        return round(sum(1 for r in ranks if r is not None and pred(r)) / len(ranks), 4) \
            if ranks else 0.0

    summary = {
        "backend": name,
        "hit_at_1": rate(lambda r: r <= 1),
        "hit_at_3": rate(lambda r: r <= 3),
        "mrr_at_5": round(sum(1.0 / r for r in ranks if r) / len(ranks), 4) if ranks else 0.0,
        "false_answer_rate_unanswerable": round(
            sum(1 for e in unanswerable if not e.get("correct_refusal")) /
            len(unanswerable), 4) if unanswerable else None,
        "median_latency_ms": int(statistics.median(e["elapsed_ms"] for e in per_query)),
        "per_kind": {},
        "per_query": per_query,
    }
    for kind in sorted({e["kind"] for e in answerable}):
        ks = [e for e in answerable if e["kind"] == kind]
        summary["per_kind"][kind] = {
            "n": len(ks),
            "hit_at_1": round(sum(1 for e in ks if e["rank"] == 1) / len(ks), 4),
            "hit_at_3": round(sum(1 for e in ks if e["rank"] and e["rank"] <= 3)
                              / len(ks), 4),
        }
    return summary


def main(cfg) -> int:
    from claimpilot.ingest import load_registry
    from claimpilot.retrieval import (DatumRetriever, FTS5Retriever,
                                      RetrieverUnavailable, build_chunks)

    queries = json.loads((HERE / "queries.json").read_text())["queries"]
    cfg.resolve_paths()
    registry = load_registry(cfg)

    # Vision transcripts make the scan/photos searchable; reuse cached
    # extractions when available so the bench includes them.
    try:
        from claimpilot.extract import run_extraction
        from claimpilot.llm import LLMClient
        from claimpilot.models import FactLedger
        client = LLMClient(cfg.provider, cfg.model, cfg.cache_dir, cfg.max_repairs)
        run_extraction(registry, FactLedger(), client, cfg)
    except Exception as exc:
        print("note: transcripts unavailable for bench ({}); image sources excluded"
              .format(exc))

    chunks = build_chunks(registry)
    print("bench corpus: {} chunks from {} sources".format(
        len(chunks), len({c.source_id for c in chunks})))

    results = []
    fts = FTS5Retriever()
    fts.index(chunks)
    results.append(run_backend("fts5", fts, queries))
    fts.close()

    try:
        import copy
        cfg_default = copy.deepcopy(cfg)
        cfg_default.datum.abstain_floor = None   # datum's own default
        datum = DatumRetriever(cfg_default)
        datum.index(chunks)
        # warm-up: first query pays lazy model init; keep metrics warm
        datum.search("warm up query about freight", k=1)
        results.append(run_backend("datum (default floor)", datum, queries))
        datum.close()

        floor = cfg.datum.abstain_floor
        if floor is not None:
            datum2 = DatumRetriever(cfg)
            datum2.index(chunks)
            datum2.search("warm up query about freight", k=1)
            results.append(run_backend(
                "datum (calibrated floor={})".format(floor), datum2, queries))
            datum2.close()
    except RetrieverUnavailable as exc:
        print("datum unavailable for bench: {}".format(exc))

    lines = ["# Retrieval benchmark - datum vs FTS5", "",
             "{} queries ({} answerable, {} unanswerable) over {} chunks; "
             "gold labels in queries.json".format(
                 len(queries),
                 sum(1 for q in queries if q["kind"] != "unanswerable"),
                 sum(1 for q in queries if q["kind"] == "unanswerable"),
                 len(chunks)), "",
             "| backend | hit@1 | hit@3 | MRR@5 | false-answer rate (unanswerable) | "
             "median latency |",
             "|---|---|---|---|---|---|"]
    for r in results:
        lines.append("| {} | {:.0%} | {:.0%} | {:.2f} | {} | {} ms |".format(
            r["backend"], r["hit_at_1"], r["hit_at_3"], r["mrr_at_5"],
            "{:.0%}".format(r["false_answer_rate_unanswerable"])
            if r["false_answer_rate_unanswerable"] is not None else "n/a",
            r["median_latency_ms"]))
    lines.append("")
    for r in results:
        lines.append("### {} - by query kind".format(r["backend"]))
        lines.append("")
        lines.append("| kind | n | hit@1 | hit@3 |")
        lines.append("|---|---|---|---|")
        for kind, st in r["per_kind"].items():
            lines.append("| {} | {} | {:.0%} | {:.0%} |".format(
                kind, st["n"], st["hit_at_1"], st["hit_at_3"]))
        lines.append("")
        misses = [e for e in r["per_query"]
                  if (e["kind"] != "unanswerable" and e.get("rank") is None)
                  or (e["kind"] == "unanswerable" and not e.get("correct_refusal"))]
        if misses:
            lines.append("Misses ({}):".format(r["backend"]))
            for e in misses:
                lines.append("- `{}` [{}] -> status={} top={}".format(
                    e["query"], e["kind"], e["status"], e.get("top")))
            lines.append("")
    report = "\n".join(lines)
    out_md = HERE / "retrieval_bench.md"
    out_md.write_text(report, encoding="utf-8")
    (HERE / "retrieval_bench.json").write_text(
        json.dumps(results, indent=1), encoding="utf-8")
    print(report)
    print("written:", out_md)
    return 0
