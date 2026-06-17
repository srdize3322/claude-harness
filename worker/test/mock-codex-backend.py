#!/usr/bin/env python3
"""
Mock Codex backend for testing the Worker locally.

Mimics the Codex Responses API SSE format. Run on port 9999.

Usage:
  python3 test/mock-codex-backend.py [PORT]

Then in the Worker:
  wrangler dev --port 8769 --var "CODEX_BASE_URL=http://127.0.0.1:9999"
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9999

# Track which auth header we received (for debugging)
LAST_REQUEST = {}


def make_sse_event(event_type, data):
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def generate_codex_stream(prompt):
    """Generate a fake Codex SSE stream with text, tool call, and completion."""
    msg_id = "msg_mock123"
    response_id = "resp_mock456"

    # 1. response.created
    yield make_sse_event("response.created", {
        "type": "response.created",
        "response": {
            "id": response_id,
            "object": "response",
            "created_at": int(time.time()),
            "model": "gpt-5.4",
            "status": "in_progress",
        }
    })

    # 2. response.output_item.added (message)
    yield make_sse_event("response.output_item.added", {
        "type": "response.output_item.added",
        "output_index": 0,
        "item": {
            "id": "item_msg1",
            "type": "message",
            "role": "assistant",
            "content": [],
        }
    })

    # 3. response.content_part.added (text)
    yield make_sse_event("response.content_part.added", {
        "type": "response.content_part.added",
        "item_id": "item_msg1",
        "output_index": 0,
        "content_index": 0,
        "part": {"type": "output_text", "text": "", "annotations": []}
    })

    # 4. response.output_text.delta (streamed text)
    chunks = ["Hello", " from", " mock", " Codex", " backend!"]
    for chunk in chunks:
        yield make_sse_event("response.output_text.delta", {
            "type": "response.output_text.delta",
            "item_id": "item_msg1",
            "output_index": 0,
            "content_index": 0,
            "delta": chunk,
        })
        time.sleep(0.05)  # simulate latency

    # 5. response.output_text.done
    yield make_sse_event("response.output_text.done", {
        "type": "response.output_text.done",
        "item_id": "item_msg1",
        "output_index": 0,
        "content_index": 0,
        "text": "".join(chunks),
    })

    # 6. response.content_part.done
    yield make_sse_event("response.content_part.done", {
        "type": "response.content_part.done",
        "item_id": "item_msg1",
        "output_index": 0,
        "content_index": 0,
    })

    # 7. response.output_item.done
    yield make_sse_event("response.output_item.done", {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": {
            "id": "item_msg1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "".join(chunks), "annotations": []}],
        }
    })

    # 8. response.completed
    yield make_sse_event("response.completed", {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "object": "response",
            "created_at": int(time.time()),
            "model": "gpt-5.4",
            "status": "completed",
            "output": [
                {
                    "id": "item_msg1",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "".join(chunks), "annotations": []}],
                }
            ],
            "usage": {
                "input_tokens": 12,
                "output_tokens": 7,
                "total_tokens": 19,
            }
        }
    })

    # 9. SSE terminator
    yield "data: [DONE]\n\n"


class MockCodexHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Quieter logging
        sys.stderr.write(f"[mock-codex] {format % args}\n")

    def do_POST(self):
        parsed = urlparse(self.path)
        content_length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length else ""

        # Log the request (redact Authorization)
        auth = self.headers.get("Authorization", "")
        auth_redacted = f"Bearer {auth[7:15]}...<redacted>" if auth.startswith("Bearer ") and len(auth) > 22 else auth
        sys.stderr.write(f"[mock-codex] POST {self.path} | auth={auth_redacted}\n")
        try:
            req_json = json.loads(body)
            sys.stderr.write(f"[mock-codex] body: model={req_json.get('model')}, stream={req_json.get('stream')}\n")
        except Exception:
            pass

        LAST_REQUEST["auth"] = auth
        LAST_REQUEST["body"] = body

        if "/codex/responses" in self.path:
            if self.headers.get("accept") == "text/event-stream" or "text/event-stream" in self.headers.get("accept", ""):
                # Streaming response
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for chunk in generate_codex_stream(""):
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
            else:
                # Non-streaming: return a single completed response
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                response_id = "resp_mock789"
                resp = {
                    "id": response_id,
                    "object": "response",
                    "created_at": int(time.time()),
                    "model": "gpt-5.4",
                    "status": "completed",
                    "output": [
                        {
                            "id": "item_msg1",
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "Non-streaming mock response.", "annotations": []}],
                        }
                    ],
                    "usage": {"input_tokens": 5, "output_tokens": 4, "total_tokens": 9},
                }
                self.wfile.write(json.dumps(resp).encode("utf-8"))
        elif "/codex/" in self.path:
            # Other Codex endpoints
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "not found"}).encode("utf-8"))
        else:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "unknown endpoint"}).encode("utf-8"))


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), MockCodexHandler)
    sys.stderr.write(f"[mock-codex] Listening on http://127.0.0.1:{PORT}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("[mock-codex] Shutting down\n")
