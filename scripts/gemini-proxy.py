#!/usr/bin/env python3
"""gemini-proxy — translate Anthropic /v1/messages requests into Google's
private Cloud Code Assist API (the backend behind `gemini` and `agy`).

Auth model: reads ~/.gemini/oauth_creds.json (the file the official Gemini CLI
maintains) and refreshes the OAuth access token automatically using the client
credentials embedded in @google/gemini-cli 0.45.2. Project discovery is done by
calling loadCodeAssist on startup, which returns a default project tied to the
user's Google One AI Pro / Gemini Code Assist subscription.

Listens on 127.0.0.1:8082 by default. Exposes:
  GET  /health   — used by claude-multi to know the proxy is ready
  POST /v1/messages — main Anthropic-shaped endpoint
"""
import argparse
import http.server
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

# OAuth client constants. The Cloud Code Assist flow uses the same OAuth
# client that @google/gemini-cli ships with — we don't have our own, and the
# user already authorized that client when they ran `gemini auth login`. To
# keep credentials out of source control while staying zero-config, we look
# them up on demand from the installed gemini-cli bundle (Homebrew, npm
# global, pnpm, yarn). Override via env if you want pin a specific pair.
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GEMINI_CLI_GLOB_CANDIDATES = [
    "/opt/homebrew/Cellar/gemini-cli/*/libexec/lib/node_modules/@google/gemini-cli/bundle/*.js",
    "/usr/local/Cellar/gemini-cli/*/libexec/lib/node_modules/@google/gemini-cli/bundle/*.js",
    str(Path.home() / ".nvm/versions/node/*/lib/node_modules/@google/gemini-cli/bundle/*.js"),
    "/usr/local/lib/node_modules/@google/gemini-cli/bundle/*.js",
]


def _discover_oauth_client() -> tuple[str, str]:
    """Pull OAuth client_id/secret from the installed gemini-cli bundle.

    Env vars take precedence so power users can pin a specific client without
    touching the filesystem.  Without an installed gemini-cli AND without env
    overrides, we raise a clear error pointing the user to either install the
    CLI or set the variables.
    """
    cid = os.environ.get("CLAUDE_HARNESS_GEMINI_CLIENT_ID", "").strip()
    csec = os.environ.get("CLAUDE_HARNESS_GEMINI_CLIENT_SECRET", "").strip()
    if cid and csec:
        return cid, csec

    import glob
    import re
    cid_re = re.compile(r'OAUTH_CLIENT_ID\s*=\s*"([^"]+)"')
    csec_re = re.compile(r'OAUTH_CLIENT_SECRET\s*=\s*"([^"]+)"')
    for pattern in _GEMINI_CLI_GLOB_CANDIDATES:
        for path in glob.glob(pattern):
            try:
                txt = Path(path).read_text(errors="ignore")
            except OSError:
                continue
            m_id = cid_re.search(txt)
            m_sec = csec_re.search(txt)
            if m_id and m_sec:
                return m_id.group(1), m_sec.group(1)
    raise RuntimeError(
        "Cannot find Gemini OAuth credentials. Install @google/gemini-cli "
        "(brew install gemini-cli) or set "
        "CLAUDE_HARNESS_GEMINI_CLIENT_ID / CLAUDE_HARNESS_GEMINI_CLIENT_SECRET."
    )


_oauth_cache: dict[str, str] = {}


def _oauth_credentials() -> tuple[str, str]:
    if "id" not in _oauth_cache:
        cid, csec = _discover_oauth_client()
        _oauth_cache["id"] = cid
        _oauth_cache["secret"] = csec
    return _oauth_cache["id"], _oauth_cache["secret"]
CREDS_PATH = Path.home() / ".gemini" / "oauth_creds.json"

CODE_ASSIST_HOST = "https://cloudcode-pa.googleapis.com"
CODE_ASSIST_API = "v1internal"

DEFAULT_MODEL = "gemini-3-pro-preview"

