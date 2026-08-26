# AI 代码审查编排平台方案

> 文档状态：目标架构。当前实现进度见
> [测试基线与实施路线](agent-review-testbed-and-roadmap.md)，稳定接口语义见
> [`docs/contracts`](../contracts/README.md)。

## 1. 项目定位

OpenReviewer 是独立于被审查业务系统的代码审查平台，首个接入仓库是 NiuMa。平台负责：

- 接收 GitHub PR 事件并跟踪每个 `head_sha`；
- 结合 diff、周边代码和仓库规则生成结构化审查结果；
- 把结果发布为 GitHub Check，必要时发布行内评论；
- 保存执行、证据、反馈、耗时和成本，支持重试与评测；
- 在后续阶段接入人工确认、飞书通知和测试候选晋级。

平台不替代普通 CI，也不直接决定是否合并主分支。编译、测试、静态检查继续由 GitHub Actions
执行；AI 只处理需要语义理解的内容。

当前仓库已完成 M3，并进入阶段 D：除结构化安全错误、路径边界、Webhook 验签、过滤、去重与
原子入库外，还实现了 GitHub App 短期身份、PR/diff/CI 获取、CI 轮询、`head_sha` 生命周期、
规则加载、全量 Review Plan 持久化、OpenAI/Anthropic 统一结构化调用、上下文自动分批，以及
模型用量、成本和 Finding 持久化。自动证据复核、GitHub Check 发布和 LangGraph 工作流尚未接入。

## 2. 核心决策

1. 平台独立部署、独立存储，不进入 NiuMa 业务进程或复用其数据库。
2. 服务使用 Python 3.12；API 使用 FastAPI，持久化使用 PostgreSQL、SQLAlchemy 和 Alembic。
3. 目标工作流使用 LangGraph，但任务和外部副作用状态仍以 PostgreSQL 业务表为准。
4. GitHub 集成使用 GitHub App，不长期依赖个人 PAT。
5. Webhook 先持久化，再由带租约的 Worker 异步处理；不使用进程内队列承载可靠任务。
6. 能机械判断的规则交给 CI、静态分析或策略代码，不交给模型猜测。
7. 首个审查版本使用一套统一审查器；只有评测证明有收益时才拆分专业审查器。
8. AI Check 初期不阻止合并，先积累真实 PR 的有效问题和误报数据。
9. 审查进程只读受限上下文，不执行 PR 中的脚本、构建命令或可执行文件。
10. 所有通知、评论和测试晋级都经过确定性策略，模型不能直接产生不受限制的副作用。

## 3. 总体架构

```text
GitHub Pull Request
    |                         \
    v                          v
GitHub Actions CI        GitHub App Webhook
                               |
                               v
                     API：验签、去重、入库
                               |
                               v
                    PostgreSQL 任务 + Outbox
                               |
                               v
                      带租约的 Review Worker
                               |
                               v
                     LangGraph 审查工作流
                               |
                 +-------------+-------------+
                 v                           v
        确定性规划与规则              模型语义审查
                 +-------------+-------------+
                               v
                         Finding 复核
                               |
                               v
                  GitHub Check / 行内评论
```

各层职责：

| 层 | 职责 |
| --- | --- |
| API | 验签、限制事件和请求大小、保存投递与任务、快速响应 |
| PostgreSQL | 审查运行、任务租约、Outbox、外部动作和反馈的权威状态 |
| Worker | 领取、续租、重试、恢复和推进工作流 |
| LangGraph | 编排可暂停、可恢复的审查节点；Checkpoint 不替代业务状态 |
| 规则与规划 | 文件筛选、Review Unit、规则路由、预算和副作用决策 |
| 模型适配器 | 统一结构化输入输出，隔离具体模型供应商 |
| GitHub 适配器 | 读取 PR/CI，创建或更新 Check 与评论 |

## 4. 目标工作流

