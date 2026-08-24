# Agent 代码审查测试基线与实施路线

> 本文只记录当前基线、缺口、后续顺序和验收条件。目标架构见
> [AI 代码审查编排平台方案](code-review-agent-design.md)，部署细节见
> [`deployment/README.md`](../../deployment/README.md)。

## 1. 当前结论

OpenReviewer 目前处于 M3 GitHub 上下文准备阶段。可靠任务、Worker、管理界面、部署链路、
GitHub 安全入口以及 PR/CI 读取代码已经具备，但模型审查与结果发布闭环尚未实现。

当前 Worker 会读取与任务 `head_sha` 匹配的 PR、diff 和 CI；CI 未结束时进入
`waiting_for_ci`，终止后进入 `ready_for_review`。两个状态都不代表 AI 审查完成。

NiuMa 继续作为首个真实被审查仓库和评测来源；OpenReviewer 独立部署在 `niuma-2`，两者不共享
应用进程、数据库或部署目录，只通过 GitHub PR、Check 和 Actions 状态协作。

## 2. 已完成基线

### OpenReviewer

- Python 3.12 项目、FastAPI API、SQLAlchemy/Alembic 和 PostgreSQL。
- 幂等任务创建，同时持久化 `ReviewRun`、`ReviewTask` 和 Outbox。
- `FOR UPDATE SKIP LOCKED` 任务领取、租约续期、超时恢复、退避和最大尝试次数。
- 单并发 Worker 心跳和健康检查。
- 管理员登录、会话保护、Dashboard、任务列表和 SSE 实时更新。
- React 管理前端和 Nginx HTTPS 测试入口。
- 后端/前端 CI、SHA 镜像发布和 `niuma-2` 自动部署配置。
- 结构化安全错误、统一脱敏、跨平台路径与符号链接越界校验。
- GitHub Webhook 原始请求体验签、大小/事件限制、delivery 去重和原子任务入库。
- installation、PR 版本、Webhook delivery、外部动作审计模型和 GitHub API 客户端骨架。
- GitHub App JWT、短期 installation token 内存缓存和只读密钥挂载。
- PR 元数据、changed files、完整 diff、Check Runs 和 Commit Statuses 分页读取。
- 文件/CI 有界快照、CI 轮询与超时，以及旧 `head_sha` 批量失效保护。

稳定语义已经拆分到以下契约中：

- [`review-contract.md`](../contracts/review-contract.md)：审查状态、Finding 和行内评论准入；
- [`review-task-api.md`](../contracts/review-task-api.md)：幂等任务创建；
- [`review-worker.md`](../contracts/review-worker.md)：领取、租约、恢复和 PR/CI 状态边界；
- [`management-api.md`](../contracts/management-api.md)：登录、Dashboard 和实时事件。
- [`github-webhook.md`](../contracts/github-webhook.md)：验签、过滤、去重和原子入库。
- [`github-context.md`](../contracts/github-context.md)：短期身份、PR/diff/CI 读取和版本保护。

### NiuMa 测试场

NiuMa 已有独立的 PR CI 和测试部署流程，可为后续评测提供：

- Java、Vue、Mapper XML 和 Flyway 等真实改动；
- 编译、测试、类型检查和生产构建结果；
- 权限、数据库迁移、业务契约和测试缺口样本；
- 修复提交以及人工确认结果。

OpenReviewer 不登录 NiuMa 服务器读取运行目录，也不执行 NiuMa PR 中的构建命令。GitHub 是代码、
提交和 CI 状态的来源。

## 3. 尚未实现

### GitHub 接入

- 阶段 C 代码部署前，需要把 GitHub App 增加到 `Checks: Read-only` 和
  `Commit statuses: Read-only`；
- 超过单文件补丁上限的 Blob API 补充读取尚未实现，当前会明确降低 diff 完整度；
- GitHub Check 写入和所有发布前的第二次 stale SHA 校验属于阶段 D。

### 审查执行

- 仓库规则和 `AGENTS.md` 加载；
- 文件筛选、Review Unit 构建和上下文预算；
- 模型适配、结构化输出、证据复核和失败降级；
- GitHub Check、行内评论、问题指纹和跨提交消解；
- Token、成本和模型调用耗时记录。

### 反馈与平台能力

- 真实 PR 离线评测集和人工裁决；
- 飞书通知、成员映射和人工恢复；
- 知识库、专业审查器和多仓库策略；
- 测试候选分支、隔离环境和自动晋级策略。

这些缺口不得用空节点或固定成功响应掩盖。

## 4. 实施顺序

### 阶段 A：接入前安全补强

状态：已完成并部署。

先完成外部服务接入所需的底座：

1. 为任务错误增加结构化错误码和统一脱敏，避免 Token、密码或带凭据 URL 进入数据库与 Dashboard。
2. 补齐仓库相对路径校验，包括 Windows 盘符绝对路径和越界路径。
3. 固定 Webhook 投递、GitHub 安装、PR 版本和外部动作的数据模型与迁移。
4. 为外部 API 配置超时、可重试错误分类、退避和调用审计字段。

验收条件：用包含模拟凭据的异常测试证明敏感值不会进入日志、数据库或 API 响应。

### 阶段 B：GitHub App 与 Webhook

状态：已完成并部署，真实 GitHub App、安装范围、Webhook 和密钥已配置。

实现最小可信入口：

```text
GitHub webhook
  -> 校验原始请求体签名
  -> 限制事件和请求大小
  -> 按 X-GitHub-Delivery 去重
  -> 同一事务保存投递、审查运行、任务和 Outbox
  -> 快速返回
```

