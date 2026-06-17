const OPENCODE_BASE_URL = "https://opencode.ai/zen/go/v1";
const DEFAULT_MODEL = "minimax-m3";

async function anthropicToOpenAI(body) {
  const messages = [];

  if (body.system) {
    if (typeof body.system === "string") {
      messages.push({ role: "system", content: body.system });
    } else if (Array.isArray(body.system)) {
      const text = body.system
        .filter((b) => b.type === "text")
        .map((b) => b.text)
        .join("\n");
      if (text) messages.push({ role: "system", content: text });
    }
  }

  for (const msg of body.messages || []) {
    const role = msg.role;
    const content = msg.content;

    if (typeof content === "string") {
      messages.push({ role, content });
      continue;
    }

    if (!Array.isArray(content)) {
      messages.push({ role, content: String(content) });
      continue;
    }

    const textParts = [];
    const toolCalls = [];
    const toolResults = [];

    for (const block of content) {
      if (block.type === "text") {
        textParts.push(block.text);
      } else if (block.type === "tool_use") {
        toolCalls.push({
          id: block.id,
          type: "function",
          function: {
            name: block.name,
            arguments: JSON.stringify(block.input),
          },
        });
      } else if (block.type === "tool_result") {
        toolResults.push({
          role: "tool",
          tool_call_id: block.tool_use_id,
          content: typeof block.content === "string"
            ? block.content
            : JSON.stringify(block.content),
        });
      } else if (block.type === "image") {
        const src = block.source || {};
        textParts.push(
          `[image: data:${src.media_type};base64,${(src.data || "").slice(0, 80)}...]`
        );
      }
    }

    if (toolResults.length > 0) {
      for (const tr of toolResults) {
        messages.push(tr);
      }
    } else if (toolCalls.length > 0) {
      const msgObj = { role: "assistant", content: textParts.join("\n") || null };
      if (toolCalls.length > 0) {
        msgObj.tool_calls = toolCalls;
      }
      messages.push(msgObj);
    } else {
      messages.push({ role, content: textParts.join("\n") });
    }
  }

  const openaiBody = {
    model: (body.model || DEFAULT_MODEL).replace(/\[[12]m\]$/i, "").trim(),
    messages,
    stream: body.stream || false,
  };

  if (body.max_tokens) openaiBody.max_tokens = body.max_tokens;
  if (body.temperature != null) openaiBody.temperature = body.temperature;
  if (body.top_p != null) openaiBody.top_p = body.top_p;
  if (body.stop_sequences) openaiBody.stop = body.stop_sequences;

  const translatedThinking = await translateThinking(body.model, body.thinking, body.output_config);
  if (translatedThinking) {
    if (translatedThinking.reasoning_effort) {
      openaiBody.reasoning_effort = translatedThinking.reasoning_effort;
    }
    if (translatedThinking.reasoning) {
      openaiBody.reasoning = translatedThinking.reasoning;
    }
    if (translatedThinking.max_tokens) {
      if (!openaiBody.max_tokens || openaiBody.max_tokens < translatedThinking.max_tokens) {
        openaiBody.max_tokens = translatedThinking.max_tokens;
      }
    }
    if (translatedThinking.strip_sampling) {
      delete openaiBody.temperature;
      delete openaiBody.top_p;
    }
  }

  if (body.tools && body.tools.length > 0) {
    openaiBody.tools = body.tools.map((tool) => ({
      type: "function",
      function: {
        name: tool.name,
        description: tool.description || "",
        parameters: tool.input_schema || {},
      },
    }));
    openaiBody.tool_choice = "auto";
  }

  return openaiBody;
}


const EFFORT_ORDER = ["minimal", "low", "medium", "high", "xhigh", "max"];

function nearestEffort(target, available) {
  if (available.includes(target)) return target;
  if (!EFFORT_ORDER.includes(target)) {
    return available.includes("medium") ? "medium" : available[0];
  }
  const targetIdx = EFFORT_ORDER.indexOf(target);
  let best = available[0];
  let bestDist = Infinity;
  for (const v of available) {
    if (!EFFORT_ORDER.includes(v)) continue;
    const dist = Math.abs(EFFORT_ORDER.indexOf(v) - targetIdx);
    if (dist < bestDist || (dist === bestDist && EFFORT_ORDER.indexOf(v) > EFFORT_ORDER.indexOf(best))) {
      bestDist = dist;
      best = v;
    }
  }
  return best;
}

