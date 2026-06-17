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
if [[ "$GITHUB_REPO" == https://github.com/* ]]; then
  RAW_URL="https://raw.githubusercontent.com/${GITHUB_REPO#https://github.com/}/$REPO_BRANCH"
else
  # Permitir override completo via CLAUDE_HARNESS_RAW_URL
  RAW_URL="${CLAUDE_HARNESS_RAW_URL:-$GITHUB_REPO}"
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
  local url="$BASE_URL/$src"
  if curl -fsSL --connect-timeout 10 -o "$dst" "$url" 2>/dev/null; then
    chmod +x "$dst" 2>/dev/null || true
    dim "$src"
  elif [ "$required" = "true" ]; then
    fail "No se pudo descargar: $url"
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
