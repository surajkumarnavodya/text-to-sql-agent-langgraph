"""A deterministic, latency-configurable stand-in for a real Ollama server,
used only by the Phase 0 load-test harness (`eval/load/`) -- see
`docs/SCALE_BASELINE.md` and `docs/SCALE_OUT_PROMPT.md`'s Phase 0 section.

Why this exists: this app's real bottleneck is LLM inference time (93.8-
98% of wall-clock per `docs/PERFORMANCE_BASELINE.md`), which a load test
must be able to hold *constant and cheap* in order to isolate and measure
everything else in the request path (thread handling, rate limiting, DB
pooling, ...) -- see `SCALE_OUT_PROMPT.md`'s bottleneck #1. Rather than
monkeypatching or config-flagging any application code, this speaks just
enough of Ollama's real `POST /api/chat` wire protocol
(https://github.com/ollama/ollama/blob/main/docs/api.md#generate-a-chat-completion)
for `agent/llm_client.py`'s real `ollama.Client` to talk to it completely
unmodified -- point `OLLAMA_HOST` at this process and the whole app
behaves exactly as it does against a real Ollama server, just fast and
deterministic. **Zero lines of application source changed** is the whole
point of a Phase 0 harness (see the plan's "Explicit non-goals").

Response content is chosen by a cheap substring check against the
*system* message content, mirroring the exact system-prompt constants
`agent/llm_client.py` already defines for each call site
(`_INSIGHT_SYSTEM_PROMPT`, `_PLAN_SYSTEM_PROMPT`, `_REVIEW_SYSTEM_PROMPT`)
-- anything else (the generation system prompt, built dynamically by
`_system_prompt(db_type)`, has no single fixed constant to fingerprint)
falls through to the SQL-generation response, which is also this app's
most frequent call by far. The returned SQL deliberately matches
`eval/load/seed/schema.sql`'s real table/column names exactly, so
`agent/sql_validator.py`'s real AST validation and the real Postgres
execution both succeed on the first attempt -- a load test measuring
*this app's* overhead should not also be measuring its own retry loop
fighting a mock that never satisfies it.

Configurable via env vars (read once at process start, no `config.settings`
dependency -- this is test infrastructure, not application code):
    MOCK_OLLAMA_LATENCY_MS        (default 800)
    MOCK_OLLAMA_LATENCY_JITTER_MS (default 300, applied as +/-)
    MOCK_OLLAMA_PORT              (default 11434, matching real Ollama's
                                    default so `.env.loadtest`'s
                                    OLLAMA_HOST needs no unusual port)
"""

from __future__ import annotations

import json
import os
import random
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LATENCY_MS = float(os.environ.get("MOCK_OLLAMA_LATENCY_MS", "800"))
LATENCY_JITTER_MS = float(os.environ.get("MOCK_OLLAMA_LATENCY_JITTER_MS", "300"))
PORT = int(os.environ.get("MOCK_OLLAMA_PORT", "11434"))

# Matches eval/load/seed/schema.sql exactly -- see this module's own
# docstring for why a mismatch here would corrupt what the load test
# actually measures.
_MOCK_SQL = "SELECT id, customer_name, amount FROM orders ORDER BY id LIMIT 50"

_INSIGHT_MARKER = "one-to-two sentence"
_PLAN_MARKER = "query-planning assistant"
_REVIEW_MARKER = "SQL reviewer"


def _artificial_latency_seconds() -> float:
    jitter = random.uniform(-LATENCY_JITTER_MS, LATENCY_JITTER_MS)
    return max(0.0, (LATENCY_MS + jitter)) / 1000.0


def _system_message_content(messages: list[dict[str, str]]) -> str:
    for message in messages:
        if message.get("role") == "system":
            return message.get("content", "")
    return ""


def _content_for(system_content: str) -> str:
    if _INSIGHT_MARKER in system_content:
        # "NONE" is a legitimate real response (see
        # generate_insight_from_llm's own docstring: "the model declined")
        # -- simplest way to make this call a real no-op without needing
        # to satisfy agent.insight.is_insight_grounded's numeric check.
        return "NONE"
    if _PLAN_MARKER in system_content:
        return json.dumps(["Select the requested columns from orders."])
    if _REVIEW_MARKER in system_content:
        return "PASS"
    return _MOCK_SQL


class MockOllamaHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(
        self, format: str, *args: object
    ) -> None:  # noqa: A002 - matches base signature
        pass  # Quiet by default -- k6 already reports request-level detail.

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's own naming convention
        if self.path != "/api/chat":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            payload = {}

        time.sleep(_artificial_latency_seconds())

        messages = payload.get("messages", [])
        content = _content_for(_system_message_content(messages))

        body = json.dumps(
            {
                "model": payload.get("model", "mock-model"),
                "created_at": datetime.now(UTC).isoformat(),
                "message": {"role": "assistant", "content": content},
                "done": True,
                "done_reason": "stop",
                "total_duration": int(LATENCY_MS * 1_000_000),
                "load_duration": 0,
                "prompt_eval_count": sum(len(m.get("content", "")) // 4 for m in messages),
                "prompt_eval_duration": 0,
                "eval_count": len(content) // 4,
                "eval_duration": int(LATENCY_MS * 1_000_000),
            }
        ).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        # `ollama.Client.list()` (called by `api/main.py`'s `/health`)
        # calls `GET /api/tags` and expects a `{"models": [...]}` shape --
        # a real bug, found via this exact harness: this handler used to
        # omit `Content-Length`, and this class sets `protocol_version =
        # "HTTP/1.1"` (keep-alive by default) -- without a declared body
        # length, an HTTP/1.1 client has no way to know the response is
        # complete and blocks until its own read timeout (confirmed:
        # `ollama.Client(...).list()` took exactly
        # `OLLAMA_REQUEST_TIMEOUT_SECONDS` to "succeed"). Every response
        # here now sets `Content-Length` explicitly, matching `do_POST`'s
        # existing correct pattern.
        if self.path == "/api/tags":
            body = json.dumps({"models": []}).encode("utf-8")
        else:
            body = b'{"status":"mock-ollama-ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    server = ThreadingHTTPServer(
        ("0.0.0.0", PORT), MockOllamaHandler
    )  # noqa: S104 - intentional: this is a container-internal test double, never exposed to a real network
    print(
        f"[mock-ollama] listening on :{PORT} " f"(latency={LATENCY_MS}ms +/-{LATENCY_JITTER_MS}ms)"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
