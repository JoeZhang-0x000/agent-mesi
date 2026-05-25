from __future__ import annotations

import pytest

from mesi_runtime.errors import Blocked, Conflict
from mesi_runtime.runtime import Runtime


@pytest.fixture
def runtime(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    runtime = Runtime(tmp_path)
    runtime.init_project(tmp_path)
    runtime.create_agent("A")
    runtime.create_agent("B")
    return runtime


def test_stale_blocks_all_writes_until_refresh(runtime):
    a_read = runtime.read("A", "README.md")
    runtime.read("B", "README.md")

    b_write = runtime.write("B", "README.md", "hello from B\n")

    stale = runtime.stale("A")["stale"]
    assert stale == [
        {
            "agent_id": "A",
            "path": "README.md",
            "old_version": a_read["version"],
            "new_version": b_write["new_version"],
            "created_at": stale[0]["created_at"],
        }
    ]

    with pytest.raises(Blocked):
        runtime.write("A", "notes.md", "blocked\n")

    refresh = runtime.refresh("A")
    assert refresh["resolved"] == ["README.md"]
    runtime.write("A", "notes.md", "ok\n")
    assert (runtime.store / "notes.md").read_text(encoding="utf-8") == "ok\n"


def test_read_latest_resolves_stale_for_path(runtime):
    runtime.read("A", "README.md")
    runtime.read("B", "README.md")
    runtime.write("B", "README.md", "new\n")

    result = runtime.read("A", "README.md")

    assert result["ok"] is True
    assert runtime.stale("A")["stale"] == []


def test_existing_stale_notice_tracks_latest_head(runtime):
    first = runtime.read("A", "README.md")
    runtime.read("B", "README.md")
    second = runtime.write("B", "README.md", "v2\n")
    third = runtime.write("B", "README.md", "v3\n")

    stale = runtime.stale("A")["stale"]
    assert len(stale) == 1
    assert stale[0]["old_version"] == first["version"]
    assert stale[0]["new_version"] == third["new_version"]
    assert stale[0]["new_version"] != second["new_version"]

    refresh = runtime.refresh("A")
    assert refresh["resolved"] == ["README.md"]
    assert runtime.stale("A")["stale"] == []


def test_existing_file_write_requires_current_read(runtime):
    with pytest.raises(Conflict):
        runtime.write("B", "README.md", "without read\n")

    events = runtime.events()
    assert events[-1]["type"] == "write_blocked"
    assert events[-1]["path"] == "README.md"
    assert events[-1]["reason"] == "must_read_current"

    runtime.read("A", "README.md")
    runtime.read("B", "README.md")
    result = runtime.write("B", "README.md", "after read\n")

    assert result["ok"] is True
    assert runtime.stale("A")["stale"][0]["path"] == "README.md"


def test_new_file_creation_uses_absent_version(runtime):
    first = runtime.write("A", "new.txt", "new\n")

    assert first["old_version"] == "absent"
    assert (runtime.store / "new.txt").read_text(encoding="utf-8") == "new\n"
    with pytest.raises(Conflict):
        runtime.write("B", "new.txt", "competing\n")
