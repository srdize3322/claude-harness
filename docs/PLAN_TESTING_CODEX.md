# Plan: Testing del modo Codex en el Worker de claude-harness

> Documento de diseno. Define como se valida el modo Codex (Anthropic -> OpenAI Responses API en `chatgpt.com/backend-api/codex/responses`) en sus cuatro niveles: unit, integracion contra un mock backend, E2E con auth real, y regresion del modo OpenCode Go.

## Contexto

- Worker: `worker/src/index.js` (749 lineas). Hoy solo implementa modo OpenCode Go (`anthropicToOpenAI`, `openAIToAnthropic`, `transformStream`).
- Modo Codex (a implementar, ver `docs/PLAN_CODEX.md`) agrega:
  - `worker/src/codex-protocol.js` (NUEVO) — conversion Anthropic <-> OpenAI Responses.
  - `worker/src/codex-handler.js` (NUEVO) — `handleCodexMessages` + `transformCodexStream`.
  - Routing en `index.js`: si el header `Authorization` (o `x-api-key`) empieza con `codex:<token>:<account_id>`, derivar a `handleCodexMessages`. Si no, mantener el flujo actual.
- Token: `ANTHROPIC_AUTH_TOKEN="codex:<access_token>:<chatgpt_account_id>"`. El Worker NO guarda tokens.
- Entorno: macOS, Node v26, Python 3.14, `wrangler` instalado en `worker/`, `codex` CLI presente, `~/.codex/auth.json` valido.

---

## 0. Decision sobre framework de tests

**Recomendacion: `node:test` + `node:assert` (vanilla, sin dependencias).**

Razones:

1. **Cero dependencias nuevas.** Node 26 trae `node:test` estable, no hay que tocar `package.json`. El Worker ya solo depende de `wrangler` (devDep). Agregar `vitest`/`jest` sumaria 50+ MB y un paso de build que no necesitamos.
2. **Mismo runtime que el Worker.** `node:test` corre en el mismo V8 que `workerd` (motor del Worker). No hay riesgo de mocks que no aplican a Cloudflare.
3. **Streaming nativo.** `TransformStream`, `TextEncoder`/`TextDecoder`, `fetch`, `Response` — todo lo que usa el handler esta disponible. Se puede instanciar `transformCodexStream(...)` en el test y consumir su salida leyendo del `ReadableStream` resultante.
4. **Ejecutable directo con `node --test`.** Compatible con `wrangler dev` (mismo proceso V8).

Comando base:
```bash
cd REPO/worker
node --test test/
```

Estructura de archivos a crear:

```
worker/
  test/
    protocol.test.js          # tests de codex-protocol.js (puro)
    handler.test.js           # tests de codex-handler.js (puro)
    stream.test.js            # tests de transformCodexStream
    fixtures/
      anthropic-request.json
      codex-response-sse.txt
```

Anadir al `package.json`:
```json
"scripts": {
  "test": "node --test test/",
  "test:watch": "node --test --watch test/"
}
```

No hace falta instalar nada. `node --test` ya esta.

---

## Section 1: Unit Test Plan

Las funciones puras son las candidatas principales. `codex-protocol.js` y `codex-handler.js` deben disenarse para que las logicas testeables sean **funciones puras exportadas**, no closures internas. Sugerencia de API interna:

```js
// codex-protocol.js
export function anthropicToCodexResponses(body) { ... }     // puro
export function codexResponsesToAnthropic(event) { ... }   // un evento -> 0..N eventos Anthropic
export function mapStopReason(codexReason) { ... }         // puro
export function codexHeaders(token, accountId) { ... }     // puro

// codex-handler.js
export function transformCodexStream(body, model) { ... }   // retorna TransformStream
```

### 1.1 `anthropicToCodexResponses(body)`

Traduce un body Anthropic (`{model, messages, system, tools, ...}`) al payload que espera `/codex/responses` de OpenAI.

| # | Test | Input | Expected |
|---|------|-------|----------|
| 1.1.1 | `system: string -> instructions string` | `{system: "You are helpful", messages: [{role:"user",content:"hi"}]}` | `payload.instructions === "You are helpful"` |
| 1.1.2 | `system: array -> join text blocks` | `system: [{type:"text",text:"a"},{type:"text",text:"b"}]` | `payload.instructions === "a\nb"` |
| 1.1.3 | `messages: user string -> input item` | `{messages:[{role:"user",content:"hola"}]}` | `payload.input[0] === {type:"message", role:"user", content:[{type:"input_text", text:"hola"}]}` |
| 1.1.4 | `messages: assistant text + tool_use -> function_call items` | `messages:[{role:"assistant", content:[{type:"text",text:"pensando"},{type:"tool_use", id:"x", name:"f", input:{a:1}}]}]` | dos items en `input`: `message` con `output_text` y `function_call` con `call_id="x"`, `name="f"`, `arguments='{"a":1}'` |
| 1.1.5 | `messages: tool_result -> function_call_output` | `messages:[{role:"user", content:[{type:"tool_result", tool_use_id:"x", content:"ok"}]}]` | `payload.input[0].type === "function_call_output"`, `output === "ok"`, `call_id === "x"` |
| 1.1.6 | `messages: assistant prev round` | mezcla de assistant+tool_result en orden | orden preservado en `payload.input` |
| 1.1.7 | `image block -> input_image part` | `content:[{type:"image", source:{media_type:"image/png", data:"<base64>"}}]` | `content:[{type:"input_image", source:{type:"base64", media_type:"image/png", data:"<base64>"}}]` |
| 1.1.8 | `tools -> tools array` | `tools:[{name:"f", description:"d", input_schema:{...}}]` | `payload.tools === [{type:"function", name:"f", description:"d", parameters:{...}}]` |
| 1.1.9 | `max_tokens -> max_output_tokens` | `body.max_tokens = 1024` | `payload.max_output_tokens === 1024` |
| 1.1.10 | `temperature se copia` | `body.temperature = 0.3` | `payload.temperature === 0.3` |
| 1.1.11 | `stream: true` | `body.stream = true` | `payload.stream === true` |
| 1.1.12 | `model default` | sin `body.model` | `payload.model === "gpt-5.4"` (o el default configurado) |
| 1.1.13 | `thinking: enabled -> reasoning_effort` | `thinking:{type:"enabled", budget_tokens:8000}` | `payload.reasoning = {effort: "high"}` o `medium` segun el mapa |
| 1.1.14 | `thinking: disabled` | `thinking:{type:"disabled"}` | `payload.reasoning === undefined` |
| 1.1.15 | `output_config.effort: "off"` | `output_config:{effort:"off"}` | `payload.reasoning === undefined` |
| 1.1.16 | `body vacio` | `{}` | no throw, devuelve objeto con defaults razonables |
| 1.1.17 | `system: array con bloques no-text` | `system:[{type:"text",text:"x"},{type:"something_else"}]` | filtra los no-text, devuelve `"x"` |
| 1.1.18 | `tool_use sin name` | `{type:"tool_use", id:"x", input:{}}` | `function_call.name === ""` (no throw) |
| 1.1.19 | `tool_result content array` | `{type:"tool_result", tool_use_id:"x", content:[{type:"text",text:"a"},{type:"text",text:"b"}]}` | `output === "a\nb"` |
| 1.1.20 | `prev_round mixing message+tool_call` | conversacion larga de 3 turnos | snapshot completo contra fixture `anthropic-multiturn.json` |

