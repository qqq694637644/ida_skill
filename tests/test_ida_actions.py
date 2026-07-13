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


class LargeResponseIdaServer(FakeIdaServer):
    def list_instances(self) -> dict[str, dict[str, object]]:
        return {
            f"{index:04d}_sample_{index}.exe": {
                "pid": 1000 + index,
                "host": "127.0.0.1",
                "port": 13338 + index,
                "database": f"sample_{index}.exe",
                "database_path": "C:/samples/" + "D" * 120 + f"/sample_{index}.exe.i64",
                "platform": "win32",
                "started_at": "2026-07-10 00:00:00",
            }
            for index in range(1500)
        }

    def make_ida_request(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        data: dict[str, object] | None = None,
        port: int | None = None,
        timeout: float = 60.0,
    ) -> dict[str, object]:
        data = data or {}
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
            return {
                "database": "sample.exe",
                "metadata": "M" * 150_000,
            }
        if endpoint == "/functions":
            count = int(data.get("limit", 5000))
            offset = int(data.get("offset", 0))
            return {
                "offset": offset,
                "limit": count,
                "total": 5000,
                "returned": count,
                "next_offset": None,
                "truncated": False,
                "functions": [
                    {
                        "name": f"function_{index:05d}_" + "N" * 80,
                        "ea": 0x401000 + index * 16,
                        "segment": ".text",
                    }
                    for index in range(count)
                ],
            }
        if endpoint == "/xrefs":
            count = int(data.get("limit", 5000))
            return {
                "returned": count,
                "truncated": count >= 5000,
                "xrefs": [
                    {
                        "index": index,
                        "from_name": f"caller_{index:05d}",
                        "to_name": "target",
                        "source_disassembly": "mov eax, " + "D" * 120,
                    }
                    for index in range(count)
                ],
            }
        if endpoint == "/decompile":
            return {
                "name": str(data.get("name") or "large"),
                "pseudocode": "P" * 120_000,
                "disassembly": [
                    {"ea": 0x401000 + index, "text": "D" * 200}
                    for index in range(2000)
                ],
                "disassembly_truncated": False,
            }
        if endpoint == "/execute":
            return {
                "status": "ok",
                "stdout": "O" * 120_000,
                "stderr": "E" * 120_000,
                "result": {"items": ["R" * 200 for _ in range(1000)]},
                "error": None,
            }
        return super().make_ida_request(
            endpoint,
            method=method,
            data=data,
            port=port,
            timeout=timeout,
        )


class OversizedLastXrefServer(FakeIdaServer):
    def make_ida_request(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        data: dict[str, object] | None = None,
        port: int | None = None,
        timeout: float = 60.0,
    ) -> dict[str, object]:
        if endpoint != "/xrefs":
            return super().make_ida_request(
                endpoint,
                method=method,
                data=data,
                port=port,
                timeout=timeout,
            )

        data = data or {}
        count = int(data.get("limit", 5000))
        xrefs = [
            {"index": index, "source_disassembly": "nop"}
            for index in range(count)
        ]
        if count == ida_actions.XREF_ADAPTER_MAX_ITEMS:
            xrefs[-1] = {
                "index": count - 1,
                "source_disassembly": "X" * 120_000,
            }
        return {
            "returned": count,
            "truncated": False,
            "xrefs": xrefs,
        }


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
    assert "external/ida-script-gptaction-version" in response.json()["hint"]


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


