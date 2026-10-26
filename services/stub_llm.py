#!/usr/bin/env python3
"""Deterministic OpenAI-compatible stub provider.

The agents do not rely on the model to invent legal text: the deterministic
policy composes claims from retrieved provisions, and a model draft is only
accepted when every claim is citation-bound and lexically supported.  This stub
therefore returns a valid, empty JSON object.  It exists to exercise the HTTP
provider path, retries, circuit breaker, token accounting, latency budgets and
load tests without network access or cost.

Run locally:

    python services/stub_llm.py --port 8080 --delay-ms 150

Endpoints:

    POST /v1/chat/completions   (also /chat/completions)
    GET  /health
    GET  /metrics               (plain-text counters)
"""
from __future__ import annotations

import argparse
import json
import os
import random
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

STARTED = time.time()
COUNTERS: Counter[str] = Counter()
LOCK = threading.Lock()


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 4))


def _config() -> dict[str, Any]:
    return {
        "delay_ms": int(os.getenv("STUB_LLM_DELAY_MS", "0")),
        "fail_rate": float(os.getenv("STUB_LLM_FAIL_RATE", "0.0")),
        "model": os.getenv("STUB_LLM_MODEL", "stub-compliance-1"),
    }


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ComplianceStubLLM/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # keep load-test output readable
        if os.getenv("STUB_LLM_VERBOSE", "0") == "1":
            super().log_message(fmt, *args)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, status: int, body_text: str, content_type: str = "text/plain; charset=utf-8") -> None:
        body = body_text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?", 1)[0]
        if path in {"/health", "/health/live", "/healthz"}:
            self._json(200, {"status": "ok", "service": "stub-llm", "uptime_s": round(time.time() - STARTED, 1)})
            return
        if path == "/metrics":
            lines = [
                "# HELP stub_llm_requests_total Requests handled by the deterministic stub provider.",
                "# TYPE stub_llm_requests_total counter",
            ]
            with LOCK:
                for key, value in sorted(COUNTERS.items()):
                    lines.append(f'stub_llm_requests_total{{outcome="{key}"}} {value}')
            lines.append(f"stub_llm_uptime_seconds {time.time() - STARTED:.3f}")
            self._text(200, "\n".join(lines) + "\n")
            return
        self._json(404, {"error": "not_found", "path": path})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?", 1)[0]
        if path not in {"/v1/chat/completions", "/chat/completions"}:
            self._json(404, {"error": "not_found", "path": path})
            return
        length = int(self.headers.get("content-length") or 0)
        if length > 2 * 1024 * 1024:
            self._json(413, {"error": "body_too_large"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid_json"})
            return

        cfg = _config()
        if cfg["delay_ms"] > 0:
            time.sleep(cfg["delay_ms"] / 1000.0)

        if cfg["fail_rate"] > 0 and random.random() < cfg["fail_rate"]:
            with LOCK:
                COUNTERS["injected_failure"] += 1
            self._json(503, {"error": "injected_failure", "retryable": True})
            return

        messages = payload.get("messages") or []
        prompt = "\n".join(
            str(item.get("content", "")) for item in messages if isinstance(item, dict)
        )
        with LOCK:
            COUNTERS["ok"] += 1
        prompt_tokens = _estimate_tokens(prompt)
        content = "{}"
        self._json(
            200,
            {
                "id": f"stub-{int(time.time() * 1000)}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": payload.get("model") or cfg["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": _estimate_tokens(content),
                    "total_tokens": prompt_tokens + _estimate_tokens(content),
                },
            },
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic OpenAI-compatible stub provider")
    parser.add_argument("--host", default=os.getenv("STUB_LLM_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("STUB_LLM_PORT", "8080")))
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), StubHandler)
    server.daemon_threads = True
    cfg = _config()
    print(
        f"stub-llm listening on {args.host}:{args.port} "
        f"(delay_ms={cfg['delay_ms']}, fail_rate={cfg['fail_rate']})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
