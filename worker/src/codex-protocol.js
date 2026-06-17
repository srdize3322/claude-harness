// Codex protocol: Anthropic Messages API <-> OpenAI Responses API
// All converters are pure functions (no I/O), easy to test in isolation.

const SUPPORTED_MODELS = new Set([
  "gpt-5.5",
  "gpt-5.4",
  "gpt-5.4-mini",
  "gpt-5.3-codex-spark",
]);

const TOOL_CALL_PROVIDERS = new Set(["openai", "openai-codex", "opencode"]);

const MAX_RETRIES = 3;
const BASE_DELAY_MS = 1000;

const CODEX_EVENT_TYPES = new Set([
  "response.created",
  "response.in_progress",
  "response.output_item.added",
  "response.output_item.done",
  "response.content_part.added",
  "response.content_part.done",
  "response.output_text.delta",
  "response.output_text.done",
  "response.function_call_arguments.delta",
  "response.function_call_arguments.done",
  "response.reasoning_summary_text.delta",
  "response.reasoning_summary_text.done",
  "response.refusal.delta",
  "response.refusal.done",
  "response.queued",
  "response.completed",
  "response.incomplete",
  "response.failed",
  "response.cancelled",
]);

// anthropicToCodexResponses converts an Anthropic Messages API request body to
// an OpenAI Responses API request body.
//
// Key mappings:
// - system -> instructions
// - messages -> input
// - max_tokens -> max_output_tokens
// - tools -> tools (with input_schema -> parameters)
// - thinking -> reasoning
export function anthropicToCodexResponses(body, model) {
  if (!body || typeof body !== "object") {
    throw new Error("Request body is required");
  }

  const out = {
    model: model || body.model,
    stream: Boolean(body.stream),
    store: false,
  };

  // System prompt -> instructions
  if (typeof body.system === "string") {
    out.instructions = body.system;
  } else if (Array.isArray(body.system)) {
    out.instructions = body.system
      .filter((b) => b && b.type === "text")
      .map((b) => b.text)
      .join("\n");
  }

  // Messages -> input
  out.input = convertMessages(body.messages || []);

  // Tools
  if (Array.isArray(body.tools) && body.tools.length > 0) {
    out.tools = convertTools(body.tools);
    out.tool_choice = "auto";
    out.parallel_tool_calls = true;
  }

  // Generation params
  const gen = {};
  if (typeof body.max_tokens === "number") gen.max_output_tokens = body.max_tokens;
  if (typeof body.temperature === "number") gen.temperature = body.temperature;
  if (typeof body.top_p === "number") gen.top_p = body.top_p;
  if (Array.isArray(body.stop_sequences) && body.stop_sequences.length > 0) {
    gen.stop = body.stop_sequences;
  }
  if (Object.keys(gen).length > 0) out.generation_config = gen;

  // Thinking / reasoning
  if (body.thinking && typeof body.thinking === "object") {
    const reasoning = {};
    if (typeof body.thinking.budget_tokens === "number") {
      reasoning.effort = effortFromBudget(body.thinking.budget_tokens);
    } else if (typeof body.thinking.effort === "string") {
      reasoning.effort = body.thinking.effort;
    }
    if (body.thinking.summary) {
      reasoning.summary = body.thinking.summary;
    } else {
      reasoning.summary = "auto";
    }
    if (Object.keys(reasoning).length > 0) out.reasoning = reasoning;
  }

  return out;
}

function effortFromBudget(budget) {
  if (budget <= 1000) return "low";
  if (budget <= 8000) return "medium";
  if (budget <= 24000) return "high";
  return "xhigh";
}

function convertMessages(messages) {
  const out = [];
  for (const msg of messages) {
    if (!msg || typeof msg !== "object") continue;
    if (msg.role === "system") continue; // already mapped to instructions
    if (msg.role === "user") {
      out.push(...convertUserMessage(msg));
    } else if (msg.role === "assistant") {
      out.push(...convertAssistantMessage(msg));
    }
  }
  return out;
}

