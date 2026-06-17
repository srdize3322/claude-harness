// Unit tests for codex-protocol.js
// Run with: node --test worker/test/codex-protocol.test.js

import { test } from "node:test";
import assert from "node:assert/strict";
import {
  anthropicToCodexResponses,
  isRetryableStatus,
  retryDelay,
  SUPPORTED_MODELS,
} from "../src/codex-protocol.js";

test("anthropicToCodexResponses: minimal request", () => {
  const out = anthropicToCodexResponses(
    { model: "gpt-5.4", messages: [{ role: "user", content: "hi" }] },
    "gpt-5.4"
  );
  assert.equal(out.model, "gpt-5.4");
  assert.equal(out.stream, false);
  assert.equal(out.store, false);
  assert.deepEqual(out.input, [
    { role: "user", content: [{ type: "text", text: "hi" }] },
  ]);
});

test("anthropicToCodexResponses: system string -> instructions", () => {
  const out = anthropicToCodexResponses(
    {
      model: "gpt-5.4",
      system: "You are helpful.",
      messages: [{ role: "user", content: "hi" }],
    },
    "gpt-5.4"
  );
  assert.equal(out.instructions, "You are helpful.");
});

test("anthropicToCodexResponses: system array -> joined instructions", () => {
  const out = anthropicToCodexResponses(
    {
      model: "gpt-5.4",
      system: [
        { type: "text", text: "You are helpful." },
        { type: "text", text: "Be concise." },
      ],
      messages: [{ role: "user", content: "hi" }],
    },
    "gpt-5.4"
  );
  assert.equal(out.instructions, "You are helpful.\nBe concise.");
});

test("anthropicToCodexResponses: tools get converted", () => {
  const out = anthropicToCodexResponses(
    {
      model: "gpt-5.4",
      messages: [{ role: "user", content: "hi" }],
      tools: [
        {
          name: "get_weather",
          description: "Get the weather for a city.",
          input_schema: {
            type: "object",
            properties: { city: { type: "string" } },
            required: ["city"],
          },
        },
      ],
    },
    "gpt-5.4"
  );
  assert.equal(out.tools.length, 1);
  assert.equal(out.tools[0].type, "function");
  assert.equal(out.tools[0].name, "get_weather");
  assert.deepEqual(out.tools[0].parameters.required, ["city"]);
  assert.equal(out.tool_choice, "auto");
});

test("anthropicToCodexResponses: tool_use + tool_result flow", () => {
  const out = anthropicToCodexResponses(
    {
      model: "gpt-5.4",
      messages: [
        { role: "user", content: "what's the weather in Paris?" },
        {
          role: "assistant",
          content: [
            {
              type: "tool_use",
              id: "toolu_01",
              name: "get_weather",
              input: { city: "Paris" },
            },
          ],
        },
        {
          role: "user",
          content: [
            {
              type: "tool_result",
              tool_use_id: "toolu_01",
              content: "sunny, 22C",
            },
          ],
        },
      ],
    },
    "gpt-5.4"
  );
  // 1 user (text) + 1 assistant (text+tool_use) + 1 function_call + 1 function_call_output = 4
  // Because we split the assistant message into a content item + a separate function_call item.
  assert.equal(out.input.length, 4);
  assert.equal(out.input[0].role, "user");
  assert.equal(out.input[1].role, "assistant");
  assert.equal(out.input[1].content[0].type, "text");
  assert.equal(out.input[2].type, "function_call");
  assert.equal(out.input[2].call_id, "toolu_01");
  assert.equal(out.input[3].type, "function_call_output");
  assert.equal(out.input[3].call_id, "toolu_01");
  assert.equal(out.input[3].output, "sunny, 22C");
});

test("anthropicToCodexResponses: thinking -> reasoning", () => {
  const out = anthropicToCodexResponses(
    {
      model: "gpt-5.4",
      thinking: { budget_tokens: 16000 },
      messages: [{ role: "user", content: "hi" }],
    },
    "gpt-5.4"
  );
  assert.equal(out.reasoning.effort, "high");
  assert.equal(out.reasoning.summary, "auto");
});

test("anthropicToCodexResponses: max_tokens -> max_output_tokens", () => {
  const out = anthropicToCodexResponses(
    {
      model: "gpt-5.4",
      max_tokens: 1024,
      messages: [{ role: "user", content: "hi" }],
    },
    "gpt-5.4"
  );
  assert.equal(out.generation_config.max_output_tokens, 1024);
});

test("anthropicToCodexResponses: temperature + top_p", () => {
  const out = anthropicToCodexResponses(
    {
      model: "gpt-5.4",
      temperature: 0.5,
      top_p: 0.9,
      messages: [{ role: "user", content: "hi" }],
    },
    "gpt-5.4"
  );
  assert.equal(out.generation_config.temperature, 0.5);
  assert.equal(out.generation_config.top_p, 0.9);
});

test("anthropicToCodexResponses: empty messages still produces valid input", () => {
  const out = anthropicToCodexResponses({ model: "gpt-5.4", messages: [] }, "gpt-5.4");
  assert.deepEqual(out.input, []);
});

test("anthropicToCodexResponses: stream flag preserved", () => {
  const out = anthropicToCodexResponses(
    { model: "gpt-5.4", stream: true, messages: [{ role: "user", content: "hi" }] },
    "gpt-5.4"
  );
  assert.equal(out.stream, true);
});

test("anthropicToCodexResponses: skips thinking blocks from assistant history", () => {
  const out = anthropicToCodexResponses(
    {
      model: "gpt-5.4",
      messages: [
        {
          role: "assistant",
          content: [
            { type: "thinking", thinking: "secret" },
            { type: "text", text: "ok" },
          ],
        },
      ],
    },
    "gpt-5.4"
  );
  // Should have only the text part
  assert.equal(out.input[0].content.length, 1);
  assert.equal(out.input[0].content[0].type, "text");
});

test("isRetryableStatus: 429/500/502/503/504 are retryable", () => {
  assert.equal(isRetryableStatus(429), true);
  assert.equal(isRetryableStatus(500), true);
  assert.equal(isRetryableStatus(502), true);
  assert.equal(isRetryableStatus(503), true);
  assert.equal(isRetryableStatus(504), true);
});

test("isRetryableStatus: 400/401/403/404 are not retryable", () => {
  assert.equal(isRetryableStatus(400), false);
  assert.equal(isRetryableStatus(401), false);
  assert.equal(isRetryableStatus(403), false);
  assert.equal(isRetryableStatus(404), false);
});

test("retryDelay: exponential backoff", () => {
  assert.equal(retryDelay(1), 1000);
  assert.equal(retryDelay(2), 2000);
  assert.equal(retryDelay(3), 4000);
});

test("SUPPORTED_MODELS: contains known models", () => {
  assert(SUPPORTED_MODELS.has("gpt-5.4"));
  assert(SUPPORTED_MODELS.has("gpt-5.5"));
  assert(SUPPORTED_MODELS.has("gpt-5.4-mini"));
  assert(SUPPORTED_MODELS.has("gpt-5.3-codex-spark"));
});
