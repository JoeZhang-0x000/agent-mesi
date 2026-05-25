from __future__ import annotations

import json
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from typer.testing import CliRunner

from mesi_runtime.client import format_tool_response
from mesi_runtime.cli import app, format_event, format_stale_notice
from mesi_runtime.constants import legacy_opencode_bindings_path, opencode_bindings_path
from mesi_runtime.opencode import default_opencode_port, get_opencode_agent, record_opencode_agent
from mesi_runtime.runtime import Runtime
from mesi_runtime.server import MesiHTTPServer, write_daemon_config


def test_http_endpoints_return_stable_json(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    runtime = Runtime(tmp_path)
    runtime.init_project(tmp_path)
    runtime.create_agent("A")
    server = MesiHTTPServer(("127.0.0.1", 0), runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        health = _json(f"http://127.0.0.1:{port}/health")
        assert health == {"ok": True}
        read = _json(
            f"http://127.0.0.1:{port}/agent/A/read",
            {"path": "README.md"},
        )
        assert read["ok"] is True
        assert read["content"] == "hello\n"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_cli_demo_outputs_expected_trace(tmp_path):
    runner = CliRunner()
    result = runner.invoke(app, ["demo", "basic-stale", "--project-root", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "READ README.md" in result.output
    assert "WRITE README.md" in result.output
    assert "STALE README.md" in result.output
    assert "WRITE_BLOCKED notes.md" in result.output
    assert "REFRESH" in result.output


def test_cli_workspace_base_mismatch_demo_outputs_mismatch(tmp_path):
    runner = CliRunner()
    result = runner.invoke(app, ["demo", "workspace-base-mismatch", "--project-root", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "WRITE created-by-b.txt absent ->" in result.output
    assert "WRITE_BLOCKED created-by-b.txt reason=workspace_base_mismatch" in result.output


def test_cli_monitor_once_uses_event_log_stream(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    runtime = Runtime(tmp_path)
    runtime.init_project(tmp_path)
    runtime.create_agent("A")

    runner = CliRunner()
    result = runner.invoke(app, ["monitor", "--project-root", str(tmp_path), "--once", "--no-color"])

    assert result.exit_code == 0, result.output
    assert "System INIT" in result.output
    assert "Agent A AGENT_CREATED" in result.output


def test_event_formatting_uses_color_and_short_versions():
    event = {
        "seq": 7,
        "type": "stale",
        "agent": "A",
        "path": "README.md",
        "old_version": "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        "new_version": "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
    }

    plain = format_event(event)
    colored = format_event(event, color=True)

    assert "12345678 -> abcdef12" in plain
    assert event["old_version"] not in plain
    assert "\x1b[" in colored
    assert colored.startswith("[007] ")
    assert "Agent A" in colored
    assert "STALE" in colored
    assert "README.md" in colored
    assert "12345678 -> abcdef12" in colored
    assert "READ README.md -> abcdef12" in format_event(
        {
            "seq": 8,
            "type": "read",
            "agent": "A",
            "path": "README.md",
            "new_version": event["new_version"],
        }
    )


def test_human_tool_output_shortens_version_fields():
    response = {
        "ok": True,
        "old_version": "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        "new_version": "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
        "event": {"version": "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"},
    }
    notice = format_stale_notice(
        [
            {
                "path": "README.md",
                "old_version": response["old_version"],
                "new_version": response["new_version"],
            }
        ]
    )
    output = format_tool_response(response)

    assert "12345678" in output
    assert "abcdef12" in output
    assert response["old_version"] not in output
    assert "README.md 12345678 -> abcdef12" in notice


def test_root_start_accepts_project_root_argument():
    runner = CliRunner()
    result = runner.invoke(app, ["start", "--help"])

    assert result.exit_code == 0, result.output
    assert "project_root" in result.output


def test_tool_outputs_visible_stale_notices(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    runtime = Runtime(tmp_path)
    runtime.init_project(tmp_path)
    runtime.create_agent("A")
    runtime.create_agent("B")
    server, thread = _start_server(runtime)
    runner = CliRunner()
    try:
        read_a = runner.invoke(app, ["tool", "read", "README.md", "--agent", "A", "--project-root", str(tmp_path)])
        assert read_a.exit_code == 0, read_a.output
        assert "hello" in read_a.output

        runtime.read("B", "README.md")
        runtime.write("B", "README.md", "from B\n")

        status = runner.invoke(app, ["tool", "status", "--agent", "A", "--project-root", str(tmp_path)])
        assert status.exit_code == 0, status.output
        assert "MESI NOTICE" in status.output
        assert "README.md" in status.output
        assert "mesi_refresh [path]" in status.output

        read_stale = runner.invoke(app, ["tool", "read", "README.md", "--agent", "A", "--project-root", str(tmp_path)])
        assert read_stale.exit_code == 0, read_stale.output
        assert "MESI NOTICE: reading latest resolved stale for README.md" in read_stale.output
        assert "from B" in read_stale.output
        assert runtime.stale("A")["stale"] == []

        runtime.write("B", "README.md", "from B again\n")
        blocked = runner.invoke(app, ["tool", "write", "notes.md", "blocked\n", "--agent", "A", "--project-root", str(tmp_path)])
        blocked_output = _combined_output(blocked)
        assert blocked.exit_code == 1, blocked_output
        assert "MESI NOTICE" in blocked_output
        assert "README.md" in blocked_output
        assert "mesi_refresh [path]" in blocked_output
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_tool_write_existing_file_requires_current_read(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    runtime = Runtime(tmp_path)
    runtime.init_project(tmp_path)
    runtime.create_agent("B")
    server, thread = _start_server(runtime)
    runner = CliRunner()
    try:
        result = runner.invoke(app, ["tool", "write", "README.md", "without read\n", "--agent", "B", "--project-root", str(tmp_path)])
        output = _combined_output(result)
        assert result.exit_code == 1, output
        assert "must_read_current" in output
        assert "README.md" in output
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_runtime_notifies_bound_opencode_when_agent_goes_stale(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    runtime = Runtime(tmp_path)
    runtime.init_project(tmp_path)
    runtime.create_agent("A")
    runtime.create_agent("B")
    server, thread, requests = _start_fake_opencode(
        [{"id": "ses_B", "directory": str(runtime.workspace("B")), "time": {"updated": 2}}]
    )
    host, port = server.server_address
    try:
        record_opencode_agent(tmp_path, "B", host=host, port=port, workspace=runtime.workspace("B"))
        runtime.read("A", "README.md")
        runtime.read("B", "README.md")

        runtime.write("A", "README.md", "from A\n")

        message_request = next(request for request in requests if request["path"] == "/session/ses_B/message")
        assert message_request["body"]["noReply"] is True
        assert message_request["body"]["parts"][0]["type"] == "text"
        assert "MESI NOTICE" in message_request["body"]["parts"][0]["text"]
        assert "README.md" in message_request["body"]["parts"][0]["text"]
        assert not any(request["path"].startswith("/tui/") for request in requests)
        notify = next(event for event in runtime.events() if event["type"] == "opencode_notify")
        assert notify["reason"] == "ok"
        assert notify["metadata"]["posted"] == ["session_message"]
        binding = get_opencode_agent(tmp_path, "B")
        assert binding is not None
        assert binding["session_id"] == "ses_B"
        assert binding["status"] == "active"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_runtime_refreshes_stale_opencode_session_binding_before_notify(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    runtime = Runtime(tmp_path)
    runtime.init_project(tmp_path)
    runtime.create_agent("A")
    runtime.create_agent("B")
    server, thread, requests = _start_fake_opencode(
        [
            {"id": "ses_old", "directory": str(runtime.workspace("B")), "time": {"updated": 1}},
            {"id": "ses_current", "directory": str(runtime.workspace("B")), "time": {"updated": 20}},
        ]
    )
    host, port = server.server_address
    try:
        record_opencode_agent(
            tmp_path,
            "B",
            host=host,
            port=port,
            workspace=runtime.workspace("B"),
            session_id="ses_old",
        )
        runtime.read("A", "README.md")
        runtime.read("B", "README.md")

        runtime.write("A", "README.md", "from A\n")

        assert any(request["path"] == "/session/ses_current/message" for request in requests)
        assert not any(request["path"] == "/session/ses_old/message" for request in requests)
        binding = get_opencode_agent(tmp_path, "B")
        assert binding is not None
        assert binding["session_id"] == "ses_current"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_runtime_marks_unreachable_opencode_binding_when_stale_notify_fails(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    runtime = Runtime(tmp_path)
    runtime.init_project(tmp_path)
    runtime.create_agent("A")
    runtime.create_agent("B")
    record_opencode_agent(tmp_path, "B", host="127.0.0.1", port=9, workspace=runtime.workspace("B"), session_id="ses_B")
    runtime.read("A", "README.md")
    runtime.read("B", "README.md")

    runtime.write("A", "README.md", "from A\n")

    notify = next(event for event in runtime.events() if event["type"] == "opencode_notify_failed")
    assert notify["reason"] == "opencode_unreachable"
    binding = get_opencode_agent(tmp_path, "B")
    assert binding is not None
    assert binding["status"] == "unreachable"
    assert "last_error" in binding


def test_runtime_creates_opencode_session_when_binding_has_no_session(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    runtime = Runtime(tmp_path)
    runtime.init_project(tmp_path)
    runtime.create_agent("A")
    runtime.create_agent("B")
    server, thread, requests = _start_fake_opencode([])
    host, port = server.server_address
    try:
        record_opencode_agent(tmp_path, "B", host=host, port=port, workspace=runtime.workspace("B"))
        runtime.read("A", "README.md")
        runtime.read("B", "README.md")

        runtime.write("A", "README.md", "from A\n")

        paths = [request["path"] for request in requests]
        assert "/session" in paths
        assert "/session/ses_created/message" in paths
        binding = get_opencode_agent(tmp_path, "B")
        assert binding is not None
        assert binding["session_id"] == "ses_created"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_agent_start_dry_run_records_fixed_opencode_binding(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    runtime = Runtime(tmp_path)
    runtime.init_project(tmp_path)
    runtime.create_agent("A")

    runner = CliRunner()
    result = runner.invoke(app, ["agent", "start", "A", "--project-root", str(tmp_path), "--port", "4101", "--dry-run"])

    assert result.exit_code == 0, result.output
    binding = get_opencode_agent(tmp_path, "A")
    assert binding is not None
    assert binding["port"] == 4101
    assert binding["workspace"] == str(runtime.workspace("A"))
    assert "opencode" in binding["command"][0]
    assert opencode_bindings_path(tmp_path).exists()
    assert not legacy_opencode_bindings_path(tmp_path).exists()


def test_agent_start_without_port_allocates_available_port(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    runtime = Runtime(tmp_path)
    runtime.init_project(tmp_path)
    runtime.create_agent("A")
    occupied_port = default_opencode_port("A", tmp_path)
    runner = CliRunner()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", occupied_port))
        result = runner.invoke(app, ["agent", "start", "A", "--project-root", str(tmp_path), "--dry-run"])

    assert result.exit_code == 0, result.output
    binding = get_opencode_agent(tmp_path, "A")
    assert binding is not None
    assert 4100 <= binding["port"] < 6100
    assert binding["port"] != occupied_port


def test_agent_bind_opencode_records_latest_session(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    runtime = Runtime(tmp_path)
    runtime.init_project(tmp_path)
    runtime.create_agent("A")
    server, thread, _requests = _start_fake_opencode(
        [{"id": "ses_A", "directory": str(runtime.workspace("A")), "time": {"updated": 5}}]
    )
    host, port = server.server_address
    runner = CliRunner()
    try:
        result = runner.invoke(
            app,
            ["agent", "bind-opencode", "A", "--project-root", str(tmp_path), "--host", host, "--port", str(port)],
        )

        assert result.exit_code == 0, result.output
        binding = get_opencode_agent(tmp_path, "A")
        assert binding is not None
        assert binding["session_id"] == "ses_A"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_legacy_opencode_binding_file_is_migrated(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    runtime = Runtime(tmp_path)
    runtime.init_project(tmp_path)
    runtime.create_agent("A")
    legacy = legacy_opencode_bindings_path(tmp_path)
    legacy.write_text(
        json.dumps(
            {
                "agents": {
                    "A": {
                        "agent": "A",
                        "append_prompt": True,
                        "host": "127.0.0.1",
                        "port": 4101,
                        "url": "http://127.0.0.1:4101",
                        "workspace": str(runtime.workspace("A")),
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    binding = get_opencode_agent(tmp_path, "A")

    assert binding is not None
    assert binding["port"] == 4101
    assert opencode_bindings_path(tmp_path).exists()
    assert not legacy.exists()


def _json(url: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="GET" if payload is None else "POST")
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _start_server(runtime: Runtime) -> tuple[MesiHTTPServer, threading.Thread]:
    server = MesiHTTPServer(("127.0.0.1", 0), runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    write_daemon_config(runtime.project_root, host, port)
    return server, thread


def _combined_output(result) -> str:
    try:
        return result.output + result.stderr
    except ValueError:
        return result.output


def _start_fake_opencode(sessions: list[dict]) -> tuple[ThreadingHTTPServer, threading.Thread, list[dict]]:
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/global/health":
                self._send_json({"healthy": True, "version": "test"})
            elif self.path == "/session":
                self._send_json(sessions)
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            body = json.loads(raw)
            requests.append({"path": self.path, "body": body})
            if self.path == "/session":
                created = {
                    "id": "ses_created",
                    "directory": sessions[0]["directory"] if sessions else "",
                    "time": {"updated": 10},
                    "title": body.get("title", ""),
                }
                sessions.append(created)
                self._send_json(created)
            elif self.path.startswith("/session/") and self.path.endswith("/message"):
                session_id = self.path.split("/")[2]
                self._send_json(
                    {
                        "info": {
                            "id": "msg_test",
                            "role": "user",
                            "sessionID": session_id,
                        },
                        "parts": body.get("parts", []),
                    }
                )
            else:
                self._send_json(True)

        def log_message(self, format, *args):
            return

        def _send_json(self, payload):
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, requests
