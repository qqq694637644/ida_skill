"""IDA Script MCP GPT Action endpoints.

This module intentionally lazy-imports ``ida_script_mcp`` so the base Skill
Temple gateway can still start before the submodule dependency is installed.
The route handlers are synchronous because the reused IDA transport uses
``http.client.HTTPConnection`` under the hood.
"""

from __future__ import annotations

import importlib
import time
from types import ModuleType
from typing import Any, Literal

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, model_validator

IDA_SETUP_HINT = "Run: py -3 -m pip install -e external/ida-script-mcp-main"
IDA_ERROR_HINT = (
    "Make sure IDA Pro is running with the IDA-Script-MCP plugin started. "
    "In IDA, use Edit -> Plugins -> IDA-Script-MCP (Ctrl+Alt+S)."
)
PLUGIN_RESPONSE_TIMEOUT_MARGIN_SECONDS = 5


class StrictIdaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IdaTargetRequest(StrictIdaRequest):
    instance_id: str | None = Field(
        default=None,
        description="Target IDA instance id or unique substring from listIdaInstances.",
    )
    port: int | None = Field(
        default=None,
        ge=1,
        le=65535,
        description="Target IDA plugin port. Overrides instance_id when provided.",
    )


class ListIdaInstancesRequest(StrictIdaRequest):
    pass


class GetIdaDatabaseInfoRequest(IdaTargetRequest):
    pass


class ListIdaFunctionsRequest(IdaTargetRequest):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=200, ge=1, le=5000)
    name_contains: str | None = None
    segment: str | None = None
    include_thunks: bool = False
    include_library_functions: bool = False


class DecompileIdaFunctionRequest(IdaTargetRequest):
    address: str | None = None
    name: str | None = None
    include_disassembly: bool = False

    @model_validator(mode="after")
    def validate_target(self) -> DecompileIdaFunctionRequest:
        _require_exactly_one_target(self.address, self.name, "decompileIdaFunction")
        return self


class GetIdaXrefsRequest(IdaTargetRequest):
    address: str | None = None
    name: str | None = None
    direction: Literal["to", "from"] = "to"
    xref_kind: Literal["all", "code", "data"] = "all"
    limit: int = Field(default=200, ge=1, le=5000)

    @model_validator(mode="after")
    def validate_target(self) -> GetIdaXrefsRequest:
        _require_exactly_one_target(self.address, self.name, "getIdaXrefs")
        return self


class ExecuteIdapythonRequest(IdaTargetRequest):
    code: str | None = Field(default=None, description="IDAPython code string to run.")
    script_path: str | None = Field(
        default=None,
        description="Path to a Python script file readable by the IDA machine.",
    )
    capture_output: bool = True
    timeout_seconds: int = Field(default=30, ge=1, le=600)

    @model_validator(mode="after")
    def validate_source(self) -> ExecuteIdapythonRequest:
        has_code = self.code is not None and bool(self.code.strip())
        has_script_path = self.script_path is not None and bool(self.script_path.strip())
        if has_code == has_script_path:
            raise ValueError("Provide exactly one of code or script_path")
        return self


def _require_exactly_one_target(address: str | None, name: str | None, operation: str) -> None:
    has_address = address is not None and bool(address.strip())
    has_name = name is not None and bool(name.strip())
    if has_address == has_name:
        raise ValueError(f"{operation} requires exactly one of address or name")


def _load_ida_server_module() -> ModuleType | None:
    try:
        return importlib.import_module("ida_script_mcp.server")
    except ImportError:
        return None


def _setup_error() -> dict[str, Any]:
    return {
        "error": "ida_script_mcp is not installed",
        "hint": IDA_SETUP_HINT,
    }


