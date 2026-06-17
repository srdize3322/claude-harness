# Design: Codex Handler Module para Cloudflare Worker

**Estado**: Draft para implementacion
**Scope**: Cut 1 del PLAN_CODEX.md — solo SSE (HTTP streaming), sin WebSocket
**Worker existente**: `REPO/worker/src/index.js` (749 lineas, ESM JS)

---

## Resumen ejecutivo

Anadiremos un modo "Codex" al Worker existente. El routing se decide por el prefijo
`codex:` en `ANTHROPIC_AUTH_TOKEN` (header `Authorization`). Cuando se detecta este prefijo,
se parsea el token y el `chatgpt-account-id` desde el header, y se enruta la peticion a
`handleCodexMessages` en vez de `handleMessages`. El modo OpenCode Go existente no se
toca: se mantiene como default y como fallback si el token no tiene el prefijo.

La conversion de protocolo (Anthropic Messages API <-> OpenAI Responses API) se aísla en
`src/codex-protocol.js` como funciones puras. La logica de fetch, reintentos, SSE y
formateo de errores vive en `src/codex-handler.js`. Helpers pequenos (parseo de JWT,
extraccion de account_id, headers especificos de Codex, mapeo de errores) viven en
`src/codex-utils.js`.

El bundle final no debe pasar ~600KB de codigo minificado (Worker gratis = 1MB, paid = 10MB
comprimido). Estimo ~30-40KB adicionales sobre los ~12KB actuales.

---

## Section 1: Module Structure

### Recomendacion: Opcion B (archivos separados)

```
worker/src/
├── index.js               # Router + health + count_tokens (~80 lineas, reducido)
├── opencode-handler.js    # Movido desde index.js (handleMessages + openai-*)
├── codex-handler.js       # NUEVO — orquestador HTTP + retry + SSE
├── codex-protocol.js      # NUEVO — funciones puras de conversion
├── codex-utils.js         # NUEVO — helpers pequenos (<200 lineas)
└── shared-errors.js       # NUEVO — formatAnthropicError() extraido de index.js
```

**NO recomendado**: Opcion A (inline en `index.js`).
- El port del protocolo Codex a JS seria ~800-1000 lineas. Sumado a las 749 actuales,
  `index.js` pasaria de 1800 lineas: inmanejable, sin tests aislables, conflict-prone.
- Wrangler bundlea todos los modulos ESM en un solo chunk, asi que el tamaño del bundle
  no cambia — pero la claridad del codigo si.

**Justificacion**:

1. **Tamano del codigo**: La implementacion de referencia de Pi (`openai-codex-responses.ts`)
   son 1374 lineas TS. Recortando todo lo que no aplica a este caso (WebSocket, diagnosticos
   de Pi, cost/pricing, model catalog, transformacion desde mensajes Pi), quedaria en
   ~800 lineas JS. Sumado al `index.js` actual de 749, son ~1500 lineas. Demasiado para un
   solo archivo.

2. **Testabilidad**: Las funciones de `codex-protocol.js` (conversor Anthropic -> Codex body,
   normalizador de eventos Codex SSE -> Anthropic SSE) son **puras** (mismo input -> mismo
   output, sin I/O, sin estado global). Esto permite tests unitarios con
   `node --test` sin levantar mocks de fetch. Si estan mezcladas con la logica de
   fetch/retry, los tests se vuelven fragiles.

3. **Backward compatibility**: La funcion `handleMessages` no esta exportada — solo se
   usa internamente desde el default export. Podemos refactorizarla a `opencode-handler.js`
   sin tocar el contrato publico del Worker (las rutas `/v1/messages`, `/health`,
   `/v1/messages/count_tokens`).

4. **Bundle size**: Wrangler hace tree-shaking automatico sobre imports ESM estaticos.
   Si un modo nunca se activa (e.g. el usuario solo usa OpenCode Go), el codigo de Codex
   **si o si** entra al bundle porque JavaScript no permite dead-code elimination entre
   condicionales dinamicas. Estimado del bundle adicional: ~20-30KB minificado. Aceptable.

5. **Aislamiento de riesgo**: Un bug en el modo Codex no debe poder crashear el modo
   OpenCode Go. Con modulos separados, un error de import en `codex-protocol.js` falla
   al deploy (visible), no en runtime.

### Alternativa considerada: Opcion C (todo en un subdirectorio `src/codex/`)

```
src/codex/
├── index.js
├── protocol.js
├── handler.js
└── utils.js
```

Rechazada. Anade un nivel de anidacion sin beneficio: el `index.js` raiz ya es el "fachada"
y debe hacer el routing. Tener un subdirectorio `codex/` sugiere que es autocontenido,
pero el router en raiz necesita importar de cualquier subdir de todas formas.

---

## Section 2: Routing Strategy

### Recomendacion: Opcion A — prefijo en `ANTHROPIC_AUTH_TOKEN`

Header que llega al Worker:
```
Authorization: codex:<jwt_access_token>:<chatgpt_account_id>
```

El wrapper bash (`scripts/claude-codex`) sera responsable de:
1. Leer `~/.codex/auth.json` (formato de OpenAI CLI)
2. Extraer `access_token` (JWT) y `account_id` del claim
   `https://api.openai.com/auth.chatgpt_account_id`
3. Exportar `ANTHROPIC_AUTH_TOKEN="codex:${access_token}:${account_id}"`

El Worker detecta el modo inspeccionando el header, **no** leyendo env vars ni secretos.

### Por que A y no las otras

- **Opcion B (headers separados `X-Codex-Token` + `X-Codex-Account-Id`)**: Claude Code (el
  cliente que habla Anthropic API) solo controla el header `Authorization` (lo setea a
  partir de `ANTHROPIC_AUTH_TOKEN`). No hay forma limpia de inyectar headers custom al
  cliente HTTP que habla con el Worker. Requeriria un proxy local o patchear Claude Code.