### 1.2 `codexResponsesToAnthropic(event)`

Convierte UN evento SSE de Codex (`response.created`, `response.output_item.added`, `response.output_text.delta`, `response.function_call_arguments.delta`, `response.completed`, `response.error`) en 0..N eventos Anthropic (`message_start`, `content_block_start`, `content_block_delta`, `content_block_stop`, `message_delta`, `message_stop`, `error`).

| # | Test | Input | Expected |
|---|------|-------|----------|
| 1.2.1 | `response.created` | `{type:"response.created", response:{id:"resp_x", model:"gpt-5.4"}}` | un evento: `message_start` con `message.id="resp_x"`, `message.model="gpt-5.4"`, `message.role="assistant"` |
| 1.2.2 | `response.output_item.added` (text) | `{type:"response.output_item.added", output_index:0, item:{type:"message", content:[]}}` | `content_block_start` con `index:0`, `content_block.type="text"` |
| 1.2.3 | `response.output_item.added` (function_call) | `{type:"response.output_item.added", output_index:1, item:{type:"function_call", call_id:"fc_1", name:"get_weather"}}` | `content_block_start` con `index:1`, `content_block.type="tool_use"`, `id="fc_1"`, `name="get_weather"`, `input={}` |
| 1.2.4 | `response.output_item.added` (reasoning) | `{type:"response.output_item.added", output_index:0, item:{type:"reasoning", summary:[]}}` | `content_block_start` con `index:0`, `content_block.type="thinking"` |
| 1.2.5 | `response.output_text.delta` | `{type:"response.output_text.delta", output_index:0, delta:"hola "}` | `content_block_delta` con `index:0`, `delta.type="text_delta"`, `delta.text="hola "` |
| 1.2.6 | `response.function_call_arguments.delta` | `{type:"response.function_call_arguments.delta", output_index:1, delta:'{"city":'}` | `content_block_delta` con `index:1`, `delta.type="input_json_delta"`, `delta.partial_json='{"city":'` |
| 1.2.7 | `response.reasoning_summary_text.delta` | `{type:"response.reasoning_summary_text.delta", output_index:0, delta:"pensando..."}` | `content_block_delta` con `index:0`, `delta.type="thinking_delta"`, `delta.thinking="pensando..."` |
| 1.2.8 | `response.output_item.done` (text) | `{type:"response.output_item.done", output_index:0, item:{type:"message", content:[{type:"output_text", text:"hola"}]}}` | `content_block_stop` con `index:0` |
| 1.2.9 | `response.output_item.done` (function_call) | `{type:"response.output_item.done", output_index:1, item:{type:"function_call", call_id:"fc_1", name:"f", arguments:'{"a":1}'}}` | `content_block_stop` con `index:1` |
| 1.2.10 | `response.completed` con `finish_reason="stop"` | `{type:"response.completed", response:{status:"completed", output:[...], usage:{input_tokens:10, output_tokens:5}}}` | un evento `message_delta` con `delta.stop_reason="end_turn"`, `usage.output_tokens=5`; luego `message_stop` |
| 1.2.11 | `response.completed` con `max_output_tokens` | `incomplete_details:{reason:"max_output_tokens"}` | `message_delta` con `stop_reason="max_tokens"` |
| 1.2.12 | `response.error` | `{type:"response.error", error:{code:"rate_limit_error", message:"429"}}` | un evento `error` con `type="api_error"`, `message="429"` (error event, no se acumula a message) |
| 1.2.13 | Evento desconocido | `{type:"response.audio.delta", delta:"x"}` | 0 eventos (ignorado) |
| 1.2.14 | Payload malformado | `{type:"response.created"}` (sin `response.id`) | 0 eventos o `error` event con `message="malformed event"` (definir uno) |
| 1.2.15 | Mismo `output_index` en items consecutivos | dos `output_item.added` con index=0,1 | indices consistentes 0,1, sin colision |

### 1.3 `mapStopReason(codexReason)`

| # | Test | Input | Expected |
|---|------|-------|----------|
| 1.3.1 | `stop` | `"stop"` | `"end_turn"` |
| 1.3.2 | `tool_calls` / `function_call` | `"tool_calls"` | `"tool_use"` |
| 1.3.3 | `length` | `"length"` | `"max_tokens"` |
| 1.3.4 | `content_filter` | `"content_filter"` | `"end_turn"` |
| 1.3.5 | undefined / null | `null` | `"end_turn"` (default seguro) |
| 1.3.6 | string desconocida | `"weird_reason"` | `"end_turn"` |

### 1.4 `codexHeaders(token, accountId)`

| # | Test | Input | Expected |
|---|------|-------|----------|
| 1.4.1 | Caso normal | `token="abc", accountId="acc_1"` | `{Authorization:"Bearer abc", "chatgpt-account-id":"acc_1", "Content-Type":"application/json", "OpenAI-Beta":"responses=experimental"}` |
| 1.4.2 | Token vacio | `token=""` | lanza `TypeError` o retorna objeto invalido (definir contrato) |
| 1.4.3 | No leak de token en errores | input con token especial | assertion: el token NUNCA aparece en logs/console del test (grep sobre stderr) |
| 1.4.4 | Headers opcionales | con `accept="text/event-stream"` | incluye `Accept: text/event-stream` |

### 1.5 `transformCodexStream(body, model)`

El test mas importante: validar el flujo de eventos Anthropic emitidos.

| # | Test | Input | Expected |
|---|------|-------|----------|
| 1.5.1 | Stream simple de texto | upstream emite `created -> output_item.added(text) -> output_text.delta("a") -> output_text.delta("b") -> output_item.done -> completed` | secuencia exacta: `message_start -> content_block_start(text,0) -> content_block_delta(text_delta,"a") -> content_block_delta(text_delta,"b") -> content_block_stop(0) -> message_delta(end_turn) -> message_stop` |
| 1.5.2 | Stream con tool_call | emite `function_call` item con deltas de argumentos | `content_block_start(tool_use,0) -> content_block_delta(input_json_delta,'{...}') -> content_block_stop(0) -> message_delta(tool_use) -> message_stop` |
| 1.5.3 | Stream con reasoning | reasoning item + texto | dos bloques: `thinking` (0) y `text` (1), indices 0 y 1, sin colision |
| 1.5.4 | Stream truncado (sin `completed`) | upstream cierra la conexion a mitad | `flush()` emite `content_block_stop` pendientes + `message_delta(end_turn)` + `message_stop` (no debe colgar el Worker) |
| 1.5.5 | Stream con `response.error` a mitad | emite `response.error` con code=401 | un evento `error` con `type="api_error"`, status code del Response final = 401 |
| 1.5.6 | Evento malformado (JSON invalido) | upstream envia `data: {malformed` | se ignora la linea, no crashea el TransformStream |
| 1.5.7 | `data: [DONE]` | upstream cierra con `[DONE]` | no se emite nada extra (DONE es terminator, no evento) |
| 1.5.8 | `message.id` consistente | multiples eventos | todos los `content_block_*` referencian el `id` de `message_start` |
| 1.5.9 | `model` propagation | upstream no envia `model` en `response.created` | `message.model` usa el parametro pasado a `transformCodexStream` |
| 1.5.10 | `output_index` discontinuo (0, 5) | upstream salta indices | se preserva el `output_index` recibido (Claude Code lo espera) |

