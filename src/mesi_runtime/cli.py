from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Annotated, Optional

import typer

from .client import Client, format_tool_response
from .constants import DEFAULT_HOST, DEFAULT_PORT
from .errors import MesiError
from .patcher import apply_patch_text
from .paths import infer_agent_id, resolve_project_root
from .runtime import Runtime
from .server import serve

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
    typer.echo(json.dumps(payload, sort_keys=True, indent=2))


def fail(exc: MesiError) -> None:
    typer.echo(json.dumps({"ok": False, "error": exc.code, "message": str(exc)}, sort_keys=True), err=True)
    raise typer.Exit(1)


@app.command("init")
def init(project_root: Annotated[Path, typer.Argument()] = Path(".")) -> None:
    runtime = Runtime(project_root)
    echo_json(runtime.init_project(project_root))


@agent_app.command("create")
def agent_create(agent_id: str, project_root: Path = Path(".")) -> None:
    echo_json(Runtime(project_root).create_agent(agent_id))


@daemon_app.command("start")
def daemon_start(
    project_root: Path = Path("."),
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> None:
    typer.echo(f"MESI daemon listening on http://{host}:{port}")
    serve(project_root, host, port)


@app.command("monitor")
def monitor(project_root: Path = Path("."), once: bool = False, interval: float = 0.5) -> None:
    runtime = Runtime(project_root)
    last = 0
    while True:
        events = runtime.events(last)
        for event in events:
            last = event["seq"]
            typer.echo(format_event(event))
        if once:
            break
        time.sleep(interval)


@tool_app.command("read")
def tool_read(path: str, agent: Optional[str] = None, project_root: Optional[Path] = None) -> None:
    try:
        agent_id = agent or infer_agent_id()
        result = Client(project_root).post(f"/agent/{agent_id}/read", {"path": path})
        typer.echo(format_tool_response(result))
    except MesiError as exc:
        fail(exc)


@tool_app.command("write")
def tool_write(path: str, content: str, kind: str = "write", agent: Optional[str] = None, project_root: Optional[Path] = None) -> None:
    try:
        agent_id = agent or infer_agent_id()
        result = Client(project_root).post(f"/agent/{agent_id}/write", {"path": path, "content": content, "kind": kind})
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
        agent_id = agent or infer_agent_id()
        client = Client(project_root)
        read_result = client.post(f"/agent/{agent_id}/read", {"path": path})
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
        root = resolve_project_root(project_root)
        agent_id = agent or infer_agent_id()
        client = Client(root)
        begin = client.post(f"/agent/{agent_id}/bash_begin", {"command": "apply_patch"})
        output = apply_patch_text(root / ".mesi" / "ws" / agent_id, patch_text)
        end = client.post(
            f"/agent/{agent_id}/bash_end",
            {"snapshot_id": begin["snapshot_id"], "exit_code": 0},
        )
        typer.echo(output)
        typer.echo(json.dumps(end, sort_keys=True, indent=2))
    except MesiError as exc:
        fail(exc)


@tool_app.command("status")
def tool_status(agent: Optional[str] = None, project_root: Optional[Path] = None) -> None:
    try:
        agent_id = agent or infer_agent_id()
        result = Client(project_root).get(f"/agent/{agent_id}/stale")
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
        agent_id = agent or infer_agent_id()
        result = Client(project_root).post(f"/agent/{agent_id}/refresh", {"paths": paths or []})
        typer.echo(format_tool_response(result))
    except MesiError as exc:
        fail(exc)


@tool_app.command("bash-begin")
def tool_bash_begin(command: str, agent: Optional[str] = None, project_root: Optional[Path] = None) -> None:
    try:
        agent_id = agent or infer_agent_id()
        echo_json(Client(project_root).post(f"/agent/{agent_id}/bash_begin", {"command": command}))
    except MesiError as exc:
        fail(exc)


@tool_app.command("bash-end")
def tool_bash_end(snapshot_id: str, exit_code: int = 0, agent: Optional[str] = None, project_root: Optional[Path] = None) -> None:
    try:
        agent_id = agent or infer_agent_id()
        echo_json(Client(project_root).post(f"/agent/{agent_id}/bash_end", {"snapshot_id": snapshot_id, "exit_code": exit_code}))
    except MesiError as exc:
        fail(exc)


@tool_app.command("bash")
def tool_bash(command: str, agent: Optional[str] = None, project_root: Optional[Path] = None) -> None:
    try:
        root = resolve_project_root(project_root)
        agent_id = agent or infer_agent_id()
        client = Client(root)
        begin = client.post(f"/agent/{agent_id}/bash_begin", {"command": command})
        proc = subprocess.run(command, cwd=root / ".mesi" / "ws" / agent_id, shell=True, text=True, capture_output=True)
        end = client.post(
            f"/agent/{agent_id}/bash_end",
            {"snapshot_id": begin["snapshot_id"], "exit_code": proc.returncode},
        )
        typer.echo(proc.stdout, nl=False)
        typer.echo(proc.stderr, nl=False, err=True)
        if not end.get("ok"):
            typer.echo(json.dumps(end, sort_keys=True, indent=2), err=True)
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
    lines = ["MESI status: unresolved stale notices:"]
    for row in stale:
        lines.append(f"- {row['path']} {row['old_version']} -> {row['new_version']}")
    lines.append("Run mesi_refresh for the affected paths, or no-arg mesi_refresh to refresh all stale paths.")
    return "\n".join(lines)


def format_event(event: dict) -> str:
    seq = f"[{event['seq']:03d}]"
    agent = f"Agent {event['agent']} " if event.get("agent") else "System "
    event_type = event["type"].upper()
    path = f" {event['path']}" if event.get("path") else ""
    versions = ""
    if event.get("old_version") or event.get("new_version"):
        versions = f" {event.get('old_version') or ''} -> {event.get('new_version') or ''}".rstrip()
    reason = f" reason={event['reason']}" if event.get("reason") else ""
    return f"{seq} {agent}{event_type}{path}{versions}{reason}"


if __name__ == "__main__":
    app()