- **Opcion C (env var del Worker)**: Imposible. El Worker recibe un request HTTPS, no
  tiene acceso al env de quien lo llama. Los env vars del Worker (`env.OPENCODE_API_KEY`,
  etc.) son los del deploy, no del request. Esto confundiria credenciales por-deployment
  con credenciales por-request.

### Pseudocodigo del routing

```javascript
// src/index.js (extracto del default export)
export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/health" && request.method === "GET") {
      return new Response("OK", { headers: { "Content-Type": "text/plain" } });
    }

    if (request.method === "POST" && url.pathname.endsWith("/messages")) {
      if (url.pathname.includes("count_tokens")) {
        return handleCountTokens(request);
      }
      return routeMessages(request, env);
    }

    return new Response(JSON.stringify({
      type: "error",
      error: { type: "not_found", message: `Unknown endpoint: ${request.method} ${url.pathname}` },
    }), { status: 404, headers: { "Content-Type": "application/json" } });
  },
};

function routeMessages(request, env) {
  const auth = request.headers.get("Authorization") || "";
  if (auth.startsWith("codex:")) {
    return handleCodexMessages(request, env, auth);
  }
  return handleMessages(request, env); // opencode mode (default)
}
```

### Parsing del token Codex

En `src/codex-utils.js`:

```javascript
export function parseCodexAuth(authHeader) {
  // Formato esperado: "codex:<jwt>:<account_id>"
  // El JWT tiene 3 segmentos separados por ".", ninguno contiene ":"
  const match = authHeader.match(/^codex:([^:]+):(.+)$/);
  if (!match) {
    return { ok: false, error: "Formato invalido. Esperado: codex:<token>:<account_id>" };
  }
  const [, accessToken, accountId] = match;

  // Validacion basica del JWT
  const segments = accessToken.split(".");
  if (segments.length !== 3) {
    return { ok: false, error: "Token no es un JWT valido" };
  }

  // Validacion de account_id (UUID-like)
  if (!/^[0-9a-f-]{32,40}$/i.test(accountId)) {
    return { ok: false, error: "account_id no tiene formato UUID" };
  }

  return { ok: true, accessToken, accountId };
}
```

**Nota de seguridad**: El access_token **nunca** debe aparecer en logs. Esto esta
cubierto por el `observability` block en `wrangler.toml`, pero se debe auditar que
ningun `console.log` lo filtre. Recomendado: en el handler, si hay error, loggear solo
el `code` del error de Codex, no el payload completo.

---

## Section 3: Reuse Plan

### Que se puede reusar

| Componente | Reusable? | Notas |
|---|---|---|
| `anthropicToOpenAI` (linea 4) | **NO** | Produce OpenAI Chat Completions (`messages: [...]` plano). Codex usa OpenAI Responses API (`input: [...]` con `ResponseInputItem` tipados: `message`, `function_call`, `function_call_output`). La forma del body es fundamentalmente distinta. |
| `openAIToAnthropic` (linea 252) | **NO** (parcial) | Mapea Chat Completions response. La forma de la respuesta de Codex (Responses API) es diferente: en vez de `choices[0].message`, hay `output` con items tipados. Se necesita un mapeo nuevo. |
| `transformStream` (linea 304) | **NO** | Parsea SSE de Chat Completions (`data: {choices: [{delta: {content, tool_calls}}]}`). Codex emite eventos tipados distintos (`response.output_text.delta`, `response.function_call_arguments.delta`, `response.reasoning_summary_text.delta`). |
| SSE encoding (`sseEvent`, `encoder.encode`) | **SI** | El formato de salida hacia Claude Code es Anthropic SSE (`event: foo\ndata: {...}\n\n`). Esta parte **no cambia** entre modos. Se extrae a `shared-sse.js`. |
| Error formatting (linea 595, 610, 638) | **SI** | El formato de error Anthropic (`{type: "error", error: {type, message}}`) es identico en ambos modos. Se extrae a `shared-errors.js`. |
| `handleCountTokens` (linea 705) | **SI** | Stateless, sin I/O, sin dependencias. Sin cambios. |
| `estimateTokens` / `estimateMessageTokens` (linea 675-703) | **SI** | Sin cambios. |

### Que se duplica vs comparte

**Duplicado** (intencionalmente, para mantener modulos desacoplados):

- Las funciones de conversion de protocolo (`codex-protocol.js` es independiente del
  `opencode-handler.js`). No hay shared protocol logic entre los dos modos.

**Compartido**:

- `shared-errors.js`: `formatAnthropicError(type, message, status)` — usado por ambos
  handlers.
- `shared-sse.js`: `sseEvent(event, data)` + `TextEncoder` cached — usado por el
  transformador SSE de Codex y, opcionalmente, por el de OpenCode Go (refactor menor).

### Decision: NO refactorizar el modo OpenCode Go en este Cut

El plan dice "OpenCode Go (default): sin cambios". Respetamos eso. No movemos
`handleMessages` a `opencode-handler.js` ni extraemos `sseEvent` hasta no haber validado
que el modo Codex funciona. La duplicacion de `sseEvent` (~5 lineas) entre los dos
modulos es aceptable por ahora; se puede extraer despues en un PR de cleanup.

**Excepcion**: Si `shared-errors.js` resulta trivialmente util y el factor de copiar/pegar
es molesto (>10 sitios de uso), se puede extraer desde el inicio. Mi recomendacion:
extraer desde el inicio, son 15 lineas y elimina ~4 bloques de error formatting duplicados.

---

## Section 4: File-by-File Plan

### 4.1 `src/shared-errors.js` (NUEVO)

**Proposito**: Centralizar el formato de error Anthropic. Usado por `index.js`,
`opencode-handler.js`, `codex-handler.js`.

**Public API**:

