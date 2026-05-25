from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Annotated, Any, Optional

import typer

from .client import Client, format_tool_response
from .constants import DEFAULT_HOST, DEFAULT_PORT
from .display import format_version_pair, short_version, shorten_version_fields
from .errors import MesiError
from .opencode import allocate_opencode_port, record_opencode_agent, refresh_opencode_agent_session
from .patcher import apply_patch_text
from .paths import infer_agent_id, resolve_project_root
from .runtime import Runtime
from .server import MesiHTTPServer, write_daemon_config

app = typer.Typer(no_args_is_help=True)
agent_app = typer.Typer(no_args_is_help=True)
daemon_app = typer.Typer(no_args_is_help=True)
demo_app = typer.Typer(no_args_is_help=True)
tool_app = typer.Typer(no_args_is_help=True)
app.add_typer(agent_app, name="agent")
app.add_typer(daemon_app, name="daemon")
app.add_typer(demo_app, name="demo")
app.add_typer(tool_app, name="tool")


def echo_json(payload: object) -> None:
    typer.echo(json.dumps(shorten_version_fields(payload), sort_keys=True, indent=2))


def fail(exc: MesiError) -> None:
    typer.echo(json.dumps({"ok": False, "error": exc.code, "message": str(exc)}, sort_keys=True), err=True)
    raise typer.Exit(1)


def tool_context(agent: Optional[str], project_root: Optional[Path]) -> tuple[str, Client]:
    agent_id = agent or infer_agent_id()
    client = Client(project_root)
    return agent_id, client


def tool_stale(client: Client, agent_id: str) -> list[dict[str, str]]:
    return client.get(f"/agent/{agent_id}/stale").get("stale", [])


def echo_stale_notice(client: Client, agent_id: str) -> list[dict[str, str]]:
    stale = tool_stale(client, agent_id)
    if stale:
        typer.echo(format_stale_notice(stale))
    return stale


def echo_read_notice(path: str, stale_before: list[dict[str, str]], stale_after: list[dict[str, str]]) -> None:
    remaining = {row["path"] for row in stale_after}
    resolved = next((row for row in stale_before if row["path"] == path and path not in remaining), None)
    if resolved is None:
        if stale_before:
            typer.echo(format_stale_notice(stale_before))
        return

    version_pair = format_version_pair(resolved["old_version"], resolved["new_version"])
    typer.echo(f"MESI NOTICE: reading latest resolved stale for {path} ({version_pair}).")
    if stale_after:
        typer.echo(format_stale_notice(stale_after))


def read_with_notice(client: Client, agent_id: str, path: str) -> dict[str, Any]:
    stale_before = tool_stale(client, agent_id)
    result = client.post(f"/agent/{agent_id}/read", {"path": path})
    echo_read_notice(result.get("path", path), stale_before, tool_stale(client, agent_id))
    return result


def tool_workspace_context(agent: Optional[str], project_root: Optional[Path]) -> tuple[Path, str, Client]:
    root = resolve_project_root(project_root)
    agent_id = agent or infer_agent_id()
    return root, agent_id, Client(root)


@app.command("init")
def init(project_root: Annotated[Path, typer.Argument()] = Path(".")) -> None:
    runtime = Runtime(project_root)
    echo_json(runtime.init_project(project_root))


@app.command("start")
def start(
    project_root: Annotated[Path, typer.Argument()] = Path("."),
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    interval: float = 0.5,
    color: bool = True,
) -> None:
    start_daemon_with_log(project_root, host, port, interval, color=color)


@agent_app.command("create")
def agent_create(agent_id: str, project_root: Path = Path(".")) -> None:
    echo_json(Runtime(project_root).create_agent(agent_id))


@agent_app.command("bind-opencode")
def agent_bind_opencode(
    agent_id: str,
    project_root: Path = Path("."),
    host: str = DEFAULT_HOST,
    port: Optional[int] = None,
    session: Optional[str] = None,
    append_prompt: bool = True,
) -> None:
    try:
        runtime = Runtime(project_root)
        runtime.stale(agent_id)
        binding = record_opencode_agent(
            runtime.project_root,
            agent_id,
            host=host,
            port=port,
            workspace=runtime.workspace(agent_id),
            session_id=session,
            append_prompt=append_prompt,
        )
        session_result = refresh_opencode_agent_session(runtime.project_root, agent_id)
        if session_result.get("ok"):
            binding["session_id"] = session_result.get("session_id")
        echo_json({"ok": True, "agent": agent_id, "opencode": binding, "session": session_result})
    except MesiError as exc:
        fail(exc)


