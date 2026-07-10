"""FastAPI gateway for GPT Actions.

The public surface is intentionally small:

- retrieveSkillContext: default first call for skill-backed tasks.
- searchSkillDocs: targeted follow-up retrieval.
- readSkillContent: precise file reading by safe path.

``listSkills`` and ``resolveSkill`` stay available as debug endpoints, but they
are intentionally hidden from the default OpenAPI schema used by GPT Actions.
The web console is also hidden from OpenAPI and may request debug output.
"""

from __future__ import annotations

import argparse
import copy
import secrets
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .ida_actions import register_ida_actions
from .runtime import (
    DEFAULT_MAX_SKILLS,
    SkillNotFoundError,
    SkillPathError,
    env_value_from_environment_or_dotenv,
    load_runtime,
)

BEARER_TOKEN_ENV_VAR = "SKILL_TEMPLE_BEARER_TOKEN"


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResolveSkillRequest(StrictRequest):
    query: str = Field(..., description="The user's task or request text.")
    hinted_skill_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Explicit skill selection handles, for example ['idapython']."
        ),
    )
    max_results: int = Field(default=3, ge=1, le=10)


class RetrieveSkillContextRequest(StrictRequest):
    query: str = Field(..., description="The user's original task or request text.")
    hinted_skill_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Explicit skill selection handles chosen from available_skills."
        ),
    )
    allow_skill_chaining: bool = Field(
        default=False,
        description=(
            "Backward-compatible hint. Multiple explicit selections are always loaded "
            "together when within the response limit."
        ),
    )


class ConsoleRetrieveRequest(RetrieveSkillContextRequest):
    include_debug: bool = Field(
        default=False,
        description="Return hidden routing diagnostics for the development console.",
    )


class SearchSkillDocsRequest(StrictRequest):
    skill_id: str = Field(..., description="Skill id to search, such as 'idapython'.")
    query: str = Field(..., description="Search query for the skill documentation.")
    paths: list[str] | None = Field(
        default=None,
        description="Optional safe relative file paths to restrict the search.",
    )
    limit: int = Field(default=5, ge=1, le=30)


class ReadSkillContentRequest(StrictRequest):
    skill_id: str = Field(..., description="Skill id to read from, such as 'idapython'.")
    path: str = Field(
        ...,
        description="Safe relative path inside the skill, for example docs/ida_hexrays.md.",
    )
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=2000, ge=1, le=5000)


class ErrorDetail(BaseModel):
    code: str
    message: str
    suggested_next_action: str


class StructuredErrorResponse(BaseModel):
    error: ErrorDetail


class SelectedSkillPacket(BaseModel):
    skill_id: str
    name: str
    description: str
    role: Literal["primary", "secondary"]
    source_path: str
    instructions: str
    content_hash: str
    total_lines: int
    truncated: bool
    next_start_line: int | None = None
    referenced_paths: list[str] = Field(default_factory=list)


class AvailableSkillMetadata(BaseModel):
    skill_id: str = Field(..., description="Selection handle used in hinted_skill_ids.")
    name: str
    description: str
    description_truncated: bool = False
    entrypoint: str
    content_hash: str


class Decision(BaseModel):
    selected: bool
    next_action: Literal[
        "followSkillInstructions",
        "readSkillContent",
        "selectSkillOrAnswer",
        "retryWithFewerSkills",
        "answerWithoutSkill",
    ]
    reason: str
    stop_retrieval: bool


class RetrieveSkillContextResponse(BaseModel):
    selected_skills: list[SelectedSkillPacket] = Field(default_factory=list)
    available_skills: list[AvailableSkillMetadata] = Field(default_factory=list)
    available_skill_count: int
    included_skill_count: int
    omitted_skill_count: int
    descriptions_truncated: bool
    catalog_char_limit: int
    catalog_included: bool
    explicit_skill_ids: list[str] = Field(default_factory=list)
    unknown_skill_mentions: list[str] = Field(default_factory=list)
    omitted_explicit_skill_ids: list[str] = Field(default_factory=list)
    decision: Decision