const HARDCODED_EFFORT_FALLBACK = {
  "minimax-m3": ["minimal", "low", "medium", "high"],
  "minimax-m2.7": ["minimal", "low", "medium", "high"],
  "minimax-m2.5": ["minimal", "low", "medium", "high"],
  "deepseek-v4-pro": ["high", "max"],
  "deepseek-v4-flash": ["high", "max"],
  "kimi-k2.7-code": ["minimal", "low", "medium", "high"],
  "qwen3.7-plus": ["minimal", "low", "medium", "high"],
};

let modelCatalogCache = { fetchedAt: 0, models: {} };

async function fetchOpencodeCatalog(force = false) {
  const now = Date.now();
  if (!force && now - modelCatalogCache.fetchedAt < 3600000) {
    return modelCatalogCache.models;
  }
  try {
    const baseUrl = (OPENCODE_BASE_URL || "").replace(/\/v1$/, "");
    const resp = await fetch(`${baseUrl}/models`, {
      headers: { Authorization: `Bearer ${OPENCODE_API_KEY}` },
    });
    if (resp.ok) {
      const data = await resp.json();
      const map = {};
      if (data && Array.isArray(data.data)) {
        for (const m of data.data) {
          if (m && m.id) map[m.id.toLowerCase()] = m;
        }
      } else if (Array.isArray(data)) {
        for (const m of data) {
          if (m && m.id) map[m.id.toLowerCase()] = m;
        }
      }
      modelCatalogCache = { fetchedAt: now, models: map };
      return map;
    }
  } catch (_) {}
  return modelCatalogCache.models;
}

async function getModelEffortCapabilities(modelId) {
  const mid = (modelId || "").toLowerCase();

  const catalog = await fetchOpencodeCatalog();
  if (catalog[mid]) {
    const m = catalog[mid];
    const opts = m.reasoning_options || [];
    const effortOpt = opts.find((o) => o && o.type === "effort");
    if (effortOpt && Array.isArray(effortOpt.values) && effortOpt.values.length > 0) {
      return effortOpt.values;
    }
    if (opts.some((o) => o && o.type === "toggle")) {
      return ["minimal", "low", "medium", "high"];
    }
    if (opts.some((o) => o && o.type === "budget_tokens")) {
      return ["low", "medium", "high"];
    }
  }

  for (const key of Object.keys(HARDCODED_EFFORT_FALLBACK)) {
    if (mid.includes(key) || key.includes(mid)) {
      return HARDCODED_EFFORT_FALLBACK[key];
    }
  }
  return ["low", "medium", "high"];
}

async function translateThinking(modelId, thinking, outputConfig) {
  if (!thinking && !outputConfig) return null;

  if (outputConfig && outputConfig.effort) {
    const effort = outputConfig.effort;
    if (effort === "off" || effort === "none" || effort === "disabled") return null;
    const available = await getModelEffortCapabilities(modelId);
    const mapped = nearestEffort(effort, available);
    return { reasoning_effort: mapped };
  }

  if (thinking) {
    if (thinking.type === "disabled") return null;
    if (thinking.type === "adaptive") {
      const effort = outputConfig?.effort || "medium";
      if (effort === "off" || effort === "none" || effort === "disabled") return null;
      const available = await getModelEffortCapabilities(modelId);
      const mapped = nearestEffort(effort, available);
      return { reasoning_effort: mapped };
    }
    if (thinking.type === "enabled") {
      const budget = thinking.budget_tokens || 0;
      let effort = "medium";
      if (budget <= 1500) effort = "minimal";
      else if (budget <= 3500) effort = "low";
      else if (budget <= 10000) effort = "medium";
      else if (budget <= 22000) effort = "high";
      else effort = "max";
      const available = await getModelEffortCapabilities(modelId);
      const mapped = nearestEffort(effort, available);
      return { reasoning_effort: mapped };
    }
  }
  return null;
}

