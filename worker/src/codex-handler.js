// Codex handler for claude-harness Worker
// Translates Anthropic Messages API <-> OpenAI Codex backend (chatgpt.com/backend-api/codex/responses)
//
// Stateless: the wrapper bash script handles OAuth (device-auth, refresh) and passes
// the access_token via ANTHROPIC_AUTH_TOKEN="codex:<token>:<account_id>".
//
// See docs/ANALYSIS_OPENCODE_CODEX.md for the full protocol analysis.

import {
  isRetryableStatus,
  retryDelay,
  anthropicToCodexResponses,
  MAX_RETRIES,
} from "./codex-protocol.js";

const CODEX_DEFAULT_BASE_URL = "https://chatgpt.com/backend-api";
const CODEX_RESPONSES_PATH = "/codex/responses";

// detectCodexMode returns true if the Authorization header signals Codex mode.
// The format is: "codex:<access_token>:<chatgpt_account_id>"
export function detectCodexMode(authHeader) {
  return typeof authHeader === "string" && authHeader.startsWith("codex:");
}

// parseCodexAuth parses the ANTHROPIC_AUTH_TOKEN header into its parts.
// Returns { accessToken, accountId, baseUrl } or null if invalid.
export function parseCodexAuth(authHeader) {
  if (!detectCodexMode(authHeader)) return null;
  const rest = authHeader.slice("codex:".length);
  // Format: <access_token>:<chatgpt_account_id>
  // The account_id is the LAST segment (it's a UUID without colons).
  const lastColon = rest.lastIndexOf(":");
  if (lastColon < 0) return null;
  const accessToken = rest.slice(0, lastColon);
  const accountId = rest.slice(lastColon + 1);
  if (!accessToken || !accountId) return null;
  return { accessToken, accountId, baseUrl: CODEX_DEFAULT_BASE_URL };
}

// codexHeaders builds the headers for a Codex backend request.
export function codexHeaders(modelHeaders, accessToken, accountId, sessionId) {
  const headers = {
    "Content-Type": "application/json",
    "Authorization": `Bearer ${accessToken}`,
    "chatgpt-account-id": accountId,
    "OpenAI-Beta": "responses=experimental",
    "accept": "text/event-stream",
  };
  if (sessionId) headers["session_id"] = sessionId;
  if (modelHeaders) {
    for (const [k, v] of Object.entries(modelHeaders)) {
      if (typeof v === "string") headers[k] = v;
    }
  }
  return headers;
}

// mapStopReason maps a Codex response status to an Anthropic stop_reason.
export function mapStopReason(status, hasToolCalls) {
  switch (status) {
    case "completed":
      return hasToolCalls ? "tool_use" : "end_turn";
    case "incomplete":
      return "max_tokens";
    case "failed":
      return "error";
    case "cancelled":
      return "end_turn";
    default:
      return "end_turn";
  }
}

// mapStatusToErrorType maps an HTTP status to an Anthropic error type.
export function mapStatusToErrorType(status) {
  if (status === 401) return "authentication_error";
  if (status === 403) return "permission_error";
  if (status === 404) return "not_found_error";
  if (status === 429) return "rate_limit_error";
  if (status >= 500 && status < 600) return "api_error";
  if (status >= 400 && status < 500) return "invalid_request_error";
  return "api_error";
}

// sseEvent formats an event as an Anthropic SSE string.
export function sseEvent(eventType, data) {
  const payload = typeof data === "string" ? data : JSON.stringify(data);
  return `event: ${eventType}\ndata: ${payload}\n\n`;
}

// formatAnthropicError returns a Response with the Anthropic error envelope.
export function formatAnthropicError(status, message, errorType) {
  const type = errorType || mapStatusToErrorType(status);
  return new Response(
    JSON.stringify({ type: "error", error: { type, message: String(message || "") } }),
    { status, headers: { "Content-Type": "application/json" } }
  );
}

