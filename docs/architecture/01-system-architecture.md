# 01 - System architecture

The current codebase, at the level of who calls what, what data moves between stages, and
where the determinism boundary sits. About 5,500 lines of Python across `claimpilot/`,
plus `evals/`.

- [Module graph and import direction](#module-graph-and-import-direction)
- [The core data model: the fact ledger](#the-core-data-model-the-fact-ledger)
- [Process and network topology](#process-and-network-topology)
- [The pipeline, stage by stage](#the-pipeline-stage-by-stage)
- [The three deterministic guards](#the-three-deterministic-guards)
- [The datum bridge protocol](#the-datum-bridge-protocol)
- [The LLM provider and cache layer](#the-llm-provider-and-cache-layer)
- [Component internals (deep dive)](#component-internals-deep-dive)
  - [Adversarial scanning engine](#adversarial-scanning-engine)
  - [Retrieval engine internals](#retrieval-engine-internals)
  - [Extraction engine: three paths](#extraction-engine-three-paths)
  - [Reconciliation engine: anatomy of a rule](#reconciliation-engine-anatomy-of-a-rule)
  - [Entitlement calculator](#entitlement-calculator)
  - [NumberGuard tokenizer](#numberguard-tokenizer)
- [Failure and degradation matrix](#failure-and-degradation-matrix)

## Module graph and import direction

Imports run one way: `cli` and `pipeline` sit on top and wire everything; the leaf
modules (`util`, `models`, `config`) depend on nothing internal. Nothing under a stage
imports the orchestrator, so each stage is testable in isolation.

```mermaid
flowchart TD
    cli["cli.py<br/><i>run · ask · eval · bench · robustness</i>"] --> pipeline
    pipeline["pipeline.py<br/><i>orchestrator</i>"]

    pipeline --> ingest["ingest.py"]
    pipeline --> extract["extract.py"]
    pipeline --> grounding["grounding.py"]
    pipeline --> security["security.py"]
    pipeline --> reconcile["reconcile.py"]
    pipeline --> entitlement["entitlement.py"]
    pipeline --> benchmark["benchmark.py"]
    pipeline --> position["position.py"]
    pipeline --> report["report.py"]

    extract --> llm["llm.py<br/><i>providers · cache · schema repair</i>"]
    entitlement --> llm
    position --> llm
    extract --> prompts["prompts.py<br/><i>schemas + templates</i>"]
    entitlement --> prompts
    position --> prompts

    entitlement --> retrieval["retrieval.py"]
    retrieval --> bridge["datum_bridge.py<br/><i>runs under datum's venv</i>"]

    ingest --> models["models.py<br/><i>FactLedger + domain types</i>"]
    extract --> models
    reconcile --> models
    grounding --> models
    security --> models
    pipeline --> config["config.py<br/><i>manifest · trust tiers · run config</i>"]
    models --> util["util.py<br/><i>money · hashing · quote match · json</i>"]

    classDef top fill:#1f6feb,stroke:#0b3d91,color:#fff;
    classDef llmnode fill:#D97757,stroke:#8a3b1e,color:#fff;
    classDef leaf fill:#57606a,stroke:#2b2f36,color:#fff;
    class cli,pipeline top;
    class llm llmnode;
    class util,models,config leaf;
```

Two design rules hold the shape:

1. **The determinism boundary is a module boundary.** `llm.py` is the only path to a
   model. `reconcile.py`, `entitlement.py` (the calculator half), `benchmark.py`,
   `grounding.py` and `security.py` never import it. If a number is wrong, it is wrong in
   Python I can unit-test, not in a prompt.
2. **`models.py` is the shared vocabulary.** Every stage reads and writes the same
   `FactLedger`, so provenance travels with the data instead of being reassembled at the
   end.

## The core data model: the fact ledger

Everything the system knows is a `Fact` in an ordered `FactLedger` with a by-key index.
Three registers are kept apart by the `kind` field, and nothing promotes one to another
silently.

```mermaid
classDiagram
    class FactLedger {
        +list~Fact~ facts
        +dict by_key
        +add(key, value, kind, method, citations) Fact
        +fact(key) Fact
        +value(key) Any
        +dec(key) Decimal
    }
    class Fact {
        +str fact_id
        +str key
        +Any value
        +str kind
        +str method
        +list~Citation~ citations
        +float confidence
        +list~str~ inputs
        +str formula
    }
    class Citation {
        +str source_id
        +str locator
        +str quote
        +bool verified
        +float match_ratio
    }
    FactLedger "1" o-- "many" Fact
    Fact "1" o-- "many" Citation
```

Field notes: `fact_id` is a stable label like `F-217`; `key` is the dotted namespace such
as `pod.received_cartons`; `kind` is one of `EXTRACTED | ASSERTED | DERIVED`; `method` is
`DETERMINISTIC | LLM | LLM_VISION | COMPUTED`; `inputs` and `formula` are populated for
`DERIVED` facts only; a `Citation.locator` looks like `page:1`, `message:4`, or
`$.events[6].pieces`.

| Register | Written by | Rule enforced elsewhere |
|---|---|---|
| `EXTRACTED` | ingest (structured), extract (LLM/vision) | must carry a quote that the quote gate verifies |
| `ASSERTED` | a party's claim (e.g. the markdown figure) | never used as a computed input; the entitlement engine treats it as a claim, not a value |
| `DERIVED` | reconcile, entitlement, benchmark | carries `formula` and `inputs` (the fact_ids it was computed from) |

Because a `DERIVED` fact records its inputs, any figure in the final brief walks back
through `inputs` to the `EXTRACTED` facts it rests on, and from there to a `Citation` with
a source file and a verified quote. That chain is what "traceable" means in this system,
and it is a data-structure property, not a reporting convention.

## Process and network topology

One run spans two Python interpreters and up to two external services. The split exists
because the app targets the system Python 3.9 while datum needs 3.11+, and because datum
is stateful (Postgres). Keeping datum out of process is also how it deploys for real.

```mermaid
flowchart LR
    subgraph proc1["Process A - claimpilot (Python 3.9)"]
        orch["pipeline orchestrator"]
        llmc["llm.LLMClient<br/>+ DiskCache"]
        dret["retrieval.DatumRetriever"]
    end

    subgraph proc2["Process B - datum bridge (Python 3.12 venv)"]
        br["datum_bridge.py<br/>stdin/stdout JSON-lines"]
        corpus["datum.Corpus"]
    end

    cache[("Disk cache<br/>.cache/llm/*.json<br/>content-addressed")]
    pg[("PostgreSQL 17<br/>+ pgvector")]
    prov{{"Model provider<br/>Anthropic API · Claude CLI · replay"}}

    orch --> llmc
    llmc <-->|"sha256 key"| cache
    llmc -->|"HTTPS or subprocess"| prov
    orch --> dret
    dret <-->|"newline-delimited JSON<br/>over a pipe"| br
    br --> corpus
    corpus <-->|"SQL + vector ops"| pg

    classDef p1 fill:#1f6feb,stroke:#0b3d91,color:#fff;
    classDef p2 fill:#6f42c1,stroke:#3f2374,color:#fff;
    classDef ext fill:#1a7f37,stroke:#0b4a20,color:#fff;
    class orch,llmc,dret p1;
    class br,corpus p2;
    class cache,pg,prov ext;
```

Process boundaries double as isolation boundaries. The bridge speaks one JSON object per
line and nothing else; its stdout is reserved for protocol replies, and every noisy
library (HuggingFace, tqdm) is redirected to stderr so it cannot corrupt the stream.
`HF_HUB_OFFLINE=1` is set on the bridge so a model-hub reachability check never blocks a
query.

## The pipeline, stage by stage

`run_pipeline(cfg)` in `pipeline.py` is the whole control flow. Each row below is a real
function boundary with a fixed contract.

```mermaid
sequenceDiagram
    autonumber
    participant P as pipeline
    participant I as ingest
    participant X as extract
    participant G as grounding
    participant S as security
    participant R as reconcile
    participant E as entitlement
    participant D as datum bridge
    participant B as benchmark
    participant C as position (compose)
    participant O as report

    P->>I: load_registry(cfg)
    I-->>P: {source_id: SourceDoc}  (sha256, trust tier, parsed text)
    P->>X: run_extraction(registry, ledger, client)
    X->>X: structured sources -> facts (deterministic)
    X->>X: text + vision sources -> facts (LLM, quoted)
    X-->>P: ledger populated (~168 facts)
    P->>S: scan_registry(registry)
    S-->>P: security findings (injection, unicode, smuggled text)
    P->>R: run_reconciliation(ledger, registry)
    R-->>P: discrepancies, gaps, demand lines, derived facts
    P->>E: run_entitlement(ledger, registry, demand_lines, retriever, client)
    E->>D: search(clause queries)
    D-->>E: ranked clauses + plan_id (or insufficient_evidence)
    E->>E: LLM reads clause params (quoted) -> deterministic calculator
    E-->>P: entitlements, contract terms, position numbers
    P->>B: run_benchmark(ledger, registry)
    B-->>P: comparables + cohort stats
    P->>G: verify_fact_citations(ledger, registry)
    G-->>P: citation QA (quarantine failures)
    P->>C: compose_position(...)  (LLM + NumberGuard + repair)
    C-->>P: brief sections + draft reply, or fail-closed
    P->>O: write_outputs(case)
    O-->>P: case_file.json · brief.html · brief.md · draft_reply.txt
```

Stage contracts, precisely:

| # | Stage | Input | Output | Determinism |
|---|---|---|---|---|
| 1 | `ingest.load_registry` | `cfg.pack_dir` + `MANIFEST` | `dict[str, SourceDoc]` with sha256, trust tier, parsed text/segments | Pure Python (pypdf, stdlib eml/json/csv/openpyxl) |
| 2 | `extract.run_extraction` | registry, ledger, LLM client | facts added to ledger | Structured sources deterministic; text and image sources LLM, schema-forced, one quote per field |
| - | `security.scan_registry` | registry (incl. vision transcripts) | list of `SecurityFinding` | Pure Python regex/codepoint scan |
| 4 | `reconcile.run_reconciliation` | ledger, registry | `discrepancies`, `gaps`, `demand_lines`, derived facts | 14 pure-Python rules, no LLM |
| 5 | `entitlement.run_entitlement` | ledger, registry, demand lines, retriever, LLM client | `entitlements`, `contract_terms`, position numbers | Retrieval + LLM clause read; caps/deadlines/math deterministic |
| 6 | `benchmark.run_benchmark` | ledger, registry | `comparables`, `cohorts` | Pure Python similarity and stats |
| 3 | `grounding.verify_fact_citations` | ledger, registry | citation QA, quarantines | Pure Python quote match |
| 7 | `position.compose_position` | assembled case input | brief sections + draft reply | LLM behind NumberGuard + reference check + bounded repair |
| - | `report.write_outputs` | the case dict | four files | Pure Python rendering |

(The numbering follows the conceptual order in the README; in code the citation
verification runs just before composition so it can catch quotes added by every prior
stage.)

## The three deterministic guards

None of the guards ask the model to police itself; all three are Python.

**Guard 1, the quote gate** (`grounding.verify_fact_citations`, `util.find_quote`). For
each `EXTRACTED` fact, the cited quote is normalized (NFKC, typographic folding,
whitespace collapse, casefold) and matched against the source text. Exact normalized
substring first; otherwise a bounded fuzzy sliding-window ratio to absorb PDF-extraction
artifacts. A fact whose every citation fails is quarantined: `confidence` drops to 0 and
it is excluded from all downstream reasoning and from the NumberGuard allow-list.

**Guard 2, the adversarial-input scanner** (`security.scan_registry`). Runs on every
source's native text and its vision transcript, plus any text layer found where none
should exist. Five deterministic classes:

| Class | Severity | Catches |
|---|---|---|
| `instruction_override` | HIGH | "ignore/disregard/override ... instructions/prompt/context" |
| `role_marker` | HIGH | `<|...|>`, `[INST]`, `system:` lines, "system note/override" |
| `ai_directive` | MEDIUM | "note/message/instructions to the AI/assistant/LLM", "you are an AI" |
| `invisible_unicode` | HIGH | zero-width and bidi control codepoints |
| `encoded_blob` | LOW | long base64-like runs |

Plus `unexpected_text_layer`: a text layer on a source registered as image-only is
recorded and flagged, and deliberately not adopted as citable text (the vision transcript
stays canonical). Patterns are tuned against the real pack so ordinary freight language
stays quiet.

**Guard 3, NumberGuard** (`grounding.build_allowed_tokens`, `scan_generated_text`). The
same tokenizer builds an allow-list of numbers, dates, times and percents from the
non-quarantined ledger values and scans the generated brief and reply. Any token that
cannot be derived from the ledger is a violation. Violations are fed back for a bounded
repair; if the output still fails, composition fails closed and the written prose is
withheld while the deterministic sections still render.

```mermaid
flowchart LR
    src["untrusted sources"] -->|extract| f["EXTRACTED facts + quotes"]
    f --> qg{"Guard 1<br/>quote in source?"}
    qg -->|no| quar["quarantine<br/>(excluded from reasoning)"]
    qg -->|yes| led["fact ledger"]
    src --> sc{"Guard 2<br/>adversarial scan"}
    sc --> panel["security panel<br/>(findings surfaced)"]
    led --> comp["LLM composition"]
    comp --> ng{"Guard 3<br/>every number in ledger?"}
    ng -->|no, after repair| closed["fail closed<br/>(prose withheld)"]
    ng -->|yes| brief["brief + reply"]

    classDef guard fill:#6f42c1,stroke:#3f2374,color:#fff;
    classDef bad fill:#c93c37,stroke:#7d211d,color:#fff;
    class qg,sc,ng guard;
    class quar,closed bad;
```

## The datum bridge protocol

`retrieval.DatumRetriever` manages one long-lived bridge subprocess and talks to it with
five verbs. The heavy cost (loading the embedder and cross-encoder on CPU) is paid once
at `open`, then queries are warm.

| Verb | Request | Reply |
|---|---|---|
| `open` | dsn, namespace, principal, abstain_floor | ok + info |
| `ingest` | list of `{source_id, markdown}` | ok + write-op count |
| `search` | query, k | status, sufficiency, plan_id, hits[] (content, section_path, score) |
| `explain` | plan_id | the compiled plan text (audit) |
| `close` | - | ok |

The client reads replies with a `selectors`-based loop and a per-verb timeout
(`startup_timeout_s=240` for `open`, `request_timeout_s=120` for the rest), drains stderr
on a daemon thread for diagnostics, and raises `RetrieverUnavailable` on a dead process so
the factory can fall back to FTS5 loudly. Chunks are ingested as markdown whose headings
become datum's section paths, so a hit's provenance lines up with the app's citation
locators. datum ranks at sub-section span precision, so each hit is mapped back to its
full canonical section before the LLM reads it, with the matched span kept for audit.

## The LLM provider and cache layer

`llm.LLMClient` is cache-first, provider-second, with schema validation and a bounded
repair loop around every structured call.

```mermaid
flowchart TD
    call["client.call(LLMRequest)"] --> key["cache key =<br/>sha256(model, system, prompt,<br/>attachment sha256s, schema)"]
    key --> hit{"cache hit?"}
    hit -->|yes| ret["return cached (cost 0)"]
    hit -->|no| prov["provider.complete()"]
    prov --> anthropic["AnthropicAPIProvider<br/>urllib · document/image blocks<br/>tool-forced JSON · ret/backoff"]
    prov --> cli["ClaudeCLIProvider<br/>claude -p headless · stdin<br/>Read tool for vision"]
    prov --> replay["ReplayProvider<br/>cache-only (CacheMiss if absent)"]
    anthropic --> val
    cli --> val
    val{"schema valid?"} -->|no, < max_repairs| repair["feed errors back"]
    repair --> prov
    val -->|yes| store["write cache + return"]
    val -->|no, exhausted| err["raise LLMError"]

    classDef llmnode fill:#D97757,stroke:#8a3b1e,color:#fff;
    class anthropic,cli,replay llmnode;
```

Because the cache key is derived from request content only (not from which provider
served it), a cache populated by the Claude CLI replays byte-identically under
`--provider replay`. That is what makes evals and CI deterministic and free, and it is
why every LLM call is temperature 0. The `AnthropicAPIProvider` honors `ANTHROPIC_BASE_URL`
so the same code path works against an enterprise gateway or Bedrock-style endpoint.

## Component internals (deep dive)

The sections above describe what each stage does. These describe how the load-bearing
engines work inside, at the level you would need to modify or defend them.

### Adversarial scanning engine

`security.py` is deterministic and stateless. It never asks the model whether text is an
attack; it matches structure. The engine runs three passes over each source and matches a
fixed set of pattern classes, emitting at most one finding per class per source so the
panel stays readable.

```mermaid
flowchart TB
    subgraph inputs["Per source (skip MISSING / UNREADABLE)"]
        t1["native text<br/>(doc.text)"]
        t2["vision transcript<br/>(doc.derived_text)"]
        t3["unexpected text layer<br/>(doc.meta, scans only)"]
    end
    t1 --> scan
    t2 --> scan
    t3 --> flag["emit unexpected_text_layer (HIGH)<br/>never adopted as citable text"]
    t3 --> scan

    subgraph scan["scan_text: match each pattern class"]
        direction TB
        c1["instruction_override (HIGH)<br/>ignore/disregard/override/bypass<br/>+ instructions/prompts/rules/context"]
        c2["role_marker (HIGH)<br/>&lt;|...|&gt; · [INST] · ^role: · 'system note'"]
        c3["ai_directive (MEDIUM)<br/>note/instructions to the AI/LLM/assistant<br/>· 'you are an AI'"]
        c4["invisible_unicode (HIGH)<br/>zero-width + bidi codepoints<br/>U+200B..U+200D U+2060 U+FEFF U+202A..E U+2066..9"]
        c5["encoded_blob (LOW)<br/>base64-like run ≥ 120 chars"]
    end

    scan --> f["SecurityFinding{source_id, kind, severity,<br/>evidence (escaped snippet), location (offset)}"]
    flag --> f
    f --> panel["security panel in the brief<br/>+ CloudWatch metric in prod"]

    classDef guard fill:#6f42c1,stroke:#3f2374,color:#fff;
    classDef bad fill:#c93c37,stroke:#7d211d,color:#fff;
    class c1,c2,c3,c4,c5 guard;
    class flag bad;
```

Three properties are deliberate:

1. **It scans the vision transcript, not just native text.** A payload printed inside a
   scanned image reaches the model through the transcript, so the transcript is scanned
   with the same classes. This is the path a naive scanner misses.
2. **The unexpected-text-layer path is both a finding and an ingest decision.** When a PDF
   registered as image-only carries a text layer (an easy way to feed a parser words the
   eye never sees), `ingest._parse_pdf_scan` records it in `doc.meta` and refuses to adopt
   it as citable text; the scanner then emits a HIGH finding and also runs the pattern
   classes over the hidden text.
3. **Tuning is against real freight language.** The patterns were tightened until ordinary
   phrases in the pack ("Special instructions:", "Transportation Management System", "The
   claimant must provide...") produce zero findings, so a HIGH finding means something. The
   scanner is a tripwire by design, and the real defense is that conclusions are computed,
   not generated, so a missed pattern still cannot move a number.

### Retrieval engine internals

Two layers cooperate: the app-side `DatumRetriever` (transport and provenance alignment)
and datum's own compiled-query plan (the retrieval algorithm). A single `search` call
flows through both.

```mermaid
flowchart TB
    q["clause query or ask question"] --> dr["DatumRetriever.search"]
    dr -->|JSON-lines RPC| plan

    subgraph plan["datum compiled plan (inside the bridge)"]
        direction TB
        acl["resolve namespace ACL<br/>(fail closed, before any operator)"]
        acl --> ops
        subgraph ops["run operators, scoped to namespace"]
            grep["grep<br/>literal"]
            bm25["BM25<br/>Postgres full-text"]
            ann["ANN<br/>pgvector HNSW (dense)"]
        end
        ops --> rrf["weighted Reciprocal Rank Fusion<br/>score = sum_o w_o / (k + rank_o(d))"]
        rrf --> rerank["cross-encoder rerank<br/>reads query+candidate together"]
        rerank --> suff{"best dense similarity<br/>≥ abstain_floor (0.50)?"}
        suff -->|no| abstain["status = insufficient_evidence"]
        suff -->|yes| hits["hits: content, section_path,<br/>page, score, span, plan_id"]
    end

    hits --> map["map span → full canonical section<br/>via _chunk_lookup (title key)<br/>keep matched span for audit"]
    abstain --> up["status propagates up:<br/>entitlement records the topic unresolved<br/>ask returns an honest refusal"]
    map --> out["RetrievalHit{source_id, locator, title,<br/>text=full section, score,<br/>extra: hit_id, section_path, matched_span}"]

    classDef det fill:#1a7f37,stroke:#0b4a20,color:#fff;
    classDef guard fill:#6f42c1,stroke:#3f2374,color:#fff;
    class grep,bm25,ann,rrf,rerank det;
    class suff,abstain guard;
```

The details that matter:

- **ACL first, fail closed.** The namespace partition resolves before any operator runs, so
  a relevance change can never become a cross-tenant leak. In production this is the
  per-shipper isolation boundary.
- **Fusion is by rank, not raw score.** A BM25 score and a cosine similarity are not the
  same unit, so datum fuses positions (`w_o / (k + rank)`) rather than mixing scales, then
  a cross-encoder reranks the shortlist by reading the query and each candidate together.
- **Abstention is a real plan step.** If the best dense similarity among the fused
  candidates is below the floor, the plan returns `insufficient_evidence` instead of the
  top-ranked chunk. The floor (0.50) is calibrated in `evals/abstention_sweep.py`. FTS5
  cannot express this, which is the capability gap the benchmark exists to show.
- **Span-to-section mapping is the integration fix.** datum ranks at sub-section span
  precision, so the app maps each hit back to its full canonical section (so the LLM reads
  complete clauses) while keeping the matched span in `extra.matched_span` for the audit
  trail. This is the bug from the datum bridge section, closed here.
- **The plan is replayable.** `plan_id` comes back with the hits and goes into the brief's
  retrieval-audit table; `explain(plan_id)` reconstructs the exact plan for audit.

### Extraction engine: three paths

`extract.run_extraction` routes each source down one of three paths by kind. Structured
data never touches the model; language and images do, always with a quote.

```mermaid
flowchart TB
    reg["registry (15 sources)"] --> router{"source kind"}

    router -->|JSON / CSV / XLSX| det["Deterministic path<br/>parse native structure"]
    det --> detq["quote = the raw JSON line / CSV row<br/>(_json_line_quote)"]
    detq --> f1["FactWriter.det → EXTRACTED / DETERMINISTIC"]

    router -->|invoice / BOL / POD / email / overview| llm["LLM path<br/>schema-forced JSON, one quote per field"]
    llm --> repair["schema validate + up to 2 repairs"]
    repair --> f2["FactWriter.fv → EXTRACTED / LLM<br/>confidence halved if a quote is missing"]

    router -->|scanned PDF / photos| vis["Vision path"]
    vis --> tr["1. full transcript<br/>(becomes the citable derived text)"]
    tr --> fields["2. fields, each quoting the transcript"]
    fields --> verify["3. second pass re-reads the image<br/>confirms key values"]
    verify --> f3["EXTRACTED / LLM_VISION<br/>confidence = read legibility;<br/>disagreement lowers it further"]

    f1 --> led["fact ledger"]
    f2 --> led
    f3 --> led

    classDef det fill:#1a7f37,stroke:#0b4a20,color:#fff;
    classDef llmnode fill:#D97757,stroke:#8a3b1e,color:#fff;
    class det,detq,f1 det;
    class llm,repair,vis,tr,fields,verify llmnode;
```

The vision path is the most defended because it is the least trustworthy input. The
transcript is produced first and becomes the source's citable text, so field quotes cite
the transcript rather than an invisible original; a second pass re-reads the image to
confirm the key numbers, and any disagreement drops the fact's confidence. Structured
extraction, by contrast, is a pure function: a JSONPath or row index and the raw line as
the quote, no model in the loop.

### Reconciliation engine: anatomy of a rule

The 14 rules share one context object and one shape: read facts, derive new facts with a
formula, raise discrepancies or gaps, degrade if inputs are missing. A rule that throws is
caught so one bug cannot take down the run.

```mermaid
flowchart TB
    subgraph ctx["Ctx (shared)"]
        led["ledger"]
        reg["registry"]
    end
    rule["rule r02_piece_counts"] --> guard{"has(tms.pieces_tendered,<br/>pod.received_cartons)?"}
    guard -->|no| skip["skip() with a note<br/>(graceful degradation)"]
    guard -->|yes| read["read facts:<br/>tendered=60, received=58, edi=59"]
    read --> derive["derive('derived.shortage_cartons', 2,<br/>formula='60 - 58', inputs=[F.., F..])"]
    read --> conflict{"edi != received?"}
    conflict -->|yes| disc["disc(HIGH, COUNT_CONFLICT,<br/>authority_note='signed POD governs',<br/>cite dd.pod_authority + dd.edi_semantics)"]
    derive --> out["new DERIVED facts + Discrepancy/Gap objects"]
    disc --> out

    runner["run_reconciliation:<br/>for each rule: try/except, log, continue"] -.-> rule

    classDef det fill:#1a7f37,stroke:#0b4a20,color:#fff;
    classDef bad fill:#c93c37,stroke:#7d211d,color:#fff;
    class read,derive,out det;
    class disc,conflict bad;
```

Three mechanisms carry the correctness story:

- **Authority rulings are data, not opinion.** When sources disagree, the rule cites the
  data-dictionary facts (`dd.pod_authority`, `dd.edi_semantics`) that assign authority, and
  records both the ruling and the conflict. The brief shows the disagreement; it does not
  hide it behind the answer.
- **Every derived value carries its formula and inputs.** `ctx.derive(...)` writes a
  DERIVED fact with a human-readable formula and the fact_ids it consumed, which is what
  makes the provenance tree in doc 02 walkable.
- **Degradation is per rule.** A missing input calls `skip()` with a note instead of
  crashing; an unexpected exception is caught by the runner, logged, and the run continues
  with the other rules. Reconciliation is best-effort and additive by construction.

### Entitlement calculator

Stage 5 is a retrieve-read-compute sandwich: retrieval and one LLM read sit between two
deterministic ends, and only the deterministic ends touch money.

```mermaid
flowchart TB
    dl["demand lines (from reconcile)"] --> rc["retrieve_clauses<br/>8 topics, primary + fallback query<br/>abstention → record unresolved"]
    rc --> et["extract_terms (LLM)<br/>read params from retrieved text,<br/>each quoted + verified vs agreement"]
    et --> params["contract facts:<br/>cap $50/lb, notice 9mo/30d,<br/>delay excluded, salvage required"]

    params --> calc

    subgraph calc["deterministic calculator"]
        direction TB
        cap["_cap_math per line:<br/>min(units x price,<br/>units x weight x $50/lb)"]
        dead["check_timeliness:<br/>_add_months(delivered, 9) etc.<br/>filed ≤ deadline?"]
        cls["classify each line:<br/>STRONG / MODERATE / NEEDS_INFO /<br/>EXCLUDED_CONTRACTUAL / GOODWILL_LEVER"]
    end

    calc --> pos["compute_position_numbers:<br/>core_low, core_high,<br/>goodwill_high, recommended_counter,<br/>expected band, reserve check"]
    pos --> led["DERIVED facts + Entitlement objects"]

    classDef det fill:#1a7f37,stroke:#0b4a20,color:#fff;
    classDef llmnode fill:#D97757,stroke:#8a3b1e,color:#fff;
    class cap,dead,cls,pos det;
    class et llmnode;
```

The calculator is where the case's judgment is encoded as arithmetic:

- **Cap math is `min(invoice value, $50/lb x weight)` per line**, with both intermediate
  values written to the ledger so the brief can show why invoice value governs (it does
  here: 22 affected units at ~15 lb gives a $16,500 cap against a $9,350 invoice value).
- **Deadlines are computed, not assumed.** `_add_months` and date arithmetic turn the
  delivery date and the notice periods into concrete deadlines, then check the filing date
  against them.
- **Classification is a decision table**, not a model call. A line is EXCLUDED_CONTRACTUAL
  when the contract excludes it and the service was non-guaranteed (the markdown);
  GOODWILL_LEVER when it is not owed but precedent pays it (the freight refund); NEEDS_INFO
  when recoverability turns on a missing fact (repack labor). The label drives which lines
  sum into `core_high` and which become negotiation levers.

### NumberGuard tokenizer

The guard's strength is that one tokenizer builds the allow-list and scans the output, so
the two can never disagree about what a number is.

```mermaid
flowchart TB
    subgraph build["build allow-list (from the ledger)"]
        vals["non-quarantined fact values<br/>+ extras (entitlements, comps)"]
        vals --> tok1["collect_tokens: strip id-like tokens,<br/>split ISO timestamps,<br/>extract num/date/time/pct, canonicalize"]
        tok1 --> widen["_widen: date without year,<br/>pct roundings, integer form of x.00"]
        widen --> allow[("allowed token set<br/>{(kind, canonical)}")]
    end

    subgraph scanpass["scan generated text (same tokenizer)"]
        gen["brief + draft reply"] --> tok2["collect_tokens"]
        tok2 --> check{"each token in allowed?"}
        check -->|small int <= 10| pass1["pass (prose counts)"]
        check -->|in set| pass2["pass"]
        check -->|no| viol["GuardViolation{kind, token, context}"]
    end

    allow --> check
    viol --> repair["feed violations back, bounded repair"]
    repair -->|still failing| closed["fail closed:<br/>withhold prose, render deterministic sections"]
    viol -. also .-> refs["check_fact_refs: every [F-x]/[E-x]/[D-x]<br/>must resolve; draft reply carries none"]

    classDef guard fill:#6f42c1,stroke:#3f2374,color:#fff;
    classDef bad fill:#c93c37,stroke:#7d211d,color:#fff;
    class tok1,tok2,widen,check guard;
    class viol,closed bad;
```

Mechanics worth knowing:

- **Identifier tokens are stripped before number scanning**, so `BLF-77209115` or `[F-217]`
  never registers as a numeric claim; only genuine figures, dates, times and percents are
  checked.
- **The allow-list is widened conservatively**, so benign renderings pass: a date written
  without its year, a percentage rounded to one decimal, an `x.00` money value shown as an
  integer. This keeps the guard from failing on legitimate phrasing while still catching a
  fabricated figure.
- **Small integers pass unconditionally.** Counts like "5 cartons" or "2 photos" and
  section numbers are prose, not claims, so integers at or below a ceiling are allowed
  without a ledger match.
- **Two checks, not one.** Alongside the number scan, `check_fact_refs` confirms every
  bracketed reference resolves and that the draft reply body carries none (it must read as a
  real email). A failure in either path drives the same repair-then-fail-closed loop.

## Failure and degradation matrix

The design rule is that deterministic output never depends on an LLM stage succeeding, and
a missing source degrades rather than aborts.

| Failure | Detected by | Behavior |
|---|---|---|
| Source file missing or unreadable | `ingest.load_source` | registered `MISSING`/`UNREADABLE`; rules skip with a note; gaps raised |
| Scanned PDF has no text layer | `ingest._parse_pdf_scan` | expected; vision transcribes it |
| Unexpected text layer on a scan | same | flagged to the security panel, not adopted |
| LLM returns invalid JSON | `llm.LLMClient` | up to 2 schema repairs, then `LLMError` |
| Extracted quote not in source | Guard 1 | fact quarantined, excluded from reasoning |
| datum venv or Postgres absent | `retrieval.make_retriever` | loud fallback to FTS5 (degraded: lexical only, no abstention) |
| Clause topic has no supporting text | datum abstention | reported as unresolved, not filled with the nearest clause |
| Generated prose has an ungrounded number | Guard 3 | repair, then fail closed (deterministic sections still render) |
| A reconciliation rule throws | `run_reconciliation` | caught per-rule, logged, run continues |
| Adversarial content in a document | Guard 2 | surfaced in the security panel; conclusions unaffected (computed, not generated) |

Next: [02 - Scenario and swimlanes](02-scenario-and-swimlanes.md).
