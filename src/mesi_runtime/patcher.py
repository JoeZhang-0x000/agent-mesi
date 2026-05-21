from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .errors import Unsupported
from .paths import managed_target, normalize_managed_path


def apply_patch_text(workspace: Path, patch_text: str) -> str:
    if patch_text.lstrip().startswith("*** Begin Patch"):
        return apply_marker_patch(workspace, patch_text)

    if shutil.which("patch") is None:
        raise Unsupported("The system 'patch' command is unavailable.")
    proc = subprocess.run(
        ["patch", "-p0"],
        input=patch_text,
        cwd=workspace,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise Unsupported(proc.stderr.strip() or proc.stdout.strip() or "patch command failed")
    return proc.stdout.strip()


def apply_marker_patch(workspace: Path, patch_text: str) -> str:
    lines = patch_text.splitlines()
    idx = 0
    messages: list[str] = []
    while idx < len(lines):
        line = lines[idx]
        if line == "*** Begin Patch" or not line:
            idx += 1
            continue
        if line == "*** End Patch":
            break
        if line.startswith("*** Add File: "):
            rel = normalize_managed_path(line.removeprefix("*** Add File: ").strip())
            idx += 1
            content: list[str] = []
            while idx < len(lines) and not lines[idx].startswith("*** "):
                if lines[idx].startswith("+"):
                    content.append(lines[idx][1:])
                idx += 1
            target = managed_target(workspace, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(content) + ("\n" if content else ""), encoding="utf-8")
            messages.append(f"added {rel}")
            continue
        if line.startswith("*** Delete File: "):
            rel = normalize_managed_path(line.removeprefix("*** Delete File: ").strip())
            target = managed_target(workspace, rel)
            if target.exists():
                target.unlink()
            messages.append(f"deleted {rel}")
            idx += 1
            continue
        if line.startswith("*** Update File: "):
            rel = normalize_managed_path(line.removeprefix("*** Update File: ").strip())
            idx = _apply_update(workspace, rel, lines, idx + 1)
            messages.append(f"updated {rel}")
            continue
        raise Unsupported(f"Unsupported patch marker: {line}")
    return "\n".join(messages)


def _apply_update(workspace: Path, rel: str, lines: list[str], idx: int) -> int:
    target = managed_target(workspace, rel)
    content = target.read_text(encoding="utf-8")
    current = content.splitlines()
    while idx < len(lines) and not lines[idx].startswith("*** "):
        if lines[idx].startswith("@@"):
            idx += 1
            old: list[str] = []
            new: list[str] = []
            while idx < len(lines) and not lines[idx].startswith("@@") and not lines[idx].startswith("*** "):
                prefix = lines[idx][:1]
                value = lines[idx][1:] if prefix in {" ", "-", "+"} else lines[idx]
                if prefix in {" ", "-"}:
                    old.append(value)
                if prefix in {" ", "+"}:
                    new.append(value)
                idx += 1
            current = _replace_once(current, old, new)
            continue
        idx += 1
    target.write_text("\n".join(current) + ("\n" if current else ""), encoding="utf-8")
    return idx


def _replace_once(current: list[str], old: list[str], new: list[str]) -> list[str]:
    if not old:
        return new + current
    for pos in range(0, len(current) - len(old) + 1):
        if current[pos : pos + len(old)] == old:
            return current[:pos] + new + current[pos + len(old) :]
    raise Unsupported("Patch hunk did not match target file.")
