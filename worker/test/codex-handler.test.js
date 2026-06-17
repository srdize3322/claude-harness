// Unit tests for codex-handler.js
// Run with: node --test worker/test/codex-handler.test.js

import { test } from "node:test";
import assert from "node:assert/strict";
import {
  detectCodexMode,
  parseCodexAuth,
  codexHeaders,
  mapStopReason,
  mapStatusToErrorType,
  sseEvent,
  formatAnthropicError,
  parseCodexErrorBody,
  translateCodexEvent,
  createStreamState,
  codexResponseToAnthropic,
  createCodexToAnthropicTransform,
  handleCodexMessages,
  resolveCodexBaseUrl,
} from "../src/codex-handler.js";

test("detectCodexMode: detects 'codex:' prefix", () => {
  assert.equal(detectCodexMode("codex:abc:123"), true);
  assert.equal(detectCodexMode("codex:long-token:uuid-here"), true);
});

test("detectCodexMode: rejects non-codex auth", () => {
  assert.equal(detectCodexMode("sk-abc-123"), false);
  assert.equal(detectCodexMode(""), false);
  assert.equal(detectCodexMode(null), false);
  assert.equal(detectCodexMode(undefined), false);
  assert.equal(detectCodexMode("Bearer xyz"), false);
});

test("parseCodexAuth: parses valid header", () => {
  const result = parseCodexAuth("codex:eyJhbGciOiJSUzI1NiJ9.abc:b86f7cb8-e91b-452a-8630-3f869bccfff0");
  assert(result !== null);
  assert.equal(result.accessToken, "eyJhbGciOiJSUzI1NiJ9.abc");
  assert.equal(result.accountId, "b86f7cb8-e91b-452a-8630-3f869bccfff0");
  // baseUrl is no longer in parseCodexAuth result; use resolveCodexBaseUrl instead
});

test("parseCodexAuth: rejects non-codex header", () => {
  assert.equal(parseCodexAuth("sk-abc:123"), null);
  assert.equal(parseCodexAuth(""), null);
  assert.equal(parseCodexAuth("codex:"), null);
  assert.equal(parseCodexAuth("codex:noaccountid"), null);
});

test("parseCodexAuth: handles access tokens with colons (JWTs)", () => {
  // A real JWT has dots but no colons; the format is "codex:<jwt>:<uuid>".
  // The parser uses lastIndexOf(":") to find the boundary.
  const jwt = "eyJhbGciOiJSUzI1NiIsImtpZCI6IjE5MzQ0ZTY1LWIwNDQ0OTdkLWEwMDctNDQ2MS04NjExLTNkNzM3NTM2OTVkIn0.fakefake";
  const accountId = "b86f7cb8-e91b-452a-8630-3f869bccfff0";
  const result = parseCodexAuth(`codex:${jwt}:${accountId}`);
  assert(result !== null);
  assert.equal(result.accountId, accountId);
  assert.equal(result.accessToken, jwt);
});

test("codexHeaders: builds correct headers", () => {
  const h = codexHeaders({}, "tok-abc", "acct-xyz");
  assert.equal(h["Content-Type"], "application/json");
  assert.equal(h["Authorization"], "Bearer tok-abc");
  assert.equal(h["chatgpt-account-id"], "acct-xyz");
  assert.equal(h["OpenAI-Beta"], "responses=experimental");
  assert.equal(h.accept, "text/event-stream");
});

test("codexHeaders: adds session_id when provided", () => {
  const h = codexHeaders({}, "tok", "acct", "session-123");
  assert.equal(h.session_id, "session-123");
});

test("codexHeaders: merges model headers (last write wins)", () => {
  const h = codexHeaders({ "X-Custom": "value" }, "tok", "acct");
  assert.equal(h["X-Custom"], "value");
  // model headers are written AFTER base headers, so they win on conflict.
  // We don't protect against Authorization override here (the Worker trusts
  // the wrapper to send a well-formed header).
  assert.equal(h["Authorization"], "Bearer tok");
});

test("mapStopReason: completed with tool calls", () => {
  assert.equal(mapStopReason("completed", true), "tool_use");
});

test("mapStopReason: completed without tool calls", () => {
  assert.equal(mapStopReason("completed", false), "end_turn");
});

test("mapStopReason: incomplete -> max_tokens", () => {
  assert.equal(mapStopReason("incomplete", false), "max_tokens");
});

test("mapStopReason: failed -> error", () => {
  assert.equal(mapStopReason("failed", false), "error");
});

test("mapStopReason: cancelled -> end_turn", () => {
  assert.equal(mapStopReason("cancelled", false), "end_turn");
});

test("mapStopReason: unknown -> end_turn", () => {
  assert.equal(mapStopReason("weird", false), "end_turn");
});

// === SSE helpers ===

test("sseEvent: formats object data as JSON", () => {
  const result = sseEvent("ping", { type: "ping" });
  assert.equal(result, 'event: ping\ndata: {"type":"ping"}\n\n');
});

test("sseEvent: passes through string data", () => {
  const result = sseEvent("ping", "raw-text");
  assert.equal(result, 'event: ping\ndata: raw-text\n\n');
});