```javascript
// status default 400 si no se especifica
export function anthropicErrorResponse(type, message, status = 400): Response

// Helpers semanticos
export function invalidRequestError(message): Response
export function authError(message): Response
export function apiError(message, upstreamStatus = 502): Response
export function notFoundError(message): Response
```

**Internals**: Constante `ANTHROPIC_ERROR_BODY = (type, message) => ({ type: "error", error: { type, message } })`.

**Tests**: No necesita tests dedicated (es trivial), pero aparecera en tests de los
handlers.

---

### 4.2 `src/codex-utils.js` (NUEVO)

**Proposito**: Helpers especificos del modo Codex, sin estado, sin I/O.

**Public API**:

```javascript
// Parsea el header "codex:<token>:<account_id>"
export function parseCodexAuth(authHeader: string): {ok: true, accessToken, accountId} | {ok: false, error}

// Extrae account_id de un JWT decodificando el payload
// Usado solo en tests/CLI; el Worker lo recibe del header
export function extractAccountIdFromJwt(jwt: string): string

// Headers requeridos por chatgpt.com/backend-api/codex/responses
export function buildCodexHeaders(accessToken, accountId, requestId): Record<string, string>

// Genera un request_id para x-client-request-id
export function newCodexRequestId(): string

// Resuelve la URL final del endpoint Codex
export function resolveCodexUrl(baseUrl?: string): string  // default: https://chatgpt.com/backend-api/codex/responses

// Mapea status de la response de Codex -> stop_reason Anthropic
//   completed -> end_turn
//   incomplete -> max_tokens
//   failed -> end_turn (con error en content)
//   cancelled -> end_turn
export function mapCodexStatusToStopReason(status: string): "end_turn" | "max_tokens" | "tool_use"

// Mapea errores HTTP de Codex a errores Anthropic-friendly
//   401 -> authentication_error (401)
//   429 -> api_error con mensaje "Rate limit reached" (429)
//   5xx -> api_error (502)
//   otros -> api_error (status original)
export function mapCodexHttpError(status: number, body: string): {type: string, message: string, status: number}

// Determina si un error HTTP es retryable
export function isCodexRetryableError(status: number, body: string): boolean
```

**Internals**:
- `sleep(ms, signal)`: helper de backoff (copiado del Pi ref, sin aborter signal por
  ahora — los requests del Worker no son cancelables mid-flight en el mismo sentido que
  Node).
- Constante `CODEX_BASE_URL = "https://chatgpt.com/backend-api"`.
- Constante `CODEX_ENDPOINT = "/codex/responses"`.
- Constante `CODEX_USER_AGENT = "claude-code/1.0 (via claude-harness-worker)"`.
- Constante `CODEX_RETRYABLE_STATUSES = new Set([429, 500, 502, 503, 504])`.

**Test strategy** (Vitest o `node --test`):
- `parseCodexAuth`: matriz de inputs validos/invalidos.
- `extractAccountIdFromJwt`: JWT firmado de ejemplo (puede ser hardcodeado en el test).
- `buildCodexHeaders`: snapshot del objeto resultante.
- `mapCodexStatusToStopReason`: tabla de verdad.
- `mapCodexHttpError`: matriz de (status, body) -> output.
- `isCodexRetryableError`: status codes que deberian y no deberian reintentar.

---

### 4.3 `src/codex-protocol.js` (NUEVO)

**Proposito**: Funciones **puras** de conversion de protocolo. Sin I/O, sin fetch, sin
estado. Testeable sin mocks.

**Public API**:

```javascript
// Convierte un body Anthropic Messages API -> body OpenAI Responses API
export function anthropicToCodex(anthropicBody: AnthropicMessagesRequest): CodexRequest

// Convierte un evento SSE de Codex (ya parseado a objeto) -> 0..N eventos SSE Anthropic
// Devuelve un array porque un evento Codex puede mapear a multiples eventos Anthropic
// (e.g. response.output_item.done con type=function_call cierra el tool_use block Y emite
//  message_delta con stop_reason=tool_use).
export function codexEventToAnthropic(event: object, state: CodexStreamState): AnthropicEvent[]

// Convierte la response final no-stream de Codex a body Anthropic
// (en realidad casi siempre vamos a streamear, pero por si el cliente lo pide)
export function codexNonStreamToAnthropic(codexResponse: object, requestModel: string): AnthropicMessagesResponse
```

**Tipo `CodexStreamState`** (mutable, pasado por referencia desde el handler):

```javascript
{
  messageId: string,
  model: string,
  contentBlockIndex: number,
  openBlocks: {
    thinking?: {index, signature?},
    text?: {index, signature?},
    toolUse?: {index, id, name, inputSoFar},
  },
  textBlockOpen: boolean,
  thinkingBlockOpen: boolean,
  toolBlockOpen: boolean,
  finished: boolean,
  // Acumuladores
  reasoningText: string,
  textText: string,
  toolInputJson: string,
  // Para mapeo de items a bloques
  outputItemToBlock: Map<string, "thinking" | "text" | "toolUse">,
  // Ultimo response.completed visto (para extraer usage)
  finalResponse: object | null,
}
```

**`anthropicToCodex(request)` — algoritmo**:

Input (Anthropic):
```json
{
  "model": "gpt-5.5",
  "max_tokens": 1024,
  "system": "You are helpful",
  "messages": [
    {"role": "user", "content": "Hi"},
    {"role": "assistant", "content": [
      {"type": "text", "text": "Hello!"},
      {"type": "tool_use", "id": "toolu_xxx", "name": "get_weather", "input": {"city": "SF"}}
    ]},
    {"role": "user", "content": [
      {"type": "tool_result", "tool_use_id": "toolu_xxx", "content": "72F sunny"}
    ]}
  ],
  "tools": [{"name": "get_weather", "description": "...", "input_schema": {...}}],
  "stream": true
}
```

