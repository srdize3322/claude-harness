#!/usr/bin/env python3
"""Codex local proxy: Anthropic Messages API <-> OpenAI Codex backend.

Drop-in replacement for the Cloudflare Worker handler in worker/src/codex-handler.js.
Listens on http://127.0.0.1:8080 by default and translates:

  POST /v1/messages  (Anthropic Messages API, SSE or JSON)
        |
        v
  POST https://chatgpt.com/backend-api/codex/responses  (Codex Responses API)
        |
        v
  POST /v1/messages  (Anthropic SSE or JSON response)

The wrapper bash script (scripts/claude-codex) reads ~/.codex/auth.json, refreshes
the access_token, and forwards it via:

    Authorization: codex:<access_token>:<account_id>

This proxy detects the "codex:" prefix, splits out the token + account_id,
and re-issues the request to chatgpt.com with:

    Authorization: Bearer <access_token>
    chatgpt-account-id: <account_id>

Only the Python standard library is used (no pip installs).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# === Constants (mirror worker/src/codex-protocol.js + codex-handler.js) ===

CODEX_DEFAULT_BASE_URL = "https://chatgpt.com/backend-api"
CODEX_RESPONSES_PATH = "/codex/responses"
MAX_RETRIES = 3
BASE_DELAY_MS = 1000
DEFAULT_MODEL = "gpt-5.4"

# HTTP retry codes (from codex-protocol.js)
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

# Retryable error text patterns (from codex-handler.js isRetryableErrorText)
RETRYABLE_TEXT_RE = re.compile(
    r"rate.?limit|overloaded|service.?unavailable|upstream.?connect|connection.?refused",
    re.IGNORECASE,
)

USAGE_LIMIT_RE = re.compile(r"usage.?limit", re.IGNORECASE)


# === Pure helpers (no I/O) ===

def random_short_id(length: int = 24) -> str:
    """Hex-only id matching JS crypto.randomUUID().replace(/-/g, '').slice(0,n)."""
    nbytes = (length + 1) // 2
    return secrets.token_hex(nbytes)[:length]


def is_retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUSES


def retry_delay(attempt: int) -> int:
    """attempt is 1-indexed; returns ms before retry N."""
    return BASE_DELAY_MS * (2 ** (attempt - 1))


def is_retryable_error_text(text: str) -> bool:
    if not text:
        return False
    return bool(RETRYABLE_TEXT_RE.search(text))


def map_stop_reason(status, has_tool_calls: bool) -> str:
    """Mirrors codex-handler.js mapStopReason (note: cancelled -> end_turn)."""
    if status == "completed":
        return "tool_use" if has_tool_calls else "end_turn"
    if status == "incomplete":
        return "max_tokens"
    if status == "failed":
        return "error"
    if status == "cancelled":
        return "end_turn"
    return "end_turn"


def map_status_to_error_type(status: int) -> str:
    """Mirrors codex-handler.js mapStatusToErrorType."""
    if status == 401:
        return "authentication_error"
    if status == 403:
        return "permission_error"
    if status == 404:
        return "not_found_error"
    if status == 429:
        return "rate_limit_error"
    if 500 <= status < 600:
        return "api_error"
    if 400 <= status < 500:
        return "invalid_request_error"
    return "api_error"


def parse_codex_error_body(body):
    """Mirror codex-handler.js parseCodexErrorBody."""
    if body is None:
        return None
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            return body or None
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    if err is None:
        return None
    if isinstance(err, str):
        return err
    if not isinstance(err, dict):
        return None
    code = err.get("code")
    if code in ("usage_limit_reached", "usage_not_included"):
        plan = err.get("plan_type") or "your plan"
        resets_at = err.get("resets_at")
        if resets_at:
            try:
                minutes = max(1, round((resets_at * 1000 - int(time.time() * 1000)) / 60000))
            except Exception:
                minutes = 1
            return f"You have hit your ChatGPT usage limit ({plan} plan). Try again in ~{minutes} min."
        return f"You have hit your ChatGPT usage limit ({plan} plan)."
    if code == "rate_limit_exceeded":
        return "Rate limit exceeded. Please slow down and try again."
    if code in ("invalid_api_key", "token_expired"):
        return "ChatGPT access token is invalid or expired. Refresh via the wrapper."
    return err.get("message") or None


def get_retry_delay_ms(headers, attempt: int) -> int:
    """Mirror codex-handler.js getRetryDelay."""
    if headers is not None:
        ms = headers.get("Retry-After-Ms")
        if ms:
            try:
                n = int(ms)
                if n >= 0:
                    return n
            except (TypeError, ValueError):
                pass
        after = headers.get("Retry-After")
        if after:
            try:
                n = int(after)
                if n >= 0:
                    return n * 1000
            except (TypeError, ValueError):
                pass
            try:
                t = parsedate_to_datetime(after)
                if t is not None:
                    delta_ms = int((t.timestamp() - time.time()) * 1000)
                    return max(0, delta_ms)
            except Exception:
                pass
    return retry_delay(attempt + 1)


# === Anthropic -> Codex conversion (mirror codex-protocol.js) ===

def effort_from_budget(budget: int) -> str:
    if budget <= 1000:
        return "low"
    if budget <= 8000:
        return "medium"
    if budget <= 24000:
        return "high"
    return "xhigh"


def _to_text(block: dict) -> str:
    return block.get("text") or ""


def _extract_tool_result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    if not isinstance(c, list):
        return str(c)
    return "\n".join(
        b.get("text", "")
        for b in c
        if isinstance(b, dict) and b.get("type") == "text"
    )


def convert_user_message(msg: dict) -> list:
    content = msg.get("content")
    if isinstance(content, str):
        return [{"role": "user", "content": [{"type": "input_text", "text": content}]}]
    if not isinstance(content, list):
        return [{"role": "user", "content": [{"type": "input_text", "text": str(content)}]}]
    parts = []
    tool_outputs = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            # Codex Responses API: user text content uses `input_text`,
            # not Anthropic's plain `text`.
            parts.append({"type": "input_text", "text": _to_text(block)})
        elif btype == "image":
            source = block.get("source") or {}
            if source.get("data"):
                media = source.get("media_type") or "image/png"
                image_val = f"data:{media};base64,{source['data']}"
            else:
                image_val = source.get("url")
            parts.append({"type": "input_image", "image_url": image_val})
        elif btype == "tool_result":
            tool_outputs.append({
                "type": "function_call_output",
                "call_id": block.get("tool_use_id"),
                "output": _extract_tool_result_text(block),
            })
    items = []
    if parts:
        items.append({"role": "user", "content": parts})
    items.extend(tool_outputs)
    if not items:
        items = [{"role": "user", "content": [{"type": "input_text", "text": ""}]}]
    return items


def convert_assistant_message(msg: dict) -> list:
    content = msg.get("content")
    if isinstance(content, str):
        return [{"role": "assistant", "content": [{"type": "output_text", "text": content}]}]
    if not isinstance(content, list):
        return [{"role": "assistant", "content": [{"type": "output_text", "text": str(content)}]}]
    text_parts = []
    tool_calls = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            # Codex Responses API: assistant text content uses `output_text`.
            text_parts.append({"type": "output_text", "text": _to_text(block)})
        elif btype == "tool_use":
            try:
                args = json.dumps(block.get("input") or {})
            except Exception:
                args = "{}"
            tool_calls.append({
                "type": "function_call",
                "call_id": block.get("id"),
                "name": block.get("name"),
                "arguments": args,
            })
        # "thinking" blocks are dropped (mirror codex-protocol.js).
    items = []
    if text_parts:
        items.append({"role": "assistant", "content": text_parts})
    items.extend(tool_calls)
    if not text_parts and tool_calls:
        items.insert(0, {"role": "assistant", "content": [{"type": "output_text", "text": ""}]})
    if not items:
        items = [{"role": "assistant", "content": [{"type": "output_text", "text": ""}]}]
    return items


def convert_messages(messages) -> list:
    out = []
    if not isinstance(messages, list):
        return out
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "system":
            continue
        if role == "user":
            out.extend(convert_user_message(msg))
        elif role == "assistant":
            out.extend(convert_assistant_message(msg))
    return out


def convert_tools(tools) -> list:
    out = []
    if not isinstance(tools, list):
        return out
    for t in tools:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        out.append({
            "type": "function",
            "name": t.get("name"),
            "description": t.get("description") or "",
            "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            "strict": False,
        })
    return out


def anthropic_to_codex(body: dict, model: str) -> dict:
    """Mirror codex-protocol.js anthropicToCodexResponses."""
    if not isinstance(body, dict):
        raise ValueError("Request body is required")
    # Strip [1m]/[2m] suffix from model name. Claude Code uses these suffixes
    # to indicate the context window size (e.g. gpt-5.5[1m] = 1M context).
    # Codex backend with ChatGPT auth rejects them.
    raw_model = model or body.get("model") or ""
    clean_model = re.sub(r"\[(1|2)m\]$", "", str(raw_model)).strip()
    # Codex backend requires stream=true. We always request a stream from
    # Codex; if the Anthropic caller wanted non-streaming, we buffer the SSE
    # events and return a single non-streaming Anthropic response.
    out = {
        "model": clean_model,
        "stream": True,
        "store": False,
    }
    system = body.get("system")
    if isinstance(system, str):
        out["instructions"] = system
    elif isinstance(system, list):
        out["instructions"] = "\n".join(
            b.get("text", "")
            for b in system
            if isinstance(b, dict) and b.get("type") == "text"
        )
    else:
        # Codex requires the `instructions` field. If the Anthropic request
        # has no system prompt, we still must send an empty string.
        out["instructions"] = ""
    out["input"] = convert_messages(body.get("messages") or [])
    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        out["tools"] = convert_tools(tools)
        out["tool_choice"] = "auto"
        out["parallel_tool_calls"] = True
    # Codex uses `text.verbosity` instead of free-form text config.
    out["text"] = {"verbosity": "low"}
    # Codex Responses API requires including reasoning.encrypted_content so
    # subsequent calls can resume from the previous reasoning trace.
    out["include"] = ["reasoning.encrypted_content"]
    # Codex rejects top-level max_output_tokens / max_completion_tokens.
    # Cap the output implicitly via truncation; the upstream picks the
    # default. If the Anthropic request had max_tokens, we still respect
    # it via Anthropic's own stop logic; we just don't forward it.
    if isinstance(body.get("temperature"), (int, float)):
        out["temperature"] = body["temperature"]
    if isinstance(body.get("top_p"), (int, float)):
        out["top_p"] = body["top_p"]
    stops = body.get("stop_sequences")
    if isinstance(stops, list) and stops:
        out["stop"] = stops
    thinking = body.get("thinking")
    if isinstance(thinking, dict):
        reasoning = {}
        if isinstance(thinking.get("budget_tokens"), (int, float)):
            reasoning["effort"] = effort_from_budget(int(thinking["budget_tokens"]))
        elif isinstance(thinking.get("effort"), str):
            reasoning["effort"] = thinking["effort"]
        if thinking.get("summary"):
            reasoning["summary"] = thinking["summary"]
        else:
            reasoning["summary"] = "auto"
        if reasoning:
            out["reasoning"] = reasoning
    return out


# === Codex -> Anthropic streaming translation (mirror codex-handler.js) ===

def create_stream_state(model: str) -> dict:
    return {
        "msg_id": f"msg_{random_short_id(24)}",
        "model": model or DEFAULT_MODEL,
        "message_start_sent": False,
        "blocks": {},  # output_index -> {type, tool_id?, name?}
        "has_tool_calls": False,
        "final_usage": None,
        "final_status": None,
        "final_model": None,
        "final_response": None,
        "finished": False,
    }


def sse_event(event_type: str, data) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {event_type}\ndata: {payload}\n\n"


def build_message_start(state: dict) -> dict:
    state["message_start_sent"] = True
    return {
        "type": "message_start",
        "message": {
            "id": state["msg_id"],
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": state["final_model"] or state["model"],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }


def build_close_blocks(state: dict) -> list:
    return [
        {"type": "content_block_stop", "index": idx}
        for idx in list(state["blocks"].keys())
    ]


def _index_of(evt: dict, default: int = 0) -> int:
    val = evt.get("output_index")
    return val if isinstance(val, int) else default


def translate_codex_event(evt: dict, state: dict) -> list:
    """Mirror codex-handler.js translateCodexEvent.

    Returns a list of {"event": str, "data": dict} Anthropic SSE payloads.
    """
    if not isinstance(evt, dict):
        return []
    out = []
    t = evt.get("type")

    if t == "response.created":
        resp = evt.get("response") or {}
        if isinstance(resp, dict):
            if resp.get("id"):
                state["msg_id"] = f"msg_{resp['id']}"
            if resp.get("model"):
                state["final_model"] = resp["model"]
        if not state["message_start_sent"]:
            out.append({"event": "message_start", "data": build_message_start(state)})

    elif t == "response.output_item.added":
        item = evt.get("item") or {}
        idx = _index_of(evt)
        if not isinstance(item, dict):
            item = {}
        itype = item.get("type")
        if itype == "message":
            state["blocks"][idx] = {"type": "text"}
            out.append({
                "event": "content_block_start",
                "data": {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {"type": "text", "text": ""},
                },
            })
        elif itype == "function_call":
            state["has_tool_calls"] = True
            tool_id = (
                item.get("call_id")
                or item.get("id")
                or f"toolu_{random_short_id(24)}"
            )
            state["blocks"][idx] = {
                "type": "tool_use",
                "tool_id": tool_id,
                "name": item.get("name") or "",
            }
            out.append({
                "event": "content_block_start",
                "data": {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": item.get("name") or "",
                        "input": {},
                    },
                },
            })
        elif itype == "reasoning":
            state["blocks"][idx] = {"type": "thinking"}
            out.append({
                "event": "content_block_start",
                "data": {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
            })

    elif t in ("response.output_text.delta", "response.refusal.delta"):
        idx = _index_of(evt)
        out.append({
            "event": "content_block_delta",
            "data": {
                "type": "content_block_delta",
                "index": idx,
                "delta": {"type": "text_delta", "text": evt.get("delta") or ""},
            },
        })

    elif t == "response.function_call_arguments.delta":
        idx = _index_of(evt)
        out.append({
            "event": "content_block_delta",
            "data": {
                "type": "content_block_delta",
                "index": idx,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": evt.get("delta") or "",
                },
            },
        })

    elif t in (
        "response.reasoning_summary_text.delta",
        "response.reasoning_text.delta",
    ):
        idx = _index_of(evt)
        out.append({
            "event": "content_block_delta",
            "data": {
                "type": "content_block_delta",
                "index": idx,
                "delta": {
                    "type": "thinking_delta",
                    "thinking": evt.get("delta") or "",
                },
            },
        })

    elif t == "response.reasoning_summary_part.done":
        idx = _index_of(evt)
        out.append({
            "event": "content_block_delta",
            "data": {
                "type": "content_block_delta",
                "index": idx,
                "delta": {"type": "thinking_delta", "thinking": "\n\n"},
            },
        })

    elif t == "response.output_item.done":
        idx = _index_of(evt)
        if idx in state["blocks"]:
            out.append({
                "event": "content_block_stop",
                "data": {"type": "content_block_stop", "index": idx},
            })
            del state["blocks"][idx]

    elif t in ("response.completed", "response.done", "response.incomplete"):
        resp = evt.get("response") or {}
        if isinstance(resp, dict):
            # Save the full response so we can build a non-streaming Anthropic
            # response later (the SSE events lose the full output array).
            state["final_response"] = resp
            if resp.get("status"):
                state["final_status"] = resp["status"]
            elif not state["final_status"]:
                state["final_status"] = "completed"
            if resp.get("usage"):
                state["final_usage"] = resp["usage"]
            if resp.get("model"):
                state["final_model"] = resp["model"]
            if resp.get("id") and not state["message_start_sent"]:
                state["msg_id"] = f"msg_{resp['id']}"
                out.append({"event": "message_start", "data": build_message_start(state)})
        for stop in build_close_blocks(state):
            out.append({"event": "content_block_stop", "data": stop})
        state["blocks"].clear()
        stop_reason = map_stop_reason(state["final_status"], state["has_tool_calls"])
        usage = state["final_usage"] or {}
        if not isinstance(usage, dict):
            usage = {}
        details = usage.get("input_tokens_details") or {}
        cached = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
        out.append({
            "event": "message_delta",
            "data": {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {
                    "input_tokens": usage.get("input_tokens", 0) or 0,
                    "output_tokens": usage.get("output_tokens", 0) or 0,
                    "cache_read_input_tokens": cached or 0,
                    "cache_creation_input_tokens": 0,
                },
            },
        })
        out.append({"event": "message_stop", "data": {"type": "message_stop"}})
        state["finished"] = True

    elif t in ("response.failed", "error"):
        if not state["message_start_sent"]:
            out.append({"event": "message_start", "data": build_message_start(state)})
        for stop in build_close_blocks(state):
            out.append({"event": "content_block_stop", "data": stop})
        state["blocks"].clear()
        out.append({
            "event": "message_delta",
            "data": {
                "type": "message_delta",
                "delta": {"stop_reason": "error", "stop_sequence": None},
                "usage": {"output_tokens": 0},
            },
        })
        out.append({"event": "message_stop", "data": {"type": "message_stop"}})
        state["finished"] = True

    return out


def process_sse_chunk(buffer: str, chunk: bytes, state: dict, is_final: bool = False):
    """Parse Codex SSE events from buffer+chunk, returning (new_buffer, [sse_strings])."""
    try:
        text = chunk.decode("utf-8", errors="replace")
    except Exception:
        text = ""
    buffer += text
    out = []
    while True:
        sep = buffer.find("\n\n")
        if sep < 0:
            break
        raw = buffer[:sep]
        buffer = buffer[sep + 2:]
        if not raw.strip():
            continue
        for line in raw.split("\n"):
            line = line.rstrip("\r")
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                evt = json.loads(payload)
            except Exception:
                continue
            for piece in translate_codex_event(evt, state):
                out.append(sse_event(piece["event"], piece["data"]))
    if is_final and buffer.strip():
        for line in buffer.split("\n"):
            line = line.rstrip("\r")
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                evt = json.loads(payload)
            except Exception:
                continue
            for piece in translate_codex_event(evt, state):
                out.append(sse_event(piece["event"], piece["data"]))
        buffer = ""
    return buffer, out


def flush_stream(state: dict, out_list: list) -> None:
    """If stream didn't finish, close blocks and emit terminator."""
    if state["finished"]:
        return
    if not state["message_start_sent"]:
        out_list.append(sse_event("message_start", build_message_start(state)))
    for stop in build_close_blocks(state):
        out_list.append(sse_event("content_block_stop", stop))
    state["blocks"].clear()
    out_list.append(sse_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 0},
    }))
    out_list.append(sse_event("message_stop", {"type": "message_stop"}))