def test_read_actions_use_gpt_action_timeout(monkeypatch) -> None:
    fake_server = FakeIdaServer()
    monkeypatch.setattr(ida_actions, "_load_ida_server_module", lambda: fake_server)
    client = TestClient(create_app())

    requests = [
        ("/v1/ida/functions", {"limit": 10}),
        ("/v1/ida/decompile", {"name": "main"}),
        ("/v1/ida/xrefs", {"name": "main", "limit": 10}),
    ]
    for path, body in requests:
        response = client.post(path, json=body)
        assert response.status_code == 200

    timeout_by_endpoint = {
        request["endpoint"]: request["timeout"] for request in fake_server.requests
    }
    assert timeout_by_endpoint["/functions"] == ida_actions.GPT_ACTION_READ_TIMEOUT_SECONDS
    assert timeout_by_endpoint["/decompile"] == ida_actions.GPT_ACTION_READ_TIMEOUT_SECONDS
    assert timeout_by_endpoint["/xrefs"] == ida_actions.GPT_ACTION_READ_TIMEOUT_SECONDS


def test_execute_target_resolution_error_is_response_bounded(monkeypatch) -> None:
    fake_server = FakeIdaServer()
    fake_server.resolve_target = lambda _request: (None, None, "X" * 120_000)
    monkeypatch.setattr(ida_actions, "_load_ida_server_module", lambda: fake_server)
    client = TestClient(create_app())

    response = client.post("/v1/ida/execute", json={"code": "result = 1"})

    assert response.status_code == 200
    assert len(response.text) <= ida_actions.IDA_ACTION_RESPONSE_MAX_CHARS
    assert response.json()["response_truncated"] is True


def test_ida_request_text_fields_have_length_limits(monkeypatch) -> None:
    monkeypatch.setattr(ida_actions, "_load_ida_server_module", lambda: FakeIdaServer())
    client = TestClient(create_app())

    requests = [
        ("/v1/ida/database-info", {"instance_id": "I" * 513}),
        ("/v1/ida/functions", {"name_contains": "N" * 513}),
        ("/v1/ida/decompile", {"name": "N" * 513}),
        ("/v1/ida/xrefs", {"address": "A" * 129}),
        ("/v1/ida/execute", {"script_path": "P" * 4097}),
    ]
    for path, body in requests:
        with_body = client.post(path, json=body)
        assert with_body.status_code == 422, path


def test_large_ida_action_responses_are_bounded(monkeypatch) -> None:
    fake_server = LargeResponseIdaServer()
    monkeypatch.setattr(ida_actions, "_load_ida_server_module", lambda: fake_server)
    client = TestClient(create_app())

    instances = client.post("/v1/ida/instances", json={"limit": 1500})
    assert instances.status_code == 200
    assert len(instances.text) <= ida_actions.IDA_ACTION_RESPONSE_MAX_CHARS
    instances_body = instances.json()
    assert instances_body["response_truncated"] is True
    assert instances_body["truncated"] is True
    assert instances_body["next_offset"] == instances_body["returned"]
    next_instances = client.post(
        "/v1/ida/instances",
        json={"offset": instances_body["next_offset"], "limit": 1500},
    )
    assert next_instances.status_code == 200
    next_instances_body = next_instances.json()
    first_ids = {item["instance_id"] for item in instances_body["instances"]}
    second_ids = {item["instance_id"] for item in next_instances_body["instances"]}
    assert first_ids.isdisjoint(second_ids)
    assert next_instances_body["offset"] == instances_body["next_offset"]

    metadata = client.post("/v1/ida/database-info", json={})
    assert metadata.status_code == 200
    assert len(metadata.text) <= ida_actions.IDA_ACTION_RESPONSE_MAX_CHARS
    metadata_body = metadata.json()
    assert metadata_body["response_truncated"] is True
    assert metadata_body["response_char_limit"] == ida_actions.IDA_ACTION_RESPONSE_MAX_CHARS

    functions = client.post("/v1/ida/functions", json={"limit": 5000})
    assert functions.status_code == 200
    assert len(functions.text) <= ida_actions.IDA_ACTION_RESPONSE_MAX_CHARS
    functions_body = functions.json()
    assert functions_body["truncated"] is True
    assert functions_body["response_truncated"] is True
    assert functions_body["next_offset"] == functions_body["returned"]

    xrefs = client.post(
        "/v1/ida/xrefs",
        json={"name": "target", "offset": 0, "limit": 5000},
    )
    assert xrefs.status_code == 200
    assert len(xrefs.text) <= ida_actions.IDA_ACTION_RESPONSE_MAX_CHARS
    xrefs_body = xrefs.json()
    assert xrefs_body["truncated"] is True
    assert xrefs_body["response_truncated"] is True
    assert xrefs_body["next_offset"] == xrefs_body["returned"]

    decompile = client.post(
        "/v1/ida/decompile",
        json={"name": "large", "include_disassembly": True},
    )
    assert decompile.status_code == 200
    assert len(decompile.text) <= ida_actions.IDA_ACTION_RESPONSE_MAX_CHARS
    decompile_body = decompile.json()
    assert decompile_body["pseudocode_truncated"] is True
    assert decompile_body["disassembly_truncated"] is True

    execute = client.post("/v1/ida/execute", json={"code": "result = large()"})
    assert execute.status_code == 200
    assert len(execute.text) <= ida_actions.IDA_ACTION_RESPONSE_MAX_CHARS
    execute_body = execute.json()
    assert execute_body["response_truncated"] is True
    assert execute_body["stdout_truncated"] is True
    assert execute_body["stderr_truncated"] is True
    assert execute_body["result_truncated"] is True


