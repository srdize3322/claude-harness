# Plan: Integrar Codex como provider de primera clase en claude-harness-ui.py

> **Estado actual**: Codex esta registrado en `PROVIDERS` (linea 145) como
> provider `family="codex"`, con `supports_claude_launch=False` y un launcher
> `claude-codex` que **solo imprime error y sale con codigo 1** (ver
> `scripts/claude-codex:1-10`). Login funciona via `codex login`, pero la TUI
> no puede lanzar Codex como si fuera Claude Code.
>
> **Objetivo de este plan**: dejar a Codex al mismo nivel que MiniMax,
> OpenRouter, OpenCode Go y Claude nativo. La TUI debe poder elegir modelo,
> setear thinking, slots y permisos, y `claude-codex` debe ser un wrapper real
> que exporte `ANTHROPIC_BASE_URL` apuntando al Worker de Codex y arme el
> `ANTHROPIC_AUTH_TOKEN="codex:<token>:<account_id>"` que el Worker espera.

---

## Section 1: Estado actual de Codex en el codigo

### 1.1 Definicion del provider

**Archivo**: `scripts/claude-harness-ui.py:145-148`

```python
ProviderDefinition("codex", "Codex", CLAUDE_CODEX_LAUNCHER, "codex",
                   supports_claude_launch=False, launch_block_reason="solo login/configuracion",
                   default_model_env="CLAUDE_HARNESS_CODEX_MODEL"),
```

Observaciones:
- `family="codex"` esta bien: enruta a `PERMISSION_OPTIONS["codex"]` (linea 158) que tiene `-a on-request`, `-a never`, `--dangerously-bypass-approvals-and-sandbox`.
- `CLAUDE_CODEX_LAUNCHER` se resuelve en `scripts/claude-codex` (linea 73) -> `str(ROOT / "claude-codex")`.
- `default_model_env="CLAUDE_HARNESS_CODEX_MODEL"` **ya esta seteado**, asi que `get_default_model()` (linea 283) y `set_default_model()` (linea 289) ya funcionan para Codex.

### 1.2 Login status check

**Archivo**: `scripts/claude-harness-ui.py:274-279`

```python
if provider.provider_id == "codex":
    import subprocess as sp
    result = sp.run(["codex", "login", "status"], capture_output=True, text=True)
    output = " ".join(p.strip() for p in [result.stdout, result.stderr] if p.strip()).lower()
    logged_in = "logged in" in output
    return ProviderStatus(logged_in, "login ok" if logged_in else "falta login")
```

Problemas:
1. No valida que el binario `codex` este instalado (`codex login status` falla con FileNotFoundError si no esta, pero eso no se traduce a un mensaje claro).
2. **No verifica que `CLAUDE_HARNESS_CODEX_PROXY_URL` este seteado**. Sin esa URL, el wrapper no puede hacer nada.
3. No hay distincion entre "no esta logueado" y "no esta configurado".

### 1.3 Login actions y API key prompt

**Archivo**: `scripts/claude-harness-ui.py:1004-1006` (`login_actions_for`)

```python
if pid == "codex":
    return [("Device auth (link device)", lambda: ["codex", "login", "--device-auth"]),
            ("API key manual", None)]
```

**Archivo**: `scripts/claude-harness-ui.py:1046-1049` (`prompt_api_key`)

```python
elif pid == "codex":
    key = getpass.getpass("  OPENAI_API_KEY: ").strip()
    if key:
        subprocess.run(["codex", "login", "--with-api-key"], input=key + "\n", text=True)
```

Observacion: las acciones de login ya funcionan, pero faltan acciones para configurar el proxy URL.

### 1.4 Launcher actual (deshabilitado)

**Archivo**: `scripts/claude-codex` (10 lineas, completo)

```bash
#!/usr/bin/env bash
set -euo pipefail

cat >&2 <<'EOF'
claude-codex fue deshabilitado.
...
EOF
exit 1
```

**Esto es lo que hay que reescribir**. El plan no cubre la reescritura completa del wrapper bash, pero el doc asume que existira un wrapper funcional que:
1. Lee `~/.codex/auth.json` para extraer `access_token` y `chatgpt_account_id`.
2. Lee `CLAUDE_HARNESS_CODEX_PROXY_URL` (env var) y lo exporta como `ANTHROPIC_BASE_URL`.
3. Exporta `ANTHROPIC_AUTH_TOKEN="codex:<access_token>:<account_id>"`.
4. Aplica el default model + slots + thinking igual que `claude-minimax`.
5. Refresca el token si esta por expirar (a menos que `CLAUDE_HARNESS_CODEX_NO_AUTO_REFRESH=1`).

