# Skill Temple

Skill Temple provides Codex-style model-driven skill selection adapted to GPT Actions.
It lets a Custom GPT inspect a bounded catalog, select skills by stable handles, load the
selected `SKILL.md` instructions, and progressively read only the referenced resources
needed for the task. Unlike Codex's pre-turn context injection, this adapter uses a
two-call Action flow because a Custom GPT cannot receive a dynamic server catalog in its
static Instructions.

The intended GPT-5.6 Sol flow is:

```text
user task
  -> retrieveSkillContext
  -> read every returned SKILL.md completely
  -> follow the selected SKILL.md routing instructions
  -> readSkillContent for exact referenced resources
  -> searchSkillDocs only when no exact resource path is known
  -> call live IDA Actions when current IDB evidence is required
```

## Skill contract

Each skill requires one entrypoint:

```text
skills/
  idapython/
    SKILL.md
    docs/
      idautils.md
      ida_hexrays.md
```

`SKILL.md` must start with YAML frontmatter:

```yaml
---
name: idapython
description: Use for IDAPython scripting, live IDA analysis, Hex-Rays, functions, xrefs, types, patches, and IDB automation. 中文：用于 IDA/IDAPython 脚本、反编译、交叉引用和 IDB 自动化。
---
```

The frontmatter `name` becomes the stable `skill_id` selection handle; `name` and
`description` are required. As in Codex, the model decides whether a description clearly
applies. The server does not rank descriptions or expand aliases and keywords. Use a
bilingual description when the Custom GPT serves Chinese and English users. No
`skill.json` or separate index file is required.

The body of `SKILL.md` should:

- define the task-specific workflow and constraints;
- point directly to relevant relative resources such as `docs/ida_hexrays.md`;
- tell the model when each resource is needed;
- avoid duplicating the detailed reference material;
- define completion and failure behavior that is not already obvious from the Action schema.

The runtime returns the selected `SKILL.md` within the response budget and provides
continuation metadata when it is truncated. It does not automatically inject all files
under the skill directory. The model follows the paths named by the selected skill and
reads only the resources required for the current task.

## GPT Action API

The public OpenAPI schema exposes:

| Operation | Method | Path | Purpose |
| --- | --- | --- | --- |
| `retrieveSkillContext` | `POST` | `/v1/skills/retrieve` | Return the skill catalog and load explicitly selected `SKILL.md` entrypoints. |
| `readSkillContent` | `POST` | `/v1/skills/read` | Read an exact resource path referenced by a selected skill. |
| `searchSkillDocs` | `POST` | `/v1/skills/search` | Fallback keyword search inside one selected skill. |
| `listIdaInstances` | `POST` | `/v1/ida/instances` | List running local IDA plugin instances. |
| `getIdaDatabaseInfo` | `POST` | `/v1/ida/database-info` | Confirm the selected database and architecture. |
| `listIdaFunctions` | `POST` | `/v1/ida/functions` | Read functions from the selected IDB. |
| `decompileIdaFunction` | `POST` | `/v1/ida/decompile` | Decompile a function by name or address. |
| `getIdaXrefs` | `POST` | `/v1/ida/xrefs` | Read incoming or outgoing cross-references. |
| `executeIdapython` | `POST` | `/v1/ida/execute` | Execute custom IDAPython in the selected IDA instance. |
| `workspaceCommand` | `POST` | `/v1/workspace/command` | Start or manage asynchronous PowerShell 7 commands in `WORKSPACE_ROOT`. |
| `workspaceInspect` | `POST` | `/v1/workspace/inspect` | Inspect a directory tree, search matches, and related file snippets. |
| `workspaceSearch` | `POST` | `/v1/workspace/search` | Search text with ripgrep. |
| `workspaceReadFiles` | `POST` | `/v1/workspace/read-files` | Read multiple UTF-8 files with line numbers and hashes. |
| `workspaceWriteFile` | `POST` | `/v1/workspace/write-file` | Create or overwrite one UTF-8 text file. |
| `workspaceApplyPatch` | `POST` | `/v1/workspace/apply-patch` | Apply a Codex `*** Begin Patch` text patch. |

All public operations publish `x-openai-isConsequential: false` for this trusted personal gateway.

### Select a skill

Without an explicit selection, the first call returns a bounded `available_skills`
catalog. Each item contains a `skill_id` selection handle plus name, description,
entrypoint metadata, and a cached content hash. The catalog uses a 20,000-character
budget and reports:

```text
available_skill_count
included_skill_count
omitted_skill_count
descriptions_truncated
catalog_char_limit
catalog_included
```

If descriptions were shortened or entries were omitted, the model must not interpret
the visible list as the complete installed set. If exactly one visible description
clearly matches, it retries once with the exact `skill_id`:

