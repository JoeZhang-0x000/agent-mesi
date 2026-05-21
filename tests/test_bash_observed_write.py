from __future__ import annotations

import pytest

from mesi_runtime.errors import Blocked
from mesi_runtime.runtime import Runtime


@pytest.fixture
def runtime(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    runtime = Runtime(tmp_path)
    runtime.init_project(tmp_path)
    runtime.create_agent("A")
    runtime.create_agent("B")
    return runtime


def test_bash_observed_write_advances_head_and_stales_readers(runtime):
    runtime.read("A", "README.md")
    runtime.read("B", "README.md")
    begin = runtime.bash_begin("B", "echo test >> README.md")

    (runtime.workspace("B") / "README.md").write_text("hello\ntest\n", encoding="utf-8")
    result = runtime.bash_end("B", begin["snapshot_id"], 0)

    assert result["ok"] is True
    assert result["changed"] == ["README.md"]
    assert (runtime.store / "README.md").read_text(encoding="utf-8") == "hello\ntest\n"
    assert runtime.stale("A")["stale"][0]["path"] == "README.md"


def test_bash_preflight_blocks_stale_agent(runtime):
    runtime.read("A", "README.md")
    runtime.read("B", "README.md")
    runtime.write("B", "README.md", "new\n")

    with pytest.raises(Blocked):
        runtime.bash_begin("A", "echo blocked >> notes.md")


def test_bash_base_mismatch_commits_nothing(runtime):
    begin = runtime.bash_begin("A", "echo local >> README.md")
    runtime.write("B", "README.md", "updated by B\n")
    (runtime.workspace("A") / "README.md").write_text("local change\n", encoding="utf-8")

    result = runtime.bash_end("A", begin["snapshot_id"], 0)

    assert result["ok"] is False
    assert result["reason"] == "workspace_base_mismatch"
    assert (runtime.store / "README.md").read_text(encoding="utf-8") == "updated by B\n"


def test_bash_observed_write_copies_binary_files(runtime):
    begin = runtime.bash_begin("A", "write binary")
    (runtime.workspace("A") / "image.bin").write_bytes(b"\x80\x81\x00")

    result = runtime.bash_end("A", begin["snapshot_id"], 0)

    assert result["ok"] is True
    assert result["changed"] == ["image.bin"]
    assert (runtime.store / "image.bin").read_bytes() == b"\x80\x81\x00"
