"""LLM access: pluggable providers, a content-addressed response cache, a
minimal JSON-schema validator, and a bounded repair loop.

Providers
  anthropic   direct Messages API over urllib (no SDK dependency); supports
              PDF/image content blocks and schema-forced tool output. The
              production path.
  cli         headless call to a local model CLI over stdin, using an existing
              login instead of an API key; vision via a Read-only tool
              allowance. A convenience for machines with no key configured.
  replay      cache-only. Same pipeline, zero network, fully deterministic -
              this is what evals/CI run against.

Every completed call is cached under sha256(model, system, prompt, attachment
hashes, schema), so a re-run is byte-identical and free regardless of which
provider served it.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .util import extract_json_block, sha256_file, sha256_text, stable_json

log = logging.getLogger("claimpilot.llm")


class LLMError(Exception):
    pass


class CacheMiss(LLMError):
    pass


@dataclass
class LLMRequest:
    prompt: str
    system: str = ""
    attachments: List[str] = field(default_factory=list)   # absolute file paths
    schema: Optional[Dict[str, Any]] = None
    max_tokens: int = 8000
    label: str = ""


@dataclass
class LLMResult:
    text: str
    obj: Optional[Dict[str, Any]]
    usage: Dict[str, Any]
    cost_usd: float
    model: str
    provider: str
    cached: bool
    cache_key: str


# ---------------------------------------------------------------- validation

def validate_schema(schema: Dict[str, Any], obj: Any, path: str = "$") -> List[str]:
    """Validate the JSON-schema subset our prompts use (type, properties,
    required, items, enum). Returns human-readable error strings."""
    errors: List[str] = []
    stype = schema.get("type")
    types = stype if isinstance(stype, list) else [stype] if stype else []

    def type_ok(value: Any) -> bool:
        for t in types:
            if t == "object" and isinstance(value, dict):
                return True
            if t == "array" and isinstance(value, list):
                return True
            if t == "string" and isinstance(value, str):
                return True
            if t == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
                return True
            if t == "integer" and isinstance(value, int) and not isinstance(value, bool):
                return True
            if t == "boolean" and isinstance(value, bool):
                return True
            if t == "null" and value is None:
                return True
        return not types

    if not type_ok(obj):
        errors.append("{}: expected {}, got {}".format(path, types, type(obj).__name__))
        return errors
    if "enum" in schema and obj not in schema["enum"]:
        errors.append("{}: {!r} not in enum {}".format(path, obj, schema["enum"]))
    if isinstance(obj, dict):
        for req in schema.get("required", []):
            if req not in obj:
                errors.append("{}: missing required key {!r}".format(path, req))
        for key, sub in schema.get("properties", {}).items():
            if key in obj:
                errors.extend(validate_schema(sub, obj[key], "{}.{}".format(path, key)))
    if isinstance(obj, list) and "items" in schema:
        for i, item in enumerate(obj):
            errors.extend(validate_schema(schema["items"], item, "{}[{}]".format(path, i)))
    return errors


# --------------------------------------------------------------------- cache

class DiskCache:
    def __init__(self, cache_dir: str) -> None:
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        path = self.dir / (key + ".json")
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return None

    def put(self, key: str, payload: Dict[str, Any]) -> None:
        path = self.dir / (key + ".json")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)


# ----------------------------------------------------------------- providers

class BaseProvider:
    name = "base"

    def complete(self, req: LLMRequest, model: str) -> Tuple[str, Dict[str, Any], float]:
        raise NotImplementedError


class AnthropicAPIProvider(BaseProvider):
    """Direct Messages API via urllib. Attachments become document/image
    blocks; when a schema is given, output is forced through a tool call so
    the shape is validated server-side rather than parsed out of prose."""

    name = "anthropic"
    MEDIA = {".pdf": ("document", "application/pdf"), ".png": ("image", "image/png"),
             ".jpg": ("image", "image/jpeg"), ".jpeg": ("image", "image/jpeg")}

    def __init__(self) -> None:
        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY not set")
        base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        self.URL = base.rstrip("/") + "/v1/messages"

    def _content_blocks(self, req: LLMRequest) -> List[Dict[str, Any]]:
        import base64
        blocks: List[Dict[str, Any]] = []
        for path in req.attachments:
            kind, media = self.MEDIA.get(Path(path).suffix.lower(), (None, None))
            if kind is None:
                raise LLMError("unsupported attachment type: {}".format(path))
            data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
            blocks.append({"type": kind,
                           "source": {"type": "base64", "media_type": media, "data": data}})
        blocks.append({"type": "text", "text": req.prompt})
        return blocks

    def complete(self, req: LLMRequest, model: str) -> Tuple[str, Dict[str, Any], float]:
        body: Dict[str, Any] = {
            "model": model, "max_tokens": req.max_tokens, "temperature": 0,
            "messages": [{"role": "user", "content": self._content_blocks(req)}],
        }
        if req.system:
            body["system"] = req.system
        if req.schema is not None:
            body["tools"] = [{"name": "emit", "description": "Emit the extraction result.",
                              "input_schema": req.schema}]
            body["tool_choice"] = {"type": "tool", "name": "emit"}
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        payload = json.dumps(body).encode("utf-8")
        last_err: Optional[Exception] = None
        for attempt in range(4):
            try:
                request = urllib.request.Request(self.URL, data=payload, headers=headers)
                with urllib.request.urlopen(request, timeout=300) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                text = ""
                for block in data.get("content", []):
                    if block.get("type") == "tool_use":
                        text = json.dumps(block.get("input", {}))
                    elif block.get("type") == "text":
                        text = text or block.get("text", "")
                return text, data.get("usage", {}), 0.0
            except urllib.error.HTTPError as exc:
                last_err = exc
                if exc.code in (429, 500, 502, 503, 529) and attempt < 3:
                    time.sleep(min(30, 2 ** (attempt + 1)))
                    continue
                raise LLMError("Anthropic API HTTP {}: {}".format(
                    exc.code, exc.read().decode("utf-8", "replace")[:500]))
            except (urllib.error.URLError, TimeoutError) as exc:
                last_err = exc
                if attempt < 3:
                    time.sleep(min(30, 2 ** (attempt + 1)))
                    continue
        raise LLMError("Anthropic API unreachable: {}".format(last_err))


class LocalCLIProvider(BaseProvider):
    """Headless local model CLI. The prompt travels over stdin (no ARG_MAX
    concerns); vision attachments are handled by allowing exactly the Read
    tool and instructing the model to read the named files first."""

    name = "cli"

    def complete(self, req: LLMRequest, model: str) -> Tuple[str, Dict[str, Any], float]:
        cmd = ["claude", "-p", "--output-format", "json", "--model", model]
        prompt = req.prompt
        if req.system:
            cmd += ["--append-system-prompt", req.system]
        if req.attachments:
            cmd += ["--allowedTools", "Read", "--max-turns", "8"]
            listing = "\n".join("- {}".format(p) for p in req.attachments)
            prompt = ("First use the Read tool to read each of these files:\n{}\n\n"
                      "Then complete the task below.\n\n{}").format(listing, prompt)
        else:
            cmd += ["--max-turns", "2"]
        last_err = ""
        for attempt in range(3):
            try:
                proc = subprocess.run(
                    cmd, input=prompt.encode("utf-8"),
                    capture_output=True, timeout=420,
                )
            except subprocess.TimeoutExpired:
                last_err = "timeout"
                continue
            if proc.returncode != 0:
                last_err = proc.stderr.decode("utf-8", "replace")[-500:]
                time.sleep(2 ** attempt)
                continue
            try:
                envelope = json.loads(proc.stdout.decode("utf-8"))
            except json.JSONDecodeError as exc:
                last_err = "bad CLI envelope: {}".format(exc)
                time.sleep(2 ** attempt)
                continue
            if envelope.get("is_error"):
                last_err = str(envelope.get("result", ""))[:500]
                time.sleep(2 ** attempt)
                continue
            return (envelope.get("result", ""), envelope.get("usage", {}),
                    float(envelope.get("total_cost_usd") or 0.0))
        raise LLMError("claude CLI failed after retries: {}".format(last_err))


class ReplayProvider(BaseProvider):
    name = "replay"

    def complete(self, req: LLMRequest, model: str) -> Tuple[str, Dict[str, Any], float]:
        raise CacheMiss(
            "replay provider has no cached response for {!r}. Run once with a live "
            "provider (anthropic or cli) to populate .cache/llm.".format(req.label))


_PROVIDERS = {"anthropic": AnthropicAPIProvider, "cli": LocalCLIProvider,
              "replay": ReplayProvider}


# -------------------------------------------------------------------- client

class LLMClient:
    """Cache + provider + JSON parsing + schema validation + bounded repair."""

    def __init__(self, provider: str, model: str, cache_dir: str, max_repairs: int = 2) -> None:
        if provider not in _PROVIDERS:
            raise LLMError("unknown provider {!r}".format(provider))
        self.provider_name = provider
        self.provider: BaseProvider = _PROVIDERS[provider]()
        self.model = model
        self.cache = DiskCache(cache_dir)
        self.max_repairs = max_repairs
        self.calls: List[Dict[str, Any]] = []   # per-call telemetry for the run log

    def _cache_key(self, req: LLMRequest) -> str:
        att = [sha256_file(p) for p in req.attachments]
        # Keyed by request content only (not provider), so a cache populated by
        # any provider replays identically under --provider replay.
        return sha256_text(stable_json({
            "model": self.model, "system": req.system, "prompt": req.prompt,
            "attachments": att, "schema": req.schema, "v": 1,
        }))

    def _record(self, req: LLMRequest, res: LLMResult, elapsed: float) -> None:
        self.calls.append({
            "label": req.label, "provider": res.provider, "model": res.model,
            "cached": res.cached, "cost_usd": res.cost_usd, "elapsed_s": round(elapsed, 2),
            "cache_key": res.cache_key,
        })

    def _complete_once(self, req: LLMRequest) -> LLMResult:
        key = self._cache_key(req)
        hit = self.cache.get(key)
        if hit is not None:
            return LLMResult(text=hit["text"], obj=hit.get("obj"), usage=hit.get("usage", {}),
                             cost_usd=0.0, model=hit.get("model", self.model),
                             provider=hit.get("provider", "cache"), cached=True, cache_key=key)
        text, usage, cost = self.provider.complete(req, self.model)
        obj: Optional[Dict[str, Any]] = None
        if req.schema is not None:
            raw = extract_json_block(text)
            if raw is not None:
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    obj = None
        self.cache.put(key, {"text": text, "obj": obj, "usage": usage, "model": self.model,
                             "provider": self.provider_name, "label": req.label})
        return LLMResult(text=text, obj=obj, usage=usage, cost_usd=cost, model=self.model,
                         provider=self.provider_name, cached=False, cache_key=key)

    def call(self, req: LLMRequest) -> LLMResult:
        """One logical call: complete, then (if a schema is set) validate and
        repair up to max_repairs times by feeding the errors back."""
        started = time.time()
        res = self._complete_once(req)
        if req.schema is None:
            self._record(req, res, time.time() - started)
            return res
        for round_no in range(self.max_repairs + 1):
            errors: List[str] = []
            if res.obj is None:
                errors = ["output was not parseable as a single JSON object"]
            else:
                errors = validate_schema(req.schema, res.obj)
            if not errors:
                self._record(req, res, time.time() - started)
                return res
            if round_no == self.max_repairs:
                break
            log.warning("%s: schema errors (repair %d): %s",
                        req.label, round_no + 1, "; ".join(errors[:5]))
            repair = LLMRequest(
                prompt=(
                    "Your previous output for the task below failed validation.\n"
                    "Errors:\n{}\n\nPrevious output:\n{}\n\n"
                    "Original task:\n{}\n\n"
                    "Output ONLY the corrected JSON object. Do not change values that "
                    "were correct; fix only the listed problems."
                ).format("\n".join("- " + e for e in errors[:20]),
                         res.text[:6000], req.prompt),
                system=req.system, attachments=req.attachments, schema=req.schema,
                max_tokens=req.max_tokens, label=req.label + "/repair{}".format(round_no + 1),
            )
            res = self._complete_once(repair)
        raise LLMError("{}: output failed schema validation after {} repairs: {}".format(
            req.label, self.max_repairs, "; ".join(errors[:5])))

    @property
    def total_cost(self) -> float:
        return round(sum(c["cost_usd"] for c in self.calls), 4)
