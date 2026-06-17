#!/usr/bin/env bash
# Smoke test for scripts/codex-proxy.py.
# Starts the proxy on a separate port, hits a few endpoints, then kills it.
# We don't have real Codex credentials, so we can only verify error paths and
# the request parsing — not a real end-to-end stream.
set -uo pipefail

PROXY_SCRIPT="$(cd "$(dirname "$0")" && pwd)/codex-proxy.py"
PORT="${CODEX_PROXY_TEST_PORT:-8081}"
HOST="127.0.0.1"
LOG="/tmp/codex-proxy-test.log"
PID_FILE="/tmp/codex-proxy-test.pid"

PASS=0
FAIL=0
FAILED_TESTS=()

cleanup() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
  fi
}
trap cleanup EXIT

ok() {
  PASS=$((PASS + 1))
  printf "  \033[32mok\033[0m   %s\n" "$1"
}

ko() {
  FAIL=$((FAIL + 1))
  FAILED_TESTS+=("$1")
  printf "  \033[31mfail\033[0m %s\n     got: %s\n" "$1" "${2:-}"
}

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [[ "$expected" == "$actual" ]]; then
    ok "$label"
  else
    ko "$label (expected '$expected', got '$actual')"
  fi
}

# 1. Syntax check
echo "==> Syntax check"
if python3 -m py_compile "$PROXY_SCRIPT" 2>&1; then
  ok "py_compile scripts/codex-proxy.py"
else
  ko "py_compile failed"
  exit 1
fi

# 2. Start proxy
echo "==> Start proxy on port $PORT"
: > "$LOG"
python3 "$PROXY_SCRIPT" --host "$HOST" --port "$PORT" >> "$LOG" 2>&1 &
PROXY_PID=$!
echo "$PROXY_PID" > "$PID_FILE"

# Wait for server to bind (max 5s)
for i in {1..50}; do
  if curl -s -o /dev/null "http://$HOST:$PORT/health" 2>/dev/null; then
    break
  fi
  if ! kill -0 "$PROXY_PID" 2>/dev/null; then
    ko "proxy crashed during startup"
    echo "--- proxy log ---"
    cat "$LOG"
    exit 1
  fi
  sleep 0.1
done
if curl -s -o /dev/null "http://$HOST:$PORT/health"; then
  ok "proxy accepts connections"
else
  ko "proxy did not become reachable"
  echo "--- proxy log ---"
  cat "$LOG"
  exit 1
fi

# 3. GET /health
echo "==> GET /health"
RESP="$(curl -s -w '\n%{http_code}' "http://$HOST:$PORT/health")"
BODY="$(printf '%s' "$RESP" | head -n1)"
STATUS="$(printf '%s' "$RESP" | tail -n1)"
assert_eq "/health status" "200" "$STATUS"
assert_eq "/health body"   '{"ok": true}' "$BODY"

# 4. GET unknown path -> 404
echo "==> GET /unknown"
STATUS="$(curl -s -o /dev/null -w '%{http_code}' "http://$HOST:$PORT/unknown")"
assert_eq "unknown GET status" "404" "$STATUS"

# 5. POST /v1/messages without auth -> 401
echo "==> POST /v1/messages without auth"
RESP="$(curl -s -w '\n%{http_code}' \
  -X POST "http://$HOST:$PORT/v1/messages" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5.4","messages":[{"role":"user","content":"hi"}],"max_tokens":16}')"
BODY="$(printf '%s' "$RESP" | head -n1)"
STATUS="$(printf '%s' "$RESP" | tail -n1)"
assert_eq "no-auth status" "401" "$STATUS"
# Verify Anthropic error envelope
ERROR_TYPE="$(printf '%s' "$BODY" | python3 -c 'import sys,json;d=json.loads(sys.stdin.read());print(d.get("error",{}).get("type",""))' 2>/dev/null || echo "")"
assert_eq "no-auth error type" "authentication_error" "$ERROR_TYPE"
ENVELOPE_TYPE="$(printf '%s' "$BODY" | python3 -c 'import sys,json;d=json.loads(sys.stdin.read());print(d.get("type",""))' 2>/dev/null || echo "")"
assert_eq "no-auth envelope type" "error" "$ENVELOPE_TYPE"

