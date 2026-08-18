"""JSON-lines bridge to datum, executed with datum's own venv python (3.12).

The main application targets the system Python 3.9; datum requires 3.11+.
Rather than entangling interpreter environments, datum runs as a tiny
out-of-process service - which is also how it would deploy for real (its
native surface is an MCP server). Protocol: one JSON request per stdin line,
one JSON response per stdout line.

This file must not import anything from claimpilot (different interpreter).
"""

from __future__ import annotations

import json
import sys
import warnings


def main() -> None:
    # Anything that prints (HF hub, tqdm, datum warnings) must not corrupt
    # the protocol stream: reserve the real stdout for responses.
    real_out = sys.stdout
    sys.stdout = sys.stderr
    warnings.filterwarnings("ignore")

    corpus = None
    principal = None
    corpus_cm = None

    def reply(payload):
        real_out.write(json.dumps(payload) + "\n")
        real_out.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            op = req.get("op")
            if op == "open":
                from datum import Corpus
                from datum.kernel.principal import Principal
                principal = Principal(id=req["principal"], namespace=req["namespace"])
                kwargs = {}
                if req.get("abstain_floor") is not None:
                    kwargs["abstain_min_similarity"] = float(req["abstain_floor"])
                corpus_cm = Corpus.open(req["dsn"], **kwargs)
                corpus = corpus_cm.__enter__() if hasattr(corpus_cm, "__enter__") else corpus_cm
                reply({"ok": True, "info": "corpus open, namespace={}, abstain_floor={}"
                       .format(req["namespace"], req.get("abstain_floor", "default"))})
            elif op == "ingest":
                ops = 0
                for doc in req["docs"]:
                    ops += corpus.ingest(doc["source_id"], doc["markdown"],
                                         principal=principal)
                reply({"ok": True, "ops": ops})
            elif op == "search":
                ev = corpus.search(req["query"], principal=principal)
                hits = []
                for h in ev.hits[: req.get("k", 5)]:
                    hits.append({
                        "hit_id": h.hit_id,
                        "source_path": h.source_path,
                        "section_path": list(h.section_path),
                        "page": h.page,
                        "score": h.score,
                        "content": h.content,
                    })
                reply({"ok": True, "status": str(ev.status),
                       "sufficiency": round(float(ev.sufficiency), 4),
                       "plan_id": ev.plan_id, "hits": hits})
            elif op == "explain":
                text = corpus.explain(req["plan_id"], principal=principal)
                reply({"ok": True, "text": text})
            elif op == "close":
                reply({"ok": True})
                break
            else:
                reply({"ok": False, "error": "unknown op {!r}".format(op)})
        except Exception as exc:  # report, never crash the protocol
            reply({"ok": False, "error": "{}: {}".format(type(exc).__name__, exc)})

    if corpus_cm is not None and hasattr(corpus_cm, "__exit__"):
        try:
            corpus_cm.__exit__(None, None, None)
        except Exception:
            pass


if __name__ == "__main__":
    main()
