from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Iterable

from .constants import (
    ABSENT_VERSION,
    IGNORED_DIR_NAMES,
    IGNORED_FILE_NAMES,
    IGNORED_FILE_PATHS,
    MESI_DIR,
    daemon_path,
)
from .errors import PathRejected


def resolve_project_root(start: Path | str | None = None) -> Path:
    env_root = os.environ.get("MESI_PROJECT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    current = Path(start or os.getcwd()).expanduser().resolve()
    if current.is_file():
        current = current.parent

    parts = current.parts
    if MESI_DIR in parts:
        idx = parts.index(MESI_DIR)
        if idx > 0:
            return Path(*parts[:idx]).resolve()

    for candidate in [current, *current.parents]:
        if daemon_path(candidate).exists() or (candidate / MESI_DIR).exists():
            return candidate.resolve()
    return current


def infer_agent_id(start: Path | str | None = None) -> str:
    env_agent = os.environ.get("MESI_AGENT_ID")
    if env_agent:
        return env_agent

    current = Path(start or os.getcwd()).expanduser().resolve()
    parts = current.parts
    for idx, part in enumerate(parts):
        if part == MESI_DIR and idx + 2 < len(parts) and parts[idx + 1] == "ws":
            return parts[idx + 2]
    raise PathRejected("Unable to infer MESI agent id. Set MESI_AGENT_ID or run inside .mesi/ws/<agent>.")


def normalize_managed_path(path: str) -> str:
    if not isinstance(path, str) or not path.strip():
        raise PathRejected("Path must be a non-empty string.")

    raw = path.replace("\\", "/")
    pure = PurePosixPath(raw)
    if pure.is_absolute():
        raise PathRejected(f"Absolute paths are not managed: {path}")
    if any(part in ("", ".", "..") for part in pure.parts):
        raise PathRejected(f"Path traversal is not allowed: {path}")
    if any(part == MESI_DIR for part in pure.parts):
        raise PathRejected(f"Runtime metadata is not managed: {path}")
    return pure.as_posix()


def ensure_within(root: Path, target: Path) -> None:
    root_resolved = root.resolve()
    target_resolved = target.resolve(strict=False)
    try:
        target_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise PathRejected(f"Path escapes managed root: {target}") from exc


def managed_target(root: Path, rel_path: str) -> Path:
    rel = normalize_managed_path(rel_path)
    target = root / rel
    ensure_within(root, target)
    if target.exists() and target.is_dir():
        raise PathRejected(f"Managed path is a directory, not a file: {rel}")
    return target


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_version(path: Path) -> str:
    if not path.exists():
        return ABSENT_VERSION
    if not path.is_file():
        raise PathRejected(f"Managed path is not a regular file: {path}")
    return hash_bytes(path.read_bytes())


def should_ignore_dir(name: str) -> bool:
    return name in IGNORED_DIR_NAMES


def should_ignore_file(rel_path: str) -> bool:
    name = PurePosixPath(rel_path).name
    return rel_path in IGNORED_FILE_PATHS or name in IGNORED_FILE_NAMES


def iter_managed_files(root: Path) -> Iterable[str]:
    root = root.resolve()
    if not root.exists():
        return

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not should_ignore_dir(d)]
        base = Path(dirpath)
        for filename in filenames:
            path = base / filename
            if path.is_symlink() or not path.is_file():
                continue
            ensure_within(root, path)
            rel_path = path.relative_to(root).as_posix()
            if should_ignore_file(rel_path):
                continue
            yield rel_path


def snapshot(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for rel_path in sorted(iter_managed_files(root)):
        result[rel_path] = file_version(managed_target(root, rel_path))
    return result


def copy_file(src_root: Path, dst_root: Path, rel_path: str) -> None:
    rel = normalize_managed_path(rel_path)
    src = managed_target(src_root, rel)
    dst = managed_target(dst_root, rel)
    if not src.exists():
        if dst.exists():
            dst.unlink()
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def copy_tree_contents(src_root: Path, dst_root: Path) -> None:
    dst_root.mkdir(parents=True, exist_ok=True)
    for rel_path in iter_managed_files(src_root):
        copy_file(src_root, dst_root, rel_path)


def write_text_file(root: Path, rel_path: str, content: str) -> str:
    target = managed_target(root, rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return file_version(target)
