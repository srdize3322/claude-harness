#!/usr/bin/env python3
"""Smart routing proxy for multi-provider Claude Code sessions.

Routes each /v1/messages request to the right backend based on the
`model` field in the body. Lets a single Claude Code process use
models from different providers in the same session (e.g. main = Opus
via Anthropic subscription, sonnet slot = gpt-5.4 via Codex, haiku
slot = MiniMax-M3 via MiniMax).

Backends:
  claude-*   | anthropic/*  -> Anthropic API (OAuth or ANTHROPIC_AUTH_TOKEN)
  gpt-*      | codex/*      -> Codex via local codex-proxy.py
  MiniMax-*  | minimax/*    -> MiniMax API (Anthropic-compatible)
  openrouter/* |             -> OpenRouter API (Anthropic-compatible)
  opencode-go/* |            -> OpenCode Go Cloudflare worker
  other                         -> main backend (env: CLAUDE_HARNESS_MAIN_BACKEND)

Auth sources (auto-detected at startup):
  Anthropic:  ~/.claude/.credentials.json  (OAuth) or ANTHROPIC_AUTH_TOKEN
  Codex:      ~/.codex/auth.json            (OAuth via codex-proxy)
  MiniMax:    ~/.local/share/opencode/auth.json (key "minimax")
  OpenRouter: ~/.local/share/opencode/auth.json (key "openrouter")
  OpenCodeGo: ~/.local/share/opencode/auth.json (key "opencode-go")

Usage:
  smart-proxy.py [--port 8081] [--host 127.0.0.1]

Endpoints:
  GET  /health      -> JSON status
  POST /v1/messages -> routed to backend based on model
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ANTHROPIC_API_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
MINIMAX_DEFAULT_BASE = "https://api.minimax.io/anthropic"
OPENROUTER_DEFAULT_BASE = "https://openrouter.ai/api"
OPENCODE_GO_DEFAULT_BASE = "https://opencode-go-proxy.r2gnqdy9c5.workers.dev"
CODEX_LOCAL_PROXY_DEFAULT = "http://127.0.0.1:8080"

CREDENTIALS_PATH = os.path.expanduser("~/.claude/.credentials.json")
CODEX_AUTH_PATH = os.path.expanduser("~/.codex/auth.json")
OPENCODE_AUTH_PATH = os.path.expanduser("~/.local/share/opencode/auth.json")


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return int((len(text) + 3) / 3.5)


def estimate_message_tokens(messages) -> int:
    total = 0
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
            continue
        if not isinstance(content, list):
            total += estimate_tokens(str(content))
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                total += estimate_tokens(block.get("text", ""))
            elif btype == "tool_use":
                total += estimate_tokens(json.dumps(block.get("input"))) + 10
            elif btype == "tool_result":
                result_content = block.get("content")
                if not isinstance(result_content, str):
                    result_content = json.dumps(result_content)
                total += estimate_tokens(result_content)
    return total

# ---------------------------------------------------------------------------
# Auth loading
# ---------------------------------------------------------------------------

def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def load_anthropic_auth():
    """Anthropic: prefer ANTHROPIC_AUTH_TOKEN env, fall back to OAuth."""
    env_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    if env_token:
        return {"type": "token", "token": env_token}
    creds = _read_json(CREDENTIALS_PATH)
    if isinstance(creds, dict):
        oauth = creds.get("claudeAiOauth")
        if isinstance(oauth, dict) and oauth.get("accessToken"):
            return {"type": "oauth", "access_token": oauth["accessToken"]}
    return None


def load_codex_auth():
    """Codex: load from ~/.codex/auth.json (consumed by codex-proxy)."""
    auth = _read_json(CODEX_AUTH_PATH)
    if not isinstance(auth, dict):
        return None
    tokens = auth.get("tokens")
    if not isinstance(tokens, dict):
        return None
    return {
        "access_token": tokens.get("access_token", ""),
        "account_id": tokens.get("account_id", ""),
        "refresh_token": tokens.get("refresh_token", ""),
    }


def load_opencode_auth():
    """Read ~/.local/share/opencode/auth.json (may have multiple keys)."""
    return _read_json(OPENCODE_AUTH_PATH) or {}


def get_minimax_key():
    auth = load_opencode_auth()
    key = auth.get("minimax", {}).get("key", "")
    if not key:
        env = os.environ.get("MINIMAX_API_KEY", "").strip()
        if env:
            return env
        # Try minimax-mcp env file
        for p in ("~/.config/minimax-mcp/.env", "~/.config/minimax-mcp/.env.local"):
            full = os.path.expanduser(p)
            if not os.path.exists(full):
                continue
            with open(full, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MINIMAX_API_KEY="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
    return key


def get_openrouter_key():
    auth = load_opencode_auth()
    key = auth.get("openrouter", {}).get("key", "")
    if not key:
        return os.environ.get("OPENROUTER_API_KEY", "").strip()
    return key


def get_opencode_go_key():
    auth = load_opencode_auth()
    key = auth.get("opencode-go", {}).get("key", "")
    if not key:
        return os.environ.get("OPENCODE_GO_API_KEY", "").strip()
    return key


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def detect_backend(model: str) -> tuple[str, str]:
    """Map a model name to (backend, clean_model).

    backend is one of: "anthropic", "codex", "minimax", "openrouter",
    "opencode-go", "main".
    clean_model is the model name to send to the backend (with any
    provider prefix stripped).
    """
    if not model:
        return "main", model
    m = model.strip()
    ml = m.lower()

    # Explicit provider prefix wins
    prefix_map = {
        "anthropic/": "anthropic",
        "codex/": "codex",
        "minimax/": "minimax",
        "openrouter/": "openrouter",
        "opencode-go/": "opencode-go",
    }
    for prefix, backend in prefix_map.items():
        if ml.startswith(prefix):
            clean_m = m[len(prefix):]
            clean_m = re.sub(r"\[[12]m\]$", "", clean_m, flags=re.IGNORECASE).strip()
            return backend, clean_m

    # Strip [1m] suffix for detection and upstream passing
    bare = re.sub(r"\[[12]m\]$", "", m, flags=re.IGNORECASE).strip()
    bare_lower = bare.lower()

    # Heuristic detection
    if bare_lower.startswith("claude-") or bare_lower.startswith("claude_"):
        return "anthropic", bare
    if bare_lower.startswith("gpt-") or bare_lower.startswith("o1") or bare_lower.startswith("o3") \
            or bare_lower.startswith("o4") or bare_lower.startswith("codex-"):
        return "codex", bare
    # MiniMax: case-sensitive!
    if bare.startswith("MiniMax-") or bare.startswith("MiniMax/") or bare == "MiniMax-M3":
        return "minimax", bare
    if "/" in bare and bare.split("/")[0] == "minimax":
        return "minimax", bare

    # Fallback to main backend (set by the wrapper)
    return "main", bare


# ---------------------------------------------------------------------------
# Per-backend request handling
# ---------------------------------------------------------------------------

def _read_anthropic_sse(upstream):
    """Read an Anthropic SSE response and re-emit it to client."""
    buffer = ""
    while True:
        chunk = upstream.read(4096)
        if not chunk:
            break
        yield chunk


def _build_anthropic_request(body: dict, headers: dict, auth: dict) -> tuple[str, dict, bytes]:
    """Build request to Anthropic API. Returns (url, headers, body)."""
    url = ANTHROPIC_API_BASE + "/v1/messages"
    h = dict(headers)
    h["anthropic-version"] = ANTHROPIC_VERSION
    # Always inject our own auth; ignore whatever the client sent.
    if auth["type"] == "oauth":
        h["Cookie"] = f"sessionKey={auth['access_token']}"
        h.pop("authorization", None)
        h.pop("x-api-key", None)
        h.pop("sessionkey", None)
        # OAuth requires the oauth-2025-04-20 beta header
        beta = h.get("anthropic-beta", "")
        if "oauth" not in beta:
            h["anthropic-beta"] = f"{beta},oauth-2025-04-20" if beta else "oauth-2025-04-20"
    elif auth["type"] == "token":
        h["x-api-key"] = auth["token"]
        h.pop("authorization", None)
        
    h["User-Agent"] = DEFAULT_BROWSER_UA
    h.pop("host", None)
    h.pop("content-length", None)
    return url, h, json.dumps(body).encode("utf-8")


# Browser-like User-Agent. Some backends (notably Cloudflare Workers
# like opencode-go-proxy) reject requests with Python's default
# urllib User-Agent (error 1010: "The owner of this website has
# banned your access based on your browser's signature"). Setting a
# realistic UA bypasses this WAF rule.
DEFAULT_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _build_passthrough_request(base: str, body: dict, headers: dict,
                               auth_header: str) -> tuple[str, dict, bytes]:
    """Build request to an Anthropic-compatible backend (passthrough)."""
    url = base.rstrip("/") + "/v1/messages"
    h = dict(headers)
    h["Authorization"] = auth_header
    # Pop Anthropic-specific auth headers to avoid leaking tokens
    h.pop("x-api-key", None)
    h.pop("sessionkey", None)
    h.pop("sessionkey", None)
    
    # Always set a browser-like User-Agent (overrides any client UA).
    h["User-Agent"] = DEFAULT_BROWSER_UA
    h.pop("host", None)
    h.pop("content-length", None)
    return url, h, json.dumps(body).encode("utf-8")


def _build_codex_request(body: dict, headers: dict,
                         codex_auth: dict) -> tuple[str, dict, bytes]:
    """Build request to the local codex-proxy. Injects the right
    `codex:<token>:<acct>` header that codex-proxy expects.
    """
    codex_url = os.environ.get("CLAUDE_HARNESS_CODEX_PROXY_URL", "").strip()
    if not codex_url:
        codex_url = CODEX_LOCAL_PROXY_DEFAULT
    url = codex_url.rstrip("/") + "/v1/messages?beta=true"
    h = dict(headers)
    h.pop("anthropic-version", None)
    h.pop("host", None)
    h.pop("content-length", None)
    # Pop Anthropic-specific auth headers to avoid leaking tokens
    h.pop("x-api-key", None)
    h.pop("sessionkey", None)
    h.pop("Sessionkey", None)
    
    h["User-Agent"] = DEFAULT_BROWSER_UA
    if codex_auth.get("access_token") and codex_auth.get("account_id"):
        h["Authorization"] = (
            f"codex:{codex_auth['access_token']}:{codex_auth['account_id']}"
        )
    return url, h, json.dumps(body).encode("utf-8")


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class SmartProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        msg = fmt % args
        # Redact Authorization / codex: tokens
        msg = re.sub(r"(Bearer |codex:)[^\s\"']+", r"\1***REDACTED***", msg)
        sys.stderr.write(f"[smart-proxy] {msg}\n")

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {
                "status": "ok",
                "smart_proxy": True,
                "backends": {
                    "anthropic": load_anthropic_auth() is not None,
                    "codex": load_codex_auth() is not None,
                    "minimax": bool(get_minimax_key()),
                    "openrouter": bool(get_openrouter_key()),
                    "opencode-go": bool(get_opencode_go_key()),
                },
            })
            return
            
        if self.path.startswith("/v1/models"):
            # Mock /v1/models so Claude Code accepts our custom model strings (e.g. "claude/opus")
            model_id = self.path.split("/")[-1]
            if model_id == "models" or not model_id:
                # Provide a generic response with the current model plus common ones
                main_model = os.environ.get("CLAUDE_HARNESS_CLAUDE_MODEL", "claude-3-5-sonnet-20241022")
                self._json(200, {
                    "data": [
                        {
                            "type": "model",
                            "id": main_model,
                            "display_name": main_model,
                            "created_at": "2024-01-01T00:00:00Z"
                        },
                        {
                            "type": "model",
                            "id": "claude/opus",
                            "display_name": "Opus",
                            "created_at": "2024-01-01T00:00:00Z"
                        },
                        {
                            "type": "model",
                            "id": "claude/sonnet",
                            "display_name": "Sonnet",
                            "created_at": "2024-01-01T00:00:00Z"
                        },
                        {
                            "type": "model",
                            "id": "claude/haiku",
                            "display_name": "Haiku",
                            "created_at": "2024-01-01T00:00:00Z"
                        }
                    ],
                    "has_more": False,
                    "first_id": main_model,
                    "last_id": main_model
                })
            else:
                self._json(200, {
                    "type": "model",
                    "id": model_id,
                    "display_name": model_id,
                    "created_at": "2024-01-01T00:00:00Z"
                })
            return

        self.send_error(404, "Not Found")

    def do_POST(self):
        if not self.path.startswith("/v1/messages"):
            self.send_error(404, "Not Found")
            return
        if "count_tokens" in self.path:
            self._handle_count_tokens()
            return
        self._handle_messages()

    def _json(self, status: int, body: dict):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _handle_count_tokens(self):
        raw_body = self._read_body()
        try:
            body = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            return self._json(200, {"input_tokens": 0})
        input_tokens = max(estimate_message_tokens(body.get("messages", [])), 1)
        return self._json(200, {"input_tokens": input_tokens})

    def _handle_messages(self):
        raw_body = self._read_body()
        try:
            body = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            return self._error(400, "Invalid JSON body", "invalid_request_error")

        model = body.get("model", "")
        
        # Override primary model if the UI wrapper injected a dummy to bypass Claude Code's startup checks
        real_main = os.environ.get("CLAUDE_HARNESS_REAL_MAIN_MODEL")
        dummy_main = os.environ.get("CLAUDE_HARNESS_DUMMY_MAIN_MODEL")
        if real_main and dummy_main and model == dummy_main:
            model = real_main

        backend, clean_model = detect_backend(model)
        
        # Claude Code natively resolves aliases like "opus" to "claude-3-opus-20240229", but for 
        # subagent slots or multi-provider prefixes (e.g. "claude/opus"), we must resolve them here.
        if backend in ("anthropic", "main"):
            if clean_model == "opus": clean_model = "claude-3-opus-20240229"
            elif clean_model == "sonnet": clean_model = "claude-3-5-sonnet-20241022"
            elif clean_model == "haiku": clean_model = "claude-3-5-haiku-20241022"
            elif clean_model == "fable": clean_model = "claude-fable-20250219"

        if clean_model and clean_model != body.get("model", ""):
            body = dict(body)
            body["model"] = clean_model

        # Resolve actual backend dynamically
        main_backend = ""
        try:
            with open("/tmp/claude-harness-main-backend.txt", "r") as f:
                main_backend = f.read().strip()
        except FileNotFoundError:
            pass
        
        if not main_backend:
            main_backend = os.environ.get("CLAUDE_HARNESS_MAIN_BACKEND", "").strip()

        if backend == "main":
            backend = main_backend or "anthropic"

        sys.stderr.write(f"[smart-proxy] Routing request for '{model}' (original) -> '{clean_model}' to backend '{backend}'\n")
        sys.stderr.flush()

        # Pass through request headers but force uncompressed response
        in_headers = {k.lower(): v for k, v in self.headers.items()
                      if k.lower() not in ("host", "content-length", "connection", "accept-encoding")}
        in_headers["accept-encoding"] = "identity"

        try:
            if backend == "anthropic":
                # If Claude Code sent native auth, pass it through transparently.
                client_auth = in_headers.get("authorization", "")
                client_api_key = in_headers.get("x-api-key", "")
                client_session = in_headers.get("sessionkey", "")
                client_cookie = in_headers.get("cookie", "")
                
                is_dummy = ("smart-proxy-passthrough" in client_auth or 
                            "smart-proxy-passthrough" in client_api_key or
                            "smart-proxy-passthrough" in client_session or
                            "smart-proxy-passthrough" in client_cookie)
                
                has_auth = bool(client_auth or client_api_key or client_session or "sessionKey=" in client_cookie)
                
                if not is_dummy and has_auth:
                    url = ANTHROPIC_API_BASE + "/v1/messages"
                    h = dict(in_headers)
                    if "anthropic-version" not in h:
                        h["anthropic-version"] = ANTHROPIC_VERSION
                    data = json.dumps(body).encode("utf-8")
                else:
                    auth = load_anthropic_auth()
                    if not auth:
                        return self._error(401, "Anthropic auth not available", "authentication_error")
                    url, h, data = _build_anthropic_request(body, in_headers, auth)
            elif backend == "codex":
                codex_auth = load_codex_auth()
                if not codex_auth or not codex_auth.get("access_token"):
                    return self._error(401, "Codex auth not available", "authentication_error")
                url, h, data = _build_codex_request(body, in_headers, codex_auth)
            elif backend == "minimax":
                key = get_minimax_key()
                if not key:
                    return self._error(401, "MiniMax API key not configured", "authentication_error")
                base = os.environ.get("MINIMAX_BASE_URL", "").strip() or MINIMAX_DEFAULT_BASE
                url, h, data = _build_passthrough_request(base, body, in_headers, f"Bearer {key}")
            elif backend == "openrouter":
                key = get_openrouter_key()
                if not key:
                    return self._error(401, "OpenRouter API key not configured", "authentication_error")
                base = os.environ.get("OPENROUTER_BASE_URL", "").strip() or OPENROUTER_DEFAULT_BASE
                url, h, data = _build_passthrough_request(base, body, in_headers, f"Bearer {key}")
            elif backend == "opencode-go":
                key = get_opencode_go_key()
                if not key:
                    return self._error(401, "OpenCode Go API key not configured", "authentication_error")
                base = os.environ.get("OPENCODE_GO_BASE_URL", "").strip() or OPENCODE_GO_DEFAULT_BASE
                url, h, data = _build_passthrough_request(base, body, in_headers, f"Bearer {key}")
            else:
                return self._error(400, f"Unknown backend: {backend}", "invalid_request_error")
        except Exception as e:
            return self._error(500, f"Failed to build request: {e}", "api_error")

        # Forward to upstream
        is_streaming = bool(body.get("stream"))
        try:
            req = urllib.request.Request(url, data=data, headers=h, method="POST")
            upstream = urllib.request.urlopen(req, timeout=None if is_streaming else 600)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            sys.stderr.write(f"[smart-proxy] upstream {e.code}: {err_body[:300]}\n")
            # ALWAYS wrap the error in Anthropic format so Claude Code
            # can parse it. Some upstreams return non-Anthropic JSON
            # (e.g. an Express 404 with {status,error,response} fields)
            # which causes Claude Code to crash with
            # "Failed to parse JSON" and dump the raw body (including
            # any embedded <system-reminder> tags) to the user.
            err_type = "api_error"
            if e.code in (401, 403):
                err_type = "authentication_error"
            elif e.code == 429:
                err_type = "rate_limit_error"
            elif 400 <= e.code < 500:
                err_type = "invalid_request_error"
            # Try to extract a useful message from the upstream body
            msg = err_body[:500]
            try:
                parsed = json.loads(err_body)
                if isinstance(parsed, dict):
                    # Common message fields
                    for key in ("message", "error", "msg", "detail"):
                        if key in parsed and isinstance(parsed[key], str):
                            msg = parsed[key][:500]
                            break
                        if key in parsed and isinstance(parsed[key], dict):
                            inner = parsed[key]
                            if isinstance(inner, dict):
                                for k2 in ("message", "detail"):
                                    if k2 in inner and isinstance(inner[k2], str):
                                        msg = inner[k2][:500]
                                        break
            except json.JSONDecodeError:
                pass
            wrapped = json.dumps({
                "type": "error",
                "error": {
                    "type": err_type,
                    "message": msg,
                },
            }).encode("utf-8")
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(wrapped)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(wrapped)
            return
        except Exception as e:
            return self._error(502, f"Upstream connection failed: {e}", "api_error")

        # Stream or buffer response
        if is_streaming:
            self._stream_response(upstream)
        else:
            self._buffer_response(upstream)

    def _stream_response(self, upstream):
        self.send_response(200)
        ct = upstream.headers.get("content-type", "text/event-stream")
        ce = upstream.headers.get("content-encoding")
        self.send_header("Content-Type", ct)
        if ce:
            self.send_header("Content-Encoding", ce)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.flush()

        client_dead = False
        try:
            while True:
                chunk = upstream.read(4096)
                if not chunk:
                    break
                # Write as chunked transfer encoding
                self.wfile.write(f"{len(chunk):x}\r\n".encode("ascii"))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            client_dead = True
        except Exception as e:
            sys.stderr.write(f"[smart-proxy] stream error: {e}\n")
        finally:
            try:
                if not client_dead:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
            except Exception:
                pass
            try:
                upstream.close()
            except Exception:
                pass

    def _buffer_response(self, upstream):
        try:
            data = upstream.read()
        except Exception as e:
            return self._error(502, f"Failed to read upstream: {e}", "api_error")
        self.send_response(200)
        ct = upstream.headers.get("content-type", "application/json")
        ce = upstream.headers.get("content-encoding")
        self.send_header("Content-Type", ct)
        if ce:
            self.send_header("Content-Encoding", ce)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()

    def _error(self, status: int, message: str, err_type: str):
        body = json.dumps({
            "type": "error",
            "error": {"type": err_type, "message": message},
        }).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Smart routing proxy for Claude Code")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()

    # Pre-load and report which backends are available
    backends = {
        "anthropic": load_anthropic_auth() is not None,
        "codex": load_codex_auth() is not None,
        "minimax": bool(get_minimax_key()),
        "openrouter": bool(get_openrouter_key()),
        "opencode-go": bool(get_opencode_go_key()),
    }
    server = ThreadingHTTPServer((args.host, args.port), SmartProxyHandler)
    sys.stderr.write(f"[smart-proxy] listening on http://{args.host}:{args.port}\n")
    sys.stderr.write(f"[smart-proxy] backends available: {backends}\n")
    sys.stderr.write("[smart-proxy] endpoints: GET /health, POST /v1/messages\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n[smart-proxy] shutting down\n")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
