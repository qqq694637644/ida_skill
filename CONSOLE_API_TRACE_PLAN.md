# 控制台 API 调用追踪实现计划

## 目标

让 `/console` 可以直接用于调试公网网关，不必每次打开浏览器 DevTools。页面需要实时展示每一次 API 调用的关键过程：请求开始、URL、method、脱敏后的 headers、请求 body、等待状态、响应状态码、响应 headers、JSON 解析成功/失败、非 JSON 原始文本、异常信息和总耗时。

第一版只做浏览器侧追踪。它不修改 GPT Action OpenAPI schema，也不新增后端 SSE 或 streaming。

## 范围

在现有隐藏页面 `/console` 中实现：

1. Bearer token 输入框，并把 token 保存到 `sessionStorage`。
2. API Call Timeline 调用时间线面板。
3. 统一的 `apiCall()` 包装函数，用它包住 `fetch()`。
4. Retrieve 按钮调用 `/console/retrieve` 时进入时间线追踪。
5. 手动 Operation 调用器，用于测试主要 GPT Action endpoints：
   - `retrieveSkillContext` -> `/v1/skills/retrieve`
   - `searchSkillDocs` -> `/v1/skills/search`
   - `readSkillContent` -> `/v1/skills/read`
   - `listIdaInstances` -> `/v1/ida/instances`
   - `getIdaDatabaseInfo` -> `/v1/ida/database-info`
   - `listIdaFunctions` -> `/v1/ida/functions`
   - `decompileIdaFunction` -> `/v1/ida/decompile`
   - `getIdaXrefs` -> `/v1/ida/xrefs`
   - `executeIdapython` -> `/v1/ida/execute`

## Token 行为

用户输入 token 后，控制台请求自动带上：

```text
Authorization: Bearer <token>
```

时间线绝不能打印完整 token，只能显示：

```text
Authorization: Bearer ***redacted***
```

Token 只保存到 `sessionStorage`，不保存到 `localStorage`，也不发给后端保存。

## 请求追踪行为

每次调用按顺序追加这些时间线事件：

1. `request start`：显示 operation label、method、URL、脱敏 headers 和 JSON body。
2. `waiting response`：在等待网络请求返回前立即显示。
3. `response received`：显示 status、content type 和耗时毫秒数。
4. `response headers`：显示一个简化 header 对象。
5. `parsed json`：JSON 解析成功时显示。
6. `non-json response`：JSON 解析失败时显示原始文本。
7. `request failed`：`fetch` 或其它异常失败时显示。

主 Result 面板优先显示解析后的 JSON。非 JSON 响应要显示清楚的解析诊断和原始 body，这样遇到反代 fallback 响应，例如：

```text
OK: use /skills
```

页面里能直接看到原因，而不是只看到 JSON parse error。

## UI 布局

保留现有 quick retrieval 表单，然后新增：

- Bearer Token 控件：token 输入框、保存按钮、清除按钮。
- API Operation runner：operation 下拉框、JSON body textarea、Run Operation 按钮。
- API Call Timeline：追加式 pre 日志面板和 Clear Timeline 按钮。

## 后端改动

第一版不需要新增后端 route，只修改现有 `/console` 内嵌 HTML/JS。

现有可选 Bearer middleware 保持不变：

- `/openapi.json`、`/health`、静态 `/console` 页面保持可读。
- 设置 `SKILL_TEMPLE_BEARER_TOKEN` 后，`/console/retrieve` 和 `/v1/*` 需要 Bearer auth。

## 测试

更新现有 console 测试，断言 HTML 包含：

- `API Call Timeline`
- `Bearer Token`
- `apiCall`
- `sessionStorage`
- `Authorization`
- `***redacted***`
- `executeIdapython`

同时断言旧的直接调用模式已经移除：

```text
fetch('/console/retrieve'
```

运行：

```powershell
PYTHONPATH=src py -3 -m ruff check --exclude external .
PYTHONPATH=src py -3 -m pytest
```

## 第一版不做的内容

- 不做 SSE。
- 不做服务端 trace id。
- 不做 IDA 插件调用过程的后端 streaming。
- 不做超过 `sessionStorage` 的持久化。
