"""Adversarial-input detection.

Every source in the claim folder is untrusted by default. Half of them are
authored by the counterparty, and any of them could carry text aimed at the
model rather than the reader: fake system notes, role markers, instructions
hidden in invisible unicode, or a text layer smuggled into a supposedly
image-only scan.

The primary defenses live elsewhere and are structural: the money conclusions
are computed, not generated, so no injected sentence can move them; extracted
facts need mechanically verified quotes; trust tiers stop carrier-authored
text from outranking signed documents; NumberGuard and the reference check
police the prose. This module adds the detection layer on top: a deterministic
scanner that runs on every ingested source (including vision transcripts) and
surfaces indicators in the brief's security panel instead of letting them pass
silently. Detection over silent resistance, because a specialist should know
the counterparty tried.

`claimpilot robustness` (evals/robustness.py) runs a copy of the pack seeded
with synthetic adversarial content and asserts both halves: the indicators
fire, and the deterministic conclusions do not move.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List

from .models import SourceDoc

# Pattern classes. Tuned against the real pack so ordinary freight language
# ("Special instructions:", "Transportation Management System") stays quiet.
_PATTERNS = [
    ("instruction_override", "HIGH", re.compile(
        r"(?i)\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,50}"
        r"\b(instructions?|prompts?|rules?|context|guidelines?)\b")),
    ("role_marker", "HIGH", re.compile(
        r"(?im)(<\|[a-z_]+\|>|\[/?INST\]|^\s*(?:system|assistant|developer)\s*:\s|"
        r"\bsystem\s+(?:note|prompt|message|override)\b)")),
    ("ai_directive", "MEDIUM", re.compile(
        r"(?i)(\b(?:note|message|instructions?)\s+(?:to|for)\s+"
        r"(?:the|any|an|all|our|your)?\s*"
        r"(?:ai|llm|assistant|language\s+model|automated\s+system)s?\b|"
        r"\byou\s+are\s+an?\s+(?:ai|llm|assistant|language\s+model)\b|\bas\s+an\s+ai\b)")),
    ("invisible_unicode", "HIGH", re.compile(
        "[\\u200b\\u200c\\u200d\\u2060\\ufeff\\u202a-\\u202e\\u2066-\\u2069]")),
    ("encoded_blob", "LOW", re.compile(r"[A-Za-z0-9+/]{120,}={0,2}")),
]

SCAN_CLASSES = [name for name, _, _ in _PATTERNS] + ["unexpected_text_layer"]


@dataclass
class SecurityFinding:
    source_id: str
    kind: str
    severity: str
    evidence: str        # short snippet around the match, control chars escaped
    location: str = ""   # character offset within the scanned text


def _snippet(text: str, start: int, end: int) -> str:
    raw = text[max(0, start - 40):min(len(text), end + 40)]
    return raw.encode("unicode_escape").decode("ascii")[:180]


def scan_text(source_id: str, text: str, where: str = "text") -> List[SecurityFinding]:
    findings: List[SecurityFinding] = []
    if not text:
        return findings
    for kind, severity, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            findings.append(SecurityFinding(
                source_id=source_id, kind=kind, severity=severity,
                evidence=_snippet(text, m.start(), m.end()),
                location="{}:offset {}".format(where, m.start())))
            break  # one finding per class per source keeps the panel readable
    return findings


def scan_registry(registry: Dict[str, SourceDoc]) -> List[SecurityFinding]:
    """Scan every source's native text and derived (vision) transcript, plus
    any text layer found where none should exist."""
    findings: List[SecurityFinding] = []
    for sid, doc in registry.items():
        if doc.status in ("MISSING", "UNREADABLE"):
            continue
        findings.extend(scan_text(sid, doc.text, "text"))
        if doc.derived_text:
            findings.extend(scan_text(sid, doc.derived_text, "transcript"))
        hidden = doc.meta.get("unexpected_text_layer")
        if hidden:
            findings.append(SecurityFinding(
                source_id=sid, kind="unexpected_text_layer", severity="HIGH",
                evidence=_snippet(hidden, 0, min(len(hidden), 80)),
                location="pdf text layer (not adopted; vision transcript remains "
                         "the citable text)"))
            findings.extend(scan_text(sid, hidden, "hidden text layer"))
    return findings
