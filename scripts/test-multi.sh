#!/usr/bin/env bash
# test-multi.sh — test environment for claude-harness multi-provider scenarios.
#
# Usage:
#   test-multi.sh <case>           # run one case
#   test-multi.sh all              # run every case sequentially
#   test-multi.sh list             # list available cases
#
# Each case:
#   1. Kills any existing smart-proxy / codex-proxy so logs start fresh.
#   2. Sets the env vars the UI would normally set.
#   3. Runs `claude-multi --print "<prompt>"` with a timeout.
#   4. Greps the proxy log for the expected routing line and reports PASS/FAIL.
#
# Test logs land in /tmp/test-multi/<case>/ (proxy log + claude stdout/stderr).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROMPT="${TEST_PROMPT:-Responde solo con la palabra: pong}"
TIMEOUT="${TEST_TIMEOUT:-60}"
LOG_ROOT="/tmp/test-multi"
SMART_PORT="${CLAUDE_HARNESS_SMART_PROXY_PORT:-8081}"
CODEX_PORT="${CLAUDE_HARNESS_CODEX_PROXY_PORT:-8080}"

if [ -t 1 ]; then
  C_GREEN="\033[32m"; C_RED="\033[31m"; C_YELLOW="\033[33m"
  C_BOLD="\033[1m"; C_DIM="\033[2m"; C_RESET="\033[0m"
else
  C_GREEN=""; C_RED=""; C_YELLOW=""; C_BOLD=""; C_DIM=""; C_RESET=""
fi

info()  { printf "${C_BOLD}==>${C_RESET} %s\n" "$*"; }
pass()  { printf "${C_BOLD}${C_GREEN}PASS${C_RESET}  %s\n" "$*"; }
fail()  { printf "${C_BOLD}${C_RED}FAIL${C_RESET}  %s\n" "$*"; }
warn()  { printf "${C_BOLD}${C_YELLOW}WARN${C_RESET}  %s\n" "$*"; }
dim()   { printf "${C_DIM}      %s${C_RESET}\n" "$*"; }

CASES=(
  "anthropic-puro"
  "opus+minimax-slots"
  "opus+opencodego-slots"
  "multi-claude-opus"
  "multi-anthropic-opus"
  "multi-mixed"
)

kill_proxies() {
  # Kill anything listening on the proxy ports. Use `lsof -t` because pgrep can
  # miss the python process when started with nohup.
  for port in "$SMART_PORT" "$CODEX_PORT"; do
    local pids
    pids="$(lsof -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
    if [ -n "$pids" ]; then
      kill $pids 2>/dev/null || true
      sleep 0.3
      kill -9 $pids 2>/dev/null || true
    fi
  done
}

clear_env() {
  unset CLAUDE_HARNESS_SLOT_MAIN
  unset CLAUDE_HARNESS_SLOT_OPUS
  unset CLAUDE_HARNESS_SLOT_SONNET
  unset CLAUDE_HARNESS_SLOT_HAIKU
  unset CLAUDE_HARNESS_MAIN_BACKEND
  unset CLAUDE_HARNESS_REAL_MAIN_MODEL
  unset CLAUDE_HARNESS_DUMMY_MAIN_MODEL
  unset ANTHROPIC_MODEL
  unset ANTHROPIC_DEFAULT_OPUS_MODEL
  unset ANTHROPIC_DEFAULT_SONNET_MODEL
  unset ANTHROPIC_DEFAULT_HAIKU_MODEL
  rm -f /tmp/claude-harness-main-backend.txt
}

setup_case() {
  case "$1" in
    anthropic-puro)
      # Single-provider Claude native, just opus. Smoke test.
      export CLAUDE_HARNESS_SLOT_MAIN="opus"
      export CLAUDE_HARNESS_MAIN_BACKEND="anthropic"
      ;;
    opus+minimax-slots)
      # SANTIAGO'S REAL CASE: Anthropic opus main + MiniMax slots.
      export CLAUDE_HARNESS_SLOT_MAIN="opus"
      export CLAUDE_HARNESS_SLOT_SONNET="MiniMax-M3"
      export CLAUDE_HARNESS_SLOT_HAIKU="MiniMax-M2.7-highspeed"
      export CLAUDE_HARNESS_MAIN_BACKEND="anthropic"
      ;;
    opus+opencodego-slots)
      export CLAUDE_HARNESS_SLOT_MAIN="opus"
      export CLAUDE_HARNESS_SLOT_SONNET="opencode-go/sonnet"
      export CLAUDE_HARNESS_SLOT_HAIKU="opencode-go/haiku"
      export CLAUDE_HARNESS_MAIN_BACKEND="anthropic"
      ;;
    multi-claude-opus)
      # Regression for Bug 2: prefix "claude/" must route to anthropic.
      export CLAUDE_HARNESS_SLOT_MAIN="claude/opus"
      export CLAUDE_HARNESS_MAIN_BACKEND="anthropic"
      ;;
    multi-anthropic-opus)
      # Control: prefix "anthropic/" should still work (was always supported).
      export CLAUDE_HARNESS_SLOT_MAIN="anthropic/claude-opus-4-7"
      export CLAUDE_HARNESS_MAIN_BACKEND="anthropic"
      ;;
    multi-mixed)
      # Three backends coexisting.
      export CLAUDE_HARNESS_SLOT_MAIN="opus"
      export CLAUDE_HARNESS_SLOT_SONNET="MiniMax-M3"
      export CLAUDE_HARNESS_SLOT_HAIKU="codex/gpt-5.4"
      export CLAUDE_HARNESS_MAIN_BACKEND="anthropic"
      ;;
    *)
      fail "Caso desconocido: $1"
      return 1
      ;;
  esac
}

