#!/usr/bin/env bash
# Test script for claude-codex wrapper.
# Crea un auth.json fake, corre el wrapper, y verifica las env vars exportadas.
# No necesita cuenta real de Codex.
set -euo pipefail

WRAPPER="$(cd "$(dirname "$0")/../../scripts" && pwd)/claude-codex"
TEST_HOME=$(mktemp -d)
export HOME="$TEST_HOME"

mkdir -p "$HOME/.codex" "$HOME/.local/bin" "$HOME/.config/claude-harness"

pass=0
fail=0

assert() {
  local name=$1
  local pattern=$2
  local output=$3
  if [[ "$output" == *"$pattern"* ]]; then
    echo "  PASS: $name"
    pass=$((pass + 1))
  else
    echo "  FAIL: $name"
    echo "    Expected pattern: $pattern"
    echo "    Got: $output"
    fail=$((fail + 1))
  fi
}

assert_not() {
  local name=$1
  local pattern=$2
  local output=$3
  if [[ "$output" != *"$pattern"* ]]; then
    echo "  PASS: $name"
    pass=$((pass + 1))
  else
    echo "  FAIL: $name (unexpected pattern found)"
    echo "    Unexpected pattern: $pattern"
    fail=$((fail + 1))
  fi
}

create_auth_json() {
  local mode=$1
  local exp_offset=${2:-3600}  # seconds from now
  python3 - "$mode" "$exp_offset" <<'PY'
import json, base64, time, sys, os
mode = sys.argv[1]
exp_offset = int(sys.argv[2])

def b64(x):
    return base64.urlsafe_b64encode(json.dumps(x).encode()).rstrip(b'=').decode()

if mode == "chatgpt":
    payload = {
        "sub": "user-123",
        "exp": int(time.time()) + exp_offset,
        "account_id": "test-acct-uuid",
        "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
    }
    header = b64({"alg": "RS256", "typ": "JWT"})
    body = b64(payload)
    sig = b64({"sig": "fake"})
    jwt = f"{header}.{body}.{sig}"
    auth = {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": jwt,
            "refresh_token": "rt-fake-token",
            "id_token": "id-fake-token",
            "account_id": "test-acct-uuid",
        },
        "last_refresh": "2024-01-01T00:00:00Z",
    }
elif mode == "apikey":
    auth = {
        "auth_mode": "apikey",
        "OPENAI_API_KEY": "sk-test-fake-key-1234567890",
        "last_refresh": "2024-01-01T00:00:00Z",
    }
else:
    sys.exit(f"unknown mode: {mode}")

with open(os.path.join(os.environ["HOME"], ".codex", "auth.json"), "w") as f:
    json.dump(auth, f, indent=2)
os.chmod(os.path.join(os.environ["HOME"], ".codex", "auth.json"), 0o600)
PY
}

create_fake_claude() {
  cat > "$HOME/.local/bin/claude" <<'BIN'
#!/usr/bin/env bash
echo "ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL:-}"
echo "ANTHROPIC_AUTH_TOKEN=${ANTHROPIC_AUTH_TOKEN:-}"
echo "ANTHROPIC_MODEL=${ANTHROPIC_MODEL:-}"
echo "ANTHROPIC_DEFAULT_OPUS_MODEL=${ANTHROPIC_DEFAULT_OPUS_MODEL:-}"
echo "ANTHROPIC_DEFAULT_SONNET_MODEL=${ANTHROPIC_DEFAULT_SONNET_MODEL:-}"
echo "ANTHROPIC_DEFAULT_HAIKU_MODEL=${ANTHROPIC_DEFAULT_HAIKU_MODEL:-}"
echo "CLAUDE_CODE_DISABLE_1M_CONTEXT=${CLAUDE_CODE_DISABLE_1M_CONTEXT:-}"
echo "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=${CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC:-}"
echo "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}"
echo "ARGS=$*"
BIN
  chmod +x "$HOME/.local/bin/claude"
}

# --- Test 1: chatgpt mode, valid JWT, no refresh needed ---
echo "=== Test 1: chatgpt mode, valid JWT, no refresh ==="
create_auth_json "chatgpt" 3600
create_fake_claude
export CLAUDE_HARNESS_CODEX_PROXY_URL="https://test-proxy.example.workers.dev"
export CLAUDE_HARNESS_CODEX_MODEL="gpt-5.4"
export CLAUDE_HARNESS_SLOT_HAIKU="gpt-5-mini"
unset CLAUDE_HARNESS_SLOT_OPUS CLAUDE_HARNESS_SLOT_SONNET