### 1.5 Lanzamiento desde la TUI / CLI

**Archivo**: `scripts/claude-harness-ui.py:1586-1592` (dentro de `run_tui`)

```python
args = [provider.launcher]
cc_model = model_id_for_claude_code(model.model_id, provider.provider_id)
if cc_model != "default":
    args.extend(["--model", cc_model])
args.extend(permission.args)
args.extend(extra_args)
os.execv(args[0], args)
return
```

**Archivo**: `scripts/claude-harness-ui.py:1608-1614` (dentro de `launch`)

```python
args = [provider.launcher]
cc_model = model_id_for_claude_code(model.model_id, provider.provider_id)
if cc_model != "default":
    args.extend(["--model", cc_model])
args.extend(permission.args)
args.extend(extra_args)
os.execv(args[0], args)
```

Observaciones:
- `provider.launcher` ya apunta a `claude-codex`. Solo necesitamos que el wrapper deje de hacer `exit 1`.
- `model_id_for_claude_code` (linea 502) agrega `[1m]` a modelos no-Anthropic. Para Codex deberia devolver el id limpio (gpt-5, gpt-5-codex, etc.) porque la Workers hace el routing. Hay que decidir si esto se queda asi o se hace un bypass para `provider_id == "codex"`.
- Los slots ya se exportan en `run_tui:1580-1585` y `launch:1602-1607` -> el wrapper los va a recibir como env vars.

### 1.6 CLI flags

**Archivo**: `scripts/claude-harness-ui.py:1632-1669` (parsing de `main`)

Flags que ya acepta el CLI:
- `--provider <id>` -> `cli_provider`
- `--model <id>` -> `cli_model`
- `--thinking <level>` -> `cli_thinking`
- `--slot-opus`, `--slot-sonnet`, `--slot-haiku`
- `--dangerously-skip-permissions` -> `cli_dangerously_skip`
- `--skip` -> bypass TUI y va directo a `claude-native`
- `--refresh-catalog`

**Combinacion de auto-launch** (linea 1671-1680):

```python
if cli_provider and cli_model and cli_dangerously_skip:
    provider = find_provider(cli_provider)
    if provider and provider.supports_claude_launch:
        ...
```

**Problema critico**: el guard `provider.supports_claude_launch` (linea 1673) bloquea a Codex aunque ya no tenga `supports_claude_launch=False`. Hay que flipear el flag en la `ProviderDefinition` (cambio A abajo).

### 1.7 Que falta resumido

| Pieza | Estado |
|-------|--------|
| Provider en `PROVIDERS` | OK |
| `default_model_env` | OK |
| `fetch_codex_models` (lee `~/.codex/models_cache.json`) | OK |
| `login_actions_for("codex")` | OK |
| `prompt_api_key("codex")` | OK |
| `provider_status("codex")` | parcial: no chequea binario ni proxy URL |
| TUI prompt para proxy URL | **falta** |
| `supports_claude_launch` | **mal** (esta en `False`) |
| `claude-codex` wrapper | **falta** (es un stub que exit 1) |
| CLI auto-launch con `--provider codex` | **bloqueado** por `supports_claude_launch` |
| Slots / thinking / permission flow en TUI | OK (usa `family="codex"` y `PERMISSION_OPTIONS["codex"]`) |

---

## Section 2: Cambios requeridos

### A) Flipear `supports_claude_launch` para Codex

**Donde**: `scripts/claude-harness-ui.py:145-148`

**Antes**:

```python
ProviderDefinition("codex", "Codex", CLAUDE_CODEX_LAUNCHER, "codex",
                   supports_claude_launch=False, launch_block_reason="solo login/configuracion",
                   default_model_env="CLAUDE_HARNESS_CODEX_MODEL"),
```

**Despues**:

```python
ProviderDefinition("codex", "Codex", CLAUDE_CODEX_LAUNCHER, "codex",
                   default_model_env="CLAUDE_HARNESS_CODEX_MODEL"),
```

**Por que**: el `launch_block_reason` se usaba como placeholder mientras el wrapper era un stub. Una vez que `claude-codex` sea un wrapper real, el flujo TUI normal (pick model -> thinking -> slots -> permission -> launch) debe correr. El check en `run_tui:1560-1562` que hacia `show_message` con "solo login/configuracion" desaparece automaticamente.

