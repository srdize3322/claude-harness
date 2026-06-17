# Analysis: opencode Codex (ChatGPT OAuth) Integration

Source files analyzed:

- `packages/ai/src/providers/openai-codex-responses.ts` (1374 lines)
- `packages/ai/src/providers/openai-responses-shared.ts` (551 lines, the `processResponsesStream`, `convertResponsesMessages`, `convertResponsesTools`)
- `packages/ai/src/utils/oauth/openai-codex.ts` (458 lines)
- `packages/ai/src/utils/oauth/pkce.ts` (34 lines)
- `packages/ai/src/utils/oauth/oauth-page.ts` (HTML, irrelevant for port)
- `packages/ai/src/utils/oauth/types.ts` (interfaces, used by wrapper)
- `packages/ai/src/providers/transform-messages.ts` (used by shared conversion)

Target port: Cloudflare Workers (JavaScript, stateless). The local OAuth callback server is **not needed** because the wrapper bash script will perform OAuth and pass credentials to the Worker via `ANTHROPIC_AUTH_TOKEN` (format `codex:<access_token>:<account_id>`). Refresh is also handled by the wrapper.

---

## Section 1: OAuth Flow

### 1.1 Overview

opencode implements an **Authorization Code flow with PKCE** using a local HTTP callback server on `127.0.0.1:1455`. There is **no device-code polling** for Codex. The `/utils/oauth/device-code.ts` file in the repo is generic infrastructure used by other providers (e.g., GitHub Copilot, possibly) but **not** used for Codex.

The shape of the flow:

```
┌────────────┐                          ┌──────────────┐                ┌──────────────┐
│  CLI user  │                          │ auth.openai  │                │  localhost   │
│            │                          │   .com       │                │  :1455       │
└─────┬──────┘                          └──────┬───────┘                └──────┬───────┘
      │  1. generatePKCE() -> verifier, challenge│                              │
      │  2. createState() -> 16 random bytes hex │                              │
      │  3. build authorize URL                  │                              │
      │─────────────────────────────────────────>│                              │
      │  4. open browser to /oauth/authorize     │                              │
      │  5. user logs in + approves              │                              │
      │  6. callback to http://localhost:1455/auth/callback?code=...&state=...
      │                                          │                              │
      │  7. POST /oauth/token (code + verifier)  │                              │
      │─────────────────────────────────────────>│                              │
      │  8. { access_token, refresh_token, expires_in }
      │<─────────────────────────────────────────│                              │
      │  9. parse JWT, extract chatgpt_account_id                              │
      │  10. persist auth.json                                                     │
```

### 1.2 Endpoints

| Step | Method | URL | Purpose |
|------|--------|-----|---------|
| Authorize | GET | `https://auth.openai.com/oauth/authorize` | User login & consent |
| Token exchange | POST | `https://auth.openai.com/oauth/token` | Exchange code for tokens |
| Token refresh | POST | `https://auth.openai.com/oauth/token` | Refresh access token |

Callback URL: `http://localhost:1455/auth/callback` (the local server path; the URL `http://127.0.0.1:1455/auth/callback` works identically because the server binds to `127.0.0.1` by default; the env var `PI_OAUTH_CALLBACK_HOST` can change the bind host, but the redirect URI stays `localhost:1455`).

### 1.3 PKCE Parameters

From `pkce.ts`:

- `code_verifier`: 32 random bytes (`new Uint8Array(32)`) base64url-encoded, no padding. That is **43 characters** (32 bytes -> 43 base64url chars). Note: this is **shorter** than the OAuth spec recommendation of 43-128 chars but is what opencode uses. The S256 challenge is still valid.
- `code_challenge`: `base64url(sha256(code_verifier))`, 43 characters.
- `code_challenge_method`: `S256` (always).

The function uses Web Crypto (`crypto.getRandomValues`, `crypto.subtle.digest`) which is available in Cloudflare Workers, Node 20+, and browsers.

### 1.4 Authorize URL

```
GET https://auth.openai.com/oauth/authorize?
  response_type=code
  &client_id=app_EMoamEEZ73f0CkXaXp7hrann
  &redirect_uri=http://localhost:1455/auth/callback
  &scope=openid profile email offline_access
  &code_challenge=<base64url sha256 of verifier>
  &code_challenge_method=S256
  &state=<32 hex chars = 16 random bytes>
  &id_token_add_organizations=true
  &codex_cli_simplified_flow=true
  &originator=pi
```

`originator` is configurable (defaults to `"pi"` in the opencode fork); for Claude Code use `originator=claude-code` or similar.

### 1.5 Token Exchange

`POST https://auth.openai.com/oauth/token`

Headers:
```
Content-Type: application/x-www-form-urlencoded
```

Body (form-urlencoded):
```
grant_type=authorization_code
&client_id=app_EMoamEEZ73f0CkXaXp7hrann
&code=<auth code from callback>
&code_verifier=<PKCE verifier>
&redirect_uri=http://localhost:1455/auth/callback
```

Response (JSON):
```json
{
  "access_token": "<JWT>",
  "refresh_token": "<opaque>",
  "expires_in": 3600,
  "id_token": "<JWT>",  // present but unused by opencode
  "scope": "openid profile email offline_access",
  "token_type": "Bearer"
}
```

Error response (non-2xx):
```json
{
  "error": "invalid_grant",
  "error_description": "..."
}
```

### 1.6 Token Refresh

`POST https://auth.openai.com/oauth/token`

Headers:
```
Content-Type: application/x-www-form-urlencoded
```

Body:
```
grant_type=refresh_token
&refresh_token=<refresh token>
&client_id=app_EMoamEEZ73f0CkXaXp7hrann
```

Response shape is identical to the authorization_code exchange. The `refresh_token` may rotate (new value returned). The access token JWT must be re-decoded to get a fresh `chatgpt_account_id` (it is stable per user, but a safety check is to re-extract).

### 1.7 Access Token Decoding

The access token is a JWT (`xxx.yyy.zzz`). The `chatgpt_account_id` is at:

```js
const payload = JSON.parse(atob(jwt.split('.')[1]));
const accountId = payload['https://api.openai.com/auth']?.chatgpt_account_id;
```

The path is the **literal string** `https://api.openai.com/auth` (an audience-style claim). If absent, auth fails with `Failed to extract accountId from token`.

### 1.8 Storage (for the wrapper bash, not the Worker)

opencode does not show the auth.json schema in these files, but the credentials object is:

```ts
type OAuthCredentials = {
  refresh: string;   // refresh_token
  access: string;    // access_token (JWT)
  expires: number;   // Date.now() + expires_in * 1000 at issue time
  accountId: string; // chatgpt_account_id from JWT
  [key: string]: unknown;
};
```

Recommended location: `~/.codex/auth.json` (per the wrapper plan in `PLAN_CODEX.md`):

```json
{
  "OPENAI_API_KEY": null,
  "tokens": {
    "access_token": "...",
    "refresh_token": "...",
    "account_id": "..."
  },
  "last_refresh": "2026-06-16T10:00:00.000Z"
}
```

The Worker **does not read this file** — it only receives the access token + account_id in `ANTHROPIC_AUTH_TOKEN`.