#### Helper para tests 1.5.x

```js
async function streamFromEvents(events) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      for (const ev of events) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(ev)}\n\n`));
      }
      controller.enqueue(encoder.encode("data: [DONE]\n\n"));
      controller.close();
    },
  });
  return stream;
}

async function collectAnthropicEvents(stream) {
  const decoder = new TextDecoder();
  const reader = stream.getReader();
  let buf = "";
  const events = [];
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const chunks = buf.split("\n\n");
    buf = chunks.pop();
    for (const c of chunks) {
      const ev = {};
      for (const line of c.split("\n")) {
        if (line.startsWith("event: ")) ev.event = line.slice(7);
        else if (line.startsWith("data: ")) ev.data = line.slice(6);
      }
      if (ev.data) {
        try { ev.data = JSON.parse(ev.data); } catch {}
      }
      events.push(ev);
    }
  }
  return events;
}
```

### 1.6 Routing en `index.js` (puede testearse como integracion)

| # | Test | Input | Expected |
|---|------|-------|----------|
| 1.6.1 | Header `codex:` | request con `Authorization: Bearer codex:abc:acc_1` | se llama a `handleCodexMessages`, NO a `handleMessages` |
| 1.6.2 | Header sin `codex:` | request con `Authorization: Bearer sk-xyz` | se llama a `handleMessages` (modo OpenCode Go) |
| 1.6.3 | Sin header | sin `Authorization` | modo default OpenCode Go (comportamiento actual) |
| 1.6.4 | `x-api-key: codex:...` | header alterno Anthropic | mismo enrutamiento que 1.6.1 |
| 1.6.5 | `count_tokens` con modo codex | POST `/v1/messages/count_tokens` con header `codex:` | sigue yendo a `handleCountTokens` (no se traduce a Codex) |

> Estos tests se pueden hacer con `unstable_dev` de `wrangler` o llamando directamente a la funcion `default.fetch` con un `Request` mockeado y un `env` falso. Mas detalle en Section 3.

---

## Section 2: Mock Backend Server

### 2.1 Por que un mock

Codex API es **no documentada y rate-limited**. Para tests de integracion reproducibles y CI-friendly, NO se puede pegar contra `chatgpt.com/backend-api/codex/responses` real. Se necesita un mock que:

- Imite el formato SSE exacto (mismos nombres de eventos, mismos campos).
- Soporte varios modos (echo, error, stream interrupt) para ejercitar todas las ramas del handler.
- Loguee todo lo recibido (headers + body) para debugging.
- Sea trivial de levantar localmente con `python3` (sin `pip install`).

### 2.2 Stack

**Solo stdlib: `http.server` + `threading` + `sse-stdlib-no, hecho a mano`.**

- Sin dependencias externas (CI, sandbox, otros dev se levantan en 1 segundo).
- Threaded para manejar multiples requests concurrentes.
- Puerto 9999 (acordado en el plan).

### 2.3 Script completo

Archivo: `worker/test/mock_codex_backend.py`

```python
#!/usr/bin/env python3
"""
Mock del backend Codex para tests de integracion.

Escucha en 127.0.0.1:9999. Imita la respuesta SSE de
chatgpt.com/backend-api/codex/responses con varios modos configurables
por header o query param.

Modos:
  echo      -> repite la pregunta del usuario en el text del response
  error     -> emite response.error con el code solicitado
  interrupt -> cierra la conexion a mitad del stream
  slow      -> emite eventos con 200ms entre cada uno (para tests de timeout)
  tool_call -> emite un function_call de ejemplo

Logs:
  Cada request se loguea a stdout con timestamp, headers (sin Authorization
  completa, solo "Bearer <primeros 4>..."), body, y modo activo.

Uso:
  python3 mock_codex_backend.py [--port 9999] [--default-mode echo]
"""

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


# --- Configuracion -----------------------------------------------------------

DEFAULT_PORT = 9999
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mock-logs")


# --- SSE helpers -------------------------------------------------------------

def sse_event(event_type: str, data: dict) -> bytes:
    """Serializa un evento SSE al formato 'event: T\\ndata: {...}\\n\\n'."""
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def sse_done() -> bytes:
    return b"data: [DONE]\n\n"


# --- Generadores de respuesta ------------------------------------------------

def make_response_created(resp_id: str, model: str = "gpt-5.4") -> dict:
    return {
        "type": "response.created",
        "response": {
            "id": resp_id,
            "object": "response",
            "status": "in_progress",
            "model": model,
            "created_at": int(time.time()),
        },
    }


def make_text_item_added(index: int) -> dict:
    return {
        "type": "response.output_item.added",
        "output_index": index,
        "item": {"type": "message", "role": "assistant", "content": []},
    }


def make_text_delta(index: int, text: str) -> dict:
    return {
        "type": "response.output_text.delta",
        "output_index": index,
        "delta": text,
    }


def make_text_item_done(index: int, full_text: str) -> dict:
    return {
        "type": "response.output_item.done",
        "output_index": index,
        "item": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": full_text, "annotations": []}],
        },
    }


def make_function_call_added(index: int, call_id: str, name: str) -> dict:
    return {
        "type": "response.output_item.added",
        "output_index": index,
        "item": {
            "type": "function_call",
            "id": call_id,
            "call_id": call_id,
            "name": name,
            "arguments": "",
        },
    }


def make_function_call_args_delta(index: int, delta: str) -> dict:
    return {
        "type": "response.function_call_arguments.delta",
        "output_index": index,
        "delta": delta,
    }


def make_function_call_done(index: int, call_id: str, name: str, args: str) -> dict:
    return {
        "type": "response.output_item.done",
        "output_index": index,
        "item": {
            "type": "function_call",
            "id": call_id,
            "call_id": call_id,
            "name": name,
            "arguments": args,
        },
    }


def make_response_completed(resp_id: str, full_text: str, in_tokens: int = 0, out_tokens: int = 0,
                            finish_reason: str = "stop", status: str = "completed") -> dict:
    return {
        "type": "response.completed",
        "response": {
            "id": resp_id,
            "object": "response",
            "status": status,
            "model": "gpt-5.4",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": full_text, "annotations": []}],
                }
            ],
            "usage": {
                "input_tokens": in_tokens,
                "output_tokens": out_tokens,
                "total_tokens": in_tokens + out_tokens,
            },
            "incomplete_details": None if status == "completed" else {"reason": finish_reason},
        },
    }


