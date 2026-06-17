#!/usr/bin/env bash
# claude-harness installer
# Usage: curl -fsSL https://raw.githubusercontent.com/<user>/claude-harness/main/install.sh | bash
#
# Options (set as env vars before running):
#   CLAUDE_HARNESS_PREFIX=/custom/path   Override install dir (default: $HOME/.local/share/claude-harness)
#   CLAUDE_HARNESS_BIN=$HOME/bin         Override bin dir for symlinks (default: $HOME/.local/bin)
#   CLAUDE_HARNESS_NO_SYMLINK=1          Don't create symlinks (manual PATH setup)
#   CLAUDE_HARNESS_NO_DEPS=1             Skip dependency check

set -euo pipefail

# REPO_URL apunta al repo base (ej: https://github.com/user/claude-harness).
# Por default usamos raw.githubusercontent.com para descargas.
GITHUB_REPO="${CLAUDE_HARNESS_REPO_URL:-https://github.com/srdize3322/claude-harness}"
REPO_BRANCH="${CLAUDE_HARNESS_REPO_BRANCH:-main}"

# Convertir github.com/... a raw.githubusercontent.com/...
# We also support a cache-bust query string for cases where the CDN has
# stale content (GitHub's raw.githubusercontent.com can take 5+ minutes
# to invalidate after a push).
if [[ "$GITHUB_REPO" == https://github.com/* ]]; then
  # Use the GitHub API for reliability (no CDN cache). The download()
  # function below falls back to raw.githubusercontent.com if the API call
  # fails (e.g., rate limit).
  RAW_URL="https://api.github.com/repos/${GITHUB_REPO#https://github.com/}/contents"
  USE_GITHUB_API=1
else
  # Permitir override completo via CLAUDE_HARNESS_RAW_URL
  RAW_URL="${CLAUDE_HARNESS_RAW_URL:-$GITHUB_REPO}"
  USE_GITHUB_API=0
fi

PREFIX="${CLAUDE_HARNESS_PREFIX:-$HOME/.local/share/claude-harness}"
BINDIR="${CLAUDE_HARNESS_BIN:-$HOME/.local/bin}"

# Colores (solo si stdout es TTY)
if [ -t 1 ]; then
  BOLD="\033[1m"
  DIM="\033[2m"
  GREEN="\033[32m"
  YELLOW="\033[33m"
  RED="\033[31m"
  RESET="\033[0m"
else
  BOLD=""; DIM=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi

log()  { printf "${BOLD}${GREEN}==>${RESET} %s\n" "$*"; }
warn() { printf "${BOLD}${YELLOW}==>${RESET} %s\n" "$*" >&2; }
fail() { printf "${BOLD}${RED}==>${RESET} %s\n" "$*" >&2; exit 1; }
dim()  { printf "    ${DIM}%s${RESET}\n" "$*"; }

# OS detection
OS="$(uname -s)"
case "$OS" in
  Darwin|Linux) ;;
  *) fail "Sistema operativo no soportado: $OS (solo macOS y Linux)" ;;
esac

log "claude-harness installer"
echo