// === Error mapping ===

test("mapStatusToErrorType: 401 -> authentication_error", () => {
  assert.equal(mapStatusToErrorType(401), "authentication_error");
});

test("mapStatusToErrorType: 403 -> permission_error", () => {
  assert.equal(mapStatusToErrorType(403), "permission_error");
});

test("mapStatusToErrorType: 404 -> not_found_error", () => {
  assert.equal(mapStatusToErrorType(404), "not_found_error");
});

test("mapStatusToErrorType: 429 -> rate_limit_error", () => {
  assert.equal(mapStatusToErrorType(429), "rate_limit_error");
});

test("mapStatusToErrorType: 5xx -> api_error", () => {
  assert.equal(mapStatusToErrorType(500), "api_error");
  assert.equal(mapStatusToErrorType(502), "api_error");
  assert.equal(mapStatusToErrorType(503), "api_error");
  assert.equal(mapStatusToErrorType(504), "api_error");
});

test("mapStatusToErrorType: 4xx -> invalid_request_error", () => {
  assert.equal(mapStatusToErrorType(400), "invalid_request_error");
  assert.equal(mapStatusToErrorType(422), "invalid_request_error");
});

test("formatAnthropicError: builds error response with auto-detected type", async () => {
  const res = formatAnthropicError(401, "bad token");
  assert.equal(res.status, 401);
  assert.equal(res.headers.get("Content-Type"), "application/json");
  const body = await res.json();
  assert.deepEqual(body, {
    type: "error",
    error: { type: "authentication_error", message: "bad token" },
  });
});

test("formatAnthropicError: honors explicit error type override", async () => {
  const res = formatAnthropicError(500, "boom", "overloaded_error");
  const body = await res.json();
  assert.equal(body.error.type, "overloaded_error");
  assert.equal(body.error.message, "boom");
});

test("formatAnthropicError: handles non-string messages", async () => {
  const res = formatAnthropicError(400, null);
  const body = await res.json();
  assert.equal(body.error.message, "");
});

// === parseCodexErrorBody ===

test("parseCodexErrorBody: usage_limit_reached with resets_at", () => {
  const future = Math.floor(Date.now() / 1000) + 1800;
  const msg = parseCodexErrorBody({
    error: { code: "usage_limit_reached", plan_type: "plus", resets_at: future },
  });
  assert.match(msg, /usage limit \(plus plan\)/);
  assert.match(msg, /Try again in ~30 min/);
});

test("parseCodexErrorBody: usage_limit_reached without resets_at", () => {
  const msg = parseCodexErrorBody({
    error: { code: "usage_limit_reached", plan_type: "pro" },
  });
  assert.equal(msg, "You have hit your ChatGPT usage limit (pro plan).");
});

test("parseCodexErrorBody: usage_not_included", () => {
  const msg = parseCodexErrorBody({ error: { code: "usage_not_included", plan_type: "free" } });
  assert.match(msg, /usage limit \(free plan\)/);
});

test("parseCodexErrorBody: rate_limit_exceeded", () => {
  const msg = parseCodexErrorBody({ error: { code: "rate_limit_exceeded" } });
  assert.equal(msg, "Rate limit exceeded. Please slow down and try again.");
});

test("parseCodexErrorBody: invalid_api_key", () => {
  const msg = parseCodexErrorBody({ error: { code: "invalid_api_key" } });
  assert.match(msg, /invalid or expired/);
});

test("parseCodexErrorBody: falls back to message field", () => {
  const msg = parseCodexErrorBody({ error: { message: "weird error" } });
  assert.equal(msg, "weird error");
});

test("parseCodexErrorBody: handles string body", () => {
  const msg = parseCodexErrorBody('{"error":{"message":"hi"}}');
  assert.equal(msg, "hi");
});

test("parseCodexErrorBody: handles plain string when not JSON", () => {
  assert.equal(parseCodexErrorBody("plain text"), "plain text");
});

test("parseCodexErrorBody: returns null for null/empty/object-without-error", () => {
  assert.equal(parseCodexErrorBody(null), null);
  assert.equal(parseCodexErrorBody(undefined), null);
  assert.equal(parseCodexErrorBody({}), null);
  assert.equal(parseCodexErrorBody({ foo: 1 }), null);
});

// === translateCodexEvent: lifecycle ===

test("translateCodexEvent: response.created emits message_start", () => {
  const state = createStreamState("gpt-5.4");
  const events = translateCodexEvent(
    { type: "response.created", response: { id: "resp_1", model: "gpt-5.4" } },
    state
  );
  assert.equal(events.length, 1);
  assert.equal(events[0].event, "message_start");
  assert.equal(events[0].data.message.id, "msg_resp_1");
  assert.equal(events[0].data.message.model, "gpt-5.4");
  assert.equal(events[0].data.message.role, "assistant");
  assert.equal(state.messageStartSent, true);
});

