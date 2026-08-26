# 仓库规则与 Review Plan 契约

## 1. 当前边界

Worker 在精确 `head_sha` 上批量读取 `AGENTS.md`，再把数据库中的 changed files 编译成有界、
可重放的 Review Plan，并原子保存。计划保存后任务保持 `ready_for_review`，下一次领取进入固定
多 Agent 审查；模型结果保存后真实工作流进入 `awaiting_approval`，人工发布成功后才完成。

规则内容只作为后续代码审查输入。仓库规则不能扩大 GitHub App 权限、读取凭据、连接服务器、执行
PR 代码或覆盖平台安全边界。

## 2. 规则作用域与读取

每个文件的候选规则从宽到窄排列。例如 `services/auth/login.py` 对应：

```text
AGENTS.md
services/AGENTS.md
services/auth/AGENTS.md
```

根规则作用于全仓库，子目录规则只作用于该目录及其后代。后续构造模型输入时保持这个顺序；同一
事项冲突时，更深目录的规则优先。

加载器先在内存中汇总所有 changed files 的候选路径，再使用一个 `POST /graphql` 请求读取全部
候选。查询使用别名和变量，规则路径不会拼进 GraphQL 语法；对象表达式绑定任务的精确
`head_sha`。响应还必须返回匹配的仓库数字 ID 和完整名称。

默认边界如下：

| 边界 | 默认值 |
| --- | --- |
| changed files | 沿用 PR 上下文上限 3000 |
| 单文件父目录作用域深度 | 32 |
| 单次候选规则路径 | 128 |
| 单个规则 UTF-8 大小 | 64 KiB |
| 规则内容总大小 | 256 KiB |
| GraphQL 响应 | 2 MiB |

不存在的候选返回 `null`，不算错误。二进制、过大、文本不可用、总量超限、响应超限，以及候选或
目录深度超限都会产生结构化 issue，并把受影响的 changed files 放入 `incomplete_files`。缺失
请求别名、部分 GraphQL 错误、仓库身份不一致或字段格式无效则拒绝整份响应，不能把它们误判成
“规则不存在”。

## 3. 文件规划

规划器不访问网络、数据库或本地仓库。每个 changed file 必须恰好得到一个结果：

| decision | 含义 |
| --- | --- |
| `planned` | 已生成一个 Review Unit，但尚未经过模型审查 |
| `binary` | 二进制补丁，不进入模型 |
| `generated` | 生成目录、压缩产物、source map 或依赖锁文件 |
| `unsupported` | 首版不支持的文件类型 |
| `patch_missing` | GitHub 没有提供可审查补丁 |
| `patch_too_large` | 单文件文本补丁在上下文阶段已超过 8 MiB 安全上限，无法送入模型 |
| `rules_incomplete` | 无法证明该文件所需规则读取完整 |
| `omitted_by_budget` | 仅用于读取旧版计划；v2 不再用预算省略可审查文件 |

`planned` 表示文件已进入模型输入，任务完成后可视为本轮 AI 已覆盖。所有其他 decision 都没有
`unit_key`，不能制造空 Review Unit 掩盖缺口。

首版使用“一文件一 Unit”。支持常见代码、脚本、配置、Markdown、Vue、Mapper XML 和 SQL；
`node_modules`、`vendor`、`build`、`dist`、`target`、覆盖率目录、压缩 JS/CSS、source map 和常见
锁文件按生成内容处理。这个分类是确定性策略，不读取或执行文件内容。

## 4. 分批与稳定身份

`review-planner-v2` 会收集最多 3000 个 changed files 中的全部可审查补丁，不再使用旧版“最多
100 个 Unit、单 PR 2 MiB”配置丢弃后面的文件。旧字段暂时保留用于配置和历史数据兼容，其中
规则作用域深度仍然生效。

模型适配器再按单批输入配置、上下文窗口和 HTTP 大小分批，默认每批输入上限为 64K Token。
Token 使用保守的 `2 UTF-8 字节/Token` 估算，
并记录供应商返回的实际输入、输出、缓存与推理 Token、耗时和成本。文件按规范化路径排序，
因此同一输入不会因 GitHub 列表顺序不同而改变结果。
Unit 稳定键包含：

```text
planner_version + review_version_key + head_sha + file + blob_sha
+ patch_sha256 + [(rule_path, rule_content_sha256)]
```

Plan 指纹再包含全部规则摘要、文件 decision 和 Unit 键。新 `head_sha`、补丁、适用规则或规划器
版本变化都会产生新身份，旧计划不能覆盖新提交。

## 5. 数据规模与查询次数

- GitHub 规则读取请求次数固定为 `O(1)`，不是每文件或每目录一次请求；
- 候选汇总使用字典和集合，最多处理 3000 个文件、每文件 32 层父目录；
- 规则按路径放入字典，规划循环内只做内存查找，复杂度约为 `O(文件数 × 有界路径深度)`；
- 计划输入用一次带 `LIMIT 3001` 的 JOIN 访问 `review_tasks`、`review_runs`、
  `pull_request_versions` 和 `pull_request_files`，查询次数为 `O(1)`；
- 保存事务锁定当前任务/运行与 PR 版本，校验租约、`review_version_key`、`head_sha` 和状态，再对
  `review_plans`、`review_plan_rules`、`review_units`、`review_file_plans` 执行批量写入；
- 没有无界查询、全表扫描、N+1、远程调用事务或 PR 代码执行。

## 6. 持久化、幂等与版本保护

`review_plans` 保存计划指纹、规划器版本、规则读取完整性、结构化 issue、数量和总预算；三张子表
分别保存规则内容快照、Review Unit 和每个 changed file 的唯一 decision。计划与
`review.plan.prepared` Outbox 在同一事务提交，任一写入失败都会整体回滚。

同一运行再次提交相同 `plan_fingerprint` 时直接复用旧计划，不重复插入子表或 Outbox；不同指纹
会报告冲突。保存前若发现同一 PR 已有更新 `head_sha`，旧任务进入 `superseded`，旧计划不会落库。
`files_complete=false`、文件数量不一致或超过 3000 个时拒绝规划；单文件 `patch_missing`、
`patch_too_large` 和二进制仍作为明确 decision 保存。
