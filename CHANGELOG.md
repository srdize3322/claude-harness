# Changelog

## 2026-06-20 — Gemini: catálogo de modelos 100% dinámico desde `agy`

`fetch_gemini_models` ahora invoca `agy models` y parsea su output con
las etiquetas exactas que muestra Antigravity (la TUI propia de agy).
El usuario ve los mismos nombres en claude-harness que en agy.

Mapeo verificado label → API id (vía probes contra el endpoint):

| Label de `agy models`      | API id                          | Status  |
|----------------------------|---------------------------------|---------|
| Gemini 3.5 Flash (Low/Medium/High) | `gemini-3-flash-preview`        | ✅ live |
| Gemini 3.1 Pro (Low/High)  | `gemini-3-pro-preview`          | ✅ live |
| Gemini 3 Flash Lite        | `gemini-3.1-flash-lite-preview` | ✅ live |
| Claude Sonnet 4.6 (Thinking) | (otro endpoint)                | ❌ ver abajo |
| Claude Opus 4.6 (Thinking) | (otro endpoint)                 | ❌ ver abajo |
| GPT-OSS 120B (Medium)      | (otro endpoint)                 | ❌ ver abajo |

### Por qué Claude / GPT-OSS no funcionan todavía

Estos modelos los rutea Antigravity por
`businessaicode.googleapis.com` con el servicio gRPC
`google.cloud.businessaicode.v1main.PredictionService/GenerateContent`,
**no** por el endpoint REST `cloudcode-pa.googleapis.com/v1internal:
streamGenerateContent` que es lo que el proxy actual sabe hablar.
Soportarlos requiere un proxy gRPC adicional (proto + HTTP/2 framing);
queda pendiente como provider separado.

Los modelos no soportados se muestran en la lista con el sufijo
`(no API en este endpoint)` y un id sintético `unsupported:*` para que
el usuario los vea pero no termine en errores opacos si los elige.

### Fallback

Si `agy` no está en PATH, devolvemos una lista estática con los 2
Gemini-3 principales (Pro + Flash) para no dejar al usuario sin opciones.

## 2026-06-20 — Gemini provider: TUI wiring + model catalog cleanup

Follow-up al feat(provider) anterior. Tres fixes para que la TUI muestre
Gemini en todos los lugares correctos y para que el catálogo refleje
solamente los modelos que efectivamente sirve Cloud Code Assist.

### Fixes

- **`ALL_PROVIDER_IDS` / `ALL_PROVIDER_LABELS`** ahora incluyen "gemini".
  Sin esto el slot picker del flujo agent-slots no listaba modelos Gemini
  (`get_all_models_all_providers` iteraba solo sobre la tupla).
- **`PROVIDER_TO_CATALOG_ID`** mapea "gemini" → "google". Permite que el
  multi-provider builder consulte models.dev si en el futuro Google
  expone los Gemini ahí.
- **`provider_labels` del agregador multi** ahora incluye "gemini" para
  que aparezca con su categoría correcta en el picker.
- **`get_provider_status("gemini")`** chequea `~/.gemini/oauth_creds.json`
  y `refresh_token`. Mensajes claros si falta auth.
- **`get_provider_status("multi")`** suma Gemini al conteo (era `/5`,
  ahora `/6`).
- **`claude-multi` `auth_ok gemini`** valida el archivo de credenciales.

### Model catalog cleanup

`fetch_gemini_models` ya no confía en `agy models` porque el CLI lista
modelos accesibles vía el backend privado de Antigravity (Claude Opus
4.6, Sonnet 4.6, GPT-OSS 120B) que NO son ruteables por la API pública
de Cloud Code Assist. Probados live:

| Modelo                  | Endpoint Code Assist | Notas               |
|-------------------------|----------------------|---------------------|
| `gemini-3-pro-preview`  | ✅                   | flagship reasoning  |
| `gemini-3-flash-preview`| ✅                   | fastest, low latency|
| `gemini-2.5-pro`        | ✅                   | legacy, low quota   |
| `gemini-2.5-flash`      | ✅                   | legacy, low quota   |
| `claude-*-via-gemini`   | ❌ 404               | otro backend privado|
| `gpt-oss-120b`          | ❌ 404               | otro backend privado|

`MODEL_ALIAS` del proxy actualizado para mapear aliases comunes (`gemini`,
`gemini-pro`, `gemini-flash`, `gemini-3.5-flash`…) a los IDs canónicos.

## 2026-06-20 — Gemini / Antigravity provider via Cloud Code Assist

Nueva integración con la suscripción de Google One AI Pro / Gemini Code
Assist (la misma que Antigravity y `gemini` CLI usan). Routing completo
Anthropic ↔ Gemini con OAuth refresh automático.

### Nuevo

