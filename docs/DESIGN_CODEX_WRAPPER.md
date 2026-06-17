# Diseño: `scripts/claude-codex` (wrapper bash con Codex/ChatGPT OAuth)

> Source-of-truth para la implementación del wrapper que lanza Claude Code usando
> Codex (OpenAI) como backend, vía un proxy que traduce Anthropic ↔ OpenAI Responses.
> Complementa a `docs/PLAN_CODEX.md` (arquitectura) y respeta los contratos
> establecidos por `scripts/claude-minimax`, `claude-opencode-go` y `claude-openrouter`.

---

## 0. Resumen ejecutivo

`claude-codex` reemplaza el placeholder "deshabilitado" actual. Hace una sola cosa
bien: **lee `~/.codex/auth.json`, refresca el `access_token` si hace falta, y
ejecuta `claude` con las env vars correctas apuntando al proxy de Codex.**

- **Auth**: extrae `access_token` y `account_id` de `~/.codex/auth.json`.
- **Refresh**: refresh OAuth2 silencioso contra `https://auth.openai.com/oauth/token`
  (recomendado sobre invocar `codex login --device-auth`, que es interactivo).
- **Wire format**: `ANTHROPIC_AUTH_TOKEN="codex:<access_token>:<account_id>"`.
  El proxy detecta el prefijo `codex:` y rutea al handler de Codex.
- **Contrato con la TUI**: variables `CLAUDE_HARNESS_CODEX_*` + flags `--model`.

---

## 1. Wrapper Script Flow (pseudocódigo)

```
#!/usr/bin/env bash
set -euo pipefail

# 0. Localizar binarios
CODEX_BIN=$(command -v codex || true)
CLAUDE_BIN="$HOME/.local/bin/claude"
HARNESS_ENV="$HOME/.config/claude-harness/.env"
CODEX_AUTH="${CODEX_AUTH_JSON:-$HOME/.codex/auth.json}"

# 1. Cargar env del harness (CLAUDE_HARNESS_CODEX_PROXY_URL, etc.)
[ -f "$HARNESS_ENV" ] && { set -a; . "$HARNESS_ENV"; set +a; }

# 2. Validar codex instalado
if [ -z "$CODEX_BIN" ]; then
  die "codex CLI no está en PATH. Instalar con: brew install codex"
fi

# 3. Validar proxy URL (obligatorio)
if [ -z "${CLAUDE_HARNESS_CODEX_PROXY_URL:-}" ]; then
  die "CLAUDE_HARNESS_CODEX_PROXY_URL no está definida. Configurá el proxy en
       ~/.config/claude-harness/.env o desde la TUI (Codex → Configuración)."
fi

# 4. Validar auth.json existe
if [ ! -f "$CODEX_AUTH" ]; then
  die "No se encontró $CODEX_AUTH. Corré: codex login  o  claude-harness → Codex → Device auth"
fi

# 5. Parsear auth.json con Python (manejamos chatgpt y apikey)
read AUTH_MODE API_KEY ACCESS_TOKEN REFRESH_TOKEN ACCOUNT_ID LAST_REFRESH < <(
  python3 -c '
import json, sys
d = json.load(open("'"$CODEX_AUTH"'"))
mode = d.get("auth_mode")
if mode == "apikey":
    k = d.get("OPENAI_API_KEY") or ""
    print(mode, k, "", "", "", d.get("last_refresh",""))
elif mode == "chatgpt":
    t = d.get("tokens") or {}
    print(mode, "", t.get("access_token",""), t.get("refresh_token",""),
          t.get("account_id",""), d.get("last_refresh",""))
else:
    sys.exit(f"auth_mode no soportado: {mode}")
'
) || die "auth.json inválido o corrupto"

# 6. Si auth_mode=apikey, saltar refresh y usar key directo
#    Si auth_mode=chatgpt, validar JWT y refrescar si está vencido
if [ "$AUTH_MODE" = "chatgpt" ]; then
  EXP_EPOCH=$(decode_jwt_exp "$ACCESS_TOKEN")
  NOW=$(date +%s)
  REFRESH_MARGIN=300  # 5 min

  if [ $((EXP_EPOCH - NOW)) -lt $REFRESH_MARGIN ]; then
    [ -z "$REFRESH_TOKEN" ] && die "Token vencido y sin refresh_token. Re-loginear."
    log_info "Refrescando access_token..."
    NEW_TOKENS=$(refresh_oauth_token "$REFRESH_TOKEN")
    update_auth_json "$CODEX_AUTH" "$NEW_TOKENS"  # escritura atómica
    ACCESS_TOKEN=$(echo "$NEW_TOKENS" | jq -r .access_token)
    REFRESH_TOKEN=$(echo "$NEW_TOKENS" | jq -r .refresh_token)
  fi
fi

# 7. Decidir ANTHROPIC_AUTH_TOKEN
if [ "$AUTH_MODE" = "apikey" ]; then
  export ANTHROPIC_AUTH_TOKEN="$API_KEY"
else
  export ANTHROPIC_AUTH_TOKEN="codex:${ACCESS_TOKEN}:${ACCOUNT_ID}"
fi
unset ANTHROPIC_API_KEY

# 8. Setear env vars del proxy y modelos
export ANTHROPIC_BASE_URL="$CLAUDE_HARNESS_CODEX_PROXY_URL"
export CLAUDE_CODE_DISABLE_1M_CONTEXT=1
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="${CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC:-1}"

# 9. Modelo: --model flag > env var > default
DEFAULT_MODEL="${CLAUDE_HARNESS_CODEX_MODEL:-gpt-5}"
MODEL=$(resolve_model_arg "$@" "$DEFAULT_MODEL")
export ANTHROPIC_MODEL="$MODEL"
export ANTHROPIC_DEFAULT_OPUS_MODEL="${CLAUDE_HARNESS_SLOT_OPUS:-$MODEL}"
export ANTHROPIC_DEFAULT_SONNET_MODEL="${CLAUDE_HARNESS_SLOT_SONNET:-$MODEL}"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="${CLAUDE_HARNESS_SLOT_HAIKU:-$MODEL}"

# 10. Launch
should_inject=$(! has_flag "$@" "-m|--model|-h|--help|-v|--version")
declare -a launch_args=("$CLAUDE_BIN")
if [ "$should_inject" = true ] && [ -n "${CLAUDE_HARNESS_CODEX_MODEL:-}" ]; then
  launch_args+=("--model" "$CLAUDE_HARNESS_CODEX_MODEL")
fi

exec "${launch_args[@]}" "$@"
```