def make_error(code: str, message: str) -> dict:
    return {
        "type": "response.error",
        "error": {"code": code, "message": message, "type": code},
    }


# --- Extraccion del ultimo user message (para echo) --------------------------

def extract_user_prompt(body: dict) -> str:
    """Extrae el texto del ultimo mensaje user del body Codex Responses."""
    items = body.get("input", [])
    last_user = ""
    for item in items:
        if item.get("type") == "message" and item.get("role") == "user":
            content = item.get("content", [])
            parts = []
            for p in content:
                if p.get("type") == "input_text":
                    parts.append(p.get("text", ""))
            if parts:
                last_user = "\n".join(parts)
    return last_user or "(empty prompt)"


# --- Handlers por modo -------------------------------------------------------

def write_sse(handler: BaseHTTPRequestHandler, chunks: list) -> None:
    """Escribe una lista de chunks SSE al socket."""
    for chunk in chunks:
        try:
            handler.wfile.write(chunk)
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def handle_echo(handler, body, log):
    resp_id = f"resp_mock_{int(time.time() * 1000)}"
    user_text = extract_user_prompt(body)
    reply = f"Echo: {user_text}"
    chunks = [
        sse_event("response.created", make_response_created(resp_id)),
        sse_event("response.output_item.added", make_text_item_added(0)),
        sse_event("response.output_text.delta", make_text_delta(0, reply)),
        sse_event("response.output_item.done", make_text_item_done(0, reply)),
        sse_event("response.completed", make_response_completed(
            resp_id, reply, in_tokens=len(user_text) // 4, out_tokens=len(reply) // 4,
        )),
        sse_done(),
    ]
    log("echo", f"replied with {len(reply)} chars")
    write_sse(handler, chunks)


def handle_tool_call(handler, body, log):
    resp_id = f"resp_mock_{int(time.time() * 1000)}"
    args = '{"city": "Santiago"}'
    chunks = [
        sse_event("response.created", make_response_created(resp_id)),
        sse_event("response.output_item.added", make_function_call_added(0, "fc_mock_1", "get_weather")),
        sse_event("response.function_call_arguments.delta", make_function_call_args_delta(0, args)),
        sse_event("response.output_item.done", make_function_call_done(0, "fc_mock_1", "get_weather", args)),
        sse_event("response.completed", make_response_completed(
            resp_id, "", in_tokens=10, out_tokens=10, finish_reason="tool_calls", status="completed",
        )),
        sse_done(),
    ]
    log("tool_call", "emitted get_weather with city=Santiago")
    write_sse(handler, chunks)


def handle_error(handler, body, log, code="rate_limit_error", message="Rate limit hit"):
    resp_id = f"resp_mock_{int(time.time() * 1000)}"
    chunks = [
        sse_event("response.created", make_response_created(resp_id)),
        sse_event("response.error", make_error(code, message)),
        sse_done(),
    ]
    log("error", f"emitted error code={code}")
    write_sse(handler, chunks)


def handle_interrupt(handler, body, log):
    resp_id = f"resp_mock_{int(time.time() * 1000)}"
    chunks = [
        sse_event("response.created", make_response_created(resp_id)),
        sse_event("response.output_item.added", make_text_item_added(0)),
        sse_event("response.output_text.delta", make_text_delta(0, "Empezando a res")),
    ]
    log("interrupt", "wrote 3 events, closing")
    write_sse(handler, chunks)
    # Cierre abrupto sin completed
    try:
        handler.wfile.close()
    except Exception:
        pass


def handle_slow(handler, body, log, delay=0.2):
    resp_id = f"resp_mock_{int(time.time() * 1000)}"
    user_text = extract_user_prompt(body)
    reply = f"Slow reply to: {user_text}"
    chunks = [
        sse_event("response.created", make_response_created(resp_id)),
    ]
    write_sse(handler, chunks)
    time.sleep(delay)
    chunks = [sse_event("response.output_item.added", make_text_item_added(0))]
    write_sse(handler, chunks)
    for ch in reply:
        time.sleep(delay / 4)
        write_sse(handler, [sse_event("response.output_text.delta", make_text_delta(0, ch))])
    time.sleep(delay)
    chunks = [
        sse_event("response.output_item.done", make_text_item_done(0, reply)),
        sse_event("response.completed", make_response_completed(resp_id, reply)),
        sse_done(),
    ]
    write_sse(handler, chunks)
    log("slow", f"replied over {len(reply) * delay / 4:.1f}s")


MODE_HANDLERS = {
    "echo": handle_echo,
    "tool_call": handle_tool_call,
    "error": handle_error,
    "interrupt": handle_interrupt,
    "slow": handle_slow,
}


# --- Request logging ---------------------------------------------------------

def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)


def log_request(log_path: str, headers: dict, body_bytes: bytes, body_obj, mode: str) -> None:
    """Loguea la request. IMPORTANTE: redacta Authorization antes de escribir."""
    redacted_headers = {}
    for k, v in headers.items():
        if k.lower() == "authorization":
            # Nunca loguear el token completo. Solo primeros 8 chars del scheme.
            parts = v.split(" ", 1)
            if len(parts) == 2:
                token = parts[1]
                if len(token) > 8:
                    redacted_headers[k] = f"{parts[0]} {token[:8]}...<redacted>"
                else:
                    redacted_headers[k] = f"{parts[0]} <redacted>"
            else:
                redacted_headers[k] = "<redacted>"
        else:
            redacted_headers[k] = v

    entry = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "headers": redacted_headers,
        "body": body_obj,
        "mode": mode,
        "body_bytes": len(body_bytes),
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# --- HTTP server -------------------------------------------------------------

