from __future__ import annotations

import pytest

from mesi_runtime.constants import store_dir
from mesi_runtime.errors import NotFound
from mesi_runtime.opencode import TOOL_NAMES
from mesi_runtime.runtime import Runtime


def test_init_excludes_metadata_and_creates_heads(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("ignored", encoding="utf-8")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.local.json").write_text("ignored", encoding="utf-8")
    (tmp_path / ".opencode").mkdir()
    (tmp_path / ".opencode" / "package-lock.json").write_text("ignored", encoding="utf-8")
    (tmp_path / ".opencode" / "tools").mkdir()
    (tmp_path / ".opencode" / "tools" / "read.ts").write_text("stale local tool", encoding="utf-8")
    (tmp_path / ".opencode" / "tools" / "custom.ts").write_text("managed", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.js").write_text("ignored", encoding="utf-8")

    runtime = Runtime(tmp_path)
    result = runtime.init_project(tmp_path)

    assert result["ok"] is True
    assert (store_dir(tmp_path) / "README.md").read_text(encoding="utf-8") == "hello\n"
    assert not (store_dir(tmp_path) / ".git").exists()
    assert not (store_dir(tmp_path) / ".claude").exists()
    assert not (store_dir(tmp_path) / ".opencode" / "package-lock.json").exists()
    assert (store_dir(tmp_path) / ".opencode" / "tools" / "read.ts").exists()
    assert (store_dir(tmp_path) / ".opencode" / "tools" / "custom.ts").exists()
    assert not (store_dir(tmp_path) / "node_modules").exists()
    with runtime.state.connect() as conn:
        heads = runtime.state.all_heads(conn=conn)
    assert ".opencode/tools/read.ts" in heads
    assert ".opencode/tools/custom.ts" in heads
    assert "README.md" in heads
    assert "stale local tool" not in (tmp_path / ".opencode" / "tools" / "read.ts").read_text(encoding="utf-8")


def test_init_installs_mesi_opencode_tools_before_snapshot(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    runtime = Runtime(tmp_path)

    result = runtime.init_project(tmp_path)

    assert result["ok"] is True
    assert set(result["opencode_tools"]) == {f".opencode/tools/{name}" for name in TOOL_NAMES}
    for name in TOOL_NAMES:
        project_tool = tmp_path / ".opencode" / "tools" / name
        store_tool = store_dir(tmp_path) / ".opencode" / "tools" / name
        assert project_tool.exists()
        assert store_tool.exists()
        assert '@opencode-ai/plugin' in project_tool.read_text(encoding="utf-8")


def test_agent_create_materializes_installed_opencode_tools(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    runtime = Runtime(tmp_path)
    runtime.init_project(tmp_path)

    runtime.create_agent("A")

    read_tool = runtime.workspace("A") / ".opencode" / "tools" / "read.ts"
    assert read_tool.exists()
    content = read_tool.read_text(encoding="utf-8")
    assert "MESI_PYTHON" in content
    assert "MESI_RUNTIME_PYTHONPATH" in content
    assert "-m mesi_runtime" in content
    assert ".quiet().nothrow()" in content


def test_agent_create_materializes_workspace_and_bases(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    runtime = Runtime(tmp_path)
    runtime.init_project(tmp_path)

    result = runtime.create_agent("A")

    assert result["ok"] is True
    assert (runtime.workspace("A") / "README.md").read_text(encoding="utf-8") == "hello\n"
    with runtime.state.connect() as conn:
        base = runtime.state.get_workspace_base("A", "README.md", conn=conn)
        head = runtime.state.get_head("README.md", conn=conn)
    assert base == head


def test_agent_recreate_clears_agent_state(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    runtime = Runtime(tmp_path)
    runtime.init_project(tmp_path)
    runtime.create_agent("A")
    runtime.create_agent("B")
    runtime.read("A", "README.md")
    runtime.read("B", "README.md")
    runtime.write("B", "README.md", "new\n")
    assert runtime.stale("A")["stale"]

    runtime.create_agent("A")

    assert runtime.stale("A")["stale"] == []


def test_operations_reject_unknown_agents(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    runtime = Runtime(tmp_path)
    runtime.init_project(tmp_path)

    with pytest.raises(NotFound):
        runtime.read("missing", "README.md")