class SearchMatch(BaseModel):
    skill_id: str
    path: str
    title: str
    heading_path: str
    score: float
    mode: str
    engine: str
    start_line: int
    end_line: int
    excerpt: str
    symbols: list[str] = Field(default_factory=list)
    document_symbols: list[str] = Field(default_factory=list)
    rank_features: dict[str, Any] = Field(default_factory=dict)
    why_relevant: str
    content_hash: str


class SearchSkillDocsResponse(BaseModel):
    skill_id: str
    query: str
    mode: str
    engine: str
    matches: list[SearchMatch] = Field(default_factory=list)
    recommended_next_action: Literal["readSkillContent", "none"]


class ReadSkillContentResponse(BaseModel):
    skill_id: str
    path: str
    start_line: int
    end_line: int
    total_lines: int
    content: str
    content_hash: str
    truncated: bool
    next_start_line: int | None = None


def _normalize_server_url(server_url: str | None) -> str | None:
    if server_url is None:
        return None

    normalized = server_url.strip().rstrip("/")
    if not normalized:
        return None

    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("server_url must be an absolute http(s) URL, for example https://example.com")
    return normalized


def _first_header_value(value: str | None) -> str | None:
    if value is None:
        return None
    first = value.split(",", 1)[0].strip()
    return first or None


def _request_server_url(request: Request) -> str:
    forwarded_proto = _first_header_value(request.headers.get("x-forwarded-proto"))
    forwarded_host = _first_header_value(request.headers.get("x-forwarded-host"))
    forwarded_prefix = _first_header_value(request.headers.get("x-forwarded-prefix")) or ""

    if forwarded_proto and forwarded_host:
        forwarded_url = f"{forwarded_proto}://{forwarded_host}{forwarded_prefix}"
        return _normalize_server_url(forwarded_url) or ""

    return _normalize_server_url(str(request.base_url)) or ""


def _normalize_bearer_token(token: str | None) -> str | None:
    if token is None:
        return None
    normalized = token.strip()
    return normalized or None


def _requires_bearer_auth(path: str) -> bool:
    return path.startswith("/v1/") or path == "/console/retrieve"


def _valid_bearer_authorization(authorization: str | None, expected_token: str) -> bool:
    if not authorization:
        return False
    scheme, separator, value = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return False
    return secrets.compare_digest(value.strip(), expected_token)


def _add_bearer_auth_security(schema: dict[str, Any]) -> dict[str, Any]:
    components = schema.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    security_schemes["BearerAuth"] = {"type": "http", "scheme": "bearer"}

    for path, path_item in schema.get("paths", {}).items():
        if not _requires_bearer_auth(path):
            continue
        for operation in path_item.values():
            if isinstance(operation, dict):
                operation.setdefault("security", [{"BearerAuth": []}])
    return schema