function openAIToAnthropic(data, requestModel) {
  const choice = (data.choices || [])[0] || {};
  const message = choice.message || {};
  const content = [];

  const reasoningText = message.reasoning_content || message.reasoning || "";
  if (reasoningText) {
    content.push({ type: "thinking", thinking: reasoningText });
  }

  if (message.content) {
    content.push({ type: "text", text: message.content });
  }

  const toolCalls = message.tool_calls || [];
  for (const tc of toolCalls) {
    let input = {};
    try {
      input = JSON.parse(tc.function?.arguments || "{}");
    } catch (_) {
      input = {};
    }
    content.push({
      type: "tool_use",
      id: tc.id,
      name: tc.function?.name || "",
      input,
    });
  }

  const stopReasonMap = {
    stop: "end_turn",
    tool_calls: "tool_use",
    length: "max_tokens",
    content_filter: "end_turn",
  };

  return {
    id: data.id || `msg_${Date.now()}`,
    type: "message",
    role: "assistant",
    model: data.model || requestModel || DEFAULT_MODEL,
    content,
    stop_reason: stopReasonMap[choice.finish_reason] || "end_turn",
    stop_sequence: null,
    usage: {
      input_tokens: data.usage?.prompt_tokens || 0,
      output_tokens: data.usage?.completion_tokens || 0,
    },
  };
}

