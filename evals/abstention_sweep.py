"""Calibrate datum's abstention floor on the gold query set.

datum's differentiator here is a *typed* refusal: if the best dense similarity
among fused candidates is below `abstain_min_similarity`, the plan returns
`insufficient_evidence` instead of whatever ranked first. The default floor is
conservative; this sweep finds the operating point for THIS corpus - the floor
that maximizes refusals on deliberately unanswerable queries while leaving
answerable-query ranking untouched.

Run with datum's interpreter:
    DATUM_PG_DSN=postgresql://localhost/datum_claims_fcc \
    $DATUM_PYTHON evals/abstention_sweep.py

Writes evals/abstention_sweep.md. The chosen floor feeds DatumConfig
(CLAIMPILOT_ABSTAIN_FLOOR) and the third row of the retrieval benchmark.
"""

from __future__ import annotations

import json
import os
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
HERE = Path(__file__).resolve().parent

FLOORS = [None, 0.45, 0.50, 0.55, 0.60, 0.65]


def match(hit, expects) -> bool:
    for exp in expects:
        if hit.source_path != exp["source_id"]:
            continue
        frag = exp.get("title_contains")
        joined = " > ".join(hit.section_path)
        if frag is None or frag.lower() in joined.lower():
            return True
    return False


def main() -> int:
    from datum import Corpus
    from datum.kernel.principal import Principal

    dsn = os.environ.get("DATUM_PG_DSN", "postgresql://localhost/datum_claims_fcc")
    principal = Principal(id="claimpilot", namespace="tenant:northstar")
    queries = json.loads((HERE / "queries.json").read_text())["queries"]
    answerable = [q for q in queries if q["kind"] != "unanswerable"]
    unanswerable = [q for q in queries if q["kind"] == "unanswerable"]

    rows = []
    for floor in FLOORS:
        kwargs = {} if floor is None else {"abstain_min_similarity": floor}
        with Corpus.open(dsn, **kwargs) as corpus:
            hit1 = hit3 = wrongly_abstained = 0
            for q in answerable:
                ev = corpus.search(q["query"], principal=principal)
                if ev.status != "ok" or not ev.hits:
                    wrongly_abstained += 1
                    continue
                ranks = [i for i, h in enumerate(ev.hits[:5], 1) if match(h, q["expect"])]
                if ranks and ranks[0] == 1:
                    hit1 += 1
                if ranks and ranks[0] <= 3:
                    hit3 += 1
            refused = 0
            for q in unanswerable:
                ev = corpus.search(q["query"], principal=principal)
                if ev.status != "ok" or not ev.hits:
                    refused += 1
        rows.append({
            "floor": "default" if floor is None else floor,
            "hit_at_1": round(hit1 / len(answerable), 3),
            "hit_at_3": round(hit3 / len(answerable), 3),
            "wrongly_abstained_answerable": wrongly_abstained,
            "refused_unanswerable": "{}/{}".format(refused, len(unanswerable)),
        })
        print(rows[-1], flush=True)

    lines = ["# Abstention-floor calibration (datum)", "",
             "{} answerable / {} unanswerable gold queries. The floor thresholds the "
             "best dense similarity of the fused candidates; below it the plan returns "
             "a typed `insufficient_evidence` instead of the top-ranked chunk.".format(
                 len(answerable), len(unanswerable)), "",
             "| floor | hit@1 | hit@3 | wrong abstentions (answerable) | "
             "refusals (unanswerable) |", "|---|---|---|---|---|"]
    for r in rows:
        lines.append("| {} | {:.0%} | {:.0%} | {} | {} |".format(
            r["floor"], r["hit_at_1"], r["hit_at_3"],
            r["wrongly_abstained_answerable"], r["refused_unanswerable"]))
    (HERE / "abstention_sweep.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("written:", HERE / "abstention_sweep.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