OUTPUT=$("$WRAPPER" --dangerously-skip-permissions 2>/dev/null) || {
  echo "  FAIL: wrapper exited non-zero"
  echo "  Output: $OUTPUT"
  fail=$((fail + 1))
}

assert "ANTHROPIC_BASE_URL set" "ANTHROPIC_BASE_URL=https://test-proxy.example.workers.dev" "$OUTPUT"
assert "ANTHROPIC_AUTH_TOKEN has codex: prefix" "ANTHROPIC_AUTH_TOKEN=codex:" "$OUTPUT"
assert "ANTHROPIC_AUTH_TOKEN contains account_id" ":test-acct-uuid" "$OUTPUT"
assert "ANTHROPIC_MODEL=gpt-5.4" "ANTHROPIC_MODEL=gpt-5.4" "$OUTPUT"
assert "ANTHROPIC_DEFAULT_HAIKU_MODEL overridden" "ANTHROPIC_DEFAULT_HAIKU_MODEL=gpt-5-mini" "$OUTPUT"
assert "ANTHROPIC_DEFAULT_OPUS_MODEL defaults to main" "ANTHROPIC_DEFAULT_OPUS_MODEL=gpt-5.4" "$OUTPUT"
assert "ANTHROPIC_DEFAULT_SONNET_MODEL defaults to main" "ANTHROPIC_DEFAULT_SONNET_MODEL=gpt-5.4" "$OUTPUT"
assert "CLAUDE_CODE_DISABLE_1M_CONTEXT=1" "CLAUDE_CODE_DISABLE_1M_CONTEXT=1" "$OUTPUT"
assert "ANTHROPIC_API_KEY unset" "ANTHROPIC_API_KEY=" "$OUTPUT"
assert "--model injected" "ARGS=--model gpt-5.4 --dangerously-skip-permissions" "$OUTPUT"

# --- Test 2: --model flag overrides, no injection ---
echo "=== Test 2: --model flag from user, no injection ==="
OUTPUT=$("$WRAPPER" --model o3 --dangerously-skip-permissions 2>/dev/null) || true
assert "user --model passed through" "ARGS=--model o3 --dangerously-skip-permissions" "$OUTPUT"
assert "ANTHROPIC_MODEL=gpt-5.4" "ANTHROPIC_MODEL=gpt-5.4" "$OUTPUT"
assert_not "no double --model injection" "--model gpt-5.4 --model" "$OUTPUT"

# --- Test 3: -h flag suppresses --model injection ---
echo "=== Test 3: -h flag suppresses --model injection ==="
OUTPUT=$("$WRAPPER" -h 2>/dev/null) || true
assert "no --model injection with -h" "ARGS=-h" "$OUTPUT"
assert_not "no --model in args" "ARGS=--model" "$OUTPUT"

# --- Test 4: apikey mode ---
echo "=== Test 4: apikey mode ==="
create_auth_json "apikey"
OUTPUT=$("$WRAPPER" --dangerously-skip-permissions 2>/dev/null) || {
  echo "  FAIL: wrapper exited non-zero in apikey mode"
  echo "  Output: $OUTPUT"
  fail=$((fail + 1))
}
assert "apikey mode: codex: prefix" "ANTHROPIC_AUTH_TOKEN=codex:sk-test-fake-key-1234567890:" "$OUTPUT"
assert "apikey mode: ANTHROPIC_MODEL=gpt-5.4" "ANTHROPIC_MODEL=gpt-5.4" "$OUTPUT"

# --- Test 5: missing auth.json ---
echo "=== Test 5: missing auth.json ==="
rm -f "$HOME/.codex/auth.json"
OUTPUT=$("$WRAPPER" --dangerously-skip-permissions 2>&1) || true
assert "dies on missing auth.json" "No se encontró" "$OUTPUT"

# --- Test 6: missing proxy URL ---
echo "=== Test 6: missing proxy URL ==="
create_auth_json "chatgpt" 3600
unset CLAUDE_HARNESS_CODEX_PROXY_URL
OUTPUT=$("$WRAPPER" --dangerously-skip-permissions 2>&1) || true
assert "dies on missing proxy URL" "CLAUDE_HARNESS_CODEX_PROXY_URL no configurada" "$OUTPUT"