Tambien elimina el bloqueo en `main:1673` que impide `--provider codex` con `--dangerously-skip-permissions`.

### B) Endurecer `provider_status` para Codex

**Donde**: `scripts/claude-harness-ui.py:274-279`

**Antes**:

```python
if provider.provider_id == "codex":
    import subprocess as sp
    result = sp.run(["codex", "login", "status"], capture_output=True, text=True)
    output = " ".join(p.strip() for p in [result.stdout, result.stderr] if p.strip()).lower()
    logged_in = "logged in" in output
    return ProviderStatus(logged_in, "login ok" if logged_in else "falta login")
```

**Despues**:

```python
if provider.provider_id == "codex":
    import subprocess as sp
    # 1. Binario codex instalado
    which = sp.run(["which", "codex"], capture_output=True, text=True)
    if which.returncode != 0:
        return ProviderStatus(False, "falta codex CLI (brew install codex)")
    # 2. Login de Codex
    result = sp.run(["codex", "login", "status"], capture_output=True, text=True)
    output = " ".join(p.strip() for p in [result.stdout, result.stderr] if p.strip()).lower()
    logged_in = "logged in" in output
    if not logged_in:
        return ProviderStatus(False, "falta login (Ctrl+L)")
    # 3. Proxy URL configurado
    env = load_env_file(HARNESS_ENV_FILE)
    proxy = env.get("CLAUDE_HARNESS_CODEX_PROXY_URL", "").strip()
    if not proxy:
        return ProviderStatus(False, "falta CLAUDE_HARNESS_CODEX_PROXY_URL")
    return ProviderStatus(True, "login ok")
```

**Por que**: con esta logica, si falta cualquiera de las tres cosas (binario, login, proxy URL) el provider aparece como "no logueado" en la lista y `pick_provider:1079-1083` lanza automaticamente el menu de login. Eso le da al usuario una pista concreta de que falta.

### C) Agregar accion "Configurar proxy URL" al menu de login

**Donde**: `scripts/claude-harness-ui.py:1004-1006` (`login_actions_for`)

**Antes**:

```python
if pid == "codex":
    return [("Device auth (link device)", lambda: ["codex", "login", "--device-auth"]),
            ("API key manual", None)]
```

**Despues**:

```python
if pid == "codex":
    return [("Device auth (link device)", lambda: ["codex", "login", "--device-auth"]),
            ("API key manual", None),
            ("Configurar proxy URL", "prompt_codex_proxy"),
            ("Limpiar auto-refresh de token", "clear_codex_no_refresh")]
```

Y agregar las funciones en cualquier lugar razonable (cerca de `prompt_api_key`):

```python
def prompt_codex_proxy_url(stdscr) -> None:
    curses.endwin()
    print()
    try:
        url = input("  CLAUDE_HARNESS_CODEX_PROXY_URL (https://...workers.dev): ").strip()
    except EOFError:
        url = ""
    if url:
        save_env_values(HARNESS_ENV_FILE, {"CLAUDE_HARNESS_CODEX_PROXY_URL": url})
        print("  Guardada.")
    print()
    try:
        input("  Enter para volver al harness...")
    except EOFError:
        pass
    stdscr.refresh()


def toggle_codex_no_refresh(stdscr) -> None:
    curses.endwin()
    env = load_env_file(HARNESS_ENV_FILE)
    current = env.get("CLAUDE_HARNESS_CODEX_NO_AUTO_REFRESH", "").strip()
    print()
    print(f"  Auto-refresh de token: {'OFF' if current == '1' else 'ON'}")
    print("  1) Dejar como esta")
    print("  2) Desactivar auto-refresh (CLAUDE_HARNESS_CODEX_NO_AUTO_REFRESH=1)")
    print("  3) Reactivar auto-refresh")
    print()
    try:
        choice = input("  Opcion: ").strip()
    except EOFError:
        choice = "1"
    if choice == "2":
        save_env_values(HARNESS_ENV_FILE, {"CLAUDE_HARNESS_CODEX_NO_AUTO_REFRESH": "1"})
        print("  Auto-refresh desactivado.")
    elif choice == "3":
        env2 = load_env_file(HARNESS_ENV_FILE)
        env2.pop("CLAUDE_HARNESS_CODEX_NO_AUTO_REFRESH", None)
        # Re-emit el archivo sin esa key
        HARNESS_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        HARNESS_ENV_FILE.write_text(
            "\n".join(f'{k}="{v}"' for k, v in sorted(env2.items())) + "\n",
            encoding="utf-8",
        )
        print("  Auto-refresh reactivado.")
    print()
    try:
        input("  Enter para volver al harness...")
    except EOFError:
        pass
    stdscr.refresh()
```

