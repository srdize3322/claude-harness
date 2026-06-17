#!/usr/bin/env bash
# Tests for scripts/smart-proxy.py and scripts/claude-multi
#
# Verifies:
# 1. Routing by model name (Anthropic, Codex, MiniMax, OpenRouter, OpenCode Go)
# 2. Explicit provider/ prefix routing
# 3. Multi-provider session via the smart proxy
# 4. Single-provider mode does NOT use the smart proxy (lazy optimization)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SMART_PROXY="$SCRIPT_DIR/smart-proxy.py"
CODEX_PROXY="$SCRIPT_DIR/codex-proxy.py"
WORKER_DIR="$SCRIPT_DIR/../worker"

PORT_SMART="${CLAUDE_HARNESS_TEST_SMART_PORT:-18701}"
PORT_CODEX="${CLAUDE_HARNESS_TEST_CODEX_PORT:-18702}"
HOST="127.0.0.1"

red()   { printf '\033[31m%s\033[0m' "$*"; }
green() { printf '\033[32m%s\033[0m' "$*"; }
yellow(){ printf '\033[33m%s\033[0m' "$*"; }
pass()  { printf "  %s %s\n" "$(green '[PASS]')" "$*"; }
fail()  { printf "  %s %s\n" "$(red   '[FAIL]')" "$*"; FAILS=$((FAILS+1)); }
info()  { printf "  %s %s\n" "$(yellow '[..  ]')" "$*"; }
header(){ printf "\n%s\n" "$(yellow "==> $*")"; }

FAILS=0
PASSES=0

cleanup() {
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  done
  PIDS=()
}
trap cleanup EXIT
PIDS=()

# Check that a real auth is available; if not, skip the live tests
ANTHROPIC_OK=0
CODEX_OK=0
MINIMAX_OK=0
[ -f "$HOME/.claude/.credentials.json" ] && ANTHROPIC_OK=1
[ -f "$HOME/.codex/auth.json" ] && CODEX_OK=1
[ -f "$HOME/.local/share/opencode/auth.json" ] && MINIMAX_OK=1

header "Setup: start smart-proxy and codex-proxy"
export CLAUDE_HARNESS_CODEX_PROXY_URL="http://${HOST}:${PORT_CODEX}"
python3 "$SMART_PROXY" --port "$PORT_SMART" >/tmp/sp-test.log 2>&1 &
PIDS+=($!)
sleep 1
python3 "$CODEX_PROXY" --port "$PORT_CODEX" >/tmp/cp-test.log 2>&1 &
PIDS+=($!)
sleep 1

if ! curl -sS --connect-timeout 1 "http://${HOST}:${PORT_SMART}/health" >/dev/null; then
  fail "smart-proxy no arranco en :$PORT_SMART"
  exit 1
fi
if ! curl -sS --connect-timeout 1 "http://${HOST}:${PORT_CODEX}/health" >/dev/null; then
  fail "codex-proxy no arranco en :$PORT_CODEX"
  exit 1
fi
pass "smart-proxy y codex-proxy arrancados"

# -------------------------------------------------------------------------
# Test 1: health endpoint reports all backends
# -------------------------------------------------------------------------
header "Test 1: health endpoint"
HEALTH=$(curl -sS "http://${HOST}:${PORT_SMART}/health")
if echo "$HEALTH" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['status']=='ok'; assert d['backends']['anthropic']==True" 2>/dev/null; then
  pass "health endpoint reporta backends disponibles"
else
  fail "health endpoint fallo: $HEALTH"
fi

# -------------------------------------------------------------------------
# Test 2: routing by model name
# -------------------------------------------------------------------------
header "Test 2: routing por nombre de modelo"
# 2a: claude-* -> Anthropic
if [ "$ANTHROPIC_OK" = "1" ]; then
  RESP=$(curl -sS -X POST "http://${HOST}:${PORT_SMART}/v1/messages" \
    -H "Content-Type: application/json" -H "anthropic-version: 2023-06-01" \
    -H "Authorization: Bearer fake-client" \
    -d '{"model":"claude-opus-4-6","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}')
  if echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); assert 'error' not in d or d.get('type')!='error'" 2>/dev/null; then
    pass "claude-opus-4-6 ruteo a Anthropic OK"
  else
    info "  response: $RESP" | head -c 200
    # Could be rate limit, but routing works if no proxy error
    if echo "$RESP" | grep -q "Invalid authentication"; then
      pass "claude-opus-4-6 ruteo a Anthropic (auth error esperado con token fake)"
    else
      fail "claude-opus-4-6 no llego a Anthropic: $RESP" | head -c 200
    fi
  fi
