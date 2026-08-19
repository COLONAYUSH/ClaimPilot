# ClaimPilot architecture

This folder is the engineering reference for ClaimPilot. It goes past the top-level
README and into how every capability is wired, how a request moves end to end, and what
the same system looks like when it runs in production on AWS.

Read them in this order:

| Doc | What it covers |
|---|---|
| [01 - System architecture](01-system-architecture.md) | The current codebase at the component level. Module graph, the fact-ledger data model, every pipeline stage's inputs and outputs, the three deterministic guards, the datum bridge protocol, the LLM provider and cache layer, and the failure/degradation matrix. |
| [02 - Scenario and swimlanes](02-scenario-and-swimlanes.md) | One claim (FCL-2026-0147) traced through the whole pipeline with real data, as a swimlane and a sequence diagram. Then the adversarial version of the same claim, showing detection and integrity. Trigger points enumerated. |
| [03 - Production architecture](03-production-architecture.md) | The AWS design. Intake triggers, the worker fleet, datum as a service, Bedrock for the model, storage and state, the network and trust boundaries, the security-first CI/CD gates, observability, and the scaling math with a worker-count table. |

All diagrams are Mermaid, so they render on GitHub. Colors are set explicitly, so they
hold in light and dark themes.

## The one-paragraph version

ClaimPilot reads a folder of untrusted claim documents and produces a negotiation
position brief. The LLM is used only to turn language into typed facts and to write
prose. Everything that has a correct answer (arithmetic, source-authority rulings,
liability caps, deadlines, similarity) is deterministic Python, and every figure it
computes lands in a fact ledger with its formula and source citations. Three
deterministic guards sit between untrusted input and output: a quote gate that verifies
every extracted quote against its source, an adversarial-input scanner, and a
NumberGuard that withholds any generated prose it cannot trace to the ledger. Retrieval
runs on datum, a compiled-query substrate with typed abstention. The whole run is
content-addressed and replayable.

## The views, at a glance

- **Module / component view**: nine layers of `claimpilot/`, one-directional imports,
  the `Corpus`-style composition in `pipeline.py`. See doc 01.
- **Data view**: the `FactLedger` and the three fact registers (extracted, asserted,
  derived), provenance to the span. See doc 01.
- **Control-flow view**: the seven-stage pipeline plus the scan guard, as a sequence.
  See docs 01 and 02.
- **Process / network view**: the main Python 3.9 process, the datum subprocess bridge
  on 3.12, Postgres, and the model provider. See doc 01.
- **Security view**: trust boundaries, the threat table, the guards. See docs 01 and 03.
- **Deployment view**: the AWS production topology. See doc 03.
