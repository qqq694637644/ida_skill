# Console API Trace Implementation Plan

## Goal

Make `/console` useful for debugging the public gateway without opening browser DevTools. The page should show each API call in real time: request start, URL, method, redacted headers, request body, waiting state, response status, response headers, JSON parse success/failure, raw non-JSON text, errors, and total duration.

This is a browser-side trace only. It does not change the GPT Action OpenAPI schema and does not add backend SSE or streaming in the first version.

## Scope

Implement in the existing hidden `/console` page:

1. A Bearer token input stored in `sessionStorage`.
2. An API Call Timeline panel.
3. A shared `apiCall()` wrapper around `fetch()`.
4. Retrieval button tracing `/console/retrieve`.
5. Manual operation runner for the main GPT Action endpoints:
   - `retrieveSkillContext` -> `/v1/skills/retrieve`
   - `searchSkillDocs` -> `/v1/skills/search`
   - `readSkillContent` -> `/v1/skills/read`
   - `listIdaInstances` -> `/v1/ida/instances`
   - `getIdaDatabaseInfo` -> `/v1/ida/database-info`
   - `listIdaFunctions` -> `/v1/ida/functions`
   - `decompileIdaFunction` -> `/v1/ida/decompile`
   - `getIdaXrefs` -> `/v1/ida/xrefs`
   - `executeIdapython` -> `/v1/ida/execute`

## Token behavior

When the user enters a token, calls include:

```text
Authorization: Bearer <token>
```

The timeline must never print the full token. It should display:

```text
Authorization: Bearer ***redacted***
```

The token is stored only in `sessionStorage`, not in localStorage and not in the server.

## Request tracing behavior

For each call, append timeline events in order:

1. `request start` with operation label, method, URL, redacted headers, and JSON body.
2. `waiting response` immediately before awaiting the network call.
3. `response received` with status, content type, and elapsed milliseconds.
4. `response headers` with a small header object.
5. `parsed json` when JSON parsing succeeds.
6. `non-json response` with raw text when parsing fails.
7. `request failed` if fetch or parsing throws unexpectedly.

The main Result pane should show the parsed JSON when available. For non-JSON responses it should show a clear parse diagnostic and raw body text so reverse-proxy fallback responses such as `OK: use /skills` are obvious.

## UI layout

Keep the existing quick retrieval form, then add:

- Bearer Token controls: token input, save button, clear button.
- API Operation runner: operation select, JSON body textarea, Run Operation button.
- API Call Timeline: preformatted append-only log and Clear Timeline button.

## Backend changes

No new backend route is required. The existing `/console` HTML changes only.

The existing optional Bearer middleware remains unchanged:

- `/openapi.json`, `/health`, and static `/console` stay readable.
- `/console/retrieve` and `/v1/*` require Bearer auth when `SKILL_TEMPLE_BEARER_TOKEN` is set.

## Tests

Update existing console tests to assert the HTML contains:

- `API Call Timeline`
- `Bearer Token`
- `apiCall`
- `sessionStorage`
- `Authorization`
- `***redacted***`
- `executeIdapython`

Also assert the old direct call pattern is gone:

```text
fetch('/console/retrieve'
```

Run:

```powershell
PYTHONPATH=src py -3 -m ruff check --exclude external .
PYTHONPATH=src py -3 -m pytest
```

## Non-goals for first version

- No SSE.
- No server-side trace IDs.
- No backend streaming from IDA plugin calls.
- No persistence beyond `sessionStorage`.
