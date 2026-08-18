# Assumptions log

Every material assumption made while building and running the copilot, with the reasoning
and the blast radius if it turns out wrong. (The system's own outputs distinguish
extracted facts, party assertions, and derived values; this file covers the judgment
calls **around** that system.)

## Domain / case interpretation

1. **The signed POD governs the receiving count (58), not the carrier EDI (59).**
   Basis: the pack's data dictionary states the signed POD is the consignee's documented
   receiving exception and that the final EDI event is carrier-reported. The conflict is
   still surfaced as a HIGH discrepancy with both counts and the ruling attached - the
   specialist sees the disagreement, not just my resolution of it.

2. **Units per carton = 4** (invoice packing note, corroborated by the email thread and
   the case overview), so 2 short cartons = 8 units and 5 damaged cartons = 20 units.

3. **Unit weight = 15 lb** for the section-2 cap math, from two agreeing sources:
   3,600 lb / 240 units, and the invoice's "approximately 15 lb including retail
   packaging." Sensitivity: the $50/lb cap only becomes the binding limit below
   425/50 = **8.5 lb/unit**, so the approximation has a ~43% error margin before it
   could change any conclusion.

4. **The $18,000 markdown is treated as a party assertion, not an established loss.**
   No markdown documentation (credit memo, POS data) exists in the folder, and the
   agreement excludes loss-of-market damages for delay regardless. It is analyzed as a
   commercial-compromise topic only.

5. **Salvage value of the 14 unsellable units is unknown.** The agreement requires
   crediting salvage; the entitlement is stated gross of salvage with an explicit open
   question, rather than inventing a credit.

6. **Repack labor ($300) recoverability is undetermined** - the folder does not say
   whether repack was third-party or internal, and the agreement excludes internal
   administrative labor unless agreed. Classified NEEDS_INFO, not assumed either way.

7. **Claim filing date = claim-system `opened_at` (2026-05-13)**, matching the first
   email. Both section-5 notice windows (9 months cargo / 30 days delay) are evaluated
   against dates in the record, not against today's date.

8. **Photo "coverage" counts cartons whose damage a photo documents** (the photo's
   subject), not cartons incidentally visible intact in the frame - photo 1 shows C-023
   intact next to the damaged C-021, and counting it would overstate the evidence.
   This matches the inspector's own note ("Photos supplied to surveyor: only C-021 and
   C-023") and the carrier's position in the thread.

9. **The case-overview PDF is a convenience summary** (it says so itself). Every figure
   on it is re-verified against primary records; it is never the sole source for a fact.

10. **CSV is canonical for historical claims; the xlsx twin is a cross-check.** The xlsx
    stores `settlement_pct` as formulas, so values are compared numerically with a small
    tolerance after a `data_only` read.

## Technical

11. **LLM = `claude-sonnet-5`, temperature 0, schema-forced JSON.** Locally the calls run
    through the Claude Code CLI in headless mode (no API key on this machine); the direct
    Anthropic API provider is implemented for production; a content-addressed replay
    cache makes evals/CI deterministic and free.

12. **Scanned/photographic sources are read by the model's vision** (transcript first,
    then fields quoted from the transcript, then an independent second-pass verification
    against the image). Facts from these sources carry reduced confidence and are
    cross-checked against structured sources where overlaps exist. In production I would
    add a second OCR engine (e.g. Textract) for disagreement detection.

13. **datum is the primary retrieval backend**, running out-of-process under its own
    Python 3.12 venv against local Postgres/pgvector (`DATUM_PYTHON`, `DATUM_PG_DSN`).
    Where it is unavailable the app falls back loudly to stdlib SQLite FTS5 - degraded
    (lexical-only, no abstention) but functional. The head-to-head numbers live in
    `evals/retrieval_bench.md`.

14. **Negotiation arithmetic is a policy, stated openly:** recommended counter = the
    fully documented supportable case (missing + all unsellable + inspection + repack)
    plus a freight-charge-scale goodwill ask; the expected band runs from
    floor + inspection fee up to the counter. Historical settlement percentages are
    shown as context and never multiplied into our claim (the data dictionary forbids
    treating them as entitlements).

15. **Single-claim scope.** The pipeline processes one claim folder per run; the manifest,
    ledger and retrieval namespace are per-claim. Scaling to a queue of claims is an
    orchestration concern deliberately left out of this exercise.