function transformStream(body, requestModel) {
    let msgId = `msg_${Date.now()}`;
    let contentBlockIndex = 0;
    let textBlockOpen = false;
    let toolBlockOpen = false;
    let toolBlockIndex = -1;
    let toolName = "";
    let toolId = "";
    let thinkingBlockOpen = false;
    let thinkingBlockIndex = -1;
    let finished = false;

  const encoder = new TextEncoder();
  const decoder = new TextDecoder();

  function sseEvent(event, data) {
    if (typeof data === "string") {
      return `event: ${event}\ndata: ${data}\n\n`;
    }
    return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  }

  return new TransformStream({
    start(controller) {
      controller.enqueue(
        encoder.encode(
          sseEvent("message_start", {
            type: "message_start",
            message: {
              id: msgId,
              type: "message",
              role: "assistant",
              model: requestModel || DEFAULT_MODEL,
              content: [],
            },
          })
        )
      );
    },

    transform(chunk, controller) {
      const text = decoder.decode(chunk, { stream: true });
      const lines = text.split("\n");

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const payload = line.slice(6).trim();
        if (payload === "[DONE]") {
          continue;
        }

        let data;
        try {
          data = JSON.parse(payload);
        } catch (_) {
          continue;
        }

        const choice = (data.choices || [])[0];
        if (!choice) continue;
        const delta = choice.delta || {};
        const reasoningText = delta.reasoning_content || delta.reasoning || "";

        if (!textBlockOpen && !toolBlockOpen && !thinkingBlockOpen
            && (delta.content !== undefined || delta.tool_calls || reasoningText)) {
          if (reasoningText && !delta.content && !delta.tool_calls) {
            thinkingBlockOpen = true;
            thinkingBlockIndex = contentBlockIndex;
            contentBlockIndex++;
            controller.enqueue(
              encoder.encode(
                sseEvent("content_block_start", {
                  type: "content_block_start",
                  index: thinkingBlockIndex,
                  content_block: { type: "thinking", thinking: "" },
                })
              )
            );
          } else {
            const toolCallDeltas = delta.tool_calls || [];
            const hasToolCalls = toolCallDeltas.length > 0;

            if (hasToolCalls) {
              const first = toolCallDeltas[0];
              toolBlockOpen = true;
              toolBlockIndex = contentBlockIndex;
              contentBlockIndex++;
              toolId = first.id || `toolu_${Date.now()}`;
              toolName = first.function?.name || "";

              controller.enqueue(
                encoder.encode(
                  sseEvent("content_block_start", {
                    type: "content_block_start",
                    index: toolBlockIndex,
                    content_block: {
                      type: "tool_use",
                      id: toolId,
                      name: toolName,
                      input: {},
                    },
                  })
                )
              );
            } else {
              textBlockOpen = true;
              controller.enqueue(
                encoder.encode(
                  sseEvent("content_block_start", {
                    type: "content_block_start",
                    index: contentBlockIndex,
                    content_block: { type: "text", text: "" },
                  })
                )
              );
            }
          }
        }

        if (thinkingBlockOpen && reasoningText) {
          controller.enqueue(
            encoder.encode(
              sseEvent("content_block_delta", {
                type: "content_block_delta",
                index: thinkingBlockIndex,
                delta: { type: "thinking_delta", thinking: reasoningText },
              })
            )
          );
        }

        if (textBlockOpen && delta.content) {
          controller.enqueue(
            encoder.encode(
              sseEvent("content_block_delta", {
                type: "content_block_delta",
                index: contentBlockIndex,
                delta: { type: "text_delta", text: delta.content },
              })
            )
          );
        }

        if (toolBlockOpen) {
          const toolCallDeltas = delta.tool_calls || [];
          for (const tcd of toolCallDeltas) {
            const args = tcd.function?.arguments || "";
            if (args) {
              controller.enqueue(
                encoder.encode(
                  sseEvent("content_block_delta", {
                    type: "content_block_delta",
                    index: toolBlockIndex,
                    delta: {
                      type: "input_json_delta",
                      partial_json: args,
                    },
                  })
                )
              );
            }
          }
        }

        const finishReason = choice.finish_reason;
        if (finishReason && !finished) {
          finished = true;

          if (thinkingBlockOpen) {
            controller.enqueue(
              encoder.encode(
                sseEvent("content_block_stop", {
                  type: "content_block_stop",
                  index: thinkingBlockIndex,
                })
              )
            );
            thinkingBlockOpen = false;
          }

          if (textBlockOpen) {
            controller.enqueue(
              encoder.encode(
                sseEvent("content_block_stop", {
                  type: "content_block_stop",
                  index: contentBlockIndex,
                })
              )
            );
            textBlockOpen = false;
            contentBlockIndex++;
          }

          if (toolBlockOpen) {
            controller.enqueue(
              encoder.encode(
                sseEvent("content_block_stop", {
                  type: "content_block_stop",
                  index: toolBlockIndex,
                })
              )
            );
            toolBlockOpen = false;
            contentBlockIndex = toolBlockIndex + 1;
          }

          const stopMap = {
            stop: "end_turn",
            tool_calls: "tool_use",
            length: "max_tokens",
          };

          controller.enqueue(
            encoder.encode(
              sseEvent("message_delta", {
                type: "message_delta",
                delta: {
                  stop_reason: stopMap[finishReason] || "end_turn",
                  stop_sequence: null,
                },
                usage: {
                  output_tokens: data.usage?.completion_tokens || 0,
                },
              })
            )
          );

          controller.enqueue(
            encoder.encode(
              sseEvent("message_stop", { type: "message_stop" })
            )
          );
        }
      }
    },

    flush(controller) {
      if (!finished) {
        if (thinkingBlockOpen) {
          controller.enqueue(
            encoder.encode(
              sseEvent("content_block_stop", {
                type: "content_block_stop",
                index: thinkingBlockIndex,
              })
            )
          );
        }
        if (textBlockOpen) {
          controller.enqueue(
            encoder.encode(
              sseEvent("content_block_stop", {
                type: "content_block_stop",
                index: contentBlockIndex,
              })
            )
          );
        }
        if (toolBlockOpen) {
          controller.enqueue(
            encoder.encode(
              sseEvent("content_block_stop", {
                type: "content_block_stop",
                index: toolBlockIndex,
              })
            )
          );
        }

        controller.enqueue(
          encoder.encode(
            sseEvent("message_delta", {
              type: "message_delta",
              delta: { stop_reason: "end_turn", stop_sequence: null },
              usage: { output_tokens: 0 },
            })
          )
        );
        controller.enqueue(
          encoder.encode(
            sseEvent("message_stop", { type: "message_stop" })
          )
        );
      }
    },
  });
}