Output (Codex Responses API):
```json
{
  "model": "gpt-5.5",
  "store": false,
  "stream": true,
  "instructions": "You are helpful",
  "input": [
    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hi"}]},
    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hello!"}], "id": "msg_xxx", "status": "completed"},
    {"type": "function_call", "call_id": "toolu_xxx", "name": "get_weather", "arguments": "{\"city\":\"SF\"}"},
    {"type": "function_call_output", "call_id": "toolu_xxx", "output": "72F sunny"}
  ],
  "tools": [{"type": "function", "name": "get_weather", "description": "...", "parameters": {...}, "strict": false}],
  "tool_choice": "auto",
  "parallel_tool_calls": true,
  "include": ["reasoning.encrypted_content"],
  "text": {"verbosity": "low"},
  "reasoning": {"effort": "medium", "summary": "auto"}
}
```

**Reglas de conversion clave** (copiadas/adaptadas de `convertResponsesMessages` en Pi):

1. `system` (string o array) -> top-level `instructions`. **No** se mete en `input[]`.
2. `messages[].content` string -> `{type: "message", role, content: [{type: "input_text", text}]}`.
3. `messages[].content[].type === "text"` -> parte de `content: [{type: "input_text"/"output_text"}]`
   segun si es user o assistant.
4. `messages[].content[].type === "image"` -> `{type: "input_image", image_url: "data:...;base64,..."}`.
5. `messages[].content[].type === "tool_use"` (assistant) -> `{type: "function_call", call_id, name, arguments}`.
   El `id` Anthropic se mapea a `call_id`. **No** emitimos `id` de Responses (eso lo
   gestiona Codex).
6. `messages[].content[].type === "tool_result"` -> `{type: "function_call_output", call_id, output}`.
   El `tool_use_id` Anthropic se mapea a `call_id`.
7. Tools Anthropic (`{name, description, input_schema}`) -> `{type: "function", name, description, parameters: input_schema, strict: false}`.
8. `thinking` block en assistant messages: **NO** se envia a Codex. Se ignora silenciosamente.
   Codex maneja su propio `reasoning` configurado en `body.reasoning`.
9. `thinking` param en el request top-level: se traduce a `body.reasoning.effort`
   (mapeo de budget_tokens -> effort igual que ya hace `translateThinking` en `index.js`,
   pero aplicado al formato de Codex).
10. `max_tokens` -> **se ignora**. Codex usa `max_output_tokens` en un lugar distinto, pero
    el cut 1 no lo respeta para mantener simpleza. Se loggea warning si se recibe.
11. `temperature` -> `body.temperature` solo si != null.
12. `top_p` -> **se ignora** (Codex lo acepta pero no es recomendado con reasoning).
13. `stop_sequences` -> **se ignora** (Codex no lo soporta).

**`codexEventToAnthropic(event, state)` — algoritmo**:

Tabla de mapeo (basada en `processResponsesStream` de Pi, adaptada a Anthropic SSE):

| Evento Codex | Eventos Anthropic emitidos |
|---|---|
| `response.created` | (none — se usa para guardar `response.id` en state) |
| `response.output_item.added` con `item.type === "reasoning"` | `content_block_start` (thinking) |
| `response.output_item.added` con `item.type === "message"` | `content_block_start` (text) |
| `response.output_item.added` con `item.type === "function_call"` | `content_block_start` (tool_use) |
| `response.reasoning_summary_text.delta` | `content_block_delta` (thinking_delta) |
| `response.reasoning_text.delta` | `content_block_delta` (thinking_delta) |
| `response.reasoning_summary_part.done` | (none — se concatena al thinking block) |
| `response.content_part.added` | (none — filtrar `ReasoningText`, aceptar `output_text`/`refusal`) |
| `response.output_text.delta` | `content_block_delta` (text_delta) |
| `response.refusal.delta` | `content_block_delta` (text_delta, marcar como refusal si se quiere — cut 1 lo trata como text) |
| `response.function_call_arguments.delta` | `content_block_delta` (input_json_delta) |
| `response.function_call_arguments.done` | (none — el .delta previo ya cubrio) |
| `response.output_item.done` con `item.type === "reasoning"` | `content_block_stop` (thinking) |
| `response.output_item.done` con `item.type === "message"` | `content_block_stop` (text) |
| `response.output_item.done` con `item.type === "function_call"` | `content_block_stop` (tool_use) |
| `response.completed` | `message_delta` (con stop_reason y usage) + `message_stop` |
| `response.failed` | (error thrown al handler, no se emite nada) |
| `error` | (error thrown al handler) |
| Otros (e.g. `response.in_progress`, `response.queued`) | (none — ignorar) |

**Especial**:
- En `start` (primera llamada), el handler emite un `message_start` antes del primer
  `codexEventToAnthropic`. Esta funcion solo emite `content_block_*`, `message_delta`,
  `message_stop`.
- El `message_start` se emite **una sola vez** al inicio del stream, controlado por el
  handler, no por esta funcion.
- Si el primer evento util es `response.output_item.added` con `reasoning`, abrimos
  el bloque thinking. Si es `message`, abrimos text. Si es `function_call`, abrimos
  tool_use. (Igual que en `transformStream` actual.)
- Multiples reasoning blocks: Codex puede emitir varios (e.g. interleave con tool calls).
  El state debe trackear cual esta abierto.
- `response.completed.response.usage` -> `output_tokens` se emite en `message_delta.usage`.

**`codexNonStreamToAnthropic(response, model)` — algoritmo**:

Para el caso no-stream (cuando `body.stream === false`). La mayoria de los clientes
(Claude Code) usan stream, pero se implementa para completeness.