Y actualizar `do_login_action` (linea 1010) para resolver los strings:

```python
def do_login_action(stdscr, provider, idx: int) -> None:
    items = login_actions_for(provider.provider_id)
    if idx >= len(items):
        return
    label, action = items[idx]
    if action is None:
        prompt_api_key(stdscr, provider.provider_id)
        return
    if action == "prompt_codex_proxy":
        prompt_codex_proxy_url(stdscr)
        return
    if action == "clear_codex_no_refresh":
        toggle_codex_no_refresh(stdscr)
        return
    run_external_in_curses(stdscr, action())
```

**Por que**:
- Si `provider_status` devuelve "falta proxy URL", el flujo normal `pick_provider:1079-1083` manda al menu de login.
- Ahi el usuario ve "Configurar proxy URL" como tercera opcion y lo setea en una sola pantalla.
- Mantiene la logica de `do_login_action` (que ya distingue entre `None` para `prompt_api_key` y callable para `run_external_in_curses`) agregando un caso para strings.

### D) Pasar `CLAUDE_HARNESS_CODEX_PROXY_URL` como env var al exec

**Donde**: `scripts/claude-harness-ui.py` en `run_tui` (alrededor de linea 1580) y en `launch` (alrededor de linea 1602).

**Antes** (en `run_tui`, despues del bloque de slots):

```python
if slots.opus:
    os.environ["CLAUDE_HARNESS_SLOT_OPUS"] = slots.opus
if slots.sonnet:
    os.environ["CLAUDE_HARNESS_SLOT_SONNET"] = slots.sonnet
if slots.haiku:
    os.environ["CLAUDE_HARNESS_SLOT_HAIKU"] = slots.haiku
args = [provider.launcher]
```

**Despues**:

```python
if slots.opus:
    os.environ["CLAUDE_HARNESS_SLOT_OPUS"] = slots.opus
if slots.sonnet:
    os.environ["CLAUDE_HARNESS_SLOT_SONNET"] = slots.sonnet
if slots.haiku:
    os.environ["CLAUDE_HARNESS_SLOT_HAIKU"] = slots.haiku
if provider.provider_id == "codex":
    env = load_env_file(HARNESS_ENV_FILE)
    proxy = env.get("CLAUDE_HARNESS_CODEX_PROXY_URL", "").strip()
    if proxy:
        os.environ["CLAUDE_HARNESS_CODEX_PROXY_URL"] = proxy
    no_refresh = env.get("CLAUDE_HARNESS_CODEX_NO_AUTO_REFRESH", "").strip()
    if no_refresh:
        os.environ["CLAUDE_HARNESS_CODEX_NO_AUTO_REFRESH"] = no_refresh
args = [provider.launcher]
```

Aplicar el mismo bloque en `launch` (linea 1602-1607).

Alternativa: factorizar en una funcion `apply_provider_env(provider, model)` y llamarla desde ambos paths. Recomendado para no duplicar logica.

**Por que**:
- `os.execv` reemplaza el proceso actual, por lo que cualquier env var que no este en `os.environ` se pierde.
- El wrapper `claude-codex` necesita `CLAUDE_HARNESS_CODEX_PROXY_URL` para armar `ANTHROPIC_BASE_URL`.
- Tambien `CLAUDE_HARNESS_CODEX_NO_AUTO_REFRESH` por si el usuario desactivo el refresh automatico.

### E) CLI: verificar que `--provider codex` ya funciona

Una vez hecho el cambio A (`supports_claude_launch` sin `False`), el guard en `main:1673`:

```python
if provider and provider.supports_claude_launch:
    ...
```

deja pasar a Codex. No hay que tocar `main` salvo verificar.

**Verificacion manual**:

```bash
claude-harness-ui --provider codex --model gpt-5 --dangerously-skip-permissions
```

debe ejecutar `claude-codex` con `gpt-5` como model.

### F) Slots: ya pasan como env vars

**Donde**: `scripts/claude-harness-ui.py:1580-1585` y `1602-1607`