// parseCodexErrorBody extracts a friendly error message from a Codex error body.
// Returns null if the body doesn't look like a structured Codex error.
export function parseCodexErrorBody(body) {
  if (body == null) return null;
  if (typeof body === "string") {
    try { body = JSON.parse(body); } catch (_) { return body || null; }
  }
  if (!body || typeof body !== "object") return null;
  const err = body.error;
  if (err == null) return null;
  if (typeof err === "string") return err;
  if (err.code === "usage_limit_reached" || err.code === "usage_not_included") {
    const planType = err.plan_type || "your plan";
    if (err.resets_at) {
      const minutes = Math.max(1, Math.round((err.resets_at * 1000 - Date.now()) / 60000));
      return `You have hit your ChatGPT usage limit (${planType} plan). Try again in ~${minutes} min.`;
    }
    return `You have hit your ChatGPT usage limit (${planType} plan).`;
  }
  if (err.code === "rate_limit_exceeded") {
    return "Rate limit exceeded. Please slow down and try again.";
  }
  if (err.code === "invalid_api_key" || err.code === "token_expired") {
    return "ChatGPT access token is invalid or expired. Refresh via the wrapper.";
  }
  return err.message || null;
}

// isRetryableErrorText checks if a response body indicates a retryable error
// (overloaded, upstream connect failure, etc).
function isRetryableErrorText(text) {
  if (!text) return false;
  return /rate.?limit|overloaded|service.?unavailable|upstream.?connect|connection.?refused/i.test(text);
}

