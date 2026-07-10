"""Tests for IDA Script MCP GPT Action endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from skill_temple import ida_actions
from skill_temple.app import create_app


class FakeIdaPluginResponseTimeoutError(RuntimeError):
    pass


class FakeIdaServer:
    IdaPluginResponseTimeout = FakeIdaPluginResponseTimeoutError

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def list_instances(self) -> dict[str, dict[str, object]]:
        return {
            "1234_sample.exe": {
                "pid": 1234,
                "host": "127.0.0.1",
                "port": 13338,
                "database": "sample.exe",
                "database_path": "C:/samples/sample.exe.i64",
                "platform": "win32",
                "started_at": "2026-07-10 00:00:00",
            }
        }

    def _sorted_instance_records(
        self,
        instances: dict[str, dict[str, object]],
    ) -> list[dict[str, object]]:
        return [
            {
                "instance_id": instance_id,
                "pid": info.get("pid"),
                "host": info.get("host"),
                "port": info.get("port"),
                "database": info.get("database"),
                "database_path": info.get("database_path"),
                "platform": info.get("platform"),
                "started_at": info.get("started_at"),
            }
            for instance_id, info in sorted(instances.items())
        ]

    def resolve_target(self, request: object) -> tuple[int | None, str | None, str]:
        selected_port = getattr(request, "port", None)
        if selected_port is not None:
            return selected_port, "port-selected.exe", "port-selected.exe"

        selected_instance = getattr(request, "instance_id", None)
        if selected_instance in {None, "sample", "1234_sample.exe"}:
            return 13338, "1234_sample.exe", "1234_sample.exe"

        return None, None, f"Instance {selected_instance!r} not found."

    def make_ida_request(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        data: dict[str, object] | None = None,
        port: int | None = None,
        timeout: float = 60.0,
    ) -> dict[str, object]:
        self.requests.append(
            {
                "endpoint": endpoint,
                "method": method,
                "data": data,
                "port": port,
                "timeout": timeout,
            }
        )
        if endpoint == "/metadata":
            return {"database": "sample.exe", "function_count": 10}
        if endpoint == "/functions":
            return {"returned": 1, "functions": [{"name": "main", "ea": "0x401000"}]}
        if endpoint == "/decompile":
            return {"name": data["name"], "pseudocode": "int main() { return 0; }"}
        if endpoint == "/xrefs":
            return {"returned": 1, "xrefs": [{"from_name": "caller", "to_name": data["name"]}]}
        if endpoint == "/execute":
            return {"status": "ok", "result": 3, "stdout": "", "stderr": ""}
        raise AssertionError(f"unexpected endpoint {endpoint}")


def test_ida_action_endpoints_call_ida_server(monkeypatch) -> None:
    fake_server = FakeIdaServer()
    monkeypatch.setattr(ida_actions, "_load_ida_server_module", lambda: fake_server)
    client = TestClient(create_app())

    instances = client.post("/v1/ida/instances", json={}).json()
    assert instances["count"] == 1
    assert instances["instances"][0]["instance_id"] == "1234_sample.exe"

    metadata = client.post("/v1/ida/database-info", json={"instance_id": "sample"}).json()
    assert metadata["database"] == "sample.exe"
    assert metadata["instance_id"] == "1234_sample.exe"
    assert metadata["port"] == 13338

    functions = client.post(
        "/v1/ida/functions",
        json={"name_contains": "main", "limit": 5, "include_thunks": True},
    ).json()
    assert functions["functions"][0]["name"] == "main"

    decompile = client.post("/v1/ida/decompile", json={"name": "main"}).json()
    assert "return 0" in decompile["pseudocode"]

    xrefs = client.post("/v1/ida/xrefs", json={"name": "main", "direction": "to"}).json()
    assert xrefs["xrefs"][0]["to_name"] == "main"

    execute = client.post("/v1/ida/execute", json={"code": "result = 1 + 2"}).json()
    assert execute["status"] == "ok"
    assert execute["result"] == 3

    endpoints = [request["endpoint"] for request in fake_server.requests]
    assert endpoints == ["/metadata", "/functions", "/decompile", "/xrefs", "/execute"]


def test_ida_action_endpoints_report_missing_dependency(monkeypatch) -> None:
    monkeypatch.setattr(ida_actions, "_load_ida_server_module", lambda: None)
    client = TestClient(create_app())

    response = client.post("/v1/ida/instances", json={})

    assert response.status_code == 200
    assert response.json()["error"] == "ida_script_mcp is not installed"
    assert "external/ida-script-mcp-main" in response.json()["hint"]


def test_execute_idapython_reports_plugin_response_timeout(monkeypatch) -> None:
    fake_server = FakeIdaServer()

    def raise_timeout(*_args, **_kwargs):
        raise FakeIdaPluginResponseTimeoutError("timeout")

    fake_server.make_ida_request = raise_timeout
    monkeypatch.setattr(ida_actions, "_load_ida_server_module", lambda: fake_server)
    client = TestClient(create_app())

    response = client.post("/v1/ida/execute", json={"code": "while True:\n    pass"})

    body = response.json()
    assert body["status"] == "plugin_response_timeout"
    assert body["error"]["type"] == "PluginResponseTimeout"
    assert body["timeout_seconds"] == 30
    assert body["port"] == 13338


def test_execute_idapython_timeout_is_bounded_for_gpt_actions(monkeypatch) -> None:
    fake_server = FakeIdaServer()
    monkeypatch.setattr(ida_actions, "_load_ida_server_module", lambda: fake_server)
    client = TestClient(create_app())

    execute_schema = client.get("/openapi.json").json()["components"]["schemas"][
        "ExecuteIdapythonRequest"
    ]
    assert execute_schema["properties"]["timeout_seconds"]["maximum"] == 35

    too_long = client.post(
        "/v1/ida/execute",
        json={"code": "result = 1", "timeout_seconds": 36},
    )
    assert too_long.status_code == 422

    accepted = client.post(
        "/v1/ida/execute",
        json={"code": "result = 1", "timeout_seconds": 35},
    )
    assert accepted.status_code == 200
    assert fake_server.requests[-1]["timeout"] == 40.0


def test_decompile_and_xrefs_require_one_target(monkeypatch) -> None:
    monkeypatch.setattr(ida_actions, "_load_ida_server_module", lambda: FakeIdaServer())
    client = TestClient(create_app())

    for path in ["/v1/ida/decompile", "/v1/ida/xrefs"]:
        missing = client.post(path, json={})
        both = client.post(path, json={"address": "0x401000", "name": "main"})
        assert missing.status_code == 422
        assert both.status_code == 422


def test_ida_script_mcp_submodule_is_recorded() -> None:
    gitmodules = Path(".gitmodules").read_text(encoding="utf-8")

    assert "external/ida-script-mcp-main" in gitmodules
    assert "https://github.com/qqq694637644/ida-script-mcp-main" in gitmodules
    assert "branch =" not in gitmodules