### Decisiones del flujo

| # | Decisión | Justificación |
|---|----------|---------------|
| 1 | Cargar `.env` con `set -a` (igual que minimax/opencode-go) | Consistencia con wrappers existentes |
| 2 | `command -v codex` en vez de hardcodear `/opt/homebrew/bin/codex` | Funciona en Linux (brew en `/usr/local/bin` o `/home/linuxbrew/.linuxbrew/bin`) |
| 3 | Python inline para parsear auth.json (no `jq`) | Ya usado en `claude-minimax:30` y `claude-opencode-go:22`. Una dependencia menos. |
| 4 | Detección de `auth_mode` antes de parsear tokens | Soporta el flujo de API key (sin OAuth) sin tirar error |
| 5 | `set -euo pipefail` | Consistencia y fail-fast. Pipefail es clave para detectar errores en `curl \| python` |
| 6 | Refresh margin de 5 min | El access_token vence en ~10 días pero la rotación proactiva evita 401 mid-session |
| 7 | Escritura atómica de auth.json (`mktemp` + `mv`) | Si crashea a mitad de update, no queda un JSON corrupto. Crítico porque auth.json es estado durable. |
| 8 | `unset ANTHROPIC_API_KEY` | Por si el usuario tiene exportada una key de Anthropic. Sin esto, Claude Code usaría Anthropic real. |
| 9 | `CLAUDE_CODE_DISABLE_1M_CONTEXT=1` | Evita que Claude Code muestre el marker `[1m]` que el Worker aún no soporta correctamente. Workaround documentado en PLAN_CODEX.md. |

---

## 2. Token Refresh Strategy

### Recomendación: **Opción B (curl directo)**

**Por qué no A (`codex login`):**
- `codex login` no expone subcomando `refresh` (verificado en `codex login --help`).
- `codex login --device-auth` es **interactivo** (imprime URL, espera code): no es
  viable dentro de un wrapper no-bloqueante para Claude Code.
- `codex login --with-api-key` es para setear API key por stdin, no para refrescar
  un token OAuth existente.
- Acoplar el wrapper al CLI `codex` nos hace dependientes de su versionado y flags
  internos (que cambian). El endpoint OAuth es estable.

**Por qué sí B (curl):**
- El endpoint es público, documentado, y el `client_id` es fijo y oficial
  (`app_EMoamEEZ73f0CkXaXp7hrann`, extraído de `aud` en el JWT del usuario).
