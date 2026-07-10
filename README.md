# Skill Temple

Skill Temple is a Codex-style `SKILL.md` runtime exposed as GPT Actions. It lets a Custom GPT discover a small set of local skills, load the selected skill instructions in full, and then progressively read only the referenced resources needed for the task.

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
description: Use for IDAPython scripting, live IDA analysis, Hex-Rays, functions, xrefs, types, patches, and IDB automation.
---
```

The frontmatter `name` is the stable `skill_id`; `name` and `description` are the only discovery metadata. No `skill.json` or separate index file is required.

The body of `SKILL.md` should:

- define the task-specific workflow and constraints;
- point directly to relevant relative resources such as `docs/ida_hexrays.md`;
- tell the model when each resource is needed;
- avoid duplicating the detailed reference material;
- define completion and failure behavior that is not already obvious from the Action schema.

The runtime returns the selected `SKILL.md` in full. It does not automatically inject all files under the skill directory. The model follows the paths named by the selected skill and reads only the resources required for the current task.

## GPT Action API

The public OpenAPI schema exposes:

| Operation | Method | Path | Purpose |
| --- | --- | --- | --- |
| `retrieveSkillContext` | `POST` | `/v1/skills/retrieve` | Select matching skills and return each selected `SKILL.md` in full. |
| `readSkillContent` | `POST` | `/v1/skills/read` | Read an exact resource path referenced by a selected skill. |
| `searchSkillDocs` | `POST` | `/v1/skills/search` | Fallback keyword search inside one selected skill. |
| `listIdaInstances` | `POST` | `/v1/ida/instances` | List running local IDA plugin instances. |
| `getIdaDatabaseInfo` | `POST` | `/v1/ida/database-info` | Confirm the selected database and architecture. |
| `listIdaFunctions` | `POST` | `/v1/ida/functions` | Read functions from the selected IDB. |
| `decompileIdaFunction` | `POST` | `/v1/ida/decompile` | Decompile a function by name or address. |
| `getIdaXrefs` | `POST` | `/v1/ida/xrefs` | Read incoming or outgoing cross-references. |
| `executeIdapython` | `POST` | `/v1/ida/execute` | Execute custom IDAPython in the selected IDA instance. |

All public operations publish `x-openai-isConsequential: false` for this trusted personal gateway.

### Select a skill

```json
{
  "query": "@idapython walk ctree calls with ctree_visitor_t",
  "hinted_skill_ids": ["idapython"],
  "allow_skill_chaining": false
}
```

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

## Multiple skills

Set `allow_skill_chaining=true` only when the task clearly needs multiple domains. The runtime returns up to three top matching skills with `primary` and `secondary` roles.

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

## Configuration

Copy `.env.example` to `.env` and replace the placeholders:

```dotenv
SKILL_TEMPLE_SERVER_URL=https://gptaction.casacam.net/skills
SKILL_TEMPLE_SKILLS_DIR=C:/Users/Administrator/Desktop/ida_skill/external/ida-script-mcp-main/src/ida_script_mcp/resources
SKILL_TEMPLE_BEARER_TOKEN=replace-with-a-long-random-secret
IDA_SCRIPT_MCP_HOST=127.0.0.1
# IDA_SCRIPT_MCP_PORT=13338
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
py -3 -m pip install -e external/ida-script-mcp-main
py -3 -m pip install -e .[dev]
skill-temple --host 127.0.0.1 --port 8001
```

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
