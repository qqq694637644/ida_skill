# 本地工作目录工具实现计划

## 目标

在 `ida_skill` 现有 FastAPI / OpenAPI 服务中增加六个本地文件夹工具，能力参考 `github-gpt-actions-gateway` 中对应的 workspace 工具，但不引入 Git、GitHub 仓库、分支、提交、PR、CI 或 workspace 克隆逻辑。

所有工具只操作 `.env` 中配置的一个固定根目录。该项目用于个人环境，因此不额外实现路径越界、绝对路径、`..`、符号链接或 junction 的安全限制。

## 配置

在 `.env.example` 中新增：

```env
WORKSPACE_ROOT=D:\path\to\target-folder
```

运行时从环境变量或项目根目录 `.env` 读取 `WORKSPACE_ROOT`。

要求：

- 服务启动时解析为 `Path`。
- 未配置、路径不存在或不是目录时，返回明确的配置错误。
- 所有相对文件路径均以 `WORKSPACE_ROOT` 为基准。
- `workspaceCommand` 的 PowerShell 工作目录固定为 `WORKSPACE_ROOT`。

## API 设计

建议新增模块：

```text
src/skill_temple/workspace_actions.py
```

由 `src/skill_temple/app.py` 中的 `create_app()` 调用 `register_workspace_actions(app)` 注册路由。

对外接口继续使用工具名称作为 `operationId`，并保持 GPT Actions 兼容：

- 请求模型使用 Pydantic 严格模型。
- 每个 operation 的描述控制在 300 字符以内。
- 每个 operation 设置 `x-openai-isConsequential: false`。
- 返回值使用结构化 JSON，不把异常堆栈直接暴露给调用方。

建议路由：

| 工具 | Method | Path |
| --- | --- | --- |
| `workspaceCommand` | POST | `/v1/workspace/command` |
| `workspaceInspect` | POST | `/v1/workspace/inspect` |
| `workspaceSearch` | POST | `/v1/workspace/search` |
| `workspaceReadFiles` | POST | `/v1/workspace/read-files` |
| `workspaceWriteFile` | POST | `/v1/workspace/write-file` |
| `workspaceApplyPatch` | POST | `/v1/workspace/apply-patch` |

不保留 URL 中的 `owner`、`repo` 或 `workspace_id`，因为服务只绑定一个本地目录。

## 功能计划

### 1. workspaceInspect

用途：一次请求查看目录结构、搜索关键词并读取少量相关文件内容。

建议请求字段：

- `paths`: 可选，需要重点检查的文件或目录。
- `queries`: 可选，文本搜索关键词。
- `max_depth`: 目录树最大深度。
- `max_tree_entries`: 最大目录树条目数。
- `context_lines`: 搜索结果上下文行数。
- `max_search_matches`: 最大搜索匹配数。
- `max_read_files`: 最大读取文件数。
- `max_file_lines`: 单文件最大行数。
- `max_bytes_per_file`: 单文件最大返回字节数。
- `max_bytes`: 整体响应大小限制。

实现内容：

- 使用 `pathlib` 遍历目录树。
- 搜索优先调用 `rg`，不可用时返回明确错误或使用 Python 文本搜索作为后备。
- 对 `paths` 中的文本文件按限制读取。
- 返回 `tree`、`searches`、`files`、`truncated`。

### 2. workspaceSearch

用途：在固定目录中使用 ripgrep 搜索文本。

建议请求字段：

- `query`
- `regex`
- `case_sensitive`
- `paths`
- `context_lines`
- `max_matches`
- `max_bytes`

实现内容：

- 使用 `rg --json` 或稳定的结构化参数执行搜索。
- 支持普通文本和正则表达式。
- 支持指定子路径。
- 返回文件路径、行号、列号、匹配行和上下文。
- 明确区分无匹配、命令错误和输出截断。

### 3. workspaceReadFiles

用途：批量读取 UTF-8 文本文件并附带行号。

建议请求字段：

- `paths`
- `start_line`
- `max_lines`
- `max_bytes_per_file`
- `max_bytes`

实现内容：

- 从 `WORKSPACE_ROOT` 拼接请求路径。
- 按 UTF-8 读取文本。
- 返回每个文件的路径、内容、起止行、是否截断和可选的下一起始行。
- 单个文件失败不影响其他文件读取，错误写入对应文件结果。

### 4. workspaceWriteFile

用途：创建或完整覆盖一个 UTF-8 文本文件。

建议请求字段：

- `path`
- `content`
- `mode`: `create_only`、`overwrite`、`overwrite_if_sha256_matches`
- `encoding`
- `line_ending`: `preserve`、`lf`、`crlf`
- `expected_sha256`
- `dry_run`
- `max_bytes`

实现内容：

- 必要时创建父目录。
- `create_only` 在文件已存在时失败。
- `overwrite` 直接替换。
- `overwrite_if_sha256_matches` 在当前文件哈希匹配后替换。
- 写入前处理换行格式。
- 优先使用临时文件加原子替换，降低半写入风险。
- 返回写入字节数、最终 SHA-256、是否创建或覆盖。

### 5. workspaceApplyPatch

用途：应用受控的文本补丁，支持一次修改多个文件。

建议请求字段：

- `patch`
- `dry_run`
- `allow_delete`
- `max_changed_files`
- `max_patch_bytes`

实现内容：

- 复用或移植 `github-gpt-actions-gateway` 的 unified diff 解析和应用逻辑。
- 支持新增、修改和按配置删除文件。
- `dry_run` 只验证补丁并返回预期变更，不写入文件。
- 失败时返回具体文件和失败 hunk，不留下部分修改。
- 正式应用前先在内存中完成全部计算，再统一写入。