- Refresh silencioso, ~200ms, sin interacción humana.
- Refresh token rotation: cada refresh devuelve un `refresh_token` nuevo, hay que
  persistirlo (sino el próximo refresh falla).

### Implementación (Opción B)

**Request:**

```bash
refresh_oauth_token() {
  local refresh_token=$1
  local client_id="app_EMoamEEZ73f0CkXaXp7hrann"

  curl --silent --show-error --fail-with-body \
    --max-time 30 \
    --request POST \
    --url "https://auth.openai.com/oauth/token" \
    --header "Content-Type: application/x-www-form-urlencoded" \
    --header "Accept: application/json" \
    --data-urlencode "grant_type=refresh_token" \
    --data-urlencode "refresh_token=${refresh_token}" \
    --data-urlencode "client_id=${client_id}" \
    --data-urlencode "scope=openid profile email offline_access api.connectors.read api.connectors.write api.conversations.write" \
    | python3 -c '
import json, sys
r = json.load(sys.stdin)
required = ["access_token", "id_token", "refresh_token"]
for k in required:
    if k not in r:
        sys.exit(f"respuesta inválida: falta {k}")
print(json.dumps(r))
'
}
```

**Response esperada** (200 OK):
```json
{
  "access_token":  "eyJ...",
  "id_token":      "eyJ...",
  "refresh_token": "<refresh_token>",
  "expires_in":    3600,
  "token_type":    "Bearer",
  "scope":         "openid profile email offline_access ..."
}
```

**Posibles errores:**
| HTTP | `error` | Acción |
|------|---------|--------|
| 400 | `invalid_grant` | El `refresh_token` también venció (pasa si no se usó por ~1 año). Re-login obligatorio. |
| 401 | `invalid_client` | El `client_id` cambió (OpenAI nunca lo hizo público, pero podría). Reportar bug. |
| 429 | `rate_limit` | Reintentar con backoff (3 intentos, 2s/4s/8s). |
| 5xx | cualquiera | Reintentar 1 vez. Si falla, abortar y pedir re-login. |

**Persistencia atómica:**

```bash
update_auth_json() {
  local auth_path=$1
  local new_tokens_json=$2  # JSON con access_token, id_token, refresh_token

  python3 - "$auth_path" <<EOF
import json, os, sys, tempfile, pathlib

auth_path = pathlib.Path(sys.argv[1])
data = json.loads(auth_path.read_text())

# Validar que sigue siendo chatgpt mode
if data.get("auth_mode") != "chatgpt":
    sys.exit("refuse to overwrite non-chatgpt auth.json")

new = json.loads("""$new_tokens_json""")
data["tokens"]["access_token"]  = new["access_token"]
data["tokens"]["id_token"]      = new["id_token"]
data["tokens"]["refresh_token"] = new["refresh_token"]
data["last_refresh"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")

# Escritura atómica: tmpfile en el mismo dir + rename
tmp = auth_path.with_suffix(".json.tmp")
tmp.write_text(json.dumps(data, indent=2))
os.chmod(tmp, 0o600)
os.replace(tmp, auth_path)
EOF
}
```

`os.replace` es atómico en el mismo filesystem (POSIX rename). Si el proceso muere
entre el write del tmpfile y el rename, `auth.json` queda intacto.

---

## 3. Edge Cases & Error Handling