// getRetryDelay computes the delay (ms) before the next retry attempt.
// Honors Retry-After-Ms (preferred, milliseconds) and Retry-After (seconds or
// HTTP date); falls back to exponential backoff from codex-protocol.
function getRetryDelay(response, attempt) {
  if (response && response.headers && typeof response.headers.get === "function") {
    const ms = response.headers.get("Retry-After-Ms");
    if (ms) {
      const n = parseInt(ms, 10);
      if (Number.isFinite(n) && n >= 0) return n;
    }
    const after = response.headers.get("Retry-After");
    if (after) {
      const n = parseInt(after, 10);
      if (Number.isFinite(n) && n >= 0) return n * 1000;
      const t = Date.parse(after);
      if (Number.isFinite(t)) return Math.max(0, t - Date.now());
    }
  }
  return retryDelay(attempt + 1);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function randomShortId(len = 24) {
  return crypto.randomUUID().replace(/-/g, "").slice(0, len);
}

function safeJsonParse(text) {
  if (!text) return null;
  try { return JSON.parse(text); } catch (_) { return null; }
}

// createStreamState initializes the state for the Codex->Anthropic transform.
// Exported for unit testing.
export function createStreamState(model) {
  return {
    msgId: `msg_${randomShortId(24)}`,
    model: model || "gpt-5.4",
    messageStartSent: false,
    blocks: new Map(), // output_index -> { type, toolId?, name? }
    hasToolCalls: false,
    finalUsage: null,
    finalStatus: null,
    finalModel: null,
    finished: false,
  };
}

function buildMessageStartPayload(state) {
  state.messageStartSent = true;
  return {
    type: "message_start",
    message: {
      id: state.msgId,
      type: "message",
      role: "assistant",
      content: [],
      model: state.finalModel || state.model,
      stop_reason: null,
      stop_sequence: null,
      usage: { input_tokens: 0, output_tokens: 0 },
    },
  };
}

function buildCloseBlocksPayloads(state) {
  const out = [];
  for (const idx of state.blocks.keys()) {
    out.push({ type: "content_block_stop", index: idx });
  }
  return out;
}

// translateCodexEvent processes a single Codex SSE event, mutating `state` and
// returning a list of {event, data} Anthropic event payloads to emit.
// Pure-ish (only state mutation), exported for unit testing.
export function translateCodexEvent(evt, state) {
  if (!evt || typeof evt !== "object") return [];
  const out = [];
  const t = evt.type;

  if (t === "response.created") {
    const resp = evt.response || {};
    if (resp.id) state.msgId = `msg_${resp.id}`;
    if (resp.model) state.finalModel = resp.model;
    if (!state.messageStartSent) {
      out.push({ event: "message_start", data: buildMessageStartPayload(state) });
    }
  } else if (t === "response.output_item.added") {
    const item = evt.item || {};
    const idx = typeof evt.output_index === "number" ? evt.output_index : 0;
    if (item.type === "message") {
      state.blocks.set(idx, { type: "text" });
      out.push({
        event: "content_block_start",
        data: {
          type: "content_block_start",
          index: idx,
          content_block: { type: "text", text: "" },
        },
      });
    } else if (item.type === "function_call") {
      state.hasToolCalls = true;
      const toolId = item.call_id || item.id || `toolu_${randomShortId(24)}`;
      state.blocks.set(idx, { type: "tool_use", toolId, name: item.name || "" });
      out.push({
        event: "content_block_start",
        data: {
          type: "content_block_start",
          index: idx,
          content_block: { type: "tool_use", id: toolId, name: item.name || "", input: {} },
        },
      });
    } else if (item.type === "reasoning") {
      state.blocks.set(idx, { type: "thinking" });
      out.push({
        event: "content_block_start",
        data: {
          type: "content_block_start",
          index: idx,
          content_block: { type: "thinking", thinking: "" },
        },
      });
    }
  } else if (t === "response.output_text.delta" || t === "response.refusal.delta") {
    const idx = typeof evt.output_index === "number" ? evt.output_index : 0;
    out.push({
      event: "content_block_delta",
      data: {
        type: "content_block_delta",
        index: idx,
        delta: { type: "text_delta", text: evt.delta || "" },
      },
    });
  } else if (t === "response.function_call_arguments.delta") {
    const idx = typeof evt.output_index === "number" ? evt.output_index : 0;
    out.push({
      event: "content_block_delta",
      data: {
        type: "content_block_delta",
        index: idx,
        delta: { type: "input_json_delta", partial_json: evt.delta || "" },
      },
    });
  } else if (t === "response.reasoning_summary_text.delta" || t === "response.reasoning_text.delta") {
    const idx = typeof evt.output_index === "number" ? evt.output_index : 0;
    out.push({
      event: "content_block_delta",
      data: {
        type: "content_block_delta",
        index: idx,
        delta: { type: "thinking_delta", thinking: evt.delta || "" },
      },
    });
  } else if (t === "response.reasoning_summary_part.done") {
    const idx = typeof evt.output_index === "number" ? evt.output_index : 0;
    out.push({
      event: "content_block_delta",
      data: {
        type: "content_block_delta",
        index: idx,
        delta: { type: "thinking_delta", thinking: "\n\n" },
      },
    });
  } else if (t === "response.output_item.done") {
    const idx = typeof evt.output_index === "number" ? evt.output_index : 0;
    if (state.blocks.has(idx)) {
      out.push({
        event: "content_block_stop",
        data: { type: "content_block_stop", index: idx },
      });
      state.blocks.delete(idx);
    }
  } else if (t === "response.completed" || t === "response.done" || t === "response.incomplete") {
    const resp = evt.response || {};
    state.finalStatus = resp.status || state.finalStatus || "completed";
    if (resp.usage) state.finalUsage = resp.usage;
    if (resp.model) state.finalModel = resp.model;
    if (resp.id && !state.messageStartSent) {
      state.msgId = `msg_${resp.id}`;
      out.push({ event: "message_start", data: buildMessageStartPayload(state) });
    }
    for (const stop of buildCloseBlocksPayloads(state)) {
      out.push({ event: "content_block_stop", data: stop });
    }
    state.blocks.clear();
    const stopReason = mapStopReason(state.finalStatus, state.hasToolCalls);
    const u = state.finalUsage || {};
    const cached = (u.input_tokens_details && u.input_tokens_details.cached_tokens) || 0;
    out.push({
      event: "message_delta",
      data: {
        type: "message_delta",
        delta: { stop_reason: stopReason, stop_sequence: null },
        usage: {
          input_tokens: u.input_tokens || 0,
          output_tokens: u.output_tokens || 0,
          cache_read_input_tokens: cached,
          cache_creation_input_tokens: 0,
        },
      },
    });
    out.push({ event: "message_stop", data: { type: "message_stop" } });
    state.finished = true;
  } else if (t === "response.failed" || t === "error") {
    if (!state.messageStartSent) {
      out.push({ event: "message_start", data: buildMessageStartPayload(state) });
    }
    for (const stop of buildCloseBlocksPayloads(state)) {
      out.push({ event: "content_block_stop", data: stop });
    }
    state.blocks.clear();
    out.push({
      event: "message_delta",
      data: {
        type: "message_delta",
        delta: { stop_reason: "error", stop_sequence: null },
        usage: { output_tokens: 0 },
      },
    });
    out.push({ event: "message_stop", data: { type: "message_stop" } });
    state.finished = true;
  }

  return out;
}

// createCodexToAnthropicTransform creates a TransformStream that converts a
// Codex SSE byte stream into Anthropic SSE bytes. Exposed for direct use and
// testing.
export function createCodexToAnthropicTransform(model) {
  const state = createStreamState(model);
  const encoder = new TextEncoder();
  const decoder = new TextDecoder();
  let buffer = "";

  function processLine(line) {
    if (!line.startsWith("data: ")) return null;
    const payload = line.slice(6).trim();
    if (!payload || payload === "[DONE]") return null;
    try {
      return JSON.parse(payload);
    } catch (_) {
      return null;
    }
  }

  return new TransformStream({
    start() {
      // Wait for response.created before emitting message_start.
    },
    transform(chunk, controller) {
      buffer += decoder.decode(chunk, { stream: true });
      let sep;
      while ((sep = buffer.indexOf("\n\n")) >= 0) {
        const raw = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        if (!raw.trim()) continue;
        for (const line of raw.split("\n")) {
          const evt = processLine(line);
          if (!evt) continue;
          for (const e of translateCodexEvent(evt, state)) {
            controller.enqueue(encoder.encode(sseEvent(e.event, e.data)));
          }
        }
      }
    },
    flush(controller) {
      if (buffer.trim()) {
        for (const line of buffer.split("\n")) {
          const evt = processLine(line);
          if (!evt) continue;
          for (const e of translateCodexEvent(evt, state)) {
            controller.enqueue(encoder.encode(sseEvent(e.event, e.data)));
          }
        }
      }
      if (!state.finished) {
        if (!state.messageStartSent) {
          controller.enqueue(
            encoder.encode(sseEvent("message_start", buildMessageStartPayload(state)))
          );
        }
        for (const stop of buildCloseBlocksPayloads(state)) {
          controller.enqueue(encoder.encode(sseEvent("content_block_stop", stop)));
        }
        state.blocks.clear();
        controller.enqueue(
          encoder.encode(
            sseEvent("message_delta", {
              type: "message_delta",
              delta: { stop_reason: "end_turn", stop_sequence: null },
              usage: { output_tokens: 0 },
            })
          )
        );
        controller.enqueue(encoder.encode(sseEvent("message_stop", { type: "message_stop" })));
      }
    },
  });
}

// codexResponseToAnthropic converts a Codex non-streaming response into an
// Anthropic Messages API response object.
export function codexResponseToAnthropic(resp, requestModel) {
  if (!resp || typeof resp !== "object") {
    return {
      id: `msg_${randomShortId(24)}`,
      type: "message",
      role: "assistant",
      model: requestModel || "gpt-5.4",
      content: [],
      stop_reason: "error",
      stop_sequence: null,
      usage: { input_tokens: 0, output_tokens: 0 },
    };
  }

  const output = Array.isArray(resp.output) ? resp.output : [];
  const content = [];
  let hasToolCalls = false;

  for (const item of output) {
    if (!item) continue;
    if (item.type === "reasoning") {
      let text = "";
      if (Array.isArray(item.summary)) {
        text = item.summary
          .filter((p) => p && p.type === "summary_text")
          .map((p) => p.text || "")
          .join("\n\n");
      }
      content.push({ type: "thinking", thinking: text });
    } else if (item.type === "message") {
      const parts = Array.isArray(item.content) ? item.content : [];
      for (const part of parts) {
        if (!part) continue;
        if (part.type === "output_text") {
          content.push({ type: "text", text: part.text || "" });
        } else if (part.type === "refusal") {
          content.push({ type: "text", text: part.refusal || "" });
        }
      }
    } else if (item.type === "function_call") {
      hasToolCalls = true;
      let input = {};
      if (typeof item.arguments === "string") {
        try { input = JSON.parse(item.arguments); } catch (_) { input = {}; }
      } else if (item.arguments && typeof item.arguments === "object") {
        input = item.arguments;
      }
      content.push({
        type: "tool_use",
        id: item.call_id || item.id || `toolu_${randomShortId(24)}`,
        name: item.name || "",
        input,
      });
    }
  }

  const status = resp.status || "completed";
  const stopReason = mapStopReason(status, hasToolCalls);
  const usage = resp.usage || {};
  const cached = (usage.input_tokens_details && usage.input_tokens_details.cached_tokens) || 0;

  return {
    id: resp.id ? `msg_${resp.id}` : `msg_${randomShortId(24)}`,
    type: "message",
    role: "assistant",
    model: resp.model || requestModel || "gpt-5.4",
    content,
    stop_reason: stopReason,
    stop_sequence: null,
    usage: {
      input_tokens: usage.input_tokens || 0,
      output_tokens: usage.output_tokens || 0,
      cache_read_input_tokens: cached,
      cache_creation_input_tokens: 0,
    },
  };
}

// === Main handler ===

// handleCodexMessages handles an Anthropic Messages API request, translates it
// to the OpenAI Codex Responses API, and returns the result (streaming or
// non-streaming).
export async function handleCodexMessages(request, _env) {
  // 1. Parse auth
  const authHeader = request.headers.get("Authorization") || "";
  const auth = parseCodexAuth(authHeader);
  if (!auth) {
    return formatAnthropicError(
      401,
      "Invalid or missing Codex authorization. Expected 'codex:<access_token>:<account_id>'.",
      "authentication_error"
    );
  }

  // 2. Read body
  let body;
  try {
    body = await request.json();
  } catch (_) {
    return formatAnthropicError(400, "Invalid JSON body", "invalid_request_error");
  }
  if (!body || typeof body !== "object") {
    return formatAnthropicError(400, "Request body must be a JSON object", "invalid_request_error");
  }

  const model = body.model || "gpt-5.4";

  // 3. Convert to Codex body
  let codexBody;
  try {
    codexBody = anthropicToCodexResponses(body, model);
  } catch (err) {
    return formatAnthropicError(
      400,
      (err && err.message) || "Failed to build Codex request",
      "invalid_request_error"
    );
  }

  // Add Codex-specific fields
  codexBody.include = ["reasoning.encrypted_content"];
  if (!codexBody.text) codexBody.text = { verbosity: "low" };

  // Service tier from header
  const serviceTier = request.headers.get("X-Codex-Service-Tier");
  if (serviceTier) codexBody.service_tier = serviceTier;

  // Verbosity from header (overrides default)
  const verbosity = request.headers.get("X-Codex-Text-Verbosity");
  if (verbosity) codexBody.text = { verbosity };

  // Prompt cache key from metadata or session header
  if (body.metadata && typeof body.metadata.user_id === "string") {
    codexBody.prompt_cache_key = body.metadata.user_id.slice(0, 64);
  } else {
    const sessionHdr = request.headers.get("X-Session-Id");
    if (sessionHdr) codexBody.prompt_cache_key = sessionHdr.slice(0, 64);
  }

  // 4. Build headers
  const sessionId =
    request.headers.get("X-Session-Id") || codexBody.prompt_cache_key || randomShortId(16);
  const headers = codexHeaders({}, auth.accessToken, auth.accountId, sessionId);

  // 5. POST with retries
  const url = `${auth.baseUrl}${CODEX_RESPONSES_PATH}`;
  let response = null;

  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    if (request.signal && request.signal.aborted) {
      return new Response("Client disconnected", { status: 499 });
    }
    try {
      response = await fetch(url, {
        method: "POST",
        headers,
        body: JSON.stringify(codexBody),
        signal: request.signal,
      });
      if (response.ok) break;
      if (!isRetryableStatus(response.status)) break;
      const cloned = response.clone();
      const text = await cloned.text();
      if (!isRetryableErrorText(text)) break;
      if (attempt < MAX_RETRIES) {
        await sleep(getRetryDelay(response, attempt));
        continue;
      }
    } catch (err) {
      if (err && (err.name === "AbortError" || (request.signal && request.signal.aborted))) {
        return new Response("Client disconnected", { status: 499 });
      }
      const msg = (err && err.message) || String(err);
      if (/usage.?limit/i.test(msg)) {
        return formatAnthropicError(429, msg);
      }
      if (attempt < MAX_RETRIES) {
        await sleep(getRetryDelay(null, attempt));
        continue;
      }
      return formatAnthropicError(500, `Network error: ${msg}`);
    }
  }

  if (!response) {
    return formatAnthropicError(500, "No response from Codex backend");
  }

  // 6. Handle response
  if (!response.ok) {
    const txt = await response.text();
    const errBody = safeJsonParse(txt);
    const friendly =
      parseCodexErrorBody(errBody) ||
      (errBody && errBody.error && errBody.error.message) ||
      txt ||
      `Upstream error ${response.status}`;
    return formatAnthropicError(response.status, friendly);
  }

  if (body.stream) {
    const transform = createCodexToAnthropicTransform(model);
    const transformed = response.body.pipeThrough(transform);
    return new Response(transformed, {
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        Connection: "keep-alive",
      },
    });
  }

  const data = await response.json();
  const anthropicData = codexResponseToAnthropic(data, model);
  return new Response(JSON.stringify(anthropicData), {
    headers: { "Content-Type": "application/json" },
  });
}