```javascript
{
  id: response.id || `msg_${Date.now()}`,
  type: "message",
  role: "assistant",
  model: response.model || model,
  content: [
    // Construir array de content blocks desde response.output
    ...response.output.filter(i => i.type === "reasoning").map(i => ({
      type: "thinking",
      thinking: i.summary?.map(s => s.text).join("\n\n") || ""
    })),
    ...response.output.filter(i => i.type === "message").map(i => ({
      type: "text",
      text: i.content?.map(c => c.type === "output_text" ? c.text : c.refusal).join("")
    })),
    ...response.output.filter(i => i.type === "function_call").map(i => ({
      type: "tool_use",
      id: i.call_id,
      name: i.name,
      input: JSON.parse(i.arguments || "{}")
    })),
  ],
  stop_reason: mapCodexStatusToStopReason(response.status),
  stop_sequence: null,
  usage: {
    input_tokens: response.usage?.input_tokens || 0,
    output_tokens: response.usage?.output_tokens || 0,
  },
}
```

**Test strategy**:
- `anthropicToCodex`: fixtures de request Anthropic -> snapshot de Codex body. Cubrir:
  - Solo system string
  - System array
  - Mensaje user simple
  - Multi-turn con assistant text
  - Multi-turn con tool_use + tool_result
  - Con tools definidos
  - Con thinking param
  - Con imagenes
- `codexEventToAnthropic`: fixtures de eventos Codex -> array de eventos Anthropic
  esperados. Cubrir cada fila de la tabla de mapeo. Test de secuencia completa
  (start -> deltas -> stop) con state compartido.
- `codexNonStreamToAnthropic`: fixture de response no-stream -> body Anthropic.

---

### 4.4 `src/codex-handler.js` (NUEVO)

**Proposito**: Orquesta la peticion HTTP a Codex, manejo de reintentos, streaming SSE
hacia Claude Code, error handling.

**Public API**:

```javascript
// Entry point del modo Codex
// authHeader ya fue validado por parseCodexAuth
export async function handleCodexMessages(request, env, codexAuth): Promise<Response>
```

**Internals**:

```javascript
// Constantes
const MAX_RETRIES = 3;
const BASE_DELAY_MS = 1000;

// Parsea el body, hace la conversion, llama a Codex con retry
async function callCodex(codexBody, codexAuth, env, signal): Promise<Response>

// Construye el TransformStream que convierte Codex SSE -> Anthropic SSE
function createCodexToAnthropicStream(requestModel): TransformStream

// Backoff con respeto a Retry-After / Retry-After-Ms headers
async function sleep(ms): Promise<void>
```

**`handleCodexMessages` — flujo**:

```
1. Parsear body JSON del request.
   Si falla -> invalidRequestError("Invalid JSON body")
2. Validar body.model. Si falta -> invalidRequestError("Missing 'model' field")
3. Convertir body Anthropic -> Codex via anthropicToCodex(body)
4. Resolver URL: env.CODEX_BASE_URL || DEFAULT_CODEX_BASE_URL + /codex/responses
5. Generar request_id (UUID v4)
6. Construir headers con buildCodexHeaders(accessToken, accountId, requestId)
7. Loop de retry (max 3):
   a. fetch(POST url, {headers, body: JSON.stringify(codexBody)})
   b. Si response.ok -> break
   c. Si !isRetryableError -> break
   d. Parsear Retry-After-Ms / Retry-After
   e. sleep(delay)
8. Si !response.ok -> mapear error y devolver apiError(...)
9. Si body.stream === true:
   a. Crear TransformStream con createCodexToAnthropicStream(body.model)
   b. Pipear response.body -> decoder -> transformer
   c. Devolver Response con Content-Type: text/event-stream
10. Si body.stream === false:
    a. response.json()
    b. codexNonStreamToAnthropic(data, body.model)
    c. Devolver Response JSON
```

**`createCodexToAnthropicStream(requestModel)` — estructura**:

```javascript
return new TransformStream({
  start(controller) {
    // Inicializar state compartido con messageId
    state = { messageId: `msg_${Date.now()}`, model: requestModel, ... };
    // Emitir message_start
    controller.enqueue(encoder.encode(sseEvent("message_start", {
      type: "message_start",
      message: { id: state.messageId, type: "message", role: "assistant", model: requestModel, content: [] }
    })));
  },
  transform(chunk, controller) {
    // Decodificar chunk
    // Parsear lineas "data: ..."
    // Para cada evento JSON: codexEventToAnthropic(event, state) -> array de eventos
    // Enqueue cada evento Anthropic como sseEvent(...)
  },
  flush(controller) {
    // Si el stream cerro sin response.completed, emitir message_stop graceful
  }
});
```

**Parsing SSE** (en `transform`): el formato de Codex es identico al de OpenAI en general
(`data: {...}\n\n`). El parser es:

```javascript
function* parseSSE(buffer) {
  let idx = buffer.indexOf("\n\n");
  while (idx !== -1) {
    const chunk = buffer.slice(0, idx);
    buffer = buffer.slice(idx + 2);
    const lines = chunk.split("\n");
    for (const line of lines) {
      if (line.startsWith("data: ")) {
        const payload = line.slice(6).trim();
        if (payload === "[DONE]") continue;
        try { yield JSON.parse(payload); } catch {}
      }
    }
    idx = buffer.indexOf("\n\n");
  }
  return buffer;
}
```

**Error handling**:
- 401 de Codex: NO retry. Devolver `authError("Codex rejected the access token...")`.
- 429: retryable. Si despues de 3 intentos sigue 429, devolver `apiError("Rate limit reached", 429)`.
- 5xx: retryable. Despues de 3 intentos, devolver `apiError("Upstream error: ...", 502)`.
- 4xx (no 401, no 429): NO retry. Devolver `apiError("Upstream rejected request: ...", status)`.
- Network error: retryable. Despues de 3 intentos, devolver `apiError("Network error contacting Codex", 502)`.