### 1.9 Local Callback Server (NOT NEEDED IN WORKER)

`startLocalOAuthServer()` in `openai-codex.ts` spins up a `node:http` server on `127.0.0.1:1455`. The handler:

1. Returns 404 for any path other than `/auth/callback`.
2. Validates `state` query param matches generated state; returns 400 + error HTML on mismatch.
3. Extracts `code`; returns 400 if missing.
4. Returns 200 + success HTML.
5. Resolves the `waitForCode` promise with `{ code }`.

Two additional flows race the callback:

- `onManualCodeInput`: lets the user paste the redirect URL or just the code.
- `onPrompt`: same idea, fallback.

The `parseAuthorizationInput` helper accepts:
- A full URL: extracts `code` and `state` from query.
- A `code#state` string.
- A `code=...&state=...` query-string fragment.
- A raw code.

For the Worker, **none of this is needed** — the wrapper bash performs the OAuth. The Worker only needs the resulting tokens.

### 1.10 Constants

| Name | Value |
|------|-------|
| `CLIENT_ID` | `app_EMoamEEZ73f0CkXaXp7hrann` |
| `AUTHORIZE_URL` | `https://auth.openai.com/oauth/authorize` |
| `TOKEN_URL` | `https://auth.openai.com/oauth/token` |
| `REDIRECT_URI` | `http://localhost:1455/auth/callback` |
| `SCOPE` | `openid profile email offline_access` |
| `JWT_CLAIM_PATH` | `https://api.openai.com/auth` |
| `CALLBACK_HOST` | `127.0.0.1` (overridable via `PI_OAUTH_CALLBACK_HOST`) |
| Callback port | `1455` (hardcoded) |
| `originator` | `pi` (configurable) |
| `id_token_add_organizations` | `true` |
| `codex_cli_simplified_flow` | `true` |

---

## Section 2: Codex Backend API

### 2.1 Base URL & Endpoint

```
POST https://chatgpt.com/backend-api/codex/responses
```

The `model.baseUrl` (from the model registry) defaults to `https://chatgpt.com/backend-api` and `resolveCodexUrl()` (line 454) appends `/codex/responses` if the path doesn't already end with it. The Worker should hardcode the full URL since it does not use the opencode model registry.

### 2.2 HTTP Headers (SSE / HTTP transport)

Required headers for every Codex request:

| Header | Value | Notes |
|--------|-------|-------|
| `Authorization` | `Bearer <access_token>` | |
| `chatgpt-account-id` | `<account_id from JWT>` | **CRITICAL** — required by ChatGPT backend |
| `OpenAI-Beta` | `responses=experimental` | For SSE transport |
| `accept` | `text/event-stream` | For SSE transport |
| `content-type` | `application/json` | |
| `originator` | `pi` (or `claude-code`) | opencode sets this for the CLI |
| `User-Agent` | `pi (<platform> <release>; <arch>)` | opencode auto-generates; Worker can send `claude-code (cloudflare worker)` |
| `session_id` | `<session id>` | optional but recommended, helps Codex server correlate requests |
| `x-client-request-id` | `<session id>` | optional, same value as `session_id` |

For WebSocket transport the headers differ (see 2.3).

### 2.3 WebSocket Transport (out of scope for v1)

Codex also supports a WebSocket transport at `wss://chatgpt.com/backend-api/codex/responses` with header `OpenAI-Beta: responses_websockets=2026-02-06`. opencode implements this with session reuse and input-delta caching. **The Worker does not need this** for the first port — SSE only.

### 2.4 Request Body

```jsonc
{
  "model": "gpt-5.4",                       // required, from the model id
  "store": false,                            // required: must be false; otherwise Codex returns "Store must be set to false"
  "stream": true,                            // required for streaming
  "instructions": "...",                     // system prompt; falls back to "You are a helpful assistant."
  "input": [ ... ],                          // OpenAI Responses input array
  "tools": [ ... ],                          // optional, OpenAI function tool defs
  "tool_choice": "auto",
  "parallel_tool_calls": true,

  "temperature": 0.7,                        // optional, only if specified by caller
  "reasoning": {                             // optional
    "effort": "medium",                      // "none"|"minimal"|"low"|"medium"|"high"|"xhigh"
    "summary": "auto"                        // "auto"|"concise"|"detailed"|"off"|"on"
  },
  "service_tier": "default",                 // "default"|"flex"|"priority"
  "text": { "verbosity": "low" },            // "low"|"medium"|"high"

  "include": ["reasoning.encrypted_content"],// required: lets Codex return encrypted reasoning for replay
  "prompt_cache_key": "<sessionId truncated to 64 chars>"
}
```

Notes:
- `instructions` is the system prompt and is a top-level field, **not** part of the `input` array. The opencode code converts system messages to a separate `system`/`developer` item only when `includeSystemPrompt: true` (the default for non-Codex Responses); for Codex it passes `includeSystemPrompt: false` and uses the top-level `instructions` field (line 372-378).
- `previous_response_id` is used by opencode's WebSocket continuation flow only; for stateless SSE it is not sent.
- `include: ["reasoning.encrypted_content"]` is required so that `response.output_item.done` events for `reasoning` items include the `encrypted_content` blob. This is what is later serialized into `ThinkingContent.thinkingSignature` for replay.

### 2.5 Response (SSE)

Standard OpenAI Responses SSE format:

```
event: response.created
data: {"type":"response.created","response":{...}}

event: response.output_item.added
data: {"type":"response.output_item.added","output_index":0,"item":{...}}

data: {"type":"response.reasoning_summary_text.delta", ...}

data: [DONE]
```

The stream is plain `data: <json>\n\n` chunks (no `event:` lines used in practice). Each `data:` payload is a JSON object with a `type` field. `[DONE]` is the end-of-stream sentinel but is not always emitted by Codex; `response.completed` is the canonical terminator.

**Important non-obvious details**:

1. The Codex backend emits `response.done` in some cases and `response.completed` in others. `mapCodexEvents()` normalizes all three (`response.done`, `response.completed`, `response.incomplete`) into a single `response.completed` event with a normalized status (lines 536-543).
2. Error events are `error` (top-level, with `code` + `message`) or `response.failed` (wrapped in `response.error`). opencode throws on both.
3. Mid-stream `error` and `response.failed` events abort the stream.

### 2.6 Event Types Reference

The full set of events the Worker must handle (extracted from `processResponsesStream` and `mapCodexEvents`):

#### Lifecycle

| Event | Direction | Schema (relevant fields) |
|-------|-----------|--------------------------|
| `response.created` | inbound | `{type, response: {id, ...}}` — sets `output.responseId` |
| `response.completed` | inbound | `{type, response: {id, status, usage, service_tier, ...}}` — finalizes usage & stop reason |
| `response.incomplete` | inbound (normalized) | Same as `response.completed` but with `status="incomplete"` |
| `response.done` | inbound (normalized to `response.completed`) | Same |
| `response.failed` | inbound (fatal) | `{type, response: {error: {code, message}, incomplete_details: {reason}}}` |
| `error` | inbound (fatal) | `{type, code, message}` |

#### Output items

