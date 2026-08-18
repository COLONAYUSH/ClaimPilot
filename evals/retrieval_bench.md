# Retrieval benchmark - datum vs FTS5

21 queries (18 answerable, 3 unanswerable) over 55 chunks; gold labels in queries.json

| backend | hit@1 | hit@3 | MRR@5 | false-answer rate (unanswerable) | median latency |
|---|---|---|---|---|---|
| fts5 | 72% | 89% | 0.81 | 100% | 0 ms |
| datum (default floor) | 89% | 94% | 0.93 | 100% | 822 ms |
| datum (calibrated floor=0.5) | 83% | 89% | 0.87 | 33% | 812 ms |

### fts5 - by query kind

| kind | n | hit@1 | hit@3 |
|---|---|---|---|
| cross_doc | 3 | 67% | 100% |
| entity | 5 | 80% | 100% |
| paraphrase | 3 | 33% | 67% |
| semantic | 5 | 80% | 80% |
| verbatim | 2 | 100% | 100% |

Misses (fts5):
- `can we recover lost promotion sales because the shipment arrived late` [semantic] -> status=ok top=email_thread::Email 5 - Maya Chen <maya.chen@northstar.example> (shipper) 2026-05-20
- `what is the warranty period for the scanner battery` [unanswerable] -> status=ok top=carrier_agreement::3. Packaging and Mitigation
- `what temperature range applies to refrigerated shipments` [unanswerable] -> status=ok top=carrier_agreement::1. Scope and Order of Precedence
- `what fuel surcharge percentage does the carrier apply` [unanswerable] -> status=ok top=email_thread::Email 6 - Daniel Ruiz <daniel.ruiz@blueline.example> (carrier) 2026-05-22

### datum (default floor) - by query kind

| kind | n | hit@1 | hit@3 |
|---|---|---|---|
| cross_doc | 3 | 100% | 100% |
| entity | 5 | 80% | 100% |
| paraphrase | 3 | 100% | 100% |
| semantic | 5 | 80% | 80% |
| verbatim | 2 | 100% | 100% |

Misses (datum (default floor)):
- `what is the warranty period for the scanner battery` [unanswerable] -> status=ok top=bill_of_lading::Signed bill of lading (tender record)
- `what temperature range applies to refrigerated shipments` [unanswerable] -> status=ok top=tms_shipment::TMS shipment record with EDI events (carrier-reported counts)
- `what fuel surcharge percentage does the carrier apply` [unanswerable] -> status=ok top=historical_claims::Historical claim HC-2026-0063

### datum (calibrated floor=0.5) - by query kind

| kind | n | hit@1 | hit@3 |
|---|---|---|---|
| cross_doc | 3 | 100% | 100% |
| entity | 5 | 80% | 100% |
| paraphrase | 3 | 100% | 100% |
| semantic | 5 | 60% | 60% |
| verbatim | 2 | 100% | 100% |

Misses (datum (calibrated floor=0.5)):
- `do we have to credit the value of goods that can still be sold` [semantic] -> status=insufficient_evidence top=None
- `what temperature range applies to refrigerated shipments` [unanswerable] -> status=ok top=tms_shipment::TMS shipment record with EDI events (carrier-reported counts)