test("translateCodexEvent: response.created does not re-emit message_start", () => {
  const state = createStreamState("gpt-5.4");
  translateCodexEvent({ type: "response.created", response: { id: "resp_1" } }, state);
  const events = translateCodexEvent({ type: "response.created", response: { id: "resp_2" } }, state);
  assert.equal(events.length, 0);
});

// === translateCodexEvent: content blocks ===

test("translateCodexEvent: output_item.added (message) opens text block", () => {
  const state = createStreamState("gpt-5.4");
  state.messageStartSent = true;
  const events = translateCodexEvent(
    { type: "response.output_item.added", output_index: 0, item: { type: "message" } },
    state
  );
  assert.equal(events.length, 1);
  assert.equal(events[0].event, "content_block_start");
  assert.equal(events[0].data.index, 0);
  assert.equal(events[0].data.content_block.type, "text");
  assert.equal(events[0].data.content_block.text, "");
  assert.equal(state.blocks.get(0).type, "text");
});

test("translateCodexEvent: output_item.added (function_call) opens tool_use block", () => {
  const state = createStreamState("gpt-5.4");
  state.messageStartSent = true;
  const events = translateCodexEvent(
    {
      type: "response.output_item.added",
      output_index: 1,
      item: { type: "function_call", name: "get_weather", call_id: "call_1", id: "fc_1" },
    },
    state
  );
  assert.equal(events[0].event, "content_block_start");
  assert.equal(events[0].data.index, 1);
  assert.equal(events[0].data.content_block.type, "tool_use");
  assert.equal(events[0].data.content_block.id, "call_1");
  assert.equal(events[0].data.content_block.name, "get_weather");
  assert.deepEqual(events[0].data.content_block.input, {});
  assert.equal(state.hasToolCalls, true);
});

test("translateCodexEvent: output_item.added (function_call) generates fallback id", () => {
  const state = createStreamState("gpt-5.4");
  state.messageStartSent = true;
  const events = translateCodexEvent(
    {
      type: "response.output_item.added",
      output_index: 0,
      item: { type: "function_call", name: "no_id_tool" },
    },
    state
  );
  assert.match(events[0].data.content_block.id, /^toolu_/);
});

test("translateCodexEvent: output_item.added (reasoning) opens thinking block", () => {
  const state = createStreamState("gpt-5.4");
  state.messageStartSent = true;
  const events = translateCodexEvent(
    { type: "response.output_item.added", output_index: 0, item: { type: "reasoning" } },
    state
  );
  assert.equal(events[0].event, "content_block_start");
  assert.equal(events[0].data.content_block.type, "thinking");
  assert.equal(events[0].data.content_block.thinking, "");
});

// === translateCodexEvent: deltas ===

test("translateCodexEvent: output_text.delta emits text_delta", () => {
  const state = createStreamState("gpt-5.4");
  state.messageStartSent = true;
  state.blocks.set(0, { type: "text" });
  const events = translateCodexEvent(
    { type: "response.output_text.delta", output_index: 0, delta: "hello" },
    state
  );
  assert.equal(events[0].event, "content_block_delta");
  assert.equal(events[0].data.index, 0);
  assert.deepEqual(events[0].data.delta, { type: "text_delta", text: "hello" });
});

test("translateCodexEvent: refusal.delta emits text_delta", () => {
  const state = createStreamState("gpt-5.4");
  state.messageStartSent = true;
  state.blocks.set(0, { type: "text" });
  const events = translateCodexEvent(
    { type: "response.refusal.delta", output_index: 0, delta: "I cannot" },
    state
  );
  assert.deepEqual(events[0].data.delta, { type: "text_delta", text: "I cannot" });
});

test("translateCodexEvent: function_call_arguments.delta emits input_json_delta", () => {
  const state = createStreamState("gpt-5.4");
  state.messageStartSent = true;
  state.blocks.set(0, { type: "tool_use" });
  const events = translateCodexEvent(
    { type: "response.function_call_arguments.delta", output_index: 0, delta: '{"city":' },
    state
  );
  assert.equal(events[0].event, "content_block_delta");
  assert.deepEqual(events[0].data.delta, {
    type: "input_json_delta",
    partial_json: '{"city":',
  });
});

test("translateCodexEvent: reasoning_summary_text.delta emits thinking_delta", () => {
  const state = createStreamState("gpt-5.4");
  state.messageStartSent = true;
  state.blocks.set(0, { type: "thinking" });
  const events = translateCodexEvent(
    { type: "response.reasoning_summary_text.delta", output_index: 0, delta: "Let me think" },
    state
  );
  assert.deepEqual(events[0].data.delta, { type: "thinking_delta", thinking: "Let me think" });
});

test("translateCodexEvent: reasoning_text.delta emits thinking_delta", () => {
  const state = createStreamState("gpt-5.4");
  state.messageStartSent = true;
  state.blocks.set(0, { type: "thinking" });
  const events = translateCodexEvent(
    { type: "response.reasoning_text.delta", output_index: 0, delta: "more" },
    state
  );
  assert.deepEqual(events[0].data.delta, { type: "thinking_delta", thinking: "more" });
});

