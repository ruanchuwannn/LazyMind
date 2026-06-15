# skill_editor RemoteFS 改造方案

## 背景

当前 `algorithm/lazymind/chat/engine/tools/skill_editor.py` 的三类操作分别调用 core 内部接口：

- `create` 调用 `/skill/create`
- `modify` 调用 `/skill/suggestion`
- `remove` 调用 `/skill/remove`

现状问题：

- `resource_suggestions` 已废弃，`/skill/suggestion` 和 `/skill/remove` 的 suggestion 逻辑不再适合作为 `skill_editor` 的持久化路径。
- 正式 skill 数据在 core 的 `skill_resources` 表中，但不希望 algorithm 侧直接手写 `skill_resources`，避免重复维护 `content_hash`、`relative_path`、`version`、child skill 删除等业务规则。
- 现有 RemoteFS 已将 `remote://skills/{category}/{skill}/SKILL.md` 映射到 `skill_resources`，但目前只读。

目标：

- 扩展 backend RemoteFS 的写入和删除能力。
- 扩展 Python `RemoteFS`，让 algorithm 能通过 FS 语义创建/删除正式 skill。
- `skill_editor create/remove` 通过 RemoteFS 操作正式 skill，不直接操作 `skill_resources`。
- `skill_editor modify` 继续走审核流，应用 operations 后写入 `skill_review_results`。
- `modify/remove` 如果发现 `skill_review_results` 中已有 pending 记录，返回 `存在未处理的变更，请先处理`。

## 代码分层

### tools

核心工具入口仍放在：

```text
algorithm/lazymind/chat/engine/tools/skill_editor.py
```

职责：

- 参数校验与 action 分发。
- 调用 infra helper。
- 组织工具返回值。
- 不直接写 SQL，不直接拼 backend HTTP 细节。

### integrations

RemoteFS 的基础实现放在：

```text
algorithm/lazymind/chat/integrations/remote_fs.py
```

职责：

- 实现 LazyLLM FS 协议。
- 对接 backend `/remote-fs/*` API。
- 支持 `ls/info/exists/read/write/rm` 等底层 FS 能力。

### tools/infra

工具依赖的封装放在：

```text
algorithm/lazymind/chat/engine/tools/infra/
```

建议新增：

```text
algorithm/lazymind/chat/engine/tools/infra/skill_remote_store.py
```

职责：

- 基于 `RemoteFS` 封装 skill 创建、读取、删除。
- 统一构造 `remote://skills/{category}/{name}/SKILL.md` 路径。
- 复用现有 `skill_registry.py` / `skill_validation.py` / `suggestion.py` 等 infra 能力。

继续复用已有能力：

- `validate_skill_name`
- `normalize_skill_category`
- `validate_skill_content`
- `parse_skill_frontmatter`
- `list_all_skill_entries`
- `_apply_skill_edit_operations`
- `tool_success` / `tool_error`

## backend RemoteFS 扩展

现有 backend RemoteFS 文件：

```text
backend/core/skill/remote_fs.go
```

现有能力：

- `GET /remote-fs/list`
- `GET /remote-fs/info`
- `GET /remote-fs/exists`
- `GET /remote-fs/content`

需要新增写/删能力。

### 写入 SKILL.md

建议新增或扩展 endpoint：

```http
PUT /remote-fs/content?path=skills/{category}/{name}/SKILL.md&session_id=...
```

body 为完整 `SKILL.md` 内容。

语义：

- 仅允许写 `skills/{category}/{name}/SKILL.md`。
- 根据 `session_id` resolve user。
- 校验 category/name/path。
- 校验 SKILL.md 内容。
- 如果不存在，创建 parent skill。
- 如果已存在，可按用途选择：
  - create 路径：返回冲突。
  - 后续如需要直接改正式 skill，再扩展覆盖写入语义。

写入时复用 core 中已有的 skill 创建逻辑或抽公共函数，避免复制字段生成规则。

### 删除 skill 目录

建议新增 endpoint：

```http
DELETE /remote-fs/path?path=skills/{category}/{name}&recursive=true&session_id=...
```

语义：

- 仅允许删除 skill 目录 `skills/{category}/{name}`。
- 根据 `session_id` resolve user。
- 查询 parent skill。
- 参考 `DELETE /skills/{skill_id}` / `DeleteSkill` / `deleteParentSkill` 的语义：
  - 删除 parent skill。
  - 同时删除该 parent 下的 child skills。
