from __future__ import annotations

import json
import socket
import sys
import time
import urllib.error
import urllib.request
from hashlib import sha256
from pathlib import Path
from typing import Any

from .constants import DEFAULT_HOST, legacy_opencode_bindings_path, opencode_bindings_path
from .display import format_version_pair


TOOL_NAMES = (
    "read.ts",
    "write.ts",
    "edit.ts",
    "bash.ts",
    "patch.ts",
    "apply_patch.ts",
    "mesi_status.ts",
    "mesi_refresh.ts",
)


COMMON_HEADER = """import {{ tool }} from "@opencode-ai/plugin"

const PYTHON = process.env.MESI_PYTHON ?? {python}
const MESI_RUNTIME_PYTHONPATH = {runtime_pythonpath}

function projectRoot(directory: string, worktree?: string) {{
  const marker = "/.mesi/ws/"
  const idx = directory.indexOf(marker)
  if (idx >= 0) return directory.slice(0, idx)
  return worktree || directory
}}

function agentId(directory: string) {{
  const parts = directory.split("/")
  const idx = parts.indexOf(".mesi")
  if (idx >= 0 && parts[idx + 1] === "ws" && parts[idx + 2]) return parts[idx + 2]
  return process.env.MESI_AGENT_ID || "default"
}}

async function runMesi(context: any, args: string[]) {{
  const root = projectRoot(context.directory, context.worktree)
  const agent = agentId(context.directory)
  const pythonPath = [MESI_RUNTIME_PYTHONPATH, `${{root}}/src`, root, process.env.PYTHONPATH ?? ""].filter(Boolean).join(":")
  const result = await Bun.$`env PYTHONPATH=${{pythonPath}} MESI_PROJECT_ROOT=${{root}} MESI_AGENT_ID=${{agent}} ${{PYTHON}} -m mesi_runtime ${{args}}`.cwd(context.directory).quiet().nothrow()
  const output = `${{result.stdout.toString()}}${{result.stderr.toString()}}`
  if (output.trim()) return output
  if (result.exitCode !== 0) return `MESI tool failed with exit code ${{result.exitCode}}`
  return ""
}}

"""


TOOL_BODIES = {
    "read.ts": """export default tool({
  description: "Read a file through MESI coherence tracking",
  args: {
    path: tool.schema.string().describe("Project-relative path to read"),
  },
  async execute(args, context) {
    return (await runMesi(context, ["tool", "read", args.path])).trim()
  },
})
""",
    "write.ts": """export default tool({
  description: "Write a file through MESI coherence checks",
  args: {
    path: tool.schema.string().describe("Project-relative path to write"),
    content: tool.schema.string().describe("Full file content"),
  },
  async execute(args, context) {
    return (await runMesi(context, ["tool", "write", args.path, args.content])).trim()
  },
})
""",
    "edit.ts": """export default tool({
  description: "Replace text in a file through MESI coherence checks",
  args: {
    path: tool.schema.string().describe("Project-relative path to edit"),
    oldString: tool.schema.string().describe("Exact text to replace"),
    newString: tool.schema.string().describe("Replacement text"),
    replaceAll: tool.schema.boolean().optional().describe("Replace all matches instead of exactly one"),
  },
  async execute(args, context) {
    const command = ["tool", "edit", args.path, args.oldString, args.newString]
    if (args.replaceAll) command.push("--replace-all")
    return (await runMesi(context, command)).trim()
  },
})
""",
    "bash.ts": """export default tool({
  description: "Run shell commands through MESI pre/post diff tracking",
  args: {
    command: tool.schema.string().describe("Command to execute in the agent workspace"),
  },
  async execute(args, context) {
    return await runMesi(context, ["tool", "bash", args.command])
  },
})
""",
    "patch.ts": """export default tool({
  description: "Apply a patch through MESI observed-write tracking",
  args: {
    patchText: tool.schema.string().describe("Unified diff or Begin Patch text"),
  },
  async execute(args, context) {
    return (await runMesi(context, ["tool", "apply-patch", args.patchText])).trim()
  },
})
""",
    "apply_patch.ts": """export default tool({
  description: "Apply a patch through MESI observed-write tracking",
  args: {
    patchText: tool.schema.string().describe("Unified diff or Begin Patch text"),
  },
  async execute(args, context) {
    return (await runMesi(context, ["tool", "apply-patch", args.patchText])).trim()
  },
})
""",
    "mesi_status.ts": """export default tool({
  description: "Show this agent's unresolved MESI stale notices",
  args: {},
  async execute(args, context) {
    return (await runMesi(context, ["tool", "status"])).trim()
  },
})
""",
    "mesi_refresh.ts": """export default tool({
  description: "Refresh stale files from the MESI authoritative store",
  args: {
    paths: tool.schema.array(tool.schema.string()).optional().describe("Specific paths to refresh; omit to refresh all stale paths"),
  },
  async execute(args, context) {
    return (await runMesi(context, ["tool", "refresh", ...(args.paths ?? [])])).trim()
  },
})
""",
}