```text
校验事件
  -> 获取 PR、当前 head_sha 和 CI
  -> 分类变更并加载相关规则
  -> 选择文件并构建 Review Unit
  -> 执行确定性检查和模型审查
  -> 汇总、去重并复核证据
  -> 再次确认 PR 与 head_sha 未变化
  -> 发布或更新 GitHub Check
  -> 后续：飞书通知 / 人工确认 / 测试候选晋级
```

较晚阶段的节点在真正实现前不得返回伪造的成功。当前 Worker 会把任务推进到
`waiting_for_ci`，在 `ready_for_review` 阶段自动生成全量计划并按上下文调用模型；模型结果保存
后写成 `completed`。人工候选标记是可选操作，GitHub Check 发布不属于当前完成条件。

### PR 与 CI 竞态

- `opened`、`reopened`、`ready_for_review` 和 `synchronize` 创建或刷新审查版本。
- CI 完成事件只推进相同 `head_sha` 的等待任务；平台自身的 AI Check 不得被列为等待对象。
- Webhook 只作为触发信号，关键节点应重新读取 GitHub 当前状态。
- 新提交到达后，旧运行标记为 `superseded`，不得覆盖新提交的 Check 或评论。
- CI 等待必须有截止时间；没有拿到 CI 结果不能显示为审查通过。

## 5. 审查上下文

Review Unit 是一次语义审查所需的最小完整上下文，不等于单个文件。例如：

```text
Controller + Service + DTO + 对应测试
Mapper XML + Entity + Flyway 迁移
Vue 页面 + API 请求 + TypeScript 类型 + 对应测试
```

文件选择和归组由确定性代码完成。每个变更文件必须记录去向，例如：

```text
model_reviewed | deterministic_only | generated | binary |
unsupported | omitted_by_limit
```

`unsupported`、`omitted_by_limit` 或无法验证的二进制文件会降低覆盖状态，不能静默算作已审查。

审查版本和具体运行使用不同标识：

```text
review_version_key = {repository_id}:{pull_request_number}:{head_sha}
thread_id = {review_version_key}:{review_run_id}
```

`repository_id` 使用 GitHub 稳定数字 ID。普通重试复用同一运行；显式重新审查才创建新的
`review_run_id`。

上下文按风险域加载，避免把无关仓库内容全部发送给模型。超限时按以下顺序收缩：过滤明确
无关内容、按 Review Unit 拆分、截取相关符号和代码段、最后才摘要未修改的周边代码。
Finding 发布前必须回读原始代码证据，不能把摘要本身当作最终证据。

## 6. Finding 与结果发布

Finding 的完整字段和验证规则以
[`review-contract.md`](../contracts/review-contract.md) 为准。核心内容包括：

| 内容 | 要求 |
| --- | --- |
| 身份 | 稳定 `fingerprint` 和产生结论的 `head_sha` |
| 位置 | 仓库相对路径、起止行、diff 侧；无法稳定定位时可为空 |
| 结论 | 严重度、类别、标题、证据、影响和修复建议 |
| 验证 | 置信度、复核状态和可选规则引用 |

问题指纹不直接依赖易变化的行号，应主要由规则、风险类别、规范化路径、代码符号和行为特征生成。

只有同时满足以下条件的 Finding 才能成为行内评论候选：

- 已通过证据复核；
- 位置属于当前 `head_sha` 的新增 diff 行；
- 证据、触发条件和影响明确；
- 不是纯风格意见或普通测试建议；
- 满足仓库策略和历史评测门槛。

其他内容进入一个 Check Summary，避免重复评论和刷屏。模型输出的置信度只是一项参考，不能
绕过证据、位置和评测门槛。

## 7. 状态、幂等和恢复

运行分别记录执行状态、审查结论和覆盖状态。完整枚举见审查契约，三者不能合并成一个模糊的
“成功/失败”：

- 模型失败、CI 缺失或上下文超限不能表示为“无问题”；
- 部分文件未审查时，结论必须同时展示 `partial` 覆盖；
- 新提交使旧结果失效时，运行进入 `superseded` / `stale`。