# Mapping from the human-readable labels Antigravity surfaces to the real model
# IDs that the Cloud Code Assist streamGenerateContent endpoint accepts. Probed
# via direct calls in 2026-06; check the README's model table if a new family
# ships.
#
# Verified accepting IDs:
#   gemini-3-pro-preview, gemini-3-flash-preview  (Gemini 3 family — current)
#   gemini-2.5-pro, gemini-2.5-flash              (legacy, quota-shared)
# Verified rejecting (404):
#   claude-*, gpt-oss-*  — Antigravity exposes these in `agy models` but routes
#   them through a different backend; the streamGenerateContent endpoint does
#   not serve them.
MODEL_ALIAS = {
    # Gemini 3 Pro
    "gemini-3-pro": "gemini-3-pro-preview",
    "gemini-3-pro-preview": "gemini-3-pro-preview",
    "gemini-3.1-pro": "gemini-3-pro-preview",
    "gemini-pro": "gemini-3-pro-preview",
    "gemini": "gemini-3-pro-preview",
    # Gemini 3 Flash
    "gemini-3-flash": "gemini-3-flash-preview",
    "gemini-3-flash-preview": "gemini-3-flash-preview",
    "gemini-3.5-flash": "gemini-3-flash-preview",
    "gemini-flash": "gemini-3-flash-preview",
    # Gemini 2.5 (legacy, low quota on this subscription)
    "gemini-2.5-pro": "gemini-2.5-pro",
    "gemini-2.5-flash": "gemini-2.5-flash",
}


