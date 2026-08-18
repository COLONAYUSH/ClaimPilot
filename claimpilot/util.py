"""Shared primitives: money, hashing, text normalization, quote matching, JSON."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from difflib import SequenceMatcher
from typing import Any, Optional, Tuple

TWO_DP = Decimal("0.01")


def D(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def dec2(value: Any) -> Decimal:
    return D(value).quantize(TWO_DP, rounding=ROUND_HALF_UP)


def money(value: Any) -> str:
    q = dec2(value)
    sign = "-" if q < 0 else ""
    return "{}${:,.2f}".format(sign, abs(q))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


_WS = re.compile(r"\s+")
_CHAR_MAP = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "­": "", " ": " ",
}


def norm_text(text: str) -> str:
    """Normalization used for quote verification: NFKC, ASCII-fold common
    typographic characters, collapse whitespace, casefold."""
    text = unicodedata.normalize("NFKC", text)
    for src, dst in _CHAR_MAP.items():
        text = text.replace(src, dst)
    return _WS.sub(" ", text).strip().lower()


def find_quote(quote: str, text: str, fuzzy_threshold: float = 0.90) -> Tuple[bool, float]:
    """Check that `quote` occurs in `text` under normalization.

    Exact normalized substring first; otherwise a fuzzy sliding word-window
    (SequenceMatcher) to absorb small OCR/PDF-extraction artifacts. Returns
    (found, match_ratio). Corpus documents here are small (<5 KB) so the
    quadratic fallback is fine.
    """
    nq, nt = norm_text(quote), norm_text(text)
    if not nq:
        return False, 0.0
    if nq in nt:
        return True, 1.0
    q_words, t_words = nq.split(), nt.split()
    n = len(q_words)
    if n == 0 or not t_words:
        return False, 0.0
    best = 0.0
    for i in range(0, max(1, len(t_words) - max(1, n // 2))):
        window = " ".join(t_words[i:i + n + 2])
        ratio = SequenceMatcher(None, nq, window).ratio()
        if ratio > best:
            best = ratio
            if best >= 0.999:
                break
    return best >= fuzzy_threshold, round(best, 4)


def parse_dt(value: str) -> datetime:
    """Parse ISO-8601 timestamps as they appear in the pack (offset-aware)."""
    return datetime.fromisoformat(value)


def fmt_date(value: str) -> str:
    """Render an ISO date(-time) as e.g. 'May 12, 2026' for prose surfaces."""
    dt = datetime.fromisoformat(value[:19]) if len(value) >= 10 else None
    if dt is None:
        return value
    return "{} {}, {}".format(dt.strftime("%B"), dt.day, dt.year)


def to_jsonable(obj: Any) -> Any:
    """Recursively convert domain objects into JSON-serializable structures.
    Decimals become 2dp strings so money never picks up float artifacts."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, Decimal):
        return str(dec2(obj))
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    return str(obj)


def stable_json(obj: Any) -> str:
    return json.dumps(to_jsonable(obj), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def pretty_json(obj: Any) -> str:
    return json.dumps(to_jsonable(obj), indent=2, ensure_ascii=False, sort_keys=False)


def extract_json_block(text: str) -> Optional[str]:
    """Pull the first JSON object out of LLM output, tolerating code fences
    and prose wrappers. Returns the raw JSON string or None."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1)
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None