```python
if slots.opus:
    os.environ["CLAUDE_HARNESS_SLOT_OPUS"] = slots.opus
if slots.sonnet:
    os.environ["CLAUDE_HARNESS_SLOT_SONNET"] = slots.sonnet
if slots.haiku:
    os.environ["CLAUDE_HARNESS_SLOT_HAIKU"] = slots.haiku
```

Esto ya funciona para todos los providers, no requiere cambios. El wrapper `claude-codex` los lee y los mapea a `ANTHROPIC_DEFAULT_OPUS_MODEL` / `ANTHROPIC_DEFAULT_SONNET_MODEL` / `ANTHROPIC_DEFAULT_HAIKU_MODEL` igual que `claude-minimax` (linea 62-64 de `claude-minimax`).

### G) (opcional) Bypass de `[1m]` suffix para Codex

**Donde**: `scripts/claude-harness-ui.py:502-518` (`model_id_for_claude_code`)

**Por que**: el wrapper de Codex manda el model id tal cual al Worker (que hace el routing). Si el id viene como `gpt-5[1m]`, el Worker de Codex no entiende ese sufijo. Hay que decidir si:

- (recomendado) No aplicar el sufijo para `provider_id == "codex"`.
- O aplicarlo y hacer que el wrapper lo strippee antes de mandar al Worker.

**Cambio recomendado en `model_id_for_claude_code`**:

```python
def model_id_for_claude_code(model_id: str, provider_id: str | None) -> str:
    if not model_id or model_id == "default":
        return model_id
    if provider_id == "codex":
        return model_id  # Codex no usa el truco de [1m]
    if is_anthropic_model(model_id, provider_id):
        return model_id
    ml = model_id.lower()
    if "[1m]" in ml or "[2m]" in ml:
        return model_id
    return f"{model_id}[1m]"
```

---

## Section 3: Nuevas env vars

### 3.1 `CLAUDE_HARNESS_CODEX_MODEL`

| Atributo | Valor |
|----------|-------|
| Default | ninguno (Codex usa `"default"` que el Worker resuelve) |
| Donde se setea | TUI (Ctrl+D en el picker de modelos, via `set_default_model` linea 289) o directamente en `~/.config/claude-harness/.env` |
| Donde se lee | `get_default_model` (linea 283) y `set_default_model` (linea 289) en la TUI. El wrapper bash lo lee como `CLAUDE_HARNESS_CODEX_MODEL` y lo exporta como `ANTHROPIC_MODEL` (igual que `claude-minimax:39`). |
| Documentado en README | Si (linea 95 de `docs/README.md`) |

### 3.2 `CLAUDE_HARNESS_CODEX_PROXY_URL` (REQUERIDO)

| Atributo | Valor |
|----------|-------|
| Default | **sin default, requerido** |
| Donde se setea | TUI (`prompt_codex_proxy_url` en el menu de login) o manualmente en `~/.config/claude-harness/.env` |
| Donde se lee | (a) `provider_status` (linea 274) para validar que exista, (b) `run_tui` / `launch` para pasarlo al wrapper, (c) el wrapper bash lo exporta como `ANTHROPIC_BASE_URL` |
| Ejemplo | `https://codex-proxy.<mi-subdominio>.workers.dev` |
| Documentado en README | **Falta agregar** (agregar despues de linea 95) |

Linea sugerida para `docs/README.md` (entre linea 95 y la linea 97 del opt-out):

```bash
# Codex: URL del Cloudflare Worker que traduce Anthropic -> OpenAI Responses
CLAUDE_HARNESS_CODEX_PROXY_URL=https://codex-proxy.tu-subdominio.workers.dev
```

### 3.3 `CLAUDE_HARNESS_CODEX_NO_AUTO_REFRESH` (opt-out)

| Atributo | Valor |
|----------|-------|
| Default | sin setear = auto-refresh ON |
| Valores validos | `"1"` para desactivar, cualquier otra cosa = ON |
| Donde se setea | TUI (`toggle_codex_no_refresh` en el menu de login) o manualmente en `~/.config/claude-harness/.env` |
| Donde se lee | el wrapper bash. Si esta en `"1"`, no refresca el token antes de la llamada (util para debug). |
| Documentado en README | **Falta agregar** |

### 3.4 Env vars que el wrapper bash va a leer (referencia, no se setean desde la TUI)