test("translateCodexEvent: reasoning_summary_part.done emits separator", () => {
  const state = createStreamState("gpt-5.4");
  state.messageStartSent = true;
  state.blocks.set(0, { type: "thinking" });
  const events = translateCodexEvent(
    { type: "response.reasoning_summary_part.done", output_index: 0 },
    state
  );
  assert.deepEqual(events[0].data.delta, { type: "thinking_delta", thinking: "\n\n" });
});

// === translateCodexEvent: close blocks ===

test("translateCodexEvent: output_item.done closes block", () => {
  const state = createStreamState("gpt-5.4");
  state.messageStartSent = true;
  state.blocks.set(0, { type: "text" });
  const events = translateCodexEvent({ type: "response.output_item.done", output_index: 0 }, state);
  assert.equal(events[0].event, "content_block_stop");
  assert.equal(events[0].data.index, 0);
  assert.equal(state.blocks.has(0), false);
});

test("translateCodexEvent: output_item.done on unknown index is a no-op", () => {
  const state = createStreamState("gpt-5.4");
  state.messageStartSent = true;
  const events = translateCodexEvent({ type: "response.output_item.done", output_index: 99 }, state);
  assert.equal(events.length, 0);
});

// === translateCodexEvent: terminators ===

test("translateCodexEvent: response.completed emits stop + message_delta + message_stop", () => {
  const state = createStreamState("gpt-5.4");
  state.messageStartSent = true;
  state.blocks.set(0, { type: "text" });
  const events = translateCodexEvent(
    {
      type: "response.completed",
      response: {
        status: "completed",
        usage: { input_tokens: 100, output_tokens: 50, input_tokens_details: { cached_tokens: 20 } },
      },
    },
    state
  );
  assert.equal(events.length, 3);
  assert.equal(events[0].event, "content_block_stop");
  assert.equal(events[1].event, "message_delta");
  assert.equal(events[1].data.delta.stop_reason, "end_turn");
  assert.equal(events[1].data.usage.input_tokens, 100);
  assert.equal(events[1].data.usage.output_tokens, 50);
  assert.equal(events[1].data.usage.cache_read_input_tokens, 20);
  assert.equal(events[1].data.usage.cache_creation_input_tokens, 0);
  assert.equal(events[2].event, "message_stop");
  assert.equal(state.finished, true);
});

test("translateCodexEvent: response.completed with tool calls -> tool_use stop_reason", () => {
  const state = createStreamState("gpt-5.4");
  state.messageStartSent = true;
  state.hasToolCalls = true;
  const events = translateCodexEvent(
    { type: "response.completed", response: { status: "completed", usage: {} } },
    state
  );
  const md = events.find((e) => e.event === "message_delta");
  assert.equal(md.data.delta.stop_reason, "tool_use");
});

test("translateCodexEvent: response.incomplete -> max_tokens", () => {
  const state = createStreamState("gpt-5.4");
  state.messageStartSent = true;
  const events = translateCodexEvent(
    { type: "response.incomplete", response: { status: "incomplete", usage: {} } },
    state
  );
  const md = events.find((e) => e.event === "message_delta");
  assert.equal(md.data.delta.stop_reason, "max_tokens");
});

test("translateCodexEvent: response.done is normalized like response.completed", () => {
  const state = createStreamState("gpt-5.4");
  state.messageStartSent = true;
  const events = translateCodexEvent(
    { type: "response.done", response: { status: "completed", usage: {} } },
    state
  );
  const stop = events.find((e) => e.event === "message_stop");
  assert.ok(stop, "should emit message_stop");
  assert.equal(state.finished, true);
});

test("translateCodexEvent: response.completed closes all open blocks first", () => {
  const state = createStreamState("gpt-5.4");
  state.messageStartSent = true;
  state.blocks.set(0, { type: "text" });
  state.blocks.set(1, { type: "tool_use" });
  const events = translateCodexEvent(
    { type: "response.completed", response: { status: "completed", usage: {} } },
    state
  );
  const stops = events.filter((e) => e.event === "content_block_stop");
  assert.equal(stops.length, 2);
});

test("translateCodexEvent: response.failed emits error stop_reason and message_stop", () => {
  const state = createStreamState("gpt-5.4");
  state.messageStartSent = true;
  state.blocks.set(0, { type: "text" });
  const events = translateCodexEvent(
    { type: "response.failed", response: { error: { message: "boom" } } },
    state
  );
  const stop = events.find((e) => e.event === "content_block_stop");
  assert.ok(stop, "should close open blocks");
  const md = events.find((e) => e.event === "message_delta");
  assert.equal(md.data.delta.stop_reason, "error");
  assert.equal(state.finished, true);
});

test("translateCodexEvent: top-level error emits error stop_reason", () => {
  const state = createStreamState("gpt-5.4");
  state.messageStartSent = true;
  const events = translateCodexEvent(
    { type: "error", code: "server_error", message: "boom" },
    state
  );
  const md = events.find((e) => e.event === "message_delta");
  assert.equal(md.data.delta.stop_reason, "error");
});

