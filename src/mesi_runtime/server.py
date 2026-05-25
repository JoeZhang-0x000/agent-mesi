from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .constants import DEFAULT_HOST, DEFAULT_PORT, daemon_path
from .errors import MesiError, NotFound
from .runtime import Runtime


class MesiHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], runtime: Runtime):
        super().__init__(server_address, MesiHandler)
        self.runtime = runtime


class MesiHandler(BaseHTTPRequestHandler):
    server: MesiHTTPServer

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _handle(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            segments = [segment for segment in parsed.path.split("/") if segment]
            query = parse_qs(parsed.query)
            body = self._json_body() if method == "POST" else {}
            result = self._dispatch(method, segments, query, body)
            self._send_json(200, result)
        except MesiError as exc:
            self._send_json(exc.status_code, {"ok": False, "error": exc.code, "message": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive HTTP boundary
            self._send_json(500, {"ok": False, "error": "internal_error", "message": str(exc)})

    def _json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _dispatch(self, method: str, segments: list[str], query: dict[str, list[str]], body: dict[str, Any]) -> Any:
        runtime = self.server.runtime
        if method == "GET" and segments == ["health"]:
            return {"ok": True}
        if method == "GET" and segments == ["events"]:
            after = int(query.get("after", ["0"])[0])
            return {"ok": True, "events": runtime.events(after)}
        if len(segments) == 3 and segments[0] == "agent":
            agent = segments[1]
            action = segments[2]
            if method == "GET" and action == "stale":
                return runtime.stale(agent)
            if method == "POST" and action == "read":
                return runtime.read(agent, body["path"])
            if method == "POST" and action == "write":
                return runtime.write(agent, body["path"], body.get("content", ""), body.get("kind", "write"))
            if method == "POST" and action == "refresh":
                return runtime.refresh(agent, body.get("paths") or None)
            if method == "POST" and action == "bash_begin":
                return runtime.bash_begin(agent, body.get("command", ""))
            if method == "POST" and action == "bash_end":
                return runtime.bash_end(agent, body["snapshot_id"], int(body.get("exit_code", 0)))
        raise NotFound(f"No route for {method} /{'/'.join(segments)}")

    def _send_json(self, status: int, payload: Any) -> None:
        raw = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def write_daemon_config(project_root: Path, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    path = daemon_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"host": host, "port": port}, sort_keys=True, indent=2), encoding="utf-8")


def serve(project_root: Path, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    runtime = Runtime(project_root)
    httpd = MesiHTTPServer((host, port), runtime)
    actual_host, actual_port = httpd.server_address
    write_daemon_config(runtime.project_root, actual_host, actual_port)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