def create_app(skills_dir: str | Path | None = None, server_url: str | None = None) -> FastAPI:
    runtime = load_runtime(skills_dir)
    configured_server_url = _normalize_server_url(
        server_url or env_value_from_environment_or_dotenv("SKILL_TEMPLE_SERVER_URL")
    )
    bearer_token = _normalize_bearer_token(
        env_value_from_environment_or_dotenv(BEARER_TOKEN_ENV_VAR)
    )

    app = FastAPI(
        title="Skill Temple Gateway",
        version="0.1.0",
        description=(
            "Codex-style model-driven skill selection adapted to Custom GPT Actions. "
            "The model chooses from a bounded catalog, then the gateway loads explicit "
            "SKILL.md entrypoints and supports progressive disclosure."
        ),
        openapi_url=None,
        servers=([{"url": configured_server_url}] if configured_server_url else None),
    )

    original_openapi = app.openapi

    def openapi_with_optional_bearer_auth() -> dict[str, Any]:
        schema = original_openapi()
        if bearer_token:
            _add_bearer_auth_security(schema)
        return schema

    app.openapi = openapi_with_optional_bearer_auth  # type: ignore[method-assign]

    @app.middleware("http")
    async def bearer_auth_middleware(request: Request, call_next: Any) -> Any:
        if bearer_token and _requires_bearer_auth(request.url.path):
            if not _valid_bearer_authorization(
                request.headers.get("authorization"),
                bearer_token,
            ):
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": {
                            "code": "unauthorized",
                            "message": "Missing or invalid Bearer token.",
                            "suggested_next_action": "configure_bearer_auth",
                        }
                    },
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return await call_next(request)

    def structured_error(
        code: str,
        message: str,
        suggested_next_action: str,
    ) -> dict[str, object]:
        return {
            "error": {
                "code": code,
                "message": message,
                "suggested_next_action": suggested_next_action,
            }
        }

    def retrieve_context(
        request: RetrieveSkillContextRequest,
        include_debug: bool = False,
    ) -> dict[str, object]:
        return runtime.retrieve(
            query=request.query,
            hinted_skill_ids=request.hinted_skill_ids,
            max_skills=DEFAULT_MAX_SKILLS,
            allow_skill_chaining=request.allow_skill_chaining,
            include_debug=include_debug,
        )

    @app.get("/openapi.json", include_in_schema=False)
    def openapi_json(request: Request) -> dict[str, Any]:
        schema = copy.deepcopy(app.openapi())
        if "servers" not in schema:
            schema["servers"] = [{"url": _request_server_url(request)}]
        return schema

    @app.get(
        "/health",
        operation_id="healthCheck",
        summary="Check gateway health.",
        include_in_schema=False,
    )
    def health_check() -> dict[str, object]:
        return {"status": "ok", "skills_dir": str(runtime.skills_dir)}

    @app.get(
        "/v1/skills",
        operation_id="listSkills",
        summary="List available reusable skills.",
        description=(
            "Use for setup or debugging. Normal GPT workflows should usually call "
            "retrieveSkillContext directly with the user's task."
        ),
        include_in_schema=False,
    )
    def list_skills() -> dict[str, object]:
        return runtime.list_skills()

    @app.post(
        "/v1/skills/resolve",
        operation_id="resolveSkill",
        summary="Resolve exact skill hints or mentions.",
        description=(
            "Diagnostic endpoint for exact hinted_skill_ids, Codex-style $skill mentions, "
            "and the gateway's @skill extension. "
            "It does not perform semantic description ranking."
        ),
        include_in_schema=False,
    )
    def resolve_skill(request: ResolveSkillRequest) -> dict[str, object]:
        return runtime.resolve(
            query=request.query,
            hinted_skill_ids=request.hinted_skill_ids,
            max_results=request.max_results,
        )

    @app.get("/console", response_class=HTMLResponse, include_in_schema=False)
    def console() -> HTMLResponse:
        return HTMLResponse(CONSOLE_HTML)

    @app.post("/console/retrieve", include_in_schema=False)
    def console_retrieve(request: ConsoleRetrieveRequest) -> dict[str, object]:
        try:
            return retrieve_context(request, include_debug=request.include_debug)
        except SkillNotFoundError as exc:
            detail = structured_error("skill_not_found", str(exc), "check_skill_id")
            raise HTTPException(status_code=404, detail=detail) from exc

    @app.post(
        "/v1/skills/retrieve",
        operation_id="retrieveSkillContext",
        response_model=RetrieveSkillContextResponse,
        response_model_exclude_none=True,
        responses={404: {"model": StructuredErrorResponse}},
        summary="Discover or load explicitly selected skills.",
        description=(
            "Return a bounded skill catalog, or load exact hinted skills and explicit "
            "$skill mentions. @skill is also supported as a gateway extension."
        ),
        openapi_extra={"x-openai-isConsequential": False},
    )
    def retrieve_skill_context(
        request: RetrieveSkillContextRequest,
    ) -> RetrieveSkillContextResponse:
        try:
            return RetrieveSkillContextResponse.model_validate(retrieve_context(request))
        except SkillNotFoundError as exc:
            detail = structured_error("skill_not_found", str(exc), "check_skill_id")
            raise HTTPException(status_code=404, detail=detail) from exc

    @app.post(
        "/v1/skills/search",
        operation_id="searchSkillDocs",
        response_model=SearchSkillDocsResponse,
        responses={404: {"model": StructuredErrorResponse}},
        summary="Search documentation for a specific skill.",
        description=(
            "Search indexed resources within one selected skill."
        ),
        openapi_extra={"x-openai-isConsequential": False},
    )
    def search_skill_docs(request: SearchSkillDocsRequest) -> SearchSkillDocsResponse:
        try:
            return SearchSkillDocsResponse.model_validate(
                runtime.search(
                    skill_id=request.skill_id,
                    query=request.query,
                    paths=request.paths,
                    limit=request.limit,
                    mode="keyword",
                    max_chars_per_match=2000,
                    include_manifest=False,
                )
            )
        except SkillNotFoundError as exc:
            detail = structured_error("skill_not_found", str(exc), "check_skill_id")
            raise HTTPException(status_code=404, detail=detail) from exc
        except SkillPathError as exc:
            detail = structured_error("unsafe_or_missing_path", str(exc), "check_path")
            raise HTTPException(status_code=404, detail=detail) from exc

    @app.post(
        "/v1/skills/read",
        operation_id="readSkillContent",
        response_model=ReadSkillContentResponse,
        responses={404: {"model": StructuredErrorResponse}},
        summary="Read a skill file by safe relative path.",
        description=(
            "Read an exact safe relative path within one selected skill."
        ),
        openapi_extra={"x-openai-isConsequential": False},
    )
    def read_skill_content(request: ReadSkillContentRequest) -> ReadSkillContentResponse:
        try:
            return ReadSkillContentResponse.model_validate(
                runtime.read(
                    skill_id=request.skill_id,
                    path=request.path,
                    start_line=request.start_line,
                    max_lines=request.max_lines,
                )
            )
        except SkillNotFoundError as exc:
            detail = structured_error("skill_not_found", str(exc), "check_skill_id")
            raise HTTPException(status_code=404, detail=detail) from exc
        except SkillPathError as exc:
            detail = structured_error("unsafe_or_missing_path", str(exc), "check_path")
            raise HTTPException(status_code=404, detail=detail) from exc

    register_ida_actions(app)

    return app