@agent_app.command("start")
def agent_start(
    agent_id: str,
    project_root: Path = Path("."),
    host: str = DEFAULT_HOST,
    port: Optional[int] = None,
    session: Optional[str] = None,
    continue_last: Annotated[bool, typer.Option("--continue")] = False,
    model: Optional[str] = None,
    opencode_agent: Annotated[Optional[str], typer.Option("--opencode-agent")] = None,
    append_prompt: bool = True,
    dry_run: bool = False,
) -> None:
    try:
        runtime = Runtime(project_root)
        runtime.stale(agent_id)
    except MesiError as exc:
        fail(exc)

    try:
        actual_port = allocate_opencode_port(runtime.project_root, agent_id, host=host, requested=port)
    except RuntimeError as exc:
        fail(MesiError(str(exc), code="opencode_port_unavailable", status_code=409))
    workspace = runtime.workspace(agent_id)
    command = ["opencode", str(workspace), "--hostname", host, "--port", str(actual_port)]
    if session:
        command.extend(["--session", session])
    if continue_last:
        command.append("--continue")
    if model:
        command.extend(["--model", model])
    if opencode_agent:
        command.extend(["--agent", opencode_agent])

    binding = record_opencode_agent(
        runtime.project_root,
        agent_id,
        host=host,
        port=actual_port,
        workspace=workspace,
        session_id=session,
        command=command,
        append_prompt=append_prompt,
    )
    echo_json({"ok": True, "agent": agent_id, "workspace": str(workspace), "opencode": binding, "command": command, "dry_run": dry_run})
    if dry_run:
        return

    try:
        process = subprocess.Popen(command, cwd=workspace)
    except OSError as exc:
        fail(MesiError(f"Unable to start opencode: {exc}", code="opencode_start_failed", status_code=500))
    record_opencode_agent(
        runtime.project_root,
        agent_id,
        host=host,
        port=actual_port,
        workspace=workspace,
        session_id=session,
        pid=process.pid,
        command=command,
        append_prompt=append_prompt,
    )
    stop = threading.Event()
    thread = threading.Thread(target=_capture_opencode_session, args=(runtime.project_root, agent_id, stop), daemon=True)
    thread.start()
    try:
        raise typer.Exit(process.wait())
    finally:
        stop.set()
        thread.join(timeout=1)


@daemon_app.command("start")
def daemon_start(
    project_root: Path = Path("."),
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    interval: float = 0.5,
    color: bool = True,
) -> None:
    start_daemon_with_log(project_root, host, port, interval, color=color)