```json
{
  "query": "反编译 main 函数",
  "hinted_skill_ids": ["idapython"],
  "allow_skill_chaining": false
}
```

Codex-style `$idapython` mentions are accepted. The gateway additionally supports
`@idapython` as a convenience extension. Unknown explicit mentions are returned in
`unknown_skill_mentions`; they are never treated like an ordinary unselected task. The
runtime does not silently select a skill from fuzzy keyword overlap.

After one or more skills are loaded, the response omits the repeated catalog by default
and returns `catalog_included=false`. This keeps the selected Skill instructions and the
Action response within the platform boundary.

A selected skill packet contains:

```json
{
  "skill_id": "idapython",
  "name": "idapython",
  "description": "Use for IDAPython scripting...",
  "role": "primary",
  "source_path": "SKILL.md",
  "instructions": "---\nname: idapython\n...",
  "content_hash": "sha256:...",
  "total_lines": 52,
  "truncated": false,
  "next_start_line": null,
  "referenced_paths": ["docs/idautils.md", "docs/ida_hexrays.md"]
}
```

`instructions` contains the selected `SKILL.md` within the response budget. When
`truncated=true`, continue the same `SKILL.md` through `readSkillContent` from
`next_start_line`. Do not call `retrieveSkillContext` again just to continue it.

### Read a referenced resource

```json
{
  "skill_id": "idapython",
  "path": "docs/ida_hexrays.md",
  "start_line": 1,
  "max_lines": 200
}
```

When `truncated=true`, continue from `next_start_line` until the selected resource has been read completely.

### Search as a fallback

```json
{
  "skill_id": "idapython",
  "query": "ctree_visitor_t cot_call",
  "limit": 5
}
```

Use search only when the selected `SKILL.md` does not identify an exact relevant resource. Search uses SQLite FTS5 with symbol, path, heading, and API-name boosts.

## Local workspace tools

The six `/v1/workspace/*` Actions are a local-folder port of the corresponding
`github-gpt-actions-gateway` workspace tools. They do not clone repositories or use
branches, commits, pushes, PRs, or CI. Every operation uses the single folder configured
by `WORKSPACE_ROOT`.

`workspaceInspect`, `workspaceSearch`, and `workspaceReadFiles` retain the gateway's
bounded response, line-number, hash, context, and truncation behavior. Search and inspect
require `rg` on `PATH`.

`workspaceWriteFile` supports `create_only`, `overwrite`, and
`overwrite_if_sha256_matches`, plus UTF-8 line-ending conversion and `dry_run`.
`workspaceApplyPatch` accepts the gateway/Codex patch format:

```text
*** Begin Patch
*** Update File: notes.txt
@@
-old
+new
*** Add File: added.txt
+content
*** End Patch
```

Deletion requires `allow_delete=true`. Write and patch dry-runs calculate changes entirely
in memory and never create, replace, delete, or restore files. A real multi-file patch is
fully parsed and validated in memory first; new contents are staged before commit, and
per-file backups are used to roll back a commit error.

`workspaceCommand` keeps the source gateway's asynchronous lifecycle:

```text
start -> get / logs -> terminal state
                   -> cancel
list
```

`start` requires a unique `idempotency_key` and a PowerShell script. The command runs
with PowerShell 7 in `WORKSPACE_ROOT`. State and logs are stored outside the target folder
under `WORKSPACE_OPERATION_ROOT` or `.runtime/workspace-operations`. `logs` supports
independent stdout/stderr byte offsets. Terminal states are `succeeded`, `failed`,
`timed_out`, `canceled`, and `interrupted`.

## Multiple skills

Multiple exact hints or explicit mentions automatically load together; callers do not
need to remember `allow_skill_chaining=true`. The field remains accepted for backward
compatibility. Up to three explicitly selected skills are loaded with `primary` and
`secondary` roles.

If more than three skills are explicitly selected, the runtime loads none of them and
returns `next_action=retryWithFewerSkills`, the full `explicit_skill_ids`, and
`omitted_explicit_skill_ids`. It never partially executes a larger explicit selection.

Skill entrypoints share a 60,000-character response budget. A single selected
`SKILL.md` receives at most 24,000 characters; multi-skill responses divide the
remaining budget dynamically. Truncated entrypoints expose `next_start_line` for
continued reading through `readSkillContent`.

For every selected skill:

1. read its returned `instructions` completely;
2. use that packet's explicit `skill_id` for later reads;
3. load only resources that contribute to the current task;
4. do not apply one skill's references or rules to another skill.

## IDA integration

The deployed architecture is:

```text
Custom GPT / GPT-5.6 Sol
        |
        | HTTPS + Bearer token
        v
ida_skill FastAPI gateway
127.0.0.1:8001
        |
        | local HTTP
        v
IDA-Script-MCP plugin
127.0.0.1:13338+
```

The Custom GPT calls only the FastAPI/OpenAPI surface. It does not call the MCP transport or the raw IDA plugin port directly.

For current IDB facts, use live IDA Actions rather than documentation or assumptions. `executeIdapython` is available for custom analysis, bulk processing, renaming, comments, patches, type changes, and validation. The GPT Action adapter accepts `timeout_seconds` from 1 to 35 seconds so the plugin timeout plus its 5-second response margin remains within the Action round trip. Inspect `status`, `stdout`, `stderr`, `result`, and `error` before reporting success. Keep mutations within the user's requested scope and perform a targeted read-back when the execution response alone does not prove the change.

Every public IDA Action response is capped at 80,000 serialized JSON characters.
Instance, function, and xref lists are reduced to a fitting page and return
`truncated` plus `next_offset`. The xref adapter supports offsets inside its
5,000-item window and never repeats the current offset when an oversized final item
cannot be returned. Database metadata uses the same hard response fallback. Decompile responses
report `pseudocode_truncated` and `disassembly_truncated`; execution responses report
`stdout_truncated`, `stderr_truncated`, `result_truncated`, and `error_truncated` when
the corresponding field is shortened.

## Configuration

Copy `.env.example` to `.env` and replace the placeholders:

```dotenv
SKILL_TEMPLE_SERVER_URL=https://gptaction.casacam.net/skills
SKILL_TEMPLE_SKILLS_DIR=C:/Users/Administrator/Desktop/ida_skill/external/ida-script-gptaction-version/src/ida_script_mcp/resources
SKILL_TEMPLE_BEARER_TOKEN=replace-with-a-long-random-secret
IDA_SCRIPT_MCP_HOST=127.0.0.1
# IDA_SCRIPT_MCP_PORT=13338
WORKSPACE_ROOT=C:/path/to/workspace
WORKSPACE_PWSH_PATH=pwsh
# WORKSPACE_OPERATION_ROOT=C:/path/to/ida_skill/.runtime/workspace-operations
# WORKSPACE_ALLOW_NETWORK=false
```

`.env` is ignored by Git. Only `.env.example` is tracked.

When `SKILL_TEMPLE_BEARER_TOKEN` is set, `/v1/*` and `/console/retrieve` require:

```text
Authorization: Bearer <token>
```

`/openapi.json`, `/health`, and the static `/console` page remain readable so the schema can be imported and the console can load. Configure Custom GPT Actions authentication as an API key sent with the Bearer scheme.

## Install and run

```powershell
git submodule update --init --recursive
py -3 -m pip install -e external/ida-script-gptaction-version
py -3 -m pip install -e .[dev]
skill-temple --host 127.0.0.1 --port 8001
```

### Install the IDA plugin with IDA 8.3's bundled Python

When IDA 8.3 is installed at `C:\Users\Administrator\Desktop\ida 8.3` and this repository is located at `C:\Users\Administrator\Desktop\ida_skill`, run the following commands in PowerShell:

```powershell
& "C:\Users\Administrator\Desktop\ida 8.3\python311\python.exe" `
  -m pip install -e "C:\Users\Administrator\Desktop\ida_skill\external\ida-script-gptaction-version"

& "C:\Users\Administrator\Desktop\ida 8.3\python311\python.exe" `
  -m ida_script_mcp.installer install
```

Restart IDA after installation. Open an IDB, then start the plugin from `Edit -> Plugins -> IDA-Script-MCP` or press `Ctrl+Alt+S`.

The submodule resource directory contains the production `idapython/SKILL.md` and its progressively disclosed docs.

OpenAPI:

```text
http://127.0.0.1:8001/openapi.json
```

Health:

```text
http://127.0.0.1:8001/health
```

Debug console:

```text
http://127.0.0.1:8001/console
```

## Console

The hidden development console supports:

- a Bearer token stored only in `sessionStorage`;
- redacted request headers;
- manual calls for all public GPT Action operations;
- request start, waiting, response status, headers, duration, parsed JSON, and raw non-JSON response tracing.

The console is not part of the public GPT Action schema.

## Custom GPT instructions

Use [`GPT_ACTION_PROMPT.md`](GPT_ACTION_PROMPT.md) as the Custom GPT Instructions prompt for GPT-5.6 Sol. It contains only global skill and Action routing rules; IDAPython behavior remains in the selected `idapython/SKILL.md`.

## Validation

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
py -3 -m ruff check --exclude external .
py -3 -m pytest
```

The full repository lint command excludes `external/` because the submodule has its own independent lint configuration and existing upstream findings.