CONSOLE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Skill Temple Console</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 2rem; max-width: 980px; }
    label { display: block; font-weight: 600; margin-top: 1rem; }
    input[type="password"], input[type="text"], input[type="number"], select, textarea {
      width: 100%; box-sizing: border-box; padding: .55rem; font: inherit;
    }
    textarea { min-height: 7rem; }
    button { margin-top: 1rem; padding: .65rem 1rem; font: inherit; }
    pre { background: #111827; color: #e5e7eb; padding: 1rem; overflow: auto; }
    .row { display: flex; gap: 1rem; align-items: center; }
    .row label { font-weight: 400; margin-top: .75rem; }
    .actions { display: flex; gap: .75rem; flex-wrap: wrap; align-items: center; }
    .muted { color: #6b7280; }
    .panel { border-top: 1px solid #e5e7eb; margin-top: 1.5rem; padding-top: 1rem; }
    #api_log { min-height: 12rem; white-space: pre-wrap; }
  </style>
</head>
<body>
  <h1>Skill Temple Console</h1>
  <p>
    This development console is hidden from the GPT Action OpenAPI schema
    and may request debug output.
  </p>

  <section class="panel">
    <h2>Bearer Token</h2>
    <p class="muted">
      Optional. Stored only in this browser tab session and redacted in the trace log.
    </p>
    <label for="bearer_token">Authorization token</label>
    <input id="bearer_token" type="password" autocomplete="off"
      placeholder="Bearer token from .env" />
    <div class="actions">
      <button id="save_token" type="button">Save token to sessionStorage</button>
      <button id="clear_token" type="button">Clear token</button>
      <span id="token_state" class="muted">No token saved.</span>
    </div>
  </section>

  <section class="panel">
    <h2>Quick retrieveSkillContext debug call</h2>
  <label for="query">Query</label>
  <textarea id="query">@idapython write a script to find xrefs to strcpy</textarea>
  <label for="hints">Hinted skill ids, comma-separated</label>
  <input id="hints" type="text" value="idapython" />
  <div class="row">
    <label><input id="allow_chain" type="checkbox" /> Allow skill chaining</label>
    <label><input id="include_debug" type="checkbox" checked /> Include debug</label>
  </div>
  <button id="run">Retrieve</button>
  </section>

  <section class="panel">
    <h2>Manual GPT Action call</h2>
    <label for="operation">Operation</label>
    <select id="operation"></select>
    <label for="operation_body">JSON body</label>
    <textarea id="operation_body"></textarea>
    <button id="run_operation" type="button">Run Operation</button>
  </section>

  <h2>Result</h2>
  <pre id="result">Ready.</pre>

  <section class="panel">
    <div class="actions">
      <h2>API Call Timeline</h2>
      <button id="clear_log" type="button">Clear Timeline</button>
    </div>
    <pre id="api_log">Ready.</pre>
  </section>

  <script>
    const result = document.getElementById('result');
    const apiLog = document.getElementById('api_log');
    const tokenInput = document.getElementById('bearer_token');
    const tokenState = document.getElementById('token_state');
    const operationSelect = document.getElementById('operation');
    const operationBody = document.getElementById('operation_body');
    const TOKEN_KEY = 'skill_temple_console_bearer_token';

    const operations = {
      retrieveSkillContext: {
        method: 'POST',
        url: '/v1/skills/retrieve',
        body: {
          query: '@idapython write a script to find xrefs to strcpy',
          hinted_skill_ids: ['idapython'],
          allow_skill_chaining: false
        }
      },
      searchSkillDocs: {
        method: 'POST',
        url: '/v1/skills/search',
        body: {skill_id: 'idapython', query: 'ctree_visitor_t cot_call', limit: 5}
      },
      readSkillContent: {
        method: 'POST',
        url: '/v1/skills/read',
        body: {skill_id: 'idapython', path: 'SKILL.md', start_line: 1, max_lines: 80}
      },
      listIdaInstances: {method: 'POST', url: '/v1/ida/instances', body: {}},
      getIdaDatabaseInfo: {method: 'POST', url: '/v1/ida/database-info', body: {}},
      listIdaFunctions: {
        method: 'POST',
        url: '/v1/ida/functions',
        body: {offset: 0, limit: 50}
      },
      decompileIdaFunction: {
        method: 'POST',
        url: '/v1/ida/decompile',
        body: {name: 'main', include_disassembly: false}
      },
      getIdaXrefs: {
        method: 'POST',
        url: '/v1/ida/xrefs',
        body: {name: 'main', direction: 'to', xref_kind: 'all', limit: 100}
      },
      executeIdapython: {
        method: 'POST',
        url: '/v1/ida/execute',
        body: {
          code: 'import idaapi\nresult = {"imagebase": hex(idaapi.get_imagebase())}',
          capture_output: true,
          timeout_seconds: 30
        }
      }
    };

    for (const name of Object.keys(operations)) {
      const option = document.createElement('option');
      option.value = name;
      option.textContent = name;
      operationSelect.appendChild(option);
    }

    function updateOperationBody() {
      const operation = operations[operationSelect.value];
      operationBody.value = JSON.stringify(operation.body, null, 2);
    }

    function setTokenState() {
      tokenState.textContent = sessionStorage.getItem(TOKEN_KEY)
        ? 'Token saved.'
        : 'No token saved.';
    }

    function nowStamp() {
      const now = new Date();
      return now.toLocaleTimeString() + '.' + String(now.getMilliseconds()).padStart(3, '0');
    }

    function appendLog(title, payload) {
      if (apiLog.textContent === 'Ready.') {
        apiLog.textContent = '';
      }
      const rendered = typeof payload === 'string' ? payload : JSON.stringify(payload, null, 2);
      apiLog.textContent += `[${nowStamp()}] ${title}\n${rendered}\n\n`;
      apiLog.scrollTop = apiLog.scrollHeight;
    }

    function getHeaders() {
      const token = sessionStorage.getItem(TOKEN_KEY);
      const headers = {'Content-Type': 'application/json'};
      if (token) {
        headers.Authorization = `Bearer ${token}`;
      }
      return headers;
    }

    function redactHeaders(headers) {
      const redacted = {...headers};
      if (redacted.Authorization) {
        redacted.Authorization = 'Bearer ***redacted***';
      }
      return redacted;
    }

    async function apiCall({label, method, url, body}) {
      const headers = getHeaders();
      const started = performance.now();
      appendLog(`${label}: request start`, {method, url, headers: redactHeaders(headers), body});
      result.textContent = 'Loading...';
      try {
        appendLog(`${label}: waiting response`, 'fetch() in progress...');
        const response = await fetch(url, {
          method,
          headers,
          body: JSON.stringify(body)
        });
        const durationMs = Math.round(performance.now() - started);
        const responseHeaders = Object.fromEntries(response.headers.entries());
        const contentType = response.headers.get('content-type') || '';
        appendLog(`${label}: response received`, {
          status: response.status,
          statusText: response.statusText,
          contentType,
          durationMs
        });
        appendLog(`${label}: response headers`, responseHeaders);
        const text = await response.text();
        try {
          const data = text ? JSON.parse(text) : null;
          appendLog(`${label}: parsed json`, data);
          result.textContent = JSON.stringify(data, null, 2);
          return data;
        } catch (parseError) {
          const diagnostic = {
            error: String(parseError),
            status: response.status,
            contentType,
            rawText: text
          };
          appendLog(`${label}: non-json response`, diagnostic);
          result.textContent = JSON.stringify(diagnostic, null, 2);
          return diagnostic;
        }
      } catch (error) {
        const durationMs = Math.round(performance.now() - started);
        const diagnostic = {error: String(error), durationMs};
        appendLog(`${label}: request failed`, diagnostic);
        result.textContent = JSON.stringify(diagnostic, null, 2);
        return diagnostic;
      }
    }

    tokenInput.value = sessionStorage.getItem(TOKEN_KEY) || '';
    setTokenState();
    updateOperationBody();

    document.getElementById('save_token').addEventListener('click', () => {
      const token = tokenInput.value.trim();
      if (token) {
        sessionStorage.setItem(TOKEN_KEY, token);
      } else {
        sessionStorage.removeItem(TOKEN_KEY);
      }
      setTokenState();
      appendLog('Bearer token updated', {Authorization: token ? 'Bearer ***redacted***' : null});
    });

    document.getElementById('clear_token').addEventListener('click', () => {
      tokenInput.value = '';
      sessionStorage.removeItem(TOKEN_KEY);
      setTokenState();
      appendLog('Bearer token cleared', {Authorization: null});
    });

    document.getElementById('clear_log').addEventListener('click', () => {
      apiLog.textContent = 'Ready.';
    });

    operationSelect.addEventListener('change', updateOperationBody);

    document.getElementById('run').addEventListener('click', async () => {
      const hinted = document.getElementById('hints').value
        .split(',').map(v => v.trim()).filter(Boolean);
      const body = {
        query: document.getElementById('query').value,
        hinted_skill_ids: hinted,
        allow_skill_chaining: document.getElementById('allow_chain').checked,
        include_debug: document.getElementById('include_debug').checked
      };
      await apiCall({
        label: 'consoleRetrieve',
        method: 'POST',
        url: '/console/retrieve',
        body
      });
    });

    document.getElementById('run_operation').addEventListener('click', async () => {
      const operation = operations[operationSelect.value];
      try {
        const body = JSON.parse(operationBody.value || '{}');
        await apiCall({
          label: operationSelect.value,
          method: operation.method,
          url: operation.url,
          body
        });
      } catch (error) {
        const diagnostic = {error: String(error), rawText: operationBody.value};
        appendLog(`${operationSelect.value}: invalid request JSON`, diagnostic);
        result.textContent = JSON.stringify(diagnostic, null, 2);
      }
    });
  </script>
</body>
</html>
"""


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Skill Temple GPT Action gateway.")
    parser.add_argument(
        "--skills-dir",
        type=Path,
        default=None,
        help="Directory containing skill folders.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--server-url",
        default=None,
        help=(
            "Public absolute http(s) URL to publish in OpenAPI servers. "
            "Can also be set with SKILL_TEMPLE_SERVER_URL."
        ),
    )
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        create_app(args.skills_dir, server_url=args.server_url),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