# 6. POST /v1/messages with malformed codex: header -> 401
echo "==> POST /v1/messages with malformed codex auth"
RESP="$(curl -s -w '\n%{http_code}' \
  -X POST "http://$HOST:$PORT/v1/messages" \
  -H 'Authorization: codex:no-colon' \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5.4","messages":[{"role":"user","content":"hi"}],"max_tokens":16}')"
BODY="$(printf '%s' "$RESP" | head -n1)"
STATUS="$(printf '%s' "$RESP" | tail -n1)"
assert_eq "bad-format status" "401" "$STATUS"
ERROR_TYPE="$(printf '%s' "$BODY" | python3 -c 'import sys,json;d=json.loads(sys.stdin.read());print(d.get("error",{}).get("type",""))' 2>/dev/null || echo "")"
assert_eq "bad-format error type" "authentication_error" "$ERROR_TYPE"

# 7. POST /v1/messages with codex: prefix and fake token -> upstream rejects
# (401 if upstream reachable, 502/500 if DNS/network fails, but never 200)
echo "==> POST /v1/messages with fake codex: token"
RESP="$(curl -s -w '\n%{http_code}' \
  -X POST "http://$HOST:$PORT/v1/messages" \
  -H 'Authorization: codex:eyJ-fake-jwt-token:00000000-0000-0000-0000-000000000000' \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5.4","messages":[{"role":"user","content":"hi"}],"max_tokens":16,"stream":false}')"
BODY="$(printf '%s' "$RESP" | head -n1)"
STATUS="$(printf '%s' "$RESP" | tail -n1)"
case "$STATUS" in
  401|403|500|502)
    ok "fake-token rejected with $STATUS"
    ;;
  *)
    ko "fake-token unexpected status" "$STATUS / $BODY"
    ;;
esac
# Verify Anthropic error envelope regardless
ERROR_TYPE="$(printf '%s' "$BODY" | python3 -c 'import sys,json
try:
  d=json.loads(sys.stdin.read())
  print(d.get("error",{}).get("type",""))
except Exception:
  print("")' 2>/dev/null || echo "")"
if [[ -n "$ERROR_TYPE" ]]; then
  ok "fake-token envelope is Anthropic (error.type=$ERROR_TYPE)"
else
  ko "fake-token envelope missing"
fi

# 8. POST unknown path -> 404
echo "==> POST unknown"
STATUS="$(curl -s -o /dev/null -w '%{http_code}' \
  -X POST "http://$HOST:$PORT/unknown" \
  -H 'Authorization: codex:fake:fake')"
assert_eq "unknown POST status" "404" "$STATUS"

# 9. Inline unit tests on the conversion logic (no network needed).
echo "==> Inline unit tests"
UNIT_OUT="$(python3 - <<'PY' 2>&1
import json, sys, importlib.util
spec = importlib.util.spec_from_file_location(
    "codex_proxy",
    "/Users/santiago/claude-harness/scripts/codex-proxy.py",
)
codex_proxy = importlib.util.module_from_spec(spec)
sys.modules["codex_proxy"] = codex_proxy
spec.loader.exec_module(codex_proxy)

# anthropic_to_codex: basic user message
b = {"model": "gpt-5.4", "messages": [{"role": "user", "content": "hi"}], "stream": True}
out = codex_proxy.anthropic_to_codex(b, "gpt-5.4")
assert out["model"] == "gpt-5.4", out
assert out["stream"] is True, out
assert out["store"] is False, out
assert out["input"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}], out

# system prompt string
b2 = {"system": "You are X.", "messages": [{"role": "user", "content": "hi"}]}
out2 = codex_proxy.anthropic_to_codex(b2, "gpt-5.4")
assert out2["instructions"] == "You are X.", out2

# system prompt array
b3 = {"system": [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}], "messages": []}
out3 = codex_proxy.anthropic_to_codex(b3, "gpt-5.4")
assert out3["instructions"] == "A\nB", out3

