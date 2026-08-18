"""Retrieval: one protocol, two conformant backends, honest selection.

Backends
  datum  - the primary backend: Ayush's compiled-query retrieval substrate
           (hybrid grep+BM25+ANN with RRF fusion and cross-encoder rerank over
           Postgres/pgvector). It is used for capabilities the baseline
           structurally lacks and this domain needs:
             * typed abstention  - `insufficient_evidence` instead of
               confidently returning the wrong clause,
             * span/section provenance on every hit, which feeds citations,
             * replayable, explainable plans (plan_id -> explain/replay),
               which go into the brief's audit appendix,
             * fail-closed namespace isolation (a real multi-shipper claims
               org requirement).
           It runs out-of-process under its own 3.12 venv via a JSON-lines
           bridge, because this app targets the system Python 3.9.
  fts5   - stdlib SQLite FTS5 BM25. Zero infrastructure; the fallback when
           datum's venv or Postgres is absent. It cannot abstain: an empty
           result is the only "no".

Selection is loud, never silent: `auto` prefers datum and logs exactly what
was missing when it falls back. `evals/retrieval_bench.py` measures both
backends on a gold query set, including unanswerable probes.

Both backends index the same chunks (built here from the source registry), so
comparisons are apples-to-apples and citations map back to the same locators.
"""

from __future__ import annotations

import json
import logging
import os
import re
import selectors
import sqlite3
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import RunConfig
from .models import SourceDoc

log = logging.getLogger("claimpilot.retrieval")


@dataclass
class Chunk:
    chunk_id: str
    source_id: str
    locator: str
    title: str
    text: str


@dataclass
class RetrievalHit:
    source_id: str
    locator: str
    title: str
    text: str
    score: Optional[float] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievalResult:
    status: str                  # ok | insufficient_evidence | empty
    hits: List[RetrievalHit]
    backend: str
    plan_id: str = ""
    sufficiency: Optional[float] = None
    elapsed_ms: int = 0

    @property
    def answered(self) -> bool:
        return self.status == "ok" and bool(self.hits)


class RetrieverUnavailable(Exception):
    pass


# ------------------------------------------------------------------ chunking

_SECTION_RE = re.compile(r"^\s*(\d\.\s+[A-Z][^\n]*?)\s*$", re.MULTILINE)


def build_chunks(registry: Dict[str, SourceDoc]) -> List[Chunk]:
    """Shared content units for both backends. Contract splits by numbered
    section; email by message; everything else is one chunk per source
    (documents here are single-page). Vision transcripts participate once
    the extraction stage has attached them."""
    chunks: List[Chunk] = []

    def add(source_id: str, locator: str, title: str, text: str) -> None:
        if text and text.strip():
            chunks.append(Chunk(
                chunk_id="{}::{}".format(source_id, locator),
                source_id=source_id, locator=locator, title=title, text=text.strip()))

    for sid, doc in registry.items():
        if doc.status == "MISSING" or not doc.citable_text.strip():
            continue
        if sid == "carrier_agreement":
            text = doc.text
            heads = list(_SECTION_RE.finditer(text))
            if heads:
                preamble = text[:heads[0].start()].strip()
                add(sid, "preamble", "Agreement header", preamble)
                for i, m in enumerate(heads):
                    end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
                    add(sid, "section:{}".format(m.group(1).split(".")[0]),
                        m.group(1).strip(), text[m.start():end])
            else:
                add(sid, "document", doc.description, text)
        elif sid == "email_thread":
            for seg in doc.segments:
                add(sid, seg.locator, "Email {} - {}".format(
                    seg.locator.split(":")[1], seg.title), seg.text)
        elif sid == "historical_claims":
            for seg in doc.segments:
                add(sid, seg.locator, "Historical claim {}".format(seg.title), seg.text)
        elif sid == "historical_claims_xlsx":
            continue   # CSV twin is canonical; indexing both would double-count
        else:
            add(sid, "document", doc.description, doc.citable_text)
    return chunks


def chunks_to_markdown(chunks: List[Chunk], source_id: str, doc_title: str) -> str:
    """Render one source's chunks as markdown whose headings become datum's
    section_path - keeping both backends' provenance aligned."""
    parts = ["# {}".format(doc_title)]
    for ch in chunks:
        if ch.source_id != source_id:
            continue
        parts.append("## {}\n\n{}".format(ch.title, ch.text))
    return "\n\n".join(parts)