test("translateCodexEvent: unknown event type returns empty", () => {
  const state = createStreamState("gpt-5.4");
  state.messageStartSent = true;
  const events = translateCodexEvent({ type: "weird.unknown" }, state);
  assert.equal(events.length, 0);
});

// === createCodexToAnthropicTransform: integration ===

test("createCodexToAnthropicTransform: full text stream", async () => {
  const codexEvents = [
    { type: "response.created", response: { id: "resp_1", model: "gpt-5.4" } },
    { type: "response.output_item.added", output_index: 0, item: { type: "message" } },
    { type: "response.output_text.delta", output_index: 0, delta: "hello" },
    { type: "response.output_text.delta", output_index: 0, delta: " world" },
    { type: "response.output_item.done", output_index: 0 },
    {
      type: "response.completed",
      response: { status: "completed", usage: { input_tokens: 5, output_tokens: 2 } },
    },
  ];
  const encoder = new TextEncoder();
  const input = new ReadableStream({
    start(controller) {
      for (const evt of codexEvents) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(evt)}\n\n`));
      }
      controller.close();
    },
  });
  const out = await new Response(input.pipeThrough(createCodexToAnthropicTransform("gpt-5.4"))).text();
  // Verify the SSE structure
  assert.match(out, /event: message_start/);
  assert.match(out, /event: content_block_start/);
  assert.match(out, /"text":"hello"/);
  assert.match(out, /"text":" world"/);
  assert.match(out, /event: content_block_stop/);
  assert.match(out, /event: message_delta/);
  assert.match(out, /"stop_reason":"end_turn"/);
  assert.match(out, /event: message_stop/);
});

test("createCodexToAnthropicTransform: tool call stream", async () => {
  const codexEvents = [
    { type: "response.created", response: { id: "resp_1", model: "gpt-5.4" } },
    {
      type: "response.output_item.added",
      output_index: 0,
      item: { type: "function_call", name: "get_weather", call_id: "call_1", id: "fc_1" },
    },
    { type: "response.function_call_arguments.delta", output_index: 0, delta: '{"city":' },
    { type: "response.function_call_arguments.delta", output_index: 0, delta: '"Paris"}' },
    { type: "response.output_item.done", output_index: 0 },
    {
      type: "response.completed",
      response: { status: "completed", usage: { input_tokens: 5, output_tokens: 2 } },
    },
  ];
  const encoder = new TextEncoder();
  const input = new ReadableStream({
    start(controller) {
      for (const evt of codexEvents) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(evt)}\n\n`));
      }
      controller.close();
    },
  });
  const out = await new Response(input.pipeThrough(createCodexToAnthropicTransform("gpt-5.4"))).text();
  assert.match(out, /"type":"tool_use"/);
  assert.match(out, /"id":"call_1"/);
  assert.match(out, /"name":"get_weather"/);
  assert.match(out, /"type":"input_json_delta"/);
  assert.match(out, /"partial_json":"\{\\"city\\":"/);
  assert.match(out, /"stop_reason":"tool_use"/);
});

test("createCodexToAnthropicTransform: handles [DONE] sentinel", async () => {
  const encoder = new TextEncoder();
  const events = [
    { type: "response.created", response: { id: "resp_1" } },
    { type: "response.output_item.added", output_index: 0, item: { type: "message" } },
    { type: "response.output_text.delta", output_index: 0, delta: "ok" },
    { type: "response.output_item.done", output_index: 0 },
    { type: "response.completed", response: { status: "completed", usage: {} } },
  ];
  let chunk = "";
  for (const evt of events) chunk += `data: ${JSON.stringify(evt)}\n\n`;
  chunk += "data: [DONE]\n\n";
  const input = new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  const out = await new Response(input.pipeThrough(createCodexToAnthropicTransform("gpt-5.4"))).text();
  assert.match(out, /event: message_stop/);
});

test("createCodexToAnthropicTransform: handles split chunks", async () => {
  const encoder = new TextEncoder();
  const full =
    `data: ${JSON.stringify({ type: "response.created", response: { id: "r1" } })}\n\n` +
    `data: ${JSON.stringify({ type: "response.output_item.added", output_index: 0, item: { type: "message" } })}\n\n` +
    `data: ${JSON.stringify({ type: "response.output_text.delta", output_index: 0, delta: "ok" })}\n\n` +
    `data: ${JSON.stringify({ type: "response.output_item.done", output_index: 0 })}\n\n` +
    `data: ${JSON.stringify({ type: "response.completed", response: { status: "completed", usage: {} } })}\n\n`;
  // Send it in many small chunks
  const input = new ReadableStream({
    start(controller) {
      for (let i = 0; i < full.length; i += 7) {
        controller.enqueue(encoder.encode(full.slice(i, i + 7)));
      }
      controller.close();
    },
  });
  const out = await new Response(input.pipeThrough(createCodexToAnthropicTransform("gpt-5.4"))).text();
  assert.match(out, /event: message_start/);
  assert.match(out, /"text":"ok"/);
  assert.match(out, /event: message_stop/);
});