function convertUserMessage(msg) {
  if (typeof msg.content === "string") {
    return [{ role: "user", content: [{ type: "text", text: msg.content }] }];
  }
  if (!Array.isArray(msg.content)) {
    return [{ role: "user", content: [{ type: "text", text: String(msg.content) }] }];
  }
  const parts = [];
  const toolOutputs = [];
  for (const block of msg.content) {
    if (!block) continue;
    if (block.type === "text") {
      parts.push({ type: "text", text: block.text || "" });
    } else if (block.type === "image" && block.source) {
      // Anthropic image: { type: "image", source: { type: "base64", media_type, data } }
      parts.push({
        type: "image",
        image: block.source.data
          ? `data:${block.source.media_type || "image/png"};base64,${block.source.data}`
          : block.source.url,
      });
    } else if (block.type === "tool_result") {
      const text = extractToolResultText(block);
      toolOutputs.push({
        type: "function_call_output",
        call_id: normalizeToolUseId(block.tool_use_id),
        output: text,
      });
    }
  }
  const items = [];
  if (parts.length > 0) items.push({ role: "user", content: parts });
  for (const o of toolOutputs) items.push(o);
  return items.length > 0 ? items : [{ role: "user", content: [{ type: "text", text: "" }] }];
}

function convertAssistantMessage(msg) {
  if (typeof msg.content === "string") {
    return [{ role: "assistant", content: [{ type: "text", text: msg.content }] }];
  }
  if (!Array.isArray(msg.content)) {
    return [{ role: "assistant", content: [{ type: "text", text: String(msg.content) }] }];
  }
  // Anthropic assistant content can be mixed: text + tool_use + thinking
  // Codex wants one assistant message with multiple content parts.
  const content = [];
  const toolCalls = [];
  for (const block of msg.content) {
    if (!block) continue;
    if (block.type === "text") {
      content.push({ type: "text", text: block.text || "" });
    } else if (block.type === "tool_use") {
      toolCalls.push({
        type: "function_call",
        call_id: normalizeToolUseId(block.id),
        name: block.name,
        arguments: JSON.stringify(block.input || {}),
      });
    } else if (block.type === "thinking") {
      // Skip thinking blocks; Codex doesn't understand Anthropic signatures.
      // The reasoning replay would need raw bytes, which we don't have.
    }
  }
  const items = [];
  if (content.length > 0 || toolCalls.length > 0) {
    // If there's text content, push the assistant message with content.
    // Tool calls become separate items.
    if (content.length > 0) {
      items.push({ role: "assistant", content: content });
    }
    for (const tc of toolCalls) items.push(tc);
    // If only tool calls and no text, still need an assistant message for context.
    if (content.length === 0 && toolCalls.length > 0) {
      items.unshift({ role: "assistant", content: [{ type: "text", text: "" }] });
    }
  }
  return items.length > 0
    ? items
    : [{ role: "assistant", content: [{ type: "text", text: "" }] }];
}

function extractToolResultText(block) {
  const c = block.content;
  if (typeof c === "string") return c;
  if (!Array.isArray(c)) return String(c);
  return c
    .filter((b) => b && b.type === "text")
    .map((b) => b.text || "")
    .join("\n");
}

// Anthropic tool ids look like "toolu_xxx"; Codex expects "fc_xxx" or arbitrary.
// We pass through unchanged, since Codex is permissive on call_id format.
function normalizeToolUseId(id) {
  return id;
}

function convertTools(tools) {
  return tools
    .filter((t) => t && t.name)
    .map((t) => ({
      type: "function",
      name: t.name,
      description: t.description || "",
      parameters: t.input_schema || { type: "object", properties: {} },
      strict: false,
    }));
}

// isRetryableStatus returns true for HTTP status codes that should trigger
// automatic retry with exponential backoff.
export function isRetryableStatus(status) {
  return status === 429 || status === 500 || status === 502 || status === 503 || status === 504;
}

// retryDelay returns the delay (in ms) before retry attempt N (1-indexed).
export function retryDelay(attempt) {
  return BASE_DELAY_MS * Math.pow(2, attempt - 1);
}

export { SUPPORTED_MODELS, MAX_RETRIES, CODEX_EVENT_TYPES, TOOL_CALL_PROVIDERS };