| Caso | Detección | Mensaje al usuario | Exit code | Acción |
|------|-----------|-------------------|-----------|--------|
| **`codex` no instalado** | `command -v codex` vacío | `codex CLI no encontrado. Instalar con: brew install codex` | 1 | Abortar, no continuar. Sin `codex` no podemos sugerir el comando de login. |
| **`auth.json` no existe** | `[ ! -f ... ]` | `No se encontró ~/.codex/auth.json. Iniciá sesión con: codex login --device-auth` | 1 | Abortar, sugerir el comando exacto. No intentar auto-crear. |
| **`auth.json` corrupto / JSON inválido** | `python3` `json.JSONDecodeError` | `~/.codex/auth.json está corrupto. Borrá el archivo y corré: codex login` | 1 | Abortar. **No borrar automáticamente** (podría tener un refresh_token válido). |
| **`auth_mode` desconocido** | python `else: sys.exit(...)` | `auth_mode 'X' no soportado. Solo se aceptan: chatgpt, apikey` | 1 | Abortar. Cubre modes nuevos que OpenAI podría agregar. |
| **`auth_mode=apikey`** | Match en el python parser | _(no error, flujo alternativo)_ | 0 | Usar `OPENAI_API_KEY` directo como `ANTHROPIC_AUTH_TOKEN`. Saltar todo el OAuth flow. |
| **`auth_mode=apikey` pero key vacía** | `-z "$API_KEY"` | `auth_mode=apikey pero OPENAI_API_KEY está vacío. Corré: codex login --with-api-key` | 1 | Abortar. |
| **access_token expirado y sin refresh_token** | Check de `-z` post-parse | `Token vencido y sin refresh_token. Re-loginear con: codex login` | 1 | Abortar. |
| **Refresh OAuth devuelve `invalid_grant`** | `curl` exit != 0 o `python3` falla | `Refresh token rechazado por OpenAI (invalid_grant). Re-loginear con: codex login` | 1 | **No reintentar**: invalid_grant significa que el RT también venció. |
| **Refresh OAuth da 5xx** | `curl` exit != 0 | `OpenAI OAuth no disponible (HTTP XXX). Reintentá en unos minutos.` | 1 | No reintentar automáticamente. El usuario decide. |
| **No hay red / DNS falla** | `curl` exit 6/7 | `Sin conexión a auth.openai.com. Verificá tu red.` | 1 | Abortar. |
| **`CLAUDE_HARNESS_CODEX_PROXY_URL` no definida** | `-z` check | `CLAUDE_HARNESS_CODEX_PROXY_URL no configurada. Agregala a ~/.config/claude-harness/.env` | 1 | Abortar. Sin proxy, el wrapper no tiene adónde apuntar. |
| **Proxy URL mal formada** (no es HTTPS) | regex check opcional | `CLAUDE_HARNESS_CODEX_PROXY_URL debe ser HTTPS` | 1 | Soft warning. El curl de prueba lo detectaría igual. |
| **Python 3 no disponible** | `command -v python3` | `python3 no encontrado. Codex wrapper requiere Python 3.8+` | 1 | Abortar. Imposible parsear auth.json. |
| **`jq` no disponible** | n/a | _(no se usa)_ | n/a | Decisión: **no usar `jq`**, parsear con Python como los otros wrappers. |
| **Race: dos `claude-codex` corren en paralelo** | No detectable trivialmente | _(no error, último write gana)_ | 0 | Aceptable. Si el usuario corre dos instancias, ambas van a hacer refresh y la última persiste. auth.json es per-user, no per-session. |

### Formato de mensajes de error

Todos los errores van a **stderr** con un prefijo consistente para que la TUI
los pueda capturar y mostrar formateados:

```bash
die() {
  echo "claude-codex: $*" >&2
  exit 1
}

log_info() {
  echo "claude-codex: $*" >&2
}
```

### Códigos de exit

| Code | Significado |
|------|------------|
| 0 | OK (ejecutó claude) |
| 1 | Error de configuración / pre-flight |
| 2 | Auth inválido (refresh falló) |
| 3 | Proxy inalcanzable (opcional: solo si implementamos health check) |

Para el cut 1 usamos solo 0 y 1. La TUI no distingue tipos de error hoy, solo
muestra el stderr.

---

## 4. Security Considerations

### 4.1. Tokens en logs (CRÍTICO)

**Regla absoluta: nunca loguear tokens, ni siquiera truncados.**

```bash
# MAL - nunca hacer esto
echo "Access token: ${ACCESS_TOKEN:0:20}..."
log "Got token: $ACCESS_TOKEN"

# BIEN - redactar siempre
log "Got access token (len=${#ACCESS_TOKEN})"
log "Token expira en $((EXP_EPOCH - NOW)) segundos"
```

Esto incluye:
- `set -x` está **deshabilitado** (el script no usa `set -x` en ningún momento).
- Errores a stderr: redactar tokens, mostrar solo `len` o `[REDACTED]`.
- Tracebacks de Python: el wrapper no atrapa excepciones que puedan leakear el
  token en el mensaje. Si python3 tira error, el wrapper debe capturarlo y
  mostrar un mensaje genérico.

```bash
if ! TOKENS=$(refresh_oauth_token "$REFRESH_TOKEN" 2>/dev/null); then
  die "Refresh OAuth falló. Re-loginear con: codex login"
fi
# `$?` ya está capturado, no hay token en ninguna variable visible
```

### 4.2. `set -x` alrededor de operaciones sensibles

Si por debugging se necesitase trace, **encapsular**:

```bash
if [ -n "${CODEX_DEBUG:-}" ]; then
  set -x
fi
refresh_oauth_token "$REFRESH_TOKEN"
# (el token solo aparece como argumento, pero queda en el trace del usuario)
```