async function handleMessages(request, env) {
  // Routing: detect Codex mode from the Authorization header BEFORE
  // checking OPENCODE_API_KEY. Codex mode uses its own auth (the
  // access_token in the header), OpenCode Go mode uses the env secret.
  const authHeader = request.headers.get("authorization") || "";
  if (authHeader.startsWith("codex:")) {
    const { handleCodexMessages } = await import("./codex-handler.js");
    return handleCodexMessages(request, env);
  }

  const apiKey = env.OPENCODE_API_KEY;
  if (!apiKey) {
    return new Response(
      JSON.stringify({
        type: "error",
        error: { type: "api_error", message: "OPENCODE_API_KEY not configured in Worker" },
      }),
      { status: 500, headers: { "Content-Type": "application/json" } }
    );
  }

  let body;
  try {
    body = await request.json();
  } catch (_) {
    return new Response(
      JSON.stringify({
        type: "error",
        error: { type: "invalid_request_error", message: "Invalid JSON body" },
      }),
      { status: 400, headers: { "Content-Type": "application/json" } }
    );
  }

  const openaiBody = await anthropicToOpenAI(body);
  const baseUrl = (env.OPENCODE_BASE_URL || OPENCODE_BASE_URL).replace(/\/$/, "");

  const upstreamRes = await fetch(`${baseUrl}/chat/completions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify(openaiBody),
  });

  if (!upstreamRes.ok) {
    const errorText = await upstreamRes.text();
    let errorData;
    try {
      errorData = JSON.parse(errorText);
    } catch (_) {
      errorData = { error: { message: errorText } };
    }

    return new Response(
      JSON.stringify({
        type: "error",
        error: {
          type: "api_error",
          message: errorData.error?.message || errorText || `Upstream error ${upstreamRes.status}`,
        },
      }),
      {
        status: upstreamRes.status,
        headers: { "Content-Type": "application/json" },
      }
    );
  }

  if (body.stream) {
    const anthropicStream = upstreamRes.body
      .pipeThrough(new TextDecoderStream())
      .pipeThrough(transformStream(body, body.model));

    return new Response(anthropicStream, {
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        Connection: "keep-alive",
      },
    });
  }

  const openaiData = await upstreamRes.json();
  const anthropicData = openAIToAnthropic(openaiData, body.model);

  return new Response(JSON.stringify(anthropicData), {
    headers: { "Content-Type": "application/json" },
  });
}

function estimateTokens(text) {
  if (!text) return 0;
  return Math.ceil(text.length / 3.5);
}

function estimateMessageTokens(messages) {
  let total = 0;
  for (const msg of messages || []) {
    const content = msg.content;
    if (typeof content === "string") {
      total += estimateTokens(content);
    } else if (Array.isArray(content)) {
      for (const block of content) {
        if (block.type === "text") {
          total += estimateTokens(block.text);
        } else if (block.type === "tool_use") {
          total += estimateTokens(JSON.stringify(block.input)) + 10;
        } else if (block.type === "tool_result") {
          const resultContent =
            typeof block.content === "string"
              ? block.content
              : JSON.stringify(block.content);
          total += estimateTokens(resultContent);
        }
      }
    }
  }
  return total;
}

async function handleCountTokens(request) {
  let body;
  try {
    body = await request.json();
  } catch (_) {
    return new Response(
      JSON.stringify({ input_tokens: 0 }),
      { headers: { "Content-Type": "application/json" } }
    );
  }

  const inputTokens = estimateMessageTokens(body.messages || []);
  return new Response(
    JSON.stringify({ input_tokens: Math.max(inputTokens, 1) }),
    { headers: { "Content-Type": "application/json" } }
  );
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/health" && request.method === "GET") {
      return new Response("OK", { headers: { "Content-Type": "text/plain" } });
    }

    if (
      (url.pathname === "/v1/messages" || url.pathname.endsWith("/messages")) &&
      request.method === "POST"
    ) {
      if (url.pathname.includes("count_tokens")) {
        return handleCountTokens(request);
      }
      return handleMessages(request, env);
    }

    return new Response(
      JSON.stringify({
        type: "error",
        error: { type: "not_found", message: `Unknown endpoint: ${request.method} ${url.pathname}` },
      }),
      { status: 404, headers: { "Content-Type": "application/json" } }
    );
  },
};
