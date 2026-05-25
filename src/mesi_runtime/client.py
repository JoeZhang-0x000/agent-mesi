from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .constants import DEFAULT_HOST, DEFAULT_PORT, daemon_path
from .display import shorten_version_fields
from .errors import MesiError
from .paths import resolve_project_root


class Client:
    def __init__(self, project_root: Path | str | None = None):
        self.project_root = resolve_project_root(project_root)
        config = self._load_config()
        self.base_url = f"http://{config['host']}:{config['port']}"

    def _load_config(self) -> dict[str, Any]:
        path = daemon_path(self.project_root)
        if not path.exists():
            return {"host": DEFAULT_HOST, "port": DEFAULT_PORT}
        return json.loads(path.read_text(encoding="utf-8"))

    def get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, payload)

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        raw = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path,
            data=raw,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {"message": body}
            raise MesiError(payload.get("message", str(exc)), code=payload.get("error", "http_error"), status_code=exc.code) from exc
        except urllib.error.URLError as exc:
            raise MesiError(f"Unable to reach MESI daemon at {self.base_url}: {exc.reason}", code="daemon_unreachable") from exc


def format_tool_response(result: dict[str, Any]) -> str:
    if "content" in result and result["content"] is not None:
        return str(result["content"])
    return json.dumps(shorten_version_fields(result), sort_keys=True, indent=2)