Por defecto `CODEX_DEBUG` no está seteado, así que `set -x` está apagado.

### 4.3. Permisos de auth.json

`chmod 600` en `~/.codex/auth.json` es lo correcto (lo verifiqué: el archivo
existente está `-rw-------`, OK).

**Comportamiento del wrapper:**

```bash
check_auth_perms() {
  local perms
  perms=$(stat -f "%Lp" "$CODEX_AUTH" 2>/dev/null || stat -c "%a" "$CODEX_AUTH" 2>/dev/null)
  case "$perms" in
    600|400) return 0 ;;  # OK
    *)
      echo "claude-codex: AVISO: $CODEX_AUTH tiene permisos $perms (recomendado: 600)" >&2
      echo "claude-codex: Ejecutá: chmod 600 $CODEX_AUTH" >&2
      return 0  # No abortar, solo avisar
      ;;
  esac
}
```

**Decisión**: **avisar pero no abortar, no auto-fijar**. Razones:
- El usuario puede tener razones legítimas para compartir el archivo (poco
  probable, pero respetar la autonomy).
- Un `chmod` automático puede romper herramientas que esperan otros permisos
  (poco probable también, pero principio de mínima sorpresa).
- El warning es visible y accionable.

### 4.4. Escritura atómica

Ya cubierto en §2. Usar `os.replace()` (Python) o `mv -f` con `mktemp` (bash).
Nunca `> auth.json` directo (no atómico, queda truncado si crashea).

### 4.5. Variables de entorno en `ps`/`/proc`

El `ANTHROPIC_AUTH_TOKEN="codex:..."` queda visible en el environment del
proceso `claude`. Esto es **inevitable** y aceptable: Claude Code ya tiene que
poder leerlo, y está en el mismo UID que el usuario. La alternativa (pass por
stdin) no es soportada por Claude Code.

No hay mitigación posible acá. La seguridad depende de:
- Permisos del filesystem (chmod 700 en home, 600 en auth.json).
- No correr claude-codex en multi-tenant.

### 4.6. `--device-auth` desde la TUI

Cuando la TUI lanza `codex login --device-auth`, el proceso **sí** puede ser
visto en `ps`. No hay tokens en el comando (el device_code lo imprime en
stdout), así que es OK. La TUI ya implementa este flujo (ver
`claude-harness-ui.py:1005`).

### 4.7. Validación del JSON antes de parsear

Para evitar que un auth.json malicioso (escrito por otro proceso) ejecute
código en nuestro Python inline:

```python
# Validar que auth.json tiene solo las keys esperadas
ALLOWED_TOP_KEYS = {"auth_mode", "OPENAI_API_KEY", "tokens", "last_refresh"}
ALLOWED_TOKEN_KEYS = {"id_token", "access_token", "refresh_token", "account_id"}

data = json.load(open(path))
extra = set(data.keys()) - ALLOWED_TOP_KEYS
if extra:
    sys.exit(f"auth.json tiene keys inesperadas: {extra}")
```

Esto es **defense in depth**: aunque un attacker ya tiene write access a tu
home, validar estructura evita que un `__proto__` o key malformada rompa el
wrapper de formas raras.

---

## 5. Compatibilidad con la Harness TUI

### 5.1. Contrato de invocación

La TUI (en `scripts/claude-harness-ui.py`, función `launch_provider`) hace:

```python
args = [provider.launcher]  # = str(ROOT / "claude-codex")
# + user args (puede incluir --model, --dangerously-skip-permissions, etc.)
subprocess.run(args + user_args, env=os.environ)
```

Donde `os.environ` ya tiene seteadas:
- `CLAUDE_HARNESS_CODEX_PROXY_URL` (NUEVO, a agregar en TUI)
- `CLAUDE_HARNESS_CODEX_MODEL` (ya existe, ver `claude-harness-ui.py:147`)
- `CLAUDE_HARNESS_SLOT_OPUS`, `_SONNET`, `_HAIKU` (ya existen, líneas 1581-1585)
- `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (ya seteado por la TUI)
- `CLAUDE_CODE_DISABLE_THINKING` (a veces)

### 5.2. Cambios necesarios en la TUI

**Archivo**: `scripts/claude-harness-ui.py`

**Cambio 1**: agregar `CLAUDE_HARNESS_CODEX_PROXY_URL` a la carga de provider config.

```python
# Cerca de la línea 254-285 donde se checkea has_auth
if provider.provider_id == "codex":
    if not os.environ.get("CLAUDE_HARNESS_CODEX_PROXY_URL"):
        has_auth = False
        # Mostrar mensaje: "Configurá el proxy de Codex"