- 不再写 `resource_suggestions`。

### remote FS 路径规则

支持的 skill 主文件路径：

```text
skills/{category}/{name}/SKILL.md
```

支持的删除目录路径：

```text
skills/{category}/{name}
```

暂不支持：

- 直接删除 `skills/{category}`。
- 写入 child skill 文件。
- 删除单个 child skill 文件。

后续如需要 child skill 管理，可在 RemoteFS 中扩展 `skills/{category}/{parent}/{child}.md` 的写删语义。

## Python RemoteFS 扩展

现有文件：

```text
algorithm/lazymind/chat/integrations/remote_fs.py
```

当前是只读实现，`mkdir/rm/write/open(w)` 都会抛 `PermissionError`。

需要扩展：

- `write_file(path, data)`
- `write(path, content)`
- `_open(path, mode='w'/'wb')`
- `rm(path, recursive=True)`

实现建议：

- 写文件时调用 backend `PUT /remote-fs/content`。
- 删除目录时调用 backend `DELETE /remote-fs/path`。
- `mkdir` 可以保持 no-op 或只允许创建 `skills/{category}` 这类虚拟目录；初期建议不支持真实 mkdir，因为 skill 创建以写 `SKILL.md` 为准。
- `_open` 写模式可用内存 buffer，在 close 时提交；也可以先只实现 `write_file/write`，由上层 wrapper 调用。

## core create 重名判断修正

当前 core `createParentSkillWithContent` 里有一段应用层重名判断：

```go
Where("owner_user_id = ? AND node_type = ? AND skill_name = ?", ...)
```

这会导致同一用户下不同 category 的同名 parent skill 也冲突。

应修正为：

```text
owner_user_id + category + node_type + skill_name
```

同时保留现有 `relative_path` 冲突检查：

```text
owner_user_id + relative_path
```

原因：

- 产品语义上 skill 由 `category/name` 唯一定位。
- 数据库已有 `owner_user_id + relative_path` 唯一索引，`relative_path` 本身包含 category/name。
- RemoteFS 路径也是 `skills/{category}/{name}/SKILL.md`。

## skill_editor create 方案

### 输入

沿用现有参数：

- `name`
- `action='create'`
- `category`
- `content`

不允许传 `operations`。

### 校验

保留：

- `validate_skill_name(name)`
- `normalize_skill_category(category)`
- `validate_skill_content(content)`
- `session_id` 必须存在

### 执行

`skill_editor.py` 调用 `tools/infra/skill_remote_store.py`：

```python
create_remote_skill(category, name, content)
```

infra 内部通过 Python `RemoteFS` 写入：

```text
remote://skills/{category}/{name}/SKILL.md
```

backend RemoteFS 负责将写入转换为 `skill_resources` 创建。

### 返回

返回 `tool_success`，包含：

- `persisted: "remote_fs"`
- `path`
- `name`
- `category`
- `action: "create"`

## skill_editor modify 方案

### 输入调整

将 `suggestions` 改为 `operations`。

支持与 `skill_rewrite` 一致的操作：

```json
{"op": "replace_text", "old": "...", "new": "..."}
```

```json
{"op": "replace_all", "content": "..."}
```

### 读取当前 skill

通过 RemoteFS 或现有 `list_all_skill_entries` / `get_skill` 读取：

```text
remote://skills/{category}/{name}/SKILL.md
```

如果找不到，返回原有“不存在”错误。

### pending 拦截

写入审核前检查 `skill_review_results`：

- `userid = user_id`
- `category = category`
- `skill_name = name`
- `review_status = 'pending'`

如果存在，则返回：

```text
存在未处理的变更，请先处理
```

### 应用 operations

复用：

```python
_apply_skill_edit_operations(current_content, {"operations": operations})
```

然后校验：

- 修改后内容不能与原内容相同。
- `validate_skill_content(edited_content)` 必须通过。

### 写入 skill_review_results

写入 `type='patch'`：

- `id`: uuid
- `skill_name`: name
- `category`: 从 edited SKILL.md frontmatter 解析，解析不到则使用参数 category
- `type`: `patch`
- `review_status`: `pending`
- `userid`: user_id
- `requestid`: session_id
- `skill_content`: edited_content
- `summary`: 保持 patch summary 语义
- `time`: 当前时间

`SkillReviewResolution.type` 不扩展 delete，自动 skill review 仍只处理 `new/patch`。