def _tool_error(
    message: str,
    *,
    instance_id: str | None = None,
    port: int | None = None,
    hint: str = IDA_ERROR_HINT,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": message, "hint": hint}
    if instance_id is not None:
        payload["instance_id"] = instance_id
    if port is not None:
        payload["port"] = port
    return payload


def _resolve_target(
    server: ModuleType,
    request: IdaTargetRequest,
) -> tuple[int | None, str | None, str]:
    return server.resolve_target(request)


def _instance_records(server: ModuleType) -> list[dict[str, Any]]:
    instances = server.list_instances()
    sorter = getattr(server, "_sorted_instance_records", None)
    if sorter is not None:
        return list(sorter(instances))

    records = []
    for instance_id, info in instances.items():
        records.append(
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
        )
    return sorted(records, key=lambda item: (str(item.get("database") or ""), item["instance_id"]))


def _request_plugin(
    server: ModuleType,
    request: IdaTargetRequest,
    endpoint: str,
    *,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    port, resolved_instance_id, label = _resolve_target(server, request)
    if port is None:
        return _tool_error(label)

    try:
        result = server.make_ida_request(
            endpoint,
            method=method,
            data=data,
            port=port,
            timeout=timeout,
        )
        result.setdefault("instance_id", resolved_instance_id)
        result.setdefault("port", port)
        return result
    except Exception as exc:
        return _tool_error(str(exc), instance_id=resolved_instance_id, port=port)


def register_ida_actions(app: FastAPI) -> None:
    """Register IDA GPT Action routes on an existing FastAPI app."""

    @app.post(
        "/v1/ida/instances",
        operation_id="listIdaInstances",
        summary="List running IDA plugin instances.",
        description="List live IDA databases registered by the local IDA-Script-MCP plugin.",
        openapi_extra={"x-openai-isConsequential": False},
    )
    def list_ida_instances(_request: ListIdaInstancesRequest) -> dict[str, Any]:
        server = _load_ida_server_module()
        if server is None:
            return _setup_error()

        records = _instance_records(server)
        if not records:
            return {
                "count": 0,
                "instances": [],
                "hint": "No IDA instances found. Start IDA Pro and enable the plugin.",
            }
        return {"count": len(records), "instances": records}

    @app.post(
        "/v1/ida/database-info",
        operation_id="getIdaDatabaseInfo",
        summary="Get selected IDA database metadata.",
        description=(
            "Return file, architecture, image base, and function metadata for one "
            "IDA database."
        ),
        openapi_extra={"x-openai-isConsequential": False},
    )
    def get_ida_database_info(request: GetIdaDatabaseInfoRequest) -> dict[str, Any]:
        server = _load_ida_server_module()
        if server is None:
            return _setup_error()
        return _request_plugin(server, request, "/metadata", timeout=10.0)

    @app.post(
        "/v1/ida/functions",
        operation_id="listIdaFunctions",
        summary="List functions from an IDA database.",
        description=(
            "List functions with filters for name, segment, thunks, library flags, "
            "offset, and limit."
        ),
        openapi_extra={"x-openai-isConsequential": False},
    )
    def list_ida_functions(request: ListIdaFunctionsRequest) -> dict[str, Any]:
        server = _load_ida_server_module()
        if server is None:
            return _setup_error()
        payload = {
            "offset": request.offset,
            "limit": request.limit,
            "name_contains": request.name_contains,
            "segment": request.segment,
            "include_thunks": request.include_thunks,
            "include_library_functions": request.include_library_functions,
        }
        return _request_plugin(server, request, "/functions", method="POST", data=payload)

    @app.post(
        "/v1/ida/decompile",
        operation_id="decompileIdaFunction",
        summary="Decompile a selected IDA function.",
        description=(
            "Return Hex-Rays pseudocode for a function by address or name, "
            "optionally with disassembly."
        ),
        openapi_extra={"x-openai-isConsequential": False},
    )
    def decompile_ida_function(request: DecompileIdaFunctionRequest) -> dict[str, Any]:
        server = _load_ida_server_module()
        if server is None:
            return _setup_error()
        payload = {
            "address": request.address,
            "name": request.name,
            "include_disassembly": request.include_disassembly,
        }
        return _request_plugin(server, request, "/decompile", method="POST", data=payload)

    @app.post(
        "/v1/ida/xrefs",
        operation_id="getIdaXrefs",
        summary="Get IDA cross references.",
        description=(
            "Return incoming or outgoing code/data xrefs for an address or symbol "
            "in an IDA database."
        ),
        openapi_extra={"x-openai-isConsequential": False},
    )
    def get_ida_xrefs(request: GetIdaXrefsRequest) -> dict[str, Any]:
        server = _load_ida_server_module()
        if server is None:
            return _setup_error()
        payload = {
            "address": request.address,
            "name": request.name,
            "direction": request.direction,
            "xref_kind": request.xref_kind,
            "limit": request.limit,
        }
        return _request_plugin(server, request, "/xrefs", method="POST", data=payload)

    @app.post(
        "/v1/ida/execute",
        operation_id="executeIdapython",
        summary="Execute IDAPython in IDA.",
        description=(
            "Run IDAPython code or a script file through the selected local "
            "IDA-Script-MCP plugin."
        ),
        openapi_extra={"x-openai-isConsequential": False},
    )
    def execute_idapython(request: ExecuteIdapythonRequest) -> dict[str, Any]:
        server = _load_ida_server_module()
        if server is None:
            return _setup_error()

        port, resolved_instance_id, label = _resolve_target(server, request)
        if port is None:
            return _tool_error(label)

        payload = {
            "code": request.code,
            "script_path": request.script_path,
            "capture_output": request.capture_output,
            "timeout_seconds": request.timeout_seconds,
        }
        timeout = float(request.timeout_seconds) + PLUGIN_RESPONSE_TIMEOUT_MARGIN_SECONDS
        started_at = time.monotonic()
        try:
            result = server.make_ida_request(
                "/execute",
                method="POST",
                data=payload,
                port=port,
                timeout=timeout,
            )
            result.setdefault("instance_id", resolved_instance_id)
            result.setdefault("port", port)
            return result
        except Exception as exc:
            timeout_class = getattr(server, "IdaPluginResponseTimeout", None)
            timeout_type = getattr(timeout_class, "__name__", "")
            if type(exc).__name__ in {timeout_type, "IdaPluginResponseTimeout"}:
                return {
                    "status": "plugin_response_timeout",
                    "result": None,
                    "stdout": "",
                    "stderr": "",
                    "error": {
                        "type": "PluginResponseTimeout",
                        "message": (
                            f"IDA plugin did not respond within {timeout:g} seconds. "
                            "The script may still be running inside IDA."
                        ),
                        "traceback": None,
                    },
                    "duration_seconds": max(0.0, time.monotonic() - started_at),
                    "timeout_seconds": request.timeout_seconds,
                    "instance_id": resolved_instance_id,
                    "port": port,
                }
            return _tool_error(str(exc), instance_id=resolved_instance_id, port=port)
