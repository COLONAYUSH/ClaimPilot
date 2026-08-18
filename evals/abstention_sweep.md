# Abstention-floor calibration (datum)

18 answerable / 3 unanswerable gold queries. The floor thresholds the best dense similarity of the fused candidates; below it the plan returns a typed `insufficient_evidence` instead of the top-ranked chunk.

| floor | hit@1 | hit@3 | wrong abstentions (answerable) | refusals (unanswerable) |
|---|---|---|---|---|
| default | 89% | 94% | 0 | 0/3 |
| 0.45 | 89% | 94% | 0 | 0/3 |
| 0.5 | 83% | 89% | 1 | 2/3 |
| 0.55 | 72% | 78% | 3 | 3/3 |
| 0.6 | 39% | 39% | 11 | 3/3 |
| 0.65 | 11% | 11% | 16 | 3/3 |