**Test strategy**:
- Unit: `createCodexToAnthropicStream` con un mock ReadableStream que emite eventos Codex
  -> capturar el output stream y comparar con eventos Anthropic esperados.
- Integration: levantar un mock server Python que emite eventos Codex y verificar el
  response completo. Ver Section 6.

---

### 4.5 `src/index.js` (MODIFICADO, no eliminado)

**Cambios**:

1. Importar `handleCodexMessages` desde `codex-handler.js`.
2. (Opcional, recomendado) Importar `handleMessages` desde `opencode-handler.js` y moverlo.
3. Agregar funcion `routeMessages(request, env)` que decide entre Codex y OpenCode Go.
4. El default export queda casi igual, pero llama a `routeMessages`.

**Estructura final**:

```javascript
import { handleMessages } from "./opencode-handler.js";
import { handleCodexMessages } from "./codex-handler.js";
import { parseCodexAuth } from "./codex-utils.js";

async function routeMessages(request, env) {
  const auth = request.headers.get("Authorization") || "";
  if (auth.startsWith("codex:")) {
    const parsed = parseCodexAuth(auth);
    if (!parsed.ok) {
      return new Response(JSON.stringify({
        type: "error",
        error: { type: "invalid_request_error", message: parsed.error },
      }), { status: 400, headers: { "Content-Type": "application/json" } });
    }
    return handleCodexMessages(request, env, parsed);
  }
  return handleMessages(request, env);
}

// handleCountTokens y el default export se mantienen casi iguales
```

**Tamanos esperados**:
- `index.js`: ~80 lineas (vs 749 actual — la mayoria se mueve a `opencode-handler.js`)
- `opencode-handler.js`: ~670 lineas (codigo actual casi sin cambios)
- `codex-utils.js`: ~150 lineas
- `codex-protocol.js`: ~350 lineas
- `codex-handler.js`: ~250 lineas
- `shared-errors.js`: ~30 lineas

**Total**: ~1530 lineas, distribuidas en 6 archivos. Cada uno bajo 700 lineas.

---

## Section 5: Wrangler Config Changes

### Cambios a `wrangler.toml`

```toml
name = "opencode-go-proxy"  # Mantener nombre o cambiar a "claude-harness-proxy"
main = "src/index.js"
compatibility_date = "2026-06-16"

[vars]
OPENCODE_BASE_URL = "https://opencode.ai/zen/go/v1"
DEFAULT_MODEL = "minimax-m3"
# NUEVO: opcional, permite override para testing/staging
CODEX_BASE_URL = "https://chatgpt.com/backend-api"

[observability]
enabled = true
logs = { enabled = true, invocation_logs = true }
```

### NO se anade nada a `[secrets]`

Justificacion: el modo Codex es 100% stateless desde el punto de vista del Worker.
El `access_token` viene en el header `Authorization` de cada request, no se guarda en
ningun lado. Esto es **deliberado** y es una decision de seguridad:

- Si el Worker se redeploya, no se pierden credenciales.
- Si el log de Wrangler filtra una peticion, solo expone el token al servidor upstream
  (chatgpt.com), no a un almacenamiento permanente nuestro.
- El refresh de tokens lo hace el wrapper bash local, no el Worker.

**NO usar**:
- KV (prohibido por la regla "stateless")
- Durable Objects (idem)
- D1 (idem)
- Workers AI bindings (no aplica)

**NO agregar** a secrets:
- `CODEX_ACCESS_TOKEN` — viene en el header
- `CODEX_ACCOUNT_ID` — viene en el header
- `CODEX_CLIENT_ID` — no es responsabilidad del Worker (lo usa el wrapper bash para
  device auth)

### Comentarios a actualizar en `wrangler.toml`

Reemplazar el bloque de comentarios actual (lineas 13-19) por:

```toml
# Modos soportados:
#   - OpenCode Go (default): usa OPENCODE_BASE_URL + OPENCODE_API_KEY
#   - Codex: detectado cuando Authorization empieza con "codex:"
#     Header esperado: Authorization: codex:<access_token>:<chatgpt_account_id>
#     El Worker es stateless: no guarda credenciales. Refresca el token desde el lado
#     del cliente (scripts/claude-codex).
```

---

## Section 6: Test Strategy

### Estructura de directorios

```
worker/
├── src/                      # Codigo fuente
│   ├── index.js
│   ├── opencode-handler.js
│   ├── codex-handler.js
│   ├── codex-protocol.js
│   ├── codex-utils.js
│   └── shared-errors.js
├── test/
│   ├── unit/                 # Tests unitarios (puros, sin I/O)
│   │   ├── codex-utils.test.js
│   │   ├── codex-protocol.test.js
│   │   ├── shared-errors.test.js
│   │   └── opencode-handler.test.js   # (futuro, no prioritario)
│   ├── integration/          # Tests con mock servers
│   │   ├── mock-codex-server.py       # Mock SSE server en Python
│   │   ├── codex-handler.integration.test.js
│   │   └── opencode-handler.integration.test.js
│   ├── e2e/                  # Tests via wrangler dev
│   │   ├── codex-e2e.sh
│   │   └── opencode-e2e.sh
│   ├── fixtures/             # JSON fixtures
│   │   ├── anthropic/
│   │   │   ├── basic-request.json
│   │   │   ├── tool-use-request.json
│   │   │   └── multi-turn-request.json
│   │   ├── codex/
│   │   │   ├── basic-request.json
│   │   │   ├── sse-stream.ndjson
│   │   │   └── non-stream-response.json
│   │   └── anthropic-responses/
│   │       ├── basic-response.json
│   │       ├── sse-stream.ndjson
│   │       └── tool-use-response.json
│   └── helpers/
│       ├── sse-parser.js     # Utilidad para testear streams SSE
│       └── jwt-fixtures.js   # JWTs de prueba hardcodeados
├── package.json
└── wrangler.toml
```

### Tier 1: Unit tests (`test/unit/`)