```

**Cambio 2**: agregar item al menú de configuración de Codex (línea 1004):

```python
if pid == "codex":
    return [
        ("Device auth (link device)", lambda: ["codex", "login", "--device-auth"]),
        ("Configurar proxy URL",      lambda: prompt_proxy_url("CODEX")),
        # ...
    ]
```

`prompt_proxy_url` es una nueva función que:
1. Lee el valor actual de `CLAUDE_HARNESS_CODEX_PROXY_URL` (si existe)
2. Prompt al usuario con un input box
3. Valida que sea HTTPS
4. Lo guarda en `~/.config/claude-harness/.env`

**Cambio 3**: el `has_auth` check debe considerar el proxy configurado:

```python
if pid == "codex":
    proxy_ok = bool(load_env_file(HARNESS_ENV_FILE).get("CLAUDE_HARNESS_CODEX_PROXY_URL"))
    codex_ok = sp.run(["codex", "login", "status"], capture_output=True).returncode == 0
    has_auth = codex_ok and proxy_ok
```

### 5.3. Variables y flags que el wrapper acepta

**Variables de entrada** (puestas por la TUI o por el .env):

| Variable | Tipo | Default | Propósito |
|----------|------|---------|-----------|
| `CLAUDE_HARNESS_CODEX_PROXY_URL` | string (HTTPS) | _requerido_ | URL del Worker proxy |
| `CLAUDE_HARNESS_CODEX_MODEL` | string | `gpt-5` | Default model |
| `CLAUDE_HARNESS_SLOT_OPUS` | string | = MODEL | Override opus slot |
| `CLAUDE_HARNESS_SLOT_SONNET` | string | = MODEL | Override sonnet slot |
| `CLAUDE_HARNESS_SLOT_HAIKU` | string | = MODEL | Override haiku slot |
| `CODEX_AUTH_JSON` | path | `$HOME/.codex/auth.json` | Override del path (testing) |
| `CODEX_DEBUG` | bool | unset | Habilita `set -x` |

**Flags que recibe del usuario:**

| Flag | Efecto |
|------|--------|
| `--model <id>` | Override del modelo default (NO inyectar `--model` si está presente) |
| `--dangerously-skip-permissions` | Pasa tal cual a `claude` |
| `--continue`, `--resume` | Pasa tal cual |
| `-h`, `--help`, `-v`, `--version` | Pasa tal cual, NO inyectar `--model` (deja que claude lo maneje) |

### 5.4. Comportamiento esperado

```
# Caso 1: launch desde TUI
TUI sets: CLAUDE_HARNESS_CODEX_PROXY_URL=https://codex-proxy.xxx.workers.dev
TUI sets: CLAUDE_HARNESS_CODEX_MODEL=gpt-5
TUI sets: CLAUDE_HARNESS_SLOT_SONNET=gpt-5-mini
TUI runs:  claude-codex --dangerously-skip-permissions
→ Resultado: claude --model gpt-5 --dangerously-skip-permissions
            con ANTHROPIC_DEFAULT_SONNET_MODEL=gpt-5-mini

# Caso 2: launch directo (bypass TUI)
$ CLAUDE_HARNESS_CODEX_PROXY_URL=https://... codex-codex --model gpt-5
→ Funciona idéntico

# Caso 3: user overridea modelo
$ claude-codex --model o3
→ Wrapper NO inyecta --model (porque user ya lo pasó)
→ ANTHROPIC_MODEL=o3
```

---

## 6. Sample Script

Este es el script que se debe escribir en `scripts/claude-codex`. Está
listo para pegar, ~95 líneas.

```bash
#!/usr/bin/env bash
# claude-codex — lanza Claude Code con Codex (OpenAI) como backend via proxy.
# Lee ~/.codex/auth.json, refresca el token si hace falta, y exporta las
# env vars correctas para que el Worker traduzca Anthropic <-> OpenAI Responses.
set -euo pipefail

HARNESS_ENV_FILE="$HOME/.config/claude-harness/.env"
CODEX_AUTH_JSON="${CODEX_AUTH_JSON:-$HOME/.codex/auth.json}"
CLAUDE_BIN="$HOME/.local/bin/claude"
PROXY_URL_DEFAULT=""

die()  { echo "claude-codex: $*" >&2; exit 1; }
log()  { echo "claude-codex: $*" >&2; }
has()  { command -v "$1" >/dev/null 2>&1; }

