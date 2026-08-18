"""Ingestion: deterministic parsers for every source kind, plus the registry.

Parsing here is intentionally boring. No LLM touches this layer: structured
records (JSON/CSV/XLSX) are parsed natively, text-native PDFs go through
pypdf, and the email thread is split with the stdlib email package. Scanned
PDFs and photos are only *registered* here; their transcripts are produced by
the vision extraction stage and attached as derived text.

A missing or unreadable file never aborts the run - the source is registered
with a MISSING/UNREADABLE status and downstream stages degrade explicitly
(that path is exercised by the ablation eval).
"""

from __future__ import annotations

import csv
import email
import email.utils
import io
import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

from .config import RunConfig, SourceSpec, MANIFEST
from .models import Segment, SourceDoc
from .util import sha256_file

log = logging.getLogger("claimpilot.ingest")

_MSG_SEPARATOR = re.compile(r"^-{3,}\s*Original Message\s*-{3,}\s*$", re.MULTILINE | re.IGNORECASE)
_HEADER_LINE = re.compile(r"^(From|To|Date|Subject):\s*(.*)$")


def _parse_email(doc: SourceDoc) -> None:
    raw = Path(doc.path).read_bytes()
    msg = email.message_from_bytes(raw)
    payload = msg.get_payload(decode=True)
    body = payload.decode(msg.get_content_charset() or "utf-8") if payload else ""
    doc.text = body
    doc.meta["envelope"] = {
        "from": msg.get("From", ""), "to": msg.get("To", ""),
        "date": msg.get("Date", ""), "subject": msg.get("Subject", ""),
    }
    blocks = _MSG_SEPARATOR.split(body)
    messages = []
    for block in blocks:
        block = block.strip("\n")
        if not block.strip():
            continue
        headers: Dict[str, str] = {}
        body_lines: List[str] = []
        in_headers = True
        for line in block.splitlines():
            m = _HEADER_LINE.match(line.strip()) if in_headers else None
            if m and in_headers:
                headers[m.group(1).lower()] = m.group(2).strip()
            else:
                if line.strip() or not in_headers:
                    in_headers = False
                    body_lines.append(line)
        sent_at = ""
        if headers.get("date"):
            try:
                sent_at = email.utils.parsedate_to_datetime(headers["date"]).isoformat()
            except (TypeError, ValueError):
                sent_at = headers["date"]
        sender = headers.get("from", "")
        role = ("shipper" if "northstar" in sender.lower()
                else "carrier" if "blueline" in sender.lower() else "unknown")
        messages.append({
            "headers": headers, "sent_at": sent_at, "role": role,
            "body": "\n".join(body_lines).strip(),
        })
    messages.sort(key=lambda m: m["sent_at"])
    doc.meta["messages"] = messages
    for i, m in enumerate(messages, start=1):
        title = "{} ({}) {}".format(
            m["headers"].get("from", "?"), m["role"], m["sent_at"][:10])
        doc.segments.append(Segment(locator="message:{}".format(i), text=m["body"], title=title))


def _parse_json(doc: SourceDoc) -> None:
    raw = Path(doc.path).read_text(encoding="utf-8")
    doc.text = raw
    doc.meta["data"] = json.loads(raw)
    doc.segments.append(Segment(locator="document", text=raw, title=doc.filename))


def _parse_csv(doc: SourceDoc) -> None:
    raw = Path(doc.path).read_text(encoding="utf-8")
    doc.text = raw
    reader = csv.DictReader(io.StringIO(raw))
    rows = list(reader)
    doc.meta["rows"] = rows
    doc.meta["fieldnames"] = reader.fieldnames or []
    lines = raw.splitlines()
    for i, row in enumerate(rows, start=1):
        line = lines[i] if i < len(lines) else json.dumps(row)
        doc.segments.append(Segment(locator="row:{}".format(i), text=line,
                                    title=next(iter(row.values()), "")))


