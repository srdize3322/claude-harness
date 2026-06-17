# worker/

Cloudflare Worker que traduce la API de Anthropic Messages (lo que habla
Claude Code) a dos backends distintos según el header `Authorization`:

- **OpenCode Go mode** (default): usa `OPENCODE_BASE_URL` +
  `OPENCODE_API_KEY` (compatible con cualquier backend OpenAI-style).
- **Codex mode**: cuando el header empieza con `codex:`. Usa el
  access_token de ChatGPT que viene en el header y rutea a
  `chatgpt.com/backend-api/codex/responses`.

Un solo Worker, un solo deploy, dos modos.

## Prerrequisitos

- Una cuenta de Cloudflare (el free tier alcanza).
- Node.js 18+ y `npm`.
- Opcional: la CLI de Codex (`brew install codex`) si vas a usar
  Codex mode.

## Setup

```bash
cd worker/
npm install
```

Si todavía no autenticaste `wrangler` con Cloudflare:

```bash
npx wrangler login
```

Solo si vas a usar **OpenCode Go mode**, seteá la API key como secret:

```bash
npx wrangler secret put OPENCODE_API_KEY
# pegá el valor y Enter
```

Codex mode no necesita ningún secret: el access_token viaja en cada
request dentro del header.

## Deploy

```bash
npx wrangler deploy
```

Wrangler te devuelve la URL del Worker, algo como:

```
Published opencode-go-proxy (1.2.3)
  https://opencode-go-proxy.<tu-subdominio>.workers.dev
```

Esa URL es la que va en `CLAUDE_HARNESS_CODEX_PROXY_URL` (Codex mode) o
la que usa el wrapper de OpenCode Go (`scripts/claude-opencode-go`).

## Cómo se elige el modo

El Worker mira el header `Authorization` en cada request:

| Header                                              | Modo            | Auth                                       |
|-----------------------------------------------------|-----------------|--------------------------------------------|
| `Authorization: Bearer <key>` (o `x-api-key: <key>`) | OpenCode Go     | secret `OPENCODE_API_KEY`                  |
| `Authorization: codex:<access_token>:<account_id>`  | Codex           | access_token del header, account_id del header |
| Cualquier otra cosa                                  | `401`           | —                                          |

El prefijo `codex:` es solo el trigger. El access_token **no se loggea,
no se persiste, no se manda a ningún lado que no sea el backend de
OpenAI**. Cada request es stateless.

## Variables de entorno

Definidas en `wrangler.toml` (sección `[vars]`):

- `OPENCODE_BASE_URL`: URL base del backend OpenCode Go (default:
  `https://opencode.ai/zen/go/v1`).
- `DEFAULT_MODEL`: modelo default si el cliente no manda uno.

Para testing o setups custom, override con `wrangler dev --var`:

```bash
npx wrangler dev --port 8787 \
  --var "OPENCODE_BASE_URL=http://127.0.0.1:11434/v1"
```

Y para apuntar Codex mode a un mock backend:

```bash
npx wrangler dev --port 8787 \
  --var "CODEX_BASE_URL=http://127.0.0.1:9999"
```

## Desarrollo local (sin deploy)

Podés correr el Worker local con `wrangler dev` y un mock backend en
Python para testear Codex mode sin gastar quota real:

```bash
# Terminal 1: mock backend
python3 worker/test/mock-codex-backend.py
# (corre en 127.0.0.1:9999)

# Terminal 2: worker local
cd worker/
npx wrangler dev --port 8787 --var "CODEX_BASE_URL=http://127.0.0.1:9999"
```

Para OpenCode Go mode localmente podés apuntar a cualquier servidor
OpenAI-compatible (`vllm`, `ollama`, `llama.cpp`, etc.):

```bash
# Asumiendo ollama en 11434
npx wrangler dev --port 8787 \
  --var "OPENCODE_BASE_URL=http://127.0.0.1:11434/v1" \
  --var "OPENCODE_API_KEY=ollama"
```

## Tests

Unit tests del protocolo y handler (Node native test runner, no deps):

```bash
cd worker/
node --test test/codex-protocol.test.js
node --test test/codex-handler.test.js
```

Test del wrapper bash (crea un `auth.json` sintético, no toca red):

