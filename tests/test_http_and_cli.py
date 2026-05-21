from __future__ import annotations

import json
import threading
import urllib.request

from typer.testing import CliRunner

from mesi_runtime.cli import app
from mesi_runtime.runtime import Runtime
from mesi_runtime.server import MesiHTTPServer


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


def _json(url: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="GET" if payload is None else "POST")
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))