### 6. workspaceCommand

用途：在固定根目录中执行 PowerShell 7 命令。

保留异步 operation 模型：

```text
start -> get / logs -> terminal state
                  -> cancel
list
```

建议请求字段：

- `action`: `start`、`get`、`logs`、`cancel`、`list`
- `idempotency_key`: `start` 时必填。
- `script`: `start` 时必填。
- `timeout_seconds`
- `max_output_bytes`
- `allow_network`
- `plain_output`
- `utf8_output`
- `operation_id`: `get`、`logs`、`cancel` 时必填。
- `stdout_offset`
- `stderr_offset`
- `max_bytes`
- `state`: `list` 的可选过滤条件。

实现内容：

- 使用 `pwsh` 或配置项指定的 PowerShell 7 可执行文件。
- `cwd` 固定为 `WORKSPACE_ROOT`。
- 每次 start 创建 operation ID，并根据 `idempotency_key` 去重。
- 分别保存 stdout、stderr、状态、开始时间、结束时间和退出码。
- 支持增量日志 offset。
- 支持超时后终止完整进程树。
- 支持主动 cancel。
- operation 状态至少包含：`running`、`succeeded`、`failed`、`timed_out`、`canceled`、`interrupted`。

建议把 operation 元数据和日志放在服务自己的运行目录，例如：

```text
.runtime/workspace-operations/
```

不要把运行记录写进 `WORKSPACE_ROOT`，避免污染用户目标文件夹。

## 模块拆分

建议保持实现可测试，避免全部堆在路由文件中：

```text
src/skill_temple/workspace_actions.py
src/skill_temple/workspace_files.py
src/skill_temple/workspace_patch.py
src/skill_temple/workspace_operations.py
```

职责：

- `workspace_actions.py`: Pydantic 模型、FastAPI 路由、错误转换。
- `workspace_files.py`: inspect、search、read、write。
- `workspace_patch.py`: unified diff 解析、dry-run 和应用。
- `workspace_operations.py`: PowerShell operation 生命周期、日志和取消。

如果移植后的代码量较小，也可以先合并为 `workspace_actions.py` 和 `workspace_operations.py` 两个模块，后续再拆分。

## 与原 gateway 的复用边界

可以复用：

- 请求和响应字段设计。
- inspect/search/read/write/patch 的文本处理逻辑。
- command 的 operation 状态机、日志 offset、超时和取消设计。
- 错误代码和截断语义。

应删除：

- GitHub owner/repo 参数。
- workspace ID 和 workspace metadata。
- clone、checkout、branch、commit、push、diff、PR、CI 逻辑。
- Git 仓库状态检查。
- GitHub API client 依赖。
- 原 gateway 中仅用于多租户隔离的目录安全限制。

## 测试计划

新增：

```text
tests/test_workspace_actions.py
tests/test_workspace_operations.py
```

测试使用临时目录设置 `WORKSPACE_ROOT`，不操作真实个人目录。

覆盖范围：

1. `.env` 根目录配置加载。
2. inspect 返回目录树、搜索和文件片段。
3. search 普通字符串、正则、大小写和无匹配。
4. read-files 多文件、行范围和截断。
5. write-file 三种写入模式、换行和 SHA-256 校验。
6. apply-patch 新增、修改、删除、dry-run、失败回滚。
7. command start/get/logs/list/cancel。
8. command stdout/stderr offset。
9. command 成功、失败、超时和取消状态。
10. OpenAPI operationId、请求 schema 和 `x-openai-isConsequential`。
11. Bearer middleware 对新 `/v1/workspace/*` 路由继续生效。

## 文档更新

实现完成时同步更新：

- `.env.example`: 增加 `WORKSPACE_ROOT`，可选增加 `WORKSPACE_PWSH_PATH`。
- `README.md`: 增加六个工具的接口表、配置和调用示例。
- `GPT_ACTION_PROMPT.md`: 告诉 GPT 何时使用 inspect/search/read/write/patch/command。
- OpenAPI 相关测试：把六个 operationId 纳入预期集合。

## 实施顺序

### Phase 1：配置和只读能力

1. 增加 `WORKSPACE_ROOT` 配置加载。
2. 实现 `workspaceReadFiles`。
3. 实现 `workspaceSearch`。
4. 实现 `workspaceInspect`。
5. 添加只读接口测试和 OpenAPI 测试。

### Phase 2：文本编辑能力

1. 实现 `workspaceWriteFile`。
2. 移植 `workspaceApplyPatch`。
3. 添加写入模式、补丁和失败回滚测试。

### Phase 3：PowerShell operation

1. 移植 operation 状态模型。
2. 实现 `pwsh 7` 启动和日志持久化。
3. 实现 get/logs/list/cancel。
4. 实现超时和进程树终止。
5. 添加异步命令生命周期测试。

### Phase 4：集成和文档

1. 在 `create_app()` 注册 workspace 路由。
2. 更新 `.env.example`、README 和 GPT Action prompt。
3. 运行完整测试。
4. 本机使用真实目标目录做一次 smoke test。
5. 验证 `/openapi.json` 可被 Custom GPT Actions 正常导入。

## 验收标准

1. `.env` 中只需配置一个 `WORKSPACE_ROOT` 即可启用工具。
2. 六个 operation 均出现在 `/openapi.json`。
3. inspect、search 和 read 能读取目标目录内容。
4. write 和 apply-patch 能修改目标目录文件。
5. command 能在目标目录中启动 `pwsh 7`，并完整支持 start/get/logs/list/cancel。
6. 不依赖 Git、GitHub API、仓库 URL、branch 或 workspace ID。
7. 所有新增自动化测试通过。
8. 现有 skill 和 IDA Action 测试不回归。