| Event | Item type | When |
|-------|-----------|------|
| `response.output_item.added` | `reasoning` | first event of a reasoning block |
| `response.output_item.added` | `message` | first event of a text block |
| `response.output_item.added` | `function_call` | first event of a tool call block |
| `response.output_item.done` | `reasoning` | last event of a reasoning block; carries full item incl. `encrypted_content` |
| `response.output_item.done` | `message` | last event of a text block |
| `response.output_item.done` | `function_call` | last event of a tool call |

#### Reasoning (item.type = `reasoning`)

| Event | Schema | Effect |
|-------|--------|--------|
| `response.reasoning_summary_part.added` | `{type, item_id, output_index, summary_index, part: {type, text}}` | Open new `summary[]` part on the current reasoning item |
| `response.reasoning_summary_text.delta` | `{type, item_id, output_index, summary_index, delta}` | Append `delta` to `ThinkingContent.thinking` and to the last summary part's text; emit `thinking_delta` |
| `response.reasoning_summary_part.done` | `{type, item_id, output_index, summary_index, part}` | Append `"\n\n"` to the thinking text (acts as part separator) |
| `response.reasoning_text.delta` | `{type, item_id, output_index, content_index, delta}` | Append `delta` to `ThinkingContent.thinking`; emit `thinking_delta` (no summary involvement) |

The reasoning item eventually has both `summary[]` (text-only parts) and `content[]` (encrypted blobs). opencode's `response.output_item.done` handler uses `summary` text first, falling back to `content` text, falling back to the accumulated `thinking` string. Since `encrypted_content` blobs are not human-readable, in practice the visible thinking text comes from `summary`. The full `item` object (including `encrypted_content`) is JSON-serialized into `ThinkingContent.thinkingSignature` for replay (line 445).

#### Message (item.type = `message`)

| Event | Schema | Effect |
|-------|--------|--------|
| `response.content_part.added` | `{type, item_id, output_index, content_index, part: {type: "output_text"\|"refusal", text?}}` | Push the part onto `currentItem.content` (output_text or refusal only — ReasoningText is filtered out) |
| `response.output_text.delta` | `{type, item_id, output_index, content_index, delta}` | Append `delta` to `TextContent.text` and to last part; emit `text_delta` |
| `response.refusal.delta` | `{type, item_id, output_index, content_index, delta}` | Append `delta` to `TextContent.text` and to `part.refusal`; emit `text_delta` |

The final text is taken from `item.content[].text` (or `refusal` if type is refusal). `textSignature` stores the item id + phase for replay: `JSON.stringify({v:1, id, phase})` (or just `{v:1, id}` if no phase).

#### Function call (item.type = `function_call`)

| Event | Schema | Effect |
|-------|--------|--------|
| `response.function_call_arguments.delta` | `{type, item_id, output_index, delta}` | Append `delta` to `partialJson`; re-parse JSON to `arguments`; emit `toolcall_delta` |
| `response.function_call_arguments.done` | `{type, item_id, output_index, arguments}` | Set `partialJson = arguments`; re-parse; emit any new tail as `toolcall_delta` (if delta is a prefix) |

On `response.output_item.done` (function_call), the `ToolCall` is finalized:
- `id` = `${item.call_id}|${item.id}`
- `name` = `item.name`
- `arguments` = `parseStreamingJson(partialJson || item.arguments)`
- `partialJson` is deleted (scratch field)

### 2.7 Status → Stop Reason

`response.completed` carries a `status` field. Mapping (`mapStopReason` lines 532-550):

| Status | StopReason |
|--------|------------|
| `completed` | `stop` |
| `incomplete` | `length` |
| `failed` | `error` |
| `cancelled` | `error` |
| `in_progress` | `stop` |
| `queued` | `stop` |

Then post-process: if the message has any `toolCall` block and `stopReason === "stop"`, change to `toolUse`. This is what makes tool-use turns come back with `stopReason: "toolUse"`.

### 2.8 Usage

In `response.completed.response.usage`:

```ts
{
  input_tokens: number,         // total input, including cached
  output_tokens: number,
  total_tokens: number,
  input_tokens_details: { cached_tokens: number },
  output_tokens_details?: { reasoning_tokens?: number }
}
```

opencode stores as:

```ts
output.usage = {
  input:    input_tokens - cached_tokens,    // non-cached input
  output:   output_tokens,
  cacheRead: cached_tokens,
  cacheWrite: 0,
  totalTokens: total_tokens,
  cost: { input, output, cacheRead, cacheWrite, total }
};
```

`calculateCost(model, usage)` then looks up pricing from the model registry. The Worker can either:
- Skip cost calculation entirely and return zeros (the plan in `PLAN_CODEX.md` suggests this is fine).
- Implement a pricing table for the supported models.

### 2.9 Error Response Format

Non-2xx HTTP response (e.g., 401, 429, 500):

```json
{
  "error": {
    "code": "usage_limit_reached" | "usage_not_included" | "rate_limit_exceeded" | ...,
    "type": "...",
    "message": "...",
    "plan_type": "plus" | "pro" | ...,
    "resets_at": 1718544000   // unix seconds, when applicable
  }
}
```

`parseErrorResponse` (lines 1269-1294) produces a friendly message for `usage_limit_reached` / `usage_not_included` / `rate_limit_exceeded` codes or status 429, e.g. `"You have hit your ChatGPT usage limit (plus plan). Try again in ~30 min."`. For other errors it uses the raw `message`.

### 2.10 Retry Behavior

opencode retries up to `MAX_RETRIES = 3` (4 total attempts) on:
- HTTP 429, 500, 502, 503, 504
- Body text matching `/rate.?limit|overloaded|service.?unavailable|upstream.?connect|connection.?refused/i`

Backoff: `BASE_DELAY_MS * 2^attempt` = 1s, 2s, 4s. Overridden by:
- `Retry-After-Ms` header (preferred, milliseconds)
- `Retry-After` header (seconds, or HTTP date)

The Worker should implement the same retry policy. Network errors (fetch throw) are also retried unless the error message contains `"usage limit"`.

### 2.11 Reasoning Configuration

| Field | Allowed values | Default in opencode |
|-------|----------------|---------------------|
| `reasoning.effort` | `none` \| `minimal` \| `low` \| `medium` \| `high` \| `xhigh` | Not set (omitted from body unless caller specifies) |
| `reasoning.summary` | `auto` \| `concise` \| `detailed` \| `off` \| `on` \| `null` | `auto` (when reasoning is enabled) |

Mapping from opencode's `reasoningEffort` to Codex's `effort`:
- `"none"` → `model.thinkingLevelMap?.off ?? "none"`
- Other values → `model.thinkingLevelMap?.[effort] ?? effort` (passthrough if no map)

If `effort` resolves to `null`, the `reasoning` object is **omitted entirely** from the body.

The `xhigh` value is supported by the type signature but is not in the OpenAI public Responses API. It works on Codex because Codex uses extended reasoning levels (e.g., for gpt-5.5).

### 2.12 Service Tier

`service_tier` (request field, also returned in response):

| Value | Cost multiplier (opencode) |
|-------|----------------------------|
| `default` | 1.0 |
| `flex` | 0.5 |
| `priority` | 2.0 (or 2.5 for `gpt-5.5`) |

