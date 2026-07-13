"""IDA Script MCP GPT Action endpoints.

This module intentionally lazy-imports ``ida_script_mcp`` so the base Skill
Temple gateway can still start before the submodule dependency is installed.
The route handlers are synchronous because the reused IDA transport uses
``http.client.HTTPConnection`` under the hood.
"""

from __future__ import annotations

import importlib
import json
import time
from types import ModuleType
from typing import Any, Literal

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, model_validator

IDA_SETUP_HINT = "Run: py -3 -m pip install -e external/ida-script-gptaction-version"
IDA_ERROR_HINT = (
    "Make sure IDA Pro is running with the IDA-Script-MCP plugin started. "
    "In IDA, use Edit -> Plugins -> IDA-Script-MCP (Ctrl+Alt+S)."
)
PLUGIN_RESPONSE_TIMEOUT_MARGIN_SECONDS = 5
GPT_ACTION_EXECUTION_TIMEOUT_MAX_SECONDS = 35
GPT_ACTION_READ_TIMEOUT_SECONDS = 35
IDA_ACTION_RESPONSE_MAX_CHARS = 80_000
XREF_ADAPTER_MAX_ITEMS = 5_000
DECOMPILE_PSEUDOCODE_MAX_CHARS = 50_000
EXECUTE_STDOUT_MAX_CHARS = 16_000
EXECUTE_STDERR_MAX_CHARS = 16_000
EXECUTE_RESULT_MAX_JSON_CHARS = 28_000
EXECUTE_ERROR_MAX_JSON_CHARS = 8_000
INSTANCE_ID_MAX_CHARS = 512
SYMBOL_NAME_MAX_CHARS = 512
ADDRESS_MAX_CHARS = 128
FILTER_TEXT_MAX_CHARS = 512
SEGMENT_NAME_MAX_CHARS = 128
SCRIPT_PATH_MAX_CHARS = 4096


class StrictIdaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IdaTargetRequest(StrictIdaRequest):
    instance_id: str | None = Field(
        default=None,
        max_length=INSTANCE_ID_MAX_CHARS,
        description="Target IDA instance id or unique substring from listIdaInstances.",
    )
    port: int | None = Field(
        default=None,
        ge=1,
        le=65535,
        description="Target IDA plugin port. Overrides instance_id when provided.",
    )


class ListIdaInstancesRequest(StrictIdaRequest):
    offset: int = Field(default=0, ge=0, description="IDA instance offset for pagination.")
    limit: int = Field(
        default=200,
        ge=1,
        le=5000,
        description="Requested instance count; the adapter may return fewer with next_offset.",
    )


class GetIdaDatabaseInfoRequest(IdaTargetRequest):
    pass


class ListIdaFunctionsRequest(IdaTargetRequest):
    offset: int = Field(default=0, ge=0, description="Function offset for pagination.")
    limit: int = Field(
        default=200,
        ge=1,
        le=5000,
        description="Requested function count; the adapter may return fewer with next_offset.",
    )
    name_contains: str | None = Field(default=None, max_length=FILTER_TEXT_MAX_CHARS)
    segment: str | None = Field(default=None, max_length=SEGMENT_NAME_MAX_CHARS)
    include_thunks: bool = False
    include_library_functions: bool = False


class DecompileIdaFunctionRequest(IdaTargetRequest):
    address: str | None = Field(default=None, max_length=ADDRESS_MAX_CHARS)
    name: str | None = Field(default=None, max_length=SYMBOL_NAME_MAX_CHARS)
    include_disassembly: bool = False

    @model_validator(mode="after")
    def validate_target(self) -> DecompileIdaFunctionRequest:
        _require_exactly_one_target(self.address, self.name, "decompileIdaFunction")
        return self