def _parse_xlsx(doc: SourceDoc) -> None:
    try:
        import openpyxl  # optional extra; only cross-checks the CSV twin
    except ImportError:
        doc.status = "UNREADABLE"
        doc.meta["error"] = "openpyxl not installed; xlsx cross-check skipped"
        return
    wb = openpyxl.load_workbook(doc.path, data_only=True, read_only=True)
    ws = wb.active
    grid = [["" if c is None else c for c in row] for row in ws.iter_rows(values_only=True)]
    wb.close()
    if not grid:
        doc.status = "UNREADABLE"
        return
    header = [str(h) for h in grid[0]]
    rows = [dict(zip(header, r)) for r in grid[1:]]
    doc.meta["rows"] = rows
    doc.meta["fieldnames"] = header
    doc.text = "\n".join("\t".join(str(c) for c in r) for r in grid)


def _parse_pdf_text(doc: SourceDoc) -> None:
    from pypdf import PdfReader
    reader = PdfReader(doc.path)
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        pages.append(text)
        doc.segments.append(Segment(locator="page:{}".format(i), text=text))
    doc.text = "\n\n".join(pages)
    if doc.kind == "PDF_TEXT" and len(doc.text.strip()) < 50:
        log.warning("%s: expected a text layer but found ~none; treating as scan", doc.source_id)
        doc.kind = "PDF_SCAN"


def _parse_pdf_scan(doc: SourceDoc) -> None:
    # Confirm there is genuinely no text layer; the transcript comes from the
    # vision stage later and lands in derived_text. A text layer on a source
    # registered as image-only is a tamper indicator (an easy way to feed a
    # parser words the human eye never sees), so it is recorded for the
    # security scanner and deliberately NOT adopted as citable text.
    from pypdf import PdfReader
    reader = PdfReader(doc.path)
    text = "".join((p.extract_text() or "") for p in reader.pages).strip()
    doc.meta["native_text_chars"] = len(text)
    if len(text) >= 50:
        log.warning("%s: unexpected text layer on an image-only scan (%d chars); "
                    "flagged for the security panel, vision remains canonical",
                    doc.source_id, len(text))
        doc.meta["unexpected_text_layer"] = text[:2000]


def _parse_text(doc: SourceDoc) -> None:
    doc.text = Path(doc.path).read_text(encoding="utf-8")
    doc.segments.append(Segment(locator="document", text=doc.text, title=doc.filename))


_PARSERS = {
    "EMAIL": _parse_email,
    "JSON": _parse_json,
    "CSV": _parse_csv,
    "XLSX": _parse_xlsx,
    "PDF_TEXT": _parse_pdf_text,
    "PDF_SCAN": _parse_pdf_scan,
    "TEXT": _parse_text,
    "IMAGE": lambda doc: None,   # binary evidence; vision stage attaches a transcript
}


def load_source(spec: SourceSpec, cfg: RunConfig) -> SourceDoc:
    path = Path(cfg.pack_dir) / spec.filename
    doc = SourceDoc(
        source_id=spec.source_id, filename=spec.filename, path=str(path),
        kind=spec.kind, trust_tier=spec.trust_tier, description=spec.description,
    )
    if spec.source_id in cfg.ablate:
        doc.status = "MISSING"
        doc.meta["ablated"] = True
        log.info("%s: ablated for this run", spec.source_id)
        return doc
    if not path.exists():
        doc.status = "MISSING"
        log.warning("%s: file not found (%s)", spec.source_id, path)
        return doc
    doc.sha256 = sha256_file(str(path))
    try:
        _PARSERS[spec.kind](doc)
    except Exception as exc:  # a bad file degrades that source, never the run
        doc.status = "UNREADABLE"
        doc.meta["error"] = "{}: {}".format(type(exc).__name__, exc)
        log.error("%s: parse failed: %s", spec.source_id, doc.meta["error"])
    return doc


def load_registry(cfg: RunConfig) -> Dict[str, SourceDoc]:
    registry = {spec.source_id: load_source(spec, cfg) for spec in MANIFEST}
    ok = sum(1 for d in registry.values() if d.status == "OK")
    log.info("registry loaded: %d/%d sources OK", ok, len(registry))
    return registry