test("createCodexToAnthropicTransform: handles truncated stream gracefully", async () => {
  const encoder = new TextEncoder();
  // Stream that never emits response.completed
  const input = new ReadableStream({
    start(controller) {
      controller.enqueue(
        encoder.encode(
          `data: ${JSON.stringify({ type: "response.created", response: { id: "r1" } })}\n\n`
        )
      );
      controller.close();
    },
  });
  const out = await new Response(input.pipeThrough(createCodexToAnthropicTransform("gpt-5.4"))).text();
  assert.match(out, /event: message_start/);
  assert.match(out, /event: message_stop/);
});

// === codexResponseToAnthropic (non-streaming) ===

test("codexResponseToAnthropic: simple text response", () => {
  const out = codexResponseToAnthropic(
    {
      id: "resp_1",
      model: "gpt-5.4",
      status: "completed",
      output: [
        { type: "message", role: "assistant", content: [{ type: "output_text", text: "hello" }] },
      ],
      usage: { input_tokens: 10, output_tokens: 5 },
    },
    "gpt-5.4"
  );
  assert.equal(out.id, "msg_resp_1");
  assert.equal(out.type, "message");
  assert.equal(out.role, "assistant");
  assert.equal(out.model, "gpt-5.4");
  assert.equal(out.stop_reason, "end_turn");
  assert.equal(out.usage.input_tokens, 10);
  assert.equal(out.usage.output_tokens, 5);
  assert.equal(out.content.length, 1);
  assert.equal(out.content[0].type, "text");
  assert.equal(out.content[0].text, "hello");
});

test("codexResponseToAnthropic: reasoning + text", () => {
  const out = codexResponseToAnthropic(
    {
      id: "resp_1",
      model: "gpt-5.4",
      status: "completed",
      output: [
        { type: "reasoning", summary: [{ type: "summary_text", text: "Let me think..." }] },
        { type: "message", role: "assistant", content: [{ type: "output_text", text: "answer" }] },
      ],
      usage: { input_tokens: 10, output_tokens: 20 },
    },
    "gpt-5.4"
  );
  assert.equal(out.content.length, 2);
  assert.equal(out.content[0].type, "thinking");
  assert.equal(out.content[0].thinking, "Let me think...");
  assert.equal(out.content[1].type, "text");
  assert.equal(out.content[1].text, "answer");
});

test("codexResponseToAnthropic: function_call becomes tool_use", () => {
  const out = codexResponseToAnthropic(
    {
      id: "resp_1",
      model: "gpt-5.4",
      status: "completed",
      output: [
        {
          type: "function_call",
          name: "get_weather",
          call_id: "call_1",
          arguments: '{"city":"Paris"}',
        },
      ],
      usage: { input_tokens: 10, output_tokens: 5 },
    },
    "gpt-5.4"
  );
  assert.equal(out.stop_reason, "tool_use");
  assert.equal(out.content.length, 1);
  assert.equal(out.content[0].type, "tool_use");
  assert.equal(out.content[0].id, "call_1");
  assert.equal(out.content[0].name, "get_weather");
  assert.deepEqual(out.content[0].input, { city: "Paris" });
});

test("codexResponseToAnthropic: function_call with bad JSON keeps empty input", () => {
  const out = codexResponseToAnthropic(
    {
      id: "resp_1",
      status: "completed",
      output: [
        { type: "function_call", name: "f", call_id: "c1", arguments: "{invalid" },
      ],
      usage: {},
    },
    "gpt-5.4"
  );
  assert.deepEqual(out.content[0].input, {});
});

test("codexResponseToAnthropic: function_call with object arguments", () => {
  const out = codexResponseToAnthropic(
    {
      id: "resp_1",
      status: "completed",
      output: [
        { type: "function_call", name: "f", call_id: "c1", arguments: { x: 1 } },
      ],
      usage: {},
    },
    "gpt-5.4"
  );
  assert.deepEqual(out.content[0].input, { x: 1 });
});

test("codexResponseToAnthropic: incomplete -> max_tokens", () => {
  const out = codexResponseToAnthropic(
    { id: "r", status: "incomplete", output: [], usage: {} },
    "gpt-5.4"
  );
  assert.equal(out.stop_reason, "max_tokens");
});

test("codexResponseToAnthropic: failed -> error", () => {
  const out = codexResponseToAnthropic(
    { id: "r", status: "failed", output: [], usage: {} },
    "gpt-5.4"
  );
  assert.equal(out.stop_reason, "error");
});

test("codexResponseToAnthropic: refusal becomes text", () => {
  const out = codexResponseToAnthropic(
    {
      id: "r",
      status: "completed",
      output: [
        { type: "message", role: "assistant", content: [{ type: "refusal", refusal: "I won't" }] },
      ],
      usage: {},
    },
    "gpt-5.4"
  );
  assert.equal(out.content[0].type, "text");
  assert.equal(out.content[0].text, "I won't");
});

test("codexResponseToAnthropic: cache_read_input_tokens surfaced", () => {
  const out = codexResponseToAnthropic(
    {
      id: "r",
      status: "completed",
      output: [],
      usage: { input_tokens: 100, input_tokens_details: { cached_tokens: 30 } },
    },
    "gpt-5.4"
  );
  assert.equal(out.usage.cache_read_input_tokens, 30);
  assert.equal(out.usage.cache_creation_input_tokens, 0);
});