# Expected substring(s) in the proxy log to call a case PASS.
expected_routing() {
  case "$1" in
    anthropic-puro)        printf "backend 'anthropic'" ;;
    opus+minimax-slots)    printf "backend 'anthropic'" ;;  # main request first
    opus+opencodego-slots) printf "backend 'anthropic'" ;;
    multi-claude-opus)     printf "backend 'anthropic'" ;;
    multi-anthropic-opus)  printf "backend 'anthropic'" ;;
    multi-mixed)           printf "backend 'anthropic'" ;;
  esac
}

run_case() {
  local case_name="$1"
  local case_dir="$LOG_ROOT/$case_name"
  mkdir -p "$case_dir"
  rm -f "$case_dir"/*

  info "Caso: ${C_BOLD}${case_name}${C_RESET}"

  clear_env
  setup_case "$case_name" || return 1

  # Show config
  dim "main=${CLAUDE_HARNESS_SLOT_MAIN:-} sonnet=${CLAUDE_HARNESS_SLOT_SONNET:-(=main)} haiku=${CLAUDE_HARNESS_SLOT_HAIKU:-(=main)} backend=${CLAUDE_HARNESS_MAIN_BACKEND:-?}"

  kill_proxies

  export CLAUDE_HARNESS_SMART_PROXY_LOG="$case_dir/smart-proxy.log"
  export CLAUDE_HARNESS_CODEX_PROXY_LOG="$case_dir/codex-proxy.log"
  export CLAUDE_HARNESS_MULTI_VERBOSE="1"

  local out="$case_dir/claude.stdout"
  local err="$case_dir/claude.stderr"
  local rc=0

  # macOS doesn't ship `timeout` by default; use perl as a fallback.
  if command -v timeout >/dev/null 2>&1; then
    timeout "$TIMEOUT" "$SCRIPT_DIR/claude-multi" --print "$PROMPT" >"$out" 2>"$err" || rc=$?
  else
    perl -e 'alarm shift; exec @ARGV' "$TIMEOUT" \
      "$SCRIPT_DIR/claude-multi" --print "$PROMPT" >"$out" 2>"$err" || rc=$?
  fi

  local log="$case_dir/smart-proxy.log"
  local expect; expect="$(expected_routing "$case_name")"

  if [ ! -s "$log" ]; then
    fail "Sin log del proxy — no se hicieron requests."
    dim "stderr: $(tail -n 3 "$err" 2>/dev/null | tr '\n' ' ')"
    return 1
  fi

  local routing_lines; routing_lines="$(grep -c "\[smart-proxy\] Routing" "$log" || true)"
  local errors; errors="$(grep -cE "upstream [45][0-9][0-9]:|Unknown backend|authentication_error" "$log" || true)"

  if [ "$errors" -gt 0 ]; then
    fail "Proxy reporta errores ($errors líneas)."
    grep -E "upstream [45][0-9][0-9]:|Unknown backend|authentication_error" "$log" | head -3 | sed 's/^/        /'
    return 1
  fi

  if [ "$routing_lines" -gt 0 ] && grep -q "$expect" "$log"; then
    pass "Routing detectado ($routing_lines requests, código de salida: $rc)."
    dim "Log: $log"
    return 0
  fi

  fail "Routing esperado no encontrado: $expect"
  dim "Últimas líneas del proxy log:"
  tail -n 5 "$log" | sed 's/^/        /'
  return 1
}

case "${1:-}" in
  ""|-h|--help)
    sed -n '2,12p' "$0"
    exit 0
    ;;
  list)
    for c in "${CASES[@]}"; do echo "  $c"; done
    exit 0
    ;;
  all)
    total=0; passed=0
    for c in "${CASES[@]}"; do
      total=$((total + 1))
      if run_case "$c"; then passed=$((passed + 1)); fi
      echo
    done
    info "Resultado total: $passed/$total casos PASS"
    [ "$passed" -eq "$total" ]
    ;;
  *)
    if [[ " ${CASES[*]} " == *" $1 "* ]]; then
      run_case "$1"
    else
      fail "Caso desconocido: $1"
      echo "Casos válidos:"
      for c in "${CASES[@]}"; do echo "  $c"; done
      exit 1
    fi
    ;;
esac