**Framework**: `node --test` (built-in, sin dependencias) o Vitest si se quiere.
Recomendacion: `node --test` para minimizar dependencias del Worker.

**Cobertura**:

1. `codex-utils.test.js`:
   - `parseCodexAuth` con 10+ inputs (validos, malformados, prefijos incorrectos, tokens
     que no son JWT, account_ids malformados).
   - `extractAccountIdFromJwt` con un JWT firmado hardcodeado de prueba.
   - `buildCodexHeaders` snapshot test.
   - `mapCodexStatusToStopReason` table-driven.
   - `mapCodexHttpError` table-driven.
   - `isCodexRetryableError` table-driven.

2. `codex-protocol.test.js`:
   - `anthropicToCodex` con 8+ fixtures (cubriendo cada combinacion de content blocks).
   - `codexEventToAnthropic` con 20+ eventos individuales y 3+ secuencias completas.
   - `codexNonStreamToAnthropic` con 2-3 fixtures.

3. `shared-errors.test.js`:
   - Verifica que el shape del JSON es el esperado por Anthropic.
   - Verifica status codes.

**Comando**: `npm test` que ejecute `node --test test/unit/`

### Tier 2: Integration tests (`test/integration/`)

**Mock server Python** (`mock-codex-server.py`):

- Escucha en `http://localhost:8765`.
- Implementa 4 endpoints:
  - `POST /responses` (Codex-like): streamea eventos SSE de un fixture NDJSON.
  - `POST /responses-error-401`: devuelve 401 con body `{"error": {...}}`.
  - `POST /responses-error-429`: devuelve 429 con header `Retry-After-Ms`.
  - `POST /responses-slow`: streamea con delays entre eventos (test del parser parcial).
- Lee los fixtures desde `test/fixtures/codex/sse-stream.ndjson`.
- Logging minimo (solo request method, path, status).

**Tests** (`codex-handler.integration.test.js`):

- Levanta el mock server como subproceso (o asume que esta corriendo en
  `localhost:8765`).
- Hace `fetch("http://localhost:8787/v1/messages", {headers: {"Authorization": "codex:..."}, body: <anthropic>})`
  contra `wrangler dev` corriendo.
- Verifica el response (status, content-type, body parseado).

**Comando**: `npm run test:integration`:
```bash
#!/usr/bin/env bash
set -e
python3 test/integration/mock-codex-server.py &
MOCK_PID=$!
trap "kill $MOCK_PID" EXIT
sleep 1
npx wrangler dev --port 8787 &
WRANGLER_PID=$!
trap "kill $MOCK_PID $WRANGLER_PID" EXIT
sleep 5
node --test test/integration/*.test.js
```

### Tier 3: E2E tests (`test/e2e/`)

**`codex-e2e.sh`**:

- Asume que el usuario tiene credenciales reales de Codex en
  `~/.codex/auth.json`.
- Construye el header `codex:<access_token>:<account_id>`.
- Hace un POST pequeno a `https://opencode-go-proxy.r2gnqdy9c5.workers.dev/v1/messages`
  con `model: "gpt-5.5"` y un prompt trivial ("Say 'ok'").
- Verifica que el response es valido (status 200, content con "ok").
- Salida: human-readable, con tiempo de respuesta.

**`opencode-e2e.sh`**:

- Similar, pero sin el prefijo `codex:`. Verifica que el modo OpenCode Go sigue
  funcionando (regression test).

**Comando**: `npm run test:e2e` (manual, no en CI).

### CI Pipeline (recomendado, no parte del Cut 1)

```yaml
# .github/workflows/worker-ci.yml
name: Worker CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: 20 }
      - run: cd worker && npm ci
      - run: cd worker && npm test
      - run: cd worker && npm run test:integration
        env:
          # No se necesitan secrets para integration tests (mock server)
```

---

## Section 7: Risks & Edge Cases

### R1: El modo Codex rompe el modo OpenCode Go

**Riesgo**: Un bug en `routeMessages` o un import que falla hace que **todos** los
requests se rompan, no solo los de Codex.

**Mitigacion**:
- El routing se hace en una funcion pura (`routeMessages`) que solo inspecciona el header.
  Si `parseCodexAuth` falla, devuelve 400 **solo para requests Codex**, no afecta a los
  requests con Authorization regular.
- Los handlers de cada modo estan en archivos separados. Un crash en `codex-handler.js`
  solo afecta requests con prefijo `codex:`.
- Mantener `handleCountTokens` (stateless) sin cambios.
- Tests E2E separados para cada modo — si OpenCode E2E falla, sabemos que el routing
  esta roto, no el modo especifico.

**Rollback**:
- `wrangler rollback` al deploy anterior (un solo comando, restaura el bundle completo).
- El deploy anterior no tiene el routing — todos los requests van a `handleMessages`
  (OpenCode Go), que sigue intacto.
- Tiempo de rollback: < 30 segundos.

### R2: Access token malformado

**Casos**:
- Sin prefijo `codex:` -> cae a OpenCode Go (intencional, no es error).
- Prefijo `codex:` pero sin `<token>:<account_id>` -> 400 invalid_request_error.
- Token que no es JWT (no tiene 3 segmentos) -> 400 invalid_request_error.
- Token JWT pero sin el claim `chatgpt_account_id` -> 401 auth_error.
- account_id con formato raro -> 400 invalid_request_error.

**Mitigacion**:
- `parseCodexAuth` es defensivo: cada caso devuelve un error claro.
- Los errores **nunca** exponen el contenido del token en el response.
- Si el token es valido pero Codex devuelve 401, eso es un problema del lado del
  cliente (token expirado o revocado) y se devuelve tal cual al usuario.

### R3: Token aparece en logs

**Riesgo**: Wrangler captura request/response bodies para observability. El header
`Authorization` puede quedar en logs.