| Variable | Fuente |
|----------|--------|
| `OPENAI_API_KEY` o `~/.codex/auth.json` | la maneja `codex login` (subsistema nativo de Codex) |
| `CLAUDE_HARNESS_CODEX_MODEL` | TUI / `.env` (3.1) |
| `CLAUDE_HARNESS_CODEX_PROXY_URL` | TUI / `.env` (3.2) |
| `CLAUDE_HARNESS_CODEX_NO_AUTO_REFRESH` | TUI / `.env` (3.3) |
| `CLAUDE_HARNESS_SLOT_OPUS` / `_SONNET` / `_HAIKU` | TUI (linea 1580-1585) |
| `CLAUDE_CODE_DISABLE_THINKING` | TUI (linea 1576-1577) si `thinking_level == "off"` |
| `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | TUI (linea 1579, via `apply_context_window_env`) |

---

## Section 4: Cambios en el flujo de la TUI

### Estado actual del flujo

```
pick_provider (1058)
  -> si logged_in=False, llama run_login_menu (1080, 1085)
  -> si logged_in=True, devuelve el provider
run_tui (1548)
  -> llama pick_provider, luego pick_model, pick_thinking, pick_agent_slots,
     pick_permission, confirm_launch
  -> al final: execv(provider.launcher, args)
```

### Como se integra Codex

Con el cambio B en `provider_status`, Codex aparece con `logged_in=False` si falta cualquiera de los tres requisitos. Eso significa que el usuario **siempre** pasa por `run_login_menu` la primera vez, y ahi ve las acciones (cambio C):

1. "Device auth (link device)" -> `codex login --device-auth`
2. "API key manual" -> `codex login --with-api-key`
3. "Configurar proxy URL" -> `prompt_codex_proxy_url` (NUEVO)
4. "Limpiar auto-refresh de token" -> `toggle_codex_no_refresh` (NUEVO)

El menu se navega con flechas + Enter, igual que ahora (no hay cambio en el UX del menu, solo se agregan dos items para Codex).

### Recomendacion de UX

**Opcion recomendada: A) Agregar al menu de login (Ctrl+L) para Codex** (ya descrito en el cambio C).

Por que:
- Es consistente con el resto del flujo: si no estas logueado/configurado, el menu de login es el lugar obvio para resolverlo.
- Reusa el patron `if action == "..."` que ya existe en `do_login_action` (linea 1010-1018).
- No requiere nueva tecla ni submenu, que seria overhead para una accion que se hace 1 vez.
- Las otras opciones (B = nuevo item "Configurar", C = inline) harian que Codex tenga un flujo especial, rompiendo la consistencia.

### Verificacion de que el flujo funciona end-to-end

1. Estado limpio: `~/.codex/` no existe, `~/.config/claude-harness/.env` sin `CLAUDE_HARNESS_CODEX_PROXY_URL`.
2. `claude-harness-ui` -> lista de providers -> Codex aparece como `falta codex CLI` o `falta login` o `falta proxy URL`.
3. Enter sobre Codex -> `run_login_menu`.
4. Opcion 1 (device auth) -> sigue el flow de `codex login --device-auth` en una subshell.
5. Despues de device auth, `provider_status` re-corre y ahora dice `falta CLAUDE_HARNESS_CODEX_PROXY_URL`.
6. Opcion 3 -> pide URL, la guarda en `.env`.
7. `provider_status` re-corre y ahora dice `login ok`.
8. Enter sobre Codex -> entra al picker de modelos -> gpt-5, gpt-5-codex, etc.
9. Flujo normal: thinking -> slots -> permission -> launch.
10. `execv` corre `claude-codex`, que lee el `.env` + `~/.codex/auth.json` y exporta `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`.

---

## Section 5: Cambios en flags del CLI

### Flags actuales que aplican a Codex sin modificacion

| Flag | Comportamiento para Codex |
|------|---------------------------|
| `--provider codex` | ya enruta al provider (linea 1646) |
| `--model gpt-5` | ya se pasa al wrapper (linea 1611) |
| `--dangerously-skip-permissions` | ya esta en el auto-launch (linea 1671) y mapea a `--dangerously-bypass-approvals-and-sandbox` via `PERMISSION_OPTIONS["codex"]` (linea 162) |
| `--thinking <level>` | ya se pasa al wrapper via env vars (linea 1576) |
| `--slot-opus/--slot-sonnet/--slot-haiku` | ya se exportan como env vars (linea 1580-1585) |

### Flags nuevos recomendados

| Flag | Por que | Sugerido |
|------|---------|----------|
| `--codex-proxy-url <url>` | Permite setear el proxy URL desde CLI sin editar `.env` a mano. Caso de uso: deploy nuevo del Worker, o testing. | **Si, agregar** |
| `--codex-no-refresh` | Para tests/debug, evita que el wrapper refresque el token. | **No, overkill** (se puede setear via env var directo) |

### Cambio sugerido en `main:1632-1669`

**Agregar antes de la linea 1666 (antes del `else` que captura extra_args)**:

```python
elif arg == "--codex-proxy-url" and i + 1 < len(sys.argv):
    save_env_values(HARNESS_ENV_FILE, {"CLAUDE_HARNESS_CODEX_PROXY_URL": sys.argv[i + 1]})
    print(f"  CLAUDE_HARNESS_CODEX_PROXY_URL guardada: {sys.argv[i + 1]}")
    i += 2
