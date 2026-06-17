// Codex handler for claude-harness Worker
// Translates Anthropic Messages API <-> OpenAI Codex backend (chatgpt.com/backend-api/codex/responses)
//
// Stateless: the wrapper bash script handles OAuth (device-auth, refresh) and passes
// the access_token via ANTHROPIC_AUTH_TOKEN="codex:<token>:<account_id>".
//
// See docs/ANALYSIS_OPENCODE_CODEX.md for the full protocol analysis.

const CODEX_DEFAULT_BASE_URL = "https://chatgpt.com/backend-api";

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