def install_opencode_tools(project_root: Path, python_executable: str | None = None) -> list[str]:
    tools_dir = project_root / ".opencode" / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)

    installed: list[str] = []
    for name in TOOL_NAMES:
        rendered = _render_tool(name, python_executable or sys.executable)
        target = tools_dir / name
        target.write_text(rendered, encoding="utf-8")
        installed.append(target.relative_to(project_root).as_posix())
    return installed


def _render_tool(name: str, python_executable: str) -> str:
    runtime_pythonpath = Path(__file__).resolve().parent.parent
    return COMMON_HEADER.format(
        python=json.dumps(python_executable),
        runtime_pythonpath=json.dumps(str(runtime_pythonpath)),
    ) + TOOL_BODIES[name]


def default_opencode_port(agent: str, project_root: Path | str | None = None) -> int:
    if project_root is None:
        seed = agent.encode("utf-8")
    else:
        seed = f"{Path(project_root).expanduser().resolve()}:{agent}".encode("utf-8")
    return 4100 + (int(sha256(seed).hexdigest()[:8], 16) % 2000)


def allocate_opencode_port(
    project_root: Path,
    agent: str,
    *,
    host: str = DEFAULT_HOST,
    requested: int | None = None,
) -> int:
    if requested is not None:
        return requested

    binding = get_opencode_agent(project_root, agent)
    existing = binding.get("port") if binding else None
    if isinstance(existing, int) and _port_available(host, existing):
        return existing

    start = default_opencode_port(agent, project_root)
    for offset in range(2000):
        port = 4100 + ((start - 4100 + offset) % 2000)
        if _port_available(host, port):
            return port
    raise RuntimeError("No available opencode port in 4100-6099.")


def load_opencode_bindings(project_root: Path) -> dict[str, Any]:
    path = opencode_bindings_path(project_root)
    if not path.exists():
        return _load_legacy_opencode_bindings(project_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"agents": {}}
    if not isinstance(payload, dict):
        return {"agents": {}}
    agents = payload.get("agents")
    if not isinstance(agents, dict):
        payload["agents"] = {}
    return payload


def _load_legacy_opencode_bindings(project_root: Path) -> dict[str, Any]:
    legacy = legacy_opencode_bindings_path(project_root)
    if not legacy.exists():
        return {"agents": {}}
    try:
        payload = json.loads(legacy.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"agents": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("agents"), dict):
        return {"agents": {}}

    save_opencode_bindings(project_root, payload)
    return payload


def save_opencode_bindings(project_root: Path, payload: dict[str, Any]) -> None:
    path = opencode_bindings_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
    _remove_legacy_opencode_bindings(project_root)


def _remove_legacy_opencode_bindings(project_root: Path) -> None:
    try:
        legacy_opencode_bindings_path(project_root).unlink(missing_ok=True)
    except OSError:
        pass


def _save_opencode_agent(project_root: Path, agent: str, binding: dict[str, Any]) -> None:
    payload = load_opencode_bindings(project_root)
    payload["agents"][agent] = binding
    save_opencode_bindings(project_root, payload)


def record_opencode_agent(
    project_root: Path,
    agent: str,
    *,
    host: str = DEFAULT_HOST,
    port: int | None = None,
    workspace: Path | str | None = None,
    session_id: str | None = None,
    pid: int | None = None,
    command: list[str] | None = None,
    append_prompt: bool = True,
) -> dict[str, Any]:
    actual_port = port if port is not None else default_opencode_port(agent, project_root)
    binding = {
        "agent": agent,
        "host": host,
        "port": actual_port,
        "url": f"http://{host}:{actual_port}",
        "workspace": str(Path(workspace).resolve()) if workspace else None,
        "session_id": session_id,
        "pid": pid,
        "command": command,
        "append_prompt": append_prompt,
        "updated_at": _now_ms(),
    }
    _save_opencode_agent(project_root, agent, binding)
    return binding


def get_opencode_agent(project_root: Path, agent: str) -> dict[str, Any] | None:
    binding = load_opencode_bindings(project_root).get("agents", {}).get(agent)
    return binding if isinstance(binding, dict) else None


def refresh_opencode_agent_session(project_root: Path, agent: str, *, timeout: float = 0.75) -> dict[str, Any]:
    binding = get_opencode_agent(project_root, agent)
    if not binding:
        return {"ok": False, "reason": "not_configured"}

    ok, sessions, error = _opencode_request(binding, "GET", "/session", timeout=timeout)
    if not ok:
        return {"ok": False, "reason": "opencode_unreachable", "error": error, "url": binding["url"]}

    workspace = binding.get("workspace")
    if not workspace:
        return {"ok": False, "reason": "missing_workspace", "url": binding["url"]}

    matches = [
        session
        for session in sessions
        if _same_path(session.get("directory"), workspace)
    ]
    if not matches:
        return {"ok": False, "reason": "session_not_found", "url": binding["url"]}

    latest = max(matches, key=lambda session: session.get("time", {}).get("updated") or session.get("updated") or 0)
    binding["session_id"] = latest.get("id")
    binding["updated_at"] = _now_ms()
    _save_opencode_agent(project_root, agent, binding)
    return {"ok": True, "session_id": binding["session_id"], "url": binding["url"]}