def start_daemon_with_log(project_root: Path, host: str, port: int, interval: float, *, color: bool = True) -> None:
    runtime = Runtime(project_root)
    httpd = MesiHTTPServer((host, port), runtime)
    actual_host, actual_port = httpd.server_address
    write_daemon_config(runtime.project_root, actual_host, actual_port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    typer.echo(f"MESI daemon listening on http://{actual_host}:{actual_port}")
    typer.echo("MESI event log:")
    try:
        stream_events(runtime, interval=interval, color=color)
    except KeyboardInterrupt:
        typer.echo("MESI daemon stopping.")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


@app.command("monitor")
def monitor(project_root: Path = Path("."), once: bool = False, interval: float = 0.5, color: bool = True) -> None:
    runtime = Runtime(project_root)
    stream_events(runtime, once=once, interval=interval, color=color)


def stream_events(runtime: Runtime, once: bool = False, interval: float = 0.5, *, color: bool = True) -> None:
    last = 0
    while True:
        events = runtime.events(last)
        for event in events:
            last = event["seq"]
            typer.echo(format_event(event, color=color), color=color)
        if once:
            break
        time.sleep(interval)


def _capture_opencode_session(project_root: Path, agent_id: str, stop: threading.Event) -> None:
    while not stop.is_set():
        result = refresh_opencode_agent_session(project_root, agent_id)
        if result.get("ok"):
            return
        stop.wait(1)


@tool_app.command("read")
def tool_read(path: str, agent: Optional[str] = None, project_root: Optional[Path] = None) -> None:
    try:
        agent_id, client = tool_context(agent, project_root)
        typer.echo(format_tool_response(read_with_notice(client, agent_id, path)))
    except MesiError as exc:
        fail(exc)


@tool_app.command("write")
def tool_write(path: str, content: str, kind: str = "write", agent: Optional[str] = None, project_root: Optional[Path] = None) -> None:
    try:
        agent_id, client = tool_context(agent, project_root)
        echo_stale_notice(client, agent_id)
        result = client.post(f"/agent/{agent_id}/write", {"path": path, "content": content, "kind": kind})
        typer.echo(format_tool_response(result))
    except MesiError as exc:
        fail(exc)


@tool_app.command("edit")
def tool_edit(
    path: str,
    old: str,
    new: str,
    replace_all: bool = False,
    agent: Optional[str] = None,
    project_root: Optional[Path] = None,
) -> None:
    try:
        agent_id, client = tool_context(agent, project_root)
        read_result = read_with_notice(client, agent_id, path)
        if not read_result.get("ok"):
            raise MesiError(f"Cannot edit missing path: {path}", code="not_found", status_code=404)
        content = read_result["content"]
        count = content.count(old)
        if count == 0:
            raise MesiError("Old text was not found.", code="edit_no_match", status_code=409)
        if count > 1 and not replace_all:
            raise MesiError("Old text matched multiple times; set replace_all=true.", code="edit_multiple_matches", status_code=409)
        updated = content.replace(old, new) if replace_all else content.replace(old, new, 1)
        result = client.post(f"/agent/{agent_id}/write", {"path": path, "content": updated, "kind": "edit"})
        typer.echo(format_tool_response(result))
    except MesiError as exc:
        fail(exc)


@tool_app.command("apply-patch")
def tool_apply_patch(patch_text: str, agent: Optional[str] = None, project_root: Optional[Path] = None) -> None:
    try:
        root, agent_id, client = tool_workspace_context(agent, project_root)
        echo_stale_notice(client, agent_id)
        begin = client.post(f"/agent/{agent_id}/bash_begin", {"command": "apply_patch"})
        output = apply_patch_text(root / ".mesi" / "ws" / agent_id, patch_text)
        end = client.post(
            f"/agent/{agent_id}/bash_end",
            {"snapshot_id": begin["snapshot_id"], "exit_code": 0},
        )
        typer.echo(output)
        typer.echo(format_tool_response(end))
        if not end.get("ok"):
            raise typer.Exit(1)
    except MesiError as exc:
        fail(exc)


@tool_app.command("status")
def tool_status(agent: Optional[str] = None, project_root: Optional[Path] = None) -> None:
    try:
        agent_id, client = tool_context(agent, project_root)
        result = client.get(f"/agent/{agent_id}/stale")
        typer.echo(format_status(result))
    except MesiError as exc:
        fail(exc)


@tool_app.command("refresh")
def tool_refresh(
    paths: list[str] = typer.Argument(None),
    agent: Optional[str] = None,
    project_root: Optional[Path] = None,
) -> None:
    try:
        agent_id, client = tool_context(agent, project_root)
        echo_stale_notice(client, agent_id)
        result = client.post(f"/agent/{agent_id}/refresh", {"paths": paths or []})
        typer.echo(format_tool_response(result))
    except MesiError as exc:
        fail(exc)


@tool_app.command("bash-begin")
def tool_bash_begin(command: str, agent: Optional[str] = None, project_root: Optional[Path] = None) -> None:
    try:
        agent_id, client = tool_context(agent, project_root)
        echo_stale_notice(client, agent_id)
        echo_json(client.post(f"/agent/{agent_id}/bash_begin", {"command": command}))
    except MesiError as exc:
        fail(exc)


@tool_app.command("bash-end")
def tool_bash_end(snapshot_id: str, exit_code: int = 0, agent: Optional[str] = None, project_root: Optional[Path] = None) -> None:
    try:
        agent_id, client = tool_context(agent, project_root)
        echo_stale_notice(client, agent_id)
        echo_json(client.post(f"/agent/{agent_id}/bash_end", {"snapshot_id": snapshot_id, "exit_code": exit_code}))
    except MesiError as exc:
        fail(exc)


@tool_app.command("bash")
def tool_bash(command: str, agent: Optional[str] = None, project_root: Optional[Path] = None) -> None:
    try:
        root, agent_id, client = tool_workspace_context(agent, project_root)
        echo_stale_notice(client, agent_id)
        begin = client.post(f"/agent/{agent_id}/bash_begin", {"command": command})
        proc = subprocess.run(command, cwd=root / ".mesi" / "ws" / agent_id, shell=True, text=True, capture_output=True)
        end = client.post(
            f"/agent/{agent_id}/bash_end",
            {"snapshot_id": begin["snapshot_id"], "exit_code": proc.returncode},
        )
        typer.echo(proc.stdout, nl=False)
        typer.echo(proc.stderr, nl=False, err=True)
        if not end.get("ok"):
            typer.echo(format_tool_response(end), err=True)
            raise typer.Exit(1)
        raise typer.Exit(proc.returncode)
    except MesiError as exc:
        fail(exc)


@demo_app.command("basic-stale")
def demo_basic_stale(project_root: Path = Path(".")) -> None:
    runtime = _fresh_demo(project_root)
    runtime.read("A", "README.md")
    runtime.read("B", "README.md")
    runtime.write("B", "README.md", "hello from B\n")
    try:
        runtime.write("A", "notes.md", "blocked\n")
    except MesiError:
        pass
    runtime.refresh("A")
    runtime.write("A", "notes.md", "ok\n")
    for event in runtime.events():
        typer.echo(format_event(event))


@demo_app.command("refresh-then-write")
def demo_refresh_then_write(project_root: Path = Path(".")) -> None:
    demo_basic_stale(project_root)


@demo_app.command("bash-observed-write")
def demo_bash_observed_write(project_root: Path = Path(".")) -> None:
    runtime = _fresh_demo(project_root)
    runtime.read("A", "README.md")
    runtime.read("B", "README.md")
    begin = runtime.bash_begin("B", "echo test >> README.md")
    (runtime.workspace("B") / "README.md").write_text("hello\ntest\n", encoding="utf-8")
    runtime.bash_end("B", begin["snapshot_id"], 0)
    for event in runtime.events():
        typer.echo(format_event(event))


@demo_app.command("workspace-base-mismatch")
def demo_workspace_base_mismatch(project_root: Path = Path(".")) -> None:
    runtime = _fresh_demo(project_root)
    runtime.write("B", "created-by-b.txt", "from B\n")
    try:
        runtime.write("A", "created-by-b.txt", "competing create\n")
    except MesiError:
        pass
    for event in runtime.events():
        typer.echo(format_event(event))


def _fresh_demo(project_root: Path) -> Runtime:
    root = project_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    readme = root / "README.md"
    if not readme.exists():
        readme.write_text("hello\n", encoding="utf-8")
    runtime = Runtime(root)
    runtime.init_project(root)
    runtime.create_agent("A")
    runtime.create_agent("B")
    return runtime


def format_status(result: dict) -> str:
    stale = result.get("stale", [])
    if not stale:
        return "MESI status: no unresolved stale notices."
    return format_stale_notice(stale)


def format_stale_notice(stale: list[dict[str, str]]) -> str:
    lines = ["MESI NOTICE: unresolved stale paths visible to this agent:"]
    for row in stale:
        lines.append(f"- {row['path']} {format_version_pair(row['old_version'], row['new_version'])}")
    lines.append("Run mesi_refresh [path] or read the stale path to resolve it before writing.")
    return "\n".join(lines)


EVENT_COLORS = {
    "read": "cyan",
    "read_not_found": "cyan",
    "write": "green",
    "observed_write": "green",
    "stale_resolved": "green",
    "refresh": "green",
    "stale": "yellow",
    "opencode_notify": "cyan",
    "bash_begin": "cyan",
    "bash_end": "cyan",
    "write_blocked": "red",
    "observed_write_blocked": "red",
    "dirty_conflict": "red",
    "opencode_notify_failed": "red",
}

LOUD_EVENTS = {
    "stale",
    "write_blocked",
    "observed_write_blocked",
    "dirty_conflict",
    "opencode_notify_failed",
}


def format_event(event: dict, *, color: bool = False) -> str:
    seq = f"[{event['seq']:03d}]"
    agent = f"Agent {event['agent']}" if event.get("agent") else "System"
    event_type = event["type"].upper()
    path = event.get("path") or ""
    versions = format_event_versions(event.get("old_version"), event.get("new_version"))
    reason = event.get("reason") or ""
    if not color:
        path_part = f" {path}" if path else ""
        reason_part = f" reason={reason}" if reason else ""
        return f"{seq} {agent} {event_type}{path_part}{versions}{reason_part}"

    agent_part = _style_event_part(agent, "cyan", bold=True) if event.get("agent") else agent
    action_color = EVENT_COLORS.get(event["type"])
    action_part = _style_event_part(
        event_type,
        action_color,
        bold=event["type"] in LOUD_EVENTS,
    )
    path_part = f" {_style_event_part(path, 'blue')}" if path else ""
    reason_part = f" reason={_style_event_part(reason, _reason_color(reason), bold=event['type'] in LOUD_EVENTS)}" if reason else ""
    return f"{seq} {agent_part} {action_part}{path_part}{versions}{reason_part}"


def format_event_versions(old_version: object, new_version: object) -> str:
    old_short = short_version(old_version)
    new_short = short_version(new_version)
    if old_short and new_short:
        return f" {old_short} -> {new_short}"
    if new_short:
        return f" -> {new_short}"
    if old_short:
        return f" {old_short} ->"
    return ""


def _style_event_part(text: str, color: str | None, *, bold: bool = False) -> str:
    if not color:
        return text
    return typer.style(text, fg=color, bold=bold)


def _reason_color(reason: str) -> str:
    if reason in {"ok", "head_advanced"}:
        return "green"
    if "failed" in reason or "blocked" in reason or "unreachable" in reason:
        return "red"
    return "yellow"


if __name__ == "__main__":
    app()
