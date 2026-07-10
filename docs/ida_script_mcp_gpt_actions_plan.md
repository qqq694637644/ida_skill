# IDA Script MCP GPT Actions Integration Plan

## Goal

Integrate `qqq694637644/ida-script-mcp-main` into `ida_skill` so a Custom GPT can use one GPT Action schema for both:

1. Skill Temple documentation retrieval.
2. Live IDA Pro analysis and IDAPython execution through the existing IDA Script MCP plugin.

This is intended for personal use. All GPT Action operations, including IDAPython execution, will publish:

```json
{"x-openai-isConsequential": false}
```

The IDA plugin must still stay bound to localhost. Only the Skill Temple / GPT Actions gateway should be exposed through the public reverse proxy.

## Key decisions

### Use a Git submodule, do not copy the project

Add `ida-script-mcp-main` as a submodule inside `ida_skill`:

```powershell
git submodule add https://github.com/qqq694637644/ida-script-mcp-main external/ida-script-mcp-main
git submodule update --init --recursive
```

Recommended path:

```text
external/ida-script-mcp-main
```

Reasons:

- Keep `ida-script-mcp-main` history and updates independent.
- Avoid duplicating the IDA plugin, protocol models, installer, and packaged IDAPython docs.
- Allow `ida_skill` to focus on the GPT Actions HTTP gateway.

### One public GPT Actions gateway

Use `ida_skill` as the only public OpenAPI service.

```text
Custom GPT
  -> https://gptaction.casacam.net/skills/openapi.json
  -> https://gptaction.casacam.net/skills/v1/...
      -> ida_skill FastAPI service, usually 127.0.0.1:8001
          -> ida_script_mcp.server wrapper functions
              -> IDA plugin local HTTP server, usually 127.0.0.1:13338+
                  -> IDA Pro
```

Do not expose the IDA plugin port directly to the public internet.

### Two local services are acceptable

There are two valid deployment shapes:

#### Preferred shape: one FastAPI gateway plus IDA plugin

```text
127.0.0.1:8001   ida_skill FastAPI GPT Actions gateway
127.0.0.1:13338  IDA-Script-MCP plugin inside IDA
```

The FastAPI gateway imports `ida_script_mcp.server` from the editable submodule install and calls the existing wrapper functions.

#### Optional shape: separate local adapter service

```text
127.0.0.1:8001   Skill Temple / skill retrieval service
127.0.0.1:8002   IDA GPT Actions adapter service
127.0.0.1:13338  IDA-Script-MCP plugin inside IDA
```

This is only worth doing if the IDA Action surface grows large. For the first implementation, prefer a single OpenAPI schema from `ida_skill` because Custom GPT configuration is simpler.

## Submodule dependency strategy

After adding the submodule, install it editable in the same Python environment used to run `ida_skill`:

```powershell
cd C:\Users\Administrator\Desktop\ida_skill
py -3 -m pip install -e .[dev]
py -3 -m pip install -e external/ida-script-mcp-main
```

The GPT gateway should import `ida_script_mcp.server` lazily. If it is missing, return a structured setup error such as:

```json
{
  "error": "ida_script_mcp is not installed",
  "hint": "Run: py -3 -m pip install -e external/ida-script-mcp-main"
}
```

This keeps the base skill gateway usable even before the submodule dependency is installed.

## Skill documentation source

Do not copy the IDAPython docs into `ida_skill`.

Use the submodule package resources as the skills directory:

```env
SKILL_TEMPLE_SKILLS_DIR=C:/Users/Administrator/Desktop/ida_skill/external/ida-script-mcp-main/src/ida_script_mcp/resources
```

That directory contains the `idapython/` skill folder and avoids pointing Skill Temple at the full submodule root, which also contains non-skill folders such as `src/` and `tests/`.

If later the runtime needs multiple skill roots, add explicit multi-root support instead of copying the docs.

## Public reverse proxy layout

Current Caddy prefix style should continue to work:

```caddy
gptaction.casacam.net {
    redir /skills /skills/ 308

    handle_path /skills/* {
        reverse_proxy 127.0.0.1:8001
    }

    handle /console* {
        reverse_proxy 127.0.0.1:8001
    }

    handle {
        respond "OK: use /skills" 200
    }
}
```

Use this `.env` in the `ida_skill` working directory:

```env
SKILL_TEMPLE_SERVER_URL=https://gptaction.casacam.net/skills
SKILL_TEMPLE_SKILLS_DIR=C:/Users/Administrator/Desktop/ida_skill/external/ida-script-mcp-main/src/ida_script_mcp/resources
IDA_SCRIPT_MCP_HOST=127.0.0.1
```

