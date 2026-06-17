#!/usr/bin/env bash
# Live smoke test: hits the real backends through the smart-proxy
# to confirm each provider's representative model responds. This
# is NOT a CI test - it requires valid auth (Codex, MiniMax, OpenRouter,
# OpenCode Go) to be present on the host. Use it as a regression
# check after changes to the routing logic.
#
# Skips providers whose auth is not present (no failure).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SMART_PROXY="$SCRIPT_DIR/smart-proxy.py"

PORT_SMART="${CLAUDE_HARNESS_TEST_SMART_PORT:-18703}"
HOST="127.0.0.1"

# Load env if present
if [ -f "$HOME/.local/share/opencode/auth.json" ]; then
  OPENCODE_GO_KEY=$(python3 -c "import json; print(json.load(open('$HOME/.local/share/opencode/auth.json')).get('opencode-go', {}).get('key', ''))")
  MINIMAX_KEY=$(python3 -c "import json; print(json.load(open('$HOME/.local/share/opencode/auth.json')).get('minimax', {}).get('key', ''))")
  OPENROUTER_KEY=$(python3 -c "import json; print(json.load(open('$HOME/.local/share/opencode/auth.json')).get('openrouter', {}).get('key', ''))")
fi

ANTHROPIC_OK=0
[ -f "$HOME/.claude/.credentials.json" ] && ANTHROPIC_OK=1
CODEX_OK=0
[ -f "$HOME/.codex/auth.json" ] && CODEX_OK=1
OCG_OK=0
[ -n "${OPENCODE_GO_KEY:-}" ] && OCG_OK=1
MM_OK=0
[ -n "${MINIMAX_KEY:-}" ] && MM_OK=1
OR_OK=0
[ -n "${OPENROUTER_KEY:-}" ] && OR_OK=1

# Start smart-proxy
pkill -9 -f "smart-proxy" 2>/dev/null
sleep 1
export CLAUDE_HARNESS_MAIN_BACKEND=opencode-go
nohup python3 "$SMART_PROXY" --port "$PORT_SMART" >/tmp/smoke-sp.log 2>&1 &
SP_PID=$!
sleep 2
trap "kill $SP_PID 2>/dev/null" EXIT

if ! curl -sS --connect-timeout 1 "http://${HOST}:${PORT_SMART}/health" >/dev/null; then
  echo "smart-proxy not running on :$PORT_SMART" >&2
  exit 1
fi

probe() {
  local label="$1" model="$2" prefix="$3"
  if [ "$prefix" = "skip" ]; then
    printf "  %-30s SKIP (no auth)\n" "$label"
    return
  fi
  local body
  if [ -n "$prefix" ]; then
    body="{\"model\":\"${prefix}${model}\",\"max_tokens\":10,\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}"
  else
    body="{\"model\":\"${model}\",\"max_tokens\":10,\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}"
  fi
  local resp
  resp=$(curl -sS -m 20 -X POST "http://${HOST}:${PORT_SMART}/v1/messages" \
    -H "Content-Type: application/json" \
    -H "anthropic-version: 2023-06-01" \
    -H "Authorization: Bearer fake" \
    -d "$body" 2>&1)
  if echo "$resp" | grep -q '"type":"error"'; then
    local err
    err=$(echo "$resp" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('error',{}).get('message','?')[:60])" 2>/dev/null)
    printf "  %-30s ERROR: %s\n" "$label" "$err"
  elif echo "$resp" | grep -q 'message_start'; then
    printf "  %-30s OK\n" "$label"
  else
    printf "  %-30s UNKNOWN\n" "$label"
  fi
}

echo "=== OpenCode Go (main backend) ==="
[ "$OCG_OK" = "1" ] || { echo "  (skipped: no key)"; OCG_SKIP=skip; }
probe "minimax-m3"           "minimax-m3"          "${OCG_SKIP:-}"
probe "deepseek-v4-pro"      "deepseek-v4-pro"     "${OCG_SKIP:-}"
probe "deepseek-v4-flash"    "deepseek-v4-flash"   "${OCG_SKIP:-}"
probe "qwen3.7-plus"         "qwen3.7-plus"        "${OCG_SKIP:-}"
probe "qwen3.5-plus"         "qwen3.5-plus"        "${OCG_SKIP:-}"
probe "kimi-k2.7-code"       "kimi-k2.7-code"      "${OCG_SKIP:-}"
probe "kimi-k2.6"            "kimi-k2.6"           "${OCG_SKIP:-}"
probe "mimo-v2.5-pro"        "mimo-v2.5-pro"       "${OCG_SKIP:-}"
probe "glm-5.1"              "glm-5.1"             "${OCG_SKIP:-}"
probe "glm-5"                "glm-5"               "${OCG_SKIP:-}"

echo ""
echo "=== MiniMax ==="
[ "$MM_OK" = "1" ] || { echo "  (skipped: no key)"; MM_SKIP=skip; }
probe "MiniMax-M3"           "MiniMax-M3"          "${MM_SKIP:-}"
probe "MiniMax-M2.7"         "MiniMax-M2.7"        "${MM_SKIP:-}"

echo ""
echo "=== Codex ==="
[ "$CODEX_OK" = "1" ] || { echo "  (skipped: no auth)"; CX_SKIP=skip; }
probe "gpt-5.4-mini"         "gpt-5.4-mini"        "${CX_SKIP:-}"
probe "gpt-5.5"              "gpt-5.5"             "${CX_SKIP:-}"

echo ""
echo "=== OpenRouter ==="
[ "$OR_OK" = "1" ] || { echo "  (skipped: no key)"; OR_SKIP=skip; }
probe "openrouter/gpt-4"     "gpt-4"               "openrouter/"

echo ""
echo "Done."