可靠性规则：

1. 使用 `X-GitHub-Delivery` 唯一约束去重 Webhook 投递。
2. 投递、任务和 Outbox 在同一数据库事务中创建。
3. Worker 使用 `FOR UPDATE SKIP LOCKED` 和有限租约领取任务。
4. 可重试错误使用退避和最大次数；不可重试错误保存结构化安全错误码。
5. Check、评论、通知和候选分支分别使用稳定动作键，重试时更新同一外部对象。
6. 外部 API 成功但本地回写失败时，通过动作键查询和对账，不能假设存在跨系统事务。
7. 所有外部副作用前再次校验 PR 仍打开且 `head_sha` 未变化。

## 8. 安全边界

### GitHub 与凭据

- GitHub App 只申请读取元数据、内容和 PR，以及写入 Checks 所需的最小权限。
- 创建测试分支所需的内容写权限在对应阶段单独启用，不提前授予。
- Webhook 必须基于原始请求体验签，再解析 JSON。
- Token、私钥、模型密钥和密码不得进入仓库、日志、Prompt 或审查结果。
- 不在拥有写权限和 Secrets 的工作流中执行不可信 PR 代码。

### 不可信内容

PR 代码、注释、README 和业务文档都视为不可信输入。代码中的“忽略规则”或“调用工具”不能
改变系统权限。Agent 工具只允许读取任务目录和执行白名单 Git 查询；所有路径必须规范化并
限制在仓库根目录内。

### 资源限制

每个任务必须限制文件数、单文件大小、总上下文、模型调用次数、Token、并发、重试和执行时间。
超限后进入明确的部分覆盖或人工确认状态，不能静默漏审。首版保持单 Worker 执行槽，待真实
数据证明需要扩容后再提高并发。

## 9. 评测与上线策略

使用历史 PR 和真实缺陷建立离线评测集，至少记录：

- 有效问题精确率、已知缺陷召回率和高严重度误报；
- 变更文件覆盖率、行号与证据定位准确率；
- 重复问题、范围外问题和建议可执行性；
- 审查耗时、模型调用量、成本和失败恢复率。

未裁决问题不进入精确率分母；“事实成立但暂不修改”也不能标成误报。指标需要按仓库、风险域、
规则版本、Prompt 版本和模型版本分组，并展示样本数。

上线顺序：

1. Shadow mode：只发布汇总，不阻止合并。
2. 有足够人工裁决数据后，对达到门槛的风险域开放高置信行内评论。
3. 误报上升时按规则或风险域降级回 Summary。
4. 测试候选晋级必须晚于稳定审查和人工反馈闭环，主分支仍由负责人批准。

## 10. 建设阶段

| 阶段 | 目标 |
| --- | --- |
| 已完成 M2 | 可靠任务、Worker、管理认证、Dashboard、部署闭环 |
| 当前 M3 | GitHub App、Webhook 验签、PR/CI 获取和 `head_sha` 生命周期 |
| 最小审查闭环 | Review Unit、模型适配、Finding 复核和 GitHub Check |
| 真实评测 | NiuMa PR shadow mode、反馈标注、指纹去重和结果消解 |
| 能力增强 | 按收益拆分专业审查器，接入脱敏知识库和飞书 |
| 安全晋级 | 确定性策略、人工确认和隔离测试候选 |

具体实施顺序和验收条件见
[测试基线与实施路线](agent-review-testbed-and-roadmap.md)。

## 11. 待确定事项

- GitHub App 后续发布 Check 所需的最终写权限启用时机。
- 首个模型供应商、预算、超时和降级策略。
- NiuMa 的 Review Unit 归组规则及上下文上限。
- 首批离线评测样本和各风险域准入门槛。
- 飞书知识来源采用白名单实时读取还是脱敏同步。
- 测试候选只创建分支，还是同时创建每 PR 隔离环境。