The Worker can ignore cost multipliers and just forward the value. The response `service_tier` is used as authoritative when present and `default`; otherwise the request value is used.

---

## Section 3: Request Building (Anthropic → Codex)

The Worker exposes an **Anthropic Messages API**-compatible endpoint. It receives a request shaped like Anthropic's `/v1/messages` and must produce a Codex `/codex/responses` body.

### 3.1 Anthropic Request Shape (input to the Worker)

```ts
{
  model: "gpt-5.4",                         // or whatever the user requested
  max_tokens: number,
  system: string | { type: "text", text: string, cache_control?: ... }[],
  messages: Array<
    | { role: "user", content: string | ContentBlock[] }
    | { role: "assistant", content: string | ContentBlock[] }
  >,
  tools?: Array<{ name, description, input_schema }>,
  tool_choice?: { type: "auto" | "tool", name?: string },
  temperature?: number,
  thinking?: { type: "enabled" | "disabled", budget_tokens?: number },
  stream: true,
  metadata?: { user_id?: string }
}
```

Content blocks (Anthropic format):
- `{ type: "text", text: string, cache_control?: {...} }`
- `{ type: "image", source: { type: "base64", media_type, data } }`
- `{ type: "tool_use", id, name, input }`
- `{ type: "tool_result", tool_use_id, content: string | block[], is_error?: boolean }`

### 3.2 Translation Rules

#### System prompt
- Anthropic `system` (string or array) → Codex `instructions` (single string, concatenated).
- Default: `"You are a helpful assistant."` if absent.
- `system` content blocks are concatenated with `\n\n` between them.

#### User messages
- String content → Codex `[{type: "user", content: [{type: "input_text", text}]}]`
- Array content:
  - `text` blocks → `input_text`
  - `image` blocks → `input_image` with `image_url: "data:<media_type>;base64,<data>"`, `detail: "auto"`
- If the model doesn't support images, replace image blocks with a placeholder text `(image omitted: model does not support images)` (see `transform-messages.ts`).

#### Assistant messages
- `text` blocks → Codex `function_call` items NOT used; instead emit `{type: "message", role: "assistant", content: [{type: "output_text", text, annotations: []}], status: "completed", id, phase?}`.
- `tool_use` blocks → Codex `{type: "function_call", id, call_id, name, arguments: JSON.stringify(input)}`.
  - Anthropic `id` becomes both `call_id` and `id` parts. The opencode code does NOT do this for same-provider messages — it uses a composite id format `${callId}|${itemId}` from the previous Codex response. **For the port**, if you have a stored `id` from a prior Codex response of the form `call_xxx|fc_xxx`, split it and use the parts. If you have a raw Anthropic tool_use id (e.g., `toolu_xxx`), use it as `call_id` and synthesize an item id starting with `fc_` (e.g., `fc_<short_hash>`).
  - `arguments` field is the **JSON-stringified** object.
- `thinking` blocks:
  - If `thinkingSignature` is present and you trust it: parse as `ResponseReasoningItem` and push verbatim (this is replay).
  - If the message is from a different model: skip encrypted/redacted thinking; keep only visible text.
  - If from the same model: keep the block.

#### Tool result messages
- Anthropic `{role: "user", content: [{type: "tool_result", tool_use_id, content, is_error}]}` (Anthropic uses `user` role for tool results)
- Codex: a separate item `{type: "function_call_output", call_id, output}`.
  - `call_id` is the tool_use id (or the first part of the `id|itemId` composite).
  - `output` is a string. If the result contains images AND the model supports images, `output` can be a list of `input_text` + `input_image` parts; otherwise concatenate text and use a placeholder for images.

#### Tools
- Anthropic `{name, description, input_schema}` → Codex `{type: "function", name, description, parameters: input_schema, strict: null}`.
- opencode sets `strict: null` for Codex (which means: let Codex decide strictness). The public Responses API defaults `strict: false`.

#### Thinking
- Anthropic `thinking: {type: "enabled", budget_tokens}` → Codex `reasoning: {effort, summary: "auto"}`.
- `budget_tokens` is **not** used as a 1:1 mapping. The port should either:
  - Map `budget_tokens` to an effort level (e.g., `budget_tokens >= 24000 → "xhigh"`, `>= 8000 → "high"`, etc.), OR
  - Use a separate `reasoningEffort` parameter in the extended request and ignore `budget_tokens`.
- Recommended: pass `reasoningEffort` directly via a non-standard query param or header, since the Anthropic `thinking` type only has `enabled`/`disabled` + `budget_tokens`.

#### Temperature
- Pass through as `body.temperature` (Codex accepts it).

#### Service tier / verbosity
- Not in standard Anthropic Messages API. The Worker can accept them via a custom header (`X-Codex-Service-Tier`, `X-Codex-Text-Verbosity`) or query param.

### 3.3 Edge Cases

1. **Empty content array in a user message**: skip the message (opencode does this at line 156).
2. **Empty assistant message after transformation**: skip (`output.length === 0` check at line 215).
3. **Tool call without result**: opencode injects a synthetic tool result `"No result provided"` with `isError: true`. The port should do the same — Codex requires every `function_call` to have a matching `function_call_output` item.
4. **Errored/aborted assistant message in history**: opencode **drops it entirely** (line 192-193) to avoid replay errors. The port should do the same.
5. **Image in tool result** when model doesn't support images: replace with placeholder `"(tool image omitted: model does not support images)"` or `(see attached image)` if no text.
6. **Cross-model tool call ids**: Anthropic ids are 26+ chars; Codex needs them to start with `fc_` for the item id. opencode's `normalizeToolCallId` rewrites them to `fc_<shortHash>` and `callId|itemId` composite.

### 3.4 The `previous_response_id` Shortcut

opencode's WebSocket path uses `previous_response_id` to skip resending the full input. **For the Worker's SSE path this is not used.** Every request sends the full `input` array.

---

## Section 4: Response Translation (Codex → Anthropic)

The Worker must emit an **Anthropic Messages SSE stream** as output. Anthropic's stream format is:

```
event: message_start
data: {"type":"message_start","message":{...}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{...}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{...}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"...","stop_sequence":null},"usage":{...}}

event: message_stop
data: {"type":"message_stop"}
```

### 4.1 Anthropic Event Mapping