# tools
b4 = {"tools": [{"name": "f", "description": "d", "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}}}], "messages": []}
out4 = codex_proxy.anthropic_to_codex(b4, "gpt-5.4")
assert out4["tool_choice"] == "auto", out4
assert out4["tools"][0]["name"] == "f", out4
assert out4["tools"][0]["strict"] is False, out4

# assistant with tool_use
b5 = {"messages": [{"role": "assistant", "content": [
    {"type": "text", "text": "ok"},
    {"type": "tool_use", "id": "toolu_abc", "name": "f", "input": {"x": 1}},
]}]}
out5 = codex_proxy.anthropic_to_codex(b5, "gpt-5.4")
items = out5["input"]
assert items[0]["role"] == "assistant", items
assert items[0]["content"][0]["text"] == "ok", items
assert items[1]["type"] == "function_call", items
assert items[1]["call_id"] == "toolu_abc", items
assert json.loads(items[1]["arguments"]) == {"x": 1}, items

# tool_result -> function_call_output
b6 = {"messages": [{"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "toolu_abc", "content": "72F"},
]}]}
out6 = codex_proxy.anthropic_to_codex(b6, "gpt-5.4")
assert out6["input"][0]["type"] == "function_call_output", out6
assert out6["input"][0]["call_id"] == "toolu_abc", out6
assert out6["input"][0]["output"] == "72F", out6

# thinking budget -> effort
b7 = {"thinking": {"type": "enabled", "budget_tokens": 16000}, "messages": []}
out7 = codex_proxy.anthropic_to_codex(b7, "gpt-5.4")
assert out7["reasoning"]["effort"] == "high", out7
assert out7["reasoning"]["summary"] == "auto", out7

# map_stop_reason
assert codex_proxy.map_stop_reason("completed", False) == "end_turn"
assert codex_proxy.map_stop_reason("completed", True) == "tool_use"
assert codex_proxy.map_stop_reason("incomplete", False) == "max_tokens"
assert codex_proxy.map_stop_reason("failed", False) == "error"
assert codex_proxy.map_stop_reason("cancelled", False) == "end_turn"

# map_status_to_error_type
assert codex_proxy.map_status_to_error_type(401) == "authentication_error"
assert codex_proxy.map_status_to_error_type(429) == "rate_limit_error"
assert codex_proxy.map_status_to_error_type(503) == "api_error"

# translate_codex_event: text stream
st = codex_proxy.create_stream_state("gpt-5.4")
events = []
events += codex_proxy.translate_codex_event({"type": "response.created", "response": {"id": "resp_x", "model": "gpt-5.4"}}, st)
events += codex_proxy.translate_codex_event({"type": "response.output_item.added", "output_index": 0, "item": {"type": "message"}}, st)
events += codex_proxy.translate_codex_event({"type": "response.output_text.delta", "output_index": 0, "delta": "hello"}, st)
events += codex_proxy.translate_codex_event({"type": "response.output_item.done", "output_index": 0}, st)
events += codex_proxy.translate_codex_event({"type": "response.completed", "response": {"status": "completed", "usage": {"input_tokens": 3, "output_tokens": 1, "input_tokens_details": {"cached_tokens": 1}}}}, st)
types = [e["event"] for e in events]
assert types == ["message_start", "content_block_start", "content_block_delta", "content_block_stop", "message_delta", "message_stop"], types
assert events[1]["data"]["content_block"]["type"] == "text"
assert events[2]["data"]["delta"]["text"] == "hello"
assert events[4]["data"]["delta"]["stop_reason"] == "end_turn"
assert events[4]["data"]["usage"]["cache_read_input_tokens"] == 1

# translate_codexEvent: tool_use
st2 = codex_proxy.create_stream_state("gpt-5.4")
events2 = []
events2 += codex_proxy.translate_codex_event({"type": "response.created", "response": {"id": "r"}}, st2)
events2 += codex_proxy.translate_codex_event({"type": "response.output_item.added", "output_index": 0, "item": {"type": "function_call", "call_id": "call_x", "id": "fc_y", "name": "f"}}, st2)
events2 += codex_proxy.translate_codex_event({"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"a":'}, st2)
events2 += codex_proxy.translate_codex_event({"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '1}'}, st2)
events2 += codex_proxy.translate_codex_event({"type": "response.output_item.done", "output_index": 0}, st2)
events2 += codex_proxy.translate_codex_event({"type": "response.completed", "response": {"status": "completed"}}, st2)
assert events2[1]["data"]["content_block"]["type"] == "tool_use"
assert events2[1]["data"]["content_block"]["id"] == "call_x"
assert events2[5]["data"]["delta"]["stop_reason"] == "tool_use"

# parse_codex_error_body: usage_limit_reached
friendly = codex_proxy.parse_codex_error_body({"error": {"code": "usage_limit_reached", "plan_type": "plus", "resets_at": int(__import__("time").time()) + 600}})
assert "usage limit" in friendly, friendly

# parse_codex_error_body: invalid_api_key
friendly2 = codex_proxy.parse_codex_error_body({"error": {"code": "invalid_api_key", "message": "x"}})
assert "invalid or expired" in friendly2, friendly2

# is_retryable_status
assert codex_proxy.is_retryable_status(429) is True
assert codex_proxy.is_retryable_status(500) is True
assert codex_proxy.is_retryable_status(404) is False

print("OK: all inline unit tests passed")
PY
)"
if [[ "$UNIT_OUT" == "OK: all inline unit tests passed" ]]; then
  ok "inline unit tests (anthropic_to_codex, translate_codex_event, etc.)"
else
  ko "inline unit tests failed"
  echo "$UNIT_OUT"
fi

# 10. Log redaction check (direct call: ensures the regex actually redacts)
echo "==> Log redaction"
# Simulate a log line that would leak the Authorization header.
# Use a tiny HTTP request that includes a Bearer token in a URL we log.
LEAK_URL="http://$HOST:$PORT/v1/messages"
LEAK_RESP="$(curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST "$LEAK_URL" \
  -H 'Authorization: Bearer should-be-redacted-token' \
  -H 'Content-Type: application/json' \
  -d '{}' 2>/dev/null || true)"
# The default BaseHTTPRequestHandler log_message does not include headers, so
# instead we directly exercise the redaction regex by writing a synthetic line
# through the proxy's stderr capture. We do that by making the proxy's log_message
# print a line containing a token by sending a special request that forces it.
# Simpler: just call the redaction regex on a synthetic message and confirm.
REDACT_OUT="$(python3 - <<'PY'
import io, re, sys, importlib.util
spec = importlib.util.spec_from_file_location(
    "codex_proxy",
    "/Users/santiago/claude-harness/scripts/codex-proxy.py",
)
m = importlib.util.module_from_spec(spec)
sys.modules["codex_proxy"] = m
spec.loader.exec_module(m)

# Call log_message unbound so we can pass synthetic args.
import io
buf = io.StringIO()
real_stderr = sys.stderr
sys.stderr = buf
try:
    m.CodexProxyHandler.log_message(
        None,  # self (log_message only touches sys.stderr)
        '%s - - [date] "%s %s" %d %s',
        "127.0.0.1", "POST",
        "/v1/messages?token=codex:supersecrettoken:account-id",
        401, "-",
    )
finally:
    sys.stderr = real_stderr
out = buf.getvalue()
# Token segment "codex:supersecrettoken:account-id" must be gone.
assert "supersecrettoken" not in out, out
# And the redaction marker must appear in its place.
assert "codex:***REDACTED***" in out, out
print("OK")
PY
)"
if [[ "$REDACT_OUT" == "OK" ]]; then
  ok "log_message redacts codex: tokens"
else
  ko "log_message redaction broken"
  echo "$REDACT_OUT"
fi

echo
echo "==> Summary: $PASS passed, $FAIL failed"
if [[ $FAIL -gt 0 ]]; then
  echo "Failed tests:"
  printf '  - %s\n' "${FAILED_TESTS[@]}"
  echo
  echo "--- proxy log ---"
  cat "$LOG"
  exit 1
fi
exit 0