def _log(msg: str) -> None:
    sys.stderr.write(f"[gemini-proxy] {msg}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Auth + project discovery
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()
_state = {
    "access_token": None,
    "expiry_ms": 0,
    "refresh_token": None,
    "project": None,
}


def _read_creds_file() -> dict:
    try:
        return json.loads(CREDS_PATH.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"cannot read {CREDS_PATH}: {e}") from e


def _write_creds_file(creds: dict) -> None:
    CREDS_PATH.write_text(json.dumps(creds, indent=2) + "\n")


def _refresh_oauth(refresh_token: str) -> dict:
    cid, csec = _oauth_credentials()
    data = urllib.parse.urlencode({
        "client_id": cid,
        "client_secret": csec,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def get_access_token() -> str:
    """Return a valid access token, refreshing on the fly when it has less
    than 60s of life left. Persists the refreshed token to disk so the next
    process boot is instant."""
    with _state_lock:
        now_ms = int(time.time() * 1000)
        tok = _state["access_token"]
        exp = _state["expiry_ms"]
        if tok and exp - now_ms > 60_000:
            return tok

        creds = _read_creds_file()
        # Honor a refresh that already happened on disk (other process did it).
        if creds.get("expiry_date", 0) - now_ms > 60_000 and creds.get("access_token"):
            _state["access_token"] = creds["access_token"]
            _state["expiry_ms"] = creds["expiry_date"]
            _state["refresh_token"] = creds.get("refresh_token")
            return _state["access_token"]

        rt = creds.get("refresh_token") or _state.get("refresh_token")
        if not rt:
            raise RuntimeError("no refresh_token in oauth_creds.json")
        _log("refreshing access token via OAuth refresh_token")
        resp = _refresh_oauth(rt)
        creds["access_token"] = resp["access_token"]
        creds["expiry_date"] = now_ms + int(resp.get("expires_in", 3600)) * 1000
        _write_creds_file(creds)
        _state["access_token"] = creds["access_token"]
        _state["expiry_ms"] = creds["expiry_date"]
        _state["refresh_token"] = creds.get("refresh_token") or rt
        return _state["access_token"]


def get_project_id() -> str:
    """Resolve the Cloud AI Companion project tied to the user's subscription.

    This proxy is for **subscription users** (Google One AI Pro / Code Assist)
    who authenticate with OAuth. The project they need is auto-assigned by
    Google when they signed up and is returned by ``loadCodeAssist``. The
    user never has to enable any API in the Cloud Console; it's all handled
    by the subscription.

    Priority order:
      1. ``loadCodeAssist`` — the normal flow. Returns the subscription-tied
         project that already has Code Assist enabled. Cached for the rest
         of the process.
      2. ``CLAUDE_HARNESS_GEMINI_PROJECT`` — escape hatch for the rare case
         where someone wants to point at a different project (e.g. a paid
         Vertex AI project with Cloud Code Assist enabled separately).

    We intentionally do *not* honor ``GOOGLE_CLOUD_PROJECT``. That variable
    is used by API-key flows (Gemini Studio, Vertex SDK, BigQuery, etc.) for
    completely unrelated projects, and silently routing the subscription's
    OAuth token at one of those will 403 every time
    ("cloudaicompanion.googleapis.com … not enabled").
    """
    with _state_lock:
        if _state["project"]:
            return _state["project"]

    override = (os.environ.get("CLAUDE_HARNESS_GEMINI_PROJECT") or "").strip()
    if override:
        with _state_lock:
            _state["project"] = override
        _log(f"using project override from CLAUDE_HARNESS_GEMINI_PROJECT={override}")
        return override

    token = get_access_token()
    body = {
        "metadata": {
            "ideType": "IDE_UNSPECIFIED",
            "platform": "PLATFORM_UNSPECIFIED",
            "pluginType": "GEMINI",
        }
    }
    req = urllib.request.Request(
        f"{CODE_ASSIST_HOST}/{CODE_ASSIST_API}:loadCodeAssist",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read())
    project = payload.get("cloudaicompanionProject")
    if not project:
        raise RuntimeError(f"loadCodeAssist returned no project: {payload}")
    with _state_lock:
        _state["project"] = project
    _log(f"resolved project={project} tier={payload.get('currentTier', {}).get('id')} (via loadCodeAssist)")
    return project


# ---------------------------------------------------------------------------
# Anthropic ↔ Gemini schema translation
# ---------------------------------------------------------------------------

# Gemini accepts only a subset of JSON Schema. Anthropic tool definitions
# ship full Draft-2020-12 schemas (with $schema, propertyNames, additional
# constraints…). Strip everything outside the whitelist before forwarding,
# recursing into nested objects/arrays.
_GEMINI_SCHEMA_KEEP = {
    "type", "properties", "items", "enum", "description", "format",
    "nullable", "required", "title",
}


def _sanitize_gemini_schema(node):
    if isinstance(node, dict):
        cleaned = {}
        for k, v in node.items():
            if k not in _GEMINI_SCHEMA_KEEP:
                continue
            cleaned[k] = _sanitize_gemini_schema(v)
        # After dropping unknown fields, prune any `required` entry whose key
        # is no longer present in `properties`. Anthropic tool definitions
        # sometimes list required keys that come from $ref-expanded slots,
        # and Gemini rejects mismatches with "required fields not defined".
        props = cleaned.get("properties")
        req = cleaned.get("required")
        if isinstance(props, dict) and isinstance(req, list):
            cleaned["required"] = [r for r in req if r in props]
            if not cleaned["required"]:
                cleaned.pop("required", None)
        elif isinstance(req, list) and not isinstance(props, dict):
            cleaned.pop("required", None)
        return cleaned
    if isinstance(node, list):
        return [_sanitize_gemini_schema(x) for x in node]
    return node


def resolve_model(model: str) -> str:
    if not model:
        return DEFAULT_MODEL
    ml = model.strip()
    # Strip our own prefix (`gemini/`) and any [1m]/[2m] suffix the harness adds.
    if ml.lower().startswith("gemini/"):
        ml = ml[len("gemini/"):]
    if ml.endswith("[1m]") or ml.endswith("[2m]"):
        ml = ml[:-4].strip()
    return MODEL_ALIAS.get(ml.lower(), ml)


def _anthropic_content_to_parts(content) -> list[dict]:
    """Anthropic message content → Gemini parts."""
    if isinstance(content, str):
        return [{"text": content}]
    parts = []
    if not isinstance(content, list):
        return parts
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append({"text": block.get("text", "")})
        elif btype == "tool_use":
            parts.append({
                "functionCall": {
                    "name": block.get("name", ""),
                    "args": block.get("input", {}) or {},
                }
            })
        elif btype == "tool_result":
            inner = block.get("content")
            if isinstance(inner, list):
                inner = "".join(b.get("text", "") for b in inner if isinstance(b, dict))
            parts.append({
                "functionResponse": {
                    "name": block.get("tool_use_id", ""),
                    "response": {"content": inner if inner is not None else ""},
                }
            })
        elif btype == "image":
            src = block.get("source", {})
            if src.get("type") == "base64":
                parts.append({
                    "inlineData": {
                        "mimeType": src.get("media_type", "image/png"),
                        "data": src.get("data", ""),
                    }
                })
    return parts


def anthropic_to_gemini_request(body: dict) -> dict:
    """Convert an Anthropic /v1/messages body into the wrapped Cloud Code
    Assist request payload."""
    contents = []
    for m in body.get("messages", []) or []:
        role = m.get("role", "user")
        gemini_role = "model" if role == "assistant" else "user"
        parts = _anthropic_content_to_parts(m.get("content", ""))
        if parts:
            contents.append({"role": gemini_role, "parts": parts})

    system = body.get("system")
    system_instruction = None
    if system:
        if isinstance(system, str):
            system_instruction = {"parts": [{"text": system}]}
        elif isinstance(system, list):
            txt = "".join(b.get("text", "") for b in system if isinstance(b, dict))
            if txt:
                system_instruction = {"parts": [{"text": txt}]}

    tools = None
    if body.get("tools"):
        decls = []
        for t in body["tools"]:
            if not isinstance(t, dict):
                continue
            decls.append({
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": _sanitize_gemini_schema(t.get("input_schema", {}) or {}),
            })
        if decls:
            tools = [{"functionDeclarations": decls}]

    generation_config: dict = {}
    if body.get("max_tokens"):
        generation_config["maxOutputTokens"] = int(body["max_tokens"])
    if "temperature" in body:
        generation_config["temperature"] = body["temperature"]
    if "top_p" in body:
        generation_config["topP"] = body["top_p"]
    if "top_k" in body:
        generation_config["topK"] = body["top_k"]
    if body.get("stop_sequences"):
        generation_config["stopSequences"] = body["stop_sequences"]
    # Gemini 3 Pro consumes a large fraction of every response on hidden
    # reasoning tokens. For non-reasoning Anthropic-shaped flows that just
    # want a fast text reply, disable the thinking budget by default. Users
    # who want the Pro reasoning can opt in by setting CLAUDE_HARNESS_GEMINI_THINKING.
    thinking_budget_env = os.environ.get("CLAUDE_HARNESS_GEMINI_THINKING", "").strip()
    if thinking_budget_env:
        try:
            generation_config["thinkingConfig"] = {"thinkingBudget": int(thinking_budget_env)}
        except ValueError:
            pass
    else:
        generation_config["thinkingConfig"] = {"thinkingBudget": 0}

    inner: dict = {"contents": contents, "session_id": str(uuid.uuid4())}
    if system_instruction:
        inner["systemInstruction"] = system_instruction
    if tools:
        inner["tools"] = tools
    if generation_config:
        inner["generationConfig"] = generation_config

    return {
        "model": resolve_model(body.get("model", "")),
        "project": get_project_id(),
        "user_prompt_id": str(uuid.uuid4()),
        "request": inner,
    }


def _gemini_part_to_anthropic_block(part: dict) -> dict | None:
    if "text" in part:
        return {"type": "text", "text": part["text"]}
    if "functionCall" in part:
        fc = part["functionCall"]
        return {
            "type": "tool_use",
            "id": f"toolu_{uuid.uuid4().hex[:24]}",
            "name": fc.get("name", ""),
            "input": fc.get("args", {}) or {},
        }
    return None


# ---------------------------------------------------------------------------
# SSE translation: Gemini stream → Anthropic Messages stream
# ---------------------------------------------------------------------------

def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")


def stream_gemini_to_anthropic(upstream, model_id: str):
    """Read Gemini SSE chunks from `upstream` and yield Anthropic-formatted SSE
    bytes. Handles incremental text deltas and converts function calls into
    tool_use blocks."""
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    yield _sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id, "type": "message", "role": "assistant",
            "model": model_id, "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })

    text_block_open = False
    tool_block_index = None
    final_stop_reason = "end_turn"
    final_usage = {"input_tokens": 0, "output_tokens": 0}
    block_index = 0

    buf = b""
    for chunk in iter(lambda: upstream.read(4096), b""):
        # The Cloud Code Assist endpoint uses CRLF line endings on its SSE
        # stream (event separator: \r\n\r\n). Normalize to LF so the parser
        # below — which expects \n\n — finds the boundaries.
        buf += chunk.replace(b"\r\n", b"\n")
        while b"\n\n" in buf:
            event_raw, buf = buf.split(b"\n\n", 1)
            data_lines = []
            for line in event_raw.split(b"\n"):
                if line.startswith(b"data:"):
                    data_lines.append(line[5:].lstrip())
            if not data_lines:
                continue
            try:
                payload = json.loads(b"\n".join(data_lines).decode("utf-8"))
            except json.JSONDecodeError:
                continue
            response = payload.get("response") or {}
            cands = response.get("candidates") or []
            if not cands:
                # could be a usage-only update
                um = response.get("usageMetadata") or {}
                if um:
                    final_usage["input_tokens"] = um.get("promptTokenCount", final_usage["input_tokens"])
                    final_usage["output_tokens"] = um.get("candidatesTokenCount", final_usage["output_tokens"])
                continue
            cand = cands[0]
            parts = (cand.get("content") or {}).get("parts") or []
            for p in parts:
                if "text" in p:
                    text = p["text"]
                    if not text_block_open:
                        yield _sse("content_block_start", {
                            "type": "content_block_start",
                            "index": block_index,
                            "content_block": {"type": "text", "text": ""},
                        })
                        text_block_open = True
                    yield _sse("content_block_delta", {
                        "type": "content_block_delta",
                        "index": block_index,
                        "delta": {"type": "text_delta", "text": text},
                    })
                elif "functionCall" in p:
                    if text_block_open:
                        yield _sse("content_block_stop", {
                            "type": "content_block_stop",
                            "index": block_index,
                        })
                        text_block_open = False
                        block_index += 1
                    fc = p["functionCall"]
                    tool_id = f"toolu_{uuid.uuid4().hex[:24]}"
                    yield _sse("content_block_start", {
                        "type": "content_block_start",
                        "index": block_index,
                        "content_block": {
                            "type": "tool_use", "id": tool_id,
                            "name": fc.get("name", ""), "input": {},
                        },
                    })
                    yield _sse("content_block_delta", {
                        "type": "content_block_delta",
                        "index": block_index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(fc.get("args", {}) or {}),
                        },
                    })
                    yield _sse("content_block_stop", {
                        "type": "content_block_stop",
                        "index": block_index,
                    })
                    block_index += 1
                    tool_block_index = block_index
                    final_stop_reason = "tool_use"
            fr = cand.get("finishReason")
            if fr == "STOP":
                final_stop_reason = "end_turn"
            elif fr == "MAX_TOKENS":
                final_stop_reason = "max_tokens"
            elif fr == "SAFETY":
                final_stop_reason = "end_turn"
        # done parsing buffered events; loop for more chunks

    if text_block_open:
        yield _sse("content_block_stop", {
            "type": "content_block_stop", "index": block_index,
        })

    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": final_stop_reason, "stop_sequence": None},
        "usage": final_usage,
    })
    yield _sse("message_stop", {"type": "message_stop"})


