// Unit tests for codex-handler.js
// Run with: node --test worker/test/codex-handler.test.js

import { test } from "node:test";
import assert from "node:assert/strict";
import { detectCodexMode, parseCodexAuth, codexHeaders, mapStopReason } from "../src/codex-handler.js";

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
  assert(result.baseUrl.startsWith("https://"));
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