# === Codex -> Anthropic non-streaming translation ===

def codex_response_to_anthropic(resp, request_model: str) -> dict:
    """Mirror codex-handler.js codexResponseToAnthropic."""
    if not isinstance(resp, dict):
        return {
            "id": f"msg_{random_short_id(24)}",
            "type": "message",
            "role": "assistant",
            "model": request_model or DEFAULT_MODEL,
            "content": [],
            "stop_reason": "error",
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    content = []
    has_tool_calls = False
    output = resp.get("output")
    if not isinstance(output, list):
        output = []
    for item in output:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "reasoning":
            text = ""
            summary = item.get("summary")
            if isinstance(summary, list):
                text = "\n\n".join(
                    p.get("text", "")
                    for p in summary
                    if isinstance(p, dict) and p.get("type") == "summary_text"
                )
            content.append({"type": "thinking", "thinking": text})
        elif itype == "message":
            parts = item.get("content")
            if not isinstance(parts, list):
                parts = []
            for part in parts:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "output_text":
                    content.append({"type": "text", "text": part.get("text") or ""})
                elif ptype == "refusal":
                    content.append({"type": "text", "text": part.get("refusal") or ""})
        elif itype == "function_call":
            has_tool_calls = True
            args = item.get("arguments")
            if isinstance(args, str):
                try:
                    inp = json.loads(args)
                except Exception:
                    inp = {}
            elif isinstance(args, dict):
                inp = args
            else:
                inp = {}
            content.append({
                "type": "tool_use",
                "id": (
                    item.get("call_id")
                    or item.get("id")
                    or f"toolu_{random_short_id(24)}"
                ),
                "name": item.get("name") or "",
                "input": inp,
            })
    status = resp.get("status") or "completed"
    stop_reason = map_stop_reason(status, has_tool_calls)
    usage = resp.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    details = usage.get("input_tokens_details") or {}
    cached = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
    return {
        "id": f"msg_{resp['id']}" if resp.get("id") else f"msg_{random_short_id(24)}",
        "type": "message",
        "role": "assistant",
        "model": resp.get("model") or request_model or DEFAULT_MODEL,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("input_tokens", 0) or 0,
            "output_tokens": usage.get("output_tokens", 0) or 0,
            "cache_read_input_tokens": cached or 0,
            "cache_creation_input_tokens": 0,
        },
    }