```

Y opcionalmente agregar un flag de salida temprana `--list-providers` para diagnostico, pero eso es scope creep.

### Verificacion del flag nuevo

```bash
claude-harness-ui --codex-proxy-url https://codex-proxy.mi-cuenta.workers.dev \
                  --provider codex --model gpt-5 --dangerously-skip-permissions
```

debe (a) persistir la URL en `.env`, (b) auto-lanzar Codex con `gpt-5`.

---

## Section 6: Plan de tests

### Tests manuales (TUI)

| # | Caso | Pasos | Resultado esperado |
|---|------|-------|---------------------|
| 1 | Codex aparece en la lista | `claude-harness-ui` (estado limpio) | 5 providers visibles, Codex con detalle de lo que falta (CLI, login, o proxy URL) |
| 2 | Status: falta binario | Sin `codex` en PATH | `falta codex CLI (brew install codex)` en rojo |
| 3 | Status: falta login | `codex` instalado pero sin `~/.codex/auth.json` | `falta login (Ctrl+L)` en rojo |
| 4 | Status: falta proxy URL | Logueado pero sin env var | `falta CLAUDE_HARNESS_CODEX_PROXY_URL` en rojo |
| 5 | Status: todo OK | Los 3 requisitos | `login ok` en verde |
| 6 | Prompt de proxy URL | Ctrl+L en Codex -> opcion 3 | Aparece prompt, acepta URL, la guarda en `.env` |
| 7 | Toggles de auto-refresh | Ctrl+L en Codex -> opcion 4 | Muestra estado actual, permite activar/desactivar |
| 8 | Picker de modelos | Enter en Codex logueado | Lista de modelos desde `~/.codex/models_cache.json` |
| 9 | Slot picker | Slot sonnet/haiku | Mismo picker que para MiniMax |
| 10 | Permission picker | Para Codex | Muestra "Default", "On request", "Never ask", "Dangerously bypass" |
| 11 | Confirmacion | Enter en confirm | Muestra resumen con modelo, thinking, slots, permisos |
| 12 | Lanzamiento | Enter final | `execv("claude-codex", args)`, terminal vuelve al shell con Claude Code activo hablando con Codex |

### Tests manuales (CLI)

| # | Comando | Resultado esperado |
|---|---------|---------------------|
| 1 | `claude-harness-ui --provider codex --model gpt-5 --dangerously-skip-permissions` | Auto-launch, sin pasar por TUI |
| 2 | `claude-harness-ui --provider codex --model gpt-5-codex --dangerously-skip-permissions` | Idem, con modelo distinto |
| 3 | `claude-harness-ui --provider codex --model gpt-5 --dangerously-skip-permissions --thinking high` | Thinking seteado como env var |
| 4 | `claude-harness-ui --provider codex --model gpt-5 --dangerously-skip-permissions --slot-sonnet gpt-5-codex` | Slot sonnet seteado |
| 5 | `claude-harness-ui --codex-proxy-url https://x.y.workers.dev --provider codex --model gpt-5 --dangerously-skip-permissions` | URL persistida en `.env` + auto-launch |
| 6 | `claude-harness-ui --provider minimax --model MiniMax-M3 --dangerously-skip-permissions` (regresion) | MiniMax sigue funcionando |
| 7 | `claude-harness-ui --provider openrouter --model anthropic/claude-sonnet-4-5 --dangerously-skip-permissions` (regresion) | OpenRouter sigue funcionando |

### Tests de env vars