Usually do not set `IDA_SCRIPT_MCP_PORT`; the MCP wrapper can auto-discover running IDA plugin instances from the instance registry file.

Set `IDA_SCRIPT_MCP_PORT` only when forcing one specific IDA plugin port:

```env
IDA_SCRIPT_MCP_PORT=13338
```

## GPT Action endpoint surface

Keep the existing skill operations:

| Operation ID | Method | Path | Purpose |
| --- | --- | --- | --- |
| `retrieveSkillContext` | `POST` | `/v1/skills/retrieve` | Retrieve relevant skill rules and docs. |
| `searchSkillDocs` | `POST` | `/v1/skills/search` | Search inside skill docs. |
| `readSkillContent` | `POST` | `/v1/skills/read` | Read a safe relative skill file path. |

Add IDA operations:

| Operation ID | Method | Path | Wrapper target |
| --- | --- | --- | --- |
| `listIdaInstances` | `POST` | `/v1/ida/instances` | `ida_script_mcp.server.list_ida_instances` |
| `getIdaDatabaseInfo` | `POST` | `/v1/ida/database-info` | `ida_script_mcp.server.get_ida_database_info` |
| `listIdaFunctions` | `POST` | `/v1/ida/functions` | `ida_script_mcp.server.list_functions` |
| `decompileIdaFunction` | `POST` | `/v1/ida/decompile` | `ida_script_mcp.server.decompile_function` |
| `getIdaXrefs` | `POST` | `/v1/ida/xrefs` | `ida_script_mcp.server.get_xrefs` |
| `executeIdapython` | `POST` | `/v1/ida/execute` | `ida_script_mcp.server.execute_idapython` |

Every operation must include:

```python
openapi_extra={"x-openai-isConsequential": False}
```

Descriptions should remain under 300 characters to satisfy Custom GPT Action import limits.

## Request model plan

Create a new module:

```text
src/skill_temple/ida_actions.py
```

Define strict Pydantic request models matching the existing MCP server inputs closely, but with GPT-friendly names.

### Shared target fields

```python
class IdaTargetRequest(StrictRequest):
    instance_id: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
```

### Instances

Use a POST with an empty strict request body so the OpenAPI surface is consistent:

```python
class ListIdaInstancesRequest(StrictRequest):
    pass
```

### Database info

```python
class GetIdaDatabaseInfoRequest(IdaTargetRequest):
    pass
```

### Functions

```python
class ListIdaFunctionsRequest(IdaTargetRequest):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=200, ge=1, le=5000)
    name_contains: str | None = None
    segment: str | None = None
    include_thunks: bool = False
    include_library_functions: bool = False
```

### Decompile

```python
class DecompileIdaFunctionRequest(IdaTargetRequest):
    address: str | None = None
    name: str | None = None
    include_disassembly: bool = False
```

Validation rule: require exactly one of `address` or `name`.

### Xrefs

```python
class GetIdaXrefsRequest(IdaTargetRequest):
    address: str | None = None
    name: str | None = None
    direction: Literal["to", "from"] = "to"
    xref_kind: Literal["all", "code", "data"] = "all"
    limit: int = Field(default=200, ge=1, le=5000)
```

Validation rule: require exactly one of `address` or `name`.

### Execute IDAPython

```python
class ExecuteIdapythonRequest(IdaTargetRequest):
    code: str | None = None
    script_path: str | None = None
    capture_output: bool = True
    timeout_seconds: int = Field(default=30, ge=1, le=600)
```

Validation rule: require exactly one of `code` or `script_path`.

## Response model plan

Use `dict[str, Any]` response payloads at first instead of large nested response models.

Reasons:

- `ida-script-mcp-main` already defines and tests the transport payloads.
- IDA plugin responses contain many fields that may evolve.
- GPT Actions can handle object responses well as long as the request schema is strict and descriptions are concise.

Later, add typed response models only for fields GPT frequently needs, such as:

- `instance_id`
- `port`
- `database`
- `functions`
- `pseudocode`
- `xrefs`
- `status`
- `stdout`
- `stderr`
- `error`

## Implementation steps

### Phase 1: Submodule and docs

1. Add `.gitmodules` and submodule at `external/ida-script-mcp-main`.
2. Document install command:
   ```powershell
   py -3 -m pip install -e external/ida-script-mcp-main
   ```