else
  info "skip: ~/.claude/.credentials.json no existe"
fi

# 2b: gpt-* -> Codex (via codex-proxy)
if [ "$CODEX_OK" = "1" ]; then
  RESP=$(curl -sS -X POST "http://${HOST}:${PORT_SMART}/v1/messages?beta=true" \
    -H "Content-Type: application/json" -H "anthropic-version: 2023-06-01" \
    -H "Authorization: Bearer fake-client" \
    -d '{"model":"gpt-5.4-mini","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}')
  if echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('model','').startswith('gpt-')" 2>/dev/null; then
    pass "gpt-5.4-mini ruteo a Codex OK (model: $(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('model',''))"))"
  else
    fail "gpt-5.4-mini no llego a Codex: $(echo "$RESP" | head -c 200)"
  fi
else
  info "skip: ~/.codex/auth.json no existe"
fi

# 2c: MiniMax-* -> MiniMax
if [ "$MINIMAX_OK" = "1" ]; then
  RESP=$(curl -sS -X POST "http://${HOST}:${PORT_SMART}/v1/messages" \
    -H "Content-Type: application/json" -H "anthropic-version: 2023-06-01" \
    -H "Authorization: Bearer fake-client" \
    -d '{"model":"MiniMax-M3","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}')
  if echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('model','')=='MiniMax-M3'" 2>/dev/null; then
    pass "MiniMax-M3 ruteo a MiniMax OK"
  else
    fail "MiniMax-M3 no llego a MiniMax: $(echo "$RESP" | head -c 200)"
  fi
else
  info "skip: opencode auth.json no existe"
fi

# -------------------------------------------------------------------------
# Test 3: explicit provider/ prefix routing
# -------------------------------------------------------------------------
header "Test 3: routing con prefijo explicito"
if [ "$CODEX_OK" = "1" ]; then
  RESP=$(curl -sS -X POST "http://${HOST}:${PORT_SMART}/v1/messages?beta=true" \
    -H "Content-Type: application/json" -H "anthropic-version: 2023-06-01" \
    -H "Authorization: Bearer fake-client" \
    -d '{"model":"codex/gpt-5.4","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}')
  if echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('model','').startswith('gpt-')" 2>/dev/null; then
    pass "codex/gpt-5.4 ruteo a Codex (model sin prefijo: $(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('model',''))"))"
  else
    fail "codex/gpt-5.4 fallo: $(echo "$RESP" | head -c 200)"
  fi
fi
if [ "$MINIMAX_OK" = "1" ]; then
  RESP=$(curl -sS -X POST "http://${HOST}:${PORT_SMART}/v1/messages" \
    -H "Content-Type: application/json" -H "anthropic-version: 2023-06-01" \
    -H "Authorization: Bearer fake-client" \
    -d '{"model":"minimax/MiniMax-M3","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}')
  if echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('model','')=='MiniMax-M3'" 2>/dev/null; then
    pass "minimax/MiniMax-M3 ruteo a MiniMax (prefix stripped)"
  else
    fail "minimax/MiniMax-M3 fallo: $(echo "$RESP" | head -c 200)"
  fi
fi

# -------------------------------------------------------------------------
# Test 4: provider inference function
# -------------------------------------------------------------------------
header "Test 4: provider inference (sin HTTP)"
export SMART_PROXY_PATH="$SMART_PROXY"
python3 - <<'PYEOF'
import os, sys, importlib.util
spec = importlib.util.spec_from_file_location("sp", os.environ["SMART_PROXY_PATH"])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
tests = [
    ("claude-opus-4-6", "anthropic"),
    ("claude-3-5-sonnet-20241022", "anthropic"),
    ("gpt-5.5", "codex"),
    ("gpt-5.4", "codex"),
    ("gpt-5.4-mini", "codex"),
    ("o3-mini", "codex"),
    ("MiniMax-M3", "minimax"),
    ("minimax-m3", "minimax"),
    ("minimax/M3", "minimax"),
    ("codex/gpt-5.5", "codex"),
    ("anthropic/claude-opus-4-6", "anthropic"),
    ("openrouter/gpt-4", "openrouter"),
]
ok = 0
for model, expected in tests:
    got = m.detect_backend(model)[0]
    if got == expected:
        ok += 1
        print(f"  [PASS] {model:30s} -> {got}")
    else:
        print(f"  [FAIL] {model:30s} -> {got} (expected {expected})")
        sys.exit(1)