# ---------------------------------------------------------------------------
# Non-streaming fallback
# ---------------------------------------------------------------------------

def gemini_response_to_anthropic_message(payload: dict, model_id: str) -> dict:
    """Used for `stream: false` requests — collapses the (potentially several)
    SSE chunks the API still sends back into a single Anthropic message body."""
    blocks: list[dict] = []
    text_buf = ""
    stop_reason = "end_turn"
    usage = {"input_tokens": 0, "output_tokens": 0}

    for cand in (payload.get("response", {}).get("candidates") or []):
        for p in (cand.get("content") or {}).get("parts") or []:
            block = _gemini_part_to_anthropic_block(p)
            if not block:
                continue
            if block["type"] == "text":
                text_buf += block["text"]
            else:
                if text_buf:
                    blocks.append({"type": "text", "text": text_buf})
                    text_buf = ""
                blocks.append(block)
        fr = cand.get("finishReason")
        if fr == "MAX_TOKENS":
            stop_reason = "max_tokens"
        elif fr in ("STOP", None):
            stop_reason = "end_turn"
    if text_buf:
        blocks.append({"type": "text", "text": text_buf})

    um = payload.get("response", {}).get("usageMetadata") or {}
    usage["input_tokens"] = um.get("promptTokenCount", 0)
    usage["output_tokens"] = um.get("candidatesTokenCount", 0)

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model_id,
        "content": blocks or [{"type": "text", "text": ""}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage,
    }


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # noqa: D401  (silence default)
        sys.stderr.write(f"[gemini-proxy] {self.command} {self.path} — {fmt % args}\n")

    def _send_json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_anthropic_error(self, status: int, message: str, error_type: str = "api_error") -> None:
        self._send_json(status, {
            "type": "error",
            "error": {"type": error_type, "message": message},
        })

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_anthropic_error(404, f"unknown path: {self.path}", "not_found_error")

    def do_POST(self) -> None:
        if self.path != "/v1/messages":
            self._send_anthropic_error(404, f"unknown path: {self.path}", "not_found_error")
            return
        length = int(self.headers.get("content-length", "0"))
        try:
            raw = self.rfile.read(length)
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self._send_anthropic_error(400, "invalid JSON body", "invalid_request_error")
            return

        try:
            wrapped = anthropic_to_gemini_request(body)
        except Exception as e:
            self._send_anthropic_error(500, f"request translation failed: {e}")
            return

        model_id = wrapped["model"]
        url = f"{CODE_ASSIST_HOST}/{CODE_ASSIST_API}:streamGenerateContent?alt=sse"
        streaming = bool(body.get("stream"))

        try:
            token = get_access_token()
            req = urllib.request.Request(
                url,
                data=json.dumps(wrapped).encode(),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            upstream = urllib.request.urlopen(req, timeout=None if streaming else 600)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            sys.stderr.write(f"[gemini-proxy] upstream {e.code}: {err_body[:400]}\n")
            try:
                parsed = json.loads(err_body)
                msg = parsed.get("error", {}).get("message") or err_body[:300]
            except json.JSONDecodeError:
                msg = err_body[:300]
            self._send_anthropic_error(e.code if 400 <= e.code < 600 else 500, msg)
            return
        except Exception as e:
            self._send_anthropic_error(500, f"upstream connection failed: {e}")
            return

        if streaming:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                for piece in stream_gemini_to_anthropic(upstream, model_id):
                    self.wfile.write(piece)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            return

        # Non-streaming: collapse the SSE stream into a single Anthropic message.
        collected: list[dict] = []
        for line in upstream.read().split(b"\n"):
            if not line.startswith(b"data:"):
                continue
            try:
                payload = json.loads(line[5:].strip().decode())
                collected.append(payload)
            except json.JSONDecodeError:
                continue
        # Merge candidates from every chunk into one response object so the
        # collapse function can walk it linearly.
        merged: dict = {"response": {"candidates": []}}
        for chunk in collected:
            r = chunk.get("response") or {}
            for cand in r.get("candidates") or []:
                merged["response"]["candidates"].append(cand)
            if r.get("usageMetadata"):
                merged["response"]["usageMetadata"] = r["usageMetadata"]
        message = gemini_response_to_anthropic_message(merged, model_id)
        self._send_json(200, message)


class ThreadingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8082)
    args = parser.parse_args()

    # Eager initialization: refresh the OAuth token and resolve the project
    # once on startup so the first real request doesn't pay the cost. Failure
    # here is non-fatal — the proxy still serves /health so claude-multi can
    # detect it's listening, and per-request errors will surface the cause.
    try:
        get_access_token()
        get_project_id()
    except Exception as e:
        _log(f"warm-up failed (will retry per-request): {e}")

    srv = ThreadingServer((args.host, args.port), Handler)
    _log(f"listening on http://{args.host}:{args.port}")
    _log(f"endpoints: GET /health, POST /v1/messages")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