class GetIdaXrefsRequest(IdaTargetRequest):
    address: str | None = Field(default=None, max_length=ADDRESS_MAX_CHARS)
    name: str | None = Field(default=None, max_length=SYMBOL_NAME_MAX_CHARS)
    direction: Literal["to", "from"] = "to"
    xref_kind: Literal["all", "code", "data"] = "all"
    offset: int = Field(
        default=0,
        ge=0,
        le=XREF_ADAPTER_MAX_ITEMS - 1,
        description="Cross-reference offset within the adapter's 5000-item window.",
    )
    limit: int = Field(
        default=200,
        ge=1,
        le=5000,
        description=(
            "Requested xref count; offset + limit must be at most 5000, and the adapter "
            "may return fewer with next_offset."
        ),
    )

    @model_validator(mode="after")
    def validate_target(self) -> GetIdaXrefsRequest:
        _require_exactly_one_target(self.address, self.name, "getIdaXrefs")
        if self.offset + self.limit > XREF_ADAPTER_MAX_ITEMS:
            raise ValueError(
                f"getIdaXrefs requires offset + limit <= {XREF_ADAPTER_MAX_ITEMS}"
            )
        return self


class ExecuteIdapythonRequest(IdaTargetRequest):
    code: str | None = Field(default=None, description="IDAPython code string to run.")
    script_path: str | None = Field(
        default=None,
        max_length=SCRIPT_PATH_MAX_CHARS,
        description="Path to a Python script file readable by the IDA machine.",
    )
    capture_output: bool = True
    timeout_seconds: int = Field(
        default=30,
        ge=1,
        le=GPT_ACTION_EXECUTION_TIMEOUT_MAX_SECONDS,
        description="IDA execution timeout in seconds. Capped for the GPT Action round trip.",
    )

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


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _json_char_count(value: Any) -> int:
    return len(_json_text(value))


def _truncate_string_field(
    payload: dict[str, Any],
    key: str,
    max_chars: int,
) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, str) or len(value) <= max_chars:
        return payload
    result = dict(payload)
    result[key] = value[:max_chars]
    result[f"{key}_truncated"] = True
    result[f"{key}_original_chars"] = len(value)
    result["response_truncated"] = True
    result["response_char_limit"] = IDA_ACTION_RESPONSE_MAX_CHARS
    return result


def _truncate_json_field(
    payload: dict[str, Any],
    key: str,
    max_json_chars: int,
) -> dict[str, Any]:
    if key not in payload:
        return payload
    value = payload[key]
    serialized = _json_text(value)
    if len(serialized) <= max_json_chars:
        return payload
    result = dict(payload)
    result[key] = serialized[:max_json_chars]
    result[f"{key}_truncated"] = True
    result[f"{key}_original_json_chars"] = len(serialized)
    result[f"{key}_original_type"] = type(value).__name__
    result[f"{key}_encoding"] = "truncated_json_preview"
    result["response_truncated"] = True
    result["response_char_limit"] = IDA_ACTION_RESPONSE_MAX_CHARS
    return result


def _hard_cap_response(payload: dict[str, Any]) -> dict[str, Any]:
    if _json_char_count(payload) <= IDA_ACTION_RESPONSE_MAX_CHARS:
        return payload

    compact: dict[str, Any] = {
        "response_truncated": True,
        "response_char_limit": IDA_ACTION_RESPONSE_MAX_CHARS,
        "truncation_reason": "Serialized IDA Action response exceeded the adapter limit.",
    }
    for key in ("status", "instance_id", "port", "found", "name", "address"):
        if key not in payload:
            continue
        value = payload.get(key)
        if isinstance(value, (bool, int, float)) or value is None:
            compact[key] = value
        elif isinstance(value, str) and len(value) <= 512:
            compact[key] = value

    preview = _json_text(payload)
    compact["response_preview_original_chars"] = len(preview)
    low = 0
    high = len(preview)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = dict(compact)
        candidate["response_preview"] = preview[:middle]
        if _json_char_count(candidate) <= IDA_ACTION_RESPONSE_MAX_CHARS:
            low = middle
        else:
            high = middle - 1
    compact["response_preview"] = preview[:low]
    return compact