## skill_editor remove 方案

### 输入

沿用现有参数：

- `name`
- `action='remove'`
- `category`
- `reason`

不允许传 `content` 或 `operations`。

### pending 拦截

删除前检查 `skill_review_results`：

- `userid = user_id`
- `category = category`
- `skill_name = name`
- `review_status = 'pending'`

如果存在，则返回：

```text
存在未处理的变更，请先处理
```

### 执行

`skill_editor.py` 调用 `tools/infra/skill_remote_store.py`：

```python
remove_remote_skill(category, name)
```

infra 内部通过 Python `RemoteFS` 删除：

```text
remote://skills/{category}/{name}
```

backend RemoteFS 负责转换为 `skill_resources` 删除，并删除 child skills。

### 返回

返回 `tool_success`，包含：

- `persisted: "remote_fs"`
- `deleted: true`
- `path`
- `name`
- `category`
- `action: "remove"`

## skill_review_results 写表调整

### category 来源

尽量不改自动 skill review 流程。

在 `insert_skill_review_records` 写表时，从 `skill_content` 的 YAML frontmatter 解析 `category`，写入 `skill_review_results.category`。

如果解析不到：

- 自动 skill review：可落空字符串或 `general`
- skill_editor modify：fallback 为工具参数 category

### summary 逻辑

保持现有语义：

```python
summary = item.summary if item.type == 'patch' else None
```

不为了 delete 改 `SkillReviewResolution`，因为 delete 不走自动 skill review 模型。

### migration

需要给 `skill_review_results` 增加 category 字段：

```sql
ALTER TABLE public.skill_review_results
ADD COLUMN IF NOT EXISTS category text DEFAULT '' NOT NULL;
```

建议增加 pending 查询索引：

```sql
CREATE INDEX IF NOT EXISTS idx_skill_review_results_pending_identity
ON public.skill_review_results (userid, category, skill_name)
WHERE review_status = 'pending';
```

## prompt / 工具契约调整

更新 `algorithm/lazymind/chat/engine/prompts/guidance.py`：

- `create`: 说明会创建正式 skill。
- `modify`: 从 `suggestions` 改为 `operations`，并说明会写审核表。
- `remove`: 说明会删除正式 skill；如有 pending 修改则拒绝。

更新 `skill_editor.py` docstring：

- 删除 `suggestions` 参数说明。
- 新增 `operations` 参数说明。
- 明确 `create/remove` 通过 RemoteFS 作用于正式 skill，`modify` 写审核表。

## 测试方案

旧测试中断言 `/skill/create`、`/skill/suggestion`、`/skill/remove` 调用路径的部分删除或改写。

新增/更新测试：

1. backend RemoteFS 支持写 `skills/{category}/{name}/SKILL.md` 并创建 `skill_resources`。
2. backend RemoteFS 支持删除 `skills/{category}/{name}` 并删除 parent/children。
3. Python `RemoteFS.write/write_file/rm` 调用正确 backend endpoint。
4. core create 重名判断允许不同 category 下同名 skill。
5. core create 仍拒绝同一 category/name 或 relative_path 冲突。
6. `skill_editor create` 通过 infra RemoteFS wrapper 创建 skill。
7. `skill_editor modify` 读取 RemoteFS 内容，应用 operations 后写入 `skill_review_results`。
8. `skill_editor modify` 有 pending 时返回 `存在未处理的变更，请先处理`。
9. `skill_editor remove` 通过 infra RemoteFS wrapper 删除 skill。
10. `skill_editor remove` 有 pending 时返回 `存在未处理的变更，请先处理`。
11. `insert_skill_review_records` 能从 SKILL.md frontmatter 解析 category 并写入 `skill_review_results.category`。

## 风险与注意点

1. RemoteFS 写入/删除能力会成为 algorithm 管理正式 skill 的统一入口，需要在 backend 做严格 path 校验。
2. backend RemoteFS 写入应复用 core skill 创建函数，避免字段生成规则分叉。
3. backend RemoteFS 删除应复用或抽取 core 删除逻辑，避免 parent/child 删除行为分叉。
4. `resource_suggestions` 已废弃后，core 旧 `/skill/remove` 的 pending suggestion 逻辑不再适合作为 `skill_editor` 删除路径。
5. 自动 `skill_review` 不引入 delete，delete 只作为 `skill_editor remove` 的直接删除行为存在。
