"""Local workspace GPT Action tools.

These tools operate on the folder configured by WORKSPACE_ROOT.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field


class WorkspacePathsRequest(BaseModel):
    paths: list[str] = Field(default_factory=list)


class WorkspaceReadRequest(WorkspacePathsRequest):
    start_line: int = 1
    max_lines: int = 200


class WorkspaceWriteRequest(BaseModel):
    path: str
    content: str


class WorkspaceSearchRequest(BaseModel):
    query: str
    paths: list[str] = Field(default_factory=list)
    max_matches: int = 50


def _root() -> Path:
    value = os.environ.get("WORKSPACE_ROOT", ".")
    return Path(value).expanduser().resolve()


def _target(path: str) -> Path:
    return _root() / path


def register_workspace_actions(app: FastAPI) -> None:
    @app.post("/v1/workspace/read-files", operation_id="workspaceReadFiles")
    def read_files(request: WorkspaceReadRequest) -> dict[str, Any]:
        files = []
        for item in request.paths:
            path = _target(item)
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
                start = max(request.start_line - 1, 0)
                selected = lines[start : start + request.max_lines]
                files.append({"path": item, "content": "\n".join(selected)})
            except Exception as exc:
                files.append({"path": item, "error": str(exc)})
        return {"files": files}

    @app.post("/v1/workspace/write-file", operation_id="workspaceWriteFile")
    def write_file(request: WorkspaceWriteRequest) -> dict[str, Any]:
        path = _target(request.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(request.content, encoding="utf-8")
        return {"path": request.path, "bytes": len(request.content.encode('utf-8'))}

    @app.post("/v1/workspace/search", operation_id="workspaceSearch")
    def search(request: WorkspaceSearchRequest) -> dict[str, Any]:
        result = []
        for base in request.paths or ["."]:
            for path in (_target(base)).rglob("*"):
                if not path.is_file():
                    continue
                try:
                    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                        if request.query in line:
                            result.append({"path": str(path.relative_to(_root())), "line": index, "text": line})
                            if len(result) >= request.max_matches:
                                return {"matches": result}
                except UnicodeDecodeError:
                    continue
        return {"matches": result}

    @app.post("/v1/workspace/inspect", operation_id="workspaceInspect")
    def inspect(request: WorkspacePathsRequest) -> dict[str, Any]:
        paths = request.paths or ["."]
        return {"paths": [str(_target(p)) for p in paths], "root": str(_root())}

    @app.post("/v1/workspace/command", operation_id="workspaceCommand")
    def command(action: dict[str, Any]) -> dict[str, Any]:
        if action.get("action") != "start":
            return {"status": "unsupported", "message": "Initial implementation supports start only."}
        operation_id = str(uuid.uuid4())
        process = subprocess.run(
            ["pwsh", "-NoProfile", "-Command", action.get("script", "")],
            cwd=_root(),
            capture_output=True,
            text=True,
            timeout=action.get("timeout_seconds", 60),
        )
        return {"operation_id": operation_id, "returncode": process.returncode, "stdout": process.stdout, "stderr": process.stderr}

    @app.post("/v1/workspace/apply-patch", operation_id="workspaceApplyPatch")
    def apply_patch(request: dict[str, Any]) -> dict[str, Any]:
        return {"status": "not_implemented", "message": "Patch engine will be added in next phase."}