def notify_opencode_stale(project_root: Path, agent: str, path: str, old_version: str, new_version: str) -> dict[str, Any]:
    binding = get_opencode_agent(project_root, agent)
    if not binding:
        return {"ok": False, "reason": "not_configured"}

    health = check_opencode_agent(project_root, agent)
    if not health.get("ok"):
        return health

    session = ensure_opencode_agent_session(project_root, agent)
    if not session.get("ok"):
        return session

    binding = get_opencode_agent(project_root, agent) or binding
    session_id = binding.get("session_id")
    if not session_id:
        return {"ok": False, "reason": "session_not_found", "url": binding["url"]}

    message = (
        "MESI NOTICE: "
        f"{path} is stale ({format_version_pair(old_version, new_version)}). "
        f"Run mesi_refresh {path} or read {path} before writing."
    )
    payload = {"noReply": True, "parts": [{"type": "text", "text": message}]}
    ok, response, error = _opencode_request(binding, "POST", f"/session/{session_id}/message", payload=payload)
    if not ok:
        refreshed = refresh_opencode_agent_session(project_root, agent)
        if refreshed.get("ok") and refreshed.get("session_id") != session_id:
            binding = get_opencode_agent(project_root, agent) or binding
            session_id = refreshed["session_id"]
            ok, response, error = _opencode_request(binding, "POST", f"/session/{session_id}/message", payload=payload)
        if not ok:
            _mark_opencode_binding(project_root, agent, status="session_message_failed", last_error=error)
            return {"ok": False, "reason": "session_message_failed", "error": error, "url": binding["url"], "session_id": session_id}

    _mark_opencode_binding(project_root, agent, status="active", last_error=None)
    message_id = response.get("info", {}).get("id") if isinstance(response, dict) else None
    return {
        "ok": True,
        "reason": "ok",
        "url": binding["url"],
        "posted": ["session_message"],
        "session_id": session_id,
        "message_id": message_id,
    }


def check_opencode_agent(project_root: Path, agent: str, *, timeout: float = 0.75) -> dict[str, Any]:
    binding = get_opencode_agent(project_root, agent)
    if not binding:
        return {"ok": False, "reason": "not_configured"}

    ok, response, error = _opencode_request(binding, "GET", "/global/health", timeout=timeout)
    if not ok:
        _mark_opencode_binding(project_root, agent, status="unreachable", last_error=error)
        return {"ok": False, "reason": "opencode_unreachable", "error": error, "url": binding["url"]}

    _mark_opencode_binding(project_root, agent, status="active", last_error=None)
    return {"ok": True, "reason": "ok", "url": binding["url"], "health": response}


def ensure_opencode_agent_session(project_root: Path, agent: str, *, timeout: float = 0.75) -> dict[str, Any]:
    binding = get_opencode_agent(project_root, agent)
    if not binding:
        return {"ok": False, "reason": "not_configured"}

    refreshed = refresh_opencode_agent_session(project_root, agent, timeout=timeout)
    if refreshed.get("ok"):
        return refreshed
    if refreshed.get("reason") != "session_not_found":
        return refreshed

    ok, response, error = _opencode_request(
        binding,
        "POST",
        "/session",
        payload={"title": f"MESI notifications for {agent}"},
        timeout=timeout,
    )
    if not ok or not isinstance(response, dict) or not response.get("id"):
        _mark_opencode_binding(project_root, agent, status="session_create_failed", last_error=error)
        return {"ok": False, "reason": "session_create_failed", "error": error, "url": binding["url"]}

    _mark_opencode_binding(project_root, agent, status="active", session_id=response["id"], last_error=None)
    return {"ok": True, "reason": "ok", "session_id": response["id"], "url": binding["url"]}


def _mark_opencode_binding(
    project_root: Path,
    agent: str,
    *,
    status: str,
    session_id: str | None = None,
    last_error: str | None = None,
) -> None:
    payload = load_opencode_bindings(project_root)
    binding = payload.get("agents", {}).get(agent)
    if not isinstance(binding, dict):
        return
    binding["status"] = status
    binding["last_checked_at"] = _now_ms()
    if session_id is not None:
        binding["session_id"] = session_id
    if last_error is None:
        binding.pop("last_error", None)
    else:
        binding["last_error"] = last_error
    save_opencode_bindings(project_root, payload)


def _opencode_request(
    binding: dict[str, Any],
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 0.75,
) -> tuple[bool, Any, str | None]:
    raw = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        binding["url"] + path,
        data=raw,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, None, str(exc)

    if not body:
        return True, None, None
    try:
        return True, json.loads(body), None
    except json.JSONDecodeError:
        return True, body, None


def _same_path(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True