# --- Test 7: expired JWT triggers refresh attempt ---
echo "=== Test 7: expired JWT triggers refresh (will fail without network) ==="
create_auth_json "chatgpt" -10  # expired 10 seconds ago
export CLAUDE_HARNESS_CODEX_PROXY_URL="https://test-proxy.example.workers.dev"
OUTPUT=$("$WRAPPER" --dangerously-skip-permissions 2>&1) || true
# Sin red, el refresh va a fallar. Aceptamos tanto "Refresh OAuth falló" como
# "refresh falló" (mensaje del Python inline).
if [[ "$OUTPUT" == *"Refresh OAuth falló"* ]] || [[ "$OUTPUT" == *"refresh falló"* ]]; then
  echo "  PASS: refresh attempted and failed with clear error"
  pass=$((pass + 1))
else
  echo "  FAIL: expected refresh error message"
  echo "  Output: $OUTPUT"
  fail=$((fail + 1))
fi

# --- Test 8: auth.json with unknown auth_mode ---
echo "=== Test 8: unknown auth_mode ==="
echo '{"auth_mode": "unknown"}' > "$HOME/.codex/auth.json"
chmod 600 "$HOME/.codex/auth.json"
OUTPUT=$("$WRAPPER" --dangerously-skip-permissions 2>&1) || true
assert "dies on unknown auth_mode" "auth.json inválido" "$OUTPUT"

# --- Test 9: auth.json with bad JSON ---
echo "=== Test 9: corrupted auth.json ==="
echo "not valid json {{{" > "$HOME/.codex/auth.json"
chmod 600 "$HOME/.codex/auth.json"
OUTPUT=$("$WRAPPER" --dangerously-skip-permissions 2>&1) || true
assert "dies on corrupted auth.json" "auth.json inválido" "$OUTPUT"

# --- Test 10: apikey mode with empty key ---
echo "=== Test 10: apikey mode with empty key ==="
echo '{"auth_mode": "apikey", "OPENAI_API_KEY": ""}' > "$HOME/.codex/auth.json"
chmod 600 "$HOME/.codex/auth.json"
OUTPUT=$("$WRAPPER" --dangerously-skip-permissions 2>&1) || true
assert "dies on empty apikey" "OPENAI_API_KEY está vacío" "$OUTPUT"

# --- Test 11: auto-start local proxy when proxy URL is localhost ---
echo "=== Test 11: auto-start local proxy ==="
create_auth_json "chatgpt" 3600

# Buscar el script del proxy (debe existir en scripts/)
PROXY_SCRIPT="$(cd "$(dirname "$0")/../../scripts" && pwd)/codex-proxy.py"
if [ ! -f "$PROXY_SCRIPT" ]; then
  echo "  SKIP: codex-proxy.py not found at $PROXY_SCRIPT"
else
  # Usar un puerto único para no chocar con otros tests
  TEST_PROXY_PORT=18765
  export CLAUDE_HARNESS_CODEX_PROXY_URL="http://127.0.0.1:$TEST_PROXY_PORT"

  # Matar cualquier proxy previo en ese puerto
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti:$TEST_PROXY_PORT 2>/dev/null | xargs -r kill -9 2>/dev/null || true
  fi
  # Esperar a que el puerto se libere
  sleep 1

  # El wrapper va a intentar arrancar el proxy y después correr la binaria claude
  # (que no existe en TEST_HOME). Capturamos la salida y verificamos que el
  # auto-arranque pasó.
  OUTPUT=$("$WRAPPER" --dangerously-skip-permissions 2>&1) || true

  # El output debe mencionar que se arrancó el proxy
  if [[ "$OUTPUT" == *"Auto-arrancando proxy local"* ]]; then
    echo "  PASS: wrapper attempted to auto-start the proxy"
    pass=$((pass + 1))
  else
    echo "  FAIL: expected 'Auto-arrancando proxy local' in output"
    echo "  Output: $OUTPUT"
    fail=$((fail + 1))
  fi

  # El proxy debe estar corriendo ahora (verificamos con /health)
  sleep 1
  if curl -fsS --max-time 2 "http://127.0.0.1:$TEST_PROXY_PORT/health" >/dev/null 2>&1; then
    echo "  PASS: proxy is running on port $TEST_PROXY_PORT"
    pass=$((pass + 1))
  else
    echo "  FAIL: proxy did not start (no /health response)"
    fail=$((fail + 1))
  fi

  # Cleanup: matar el proxy
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti:$TEST_PROXY_PORT 2>/dev/null | xargs -r kill -9 2>/dev/null || true
  fi
  unset CLAUDE_HARNESS_CODEX_PROXY_URL
fi

# --- Summary ---
echo ""
echo "=== Results: $pass passed, $fail failed ==="

# Cleanup
rm -rf "$TEST_HOME"

if [ "$fail" -gt 0 ]; then
  exit 1
fi
exit 0