```bash
bash worker/test/test-claude-codex.sh
```

End-to-end con mock backend (levanta Worker + mock y manda un curl real):

```bash
# Terminal 1
python3 worker/test/mock-codex-backend.py

# Terminal 2
cd worker/ && npx wrangler dev --port 8787 \
  --var "CODEX_BASE_URL=http://127.0.0.1:9999"

# Terminal 3
bash worker/test/test-claude-codex.sh  # tests del wrapper, no del Worker
```

## Arquitectura

```
   Claude Code          Cloudflare Worker              Backend real
 ┌─────────────┐       ┌───────────────────────┐       ┌──────────────┐
 │  ANTHROPIC  │       │  detectCodexMode()    │       │              │
 │  Messages   │──────▶│  Authorization header │       │              │
 │  /v1/       │       │  ┌─────────────────┐  │       │              │
 │  messages   │       │  │ "Bearer xyz"    │──┼──────▶│ OpenCode Go  │
 │             │       │  │   → OpenCode    │  │       │ (OpenAI API) │
 │             │       │  │ "codex:t:acc"   │──┼──────▶│ chatgpt.com  │
 │             │       │  │   → Codex       │  │       │ /backend-api │
 └─────────────┘       │  └─────────────────┘  │       │ /codex/      │
                       │                       │       │ responses    │
                       │  anthropicToOpenAI()  │       └──────────────┘
                       │  anthropicToCodex..() │
                       └───────────────────────┘
```

- `src/index.js`: handler de OpenCode Go (Anthropic → OpenAI Chat
  Completions, con SSE streaming).
- `src/codex-handler.js`: handler de Codex (Anthropic → OpenAI Responses
  API, con SSE streaming y retry con backoff).
- `src/codex-protocol.js`: converters puros (sin I/O), fáciles de
  testear aislados.
- `test/`: unit tests + mock backend en Python + test del wrapper bash.

## Troubleshooting

### `wrangler: command not found`

`npm install` no instaló las devDependencies. Corré `npm install` en
`worker/`. Si seguís sin wrangler, probá `npx wrangler --version`.

### `Authentication error [code: 401]` en Codex mode

El access_token venció. Re-logineá con `codex login --device-auth`. El
wrapper intenta refresh automático, pero si falló puede ser que el
refresh_token también esté vencido (pasa si no usás Codex por meses).

### El Worker devuelve 404 contra `chatgpt.com`

Codex mode está hablando con el endpoint equivocado. Verificá que
estés en el último deploy. El endpoint puede haber cambiado — es no
oficial.

### Streaming se corta a la mitad

El `text/event-stream` del backend de Codex es experimental y se cae
bajo carga. El Worker tiene retry con backoff exponencial (ver
`codex-protocol.js:retryDelay`), pero si persiste puede ser un cambio
en el schema de eventos. Mirá los logs en
https://dash.cloudflare.com → Workers → tu Worker → Logs.

### `codex:fake_token:acc_123` en curl da 401

`parseCodexAuth` (en `codex-handler.js`) requiere el formato
`<token>:<account_id>` con **exactamente** el último `:` separando
account_id. Si tu header no matchea, da 401 sin distinguir el motivo.

### El deploy falla con "Authentication error [code: 10000]"

Tu `wrangler` no está autenticado. Corré `npx wrangler login` y reintentá.

### Costos / cuotas

El Worker corre en el free tier de Cloudflare (100k requests/día). Si
te pasás, Cloudflare cobra por request, no por compute. Codex mode
además consume la quota de ChatGPT Plus/Pro (no API key), así que
andate con cuidado con loops largos.

## Seguridad

- **Nunca commitees `OPENCODE_API_KEY`**. Va solo como secret de
  Cloudflare (`npx wrangler secret put`).
- **Nunca commitees el access_token de Codex**. Va en
  `~/.codex/auth.json` (permisos 600) y solo se manda dentro del
  header `Authorization`.
- **El Worker es stateless**: no loggea tokens, no los persiste, no los
  manda a ningún lado que no sea OpenAI/Codex.
- **El repo es público**: revisá que no haya paths hardcodeados
  (`/Users/...`, `/home/...`) ni tokens en los tests antes de pushear.