print(f"  {ok}/{len(tests)} inference tests passed")
PYEOF
if [ $? -eq 0 ]; then
  pass "provider inference funciona para todos los modelos"
else
  fail "provider inference fallo"
fi

# -------------------------------------------------------------------------
# Test 5: claude-multi wrapper auto-starts proxies
# -------------------------------------------------------------------------
header "Test 5: claude-multi wrapper auto-start"
# Kill existing smart-proxy
for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
PIDS=()
sleep 1
# Run claude-multi (will fail because it tries to exec claude, but we
# can check the proxy was started)
unset CLAUDE_HARNESS_SMART_PROXY_HOST CLAUDE_HARNESS_SMART_PROXY_PORT
CLAUDE_HARNESS_SMART_PROXY_PORT="$PORT_SMART" \
  CLAUDE_HARNESS_CODEX_PROXY_PORT="$PORT_CODEX" \
  CLAUDE_HARNESS_SLOT_MAIN="claude-opus-4-6" \
  CLAUDE_HARNESS_SLOT_SONNET="gpt-5.4" \
  CLAUDE_HARNESS_SLOT_HAIKU="MiniMax-M3" \
  timeout 5 "$SCRIPT_DIR/claude-multi" --version >/tmp/cm-test.log 2>&1 || true
sleep 1
if curl -sS --connect-timeout 1 "http://${HOST}:${PORT_SMART}/health" >/dev/null; then
  pass "claude-multi auto-arranco smart-proxy"
else
  fail "claude-multi no arranco smart-proxy"
fi
if curl -sS --connect-timeout 1 "http://${HOST}:${PORT_CODEX}/health" >/dev/null; then
  pass "claude-multi auto-arranco codex-proxy (porque hay slot Codex)"
else
  fail "claude-multi no arranco codex-proxy"
fi
# Also verify env vars are set correctly
if grep -q "ANTHROPIC_BASE_URL" /tmp/cm-test.log 2>/dev/null; then
  info "claude-multi log: $(head -3 /tmp/cm-test.log)"
fi

# -------------------------------------------------------------------------
# Test 6: is_multi_provider_needed
# -------------------------------------------------------------------------
header "Test 6: TUI is_multi_provider_needed"
export TUI_PATH="$SCRIPT_DIR/claude-harness-ui.py"
python3 - <<'PYEOF'
import os, sys, importlib.util
spec = importlib.util.spec_from_file_location("ch_ui_test", os.environ["TUI_PATH"])
m = importlib.util.module_from_spec(spec)
sys.modules["ch_ui_test"] = m  # Required for @dataclass
spec.loader.exec_module(m)
# Test cases
cases = [
    # (main_provider, slots, expected_multi)
    ("codex", m.AgentSlots(), False),
    ("codex", m.AgentSlots(sonnet="gpt-5.4"), False),
    ("codex", m.AgentSlots(sonnet="gpt-5.4", haiku="gpt-5.4-mini"), False),
    ("codex", m.AgentSlots(sonnet="claude-opus-4-6"), True),
    ("claude", m.AgentSlots(sonnet="gpt-5.4", haiku="MiniMax-M3"), True),
    ("claude", m.AgentSlots(sonnet="claude-3-5-sonnet-20241022"), False),
    ("minimax", m.AgentSlots(sonnet="gpt-5.4"), True),
    ("minimax", m.AgentSlots(sonnet="minimax-m3"), False),  # same provider
]
ok = 0
for main_prov, slots, expected in cases:
    got = m.is_multi_provider_needed(main_prov, slots)
    if got == expected:
        ok += 1
        print(f"  [PASS] main={main_prov:8s} slots={slots} -> {got}")
    else:
        print(f"  [FAIL] main={main_prov:8s} slots={slots} -> {got} (expected {expected})")
print(f"  {ok}/{len(cases)} multi-provider detection tests passed")
PYEOF
if [ $? -eq 0 ]; then
  pass "is_multi_provider_needed correcto en todos los casos"
else
  fail "is_multi_provider_needed fallo en algunos casos"
fi

# -------------------------------------------------------------------------
# Summary
# -------------------------------------------------------------------------
header "Summary"
if [ "$FAILS" -eq 0 ]; then
  printf "  %s\n" "$(green "all tests passed, 0 failed")"
  exit 0
else
  printf "  %s\n" "$(red "$FAILS tests failed")"
  exit 1
fi