# === HTTP handler ===

class CodexProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Quiet logging, with redaction.
    def log_message(self, fmt, *args):
        msg = fmt % args
        msg = re.sub(r"(Bearer |codex:)[^\s\"']+", r"\1***REDACTED***", msg)
        sys.stderr.write(f"[codex-proxy] {msg}\n")

    def _route(self):
        """Strip query string and return the path only."""
        return self.path.split("?", 1)[0]

    def do_GET(self):
        if self._route() == "/health":
            self._json_response(200, {"ok": True})
            return
        self._json_response(404, {"error": "not found"})

    def do_POST(self):
        if self._route() != "/v1/messages":
            self._json_response(404, {"error": "not found"})
            return
        self._handle_messages()

    # ---- Response helpers ----

    def _json_response(self, status: int, body: dict) -> None:
        body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body_bytes)
        self.wfile.flush()

    def _anthropic_error(self, status: int, message, error_type=None) -> None:
        etype = error_type or map_status_to_error_type(status)
        self._json_response(status, {
            "type": "error",
            "error": {"type": etype, "message": str(message or "")},
        })

    def _read_body(self) -> bytes:
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            return self.rfile.read(int(length))
        except Exception:
            return b""

    # ---- Main handler ----

    def _handle_messages(self) -> None:
        # 1. Parse Codex auth header. Claude Code may send either:
        #   Authorization: codex:<token>:<account_id>
        #   Authorization: Bearer codex:<token>:<account_id>
        # (Claude Code prepends "Bearer " to ANTHROPIC_AUTH_TOKEN.)
        auth_header = self.headers.get("Authorization", "")
        # Strip "Bearer " prefix if present
        if auth_header.startswith("Bearer "):
            auth_header = auth_header[len("Bearer "):]
        if not auth_header.startswith("codex:"):
            self._anthropic_error(
                401,
                "Invalid or missing Codex authorization. "
                "Expected 'codex:<access_token>:<account_id>'.",
                "authentication_error",
            )
            return
        rest = auth_header[len("codex:"):]
        last_colon = rest.rfind(":")
        if last_colon < 0 or not rest[:last_colon] or not rest[last_colon + 1:]:
            self._anthropic_error(
                401,
                "Invalid Codex auth format. "
                "Expected 'codex:<access_token>:<account_id>'.",
                "authentication_error",
            )
            return
        access_token = rest[:last_colon]
        account_id = rest[last_colon + 1:]

        # 2. Read + parse body
        body_bytes = self._read_body()
        if not body_bytes:
            self._anthropic_error(400, "Missing request body", "invalid_request_error")
            return
        try:
            body = json.loads(body_bytes)
        except json.JSONDecodeError as e:
            self._anthropic_error(
                400, f"Invalid JSON body: {e}", "invalid_request_error"
            )
            return
        if not isinstance(body, dict):
            self._anthropic_error(
                400, "Request body must be a JSON object", "invalid_request_error"
            )
            return

        model = body.get("model") or DEFAULT_MODEL

        # 3. Convert to Codex format
        try:
            codex_body = anthropic_to_codex(body, model)
        except Exception as e:
            self._anthropic_error(
                400, str(e) or "Failed to build Codex request",
                "invalid_request_error",
            )
            return

        codex_body["include"] = ["reasoning.encrypted_content"]
        if not isinstance(codex_body.get("text"), dict):
            codex_body["text"] = {"verbosity": "low"}

        service_tier = self.headers.get("X-Codex-Service-Tier")
        if service_tier:
            codex_body["service_tier"] = service_tier
        verbosity = self.headers.get("X-Codex-Text-Verbosity")
        if verbosity:
            codex_body["text"] = {"verbosity": verbosity}

        metadata = body.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("user_id"), str):
            codex_body["prompt_cache_key"] = metadata["user_id"][:64]
        else:
            session_hdr = self.headers.get("X-Session-Id")
            if session_hdr:
                codex_body["prompt_cache_key"] = session_hdr[:64]

        session_id = (
            self.headers.get("X-Session-Id")
            or codex_body.get("prompt_cache_key")
            or random_short_id(16)
        )

        # 4. Build upstream headers
        upstream_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id,
            "OpenAI-Beta": "responses=experimental",
            "accept": "text/event-stream",
        }
        if session_id:
            upstream_headers["session_id"] = session_id

        # 5. POST to Codex with retries
        base_url = os.environ.get("CODEX_BASE_URL") or CODEX_DEFAULT_BASE_URL
        url = base_url + CODEX_RESPONSES_PATH
        body_data = json.dumps(codex_body, ensure_ascii=False).encode("utf-8")
        # We always request a stream from Codex (`stream: True` in
        # anthropic_to_codex). The Anthropic caller may have asked for
        # streaming or non-streaming; we honor that on the way out.
        is_streaming = bool(body.get("stream"))
        req_timeout = 600

        last_error = None  # (status, err_text, headers)
        response = None
        for attempt in range(MAX_RETRIES + 1):
            req = urllib.request.Request(
                url, data=body_data, headers=upstream_headers, method="POST"
            )
            try:
                response = urllib.request.urlopen(req, timeout=req_timeout)
                break  # 2xx
            except urllib.error.HTTPError as e:
                status = e.code
                try:
                    err_text = e.read().decode("utf-8", errors="replace")
                except Exception:
                    err_text = ""
                last_error = (status, err_text, e.headers)
                response = None
                if not is_retryable_status(status):
                    break
                if not is_retryable_error_text(err_text):
                    break
                if attempt < MAX_RETRIES:
                    delay_ms = get_retry_delay_ms(e.headers, attempt)
                    time.sleep(delay_ms / 1000.0)
                    continue
                break
            except urllib.error.URLError as e:
                msg = str(e)
                if USAGE_LIMIT_RE.search(msg):
                    self._anthropic_error(429, msg, "rate_limit_error")
                    return
                last_error = (0, msg, None)
                if attempt < MAX_RETRIES:
                    delay_ms = retry_delay(attempt + 1)
                    time.sleep(delay_ms / 1000.0)
                    continue
                break
            except OSError as e:
                msg = str(e)
                if USAGE_LIMIT_RE.search(msg):
                    self._anthropic_error(429, msg, "rate_limit_error")
                    return
                last_error = (0, msg, None)
                if attempt < MAX_RETRIES:
                    delay_ms = retry_delay(attempt + 1)
                    time.sleep(delay_ms / 1000.0)
                    continue
                break

        if response is None:
            # Network failure or non-2xx final response.
            if last_error is None:
                self._anthropic_error(
                    500, "No response from Codex backend", "api_error"
                )
                return
            status, err_text, _ = last_error
            if status == 0:
                self._anthropic_error(
                    500, f"Network error: {err_text}", "api_error"
                )
                return
            err_body = None
            if err_text:
                try:
                    err_body = json.loads(err_text)
                except Exception:
                    err_body = None
            friendly = (
                parse_codex_error_body(err_body)
                or (isinstance(err_body, dict) and err_body.get("error", {}).get("message"))
                or err_text
                or f"Upstream error {status}"
            )
            self._anthropic_error(status, friendly)
            return

        # 6. Translate successful response
        try:
            if is_streaming:
                self._stream_anthropic_response(response, model)
            else:
                self._non_stream_anthropic_response(response, model)
        finally:
            try:
                response.close()
            except Exception:
                pass

    # ---- Streaming response ----

    def _stream_anthropic_response(self, upstream, model: str) -> None:
        state = create_stream_state(model)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.flush()

        def write_chunk(data: bytes) -> None:
            self.wfile.write(f"{len(data):x}\r\n".encode("ascii"))
            self.wfile.write(data)
            self.wfile.write(b"\r\n")
            self.wfile.flush()

        def write_terminator() -> None:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        buffer = ""
        client_dead = False
        try:
            while True:
                chunk = upstream.read(4096)
                if not chunk:
                    break
                buffer, translated = process_sse_chunk(
                    buffer, chunk, state, is_final=False
                )
                for piece in translated:
                    write_chunk(piece.encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            client_dead = True
        except Exception as e:
            sys.stderr.write(f"[codex-proxy] stream error: {e}\n")
        finally:
            if not client_dead:
                # Drain any trailing data in buffer
                tail = []
                if buffer.strip():
                    for line in buffer.split("\n"):
                        line = line.rstrip("\r")
                        if not line.startswith("data: "):
                            continue
                        payload = line[6:].strip()
                        if not payload or payload == "[DONE]":
                            continue
                        try:
                            evt = json.loads(payload)
                        except Exception:
                            continue
                        for piece in translate_codex_event(evt, state):
                            tail.append(sse_event(piece["event"], piece["data"]))
                flush_out = []
                flush_stream(state, flush_out)
                try:
                    for piece in tail + flush_out:
                        write_chunk(piece.encode("utf-8"))
                    write_terminator()
                except (BrokenPipeError, ConnectionResetError):
                    pass

    # ---- Non-streaming response ----

    def _non_stream_anthropic_response(self, upstream, model: str) -> None:
        # Codex always returns SSE. We consume the stream, then build a
        # single non-streaming Anthropic response from the final response
        # object embedded in the `response.completed` event.
        state = create_stream_state(model)
        buffer = ""
        try:
            while True:
                chunk = upstream.read(4096)
                if not chunk:
                    break
                buffer, _translated = process_sse_chunk(
                    buffer, chunk, state, is_final=False
                )
        except Exception as e:
            self._anthropic_error(
                502, f"Failed to read upstream response: {e}", "api_error"
            )
            return
        # Drain any trailing data
        if buffer.strip():
            process_sse_chunk(buffer, b"", state, is_final=True)

        final = state.get("final_response")
        if not isinstance(final, dict):
            # Stream did not complete; still try to return something useful
            self._anthropic_error(
                502, "Codex stream did not complete (no response.completed event)",
                "api_error",
            )
            return
        anthropic_data = codex_response_to_anthropic(final, model)
        body_bytes = json.dumps(anthropic_data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body_bytes)
        self.wfile.flush()


# === Entrypoint ===

def main() -> None:
    parser = argparse.ArgumentParser(description="Codex local proxy")
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int, default=8080,
        help="Port to listen on (default: 8080)",
    )
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), CodexProxyHandler)
    base_url = os.environ.get("CODEX_BASE_URL") or CODEX_DEFAULT_BASE_URL
    sys.stderr.write(
        f"[codex-proxy] listening on http://{args.host}:{args.port}\n"
    )
    sys.stderr.write(
        f"[codex-proxy] upstream: {base_url}{CODEX_RESPONSES_PATH}\n"
    )
    sys.stderr.write(
        "[codex-proxy] endpoints: GET /health, POST /v1/messages\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n[codex-proxy] shutting down\n")
        server.shutdown()


if __name__ == "__main__":
    main()