| # | Verificacion | Como |
|---|--------------|------|
| 1 | `CLAUDE_HARNESS_CODEX_MODEL` se persiste | Editar `~/.config/claude-harness/.env`, confirmar que aparece |
| 2 | `CLAUDE_HARNESS_CODEX_PROXY_URL` se persiste desde TUI | TUI -> login -> opcion 3, grep `.env` |
| 3 | `CLAUDE_HARNESS_CODEX_NO_AUTO_REFRESH=1` se persiste | TUI -> login -> opcion 4, opcion 2, grep `.env` |
| 4 | Slots se exportan al wrapper | `os.execv` no permite inspeccionar; alternativa: agregar `printenv` en el wrapper bash antes del exec final para confirmar |
| 5 | Proxy URL llega al wrapper | Misma tecnica: `printenv | grep CLAUDE_HARNESS_CODEX` |

### Tests de regresion

- `claude-harness-ui` (sin flags) sigue arrancando la TUI con los 5 providers.
- El ciclo de login de los otros providers (MiniMax, OpenRouter, etc.) no se ve afectado por los cambios en `do_login_action` (verificar que el path de strings no se dispara accidentalmente para otros pids).
- El auto-launch CLI para los otros providers sigue funcionando (no se toco el guard `supports_claude_launch` para ellos).

### Comandos de verificacion

```bash
# Compilacion (no hay type-check, asi que minimo: syntax check)
python3 -m py_compile REPO/scripts/claude-harness-ui.py

# Verificar que .env se actualiza correctamente
cat ~/.config/claude-harness/.env

# Verificar que claude-codex ya no es un stub
head -3 REPO/scripts/claude-codex
# debe mostrar carga de env, no "claude-codex fue deshabilitado"
```

---

## Resumen de archivos a tocar

| Archivo | Cambios | Lineas |
|---------|---------|--------|
| `scripts/claude-harness-ui.py` | Quitar `supports_claude_launch=False` y `launch_block_reason` del provider Codex | 145-148 |
| `scripts/claude-harness-ui.py` | Endurecer `provider_status` para Codex (binario + login + proxy URL) | 274-279 |
| `scripts/claude-harness-ui.py` | Agregar acciones "Configurar proxy URL" y "Limpiar auto-refresh" | 1004-1006, 1010-1018 |
| `scripts/claude-harness-ui.py` | Agregar `prompt_codex_proxy_url` y `toggle_codex_no_refresh` (funciones nuevas) | despues de linea 1055 |
| `scripts/claude-harness-ui.py` | Pasar `CLAUDE_HARNESS_CODEX_PROXY_URL` y `CLAUDE_HARNESS_CODEX_NO_AUTO_REFRESH` en `run_tui` y `launch` | 1580-1585, 1602-1607 |
| `scripts/claude-harness-ui.py` | Agregar flag `--codex-proxy-url` en `main` | entre 1666 y 1667 |
| `scripts/claude-harness-ui.py` | (opcional) Bypass de `[1m]` suffix en `model_id_for_claude_code` para Codex | 502-518 |
| `scripts/claude-codex` | **Reescritura completa del wrapper** (fuera del scope de este plan, pero prerequisito) | completo |
| `docs/README.md` | Documentar `CLAUDE_HARNESS_CODEX_PROXY_URL` y `CLAUDE_HARNESS_CODEX_NO_AUTO_REFRESH` | despues de linea 95 |

Total estimado: ~50-70 lineas de cambios en la TUI Python, mas la reescritura del wrapper bash (que es trabajo separado, ya planeado en `docs/PLAN_CODEX.md`).

---

## Riesgos y supuestos

- **Supuesto**: la reescritura de `scripts/claude-codex` se hace en paralelo a este plan (ya esta planeada en `docs/PLAN_CODEX.md` paso 6). Sin ese wrapper, los cambios de la TUI aqui descritos haran que `execv` corra un script que exit 1.
- **Riesgo**: si `codex login status` cambia su output (ej. deja de contener "logged in"), el `provider_status` va a reportar `falta login` falsamente. Workaround: parsear `~/.codex/auth.json` directamente, que es JSON.
- **Riesgo**: el bypass de `[1m]` para Codex puede romper si en el futuro Codex acepta ese sufijo. Workaround: dejar el comportamiento actual y hacer que el wrapper strippee.
- **No regresion**: los cambios a `do_login_action` para soportar strings son backwards-compatible (los otros providers siguen usando `callable` o `None`).