# 1. Dependencias
if [ -z "${CLAUDE_HARNESS_NO_DEPS:-}" ]; then
  log "Verificando dependencias"
  missing=()
  for cmd in python3 curl; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      missing+=("$cmd")
    fi
  done
  if [ ${#missing[@]} -gt 0 ]; then
    fail "Faltan dependencias: ${missing[*]}. Instala con: brew install ${missing[*]} (macOS) o apt install ${missing[*]} (Linux)"
  fi
  PYTHON_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
  if [ "$PYTHON_MAJOR" -lt 3 ]; then
    fail "Se requiere Python 3.8+. Versión actual: $(python3 --version)"
  fi
  dim "Python: $(python3 --version)"
  dim "curl:   $(curl --version | head -1)"

  # Check opcional: claude binary
  if command -v claude >/dev/null 2>&1; then
    dim "claude: $(claude --version 2>/dev/null | head -1)"
  else
    warn "No se encontró el binario 'claude' en PATH"
    dim "Instala Claude Code desde: https://claude.com/download"
    dim "(podes seguir igual, vas a poder configurar los providers desde la TUI)"
  fi
  echo
fi

# 2. Crear directorios
log "Creando directorios"
mkdir -p "$PREFIX/scripts" "$PREFIX/docs"
dim "PREFIX:  $PREFIX"
dim "BINDIR:  $BINDIR"
echo

# 3. Descargar archivos
log "Descargando archivos del repo"
BASE_URL="$RAW_URL"

download() {
  local src="$1" dst="$2" required="${3:-true}"
  if [ "$USE_GITHUB_API" = "1" ]; then
    # Use GitHub API to avoid CDN cache staleness on raw.githubusercontent.com.
    # The API returns base64-encoded content; we decode it.
    local api_url="$RAW_URL/$src?ref=$REPO_BRANCH"
    local b64
    b64=$(curl -fsSL --connect-timeout 10 "$api_url" 2>/dev/null \
      | python3 -c "import json, sys; print(json.load(sys.stdin).get('content', ''))" 2>/dev/null) || true
    if [ -n "$b64" ]; then
      echo "$b64" | base64 -d > "$dst" 2>/dev/null
      if [ $? -eq 0 ]; then
        chmod +x "$dst" 2>/dev/null || true
        dim "$src"
        return 0
      fi
    fi
    # Fallback 1: try raw.githubusercontent.com with cache-bust query
    local url="https://raw.githubusercontent.com/${GITHUB_REPO#https://github.com/}/$REPO_BRANCH/$src?ts=$(date +%s%N)"
    if curl -fsSL --connect-timeout 10 -o "$dst" "$url" 2>/dev/null; then
      chmod +x "$dst" 2>/dev/null || true
      dim "$src (raw)"
      return 0
    fi
    # Fallback 2: download the full tarball and extract just this file.
    # Tarballs are versioned by commit SHA so CDN can't serve stale content.
    local tar_url="https://codeload.github.com/${GITHUB_REPO#https://github.com/}/tar.gz/$REPO_BRANCH"
    if command -v tar >/dev/null 2>&1; then
      local tmp_tar
      tmp_tar="$(mktemp)"
      if curl -fsSL --connect-timeout 30 -o "$tmp_tar" "$tar_url" 2>/dev/null; then
        # Find the file in the tarball (top-level dir is "<repo>-<sha>")
        if tar -tzf "$tmp_tar" 2>/dev/null | grep -E "/${src}\$" >/dev/null 2>&1; then
          tar -xzf "$tmp_tar" -C "$(dirname "$dst")" --strip-components=1 \
            "$(tar -tzf "$tmp_tar" 2>/dev/null | grep -E "/${src}\$" | head -1 | xargs -I{} dirname {} | sed 's|/[^/]*$||')/${src}" \
            2>/dev/null \
            && chmod +x "$dst" 2>/dev/null \
            && dim "$src (tarball)" \
            && rm -f "$tmp_tar" \
            && return 0
        fi
        rm -f "$tmp_tar"
      fi
    fi
  else
    local url="$BASE_URL/$src"
    if curl -fsSL --connect-timeout 10 -o "$dst" "$url" 2>/dev/null; then
      chmod +x "$dst" 2>/dev/null || true
      dim "$src"
      return 0
    fi
  fi
  if [ "$required" = "true" ]; then
    fail "No se pudo descargar: $src"
  else
    warn "Opcional no disponible: $src"
  fi
}

download "scripts/claude-harness"        "$PREFIX/scripts/claude-harness"        true
download "scripts/claude-harness-ui.py"  "$PREFIX/scripts/claude-harness-ui.py"  true
download "scripts/claude-minimax"        "$PREFIX/scripts/claude-minimax"        true
download "scripts/claude-openrouter"     "$PREFIX/scripts/claude-openrouter"     true
download "scripts/claude-opencode-go"    "$PREFIX/scripts/claude-opencode-go"    true
download "scripts/claude-native"         "$PREFIX/scripts/claude-native"         true
download "scripts/claude-codex"          "$PREFIX/scripts/claude-codex"          true
download "scripts/codex-proxy.py"        "$PREFIX/scripts/codex-proxy.py"        true
download "docs/README.md"                "$PREFIX/docs/README.md"                false
echo

# 4. Symlinks en BINDIR
if [ -z "${CLAUDE_HARNESS_NO_SYMLINK:-}" ]; then
  log "Creando symlinks en $BINDIR"
  mkdir -p "$BINDIR"
  for script in claude-harness claude-minimax claude-openrouter claude-opencode-go claude-native claude-codex; do
    ln -sf "$PREFIX/scripts/$script" "$BINDIR/$script"
    dim "$BINDIR/$script -> $PREFIX/scripts/$script"
  done
  echo
else
  warn "Symlinks no creados (CLAUDE_HARNESS_NO_SYMLINK=1)"
  dim "Agrega $PREFIX/scripts a tu PATH manualmente"
  echo
fi

# 5. Aviso sobre PATH
if [ -z "${CLAUDE_HARNESS_NO_SYMLINK:-}" ]; then
  case ":$PATH:" in
    *":$BINDIR:"*) ;;
    *)
      warn "Agrega $BINDIR a tu PATH"
      dim "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc  # o ~/.bashrc"
      echo
      ;;
  esac
fi

# 6. Estado final
log "Instalación completa"
echo
printf "    ${BOLD}Próximos pasos:${RESET}\n"
dim "  1. Asegúrate que ~/.local/bin está en tu PATH (ver arriba)"
dim "  2. Ejecutá: claude-harness"
dim "  3. Primera vez: elegí un provider y seguí las instrucciones de login"
echo
printf "    ${BOLD}Documentación:${RESET}\n"
dim "  https://github.com/srdize3322/claude-harness#readme"
dim "  cat $PREFIX/docs/README.md"
echo