| Codex event | Anthropic event(s) | Notes |
|------------|-------------------|-------|
| (first event) | `message_start` | Synthesize a `message` object with `id`, `type:"message"`, `role:"assistant"`, `content:[], model, stop_reason:null, usage:{input_tokens:0,output_tokens:0}`. |
| `response.output_item.added` (item.type=`reasoning`) | `content_block_start` (thinking) + `content_block_delta` (empty) | `content_block: {type:"thinking", thinking:""}`. Index = current block count. |
| `response.reasoning_summary_text.delta` | `content_block_delta` (`delta:{type:"thinking_delta", thinking:delta}`) | Append `delta` to current thinking text. |
| `response.reasoning_summary_part.done` | `content_block_delta` (`delta:{type:"thinking_delta", thinking:"\n\n"}`) | Separator. |
| `response.reasoning_text.delta` | `content_block_delta` (`delta:{type:"thinking_delta", thinking:delta}`) | Same shape. |
| `response.output_item.done` (item.type=`reasoning`) | `content_block_stop` (thinking) | |
| `response.output_item.added` (item.type=`message`) | `content_block_start` (text) | `content_block: {type:"text", text:""}`. |
| `response.content_part.added` (part.type=`output_text`) | nothing visible (track part) | Track on the message item. |
| `response.output_text.delta` | `content_block_delta` (`delta:{type:"text_delta", text:delta}`) | |
| `response.refusal.delta` | `content_block_delta` (`delta:{type:"text_delta", text:delta}`) | Treat refusal as text in the port (Codex never emits refusals in practice). |
| `response.output_item.done` (item.type=`message`) | `content_block_stop` (text) | |
| `response.output_item.added` (item.type=`function_call`) | `content_block_start` (tool_use) | `content_block: {type:"tool_use", id, name, input:{}}`. |
| `response.function_call_arguments.delta` | `content_block_delta` (`delta:{type:"input_json_delta", partial_json:delta}`) | |
| `response.function_call_arguments.done` | (any remaining delta via `content_block_delta` (`input_json_delta`)) | Only emit if the final args string is a strict extension of the streamed partial. |
| `response.output_item.done` (item.type=`function_call`) | `content_block_stop` (tool_use) | |
| `response.completed` | `message_delta` + `message_stop` | See 4.2. |
| `error` / `response.failed` | `message_delta` (`stop_reason:"error"`) + emit a final error payload, then close. | Anthropic has no first-class error mid-stream. Most clients treat this as a transport error. |

### 4.2 Final Message

On `response.completed`:

```
event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"end_turn"|"max_tokens"|"tool_use"|"error","stop_sequence":null},"usage":{"input_tokens":N,"output_tokens":M}}

event: message_stop
data: {"type":"message_stop"}
```

`message_delta.usage` for Anthropic:
```ts
{
  input_tokens: usage.input + usage.cacheRead,
  output_tokens: usage.output,
  cache_read_input_tokens: usage.cacheRead,
  cache_creation_input_tokens: usage.cacheWrite,  // always 0 for Codex
}
```

The full final usage is also typically patched back into the `message_start` via a `message_delta`. Anthropic's spec says usage is sent in `message_delta` only (not in `message_start`), with the `message_start.usage` typically `{"input_tokens":0,"output_tokens":0}`.

### 4.3 Stop Reason Mapping

| Codex `response.completed.response.status` | Has tool calls? | Anthropic `stop_reason` |
|--------------------------------------------|-----------------|--------------------------|
| `completed` | no | `end_turn` |
| `completed` | yes | `tool_use` |
| `incomplete` | no | `max_tokens` |
| `incomplete` | yes | `tool_use` (rare; length-limited tool call) |
| `failed` | * | `error` (also surface error text to client) |
| `cancelled` | * | `error` |

### 4.4 Error Mid-Stream

When Codex sends `error` or `response.failed`:

1. Stop forwarding events.
2. Send a final `message_delta` with `stop_reason: "error"`.
3. Close the stream.
4. The HTTP status of the response is `200` (SSE) by convention; the error is in the body. The Anthropic SDK on the client side will detect `stop_reason: "error"` and raise an exception with the message from a synthetic content block, OR it will see a truncated stream and raise a network error.

The Worker may also detect `parseErrorResponse` patterns (e.g., `usage_limit_reached`) and translate to a friendlier error.

### 4.5 HTTP Error Before Stream

If the initial POST returns non-2xx, the Worker should return a non-2xx HTTP response with an Anthropic-shaped error body:

```json
{
  "type": "error",
  "error": {
    "type": "api_error" | "authentication_error" | "rate_limit_error" | "overloaded_error",
    "message": "<friendly message from parseErrorResponse>"
  }
}
```

The HTTP status mirrors Codex's:
- 401 → `authentication_error`
- 403 → `permission_error`
- 404 → `not_found_error`
- 429 → `rate_limit_error`
- 500+ → `api_error`
- everything else → `api_error`

### 4.6 Message ID

The `message_start.message.id` should be a unique string per request. opencode uses `output.responseId` (set from `response.created.response.id`), which is a Codex-issued id (e.g., `resp_xxx`). That works fine for Anthropic-shaped clients.

---

## Section 5: Code Reuse Notes (for the Worker port)

### 5.1 What to DROP

| opencode piece | Why drop |
|----------------|----------|
| `startLocalOAuthServer` (node:http server) | Wrapper bash handles OAuth. Worker never sees a callback. |
| `loginOpenAICodex` (the entire flow) | Same. |
| `refreshAccessToken` | Wrapper bash handles refresh. Worker just gets a fresh access token in `ANTHROPIC_AUTH_TOKEN`. (Optional: see 5.4 below.) |
| All WebSocket transport (lines 610-1263) | SSE only for v1. |
| `OAuthCredentials` types in `types.ts` | Not needed in the Worker; only the wrapper uses them. |
| `oauth-page.ts` (HTML pages) | Not needed. |
| `oauthSuccessHtml` / `oauthErrorHtml` | Not needed. |
| `clampThinkingLevel` and the model registry's `thinkingLevelMap` | The port can pick a default `reasoningEffort` per request without a model registry (or build a tiny static map for the few supported models). |
| Session-scoped WebSocket cache (`websocketSessionCache`, `SESSION_WEBSOCKET_CACHE_TTL_MS`) | Stateless. |
| `parseStreamingJson` partial-JSON parser | Use `JSON.parse` (Codex sends well-formed deltas; see 5.5). |
| `shortHash` and `transform-messages.ts` | Not needed; the Worker is a fresh consumer. |
| `clampOpenAIPromptCacheKey` | The port can derive `prompt_cache_key` from the session id (Anthropic `metadata.user_id` or a header) and truncate to 64 chars. |

### 5.2 Node-specific APIs and their CF Workers equivalents

| Node API | Where used | CF Workers equivalent |
|----------|------------|------------------------|
| `node:http.createServer` | `startLocalOAuthServer` | None (dropped). |
| `node:os.platform/release/arch` | `buildBaseCodexHeaders` for User-Agent | Hardcode `claude-code (cloudflare worker)` or read from `navigator.userAgent`. |
| `node:crypto.randomBytes` | `createState` | `crypto.getRandomValues(new Uint8Array(16))` (Web Crypto; available in Workers). |
| `globalThis.crypto.randomUUID` | `createCodexRequestId` | Available in Workers via `crypto.randomUUID()`. |
| `WebSocket` (node-undici or global) | WebSocket path | Dropped (SSE only). |
| `atob` / `btoa` | JWT decode | Available in Workers. |
| `URL`, `URLSearchParams` | everywhere | Available in Workers. |
| `TextDecoder`, `TextEncoder` | SSE parsing | Available in Workers. |
| `fetch` | All HTTP | Available in Workers (with `ReadableStream` body). |
| `Headers` | All header building | Available. |
| `setTimeout` / `setInterval` | Retry backoff, WS idle timer | Available. |
| `AbortController` / `AbortSignal` | Cancellation | Available. |

### 5.3 What the Worker MUST implement