test("codexResponseToAnthropic: handles null input", () => {
  const out = codexResponseToAnthropic(null, "gpt-5.4");
  assert.equal(out.stop_reason, "error");
  assert.deepEqual(out.content, []);
});

// === handleCodexMessages (end-to-end with mock fetch) ===

function makeRequest(body, options = {}) {
  const headers = {
    "Content-Type": "application/json",
    Authorization: "codex:tok-abc:b86f7cb8-e91b-452a-8630-3f869bccfff0",
    ...(options.headers || {}),
  };
  return new Request("https://example.com/v1/messages", {
    method: "POST",
    headers,
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

function mockFetch(responses) {
  const original = globalThis.fetch;
  let i = 0;
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    calls.push({ url: String(url), opts });
    if (i >= responses.length) {
      throw new Error(`mockFetch: no response queued (call #${i + 1})`);
    }
    const r = responses[i++];
    if (r instanceof Error) throw r;
    return r;
  };
  return {
    calls,
    restore: () => {
      globalThis.fetch = original;
    },
  };
}

function makeSSE(events) {
  const encoder = new TextEncoder();
  let payload = "";
  for (const evt of events) payload += `data: ${JSON.stringify(evt)}\n\n`;
  return new Response(encoder.encode(payload), {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

test("handleCodexMessages: streams SSE response", async () => {
  const m = mockFetch([
    makeSSE([
      { type: "response.created", response: { id: "resp_1", model: "gpt-5.4" } },
      { type: "response.output_item.added", output_index: 0, item: { type: "message" } },
      { type: "response.output_text.delta", output_index: 0, delta: "hello" },
      { type: "response.output_item.done", output_index: 0 },
      { type: "response.completed", response: { status: "completed", usage: { input_tokens: 5, output_tokens: 1 } } },
    ]),
  ]);
  try {
    const res = await handleCodexMessages(
      makeRequest({ model: "gpt-5.4", stream: true, messages: [{ role: "user", content: "hi" }] }),
      {}
    );
    assert.equal(res.status, 200);
    assert.equal(res.headers.get("Content-Type"), "text/event-stream");
    const text = await res.text();
    assert.match(text, /event: message_start/);
    assert.match(text, /event: content_block_start/);
    assert.match(text, /"text":"hello"/);
    assert.match(text, /event: message_stop/);
    // Verify fetch was called with the right URL and auth
    assert.equal(m.calls.length, 1);
    assert.match(m.calls[0].url, /\/codex\/responses$/);
    const sent = JSON.parse(m.calls[0].opts.body);
    assert.equal(sent.model, "gpt-5.4");
    assert.equal(sent.stream, true);
    assert.equal(sent.include[0], "reasoning.encrypted_content");
    const reqHeaders = new Headers(m.calls[0].opts.headers);
    assert.equal(reqHeaders.get("Authorization"), "Bearer tok-abc");
    assert.equal(reqHeaders.get("chatgpt-account-id"), "b86f7cb8-e91b-452a-8630-3f869bccfff0");
    assert.equal(reqHeaders.get("OpenAI-Beta"), "responses=experimental");
  } finally {
    m.restore();
  }
});

test("handleCodexMessages: non-streaming JSON response", async () => {
  const m = mockFetch([
    new Response(
      JSON.stringify({
        id: "resp_1",
        model: "gpt-5.4",
        status: "completed",
        output: [
          { type: "message", role: "assistant", content: [{ type: "output_text", text: "ok" }] },
        ],
        usage: { input_tokens: 5, output_tokens: 1 },
      }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    ),
  ]);
  try {
    const res = await handleCodexMessages(
      makeRequest({ model: "gpt-5.4", stream: false, messages: [{ role: "user", content: "hi" }] }),
      {}
    );
    assert.equal(res.status, 200);
    assert.equal(res.headers.get("Content-Type"), "application/json");
    const body = await res.json();
    assert.equal(body.type, "message");
    assert.equal(body.stop_reason, "end_turn");
    assert.equal(body.content[0].text, "ok");
  } finally {
    m.restore();
  }
});

test("handleCodexMessages: rejects non-codex auth header", async () => {
  const req = new Request("https://example.com/v1/messages", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: "sk-foo" },
    body: JSON.stringify({ model: "gpt-5.4", messages: [] }),
  });
  const res = await handleCodexMessages(req, {});
  assert.equal(res.status, 401);
  const body = await res.json();
  assert.equal(body.error.type, "authentication_error");
});

test("handleCodexMessages: rejects missing auth header", async () => {
  const req = new Request("https://example.com/v1/messages", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model: "gpt-5.4", messages: [] }),
  });
  const res = await handleCodexMessages(req, {});
  assert.equal(res.status, 401);
});

test("handleCodexMessages: rejects invalid JSON body", async () => {
  const res = await handleCodexMessages(makeRequest("not json"), {});
  assert.equal(res.status, 400);
  const body = await res.json();
  assert.equal(body.error.type, "invalid_request_error");
});

test("handleCodexMessages: 401 from Codex returns authentication_error", async () => {
  const m = mockFetch([
    new Response(
      JSON.stringify({ error: { code: "invalid_api_key", message: "bad token" } }),
      { status: 401, headers: { "Content-Type": "application/json" } }
    ),
  ]);
  try {
    const res = await handleCodexMessages(
      makeRequest({ model: "gpt-5.4", messages: [{ role: "user", content: "hi" }] }),
      {}
    );
    assert.equal(res.status, 401);
    const body = await res.json();
    assert.equal(body.error.type, "authentication_error");
    assert.match(body.error.message, /invalid or expired/);
  } finally {
    m.restore();
  }
});

test("handleCodexMessages: 429 usage_limit_reached returns friendly message", async () => {
  const future = Math.floor(Date.now() / 1000) + 600;
  const m = mockFetch([
    new Response(
      JSON.stringify({
        error: { code: "usage_limit_reached", plan_type: "plus", resets_at: future },
      }),
      { status: 429, headers: { "Content-Type": "application/json" } }
    ),
  ]);
  try {
    const res = await handleCodexMessages(
      makeRequest({ model: "gpt-5.4", messages: [{ role: "user", content: "hi" }] }),
      {}
    );
    assert.equal(res.status, 429);
    const body = await res.json();
    assert.equal(body.error.type, "rate_limit_error");
    assert.match(body.error.message, /usage limit \(plus plan\)/);
  } finally {
    m.restore();
  }
});

test("handleCodexMessages: 500 from Codex retries then succeeds", async () => {
  const m = mockFetch([
    new Response("overloaded", { status: 503, headers: { "Retry-After-Ms": "5" } }),
    makeSSE([
      { type: "response.created", response: { id: "resp_1" } },
      { type: "response.output_item.added", output_index: 0, item: { type: "message" } },
      { type: "response.output_text.delta", output_index: 0, delta: "ok" },
      { type: "response.output_item.done", output_index: 0 },
      { type: "response.completed", response: { status: "completed", usage: {} } },
    ]),
  ]);
  try {
    const res = await handleCodexMessages(
      makeRequest({ model: "gpt-5.4", stream: true, messages: [{ role: "user", content: "hi" }] }),
      {}
    );
    assert.equal(res.status, 200);
    const text = await res.text();
    assert.match(text, /"text":"ok"/);
    assert.equal(m.calls.length, 2);
  } finally {
    m.restore();
  }
});

test("handleCodexMessages: 400 from Codex is not retried", async () => {
  const m = mockFetch([
    new Response(
      JSON.stringify({ error: { message: "bad request" } }),
      { status: 400, headers: { "Content-Type": "application/json" } }
    ),
  ]);
  try {
    const res = await handleCodexMessages(
      makeRequest({ model: "gpt-5.4", messages: [{ role: "user", content: "hi" }] }),
      {}
    );
    assert.equal(res.status, 400);
    assert.equal(m.calls.length, 1);
    const body = await res.json();
    assert.equal(body.error.type, "invalid_request_error");
    assert.equal(body.error.message, "bad request");
  } finally {
    m.restore();
  }
});

test("handleCodexMessages: includes prompt_cache_key from metadata.user_id", async () => {
  const m = mockFetch([
    new Response(
      JSON.stringify({ id: "r", model: "gpt-5.4", status: "completed", output: [], usage: {} }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    ),
  ]);
  try {
    await handleCodexMessages(
      makeRequest({
        model: "gpt-5.4",
        stream: false,
        messages: [{ role: "user", content: "hi" }],
        metadata: { user_id: "user-12345" },
      }),
      {}
    );
    const sent = JSON.parse(m.calls[0].opts.body);
    assert.equal(sent.prompt_cache_key, "user-12345");
  } finally {
    m.restore();
  }
});

test("handleCodexMessages: honors X-Codex-Service-Tier header", async () => {
  const m = mockFetch([
    new Response(
      JSON.stringify({ id: "r", model: "gpt-5.4", status: "completed", output: [], usage: {} }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    ),
  ]);
  try {
    await handleCodexMessages(
      makeRequest(
        { model: "gpt-5.4", stream: false, messages: [{ role: "user", content: "hi" }] },
        { headers: { "X-Codex-Service-Tier": "flex" } }
      ),
      {}
    );
    const sent = JSON.parse(m.calls[0].opts.body);
    assert.equal(sent.service_tier, "flex");
  } finally {
    m.restore();
  }
});

test("resolveCodexBaseUrl: uses env override when set", () => {
  assert.equal(
    resolveCodexBaseUrl({ CODEX_BASE_URL: "http://mock:9999" }),
    "http://mock:9999"
  );
});

test("resolveCodexBaseUrl: uses default when env not set", () => {
  assert.equal(
    resolveCodexBaseUrl({}),
    "https://chatgpt.com/backend-api"
  );
  assert.equal(
    resolveCodexBaseUrl(undefined),
    "https://chatgpt.com/backend-api"
  );
});