# ------------------------------------------------------------- FTS5 baseline

class FTS5Retriever:
    name = "fts5"
    can_abstain = False

    def __init__(self) -> None:
        self._db = sqlite3.connect(":memory:")
        self._db.execute(
            "CREATE VIRTUAL TABLE chunks USING fts5"
            "(chunk_id UNINDEXED, source_id UNINDEXED, locator UNINDEXED, "
            "title, text, tokenize='porter unicode61')")
        self._by_id: Dict[str, Chunk] = {}

    def index(self, chunks: List[Chunk]) -> None:
        self._db.executemany(
            "INSERT INTO chunks(chunk_id, source_id, locator, title, text) VALUES (?,?,?,?,?)",
            [(c.chunk_id, c.source_id, c.locator, c.title, c.text) for c in chunks])
        self._db.commit()
        self._by_id = {c.chunk_id: c for c in chunks}

    def search(self, query: str, k: int = 5) -> RetrievalResult:
        started = time.time()
        words = re.findall(r"[A-Za-z0-9]+", query)
        if not words:
            return RetrievalResult("empty", [], self.name)
        match = " OR ".join('"{}"'.format(w) for w in words)
        rows = self._db.execute(
            "SELECT chunk_id, bm25(chunks) FROM chunks WHERE chunks MATCH ? "
            "ORDER BY bm25(chunks) LIMIT ?", (match, k)).fetchall()
        hits = []
        for chunk_id, score in rows:
            ch = self._by_id[chunk_id]
            hits.append(RetrievalHit(source_id=ch.source_id, locator=ch.locator,
                                     title=ch.title, text=ch.text, score=round(score, 4)))
        return RetrievalResult("ok" if hits else "empty", hits, self.name,
                               elapsed_ms=int((time.time() - started) * 1000))

    def close(self) -> None:
        self._db.close()


# ------------------------------------------------------------- datum client

