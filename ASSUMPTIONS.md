# Assumptions

The judgment calls I made while building and running this, why I made them, and what
breaks if one turns out wrong. The pipeline itself separates extracted facts, party
assertions and derived values; this file covers the decisions around that machinery.

## Reading the case

1. **The signed POD governs the receiving count (58), not the carrier EDI (59).**
   The pack's data dictionary says the signed POD is the consignee's documented
   receiving exception and that the final EDI event is carrier-reported. I still
   surface the conflict as a HIGH discrepancy with both counts and the ruling attached,
   so a specialist sees the disagreement rather than my resolution of it.

2. **Units per carton is 4.** From the invoice packing note, corroborated by the email
   thread and the case overview. So 2 short cartons means 8 units and 5 damaged
   cartons means 20 units.

3. **Unit weight is 15 lb** for the section 2 cap math, from two agreeing sources:
   3,600 lb over 240 units, and the invoice's "approximately 15 lb including retail
   packaging". The $50/lb cap only becomes the binding limit below 425/50 = 8.5 lb per
   unit, so the approximation has about a 43% margin before it could change any
   conclusion.

4. **The $18,000 markdown is a party assertion, not an established loss.** There is no
   markdown documentation (credit memo, POS data) anywhere in the folder, and the
   agreement excludes loss-of-market damages for delay regardless. It gets analyzed as
   a commercial-compromise topic only.

5. **Salvage value of the 14 unsellable units is unknown.** The agreement requires
   crediting salvage. I state the entitlement gross of salvage and raise the open
   question instead of inventing a credit.

6. **Whether the $300 repack labor is recoverable is undetermined.** The folder does
   not say if the repack was third-party or internal, and the agreement excludes
   internal administrative labor unless agreed. Classified NEEDS_INFO, not assumed
   either way.

7. **The claim filing date is the claim system's `opened_at` (2026-05-13)**, which
   matches the first email. Both section 5 notice windows (9 months cargo, 30 days
   delay) are checked against dates in the record, not against today's date.

8. **Photo "coverage" counts cartons whose damage a photo documents**, not cartons that
   happen to be visible. Photo 1 shows C-023 intact next to the damaged C-021, and
   counting it would overstate the evidence. This matches the inspector's note
   ("Photos supplied to surveyor: only C-021 and C-023") and the carrier's own position
   in the thread.

9. **The case-overview PDF is a convenience summary.** It says so itself. Every figure
   on it gets re-verified against primary records and it is never the sole source for
   a fact.

10. **The CSV is canonical for historical claims, the xlsx is a cross-check.** The xlsx
    stores `settlement_pct` as formulas, so values are compared numerically with a
    small tolerance after a `data_only` read.

## Technical

11. **The LLM is `claude-sonnet-5` at temperature 0 with schema-forced JSON.** The direct
    Anthropic API provider is the production path; a local model CLI provider and a
    cache-only replay provider cover machines with no key configured; the
    content-addressed replay cache makes evals and CI deterministic and free.

12. **Scanned and photographic sources are read by the model's vision.** Transcript
    first, then fields quoted from the transcript, then an independent second pass
    against the image. Facts from these sources carry reduced confidence and get
    cross-checked against structured sources where they overlap. In production I would
    add a second OCR engine and diff the two.

13. **datum is the primary retrieval backend**, running out of process under its own
    Python 3.12 venv against local Postgres/pgvector (`DATUM_PYTHON`, `DATUM_PG_DSN`).
    Where it is unavailable the app falls back to stdlib SQLite FTS5 and logs that the
    run is degraded (lexical only, no abstention). The head-to-head numbers live in
    `evals/retrieval_bench.md`.

14. **The negotiation arithmetic is a policy, stated in the open.** Recommended counter
    equals the fully documented supportable case (missing product, all unsellable
    units, inspection fee, repack labor) plus a goodwill ask at freight-charge scale.
    The expected band runs from floor plus inspection fee up to the counter. Historical
    settlement percentages appear as context and never get multiplied into this claim,
    because the data dictionary says they are outcomes, not entitlements.

15. **One claim per run.** The manifest, ledger and retrieval namespace are all
    per-claim. Scaling to a queue of claims is an orchestration problem I deliberately
    left out of the exercise.