def test_hard_response_fallback_stays_within_the_limit() -> None:
    payload = {"status": "ok", "metadata": "M" * 300_000}

    bounded = ida_actions._hard_cap_response(payload)

    assert ida_actions._json_char_count(bounded) <= ida_actions.IDA_ACTION_RESPONSE_MAX_CHARS
    assert bounded["response_truncated"] is True
    assert bounded["response_preview_original_chars"] > ida_actions.IDA_ACTION_RESPONSE_MAX_CHARS


def test_list_budget_never_returns_a_non_advancing_offset() -> None:
    bounded = ida_actions._budget_list_response(
        {
            "functions": [],
            "truncated": True,
            "next_offset": 100,
        },
        list_key="functions",
        offset=100,
        source_has_more=True,
    )

    assert bounded["returned"] == 0
    assert bounded["next_offset"] is None
    assert bounded["more_available"] is True
    assert "no forward progress" in bounded["truncation_hint"]


def test_xrefs_adapter_supports_bounded_offset_pagination(monkeypatch) -> None:
    fake_server = LargeResponseIdaServer()
    monkeypatch.setattr(ida_actions, "_load_ida_server_module", lambda: fake_server)
    client = TestClient(create_app())

    page = client.post(
        "/v1/ida/xrefs",
        json={"name": "target", "offset": 100, "limit": 25},
    )

    assert page.status_code == 200
    body = page.json()
    assert body["offset"] == 100
    assert body["returned"] == 25
    assert body["xrefs"][0]["index"] == 100
    assert fake_server.requests[-1]["data"]["limit"] == 125

    invalid = client.post(
        "/v1/ida/xrefs",
        json={"name": "target", "offset": 4999, "limit": 2},
    )
    assert invalid.status_code == 422


def test_oversized_last_xref_does_not_repeat_the_same_offset(monkeypatch) -> None:
    fake_server = OversizedLastXrefServer()
    monkeypatch.setattr(ida_actions, "_load_ida_server_module", lambda: fake_server)
    client = TestClient(create_app())

    response = client.post(
        "/v1/ida/xrefs",
        json={"name": "target", "offset": 4999, "limit": 1},
    )

    assert response.status_code == 200
    assert len(response.text) <= ida_actions.IDA_ACTION_RESPONSE_MAX_CHARS
    body = response.json()
    assert body["oversized_item_omitted"] is True
    assert body["next_offset"] is None
    assert body["next_offset"] != 4999
    assert body["more_available"] is False


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

    assert "external/ida-script-gptaction-version" in gitmodules
    assert "https://github.com/qqq694637644/ida-script-gptaction-version" in gitmodules
    assert "branch =" not in gitmodules