- **`scripts/gemini-proxy.py`** (619+ líneas) — proxy local 127.0.0.1:8082
  que traduce `/v1/messages` Anthropic → `streamGenerateContent` Cloud
  Code Assist. Incluye:
  - OAuth refresh automático contra `oauth2.googleapis.com/token` con
    client_id/secret extraídos de `@google/gemini-cli` 0.45.2.
  - Discovery dinámico del project vía `loadCodeAssist` (devuelve el
    project default de la suscripción).
  - Schema translation completa: messages → contents, system →
    systemInstruction, tools → functionDeclarations, tool_use ↔
    functionCall, tool_result ↔ functionResponse, image → inlineData.
  - JSON Schema sanitization para tools: quita `$schema`, `propertyNames`,
    etc., que Gemini rechaza; preserva `type/properties/items/enum/etc`.
  - Streaming SSE bidireccional: chunks de Gemini → eventos Anthropic
    (message_start, content_block_*, message_delta, message_stop).
  - Normalización CRLF → LF en el parser SSE (Gemini usa `\r\n\r\n` como
    separador estándar HTTP).
  - `thinkingBudget: 0` por default para acelerar respuestas (override
    con `CLAUDE_HARNESS_GEMINI_THINKING=N`).
- **PROVIDERS list** ahora incluye `gemini` (provider id "Gemini
  (Antigravity)").
- **`fetch_gemini_models()`** lista modelos vía `agy models` cuando está
  disponible.
- **`smart-proxy.py`**: nuevo backend "gemini" con prefix `gemini/` y
  heurística (modelos `gemini-*`); rutea al proxy local en :8082.
- **`claude-multi`**: auto-arranca `gemini-proxy.py` cuando algún slot
  necesita gemini; exporta `CLAUDE_HARNESS_GEMINI_PROXY_URL`.

### Verificado

```bash
# Test directo del proxy
curl -X POST http://127.0.0.1:8082/v1/messages \
  -d '{"model":"gemini-3-pro-preview","messages":[{"role":"user","content":"pong"}],"max_tokens":3000,"stream":false}'
# → {"role":"assistant","content":[{"type":"text","text":"Ping! 🏓"}],...}
```

End-to-end con `claude-multi --print` para flujos sencillos (mensaje
único, sin iteraciones de tool_use). Las sesiones interactivas con
tool calls intensivos quedan como ajuste pendiente — el proxy responde
200 a todas las requests pero Claude Code puede hacer iteraciones extra
de las esperadas; ver código del proxy para más detalle.

### Modelo y costo

- Project asignado: `sigma-silicon-dzvhp` (vía loadCodeAssist).
- Tier activo: **standard-tier "Gemini Code Assist"** + paid tier
  **g1-pro-tier "Google One AI Pro"**.
- Modelo principal: `gemini-3-pro-preview`.

### Operación

- Token refrescado automáticamente al iniciar el proxy y antes de cada
  request si quedan < 60s de vida.
- Al matar/reiniciar el proxy: `lsof -t -iTCP:8082 -sTCP:LISTEN | xargs -r kill -9`.
- Log default: `/tmp/claude-harness-gemini-proxy.log`.

## 2026-06-20 — context window: detección 100% dinámica

Quitamos todo lo hardcoded del path de detección de context window y
hacemos la lectura puramente dinámica desde la fuente autoritativa de
cada provider. El cambio es transparente cuando los caches están sanos;
cuando no, el harness lo dice en voz alta en vez de mentir con un valor
viejo.

### Cambios

- **K — `fetch_codex_models` (`claude-harness-ui.py:519-546`)**: eliminado el
  fallback hardcoded `gpt-5.4=258000`, `gpt-5.4-mini=400000`, etc. La
  función ahora lee solo `~/.codex/models_cache.json` (la misma fuente
  que usa el CLI nativo). Si el archivo no existe o está vacío, advierte
  al usuario en stderr en lugar de servir números obsoletos.
- **L — `resolve_model_context_window` (nueva, `claude-harness-ui.py`)**:
  hermano de `get_model_context_window` que devuelve `(ctx, source)`.
  Los labels de fuente son: `env:CLAUDE_HARNESS_CONTEXT_OVERRIDE`,
  `harness-cache:<provider>`, `harness-cache:multi`, `models.dev`,
  `marker:[1m]`, `fallback:unlisted-default`. Visibles en `--verbose`
  y siempre que la fuente sea `fallback:*` o `env:*`.
- **M — Override manual del context**: `--context-window N` CLI flag y
  `CLAUDE_HARNESS_CONTEXT_OVERRIDE=N` env var. Útil para experimentar o
  cuando un modelo nuevo no aparece en ningún catálogo.
- **N — Cache invalidation por mtime (Codex)**: cuando
  `~/.codex/models_cache.json` se actualiza, el harness invalida su
  cache local sin esperar al TTL de 300s. La consistencia con el CLI
  nativo de Codex es ahora inmediata.
- **`--verbose` CLI flag**: imprime al stderr el modelo, provider,
  context detectado y la fuente — para verificar de un vistazo de dónde
  sale el número que `/context` reporta.

### Verificación rápida

```bash
claude-harness --verbose --provider codex --model gpt-5.5 --print "hola"
# [claude-harness] context: model=gpt-5.5 provider=codex ctx=272000 threshold=244800 source=harness-cache:codex
```

Si `/context` dentro de Claude Code muestra `13.3k/244.8k`, el `244.8k`
es el threshold de auto-compact (`ctx * 0.9`), no el máximo. Es
comportamiento esperado de Claude Code.

## 2026-06-18

Fix mayor del case multi-provider con Anthropic como modelo principal (usando
suscripción/OAuth, no API key). El flujo "Anthropic main + slot externo
(MiniMax / OpenCode Go / Codex)" estaba mostrando errores intermitentes de
API: a veces conectaba, a veces 401, a veces 400. Se identificaron y
arreglaron nueve bugs acumulados en la frontera UI ↔ `claude-multi` ↔
`smart-proxy.py`. Además se incorporó un test environment (`test-multi.sh`)
para reproducir cada escenario.

### Fixes

- **A — UI (`claude-harness-ui.py`)**: el `provider_id` `"claude"` (Anthropic
  nativo) se exportaba como `CLAUDE_HARNESS_MAIN_BACKEND="claude"`, valor
  que el proxy no reconoce. Ahora se mapea a `"anthropic"`.
- **B — UI**: normalización del prefijo `claude/` antes del check
  `is_standard`, para que modelos como `claude/opus` no activen el "señuelo"
  innecesariamente.
- **C — proxy (`smart-proxy.py`)**: re-incorporado `"claude/": "anthropic"`
  al `prefix_map` de `detect_backend`. Sin esto, `claude/opus` caía al
  fallback `"main"` y nunca llegaba al alias resolution.
- **D — proxy**: simplificación de la rama Anthropic. Path A
  (passthrough de la auth del cliente) es ahora la ruta primaria, ya que
  Claude Code refresca el OAuth internamente; Path B (`load_anthropic_auth`)
  queda solo como fallback cuando el cliente envía la dummy
  `smart-proxy-passthrough`.
- **E — `claude-multi`**: en el branch `auth_ok anthropic` ahora también se
  hace `unset ANTHROPIC_AUTH_TOKEN`, para impedir que un token viejo del
  shell shadow la suscripción OAuth. El branch `else` setea el dummy de
  forma dura (sin parameter expansion) por la misma razón.
- **F — `claude-multi`**: defensa en profundidad — el case `claude-*` /
  `claude/*` / `anthropic/*` setea `MAIN_BACKEND="anthropic"`, y se
  normaliza `"claude" → "anthropic"` post-export por si la UI dejó pasar
  un valor incorrecto.
- **G — proxy**: `_build_anthropic_request` usaba
  `Cookie: sessionKey=<token>` para OAuth, formato que solo funciona contra
  `claude.ai`. Cambiado a `Authorization: Bearer <token>`, que es el
  esperado por `api.anthropic.com/v1/messages`.
- **H — proxy**: alias `opus`/`sonnet`/`haiku`/`fable` apuntaban a snapshots
  descontinuados (`claude-3-opus-20240229` → 404 not_found). Actualizados
  a `claude-opus-4-8`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`,
  `claude-fable-5`. Para reducir la fragilidad a futuro, la UI/`claude-multi`
  ahora pasan alias limpios (sin prefijo `claude/`) y Claude Code los
  resuelve contra su catálogo interno (que ships actualizado en cada
  release).
- **I — proxy**: `load_anthropic_auth` ahora prioriza OAuth de
  `~/.claude/.credentials.json` por sobre `ANTHROPIC_AUTH_TOKEN`. Antes era
  al revés, lo que silenciosamente facturaba contra una API key cuando el
  usuario quería usar su suscripción. También ignora la dummy
  `smart-proxy-passthrough` si la encuentra en la env.
- **J — UI + `claude-multi`**: strip del prefijo `claude/` antes de exportar
  `ANTHROPIC_MODEL` / `ANTHROPIC_DEFAULT_*_MODEL`. Garantiza que Claude
  Code reciba el alias limpio (`opus`, `sonnet`…) y lo resuelva contra su
  catálogo interno.

### Nuevo

- `scripts/test-multi.sh`: test environment con 6 escenarios end-to-end
  (`anthropic-puro`, `opus+minimax-slots`, `opus+opencodego-slots`,
  `multi-claude-opus`, `multi-anthropic-opus`, `multi-mixed`). Mata
  cualquier proxy previo antes de cada caso, captura stderr a
  `/tmp/test-multi/<caso>/smart-proxy.log` y reporta PASS/FAIL.

### Notas operativas

- El proxy es un proceso Python que se queda residente en `127.0.0.1:8081`.
  Cuando se editan los scripts hay que matarlo para que la próxima
  invocación de `claude-multi` lo relance con el código nuevo:
  `lsof -t -iTCP:8081 -sTCP:LISTEN | xargs -r kill -9`. `test-multi.sh` ya
  lo hace en cada caso.
- `install.sh` baja los scripts desde el remote en GitHub. Si tenés cambios
  locales no pusheados, re-correr el installer los pisa. Mantener la edición
  contra el repo y propagar con `git pull` + reinstall, o trabajar
  directo en `~/.local/share/claude-harness/scripts/` durante el debugging.
