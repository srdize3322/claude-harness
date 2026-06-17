# claude-harness

> TUI launcher y harness multi-provider para Claude Code. Switch entre Anthropic, OpenRouter, OpenCode Go, MiniMax y Codex desde una sola interfaz. Pensado para self-hosting y power users.

```
╭─── Claude Code v2.1.178 ────────────╮
│  Bienvenido                          │
│  MiniMax-M3[1m] with high effort      │  ← provider + model + thinking level
│  /Users/santiago                     │
╰──────────────────────────────────────╯
❯ /context
   31.9k/460.8k tokens (7%)              ← context real del modelo (no fake 200k)
   Auto-compact window: 460.8k tokens     ← threshold = 90% del real
```

## Por qué

Claude Code está atado a Anthropic. Pero hay un montón de modelos en otros providers
(MiniMax, DeepSeek, Kimi, Qwen, GPT-5, etc.) que querés usar desde la misma interfaz,
con el mismo flujo, los mismos slash commands, las mismas skills, los mismos MCP servers.

`claude-harness` te da:

- **Multi-provider**: 5 providers listos (Anthropic, OpenRouter, OpenCode Go, MiniMax, Codex)
- **Model catalog**: 5000+ modelos via [models.dev](https://models.dev), con context windows reales
- **Per-agent model slots**: opus/sonnet/haiku configurables independientemente
- **Thinking levels**: `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultracode`
- **Auto-compact inteligente**: threshold al 90% del context real del modelo (no del fake 200k default de Claude Code)
- **Display correcto en `/context`**: muestra el context window real aunque el modelo no sea nativo de Anthropic
- **Sin tocar Claude Code**: el harness solo setea env vars estables; las updates de Anthropic no rompen nada

## Instalación

### One-liner (recomendado)

```bash
curl -fsSL https://raw.githubusercontent.com/srdize3322/claude-harness/main/install.sh | bash
```

Eso es. El script:

1. Verifica Python 3.8+ y curl
2. Descarga los scripts a `~/.local/share/claude-harness/scripts/`
3. Crea symlinks en `~/.local/bin/` (y te avisa si tenés que agregarlo al `PATH`)
4. No toca nada más

Después de instalar:

```bash
claude-harness
```

Y vas a entrar a la TUI.

### Instalación custom

```bash
# En otro directorio
CLAUDE_HARNESS_PREFIX=$HOME/mis-scripts \
CLAUDE_HARNESS_BIN=$HOME/mis-bin \
  curl -fsSL https://raw.githubusercontent.com/anomalyco/claude-harness/main/install.sh | bash

# Sin symlinks (manual PATH)
CLAUDE_HARNESS_NO_SYMLINK=1 \
  curl -fsSL https://raw.githubusercontent.com/anomalyco/claude-harness/main/install.sh | bash

# Desde un fork
CLAUDE_HARNESS_REPO_URL=https://github.com/mi-user/claude-harness \
  curl -fsSL https://raw.githubusercontent.com/anomalyco/claude-harness/main/install.sh | bash
```

### Requisitos

- macOS o Linux
- Python 3.8+
- `curl`
- [Claude Code](https://claude.com/download) instalado (la binaria que wrappeamos)

## Configuración

### 1. Crear `~/.config/claude-harness/.env`

```bash
mkdir -p ~/.config/claude-harness
```

Editá el archivo con tus defaults por provider:

```bash
# Default model por provider
CLAUDE_HARNESS_MINIMAX_MODEL=MiniMax-M3
CLAUDE_HARNESS_OPENROUTER_MODEL=anthropic/claude-sonnet-4-5
CLAUDE_HARNESS_OPENCODE_GO_MODEL=minimax-m3
CLAUDE_HARNESS_CLAUDE_MODEL=claude-sonnet-4-5
CLAUDE_HARNESS_CODEX_MODEL=gpt-5

# Opt-out del auto-context-window
# CLAUDE_HARNESS_CONTEXT_WINDOW=0
```

### 2. API keys por provider

Los providers buscan sus API keys en estos lugares (en orden):

| Provider | Variables / archivos |
|----------|---------------------|
| **Anthropic** | `ANTHROPIC_API_KEY` o `claude` login (OAuth) |
| **OpenRouter** | `OPENROUTER_API_KEY` o `~/.config/openrouter/auth.json` |
| **OpenCode Go** | `OPENCODE_GO_API_KEY` o `~/.local/share/opencode/auth.json` |
| **MiniMax** | `MINIMAX_API_KEY` o `~/.local/share/opencode/auth.json` |
| **Codex** | `OPENAI_API_KEY` o `codex` login |

Si no tenés la key, la TUI te lleva al menú de "Login / Configuración" del provider.

## Uso

### TUI interactiva

```bash
claude-harness
```

Flujo: **Provider → Modelo → Thinking → Subagentes → Permisos → Confirmar → Launch**.

Key bindings:
- `Enter`: elegir / avanzar
- `Esc`: volver
- `↑/↓`: navegar
- `Ctrl+R`: refresh catalog
- `Ctrl+F`: filter (en model picker)
- `Ctrl+S`: favorite
- `Ctrl+D`: default
- `Ctrl+L`: login
- `Ctrl+Q`: quit
- Type: search

### CLI directo

```bash
# Launch con provider + model + thinking + skip-permissions
claude-harness --provider minimax --model MiniMax-M3 --thinking high --dangerously-skip-permissions

# Con slots custom
claude-harness --provider minimax --model MiniMax-M3 \
  --slot-opus claude-sonnet-4-5 \
  --slot-sonnet MiniMax-M3 \
  --slot-haiku MiniMax-M2.5 \
  --dangerously-skip-permissions

# Refresh del catalog
claude-harness --refresh-catalog

# Skip a la TUI (llama claude directamente)
claude-harness --skip --model claude-sonnet-4-5
```

### Slash commands dentro de Claude Code

Una vez dentro, todo funciona normal:

- `/context`: muestra el context real del modelo
- `/compact`: compact manual (auto-compact al 90% ya viene configurado)
- `/effort <level>`: cambia el thinking level (high, max, ultracode, etc.)
- `/mcp`: lista MCP servers
- `/agents`: lista subagents
- `/memory`: gestiona CLAUDE.md
- `/status`: estado de la sesión

## Providers

### Anthropic (nativo)

```bash
claude-harness
# → Claude → claude-sonnet-4-5
```

Usa tu login de Claude. Sin API key, anda con OAuth.

### OpenRouter

```bash
claude-harness
# → OpenRouter → anthropic/claude-sonnet-4-5
```

Acceso a 400+ modelos via OpenRouter. Recomendado para mezclar providers.

### OpenCode Go

```bash
claude-harness
# → OpenCode Go → minimax-m3
```

Pasa por un [Cloudflare Worker](https://github.com/anomalyco/claude-harness) que traduce
Anthropic ↔ OpenAI. Útil para providers que solo exponen OpenAI-compatible.

### MiniMax

```bash
claude-harness
# → MiniMax → MiniMax-M3
```

Anthropic-compatible API. Modelos M1-M3 con context hasta 512k.

### Codex

```bash
claude-harness
# → Codex → gpt-5
```

OpenAI. Modelos GPT-5, GPT-4, o1, o3, etc.

## Cómo funciona (el truco del `[1m]`)

Claude Code internamente tiene una lista hardcodeada de modelos con sus context windows.
Para modelos no-Anthropic, devuelve 200k como default (incorrecto para M3 que tiene 512k).

**El truco**: Claude Code tiene un detector `Jf(H)` que matchea `/\[1m\]/i` en el model name
y trata al modelo como 1M context. Y tiene `aO(H)` que **strippea el `[1m]` antes de mandar
a la API**:

```js
function aO(H){ return H.replace(/\[(1|2)m\]/gi, "") }
```

El harness explota esto:

1. Detecta si el modelo NO es Anthropic
2. Le agrega `[1m]` al `--model` flag que va a Claude Code → display muestra 1M
3. Antes de cada API call, Claude Code strippea el `[1m]` → API recibe el nombre limpio
4. Setea `AUTO_COMPACT_WINDOW = real_ctx * 0.9` (90% del real)

Resultado:
- M3 (512k real): display 1M, threshold 460k, auto-compact a tiempo
- M2.5 (204k real): display 1M, threshold 184k, auto-compact a tiempo
- Claude Sonnet 4 (200k real): display 200k, threshold 180k, auto-compact normal

### Por qué no se rompe con updates de Claude Code

El harness solo usa:
- `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (env var estable, ya documentada)
- `[1m]` (feature nativo de Claude Code: `Jf` y `aO`)

Si Anthropic remueve el soporte `[1m]` en una versión futura, el display vuelve a 200k
pero el threshold de auto-compact sigue funcionando correctamente. No rompemos nada.

## Auto-compact inteligente

Por default, Claude Code asume 200k para modelos no-Anthropic y compacta a los 180k (90%).
Eso significa que con M3 (512k) perdés 65% del context real antes de compactar.

El harness hace:
- `CLAUDE_CODE_AUTO_COMPACT_WINDOW = int(real_ctx * 0.9)`
- M3 (512k) → 460.8k → auto-compact al 90% real
- M2.5 (204k) → 184.3k → auto-compact al 90% real

## Troubleshooting

### `claude-harness: command not found`

Agregá `~/.local/bin` a tu PATH:

```bash
# zsh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc

# bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

### `/context` muestra 200k en vez del real

El truco `[1m]` no se aplicó. Posibles causas:
1. Tu versión de Claude Code no soporta `[1m]` (muy vieja, < 2.1.0)
2. Estás usando Claude nativo (Anthropic), que ya conoce su context → correcto
3. El provider es OpenRouter con modelo `anthropic/*` → detectado como Anthropic → correcto

### El auto-compact no dispara

1. Verificá que `CLAUDE_HARNESS_CONTEXT_WINDOW` no esté en `0` en tu `.env`
2. Adentro de Claude Code corré `/config` → "Auto-compact window" debería mostrar el real
3. Si dice 200000, tu modelo está mal catalogado. Forzá el threshold:
   ```bash
   export CLAUDE_CODE_AUTO_COMPACT_WINDOW=460800
   claude-harness --provider minimax --model MiniMax-M3
   ```

### El modelo no se encuentra

Refresh el catalog:
```bash
claude-harness --refresh-catalog
```

O borrá el cache:
```bash
rm -rf ~/.config/claude-harness/models-catalog.json
```

### El `claude` binary no está en PATH

```bash
# macOS
brew install --cask claude-code

# O seguí las instrucciones de https://claude.com/download
```

## Desarrollo

```bash
# Clonar
git clone https://github.com/anomalyco/claude-harness.git
cd claude-harness

# Test el install
CLAUDE_HARNESS_NO_DEPS=1 \
CLAUDE_HARNESS_PREFIX=/tmp/test-ch \
CLAUDE_HARNESS_BIN=/tmp/test-ch-bin \
CLAUDE_HARNESS_REPO_URL="file://$PWD" \
  bash install.sh

# Validar sintaxis
python3 -c "import ast; ast.parse(open('scripts/claude-harness-ui.py').read())"
```

## Roadmap

- [ ] Hook `PreCompact` que override el threshold real (para modelos > 1M)
- [ ] Soporte para `[2m]` cuando Anthropic lo agregue
- [ ] Profile per-project (`.claude-harness.yaml` en el cwd)
- [ ] Tmux/screen launcher integrado

## Licencia

MIT
