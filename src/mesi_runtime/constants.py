from __future__ import annotations

from pathlib import Path

MESI_DIR = ".mesi"
STORE_DIR = "store/current"
WORKSPACES_DIR = "ws"
STATE_FILE = "state.sqlite"
EVENTS_FILE = "events.jsonl"
DAEMON_FILE = "daemon.json"
OPENCODE_BINDINGS_FILE = "opencode-bindings.json"
LEGACY_OPENCODE_BINDINGS_FILE = "opencode.json"
ABSENT_VERSION = "absent"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_PYTHON = "/Users/zhangxin/Desktop/Aeloon-Pro/.venv/bin/python"

IGNORED_DIR_NAMES = {
    ".mesi",
    ".git",
    ".claude",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    "node_modules",
    "dist",
    "build",
    "target",
    ".tox",
}

IGNORED_FILE_PATHS = {
    ".opencode/.gitignore",
    ".opencode/bun.lock",
    ".opencode/package-lock.json",
    ".opencode/package.json",
}

IGNORED_FILE_NAMES = {
    ".DS_Store",
}

TEXT_EXTENSIONS = {
    ".bash",
    ".c",
    ".cfg",
    ".conf",
    ".cpp",
    ".css",
    ".csv",
    ".env",
    ".go",
    ".h",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".lock",
    ".md",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


def mesi_dir(project_root: Path) -> Path:
    return project_root / MESI_DIR


def store_dir(project_root: Path) -> Path:
    return mesi_dir(project_root) / STORE_DIR


def workspaces_dir(project_root: Path) -> Path:
    return mesi_dir(project_root) / WORKSPACES_DIR


def workspace_dir(project_root: Path, agent_id: str) -> Path:
    return workspaces_dir(project_root) / agent_id


def state_path(project_root: Path) -> Path:
    return mesi_dir(project_root) / STATE_FILE


def events_path(project_root: Path) -> Path:
    return mesi_dir(project_root) / EVENTS_FILE


def daemon_path(project_root: Path) -> Path:
    return mesi_dir(project_root) / DAEMON_FILE


def opencode_bindings_path(project_root: Path) -> Path:
    return mesi_dir(project_root) / OPENCODE_BINDINGS_FILE


def legacy_opencode_bindings_path(project_root: Path) -> Path:
    return mesi_dir(project_root) / LEGACY_OPENCODE_BINDINGS_FILE