1. SSE response reader (the `parseSSE` function, lines 558-604) — straightforward in Workers.
2. Event mapping (`mapCodexEvents`, lines 515-547) — exactly the same logic.
3. Stream-to-Anthropic translation (the inverse of `processResponsesStream`).
4. Error parsing (`parseErrorResponse`, lines 1269-1294).
5. Retry logic with backoff and `Retry-After`/`Retry-After-Ms` headers.
6. Auth header construction (`buildSSEHeaders`, lines 1338-1356) — minus the `node:os` User-Agent dependency.
7. Request body construction (Section 3) — substantially simpler than `buildRequestBody` because there's no model registry.

### 5.4 What the WRAPPER BASH needs

The wrapper bash script (per `PLAN_CODEX.md`) must:

1. Implement the OAuth flow from Section 1 (generate PKCE, open browser, wait for callback on `127.0.0.1:1455`, exchange code, store tokens).
2. Store credentials in `~/.codex/auth.json`:
   ```json
   { "access_token": "...", "refresh_token": "...", "account_id": "...", "expires_at": "2026-06-16T11:00:00Z" }
   ```
3. On every invocation: read `auth.json`; if `expires_at` is past or within 5 minutes, refresh.
4. Refresh logic: `POST https://auth.openai.com/oauth/token` with `grant_type=refresh_token&refresh_token=...&client_id=app_EMoamEEZ73f0CkXaXp7hrann`. Update `auth.json`.
5. Set the `ANTHROPIC_AUTH_TOKEN` env var to `codex:<access_token>:<account_id>` before exec'ing `claude`.

**Optional**: the Worker could itself refresh the token via a separate endpoint if `ANTHROPIC_AUTH_TOKEN` is augmented to `codex:<access>:<refresh>:<account_id>` and the Worker refreshes proactively on 401. The simpler design (per `PLAN_CODEX.md`) is to have the wrapper always send a fresh token.

### 5.5 Streaming JSON parsing

opencode uses `parseStreamingJson` to handle partial JSON. In practice, Codex's `function_call_arguments.delta` events emit **valid, complete JSON substrings** that are prefixes of the final JSON (e.g., `{"`, `{"x`, `{"x":`, `{"x":1`, `{"x":1}`). You can `JSON.parse` each prefix and it will work for object literals whose property order matches the final response. For arrays, a prefix like `[1` is invalid JSON; in that case you need the streaming parser.

For the port: **include a simple streaming JSON parser** (or copy `parseStreamingJson` from `packages/ai/src/utils/json-parse.ts` — ~50 lines). The semantics: when given a partial JSON, return the longest valid prefix; if nothing is valid, return the previously-valid value.

---

## Section 6: Constants & Config

### 6.1 OAuth

| Constant | Value |
|----------|-------|
| `CLIENT_ID` | `app_EMoamEEZ73f0CkXaXp7hrann` |
| `AUTHORIZE_URL` | `https://auth.openai.com/oauth/authorize` |
| `TOKEN_URL` | `https://auth.openai.com/oauth/token` |
| `REDIRECT_URI` | `http://localhost:1455/auth/callback` |
| `SCOPE` | `openid profile email offline_access` |
| `JWT_CLAIM_PATH` | `https://api.openai.com/auth` |
| `CALLBACK_HOST` (default) | `127.0.0.1` |
| Callback port | `1455` |
| `originator` (default) | `pi` |
| `id_token_add_organizations` | `true` |
| `codex_cli_simplified_flow` | `true` |
| PKCE method | `S256` |
| Verifier length | 32 random bytes -> 43 base64url chars |
| State length | 16 random bytes -> 32 hex chars |

### 6.2 Codex API

| Constant | Value |
|----------|-------|
| `DEFAULT_CODEX_BASE_URL` | `https://chatgpt.com/backend-api` |
| Codex endpoint | `/codex/responses` (SSE) or `wss://chatgpt.com/backend-api/codex/responses` (WS, not v1) |
| SSE `OpenAI-Beta` | `responses=experimental` |
| WS `OpenAI-Beta` | `responses_websockets=2026-02-06` (out of scope) |
| WS close code (msg too big) | `1009` |
| SSE end sentinel | `data: [DONE]` (sometimes absent; `response.completed` is canonical) |
| Required header | `chatgpt-account-id` |
| Required header | `Authorization: Bearer <token>` |
| Required body field | `store: false` (Codex rejects `store: true`) |
| Required body field | `stream: true` |
| Required body field | `include: ["reasoning.encrypted_content"]` |
| Default `tool_choice` | `auto` |
| Default `parallel_tool_calls` | `true` |
| Default `text.verbosity` | `low` |
| Default `reasoning.summary` | `auto` |

### 6.3 Reasoning effort

| Value | Notes |
|-------|-------|
| `none` | No reasoning. The `reasoning` object is omitted from the body (after level-map resolution). |
| `minimal` | |
| `low` | |
| `medium` | |
| `high` | |
| `xhigh` | Codex extension; not in public Responses API but accepted by Codex. |

### 6.4 Reasoning summary

| Value | Notes |
|-------|-------|
| `auto` | Default. |
| `concise` | |
| `detailed` | |
| `off` | |
| `on` | Legacy; `auto` is preferred. |

### 6.5 Service tier

| Value | Notes |
|-------|-------|
| `default` | Normal. |
| `flex` | Cheaper, slower. Multiplier 0.5. |
| `priority` | Faster. Multiplier 2.0 (2.5 for `gpt-5.5`). |

### 6.6 Text verbosity

| Value | Notes |
|-------|-------|
| `low` | Default in opencode for Codex. |
| `medium` | |
| `high` | |

### 6.7 Response status

| Status | Stop reason (mapped) |
|--------|----------------------|
| `completed` | `stop` |
| `incomplete` | `length` |
| `failed` | `error` |
| `cancelled` | `error` |
| `in_progress` | `stop` |
| `queued` | `stop` |

### 6.8 Retry policy

| Constant | Value |
|----------|-------|
| `MAX_RETRIES` | `3` (4 total attempts) |
| `BASE_DELAY_MS` | `1000` |
| Backoff | `1000 * 2^attempt` ms (1s, 2s, 4s) |
| Retryable statuses | 429, 500, 502, 503, 504 |
| Retryable text regex | `/rate.?limit\|overloaded\|service.?unavailable\|upstream.?connect\|connection.?refused/i` |
| Override headers | `Retry-After-Ms` (preferred), `Retry-After` (seconds or HTTP date) |

### 6.9 Models (from `register-builtins.ts`, inferred)

The plan says gpt-5.4 and gpt-5.5; opencode's actual model list is broader. For the port, support at minimum:

| Model id | Notes |
|----------|-------|
| `gpt-5` | |
| `gpt-5-codex` | |
| `gpt-5.1` | |
| `gpt-5.1-codex` | |
| `gpt-5.1-codex-max` | |
| `gpt-5.2` | |
| `gpt-5.2-codex` | |
| `gpt-5.4` | (target for this port) |
| `gpt-5.5` | (target for this port; service_tier `priority` is 2.5x) |
| `gpt-5.5-codex` | |

The Worker should pass the model id through as-is to Codex; the Codex backend will return 400 if the model is unknown. The list above is what `openai-codex-responses` is built to support.

### 6.10 Allowed tool-call providers