class MockCodexHandler(BaseHTTPRequestHandler):
    default_mode = "echo"
    error_code = "rate_limit_error"
    error_message = "Rate limit hit"

    def log_message(self, format, *args):
        # Silenciar el log estandar de BaseHTTPRequestHandler; usamos el nuestro.
        return

    def do_POST(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        mode = params.get("mode", [self.default_mode])[0]
        if "error_code" in params:
            self.error_code = params["error_code"][0]
        if "error_message" in params:
            self.error_message = params["error_message"][0]

        # Leer body
        content_length = int(self.headers.get("Content-Length", "0"))
        body_bytes = self.rfile.read(content_length) if content_length > 0 else b""
        try:
            body_obj = json.loads(body_bytes) if body_bytes else {}
        except json.JSONDecodeError:
            body_obj = {"_raw": body_bytes[:200].decode("utf-8", errors="replace")}

        # Log
        log_path = os.path.join(LOG_DIR, "requests.jsonl")
        log_request(log_path, dict(self.headers), body_bytes, body_obj, mode)

        # Responder como SSE
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Mock-Mode", mode)
        self.end_headers()

        handler_fn = MODE_HANDLERS.get(mode)
        if handler_fn is None:
            self.wfile.write(sse_event("response.error", make_error(
                "invalid_request_error", f"Unknown mock mode: {mode}"
            )))
            self.wfile.write(sse_done())
            return

        try:
            if mode == "error":
                handler_fn(self, body_obj, lambda m, d: None, code=self.error_code, message=self.error_message)
            else:
                handler_fn(self, body_obj, lambda m, d: None)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
            return
        self.send_response(404)
        self.end_headers()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--default-mode", choices=MODE_HANDLERS.keys(), default="echo")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    setup_logging()
    MockCodexHandler.default_mode = args.default_mode

    server = ThreadingHTTPServer((args.host, args.port), MockCodexHandler)
    print(f"[mock-codex] listening on http://{args.host}:{args.port}", flush=True)
    print(f"[mock-codex] default mode: {args.default_mode}", flush=True)
    print(f"[mock-codex] logs: {LOG_DIR}/requests.jsonl", flush=True)
    print(f"[mock-codex] health: GET /health", flush=True)
    print(f"[mock-codex] modes: {', '.join(MODE_HANDLERS.keys())}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[mock-codex] shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
```

### 2.4 Como se usa

Levantar el mock:
```bash
cd REPO/worker
python3 test/mock_codex_backend.py --port 9999 --default-mode echo
```

Probarlo directamente (sin Worker):
```bash
curl -N -X POST http://127.0.0.1:9999/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer test_token" \
  -d '{"model":"gpt-5.4","input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"hola"}]}]}'

# Salida esperada:
# event: response.created
# data: {"type":"response.created",...}
# event: response.output_item.added
# data: {...}
# event: response.output_text.delta
# data: {"type":"response.output_text.delta","delta":"Echo: hola"}
# ...
# data: [DONE]
```

Cambiar modo via query string:
```bash
# Test de error
curl -N -X POST "http://127.0.0.1:9999/v1/responses?mode=error&error_code=401&error_message=Invalid+token" ...

# Test de tool_call
curl -N -X POST "http://127.0.0.1:9999/v1/responses?mode=tool_call" ...

# Test de interrupcion
curl -N -X POST "http://127.0.0.1:9999/v1/responses?mode=interrupt" ...
```

Ver los logs:
```bash
tail -f REPO/worker/test/mock-logs/requests.jsonl | jq .
```

---

## Section 3: Integration Tests

### 3.1 Setup

El Worker necesita una variable de entorno apuntando al mock. El `codex-handler.js` debe leer `env.CODEX_BASE_URL` con default `https://chatgpt.com/backend-api`. En `wrangler.toml` no se setea esa var (el default es el real). En `.dev.vars` o en la linea de comando de `wrangler dev` se setea el override.

Crear `worker/.dev.vars` (gitignored):
```bash
# REPO/worker/.dev.vars
OPENCODE_API_KEY=test-opencode-key
CODEX_BASE_URL=http://127.0.0.1:9999
```

> **IMPORTANTE:** `.dev.vars` debe estar en `.gitignore`. Verificar que ya lo esta:
> ```bash
> cat .gitignore
> ```
> Si no lo esta, agregar `.dev.vars`.

Alternativa sin tocar archivos, pasando `--var`:
```bash
cd REPO/worker
npx wrangler dev --var "CODEX_BASE_URL=http://127.0.0.1:9999" --var "OPENCODE_API_KEY=test"
```

### 3.2 Pasos

**Terminal 1 — Mock backend:**
```bash
cd REPO/worker
python3 test/mock_codex_backend.py --port 9999 --default-mode echo
# Output esperado:
# [mock-codex] listening on http://127.0.0.1:9999
# [mock-codex] default mode: echo
# [mock-codex] logs: .../test/mock-logs/requests.jsonl
```

**Terminal 2 — Wrangler dev:**
```bash
cd REPO/worker
npx wrangler dev --port 8787
# Output esperado:
# ⎔ Starting local server on http://127.0.0.1:8787
```

**Terminal 3 — Tests con curl:**

#### Test 3.1: Health check
```bash
curl -s http://127.0.0.1:8787/health
# Esperado: OK
```

#### Test 3.2: Echo mode (modo codex)
```bash
curl -sN -X POST http://127.0.0.1:8787/v1/messages \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -H "Authorization: codex:fake_access_token:acc_123" \
  -d '{
    "model": "gpt-5.4",
    "max_tokens": 256,
    "stream": true,
    "messages": [
      {"role": "user", "content": "Hola mundo"}
    ]
  }'
```

Salida esperada (eventos Anthropic SSE):
```
event: message_start
data: {"type":"message_start","message":{"id":"msg_...","type":"message","role":"assistant","model":"gpt-5.4","content":[]}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Echo: Hola mundo"}}

event: content_block_stop
data: {"type":"content_block_start","index":0}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":3}}

event: message_stop
data: {"type":"message_stop"}
```

Validar ademas:
```bash
# Verificar que el Worker recibio el body correcto en el mock
tail -n 1 REPO/worker/test/mock-logs/requests.jsonl | jq .
# Esperado: contiene un body con `input[0].type=="message"`, `role=="user"`,
# `content[0].type=="input_text"`, `content[0].text=="Hola mundo"`.
# Tambien: `model=="gpt-5.4"`, `stream==true`.
# Y headers: Authorization redactado a "Bearer fake_ac...<redacted>",
# chatgpt-account-id=="acc_123", OpenAI-Beta presente.
```

#### Test 3.3: Tool call
```bash
curl -sN -X POST "http://127.0.0.1:8787/v1/messages" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -H "Authorization: codex:fake_token:acc_123" \
  -d '{
    "model": "gpt-5.4",
    "max_tokens": 512,
    "stream": true,
    "messages": [{"role": "user", "content": "que clima hace en santiago"}]
  }' | grep -E "^(event|data):" | head -30
```

Verificar que aparece un `content_block_start` con `type: tool_use` y un `input_json_delta` con `{"city":"Santiago"}`.

#### Test 3.4: Error 401 (token invalido)
```bash
# Levantar mock en modo error especifico
pkill -f mock_codex_backend.py 2>/dev/null
python3 test/mock_codex_backend.py --port 9999 --default-mode error &
sleep 1

# Disparar request
curl -sN -w "\nHTTP_STATUS:%{http_code}\n" -X POST "http://127.0.0.1:8787/v1/messages" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -H "Authorization: codex:bad:acc_123" \
  -d '{"model":"gpt-5.4","max_tokens":100,"stream":true,"messages":[{"role":"user","content":"hi"}]}'
```

Salida esperada:
- Un evento `event: error` con `data: {"type":"error","error":{"type":"api_error","message":"..."}}`.
- `HTTP_STATUS: 200` (Claude Code espera 200 + eventos SSE de error, no 401 HTTP, para mantener el stream abierto). Aclarar este contrato con el handler.

> Nota: si el handler decide propagar 401 como HTTP, el assertion cambia a `HTTP_STATUS: 401`. Documentar la decision en el handler.

#### Test 3.5: Stream interrupt (resilencia)
```bash
pkill -f mock_codex_backend.py 2>/dev/null
python3 test/mock_codex_backend.py --port 9999 --default-mode interrupt &
sleep 1

timeout 5 curl -sN -X POST "http://127.0.0.1:8787/v1/messages" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -H "Authorization: codex:fake:acc_123" \
  -d '{"model":"gpt-5.4","max_tokens":100,"stream":true,"messages":[{"role":"user","content":"hola"}]}'
```

Salida esperada:
- 3 eventos: `message_start`, `content_block_start`, `content_block_delta("Empezando a res")`.
- Despues el cliente cierra (timeout 5s).
- **No debe haber crash del Worker** (verificar que `wrangler dev` sigue vivo y responde a otro curl `/health`).

#### Test 3.6: Modo no-codex sigue yendo a OpenCode Go
```bash
curl -sN -X POST "http://127.0.0.1:8787/v1/messages" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -H "Authorization: Bearer sk-test-not-codex" \
  -d '{"model":"minimax-m3","max_tokens":50,"messages":[{"role":"user","content":"hola"}]}'
```

Salida esperada: JSON con `{"type":"error",...}` o respuesta del upstream OpenCode Go (que en este test no esta mockeado, asi que probablemente 500 con error de upstream). Lo importante: **NO** llega una request al mock backend en puerto 9999. Verificar:
```bash
tail -f REPO/worker/test/mock-logs/requests.jsonl
# No debe aparecer una nueva linea con esta request.
```

### 3.3 Test runner en bash

Crear `worker/test/integration.sh` que automatiza 3.1-3.6:

```bash
#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

WORKER_PORT=8787
MOCK_PORT=9999
BASE="http://127.0.0.1:$WORKER_PORT"
MOCK="http://127.0.0.1:$MOCK_PORT"

cleanup() {
  pkill -f mock_codex_backend.py 2>/dev/null || true
  pkill -f "wrangler dev" 2>/dev/null || true
}
trap cleanup EXIT

echo "[1/6] starting mock backend..."
python3 test/mock_codex_backend.py --port $MOCK_PORT --default-mode echo > /tmp/mock.log 2>&1 &
MOCK_PID=$!
sleep 1

echo "[2/6] starting wrangler dev..."
npx wrangler dev --port $WORKER_PORT --var "CODEX_BASE_URL=$MOCK" --var "OPENCODE_API_KEY=test" > /tmp/wrangler.log 2>&1 &
WRANGLER_PID=$!
echo "  waiting for worker to be ready..."
for i in {1..30}; do
  if curl -fsS "$BASE/health" >/dev/null 2>&1; then break; fi
  sleep 1
done

echo "[3/6] test 3.1 health..."
test "$(curl -fsS $BASE/health)" = "OK" || { echo "FAIL: health"; exit 1; }

echo "[4/6] test 3.2 echo..."
RESP=$(curl -sN -X POST $BASE/v1/messages \
  -H "Content-Type: application/json" \
  -H "Authorization: codex:fake:acc_1" \
  -d '{"model":"gpt-5.4","max_tokens":50,"stream":true,"messages":[{"role":"user","content":"ping"}]}')
echo "$RESP" | grep -q "text_delta" || { echo "FAIL: no text_delta"; echo "$RESP"; exit 1; }
echo "$RESP" | grep -q "message_stop" || { echo "FAIL: no message_stop"; exit 1; }

echo "[5/6] test 3.6 routing..."
# Reset mock logs
> test/mock-logs/requests.jsonl
curl -sN -X POST $BASE/v1/messages \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer not-codex" \
  -d '{"model":"minimax-m3","max_tokens":10,"messages":[{"role":"user","content":"x"}]}' >/dev/null 2>&1 || true
# Mock should NOT have received anything
[ ! -s test/mock-logs/requests.jsonl ] || { echo "FAIL: mock got a non-codex request"; cat test/mock-logs/requests.jsonl; exit 1; }

echo "all tests passed"
```

Uso:
```bash
cd REPO/worker
bash test/integration.sh
```

---

## Section 4: E2E Test (Manual)

Asume que el Worker esta deployado en Cloudflare (`wrangler deploy`) y que la URL publica es conocida (la leemos de `wrangler deploy` o de `https://<name>.<subdomain>.workers.dev`).

### 4.1 Pre-flight

```bash
# 1. Verificar que el deploy esta vivo
curl -fsS https://opencode-go-proxy.<TU_SUBDOMAIN>.workers.dev/health
# Esperado: OK

# 2. Verificar que codex CLI tiene auth valido
codex login --check 2>&1 || codex whoami
# O leer el token directamente:
python3 -c '
import json, pathlib
auth = json.loads(pathlib.Path("~/.codex/auth.json").read_text())
print("account_id:", auth.get("account_id", "MISSING"))
token = auth.get("access_token") or auth.get("tokens", {}).get("access_token", "")
print("token:", token[:8] + "..." if token else "MISSING")
print("expires:", auth.get("expires_at") or auth.get("tokens", {}).get("expires_at", "unknown"))
'

# 3. Verificar que claude-codex wrapper existe y es ejecutable
ls -la ~/.local/bin/claude-codex
chmod +x ~/.local/bin/claude-codex
```

### 4.2 Smoke test E2E

#### Paso 1: Desplegar el Worker (si no lo esta)
```bash
cd REPO/worker
npx wrangler deploy
# Output esperado:
# Total Upload: X.XX KiB / gzip: X.XX KiB
# Worker Startup Time: Xms
# Worker First Byte: Xms
# Uploaded opencode-go-proxy (Y.YY sec)
# Deployed opencode-go-proxy triggers (Y.YY sec)
#   https://opencode-go-proxy.<sub>.workers.dev
# Current Version ID: ...
```

#### Paso 2: Verificar que el modo codex esta activo en el deploy
```bash
WORKER_URL="https://opencode-go-proxy.<TU_SUBDOMAIN>.workers.dev"
# Test rapido con curl (sin Claude Code todavia):
curl -sN -X POST "$WORKER_URL/v1/messages" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -H "Authorization: codex:$(python3 -c 'import json,pathlib; print(json.loads(pathlib.Path("~/.codex/auth.json").read_text())["access_token"])'):$(python3 -c 'import json,pathlib; print(json.loads(pathlib.Path("~/.codex/auth.json").read_text())["account_id"])')" \
  -d '{"model":"gpt-5.4","max_tokens":50,"stream":true,"messages":[{"role":"user","content":"di hola en una palabra"}]}'
# Esperado: stream de eventos Anthropic, terminando con un text_delta que dice "hola" (o similar).
```

#### Paso 3: Ejecutar claude-codex
```bash
# Sanity check: el wrapper debe detectar el auth y exportar las env vars correctas
bash -x ~/.local/bin/claude-codex --help 2>&1 | head -50
# Esperado: variables ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN=codex:..., etc. exportadas
# y luego exec del binario claude original.

# Ejecutar interactivamente con un prompt simple
claude-codex -p "di hola en una palabra"
```

Salida esperada:
- El wrapper detecta `~/.codex/auth.json`, arma el token `codex:<access>:<account_id>`, exporta env vars, y hace `exec claude -p "..."`.
- Claude Code arranca, habla con el Worker (vía `ANTHROPIC_BASE_URL`), el Worker detecta el prefijo `codex:` y rutea a `handleCodexMessages`.
- Claude Code muestra la respuesta de Codex (algo como "Hola." o similar) y termina limpio.

#### Paso 4: Verificar logs del Worker
1. Ir a https://dash.cloudflare.com > Workers & Pages > `opencode-go-proxy`.
2. Logs > buscar invocaciones recientes.
3. Verificar:
   - La peticion aparece como 200 (o 200 + SSE).
   - **NO** aparece el token completo en ningun campo (buscar `codex:sk-...` o `codex:eyJh...`).
   - Aparece el `model` enviado (`gpt-5.4` u otro).
   - Aparece la cantidad de tokens consumidos en `usage`.

#### Paso 5: Edge case - peticion que falla con 401 real
```bash
# Forzar token invalido (modificando temporalmente ~/.codex/auth.json)
cp ~/.codex/auth.json ~/.codex/auth.json.bak
python3 -c '
import json, pathlib
p = pathlib.Path("~/.codex/auth.json")
auth = json.loads(p.read_text())
auth["access_token"] = "intentionally_invalid_token_xxxxxxxx"
p.write_text(json.dumps(auth))
'
# Limpieza al final del test:
# mv ~/.codex/auth.json.bak ~/.codex/auth.json

claude-codex -p "hola"
# Esperado: el Worker devuelve error de Codex ("401 Unauthorized"),
# Claude Code lo muestra como error de API, NO crashea la shell.
# Restaurar el auth original.
```

### 4.3 Checklist de aceptacion E2E

- [ ] Worker deploya sin errores.
- [ ] `curl` directo al endpoint con `codex:` token real devuelve un stream valido.
- [ ] `claude-codex` arranca Claude Code.
- [ ] Un prompt simple devuelve respuesta coherente (no error, no hang).
- [ ] Los logs del Worker NO contienen el token completo (solo primeros chars si hay debug).
- [ ] Un token invalido produce un mensaje de error legible (no stack trace, no hang).

---

## Section 5: Regression Tests

### 5.1 Objetivo

Asegurar que el modo OpenCode Go existente **no se rompe** al agregar el routing del modo codex. Específicamente: una request SIN prefijo `codex:` debe seguir siendo manejada por `handleMessages` (modo actual), no por `handleCodexMessages`.

### 5.2 Tests automatizados

Agregar a `worker/test/handler.test.js`:

```js
import { test } from "node:test";
import assert from "node:assert/strict";

// Asumimos que el modulo principal exporta una funcion `route` o
// que la logica de routing es testeable invocando `default.fetch` con
// distintos headers. Si la logica no es testeable directamente, ver
// la nota al final.

test("regression: header sin prefijo 'codex:' va a OpenCode Go", async () => {
  const env = {
    OPENCODE_API_KEY: "test-key",
    OPENCODE_BASE_URL: "http://127.0.0.1:9998",  // mock distinto al de codex
  };

  // Mock fetch global para que no salga a internet
  let fetchCalledWith = null;
  globalThis.fetch = async (url, opts) => {
    fetchCalledWith = { url, opts };
    return new Response(JSON.stringify({
      id: "chatcmpl-x",
      object: "chat.completion",
      model: "minimax-m3",
      choices: [{
        index: 0,
        message: { role: "assistant", content: "hi from opencode-go" },
        finish_reason: "stop",
      }],
      usage: { prompt_tokens: 1, completion_tokens: 3, total_tokens: 4 },
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  };

  // Importar el default export
  const mod = await import("../src/index.js");
  const req = new Request("http://worker/v1/messages", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": "Bearer sk-test",
    },
    body: JSON.stringify({
      model: "minimax-m3",
      max_tokens: 10,
      messages: [{ role: "user", content: "test" }],
    }),
  });
  const res = await mod.default.fetch(req, env);
  assert.equal(res.status, 200);
  const data = await res.json();
  assert.equal(data.content[0].text, "hi from opencode-go");

  // Y el fetch fue a OpenCode Go, NO a Codex backend
  assert.ok(fetchCalledWith.url.includes("9998"), "fetch fue a opencode-go mock");
  assert.ok(!fetchCalledWith.url.includes("/codex/responses"), "fetch NO fue a codex");
});
```

### 5.3 Test contra mock backend dual

Si el routing no es testeable de manera aislada (p.ej. usa `env` interno no expuesto), correr integration test que levante **dos** mocks:

- Mock A (`127.0.0.1:9998`): simula OpenCode Go. Responde con formato OpenAI chat completions.
- Mock B (`127.0.0.1:9999`): simula Codex.

```bash
# Terminal 1: mock opencode-go (formato OpenAI)
python3 test/mock_opencode_backend.py --port 9998 &

# Terminal 2: mock codex
python3 test/mock_codex_backend.py --port 9999 &

# Terminal 3: wrangler dev con ambas URLs
npx wrangler dev --port 8787 \
  --var "OPENCODE_BASE_URL=http://127.0.0.1:9998/v1" \
  --var "CODEX_BASE_URL=http://127.0.0.1:9999" \
  --var "OPENCODE_API_KEY=test"

# Test 5.1: request sin codex prefix -> va a mock 9998
RESP=$(curl -sN -X POST http://127.0.0.1:8787/v1/messages \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer not-codex" \
  -d '{"model":"minimax-m3","max_tokens":10,"messages":[{"role":"user","content":"x"}]}')
echo "$RESP" | jq -e '.content[0].text' && echo "  -> OK: llego al mock opencode-go"

# Test 5.2: request con codex prefix -> va a mock 9999
RESP=$(curl -sN -X POST http://127.0.0.1:8787/v1/messages \
  -H "Content-Type: application/json" \
  -H "Authorization: codex:fake:acc_1" \
  -d '{"model":"gpt-5.4","max_tokens":10,"stream":true,"messages":[{"role":"user","content":"y"}]}')
echo "$RESP" | grep -q "Echo: y" && echo "  -> OK: llego al mock codex"

# Validar: mock opencode-go recibio 1 request, mock codex recibio 1 request
[ "$(wc -l < test/mock-logs/requests.jsonl)" = "1" ] && echo "  -> OK: routing codex"
[ "$(wc -l < mock_opencode_logs/requests.jsonl)" = "1" ] && echo "  -> OK: routing opencode-go"
```

> El script `mock_opencode_backend.py` es trivial (10 lineas), devuelve un JSON con el formato de OpenAI chat completions. No se incluye aqui, es un duplicado conceptual del de codex pero devolviendo JSON en vez de SSE.

### 5.4 Cobertura de regresion

| Caso | Antes del codex mode | Despues del codex mode | Test |
|------|---------------------|------------------------|------|
| `Authorization: Bearer sk-...` | OpenCode Go | OpenCode Go (sin cambio) | 5.3 Test 5.1 |
| Sin `Authorization` | 500 (no key) | 500 (no key) | unit 1.6.3 |
| `Authorization: codex:...` | n/a (no soportado) | Codex mode | unit 1.6.1 + integ 3.2 |
| `x-api-key: codex:...` | n/a | Codex mode | unit 1.6.4 |
| `POST /v1/messages/count_tokens` con codex | handleCountTokens | handleCountTokens (sin cambio) | unit 1.6.5 |
| `GET /health` | "OK" | "OK" (sin cambio) | integ 3.1 |
| `GET /unknown` | 404 | 404 (sin cambio) | smoke |

---

## Section 6: Acceptance Checklist

Criterios para declarar el modo Codex **"done"**:

### Unit tests
- [ ] `node --test test/` pasa con 0 failures.
- [ ] Cobertura de `anthropicToCodexResponses` >= 90% (las 20 funciones de la Section 1.1 implementadas).
- [ ] Cobertura de `codexResponsesToAnthropic` >= 90% (las 15 funciones de Section 1.2).
- [ ] Cobertura de `transformCodexStream` >= 80% (10 funciones de Section 1.5). El flush() path es obligatorio.
- [ ] No hay tests que asuman formato interno de OpenAI Responses no validado (todos leen fixtures reales del mock).

### Mock backend integration
- [ ] `python3 test/mock_codex_backend.py` arranca en <2s sin warnings.
- [ ] Los 5 modos (echo, tool_call, error, interrupt, slow) responden cada uno con el formato esperado.
- [ ] Los logs en `mock-logs/requests.jsonl` redactan el header `Authorization` (verificado con `jq` y `grep`).
- [ ] `bash test/integration.sh` pasa los 6 tests en <30s.

### E2E con Codex real
- [ ] `wrangler deploy` termina sin errores.
- [ ] `curl` directo con `~/.codex/auth.json` real devuelve un stream valido con `text_delta` y `message_stop`.
- [ ] `claude-codex -p "di hola"` arranca Claude Code y devuelve respuesta coherente en <30s.
- [ ] Token invalido produce error legible (no stack trace, no hang > 30s).
- [ ] Token expirado (forzar con `cp auth.json auth.json.bak && edit`) -> error 401 del backend Codex -> Claude Code lo reporta limpio.

### Regresion OpenCode Go
- [ ] Test 5.1 pasa: request sin `codex:` va al mock OpenCode Go.
- [ ] Test 5.2 pasa: request con `codex:` va al mock Codex.
- [ ] El helper `estimateMessageTokens` y `handleCountTokens` siguen funcionando (smoke test con `POST /v1/messages/count_tokens`).
- [ ] El `transformStream` existente (modo OpenCode Go) sigue emitiendo la misma secuencia de eventos Anthropic que antes (no se modifico por error).

### Manejo de errores
- [ ] **401** (token codex invalido): Worker responde con `event: error` y/o HTTP 401, mensaje claro.
- [ ] **429** (rate limit): Worker responde con `event: error`, status 429, Claude Code reintenta segun su politica interna.
- [ ] **500** (backend caido): Worker responde con `event: error`, status 502 o 500, NO se cuelga.
- [ ] **Stream interrupt** (backend cierra a mitad): Worker emite `content_block_stop` + `message_stop` en `flush()`, NO crashea.
- [ ] **Body Anthropic invalido**: Worker responde HTTP 400 con `error.type="invalid_request_error"`.

### Seguridad / privacidad
- [ ] **NO** se loguea el token completo en ningun nivel: ni en `wrangler dev` stdout, ni en `worker/test/mock-logs/requests.jsonl`, ni en Cloudflare Workers Logs, ni en stderr del wrapper bash.
- [ ] El header `Authorization` se redacta a `<primeros 8 chars>...<redacted>` en logs.
- [ ] El header `chatgpt-account-id` se loguea completo (no es secreto).
- [ ] `.dev.vars` esta en `.gitignore` (verificar `cat .gitignore`).
- [ ] No hay `console.log(token)` o equivalente en `codex-handler.js` (buscar con `grep -rn "access_token" worker/src/` y auditar cada match).

### Documentacion
- [ ] `worker/README.md` documenta deploy + env vars + endpoints.
- [ ] `docs/PLAN_TESTING_CODEX.md` (este doc) mergeado al repo.
- [ ] `docs/README.md` actualizado con seccion "Codex" (como usar el wrapper, troubleshooting comun).
- [ ] `scripts/claude-codex` actualizado con `--help` que documente sus flags.
- [ ] Comentarios inline en `codex-handler.js` y `codex-protocol.js` (no excesivo, pero explica el "por que" de decisiones raras como el header `OpenAI-Beta`).

---

## Apéndice A: Comandos utiles

```bash
# Correr solo un test file
node --test test/protocol.test.js

# Correr solo un test por nombre
node --test --test-name-pattern="tool_call" test/

# Watch mode
node --test --watch test/

# Cobertura con c8 (sin instalar: usar --experimental-test-coverage)
node --test --experimental-test-coverage --test-coverage-include='src/**' test/

# Ver el body exacto que llega al mock
tail -f REPO/worker/test/mock-logs/requests.jsonl | jq .

# Filtrar logs de Cloudflare por status
wrangler tail --format=json | jq 'select(.outcome == "ok")'
```

## Apéndice B: Riesgos conocidos y como se mitigan en los tests

| Riesgo | Como se testea |
|--------|----------------|
| Codex API cambia formato de eventos | Los tests 1.2.x y 1.5.x validan contra snapshots, fallarian al primer cambio de upstream |
| Rate limit real interfiere con E2E | E2E usa el token real, pero la mayoria del coverage viene del mock; el E2E se corre 1-2 veces por sesion |
| Tokens se filtran a logs | Test 1.4.3 + assertion `grep "codex:" /tmp/wrangler.log` debe ser vacio |
| Worker se cuelga en flush() | Test 1.5.4 + integration 3.5 validan el flush con timeout externo |
| El mock no imita bien un edge case | Agregar nuevos modos al mock es trivial (10 lineas), expandir la suite conforme aparezcan bugs |

## Apéndice C: Orden de ejecucion recomendado

1. Implementar `codex-protocol.js` con funciones puras.
2. Escribir los tests de `codex-protocol.js` (Section 1.1-1.4) **antes** o en paralelo — TDD-friendly.
3. Implementar `codex-handler.js` con `transformCodexStream`.
4. Escribir los tests de stream (Section 1.5) usando el helper de ReadableStream.
5. Escribir `mock_codex_backend.py` y probar manualmente con curl.
6. Integrar el routing en `index.js`.
7. Escribir los tests de routing (Section 1.6).
8. Correr `bash test/integration.sh` end-to-end local.
9. `wrangler deploy` y correr Section 4 (E2E manual).
10. Tickear el acceptance checklist de Section 6.