class DatumRetriever:
    """Client for the out-of-process datum bridge (claimpilot/datum_bridge.py
    executed with datum's own venv python). One resident process per run;
    model load is paid once."""

    name = "datum"
    can_abstain = True

    def __init__(self, cfg: RunConfig) -> None:
        self.cfg = cfg
        self._proc: Optional[subprocess.Popen] = None
        self._sel: Optional[selectors.BaseSelector] = None
        self._stderr_tail: deque = deque(maxlen=40)
        self._buf = b""
        self._chunk_lookup: Dict[str, Chunk] = {}
        if not cfg.datum.python or not Path(cfg.datum.python).exists():
            raise RetrieverUnavailable(
                "datum venv python not found (set DATUM_PYTHON); looked at {!r}"
                .format(cfg.datum.python))

    # -- process management ------------------------------------------------
    def _ensure_db(self) -> None:
        db = self.cfg.datum.dsn.rsplit("/", 1)[-1]
        subprocess.run(["createdb", db], capture_output=True)
        subprocess.run(["psql", "-d", db, "-q", "-c", "CREATE EXTENSION IF NOT EXISTS vector;"],
                       capture_output=True)

    def _start(self) -> None:
        self._ensure_db()
        bridge = str(Path(__file__).resolve().parent / "datum_bridge.py")
        env = dict(os.environ)
        env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                    "TOKENIZERS_PARALLELISM": "false"})
        self._proc = subprocess.Popen(
            [self.cfg.datum.python, bridge], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self._sel = selectors.DefaultSelector()
        self._sel.register(self._proc.stdout, selectors.EVENT_READ)
        reply = self._rpc({"op": "open", "dsn": self.cfg.datum.dsn,
                           "namespace": self.cfg.datum.namespace,
                           "principal": self.cfg.datum.principal_id,
                           "abstain_floor": self.cfg.datum.abstain_floor},
                          timeout=self.cfg.datum.startup_timeout_s)
        log.info("datum bridge up: %s", reply.get("info", ""))

    def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for line in iter(self._proc.stderr.readline, b""):
            self._stderr_tail.append(line.decode("utf-8", "replace").rstrip())

    def _rpc(self, payload: Dict[str, Any], timeout: int) -> Dict[str, Any]:
        if self._proc is None or self._proc.poll() is not None:
            raise RetrieverUnavailable("datum bridge process is not running: {}".format(
                " | ".join(list(self._stderr_tail)[-3:])))
        assert self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        self._proc.stdin.flush()
        deadline = time.time() + timeout
        while b"\n" not in self._buf:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise RetrieverUnavailable("datum bridge timed out on {!r}".format(payload["op"]))
            if self._sel.select(timeout=min(remaining, 1.0)):
                data = os.read(self._proc.stdout.fileno(), 65536)
                if not data:
                    raise RetrieverUnavailable(
                        "datum bridge closed unexpectedly: {}".format(
                            " | ".join(list(self._stderr_tail)[-3:])))
                self._buf += data
        line, self._buf = self._buf.split(b"\n", 1)
        reply = json.loads(line.decode("utf-8"))
        if not reply.get("ok"):
            raise RetrieverUnavailable("datum bridge error: {}".format(reply.get("error")))
        return reply

    # -- public API ---------------------------------------------------------
    def index(self, chunks: List[Chunk]) -> None:
        if self._proc is None:
            self._start()
        self._chunk_lookup = {}
        docs = []
        by_source: Dict[str, List[Chunk]] = {}
        for ch in chunks:
            by_source.setdefault(ch.source_id, []).append(ch)
            self._chunk_lookup[_title_key(ch.source_id, ch.title)] = ch
        for source_id, source_chunks in by_source.items():
            docs.append({"source_id": source_id,
                         "markdown": chunks_to_markdown(source_chunks, source_id, source_id)})
        reply = self._rpc({"op": "ingest", "docs": docs},
                          timeout=self.cfg.datum.startup_timeout_s)
        log.info("datum indexed %d sources (%s write ops)", len(docs), reply.get("ops"))

    def search(self, query: str, k: int = 5) -> RetrievalResult:
        started = time.time()
        reply = self._rpc({"op": "search", "query": query, "k": k},
                          timeout=self.cfg.datum.request_timeout_s)
        hits: List[RetrievalHit] = []
        for h in reply.get("hits", [])[:k]:
            section_path = h.get("section_path") or []
            title = section_path[-1] if section_path else h.get("source_path", "")
            chunk = self._chunk_lookup.get(_title_key(h.get("source_path", ""), title))
            # datum ranks at sub-section span precision; consumers get the full
            # canonical chunk for that section (complete clauses to read/quote),
            # with the matched span preserved for the audit trail.
            hits.append(RetrievalHit(
                source_id=h.get("source_path", ""),
                locator=(chunk.locator if chunk else "section_path:" + " > ".join(section_path)),
                title=title,
                text=(chunk.text if chunk else h.get("content", "")),
                score=h.get("score"),
                extra={"hit_id": h.get("hit_id", ""), "section_path": section_path,
                       "matched_span": h.get("content", "")}))
        return RetrievalResult(
            status=reply.get("status", "ok"), hits=hits, backend=self.name,
            plan_id=reply.get("plan_id", ""), sufficiency=reply.get("sufficiency"),
            elapsed_ms=int((time.time() - started) * 1000))

    def explain(self, plan_id: str) -> str:
        return self._rpc({"op": "explain", "plan_id": plan_id},
                         timeout=self.cfg.datum.request_timeout_s).get("text", "")

    def close(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._rpc({"op": "close"}, timeout=10)
            except RetrieverUnavailable:
                pass
            self._proc.terminate()


def _title_key(source_id: str, title: str) -> str:
    return "{}::{}".format(source_id, re.sub(r"\W+", " ", title).strip().lower())


# ------------------------------------------------------------------- factory

def make_retriever(cfg: RunConfig, chunks: List[Chunk]):
    """Build and index the configured backend. `auto` prefers datum and falls
    back loudly - the fallback reason is recorded for the run log."""
    requested = cfg.retrieval_backend
    note = ""
    retriever = None
    if requested in ("auto", "datum"):
        try:
            retriever = DatumRetriever(cfg)
            retriever.index(chunks)
        except RetrieverUnavailable as exc:
            retriever = None
            note = str(exc)
            if requested == "datum":
                raise
            log.warning("datum unavailable, falling back to FTS5 (this is a DEGRADED "
                        "retrieval mode - no abstention, lexical-only): %s", exc)
    if retriever is None:
        retriever = FTS5Retriever()
        retriever.index(chunks)
    return retriever, note