`CODEX_TOOL_CALL_PROVIDERS = new Set(["openai", "openai-codex", "opencode"])` — only used in `convertResponsesMessages` to decide whether to re-normalize tool call ids. Not relevant for the Worker since the Worker is the producer; the consumers are opencode/Codex CLI clients that already speak this format.

---

## Section 7: Test Cases

These are the cases the port should cover. Each case is a self-contained test the Worker must pass.

### 7.1 Simple text request/response (no streaming error)

- **Input**: Anthropic Messages request with `model="gpt-5.4"`, `messages=[{role:"user", content:"Say 'ok'"}]`, `stream=true`.
- **Expected Codex body**: `{"model":"gpt-5.4","store":false,"stream":true,"instructions":"You are a helpful assistant.","input":[{"role":"user","content":[{"type":"input_text","text":"Say 'ok'"}]}],"text":{"verbosity":"low"},"include":["reasoning.encrypted_content"],"tool_choice":"auto","parallel_tool_calls":true}`.
- **Expected Anthropic stream** (synthesized from Codex events):
  - `message_start`
  - `content_block_start` (text, index 0)
  - `content_block_delta` (text_delta with `"ok"`)
  - `content_block_stop`
  - `message_delta` (stop_reason `end_turn`, usage `{input_tokens, output_tokens, ...}`)
  - `message_stop`
- **Verify**: `stop_reason === "end_turn"`, body has exactly one content block of type text, `text === "ok"`.

### 7.2 System prompt

- **Input**: `system="You only answer in haiku."`, `messages=[{role:"user", content:"Hello"}]`.
- **Expected Codex body**: `instructions="You only answer in haiku."`, no `system` role in `input`.
- **Verify**: `body.instructions` matches the system prompt verbatim, `body.input` does not contain a system message.

### 7.3 System prompt as array

- **Input**: `system=[{type:"text", text:"Part 1."}, {type:"text", text:"Part 2."}]`.
- **Expected Codex body**: `instructions="Part 1.\n\nPart 2."`.

### 7.4 Tool calling

- **Input**: `tools=[{name:"get_weather", description:"...", input_schema:{type:"object", properties:{city:{type:"string"}}, required:["city"]}}]`, `messages=[{role:"user", content:"Weather in Paris?"}]`.
- **Expected Codex body**: `tools=[{type:"function", name:"get_weather", description:"...", parameters:{...}, strict:null}]`, `tool_choice:"auto"`, `parallel_tool_calls:true`.
- **Mocked Codex stream**: `response.created` -> `output_item.added(function_call, {name:"get_weather", call_id:"call_xxx", id:"fc_xxx", arguments:""})` -> `function_call_arguments.delta` (multiple) -> `function_call_arguments.done` -> `output_item.done` -> `response.completed` (status `completed`).
- **Expected Anthropic stream**:
  - `content_block_start` (tool_use, `id="call_xxx|fc_xxx"`, `name="get_weather"`, `input={}`)
  - `content_block_delta` (input_json_delta with `{"city":"Paris"}`)
  - `content_block_stop`
  - `message_delta` (stop_reason `tool_use`)
  - `message_stop`
- **Verify**: the final assistant message has a single `tool_use` block with parsed `input.city === "Paris"`, `stop_reason === "tool_use"`.

### 7.5 Tool result follows

- **Input**: append `{role:"user", content:[{type:"tool_result", tool_use_id:"call_xxx|fc_xxx", content:"72F sunny"}]}` to the previous request.
- **Expected Codex body**: `input` ends with `{type:"function_call_output", call_id:"call_xxx", output:"72F sunny"}`.
- **Verify**: `call_id` is split off the composite id; `output` is a string.

### 7.6 Tool call split across requests (replay)

- **Input**: a request where the assistant message in history has a tool_call with `id="call_xxx|fc_abc"` and the corresponding `function_call_output` for `call_xxx`.
- **Expected Codex body**: `input` includes:
  - `{type:"function_call", id:"fc_abc", call_id:"call_xxx", name:"...", arguments:"{...}"}`
  - `{type:"function_call_output", call_id:"call_xxx", output:"..."}`
- **Verify**: id format is preserved through round-trip.

### 7.7 Thinking / reasoning

- **Input**: a request with extended headers `X-Codex-Reasoning-Effort: high` (or similar).
- **Expected Codex body**: `reasoning: {effort:"high", summary:"auto"}`.
- **Mocked Codex stream**: `response.created` -> `output_item.added(reasoning, {id:"rs_xxx"})` -> `reasoning_summary_part.added` -> `reasoning_summary_text.delta` ("Let me think") -> `reasoning_summary_text.delta` (" about Paris") -> `reasoning_summary_part.done` -> `output_item.done(reasoning, {id:"rs_xxx", summary:[{type:"summary_text", text:"Let me think about Paris\n\n"}], encrypted_content:"..."})` -> `output_item.added(message, ...)` -> ... -> `response.completed`.
- **Expected Anthropic stream**:
  - `content_block_start` (thinking, index 0)
  - `content_block_delta` (thinking_delta with "Let me think")
  - `content_block_delta` (thinking_delta with " about Paris")
  - `content_block_delta` (thinking_delta with "\n\n")
  - `content_block_stop`
  - `content_block_start` (text, index 1)
  - `content_block_delta` (text_delta with the answer)
  - `content_block_stop`
  - `message_delta` (stop_reason `end_turn`)
  - `message_stop`
- **Verify**: exactly 2 content blocks, indices 0 (thinking) and 1 (text); thinking text contains "Let me think about Paris\n\n".

### 7.8 `response.incomplete`

- **Mocked**: `response.completed` with `status:"incomplete"`, no `incomplete_details.reason`.
- **Expected**: `stop_reason === "max_tokens"`.

### 7.9 `response.failed` mid-stream

- **Mocked**: stream emits `output_item.added(message, ...)` then `error` event with `code:"server_error"`, `message:"..."`.
- **Expected**:
  - The Worker aborts forwarding.
  - A final `message_delta` with `stop_reason:"error"`.
  - Stream closes.
  - HTTP status is 200 (SSE).

### 7.10 401 from Codex

- **Mocked**: POST returns HTTP 401 with body `{"error":{"code":"invalid_api_key","message":"..."}}`.
- **Expected**: Worker returns HTTP 401 with Anthropic error shape `{"type":"error","error":{"type":"authentication_error","message":"..."}}`.

### 7.11 429 with `usage_limit_reached`

- **Mocked**: HTTP 429, body `{"error":{"code":"usage_limit_reached","plan_type":"plus","resets_at":<future>}}`.
- **Expected**: Anthropic error body with `type:"rate_limit_error"`, `message` includes `"You have hit your ChatGPT usage limit (plus plan). Try again in ~N min."` (where N is `(resets_at*1000 - now) / 60000` rounded).

### 7.12 500 transient

- **Mocked**: First POST returns 500; second returns 200.
- **Expected**: Worker retries after 1s; final response is the parsed stream. (Use a fake clock or short backoff in tests.)

### 7.13 500 exhausted

- **Mocked**: POST returns 500 four times.
- **Expected**: Worker returns HTTP 500 with Anthropic error shape `{"type":"error","error":{"type":"api_error","message":"..."}}`. Total attempts = 4.