def _budget_list_response(
    payload: dict[str, Any],
    *,
    list_key: str,
    offset: int,
    source_has_more: bool | None = None,
    max_next_offset: int | None = None,
) -> dict[str, Any]:
    items = payload.get(list_key)
    if not isinstance(items, list):
        return _hard_cap_response(payload)

    original_items = list(items)
    if source_has_more is None:
        source_has_more = bool(payload.get("truncated")) or payload.get("next_offset") is not None

    def enforce_forward_progress(result: dict[str, Any]) -> dict[str, Any]:
        next_offset = result.get("next_offset")
        if isinstance(next_offset, int) and next_offset <= offset:
            result = dict(result)
            result["next_offset"] = None
            result["more_available"] = bool(source_has_more)
            result["truncation_hint"] = (
                "Pagination made no forward progress; narrow the query or use "
                "executeIdapython for a targeted read."
            )
        return result

    def build(count: int) -> dict[str, Any]:
        result = dict(payload)
        result[list_key] = original_items[:count]
        result["returned"] = count
        adapter_truncated = count < len(original_items)
        truncated = bool(source_has_more) or adapter_truncated
        result["truncated"] = truncated
        if truncated:
            candidate_next_offset = offset + count
            if max_next_offset is not None and candidate_next_offset >= max_next_offset:
                result["next_offset"] = None
                result["more_available"] = True
                result["truncation_hint"] = (
                    "The adapter pagination window is exhausted; use executeIdapython "
                    "for a custom narrower query."
                )
            else:
                result["next_offset"] = candidate_next_offset
        else:
            result["next_offset"] = None
        if adapter_truncated:
            result["response_truncated"] = True
            result["response_char_limit"] = IDA_ACTION_RESPONSE_MAX_CHARS
        else:
            result.pop("response_truncated", None)
            result.pop("response_char_limit", None)
        return result

    full = enforce_forward_progress(build(len(original_items)))
    if _json_char_count(full) <= IDA_ACTION_RESPONSE_MAX_CHARS:
        return full

    low = 0
    high = len(original_items)
    while low < high:
        middle = (low + high + 1) // 2
        if _json_char_count(build(middle)) <= IDA_ACTION_RESPONSE_MAX_CHARS:
            low = middle
        else:
            high = middle - 1

    result = build(low)
    if low == 0 and original_items:
        preview = _json_text(original_items[0])
        result["oversized_item_omitted"] = True
        result["oversized_item_preview"] = preview[:8_000]
        result["oversized_item_original_chars"] = len(preview)
        candidate_next_offset = offset + 1
        if max_next_offset is not None and candidate_next_offset >= max_next_offset:
            result["next_offset"] = None
            result["more_available"] = bool(source_has_more)
            if source_has_more:
                result["truncation_hint"] = (
                    "The adapter pagination window is exhausted; use executeIdapython "
                    "for a custom narrower query."
                )
            else:
                result.pop("truncation_hint", None)
        else:
            result["next_offset"] = candidate_next_offset
    return _hard_cap_response(enforce_forward_progress(result))


def _budget_decompile_response(payload: dict[str, Any]) -> dict[str, Any]:
    result = _truncate_string_field(
        dict(payload),
        "pseudocode",
        DECOMPILE_PSEUDOCODE_MAX_CHARS,
    )
    disassembly = result.get("disassembly")
    if isinstance(disassembly, list) and _json_char_count(result) > IDA_ACTION_RESPONSE_MAX_CHARS:
        source_truncated = bool(result.get("disassembly_truncated"))

        def build(count: int) -> dict[str, Any]:
            candidate = dict(result)
            candidate["disassembly"] = disassembly[:count]
            candidate["disassembly_returned"] = count
            adapter_truncated = count < len(disassembly)
            candidate["disassembly_truncated"] = source_truncated or adapter_truncated
            if adapter_truncated:
                candidate["response_truncated"] = True
                candidate["response_char_limit"] = IDA_ACTION_RESPONSE_MAX_CHARS
            return candidate

        low = 0
        high = len(disassembly)
        while low < high:
            middle = (low + high + 1) // 2
            if _json_char_count(build(middle)) <= IDA_ACTION_RESPONSE_MAX_CHARS:
                low = middle
            else:
                high = middle - 1
        result = build(low)
    return _hard_cap_response(result)


