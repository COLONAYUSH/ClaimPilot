# 02 - Scenario and swimlanes

One claim traced through the whole system with real values, then the adversarial version
of the same claim. If doc 01 is the map, this is the drive.

- [Trigger points](#trigger-points)
- [The claim](#the-claim)
- [Swimlane: who does what, in order](#swimlane-who-does-what-in-order)
- [Following one number to the brief](#following-one-number-to-the-brief)
- [The adversarial run](#the-adversarial-run)

## Trigger points

Today the trigger is the CLI (`claimpilot run --pack <folder>`). The events that would
start a run in a real deployment, all of which reduce to "a claim folder is ready":

| Trigger | Source | Notes |
|---|---|---|
| New claim filed | inbound email or a portal upload | the common case; assemble the folder, enqueue |
| Carrier reply arrives | inbound email on an open claim | re-run to refresh the position with the new message |
| Analyst re-run | reviewer UI button | after adding a missing document (e.g. the packaging spec) |
| Scheduled batch | nightly backfill / re-evaluation | re-score open claims against updated comparables |
| Interactive question | reviewer asks a question | `ask` path, retrieval + one grounded answer, no full pipeline |

## The claim

FCL-2026-0147, Northstar vs BlueLine. 240 barcode scanners, $102,000 invoice, delivered 4
days late with 2 cartons missing and 5 damaged. Demand $29,920, carrier offer $7,225. The
folder holds 15 sources across five trust tiers:

<p align="center"><img src="../diagrams/sources.png" alt="Claim sources by trust tier" width="740"></p>

Trust tier is not decoration. When the carrier's EDI event (OPERATIONAL) says 59 pieces
delivered and the signed POD (PRIMARY_RECORD) says 58 cartons received, reconciliation
rules the POD governs and quotes the data dictionary as the authority, while still
surfacing the conflict.

## Swimlane: who does what, in order

The lanes are the actors. Read top to bottom. Deterministic lanes never wait on the LLM
lane for a computed answer.

<p align="center"><img src="../diagrams/swimlane_claim.png" alt="Claim traced through the pipeline as a swimlane" width="470"></p>

## Following one number to the brief

Take the headline number, the $11,920 counter, and walk it backward. This is the
provenance chain the data model guarantees.

<p align="center"><img src="../diagrams/provenance.png" alt="Provenance of the recommended counter" width="840"></p>

The counter is never something the model wrote. It is `core_high + goodwill_high`, each of
which is a sum of per-line entitlements, each of which is units times a verified price, and
each unit count traces to a quote checked against a signed document. The LLM's only job at
the end was to explain this in a paragraph, and NumberGuard confirmed the paragraph
introduced no number that is not already in this tree.

## The adversarial run

`claimpilot robustness` seeds a copy of the pack with three synthetic attacks and runs the
identical pipeline. This is the same swimlane with an attacker in the input.

<p align="center"><img src="../diagrams/swimlane_adv.png" alt="Adversarial run swimlane" width="540"></p>

Why the attack cannot win: the recommended counter is computed from structured facts the
injection never touched. The forged instruction lands in the ledger as one more `ASSERTED`
fact with a quote, and there is no code path from an assertion to a conclusion. The scanner
surfaces the attempt so a human sees it, and reconciliation catches the offer tamper
because a second source disagrees. The prose is the only surface the attacker could hope to
move, and NumberGuard plus the reference check hold it to the ledger. See
`evals/robustness_report.md` for the committed run.

Next: [03 - Production architecture](03-production-architecture.md).