### 7.14 Network error

- **Mocked**: `fetch` throws `TypeError: fetch failed`.
- **Expected**: Worker retries up to 3 times; if all fail, returns 500 with `api_error`.

### 7.15 Retry-After-Ms

- **Mocked**: First POST returns 503 with `Retry-After-Ms: 50`; second returns 200.
- **Expected**: Second attempt happens ~50 ms after the first (use `vi.useFakeTimers()` or check delay between timestamps).

### 7.16 Aborted stream (client disconnect)

- **Input**: Worker is reading Codex SSE, client disconnects.
- **Expected**: Worker calls `reader.cancel()` on the Codex body and aborts the in-flight `fetch`.

### 7.17 Image in user message

- **Input**: user content is `[{type:"text", text:"What's this?"}, {type:"image", source:{type:"base64", media_type:"image/png", data:"<base64>"}}]`.
- **Expected Codex body**: `input[0].content = [{type:"input_text", text:"What's this?"}, {type:"input_image", detail:"auto", image_url:"data:image/png;base64,..."}]`.

### 7.18 Image in tool result for a vision-capable model

- **Input**: tool result content is `[{type:"text", text:"Screenshot:"}, {type:"image", source:{type:"base64", media_type:"image/png", data:"..."}}]`.
- **Expected Codex body** (if model supports images): `input` includes `{type:"function_call_output", call_id, output:[{type:"input_text", text:"Screenshot:"}, {type:"input_image", detail:"auto", image_url:"data:image/png;base64,..."}]}`.

### 7.19 Empty assistant message in history (errored)

- **Input**: history contains an assistant message with `stopReason:"error"` and no content blocks.
- **Expected**: Worker **skips** the message entirely (opencode drops these at line 192-193).

### 7.20 Cross-provider tool call id

- **Input**: history contains a tool_call with `id="toolu_01abc"` (Anthropic format) and a tool_result for it.
- **Expected**: Worker rewrites to `call_id="toolu_01abc"`, `id="fc_<shortHash of toolu_01abc>"` (must start with `fc_`).

### 7.21 prompt_cache_key

- **Input**: Anthropic `metadata.user_id="user-12345"` (or header `X-Session-Id`).
- **Expected**: Codex body has `prompt_cache_key="user-12345"` truncated to 64 chars.

### 7.22 service_tier passthrough

- **Input**: header `X-Codex-Service-Tier: flex`.
- **Expected**: Codex body has `service_tier:"flex"`. The Worker does NOT modify the cost.

### 7.23 Encrypted reasoning replay

- **Input**: a follow-up request where the previous assistant message had a thinking block with `thinkingSignature` set to the JSON-stringified reasoning item (from the previous `response.output_item.done`).
- **Expected Codex body**: the `input` array contains the same reasoning item verbatim (as a `ResponseReasoningItem`).

### 7.24 Multiple tool calls in one response

- **Mocked**: Codex emits two `output_item.added(function_call)` events with `parallel_tool_calls:true`.
- **Expected Anthropic stream**: two `content_block_start` (tool_use) at indices 0 and 1, with corresponding deltas and stops. Final `stop_reason:"tool_use"`.

### 7.25 `response.done` (Codex variant)

- **Mocked**: stream ends with `response.done` instead of `response.completed`.
- **Expected**: Worker treats it identically (normalization step in `mapCodexEvents`).

### 7.26 Refusal deltas

- **Mocked**: Codex emits `response.refusal.delta` events.
- **Expected**: Worker forwards them as `text_delta` deltas (per opencode's handling in `processResponsesStream`). The final `text` block contains the refusal text.

### 7.27 User-Agent (sanity)

- **Expected**: The Worker sends a `User-Agent` header (Codex may not require it, but mirror opencode's behavior to avoid being flagged as bot traffic).

---

## Appendix A: File reference for the port engineer

If you need to consult the source again:

| What | Where |
|------|-------|
| OAuth flow | `packages/ai/src/utils/oauth/openai-codex.ts:308-435` |
| Authorize URL construction | `openai-codex.ts:187-206` |
| Token exchange | `openai-codex.ts:92-137` |
| Token refresh | `openai-codex.ts:139-185` |
| JWT account_id extraction | `openai-codex.ts:80-90, 290-295` |
| Main stream function | `openai-codex-responses.ts:128-339` |
| Request body builder | `openai-codex-responses.ts:365-413` |
| SSE parsing | `openai-codex-responses.ts:558-604` |
| Event mapping (Codex -> Responses) | `openai-codex-responses.ts:515-547` |
| Header building (SSE) | `openai-codex-responses.ts:1338-1356` |
| Header building (base) | `openai-codex-responses.ts:1320-1336` |
| URL resolution | `openai-codex-responses.ts:454-467` |
| Retry policy | `openai-codex-responses.ts:103-122, 235-307` |
| Error parsing | `openai-codex-responses.ts:1269-1294` |
| Message conversion (Anthropic/Common -> Responses) | `openai-responses-shared.ts:90-262` |
| Tool conversion | `openai-responses-shared.ts:268-277` |
| Stream event handling (Responses -> AssistantMessage) | `openai-responses-shared.ts:283-530` |
| Stop reason mapping | `openai-responses-shared.ts:532-550` |
| Message transformation (cross-model) | `packages/ai/src/providers/transform-messages.ts` |
| PKCE | `packages/ai/src/utils/oauth/pkce.ts:21-34` |

## Appendix B: Notes on what opencode does NOT do (and shouldn't be in the port)

- No `stream: false` mode. Always stream.
- No `previous_response_id` for SSE. Only used in WS continuation.
- No `store: true`. Codex rejects it.
- No `truncation` strategy. Codex handles its own.
- No `metadata` field forwarded to Codex (other than `prompt_cache_key`).
- The full Conversation state (system + tools) is rebuilt from scratch every request. No server-side conversation memory in Codex; all history must be sent.

## Appendix C: Risk areas

1. **Encrypted reasoning content**: `item.encrypted_content` from one response must be replayed verbatim. If you serialize/deserialize it through JSON, the order of fields doesn't matter (JSON), but the **content** of the encrypted blob must be byte-identical. Don't strip whitespace or normalize. Just `JSON.stringify(item)` and store.

2. **Tool call id format**: Codex rejects tool calls where `id` doesn't start with `fc_`. The Worker must always rewrite foreign-format ids (e.g., `toolu_xxx` from Anthropic) to `fc_<hash>`.

3. **`call_id` is the public id**: `call_id` is what the user sees and what the tool result must reference. It does NOT need to start with `fc_`.

4. **`input` ordering**: each `function_call` MUST be followed in the next position(s) by zero or more `function_call_output` items (or interleaved with other items per the spec). opencode's `convertResponsesMessages` puts them in the same order as in the source messages; the Worker should preserve order.

5. **`response.incomplete_details.reason`**: when status is `incomplete`, this may be `{"reason":"max_output_tokens"}` or `{"reason":"content_filter"}`. Worker should surface it in the `error` message for transparency.

6. **`reasoning.encrypted_content` field is large**: storing it per response increases the request payload on follow-ups. For long sessions, consider stripping it from `thinkingSignature` after some turns, or compress the JSON.