3. Point `SKILL_TEMPLE_SKILLS_DIR` to:
   ```text
   external/ida-script-mcp-main/src/ida_script_mcp/resources
   ```
4. Confirm Skill Temple can load `idapython` from the submodule resources.

### Phase 2: IDA Action router

1. Add `src/skill_temple/ida_actions.py`.
2. Lazy-import `ida_script_mcp.server` inside a helper.
3. Add async wrappers around:
   - `list_ida_instances`
   - `get_ida_database_info`
   - `list_functions`
   - `decompile_function`
   - `get_xrefs`
   - `execute_idapython`
4. Convert GPT request models into the MCP server input models.
5. Return structured setup errors if the submodule package is not installed.

### Phase 3: Register endpoints in FastAPI

In `src/skill_temple/app.py`, register the IDA router when creating the app.

Every route uses:

```python
openapi_extra={"x-openai-isConsequential": False}
```

Keep descriptions concise and under 300 characters.

### Phase 4: Tests

Add unit tests without requiring IDA Pro:

1. Monkeypatch `ida_script_mcp.server.list_instances` to return fake instances.
2. Monkeypatch `ida_script_mcp.server.make_ida_request` to return fake metadata/functions/decompile/xrefs results.
3. Test each `/v1/ida/*` endpoint through `TestClient`.
4. Test OpenAPI includes all expected IDA operation IDs.
5. Test every public operation has:
   ```python
   operation.get("x-openai-isConsequential") is False
   len(operation.get("description", "")) <= 300
   ```
6. Test missing `ida_script_mcp` dependency returns a setup error instead of crashing app startup.

### Phase 5: Local smoke test with IDA

1. Install and start the IDA plugin.
2. Confirm plugin prints a local port such as `127.0.0.1:13338`.
3. Start the GPT Actions gateway:
   ```powershell
   py -3 -m skill_temple.app --host 127.0.0.1 --port 8001
   ```
4. Test locally:
   ```powershell
   Invoke-RestMethod http://127.0.0.1:8001/v1/ida/instances -Method Post -ContentType 'application/json' -Body '{}'
   ```
5. Test through Caddy:
   ```powershell
   Invoke-RestMethod https://gptaction.casacam.net/skills/v1/ida/instances -Method Post -ContentType 'application/json' -Body '{}'
   ```
6. Import:
   ```text
   https://gptaction.casacam.net/skills/openapi.json
   ```
   into the Custom GPT Action.

## Expected GPT workflow

For an IDA task, the Custom GPT should usually call tools in this order:

```text
1. retrieveSkillContext(query, hinted_skill_ids=["idapython"])
2. listIdaInstances({})
3. getIdaDatabaseInfo({instance_id})
4. listIdaFunctions / decompileIdaFunction / getIdaXrefs
5. executeIdapython only when structured tools are insufficient
```

Because this is personal-use automation, all operations are marked non-consequential in OpenAPI. The prompt instructions should still tell the GPT to prefer read-only operations first and use `executeIdapython` deliberately.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| `executeIdapython` can modify IDB state. | Keep IDA plugin localhost-only; rely on personal-use GPT and prompt discipline. |
| Multiple IDA instances are open. | Require `listIdaInstances` first and pass `instance_id` in follow-up calls. |
| Submodule package not installed. | Lazy import and structured setup error with install command. |
| GPT Action import rejects schema. | Keep descriptions <= 300 chars and every operation has `x-openai-isConsequential: false`. |
| Caddy prefix mismatch. | Set `SKILL_TEMPLE_SERVER_URL=https://gptaction.casacam.net/skills`; keep FastAPI internal paths unprefixed. |
| Full submodule root is not a valid skills directory. | Use `external/ida-script-mcp-main/src/ida_script_mcp/resources` as `SKILL_TEMPLE_SKILLS_DIR`. |

## Acceptance criteria

The integration is ready when:

1. `git submodule update --init --recursive` restores `external/ida-script-mcp-main`.
2. `py -3 -m pip install -e external/ida-script-mcp-main` makes `ida_script_mcp` importable.
3. `skill-temple` loads `idapython` from the submodule resources.
4. `/openapi.json` imports into Custom GPT Actions without schema errors.
5. All public operations publish `x-openai-isConsequential: false`.
6. `/v1/ida/instances` works without IDA by returning a useful empty/no-instance message.
7. With IDA plugin running, the GPT can list instances, inspect database info, list functions, decompile functions, get xrefs, and execute IDAPython.