def _budget_execute_response(payload: dict[str, Any]) -> dict[str, Any]:
    result = _truncate_string_field(dict(payload), "stdout", EXECUTE_STDOUT_MAX_CHARS)
    result = _truncate_string_field(result, "stderr", EXECUTE_STDERR_MAX_CHARS)
    result = _truncate_json_field(result, "result", EXECUTE_RESULT_MAX_JSON_CHARS)
    result = _truncate_json_field(result, "error", EXECUTE_ERROR_MAX_JSON_CHARS)
    return _hard_cap_response(result)


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
    timeout: float = GPT_ACTION_READ_TIMEOUT_SECONDS,
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
    def list_ida_instances(request: ListIdaInstancesRequest) -> dict[str, Any]:
        server = _load_ida_server_module()
        if server is None:
            return _setup_error()

        records = _instance_records(server)
        total = len(records)
        if not records:
            return _hard_cap_response({
                "count": 0,
                "returned": 0,
                "offset": request.offset,
                "next_offset": None,
                "truncated": False,
                "instances": [],
                "hint": "No IDA instances found. Start IDA Pro and enable the plugin.",
            })

        page_end = request.offset + request.limit
        page = records[request.offset:page_end]
        return _budget_list_response(
            {
                "count": total,
                "offset": request.offset,
                "limit": request.limit,
                "instances": page,
            },
            list_key="instances",
            offset=request.offset,
            source_has_more=page_end < total,
            max_next_offset=total,
        )

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
        return _hard_cap_response(
            _request_plugin(server, request, "/metadata", timeout=10.0)
        )

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
        result = _request_plugin(
            server,
            request,
            "/functions",
            method="POST",
            data=payload,
            timeout=GPT_ACTION_READ_TIMEOUT_SECONDS,
        )
        return _budget_list_response(
            result,
            list_key="functions",
            offset=request.offset,
        )

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
        result = _request_plugin(
            server,
            request,
            "/decompile",
            method="POST",
            data=payload,
            timeout=GPT_ACTION_READ_TIMEOUT_SECONDS,
        )
        return _budget_decompile_response(result)

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
        fetch_limit = request.offset + request.limit
        payload = {
            "address": request.address,
            "name": request.name,
            "direction": request.direction,
            "xref_kind": request.xref_kind,
            "limit": fetch_limit,
        }
        result = _request_plugin(
            server,
            request,
            "/xrefs",
            method="POST",
            data=payload,
            timeout=GPT_ACTION_READ_TIMEOUT_SECONDS,
        )
        xrefs = result.get("xrefs")
        if not isinstance(xrefs, list):
            return _hard_cap_response(result)

        page_end = request.offset + request.limit
        page = xrefs[request.offset:page_end]
        source_has_more = len(xrefs) > page_end or bool(result.get("truncated"))
        result = dict(result)
        result["xrefs"] = page
        query = result.get("query")
        if isinstance(query, dict):
            query = dict(query)
            query["offset"] = request.offset
            query["limit"] = request.limit
            result["query"] = query
        result["offset"] = request.offset
        return _budget_list_response(
            result,
            list_key="xrefs",
            offset=request.offset,
            source_has_more=source_has_more,
            max_next_offset=XREF_ADAPTER_MAX_ITEMS,
        )

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
            return _budget_execute_response(_tool_error(label))

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
            return _budget_execute_response(result)
        except Exception as exc:
            timeout_class = getattr(server, "IdaPluginResponseTimeout", None)
            timeout_type = getattr(timeout_class, "__name__", "")
            if type(exc).__name__ in {timeout_type, "IdaPluginResponseTimeout"}:
                return _budget_execute_response({
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
                })
            return _budget_execute_response(
                _tool_error(str(exc), instance_id=resolved_instance_id, port=port)
            )
