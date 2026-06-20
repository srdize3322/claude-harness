# Plan — Provider Gemini/Antigravity para claude-harness

> Estado: **propuesta para evaluación**. No implementado. Documenta el diseño,
> las dos opciones técnicas, esfuerzo estimado y trade-offs.

## Resumen ejecutivo

Es **viable** integrar Antigravity/Gemini como provider en claude-harness al
estilo de Codex (suscripción + CLI + proxy local). El sistema operativo del
usuario ya tiene todo lo necesario:

- `~/.local/bin/agy` — CLI standalone de Antigravity (141 MB, Mach-O arm64).
- `/opt/homebrew/bin/gemini` — Gemini CLI oficial de Google.
- `~/.gemini/oauth_creds.json` — credenciales OAuth de Google Cloud activas,
  con `refresh_token` presente.

Además, hay un bonus inesperado: `agy models` lista no solo Gemini 3.5/3.1
sino también **Claude Opus 4.6 (Thinking)**, **Claude Sonnet 4.6 (Thinking)**
y **GPT-OSS 120B**, todos accesibles via la suscripción de Antigravity. Eso
significa que un solo provider nuevo abre acceso a tres familias de modelos.

## Auth

Antigravity y Gemini CLI comparten `~/.gemini/oauth_creds.json`:

```json
{
  "access_token": "ya29.a0AT3oNZ9Vj66jMBdR7Z...",
  "refresh_token": "1//...",
  "token_type": "Bearer",
  "expiry_date": 1781750776464,
  "scope": "https://www.googleapis.com/auth/cloud-platform ..."
}
```

Es el mismo patrón que usamos hoy con `~/.codex/auth.json`: el proxy lo lee,
inyecta `Authorization: Bearer <access_token>` en cada request al backend, y
si está expirado dispara un refresh con el `refresh_token` contra
`https://oauth2.googleapis.com/token`.

## Modelos disponibles

```
agy models
─────────
Gemini 3.5 Flash (Low|Medium|High)
Gemini 3.1 Pro (Low|High)
Claude Sonnet 4.6 (Thinking)
Claude Opus 4.6 (Thinking)
GPT-OSS 120B (Medium)
```

Los modelos tienen nombres "marketing" con sufijos de thinking embebidos —
hay que mapearlos a IDs internos antes de mandar al backend.

## Dos enfoques técnicos

### Opción A — `gemini-proxy.py` (REST API)

Patrón idéntico al `codex-proxy.py` que ya funciona:

```
Claude Code ──> smart-proxy ──> gemini-proxy ──> Google Generative AI API
                                     │                ↑
                                     └─ lee ~/.gemini/oauth_creds.json
                                     └─ refresh contra oauth2.googleapis.com
                                     └─ traduce Anthropic ↔ Gemini schema
```

**Pros**: streaming nativo via SSE, soporte completo de tool calls, system
prompts compuestos, mismo patrón mental que Codex.

**Contras**: traducción Anthropic↔Gemini no es trivial. Gemini usa `contents:
[{role, parts: [{text}, {functionCall}]}]` en vez de
`messages: [{role, content}]` de Anthropic. Tool calls y multimodal hay que
mapearlos campo a campo. Quizás 1–2 días de trabajo.

**URL del backend**: `https://generativelanguage.googleapis.com/v1beta/models/<MODEL>:streamGenerateContent`
con `?alt=sse` para streaming.

### Opción B — Subprocess de `agy` o `gemini` (MVP rápido)

El proxy invoca el CLI como subprocess y captura stdout:

```python
proc = subprocess.Popen(
    ["agy", "--print", prompt, "--model", model_name],
    stdout=subprocess.PIPE,
)
for line in proc.stdout:
    yield format_as_anthropic_sse(line.decode())
```

**Pros**: trivial de implementar (probado: `agy -p "responde solo: ping"
--model "Gemini 3.5 Flash (Low)"` ya responde `ping`). No requiere entender
el auth ni la traducción de schemas — el CLI lo maneja todo. Acceso
inmediato a Claude Opus 4.6, Sonnet 4.6, GPT-OSS y Gemini.

**Contras**:

- Latencia: cada request abre un proceso de ~141 MB. Cold start ~200–500 ms.
- Streaming pobre: hay que leer stdout línea por línea y simular SSE.
- Tool calls: imposibles (el CLI no expone función-call protocol al stdout).
- Conversación: cada `agy -p` es un proceso nuevo, sin memoria entre
  requests. Para mantener contexto habría que pasar el historial completo
  en cada invocación.
- System prompts complejos: difícil de encodear como CLI arg.

### Recomendación

**Hacer Opción B primero como MVP** para validar el flujo end-to-end y
desbloquear el acceso a los modelos. Después migrar a **Opción A** si querés
streaming real, tool calls y mejor latencia. La Opción A es el endgame; la B
es la pista de aterrizaje.

Ventaja de empezar con B: si Antigravity cambia su pricing o el OAuth, no
quedás bloqueado mientras rediseñás el proxy.

## Esfuerzo estimado

| Pieza | Opción B (CLI) | Opción A (REST) |
|-------|---------------|-----------------|
| `gemini-proxy.py` base | 2–3 h | 6–8 h |
| Mapping de modelos | 30 min | 30 min |
| Auth + refresh | 0 (lo hace el CLI) | 2–3 h |
| Schema translation (Anthropic↔Gemini) | mínimo | 4–6 h |
| Tool calls support | no factible | 3–4 h |
| Streaming | best-effort por líneas | nativo |
| UI integration en `claude-harness-ui.py` | 1–2 h | 1–2 h |
| Tests en `test-multi.sh` | 1 h | 1 h |
| **Total** | **5–7 h** | **17–25 h** |

## Cambios necesarios en el harness

Independiente de la opción:

1. **`PROVIDERS` list** (`claude-harness-ui.py:298+`) — agregar entrada
   `gemini` con su `provider_id`, label y launcher.
2. **`fetch_*_models`** — agregar `fetch_gemini_models()` que llama `agy
   models` y parsea la salida.
3. **`is_anthropic_model`** — si el modelo es "Claude * (Thinking)" servido
   por Antigravity, ¿lo consideramos Anthropic para el manejo de context
   window? Probablemente sí (mismo context window real).
4. **`smart-proxy.py:detect_backend`** — agregar prefix `gemini/` y
   heurística para "Gemini *" / "Claude * (Thinking)" / "GPT-OSS *".
5. **Nuevo `gemini-proxy.py`** en port 8082 con su lógica.
6. **`claude-multi`** — agregar `ensure_gemini_proxy_if_needed` y exportar
   `CLAUDE_HARNESS_GEMINI_PROXY_URL`.

## Riesgos

- **Antigravity puede aplicar rate limits/quota a sus modelos**. Antes de
  invertir en Opción A conviene confirmar con Santiago si su plan soporta
  uso programático intensivo o solo IDE interactivo.
- **El CLI `agy` puede cambiar la sintaxis de `--print` o `models` entre
  versiones**. Opción B es más frágil a eso; Opción A no le importa.
- **Tokens OAuth pueden requerir aprobación adicional** al cambiar de
  scope. El scope actual (`cloud-platform`) ya permite Vertex AI / Gemini
  API, así que probablemente no.

## Próximos pasos sugeridos

Si querés que avancemos:

1. Decidí Opción A o B.
2. Si B: te armo el `gemini-proxy.py` y la entrada en `PROVIDERS` en una
   sesión de ~5–7 h estimadas. Quedaría usable inmediatamente.
3. Si A: te armo un spec más detallado del schema mapping antes de
   implementar, así no perdemos tiempo en idas y vueltas.