首版只接受 `pull_request` 的 `opened`、`synchronize`、`reopened` 和 `ready_for_review`。
Nginx 只新增专用 Webhook 代理路径；管理接口继续要求登录，PostgreSQL 和 Worker 仍不暴露端口。

验收条件：重复 delivery 不重复创建任务；签名错误和不支持事件不会进入队列；日志不包含凭据。

### 阶段 C：PR、CI 与提交生命周期

状态：代码与自动化测试已完成，尚未提交、推送或部署。

1. 通过 installation token 获取 PR 的 base/head、changed files 和 CI 状态。
2. 处理分页、截断 patch、大文件、二进制文件、删除和重命名。
3. 为 Worker 配置访问 GitHub 和模型 API 所需的受控出网，不增加任何 Worker 入站端口。
4. 以 `{repository_id}:{pr_number}:{head_sha}` 作为审查版本。
5. Worker 只轮询匹配同一 `head_sha` 的 CI，正常轮询不消耗失败重试次数。
6. 新提交把旧运行标记为 `superseded`，所有副作用前重新检查当前 SHA。
7. CI 超时或结果不完整时进入明确的非确定状态。

验收条件：乱序 Webhook、重复事件和连续推送新提交都不会让旧结果覆盖新结果。

### 阶段 D：最小审查闭环

```text
选择文件 -> 构建 Review Unit -> 加载相关规则
         -> 调用统一审查器 -> 校验 Finding
         -> 回读原始证据 -> 发布或更新一个 Check
```

首版要求：

- 模型供应商可替换，设置调用次数、Token、耗时和单 PR 总预算；
- 所有变更文件都有明确处理去向，无法审查时降低覆盖状态；
- 模型输出必须通过结构校验和证据复核；
- 无法稳定锚定 diff 的问题只进入 Check Summary；
- Check 使用稳定动作键更新，重试不重复创建；
- Agent 不执行 PR 代码，也不连接 NiuMa 服务器运行测试。

验收条件：固定 PR 样本可以本地重放；真实 PR 能得到带 SHA、文件、证据、影响和建议的报告；
模型失败不会显示为审查通过。

### 阶段 E：真实 PR 评测

先以 shadow mode 接入 NiuMa PR，不把 AI Check 设为合并必需条件。每个样本保存：

- 人工确认的预期问题；
- CI 结果和 Agent Findings；
- 有效、误报、重复、范围外或已知问题等裁决；
- 修复提交以及下一轮是否正确消解旧问题；
- 审查耗时、模型调用量和成本。

重点指标是高风险精确率、已知缺陷召回率、文件覆盖率、行号准确率、重复率和成本。样本不足时
只发布 Summary；某个风险域达到准入门槛后，才开放高置信行内评论。

验收条件：评测可重复运行，报表展示样本数和未裁决数量，误报上升时可按风险域自动降级。

### 阶段 F：按收益增强

最小闭环稳定后，再按实际评测结果选择：

- 拆分鉴权、数据库、架构、业务和测试等专业审查器；
- 接入经过白名单和脱敏的业务文档；
- 增加飞书通知、反馈和人工恢复；
- 支持多仓库策略、趋势和成本看板；
- 设计确定性测试候选策略和每 PR 隔离环境。

自动修复、自动批准和自动合并不属于早期范围。

## 5. 测试策略

每个阶段至少覆盖：

| 类型 | 重点 |
| --- | --- |
| 单元测试 | 签名、规范化、状态转换、指纹、预算和脱敏 |
| 数据库集成测试 | 幂等、唯一约束、租约竞争、Outbox 和恢复 |
| GitHub 契约测试 | 分页、限流、错误映射、Check 创建与更新 |
| 固定样本回放 | 同一 PR 输入应得到可比较的结构化结果 |
| 故障注入 | 超时、重复事件、新提交、外部成功但本地回写失败 |

任何会向真实 GitHub PR 写入内容的测试，都应使用专门的测试仓库或明确的 dry-run；本地和 CI
默认不写真实 PR。

## 6. 操作边界

- 不把 OpenReviewer 源码写入 NiuMa 测试 worktree。
- 不提交服务器地址、真实凭据、私钥、Token、密码哈希或生产配置。
- 不复用 NiuMa 的 PostgreSQL、Redis、MinIO 或部署目录。
- 不执行不可信 PR 中的 Maven、npm、Shell 或其他程序。
- 不修改已发布的数据库迁移，只增加向后兼容的新迁移。
- 不把 CI 缺失、模型失败或部分覆盖显示为审查通过。
- 不在评测成熟前让 AI Check 阻止合并。
- 不允许模型直接创建分支、触发部署、批准或合并 PR。
- 不自动回滚数据库结构，也不依赖可移动镜像标签判断部署版本。

## 7. 下一批具体产物

按当前状态，下一批实现应集中在阶段 D：

1. 加载仓库根目录和相关子目录的 `AGENTS.md`，把规则限制在对应文件范围；
2. 按文件类型、大小和补丁完整度筛选文件，构建有总预算的 Review Unit；
3. 定义可替换模型适配器、结构化 Finding 输出、调用次数、Token、耗时和成本记录；
4. 对模型 Finding 做 Schema 校验、原始证据回读、指纹去重和行内定位准入；
5. 发布一个使用稳定动作键的 GitHub Check，并在写入前重新校验当前 `head_sha`。

阶段 D 完成前，`ready_for_review` 仍只是明确的待处理边界，不能显示为审查成功。
