from __future__ import annotations

import pytest

from mesi_runtime.constants import ABSENT_VERSION
from mesi_runtime.errors import PathRejected
from mesi_runtime.paths import file_version, hash_bytes, managed_target, normalize_managed_path


def test_rejects_unsafe_paths(tmp_path):
    for path in ["/tmp/file", "../file", "a/../file", ".mesi/state.sqlite", "src/.mesi/file"]:
        with pytest.raises(PathRejected):
            normalize_managed_path(path)


def test_rejects_symlink_escape(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside)

    with pytest.raises(PathRejected):
        managed_target(root, "link/escape.txt")


def test_hashes_bytes_and_absent_files(tmp_path):
    target = tmp_path / "file.txt"
    assert file_version(target) == ABSENT_VERSION
    target.write_bytes(b"hello")
    assert file_version(target) == hash_bytes(b"hello")
