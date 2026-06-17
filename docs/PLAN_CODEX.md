# Plan: Codex integration con claude-harness

## Goal
Permitir que Claude Code use modelos de Codex (gpt-5.4, gpt-5.5, etc.) via LinkDevice (ChatGPT OAuth), pasando por un Cloudflare Worker que traduce Anthropic API ↔ OpenAI Responses API.

## Arquitectura

```
Tu Mac
├── claude-harness-ui.py (TUI)
└── claude-codex (bash wrapper)
    │ Lee ~/.codex/auth.json
    │ Pasa access_token en ANTHROPIC_AUTH_TOKEN="codex:<token>:<account_id>"
    └─ exec claude
            │
            ▼ HTTPS
       Cloudflare Worker (TU deploy, stateless)
       ├── Routing: si ANTHROPIC_AUTH_TOKEN empieza con "codex:" → modo Codex
       ├── handleCodexMessages: traduce Anthropic → OpenAI Responses API
       │   Llama chatgpt.com/backend-api/codex/responses
       │   Con Bearer <access_token> + chatgpt-account-id
       └── Si no → handleOpenCodeGoMessages (modo actual, sin cambios)
            │
            ▼ HTTPS
       Backend Codex (OpenAI)
```

## Scope del Cut 1

- Solo SSE (HTTP streaming), no WebSocket
- Wrapper bash maneja TODO el OAuth (device-auth, refresh)
- Worker es stateless, solo forwardea el token
- Paridad casi total con opencode (todas las features excepto WS)

## Decisiones de diseno

- Auth: client_id="app_EMoamEEZ73f0CkXaXp7hrann" (fijo, oficial OpenAI)
- Auth base: https://auth.openai.com
- API base: https://chatgpt.com/backend-api
- Endpoint: /codex/responses
- Header magic: ANTHROPIC_AUTH_TOKEN="codex:<access_token>:<chatgpt_account_id>"
- Worker NO guarda tokens (stateless)

## Archivos a tocar

| Archivo | Cambio |
|---------|--------|
| `worker/src/index.js` | +handleCodexMessages, routing, ~400 lineas |
| `worker/src/codex-protocol.js` | NUEVO, conversion Anthropic <-> OpenAI Responses |
| `worker/src/codex-handler.js` | NUEVO, request/response handler |
| `worker/test/` | NUEVO, unit tests del handler |
| `worker/wrangler.toml` | Comment updates |
| `worker/README.md` | NUEVO, deploy instructions |
| `scripts/claude-codex` | Rewrite completo (~100 lineas) |
| `scripts/claude-harness-ui.py` | +50 lineas (provider config) |
| `docs/README.md` | +150 lineas (Codex setup) |

## Pasos de ejecucion (orden)

1. **Analisis** (5 subagentes en paralelo) -> este plan
2. Preparar estructura del Worker (skeletons + tests vacios)
3. Port del protocolo (Anthropic -> OpenAI)
4. Port del handler (OpenAI -> Anthropic SSE)
5. Routing en handleMessages
6. Reescribir claude-codex wrapper
7. Cambios a la TUI Python
8. Documentacion + audit
9. Push al repo + deploy del Worker

## Riesgos

- API Codex no documentada (puede cambiar)
- Rate limits compartidos con CLI Codex
- Tokens personales en logs (audit final necesario)
- Tiempo de port (~1200 lineas TS)