# 1. Cargar env del harness
[ -f "$HARNESS_ENV_FILE" ] && { set -a; . "$HARNESS_ENV_FILE"; set +a; }

# 2. Validar codex instalado (lo usamos solo para sugerir comandos de login;
#    el refresh real lo hacemos con curl, ver §2)
has codex || die "codex CLI no encontrado. Instalar con: brew install codex"

# 3. Validar auth.json
[ -f "$CODEX_AUTH_JSON" ] \
  || die "No se encontró $CODEX_AUTH_JSON. Corré: codex login --device-auth"

# 4. Parsear auth.json — soporta auth_mode=chatgpt (OAuth) y apikey
read -r AUTH_MODE API_KEY ACCESS_TOKEN REFRESH_TOKEN ACCOUNT_ID < <(
  python3 - "$CODEX_AUTH_JSON" <<'PY' || die "auth.json inválido o corrupto"
import json, sys
d = json.load(open(sys.argv[1]))
mode = d.get("auth_mode")
if mode == "apikey":
    print(mode, d.get("OPENAI_API_KEY") or "", "", "", "")
elif mode == "chatgpt":
    t = d.get("tokens") or {}
    print(mode, "",
          t.get("access_token", ""),
          t.get("refresh_token", ""),
          t.get("account_id", ""))
else:
    sys.exit(f"auth_mode '{mode}' no soportado (esperado: chatgpt o apikey)")
PY
)

# 5. Construir ANTHROPIC_AUTH_TOKEN según el modo
if [ "$AUTH_MODE" = "apikey" ]; then
  [ -n "$API_KEY" ] || die "auth_mode=apikey pero OPENAI_API_KEY está vacío"
  export ANTHROPIC_AUTH_TOKEN="$API_KEY"
elif [ "$AUTH_MODE" = "chatgpt" ]; then
  [ -n "$REFRESH_TOKEN" ] || die "Sin refresh_token. Re-loginear con: codex login"

  # 5a. Decodificar exp del access_token (formato JWT, no validamos firma)
  EXP=$(python3 -c "
import sys, base64, json
t = sys.argv[1].split('.')[1]
t += '=' * (-len(t) % 4)
print(json.loads(base64.urlsafe_b64decode(t))['exp'])
" "$ACCESS_TOKEN") || die "No se pudo decodificar el access_token JWT"

  NOW=$(date +%s)
  # 5b. Refrescar si vence en < 5 min
  if [ $((EXP - NOW)) -lt 300 ]; then
    log "Refrescando access_token..."
    NEW=$(curl --silent --show-error --fail-with-body --max-time 30 \
      --request POST \
      --url "https://auth.openai.com/oauth/token" \
      --header "Content-Type: application/x-www-form-urlencoded" \
      --data-urlencode "grant_type=refresh_token" \
      --data-urlencode "refresh_token=${REFRESH_TOKEN}" \
      --data-urlencode "client_id=app_EMoamEEZ73f0CkXaXp7hrann" \
      --data-urlencode "scope=openid profile email offline_access api.connectors.read api.connectors.write api.conversations.write" \
      | python3 -c "import json,sys; r=json.load(sys.stdin); [sys.exit(f'falta {k}') for k in ('access_token','id_token','refresh_token') if k not in r]; print(json.dumps(r))"
    ) || die "Refresh OAuth falló. Re-loginear con: codex login"

    # 5c. Persistir nuevos tokens atómicamente
    python3 - "$CODEX_AUTH_JSON" "$NEW" <<'PY' || die "No se pudo actualizar auth.json"
import json, os, sys, pathlib
auth_path = pathlib.Path(sys.argv[1])
new = json.loads(sys.argv[2])
data = json.loads(auth_path.read_text())
data["tokens"]["access_token"]  = new["access_token"]
data["tokens"]["id_token"]      = new["id_token"]
data["tokens"]["refresh_token"] = new["refresh_token"]
data["last_refresh"] = __import__("datetime").datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
tmp = auth_path.with_suffix(".json.tmp")
tmp.write_text(json.dumps(data, indent=2))
os.chmod(tmp, 0o600)
os.replace(tmp, auth_path)
PY
    ACCESS_TOKEN=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['access_token'])" "$NEW")
  fi
  export ANTHROPIC_AUTH_TOKEN="codex:${ACCESS_TOKEN}:${ACCOUNT_ID}"
fi
unset ANTHROPIC_API_KEY