**Mitigacion**:
- Revisar `wrangler.toml` `[observability]`. La opcion `invocation_logs = true` puede
  incluir headers.
- Workaround: en `codex-handler.js`, antes de hacer fetch, **no loggear el header
  Authorization**. Agregar regla: nunca `console.log(request.headers)` ni
  `console.log(env)`.
- En el error handler, loggear solo `code` y `status`, nunca el body crudo de Codex
  (que puede contener el token en metadata).
- Audit final: despues de implementar, hacer un deploy de prueba, generar 1 request con
  token valido, descargar los logs de Cloudflare y verificar que el token NO aparece.

### R4: Codex cambia el formato del evento SSE

**Riesgo**: La API de Codex no es publica y puede cambiar. Un nuevo evento no manejado
puede crashear el stream.

**Mitigacion**:
- `codexEventToAnthropic` tiene un `default` que ignora eventos desconocidos (no emite
  nada, no falla).
- Solo `response.failed` y `error` son fatales — y estan envueltos en try/catch en el
  handler.
- Los tests deben incluir un evento sintetico desconocido para verificar que no rompe.

### R5: Streaming parcial (cliente desconecta a mitad)

**Riesgo**: Claude Code puede desconectar antes de recibir `message_stop`. El Worker
sigue streameando, desperdiciando CPU upstream.

**Mitigacion**:
- Cloudflare Workers automaticamente abortan el `fetch` upstream cuando el cliente
  desconecta. No se necesita codigo extra.
- Documentar esto en el handler para futuros mantenedores.

### R6: `body.stream === false` (non-streaming)

**Riesgo**: Claude Code casi siempre usa streaming, pero si alguien lo desactiva, el
camino non-stream debe funcionar.

**Mitigacion**:
- `codexNonStreamToAnthropic` se implementa desde el inicio (no como TODO).
- Test dedicado en `codex-protocol.test.js`.

### R7: Codigo nuevo aumenta el bundle > 1MB

**Mitigacion**:
- Medir el bundle despues de implementar: `npx wrangler deploy --dry-run --outdir=dist`.
- Estimado: ~30KB adicionales, holgura amplia.
- Si se pasa: comprimir assets, eliminar regex innecesarios, mover fixtures a build
  time.

### R8: Worker hace doble parseo del body

**Riesgo**: `request.json()` solo se puede llamar una vez. Si `routeMessages` lo lee
y luego `handleCodexMessages` lo lee de nuevo, falla.

**Mitigacion**:
- El body **no** se lee en `routeMessages`. Solo se inspecciona el header.
- `handleCodexMessages` lee el body una vez al inicio. Lo pasa como argumento a
  funciones internas si es necesario.

### R9: El handler de Codex se cuelga en una llamada upstream

**Riesgo**: `fetch` a Codex puede colgarse si el server no responde. Cloudflare Workers
tiene un timeout de 30s (CPU time) en el plan free, 5min en el de pago.

**Mitigacion**:
- Cloudflare cancela automaticamente fetch salientes que pasan el timeout. El handler
  recibe un error de tipo "fetch failed".
- Envolver el `fetch` en `AbortController` con `setTimeout` no es necesario (Cloudflare
  lo maneja), pero se puede agregar `signal: AbortSignal.timeout(120000)` (2 min) como
  salvaguarda explicita.

### R10: Conversion Anthropic -> Codex pierde informacion

**Riesgo**: Campos de Anthropic que Codex no soporta (e.g. `metadata.user_id`, `stop_sequences`)
se ignoran silenciosamente. El usuario puede sorprenderse.

**Mitigacion**:
- Documentar en el README del Worker que campos no soportados se ignoran.
- Opcional: en cut 2, agregar `console.warn` para campos ignorados. Por ahora, no
  agregar warn para no spammear logs.

---

## Anexo: Open Questions para resolver antes de implementar

1. **Modelo default**: si el request Anthropic no incluye `model`, ¿que modelo Codex usamos?
   Sugerencia: `gpt-5.5` o leer de `env.CODEX_DEFAULT_MODEL`.

2. **Soporte de `system` como array**: Anthropic permite `system: [{type: "text", text: "..."}]`.
   Codex espera `instructions` como string. Hay que concatenar los bloques de text.
   Confirmado en la conversion, pero ¿concatenamos con `\n` o `\n\n`?

3. **Thinking blocks de Anthropic en input**: si el cliente (Claude Code) envia un
   `messages[].content[].type === "thinking"` en un assistant turn, ¿lo enviamos a Codex
   o lo descartamos? Sugerencia: **descartar** — Codex no entiende signatures
   de Anthropic.

4. **Tool result con imagenes**: Anthropic permite `tool_result.content` ser array con
   bloques `type: "image"`. Codex Responses API permite `function_call_output.output`
   ser un array con `input_image`. ¿Lo soportamos en cut 1 o solo string?
   Sugerencia: cut 1 solo string, cut 2 imagenes.

5. **`max_tokens` de Anthropic**: ignoramos en cut 1. Si el usuario lo pasa y el modelo
   tiene un limite inferior, no avisamos. ¿OK? Sugerencia: OK para cut 1.

6. **Soporte de WebSocket**: PLAN_CODEX.md dice "no en cut 1". Confirmado, pero ¿hay
   algun cliente que lo necesite? Sugerencia: no, todos los clientes Anthropic-compatibles
   usan SSE.

7. **Manejo de `temperature` con reasoning**: Codex ignora `temperature` cuando
   `reasoning.effort` esta seteado. ¿Lo seteamos siempre o solo si el cliente lo pide?
   Sugerencia: solo si el cliente lo pide, default a no incluirlo.

8. **Logging**: ¿que nivel de logging queremos en produccion? Sugerencia: `console.error`
   solo para errores fatales, `console.log` solo para metricas (latencia, bytes
   streameados). Nunca request/response bodies.