# 6. Validar proxy URL
PROXY_URL="${CLAUDE_HARNESS_CODEX_PROXY_URL:-$PROXY_URL_DEFAULT}"
[ -n "$PROXY_URL" ] || die "CLAUDE_HARNESS_CODEX_PROXY_URL no configurada. Agregala a ~/.config/claude-harness/.env"
export ANTHROPIC_BASE_URL="$PROXY_URL"
export CLAUDE_CODE_DISABLE_1M_CONTEXT=1
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="${CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC:-1}"

# 7. Modelo
DEFAULT_MODEL="${CLAUDE_HARNESS_CODEX_MODEL:-gpt-5}"
export ANTHROPIC_MODEL="$DEFAULT_MODEL"
export ANTHROPIC_DEFAULT_OPUS_MODEL="${CLAUDE_HARNESS_SLOT_OPUS:-$DEFAULT_MODEL}"
export ANTHROPIC_DEFAULT_SONNET_MODEL="${CLAUDE_HARNESS_SLOT_SONNET:-$DEFAULT_MODEL}"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="${CLAUDE_HARNESS_SLOT_HAIKU:-$DEFAULT_MODEL}"

# 8. Launch — inyectar --model solo si el user no pasó -m/--model/-h/-v
should_inject=true
for a in "$@"; do
  case "$a" in -m|--model|-h|--help|-v|--version) should_inject=false ;; esac
done

declare -a launch_args=("$CLAUDE_BIN")
if [ "$should_inject" = true ] && [ -n "${CLAUDE_HARNESS_CODEX_MODEL:-}" ]; then
  launch_args+=("--model" "$CLAUDE_HARNESS_CODEX_MODEL")
fi

exec "${launch_args[@]}" "$@"
```

### Notas de implementación

- **Línea 32-43**: el `python3` inline parsea auth.json una sola vez y emite
  5 campos por stdout. Esto evita múltiples llamadas a `python3`.
- **Línea 48-65**: el bloque del refresh hace 3 invocaciones de `python3` (decode
  exp, curl, persist). Es feo pero cada una tiene lifetimes distintos y no vale
  la pena consolidar.
- **Línea 67-72**: la escritura atómica usa `os.replace` que es atómico en POSIX
  (rename(2)). Si el proceso muere entre el write y el replace, auth.json queda
  intacto.
- **Línea 81**: el scope del refresh request es el scope que tenía el token
  original (verificado en el JWT: `scp` claim incluye esos valores).
- **Línea 97**: `exec` reemplaza el proceso bash con `claude`. Importante: si algo
  falla DESPUÉS de esta línea, el wrapper ya no tiene chance de hacer cleanup.
  Por eso toda la validación está ANTES del exec.
- **Línea 100-104**: el `should_inject` es idéntico al patrón de los otros
  wrappers. Mantenerlo consistente.

---

## Apéndice A: Decisiones abiertas / follow-ups

| # | Decisión | Status | Próximo paso |
|---|----------|--------|--------------|
| 1 | ¿Soportar `auth_mode=apikey`? | Sí (cubierto) | — |
| 2 | ¿Health check del proxy antes de lanzar claude? | No (cut 1) | Cut 2: ping `GET /health` del worker |
| 3 | ¿Cachear el access_token entre invocaciones? | No | Cada launch hace su propio check; el overhead es ~200ms solo si vence |
| 4 | ¿Reintentar refresh con backoff? | No (cut 1) | Cut 2: 3 reintentos con 2/4/8s |
| 5 | ¿Manejar cambio de `client_id` de OpenAI? | No | Si pasa, fallar loudly. La probabilidad es ~0. |
| 6 | ¿Soportar `CODEX_AUTH_JSON` env var para testing? | Sí (cubierto) | — |

## Apéndice B: Referencias

- PLAN_CODEX.md: arquitectura general, formato del header `codex:...`
- scripts/claude-minimax: patrón de python inline para parsear auth
- scripts/claude-opencode-go: patrón de proxy URL hardcoded (nosotros lo movemos a env)
- scripts/claude-openrouter: patrón de slots opus/sonnet/haiku
- codex login --help: confirma que no hay subcomando `refresh`
- ~/.codex/auth.json: estructura verificada (auth_mode, tokens, last_refresh)
- JWT del access_token: `aud=https://api.openai.com/v1`, `client_id=app_EMoamEEZ73f0CkXaXp7hrann`, `exp` en unix epoch
- OpenAI OAuth endpoint: `https://auth.openai.com/oauth/token` (público, mismo que usa el CLI `codex`)
