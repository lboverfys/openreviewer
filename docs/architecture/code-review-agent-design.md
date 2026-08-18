# AI 代码审查编排平台方案

> 文档状态：方案草案  
> 最后更新：2026-08-18  
> 首个接入仓库：NiuMa  
> 核心编排框架：LangGraph

## 1. 项目定位

本项目是独立于 NiuMa 业务系统的研发效能平台。它不是在 NiuMa 中增加几个模型调用，而是建设一套可接入多个 GitHub 仓库的代码审查、规范检查、人工反馈和测试晋级系统。

NiuMa 作为首个真实接入项目，用来验证平台是否能处理以下实际问题：

- Java、Vue、Mapper XML、Flyway 等多种文件类型。
- Sa-Token 鉴权、资源归属和业务权限审查。
- 项目架构约束和团队 Git/Flyway 协作规范。
- 飞书接口文档、业务文档和项目规范检索。
- PR 审查、测试分支生成、飞书通知和人工审批闭环。
- 多成员协作下的问题路由、误报反馈和审查效果评估。

项目最终应当具备独立展示、独立部署和独立接入其他仓库的能力，适合作为简历中的完整项目。

## 2. 已确定的关键决策

1. 项目单独建立仓库，不放进 `niuma-business` 或 NiuMa 生产运行时。
2. 使用 Python + LangGraph 作为 Agent 编排主线。
3. 不同时引入 Spring AI 或 Google ADK，避免编排框架重复。
4. GitHub 集成使用 GitHub App，不长期依赖个人 PAT，也不以 GitHub MCP 作为生产集成核心。
5. 飞书通知使用企业自建应用和机器人，不只使用群 Webhook。
6. 确定性规则优先交给普通 CI、静态分析和策略引擎，AI 只负责需要理解语义的部分。
7. AI 不直接拥有不受限制的合并权限。AI 输出结构化结论，最终由确定性策略引擎决定是否允许进入测试阶段。
8. 阶段二的 AI 审查不阻止合并，先积累真实反馈和误报数据。
9. 自动测试优先使用每个 PR 独立的临时测试分支，不把所有未批准 PR 混进同一个长期测试分支。
10. NiuMa 当前共享 PostgreSQL、Redis 测试环境不直接提供给任意 PR 自动执行迁移或破坏性测试。
11. OpenCodeReview、PR-Agent 等开源项目只作为设计和评测参考，不作为阶段二的运行依赖；核心审查能力继续使用 Python + LangGraph 原生实现。
12. Webhook 接收后先在 PostgreSQL 中持久化投递记录和审查任务，再由 Worker 异步领取；阶段二不使用进程内队列或 FastAPI `BackgroundTasks` 承载可靠任务。
13. PostgreSQL 业务表是审查运行和外部副作用状态的权威来源，LangGraph Checkpoint 只负责工作流恢复，GitHub Check 和飞书消息是对外投影。
14. 阶段二的线上主模型使用 DeepSeek V4 Flash，同时保留模型适配接口；即使模型上下文较大，也继续执行上下文筛选、预算控制和分层压缩。
15. 各阶段以第 19 节定义的能力范围和验收条件为准；AI 辅助开发可以加快实现，但不能跳过事件契约、状态恢复、外部联调和效果评测。
16. NiuMa 业务和测试部署继续运行在 `niuma` 服务器，Agent 审查平台的阶段二测试环境独立部署在 `niuma-2`，两者通过 GitHub PR、Check 和 Actions 状态协作，不共享进程、部署目录或平台数据库。
17. `niuma-2` 使用容器固定 Python 3.12 运行时，首版只部署 FastAPI API、单并发 Worker 和独立 PostgreSQL；Redis 仍按实际需要增加，不因为服务器已空闲就提前引入。
18. 阶段二测试期允许使用 `http://<niuma-2-public-ip>:18090` 接收 GitHub Webhook，域名、HTTPS 和主机级安全加固暂缓；原始请求体验签、事件白名单、请求大小限制、投递去重和内部端口隔离仍是必需能力。

## 3. 为什么选择 LangGraph

代码审查平台的核心不是聊天，而是长时间、有状态、可暂停和可审计的工作流：

```text
读取 PR
  -> 获取代码与规范
  -> 运行确定性检查
  -> 按审查单元执行语义审查
  -> 汇总和证据复核
  -> 可能暂停等待人工审批
  -> 可能在数小时后继续
  -> 创建测试候选分支
  -> 等待部署和验收结果
```

LangGraph 适合这个场景的主要原因：

- 可以在同一张图中混合普通代码节点、工具节点和模型节点。
- 支持条件路由、并行分支、子图和有限循环。
- Checkpoint 可以保存每一步状态，进程重启后能够恢复。
- Interrupt 可以暂停执行，等待飞书或管理台返回人工决定。
- 可以查看、修改和审计状态，再从指定线程继续。
- 容易把 PR 编号、提交 SHA 和审查状态绑定为稳定工作流标识。
- 模型提供商可替换，不强制绑定单一厂商。

Google ADK 2.0 已支持 Graph Workflows、人工输入、恢复运行和评测，开发体验也更一体化。但其 Graph Workflows 相对较新，当前官方文档仍列出语言支持和部分集成限制。对于以持久化、人工审批和严格副作用控制为核心的代码审查平台，最终选择 LangGraph。

参考资料：

- LangGraph 概览：https://docs.langchain.com/oss/python/langgraph/overview
- LangGraph 持久化：https://docs.langchain.com/oss/python/langgraph/persistence
- LangGraph Interrupt：https://docs.langchain.com/oss/python/langgraph/interrupts
- ADK Graph Workflows：https://adk.dev/graphs/
- ADK Evaluation：https://adk.dev/evaluate/

### 3.1 外部项目借鉴边界

[OpenCodeReview](https://github.com/alibaba/open-code-review) 已经验证了“确定性工程 + Agent 推理”的代码审查思路。平台借鉴以下设计：

- 由普通代码完成待审文件筛选，避免模型漏看文件或自行缩小范围。
- 将有业务关联的文件组成 Review Unit，在有限上下文中完成一次相对完整的行为审查。
- 根据路径、语言、模块和风险域精确匹配规则，减少无关 Prompt。
- 将问题发现、行号定位和结论复核拆开，降低错行和泛化意见。
- 使用结构化输出并记录规则、模型、耗时和 Token，支持可重复评测。

阶段二不引入 OpenCodeReview 的 Go CLI，不单独部署外部审查服务，也不 fork 其源码。平台自身使用 Python + LangGraph 实现上述能力，避免同时维护两套任务状态、会话恢复、配置和部署链路。只有当历史 PR 评测证明原生审查在精确率、召回率或成本上无法达到目标时，才重新评估外部引擎适配。

OpenCodeReview 解决的是“一次代码变更如何审查”；本平台重点解决的是“如何结合项目规范和业务资料，形成可恢复、可审计、可人工介入并能安全触发测试晋级的完整流程”。

## 4. 普通 PR CI 是什么

普通 PR CI 是不依赖大模型的自动化检查。开发者创建 PR 或向 PR 推送新提交后，GitHub 自动拉取对应提交并执行固定命令。

这些检查具有确定性，同一份代码重复运行通常应得到相同结果，因此可以作为强制合并条件。

NiuMa 阶段一的 PR CI 可以包含：

### 4.1 后端检查

- Java 编译和 Maven `verify`。
- JUnit 单元测试。
- Spring 上下文测试。
- ArchUnit 架构约束测试。
- Controller、Service、Mapper XML 契约测试。
- Flyway 文件命名、版本冲突和历史文件修改检查。

### 4.2 前端检查

- TypeScript 类型检查。
- Vitest 测试。
- Vite 生产构建。

### 4.3 协作规范检查

- 分支命名是否符合团队约定。
- 提交标题和正文是否符合仓库规范。
- 是否在 Mapper 注解中编写 SQL。
- 包结构、模型归属和 `service.impl` 位置是否正确。
- 是否修改已经发布的 Flyway 脚本。
- 是否意外提交密钥、Token、构建产物或大文件。

CI 能发现“测试失败”和“确定性规范不符合”，但如果代码缺少测试，或者问题需要理解业务语义，CI 可能全部通过。因此 AI 审查建立在 CI 之上，而不是替代 CI。

## 5. 确定性规则与 AI 规则的边界

| 问题类型 | 主要执行者 | 示例 |
|---|---|---|
| 可以机械判断 | CI、ArchUnit、Semgrep、策略代码 | 包结构、迁移命名、禁止注解 SQL |
| 可以通过测试判断 | Maven、Vitest、Testcontainers | 查询结果、异常行为、上下文启动 |
| 需要理解代码语义 | 审查 Agent | 越权、事务边界、状态流转错误 |
| 需要理解业务资料 | 审查 Agent + 文档检索 | 接口是否符合飞书业务规则 |
| 涉及真实副作用 | 确定性策略引擎 | 是否创建测试分支、是否发送通知 |
| 最终主线决策 | 仓库负责人 | 是否合入 `master` |

基本原则：能用普通代码准确判断的规则，不交给大模型猜测。

## 6. 总体架构

```text
GitHub Pull Request
        |
        +--> GitHub Actions PR CI
        |        |
        |        +--> 编译、测试、静态分析、规范检查
        |
        +--> GitHub App Webhook
                 |
                 v
          Review API / Task Service
                 |
                 v
       PostgreSQL WebhookDelivery
          + ReviewTask + Outbox
                 |
                 v
           Review Worker
                 |
                 v
          LangGraph Review Workflow
                 |
                 v
       Deterministic Review Planner
       分类 -> 文件筛选 -> Review Unit -> 规则路由
                 |
                 v
          Native Semantic Review
       阶段二统一审查器，阶段三按风险域拆分
                 |
                 v
          Finding Verifier
                 |
                 v
          Policy Decision Service
           |                  |
           v                  v
     GitHub Checks       飞书机器人
           |
           v
    PR 临时测试分支和测试环境
```

Webhook API 只负责验签、解析最小事件信息，并在一个数据库事务中保存 `WebhookDelivery`、需要创建的 `ReviewTask` 和对应 Outbox 事件，然后立即返回。Worker 使用带租约的任务领取机制异步执行审查，支持重试、退避、超时恢复和死信标记。阶段二直接使用 PostgreSQL `FOR UPDATE SKIP LOCKED` 实现，不需要为了任务队列提前引入 RabbitMQ。

## 7. LangGraph 工作流设计

以下是平台的目标工作流。阶段二启用从 `START` 到 `publish_github_check` 的审查主链路；飞书通知与人工恢复在阶段四启用，测试晋级相关节点在阶段五启用。

```text
START
  -> validate_event
  -> collect_pr_context
  -> wait_for_ci
  -> classify_changes
  -> retrieve_project_rules
  -> run_static_analysis
  -> select_review_files
  -> build_review_units
  -> review_units
  -> aggregate_findings
  -> verify_findings
  -> publish_github_check
  -> notify_feishu
  -> decide_test_promotion
       -> reject_or_wait
       -> interrupt_for_human
       -> create_test_candidate
  -> END
```

阶段四和阶段五的节点在对应阶段启用前只保留状态契约和接口边界，不在较早阶段伪造空实现，也不产生分支、部署或通知等副作用。

`select_review_files` 和 `build_review_units` 默认由普通代码完成。模型可以在无法确定文件关系时提供建议，但不能决定是否跳过已变更文件。只修改 Vue 页面时不加载 Flyway 规则，只修改 README 时也不调用鉴权规则。

Review Unit 是一次语义审查所需的最小完整上下文，不等同于单个文件。例如：

```text
Controller + Service + DTO/VO + 对应测试
Mapper XML + Entity/Projection + Flyway
Vue 页面 + API 请求代码 + TypeScript 类型 + 对应测试
```

阶段二由同一个统一审查器处理所有 Review Unit，可以在明确并发上限后并行执行。阶段三再在 `review_units` 内部按风险域路由到架构、鉴权、数据库、业务和测试审查器，不改变外层工作流协议。

建议的审查单元对象：

```python
class ReviewUnit(TypedDict):
    unit_id: str
    changed_files: list[str]
    context_files: list[str]
    applicable_rule_ids: list[str]
    risk_domains: list[str]
    estimated_input_tokens: int
```

每个变更文件还需要记录明确的处理去向：`MODEL_REVIEWED`、`DETERMINISTIC_ONLY`、`GENERATED`、`BINARY`、`UNSUPPORTED` 或 `OMITTED_BY_LIMIT`。`UNSUPPORTED`、`OMITTED_BY_LIMIT` 以及无法由确定性策略验证的 `BINARY` 会降低覆盖状态，不能被静默当作已审查。

建议的状态对象至少包含：

```python
class ReviewState(TypedDict):
    review_run_id: str
    installation_id: int
    repository_id: int
    repository: str
    pull_request_number: int
    base_sha: str
    head_sha: str
    changed_files: list[str]
    file_dispositions: list[dict]
    selected_files: list[str]
    review_units: list[ReviewUnit]
    rule_bundle_version: str
    ci_results: list[dict]
    applicable_rules: list[dict]
    static_findings: list[dict]
    agent_findings: list[dict]
    verified_findings: list[dict]
    execution_status: str
    review_conclusion: str
    coverage_status: str
    budget_usage: dict
    risk_level: str
    promotion_decision: str
    human_decision: dict | None
```

逻辑审查版本和具体工作流线程使用不同标识：

```text
review_version_key = {repository_id}:{pull_request_number}:{head_sha}
thread_id = {review_version_key}:{review_run_id}
```

`repository_id` 使用 GitHub 不随仓库改名变化的数字 ID。普通重试复用同一个 `review_run_id` 和 `thread_id`；用户明确要求对同一提交重新审查时创建新的 `review_run_id`。PR 推送新提交后创建新的 `head_sha` 审查版本，旧运行标记为 `SUPERSEDED`，旧结论不能用于处理新提交。

### 7.1 Webhook、CI 和新提交竞态

- 使用 GitHub `X-GitHub-Delivery` 作为 Webhook 投递去重键，重复投递只更新接收记录，不重复创建同一任务。
- PR 的 `opened`、`reopened`、`ready_for_review` 和 `synchronize` 事件负责创建或刷新审查版本；CI 的完成事件只推进匹配同一 `head_sha` 的等待任务。
- `wait_for_ci` 是事件驱动的等待状态，不在 Worker 中长轮询或占用线程。CI 状态变化后由新 Webhook 重新投递可运行任务。
- 每个仓库显式配置需要等待的 CI Check 名称，并排除平台自身的 AI Check，避免形成自我等待。
- CI 事件乱序到达时，以 GitHub 当前 PR 状态和当前 `head_sha` 的重新读取结果为准，事件负载只作为触发信号。
- Worker 在关键节点和所有外部副作用前重新检查当前 `head_sha`。发现新提交后协作取消旧任务，并把尚未开始的 Review Unit 标记为过期。
- CI 等待必须有截止时间；超时后进入可解释的非确定状态，不得把“没有拿到 CI 结果”当作 CI 通过。

## 8. 审查器职责

### 8.1 改动分类器

- 判断变更涉及哪些语言、模块和风险域。
- 选择规则范围和需要补充的代码、测试及文档上下文。
- 不输出最终缺陷结论。

### 8.2 审查单元构建器

- 先确定所有需要审查的变更文件，再按照模块依赖、调用关系、命名和目录规则组成 Review Unit。
- 为每个单元附加必要的周边代码、测试和规则引用，避免只看孤立 diff。
- 记录无法归组、被过滤和上下文截断的文件，任何变更文件都必须有明确去向。
- 对单元大小、文件数和 Token 估算设置上限，过大时按行为边界继续拆分。

### 8.3 统一审查器

- 阶段二使用统一 Prompt、工具集和输出模型处理所有 Review Unit。
- 根据单元携带的风险域和规则进行审查，不加载无关规则。
- 可以读取允许的周边文件和搜索代码，但不能修改代码或执行仓库脚本。
- 只输出候选问题，不负责测试晋级或其他副作用决策。

以下专业审查器在阶段三按真实评测结果逐步拆分，不属于阶段二的能力范围。

### 8.4 架构审查器

- 包结构和模块依赖。
- DTO、VO、Entity、Projection 归属。
- Service 与实现类位置。
- 跨域接口、workflow、port 和 component 的归属。
- 是否出现无实际价值的空抽象和重复封装。

### 8.5 鉴权和安全审查器

- Sa-Token 登录态、终端、角色和权限。
- 是否信任前端传入的用户 ID、门店 ID 或资源归属。
- 越权、敏感信息日志、Token 泄漏和配置风险。
- GitHub Workflow、脚本和依赖修改带来的供应链风险。

### 8.6 数据库和 Flyway 审查器

- 新迁移命名和成员编号。
- 是否修改、删除或重命名已经发布的迁移。
- 可空字段、约束、索引、稳定排序和分页。
- Mapper XML 动态条件、批量查询和 N+1 风险。

### 8.7 业务契约审查器

- 对照飞书接口文档和原始业务流程。
- 检查暂定字段是否被错误当作最终规则。
- 检查接口实现、错误码和返回契约的一致性。

### 8.8 测试缺口审查器

- 判断改动是否缺少正常路径、空值、越权和状态边界测试。
- 检查修复是否只覆盖现象而没有覆盖根因。
- 不把“测试数量多”错误等同于“关键行为已覆盖”。

### 8.9 结论复核器

- 再次读取相关代码和规范，为每个结论寻找证据。
- 删除没有文件、行号、行为影响或规则依据的泛化意见。
- 合并重复问题并标记置信度。
- 区分“确定问题”“疑似风险”“改进建议”“测试缺口”。

## 9. 审查输出格式

每个问题建议包含：

```json
{
  "fingerprint": "稳定问题指纹",
  "head_sha": "当前审查提交 SHA",
  "severity": "HIGH",
  "category": "AUTHORIZATION",
  "location": {
    "file": "src/main/.../ExampleService.java",
    "blob_sha": "文件 Blob SHA",
    "start_line": 128,
    "end_line": 128,
    "side": "RIGHT",
    "in_diff": true,
    "symbol": "ExampleService#getByUserId"
  },
  "title": "资源归属未在服务端校验",
  "evidence": "当前实现只读取请求中的 userId",
  "impact": "已登录用户可能查询其他用户资源",
  "suggestion": "从登录态获取当前用户并校验资源归属",
  "required_test": "增加跨用户访问被拒绝的测试",
  "confidence": 0.93,
  "verification_status": "VERIFIED",
  "rule_reference": "AGENTS.md / 开发质量要求"
}
```

领域对象使用起止行和 `side` 描述位置，由 GitHub 适配器转换成 Review Comment 或 Check Annotation 所需参数。删除文件、重命名文件、非 diff 行、过期 `head_sha` 或无法稳定锚定的位置不得强行发布行内评论，应降级到 Check Summary。

问题指纹不能直接包含易变化的原始行号，建议由规则 ID、风险类别、规范化文件路径、代码符号和行为特征生成。行号只用于当前提交定位，指纹用于跨提交识别同一问题。GitHub 中只对满足评测准入条件且已复核的问题发布行内评论，其余内容集中放在一个 Check Summary 中，避免刷屏。

## 10. 阶段二的单 Agent 设计

阶段二的“单 Agent”是指所有 Review Unit 共用一套统一审查器，不急于拆成大量会互相讨论的 Agent。改动分类、文件筛选、单元构建、规则路由和结果校验属于确定性工作流节点，不为了数量而包装成 Agent。

统一审查器可以顺序处理 Review Unit，也可以在模型限流和成本上限内有限并行。并行处理多个审查单元不等同于多 Agent 协商，最终仍使用相同的工具、Prompt、输出协议和复核流程。

阶段二输出：

- PR 风险摘要。
- 有证据的问题。
- 测试缺口。
- 需要人工确认的事项。

阶段二边界：

- 不因为 AI 的单独判断阻止合并。
- 不自动修改代码。
- 不自动向开发者分支推送提交。
- 不自动合并到 `master`。
- 不对每个小建议发送飞书私聊。

目的在于先收集真实 PR 数据，计算采纳率和误报率。多个 Agent 不是项目成熟度的证明，可靠的评测闭环才是。

## 11. PR 自动进入测试阶段

### 11.1 推荐方案

原始 PR 继续以 `master` 为目标。AI 和 CI 审查通过后，平台从最新 `master` 创建一个临时测试候选分支，再合入该 PR 的指定提交：

```text
test/pr-{pr_number}-{head_sha_short}
```

例如：

```text
test/pr-128-a1b2c3d
```

这一步用于生成“如果当前 PR 合入最新主线后”的候选版本，但不把原始 PR 标记为已合并。候选版本完成部署和验收后，仍由仓库负责人决定是否把原始 PR 合入 `master`。

不建议把所有未批准 PR 直接合进一个长期 `test` 分支，因为会造成改动互相污染、失败难以归因和分支长期偏离主线。

### 11.2 自动晋级必须同时满足的条件

- PR 不是 Draft。
- PR 带有明确的自动测试授权标签，例如 `auto-test`。
- 后端、前端、静态分析和规范 CI 全部通过。
- 当前 PR `head_sha` 与 AI 审查时完全一致。
- 能与最新目标分支无冲突合并。
- 没有高风险且已验证的问题。
- 没有未解决的人工阻塞意见。
- 修改范围没有命中敏感文件规则。
- GitHub App 只对允许的测试分支具有写权限。

### 11.3 必须人工确认的敏感范围

- `db/migration` 下的正式 Flyway 脚本。
- 鉴权、权限和 Sa-Token 核心配置。
- 支付、余额、资金流水和订单资金状态。
- GitHub Actions、构建脚本和依赖锁文件。
- 生产配置、密钥引用和部署配置。
- 大规模删除、重命名或跨模块架构调整。

### 11.4 共享测试环境风险

NiuMa 当前使用团队共享 PostgreSQL、Redis 等测试基础设施。PR 自动测试不能默认连接共享数据库执行迁移。

建议：

- 普通 PR 在 CI 中使用 Testcontainers 或隔离数据库。
- 涉及 Flyway 的 PR 禁止自动部署到共享环境。
- 共享测试环境迁移继续遵守现有 Flyway 协作和人工确认流程。
- 后续如需每 PR 自动部署，使用独立数据库、schema、Redis 前缀或独立命名空间。

## 12. 飞书集成

### 12.1 接入方式

使用飞书企业自建应用和机器人，直接调用飞书开放平台 API。生产通知不依赖个人会话中的飞书 MCP。

企业自建应用可以：

- 给指定成员发送机器人私聊。
- 在项目群中精确 `@` 负责人。
- 发送和更新交互式卡片。
- 接收“已处理”“误报”“请求复审”“批准测试”等按钮回调。
- 将回调结果恢复到暂停中的 LangGraph 工作流。

### 12.2 责任人路由

建议优先级：

1. PR 作者是默认处理人。
2. 根据 `CODEOWNERS` 查找文件负责人。
3. 根据仓库业务域规则覆盖负责人。
4. 高风险问题同时升级给仓库负责人。

需要维护 GitHub 用户和飞书 `open_id` 的映射，不能只依赖昵称：

```yaml
members:
  dev_lboverfys: ou_xxxxx
  dev_wh: ou_xxxxx
  dev_yunyu: ou_xxxxx
```

### 12.3 消息卡片内容

- 仓库、PR 编号、标题和当前提交。
- 风险等级和问题数量。
- 问题文件、行号、证据和影响。
- GitHub PR 与 Check 链接。
- “标记已处理”“标记误报”“请求复审”“批准进入测试”按钮。

### 12.4 消息降噪

- P0/P1：负责人私聊，同时在群中通知负责人和管理员。
- P2：只通知 PR 作者或代码负责人。
- P3/建议项：只显示在 GitHub 报告中。
- 同一个 PR 更新同一张卡片，不重复发送新消息。
- 新提交到达后将旧问题标记为过期，再发布增量复审结果。
- 飞书是否出现桌面弹窗取决于成员客户端通知设置，平台只能保证消息送达。

## 13. 推荐技术栈

### 13.1 后端和 Agent

```text
Python 3.12
FastAPI
LangGraph
Pydantic
SQLAlchemy 2
Alembic
httpx
pytest
```

LangGraph 只负责 Agent 工作流编排。FastAPI 负责 GitHub/飞书 Webhook 和管理 API，PostgreSQL 负责平台业务数据，职责不要混在一起。

### 13.2 代码理解和确定性工具

```text
GitHub REST/GraphQL API  PR、提交、评论、Check
Git diff                 增量差异
tree-sitter              多语言 AST 和代码结构
Semgrep                  可配置静态规则和安全扫描
CodeQL                   后期可选的深度安全分析
```

OpenCodeReview 和 PR-Agent 属于参考实现，不进入阶段二的依赖、容器或部署清单。需要借鉴的文件筛选、Review Unit、规则路由和复核能力直接在 `review_engine` 中实现。

### 13.3 数据和状态

```text
PostgreSQL               PR、审查、问题、反馈、审计
PostgreSQL Task/Outbox   Webhook 去重、可靠任务投递和外部动作事件
LangGraph PostgresSaver  工作流 Checkpoint
pgvector                 阶段三的文档和规范向量检索
Redis                    幂等、锁、限流和短缓存，按需增加
```

阶段二使用 PostgreSQL 任务表和 Outbox 完成可靠投递。Worker 通过 `FOR UPDATE SKIP LOCKED` 领取任务，并记录租约截止时间、尝试次数、下次重试时间和最终失败原因。进程崩溃后，其他 Worker 可以在租约过期后重新领取。

状态权威关系固定为：

1. PostgreSQL 业务表保存 `ReviewRun`、问题、覆盖率、反馈和外部动作，是平台事实来源。
2. LangGraph Checkpoint 保存节点级中间状态，用于恢复同一 `thread_id`，不能代替业务审计表。
3. GitHub Check、PR 评论和飞书消息是业务状态的外部投影，必须把外部对象 ID 回写数据库。

平台按“至少执行一次 + 幂等”设计，不宣称严格 exactly-once。RabbitMQ 不作为阶段二必需组件；只有当 PostgreSQL 任务领取无法满足吞吐、延迟、重试隔离或死信治理要求时，再通过任务接口替换为独立消息队列。

### 13.4 模型层

- 阶段二的线上主模型使用 DeepSeek V4 Flash，统一审查和证据复核可以先使用同一模型的不同 Prompt 与输出契约。
- 通过 LangChain 模型适配包、LiteLLM 或项目内模型端口保留提供商替换能力，领域层不直接依赖厂商响应对象。
- 普通代码负责文件筛选、Review Unit 构建和规则路由；便宜模型只在关系难以确定时提供受约束的辅助建议。
- 强模型负责语义审查和最终证据复核。
- 阶段三再引入 Embedding 模型，只处理允许进入知识库的规范和业务文档。
- 模型名称、服务端点、上下文上限、温度、超时、重试和成本上限均由仓库策略配置，不把模型标称上下文长度硬编码在审查器中。
- 每次调用记录模型与接口版本、Prompt 版本、规则版本、输入输出 Token、缓存命中、耗时、重试和费用。模型自报置信度必须经过离线样本和线上反馈校准，不能直接解释成真实正确概率。

### 13.5 前端管理台

```text
Vue 3
TypeScript
Vite
Element Plus
Axios
```

管理台主要展示：

- 仓库和 GitHub App 安装状态。
- PR 审查运行图和节点耗时。
- 问题、证据、反馈和处理状态。
- 模型调用量、成本和失败率。
- 规则配置、成员映射和通知策略。
- 自动测试分支和环境状态。

### 13.6 测试、部署和观测

```text
pytest
Testcontainers
Docker Compose
GitHub Actions
OpenTelemetry
Prometheus/Grafana（后期）
```

### 13.7 阶段二测试部署基线

截至 2026-08-18，阶段二测试部署位置已经确定为 `niuma-2`。NiuMa 应用继续运行在
`niuma` 服务器，审查平台不通过 SSH 读取 NiuMa 运行目录，也不连接 NiuMa 的业务或
共享测试数据库；PR 代码、提交状态、CI 结果和审查结论统一通过 GitHub 交换。

`niuma-2` 的只读检查结果如下：

| 项目 | 当前状态 |
|---|---|
| 操作系统 | Debian 13，x86_64 KVM |
| 计算资源 | 3 vCPU、约 3.8 GiB 内存、2 GiB Swap |
| 磁盘 | 根盘约 62 GiB，可用约 57 GiB |
| 容器环境 | Docker 26.1.5、Docker Compose 2.26.1 |
| 当前负载 | 负载较低，没有已有容器、镜像或 Docker 数据卷 |
| 网络 | 可访问 GitHub API、GHCR 和 DeepSeek API 域名；当前只有 SSH 端口监听 |
| 主机 Python | Python 3.13.5，仅作为主机工具，不作为项目运行时 |

该资源适合外部模型 API 模式下的低并发 MVP，不适合在本机运行大模型，也不作为高并发、
高可用生产环境。首版 Worker 并发固定为 `1`，根据真实 PR 的内存、耗时和队列数据再决定
是否提高。API、Worker 和 PostgreSQL 使用独立容器及内部网络，PostgreSQL 不映射公网
端口；项目使用 Python 3.12 基础镜像，避免依赖主机 Python 版本。

测试期入口拓扑为：

```text
GitHub
  -> http://<niuma-2-public-ip>:18090/webhooks/github
  -> FastAPI API
  -> PostgreSQL ReviewTask / Outbox
  -> Worker（concurrency=1）
  -> GitHub API / DeepSeek API
```

公网入口只提供 Webhook 和不含敏感信息的健康检查。管理 API、OpenAPI 文档和数据库保持
内部可见。直接 IP 和 HTTP 是测试阶段接受的临时边界，Webhook 内容不会获得传输加密；
转为公开演示、长期运行或生产用途前，必须改为域名、HTTPS 和经过限制的公网入口。

## 14. 建议的服务模块

```text
code-review-agent/
├── apps/
│   ├── api/                    FastAPI、Webhook、管理 API
│   └── worker/                 审查任务执行器
├── review_engine/
│   ├── graph/                  LangGraph 状态、节点和路由
│   ├── planning/               改动分类、文件筛选、Review Unit 和规则路由
│   ├── reviewers/              统一审查器及后续专业审查器
│   ├── context/                diff、代码、CI 和文档上下文
│   └── verification/           证据复核和问题去重
├── policy_engine/              确定性晋级、熔断和权限规则
├── integrations/
│   ├── github/                 GitHub App、Webhook、Checks
│   └── feishu/                 消息、卡片和事件回调
├── knowledge/                  文档同步、脱敏、切分和检索
├── persistence/                SQLAlchemy、Alembic、Checkpoint
├── web/                        Vue 管理台
├── tests/
├── deployment/
└── docs/
```

阶段一和阶段二采用单体仓库和单个部署单元，但保留清晰的代码边界；只有平台规模和部署隔离需求出现后，才评估拆分微服务。

## 15. 建议的数据对象

- `RepositoryInstallation`：GitHub App 安装和仓库配置。
- `WebhookDelivery`：Webhook 投递 ID、事件类型、验签结果、接收时间和处理状态。
- `ReviewTask`：可领取的审查任务、租约、重试、优先级和失败原因。
- `OutboxEvent`：与业务状态同事务写入、等待可靠分发的内部或外部事件。
- `ExternalActionRecord`：幂等键、外部对象 ID、请求版本、执行状态和最后响应。
- `PullRequestSnapshot`：PR、base SHA、head SHA 和变更快照。
- `ReviewRun`：一次审查执行及其执行状态、审查结论、覆盖状态、模型和耗时。
- `ReviewUnitSnapshot`：审查单元包含的变更文件、上下文文件、规则版本和风险域。
- `ReviewCoverageItem`：每个变更文件的处理去向、覆盖结果和未覆盖原因。
- `ReviewFinding`：问题、证据、严重度、指纹、提交定位、复核和发布状态。
- `ReviewFeedback`：有效、误报、重复、范围外、已修复、暂不修改及操作者。
- `ModelInvocation`：模型、Prompt、Token、费用、延迟、重试和响应校验结果。
- `CiCheckSnapshot`：当前提交对应的 CI 结果。
- `PromotionRequest`：测试候选分支创建和审批状态。
- `NotificationRecord`：飞书消息 ID、接收者和更新状态。
- `MemberIdentityMapping`：GitHub 用户与飞书 `open_id` 映射。
- `RepositoryPolicy`：敏感路径、阈值、模型和通知规则。
- `KnowledgeDocument`：文档版本、脱敏状态和索引元数据。

## 16. 安全和可靠性要求

### 16.1 GitHub 权限

GitHub App 使用最小权限：

```text
Pull requests: Read
Metadata: Read
Checks: Write
Commit statuses: Read/Write（按实际方案）
Contents: Read
Contents: Write（仅确实需要创建测试分支时）
```

`master` 继续启用分支保护，GitHub App 不应拥有绕过主线保护的权限。

### 16.2 Webhook 和凭据

- 校验 GitHub 和飞书 Webhook 签名。
- GitHub 验签必须基于未经重新编码的原始请求体，验签成功后再解析 JSON。
- 使用 `X-GitHub-Delivery` 唯一约束去重，并保存 `installation_id`、`repository_id`、PR 编号和事件中的 `head_sha`。
- GitHub 私钥、飞书 App Secret 和模型密钥放在服务端 Secret 中。
- 不把凭据写入仓库、日志、Prompt 或向量库。
- Webhook 在同一事务中持久化投递记录、任务和 Outbox 后快速确认，耗时审查由 Worker 异步执行。
- 阶段二测试期直接通过 `niuma-2` 公网 IP 的 `18090` 端口接收 HTTP Webhook；地址只保存在 GitHub App 和部署环境配置中，不把真实 IP 写入仓库。
- 即使测试期暂缓 HTTPS 和主机级加固，也必须限制事件类型和请求体大小；公网入口不得暴露管理 API、OpenAPI 文档、PostgreSQL 或 Worker 控制接口。

### 16.3 Prompt Injection

PR 代码、注释、README 和文档都视为不可信输入：

- 代码中的“忽略规则”“调用某工具”等文本不能改变系统权限。
- Agent 工具使用明确白名单。
- 审查 Agent 默认只有读取权限。
- 发送给模型前过滤密钥、Token 和敏感配置。
- 模型输出必须经过 Pydantic 结构校验和策略校验。

### 16.4 副作用与幂等

LangGraph 恢复、网络重试或消息重复可能导致节点再次执行。外部动作使用两级标识：

```text
action_key = {installation_id}:{repository_id}:{pr_number}:{head_sha}:{action_type}:{action_target}
request_idempotency_key = {action_key}:{action_revision}
```

`action_key` 唯一标识一个逻辑外部对象，例如 AI Check、某个 Finding 指纹对应的行内评论、某个接收人的飞书卡片或测试候选分支；`action_target` 用于区分同类动作的多个目标。`action_revision` 标识该对象的内容版本，数据库分别对逻辑对象键和请求幂等键建立唯一约束。创建成功后保存 GitHub Check Run ID、评论 ID、飞书 Message ID 或候选分支 SHA；新版本更新同一个逻辑对象，重试同一版本不重复发送。

需要幂等保护的操作包括：

- 创建或更新 GitHub Check。
- 发布或更新 PR 评论。
- 创建测试候选分支。
- 触发部署。
- 发送或更新飞书消息。
- 处理飞书审批回调。

任何自动晋级动作执行前都必须再次读取 GitHub 当前状态，确认 PR 仍然打开且 `head_sha` 没有变化。

业务状态变更和 Outbox 事件必须在同一数据库事务中提交。调用外部 API 不能与本地数据库形成一个假设的分布式事务；发生“外部成功、本地回写失败”时，通过幂等键和外部对象查询完成对账恢复。

### 16.5 GitHub Actions 安全

- 不在带写权限和密钥的工作流中直接执行不可信 PR 代码。
- 谨慎使用 `pull_request_target`，避免检出 PR 代码后暴露写权限 Secret。
- 普通 PR CI 使用只读权限。
- 自动创建测试分支的可信任务在 CI 和 AI Check 完成后执行，并再次验证提交 SHA。

### 16.6 阶段二审查执行边界

阶段二的平台只读取 Git diff、源码、测试和白名单文档，不在审查进程中执行 Maven、npm、项目脚本或 PR 提供的可执行文件。编译、测试和静态分析仍由权限受限的 GitHub Actions 完成。

- Agent 工具只允许读取任务工作目录和执行白名单 Git 查询。
- 所有文件路径在读取前规范化，并拒绝越出仓库根目录。
- 对文件大小、总上下文、工具调用次数、并发、Token 和执行时间设置上限。
- `niuma-2` 首版只启动一个 Worker 执行槽；Agent API 和 Worker 设置容器资源及日志轮转上限，防止异常 PR 持续占用整台测试机。
- 阶段二不要求为每个 PR 建立独立容器或外部审查服务。
- 后续只有在平台需要自行执行 PR 代码或构建测试环境时，才引入任务级运行隔离。

### 16.7 上下文预算和自动压缩

大上下文模型降低了切分压力，但不能替代上下文治理。过多无关代码仍会稀释证据、增加延迟和费用，并可能降低长上下文中间位置的召回。阶段二采用以下分层策略：

1. 先过滤二进制、生成文件、压缩文件和当前风险域明确无关的内容，并保留处理原因。
2. changed diff 原则上保留原文；大文件按变更区块及其所在方法、类或模板片段切分。
3. 按 Review Unit 分离前端、数据库、鉴权等上下文，优先加入直接调用方、被调用方、接口、数据模型和测试。
4. 对未修改的周边文件提取符号、字段、约束和相关代码段，删除无关方法和样板内容。
5. 仍然超限时，才对未修改上下文生成带文件、符号和来源范围的结构化摘要；不得把摘要当作最终缺陷证据。
6. 结论复核必须重新读取原始文件和对应代码范围，确认后才能发布行内评论。
7. 保存压缩清单、被省略内容、Token 估算和覆盖率；按 `blob_sha` 缓存结构提取与摘要结果。

阶段二的默认资源策略如下，全部允许按仓库配置覆盖：

| 项目 | 默认策略 |
|---|---|
| 变更文件数 | `<= 100` 正常审查；`101～300` 分批进入大型 PR 模式；`> 300` 需要大型 PR 策略或人工确认 |
| 单次模型输入 | 不超过模型标称上下文的 `60%～70%`，为系统指令、工具结果和输出保留空间 |
| 单 PR 总输入 | 初始上限为模型上下文的约 `4` 倍，允许多个 Review Unit 分批调用 |
| 模型调用次数 | 单 PR 默认最多 `30～40` 次，包括复核和重试 |
| 节点重试 | 同一节点默认最多重试 `2` 次，使用退避并区分可重试错误 |
| 审查耗时 | 不含等待 CI，目标 P95 小于 10 分钟，硬上限 20～30 分钟 |
| 费用 | 初期用 Token 和调用次数限额；积累真实数据后以历史 P95 成本约 `2` 倍设置单 PR 上限 |

文件阈值用于调度、公平性和异常保护，不代表超出后可以静默漏审。平台还应设置单仓库每日和全平台每日预算，防止 Webhook 重放、频繁推送或异常循环持续消耗资源。

### 16.8 运行结论和异常状态

审查结果不能只使用“通过/失败”。平台分别记录：

```text
execution_status  = QUEUED | WAITING_FOR_CI | RUNNING | COMPLETED | FAILED | TIMED_OUT | CANCELLED | SUPERSEDED
review_conclusion = NO_CONFIRMED_FINDINGS | FINDINGS_PRESENT | NEEDS_HUMAN | INDETERMINATE | NOT_APPLICABLE
coverage_status   = COMPLETE | PARTIAL | UNKNOWN | STALE
```

典型状态映射：

| 场景 | execution_status | review_conclusion | coverage_status |
|---|---|---|---|
| 完整执行且无确认问题 | `COMPLETED` | `NO_CONFIRMED_FINDINGS` | `COMPLETE` |
| 完整执行且发现问题 | `COMPLETED` | `FINDINGS_PRESENT` | `COMPLETE` |
| 拆分和压缩后仍有内容无法审查 | `COMPLETED` | `NEEDS_HUMAN` | `PARTIAL` |
| 模型超时、限流或结构校验重试失败 | `FAILED` 或 `TIMED_OUT` | `INDETERMINATE` | `UNKNOWN` |
| 新提交使当前运行过期 | `SUPERSEDED` | `NOT_APPLICABLE` | `STALE` |

证据不足是 Finding 级状态：该问题标记为 `UNVERIFIED`，不得发布行内评论，可以进入 Summary 的“需要人工确认”区域；只要其他文件已完整处理，它本身不必导致整个运行失败。上下文超限、模型失败和 CI 结果缺失都不得显示为“AI 审查通过”。

阶段二的 AI Check 不配置为主分支必需检查。完整且无确认问题可使用 GitHub `success`，发现问题或部分覆盖使用 `neutral`，平台失败使用 `failure`，超时使用 `timed_out`，被新提交替代使用 `cancelled`；Check 标题和 Summary 必须同时展示风险结论与覆盖状态。

## 17. 文档和知识库策略

飞书文档可以同时作为：

1. 审查 Agent 的业务知识来源。
2. 当前规则版本和审查结论的依据。

但不能把整个飞书空间无差别写入向量库。

建议流程：

```text
文档白名单
  -> 拉取指定版本
  -> 敏感信息扫描和脱敏
  -> 按标题、表格、接口章节切分
  -> 保存文档版本和来源链接
  -> 向量索引
  -> 检索后附带引用
```

特别注意：`6.1 配置说明` 可能包含连接信息或其他敏感配置，只允许经过明确白名单和脱敏的内容进入模型上下文。

业务文档、代码和用户最新指令不一致时，Agent 应报告冲突，不得静默选择其中一种。

## 18. 评测和效果指标

代码审查 Agent 不能只展示几个成功案例，需要建立可重复评测。

### 18.1 离线评测集

- 从历史 PR 和真实缺陷中整理样本。
- 给每个样本标注应发现问题和不应报告的问题。
- 覆盖正常代码、真实缺陷、诱导性注释和无关改动。
- 固定代码版本、规范版本、Review Unit 规划版本和期望结果。
- OpenCodeReview 的 AACR-Bench 等公开数据集只用于补充通用缺陷样本和借鉴评测方法，不能替代 NiuMa 自身的业务与权限样本。

### 18.2 核心指标

- 有效问题采纳率。
- 已报告问题中的误报占比。
- 已知缺陷召回率。
- 高风险问题精确率。
- 变更文件和关键行为的审查覆盖率。
- 文件、行号和证据定位准确率。
- 重复问题比例。
- 平均审查耗时和 P95 耗时。
- 单次 PR 模型成本。
- 工作流失败和恢复成功率。
- 飞书消息送达率及重复消息率。
- 自动测试候选创建成功率。

这里的高风险问题精确率定义为：

```text
高风险问题精确率 = 已确认有效的高风险问题数 / 已完成人工裁决的高风险问题数
已报告问题误报占比 = 已确认误报数 / 已完成人工裁决的问题数
```

“准确率”不适合衡量代码审查问题，因为没有被报告的正常代码数量巨大且无法完整标注。报表必须同时显示分子、分母和未裁决数量，不能只显示百分比。

### 18.3 反馈闭环

开发者在 GitHub 或飞书中选择：

```text
有效问题
误报
重复问题
范围外问题
已知问题
建议但暂不修改
已经修复
```

反馈用于评测和规则优化，但不能未经审核直接改写系统 Prompt 或高风险策略。

统计时采用以下口径：

- `有效问题`必须同时满足事实判断成立，并且与当前改动或当前改动触发的规则风险相关；`已知问题`以及确认成立但选择暂不修改的问题仍计为有效发现。
- `误报`表示事实判断不成立，不能把“当前不修”或“优先级低”混为误报。
- `范围外问题`表示事实可能成立，但与当前改动无关，不计为有效发现；`重复问题`也不重复计为有效发现。两者与事实误报分别统计，并共同进入体验噪声指标。
- 未回复、仍在讨论或证据不足的问题属于未裁决样本，不进入精确率和误报占比的分母。
- 指标按仓库、风险域、规则版本、Prompt 版本、模型版本和严重度分组；全局平均值只用于概览。

### 18.4 行内评论准入和自动降级

单条问题只有同时满足以下条件才具备行内评论资格：

- 统一审查器和结论复核器都确认问题成立，`verification_status` 为 `VERIFIED`。
- 存在可复查的原始代码证据、具体触发条件和明确行为影响。
- 位置能够稳定锚定到当前 `head_sha` 的 diff 行。
- 不是纯风格意见、一般改进建议、普通测试缺口或仅凭缺少上下文产生的疑点。
- 经过历史样本校准后的置信度达到仓库策略阈值，初始建议为 `>= 0.90`。

模型输出的置信度只是一项特征，不能绕过证据和位置条件。系统级准入使用人工裁决样本：初始样本不足时，高风险候选先进入 Summary 或人工预览；累计至少 30 条已裁决高风险问题后，样本精确率达到 `>= 90%` 且 Wilson 置信区间下界达到 `>= 80%`，才自动启用对应风险域的行内评论。

线上使用滚动窗口监控误报，初始策略如下：

| 条件 | 动作 |
|---|---|
| 最近窗口误报占比 `<= 10%` | 正常发布满足单条门槛的行内评论 |
| 误报占比 `> 10%` 且 `<= 20%` | 告警、提高该风险域置信度门槛并增加人工抽检 |
| 最近至少 30 条已裁决问题中误报占比 `> 20%` | 对应风险域自动降级为仅 Summary |
| 最近至少 30 条已裁决高风险问题中精确率 `< 80%` | 即使事实误报不高，也因重复或范围外噪声降级为仅 Summary |
| 连续出现可能误导安全或业务决策的严重误报 | 不等待样本窗口，立即关闭对应规则的行内发布 |

降级按风险域或规则执行，不因低质量测试建议关闭准确的鉴权评论。恢复行内发布必须经过规则、Prompt 或模型版本修正，重新跑离线评测，并通过一批新的人工抽检；使用恢复门槛和降级门槛之间的滞回区间，避免频繁开关。

## 19. 分阶段建设方案

### 阶段一：PR CI 和规范基线

- 为 NiuMa 增加后端、前端和架构检查。
- 建立 PR 模板、CODEOWNERS 和分支保护建议。
- 将可机械判断的 AGENTS/Flyway 规则转换成测试或脚本。
- 输出统一 GitHub Check。

验收条件：任何 PR 都能得到稳定、可重复的非 AI 检查结果。

### 阶段二：单 Agent 非阻塞审查

- 审查入口限定为 GitHub Pull Request，覆盖 Java、XML、SQL、Vue 和 TypeScript 常见文件。
- 建立 GitHub App 和 Webhook。
- 使用 PostgreSQL `WebhookDelivery`、`ReviewTask` 和 Outbox 建立可靠任务投递，支持去重、租约、重试和恢复。
- 使用 PostgreSQL 保存业务状态和 LangGraph Checkpoint，明确两者的状态权威边界。
- 获取 PR diff、周边代码、测试和 CI 结果。
- 实现 CI 事件驱动等待、新提交失效和 `head_sha` 一致性校验。
- 实现确定性文件筛选、Review Unit 构建和规则路由。
- 使用 DeepSeek V4 Flash 作为线上主模型，通过可替换接口接入 Python + LangGraph 统一审查器和结构化证据复核，不接入外部审查引擎。
- 采用“小步实现、小步部署”：先在 `niuma-2` 验证 API 健康检查和 PostgreSQL 迁移，再部署本地 dry-run、Webhook 任务链路、模型调用和 GitHub Check，每一步都保留可回退的容器版本。
- 默认发布 Check Summary；达到准入门槛的风险域可以发布高置信行内评论，但 AI Check 不阻止合并。
- 收集人工反馈和误报数据。

验收条件：真实 PR 能得到带提交定位、文件、行号、证据和测试建议的报告；重复 Webhook 不重复执行，新提交会使旧运行失效，模型失败或覆盖不完整会得到明确的非确定状态。

### 阶段三：专业审查器和知识库

- 根据阶段二评测结果完善改动分类和 Review Unit 规划。
- 只对确有收益的风险域拆分架构、鉴权、数据库、业务和测试审查器。
- 接入经过脱敏的飞书业务文档。
- 完善跨提交问题指纹、增量结果复用和旧结论对比展示；旧运行失效机制已在阶段二建立。

验收条件：不同改动只调用相关审查器，结果可引用对应规范和文档版本。

### 阶段四：飞书和人工介入

- 创建飞书自建应用。
- 建立 GitHub 与飞书成员映射。
- 支持交互式卡片、误报反馈和复审。
- 使用 LangGraph Interrupt 暂停并从飞书回调恢复。

验收条件：高风险问题能准确通知负责人，人工操作能可靠恢复原审查线程。

### 阶段五：测试候选自动晋级

- 实现确定性 Policy Engine。
- 创建每 PR 临时测试候选分支。
- 校验 CI、AI 结论、敏感路径、标签和 SHA。
- 接入隔离测试环境和部署结果。
- 保留主分支人工审批。

验收条件：符合条件的 PR 能自动进入隔离测试阶段，提交变化或敏感改动会可靠熔断。

### 阶段六：平台化和展示

- 增加 Vue 管理台。
- 增加运行图、成本、效果和错误看板。
- 支持多个仓库和每仓库策略。
- 完善部署、演示数据、架构文档和评测报告。

验收条件：新仓库通过安装 GitHub App 和填写策略即可接入。

## 20. 简历表达建议

项目名称可以暂定为：

```text
基于 LangGraph 的 AI 代码审查与测试晋级平台
```

简历描述可以围绕真实闭环展开：

- 基于 LangGraph 构建可持久化的 PR 审查状态图，将确定性静态检查与架构、鉴权、数据库和测试缺口等语义审查结合。
- 设计确定性 Review Unit 规划，将关联代码、测试和项目规则组成受控上下文，提升大 PR 的文件覆盖率和审查稳定性。
- 集成 GitHub App、Checks API 和 GitHub Actions，实现 PR 增量审查、问题指纹去重、提交一致性校验和测试候选分支自动晋级。
- 接入飞书开放平台，通过 CODEOWNERS、业务域规则和成员映射自动路由问题，并利用交互式卡片完成误报反馈、人工审批和工作流恢复。
- 基于 PostgreSQL、pgvector 和飞书文档构建带版本引用的规范检索，结合证据复核节点降低无依据审查结论。
- 建立历史 PR 离线评测集，统计采纳率、误报率、缺陷召回率、审查耗时和模型成本，形成可量化优化闭环。

不要把重点写成“调用了多个大模型”或“使用了很多 Agent”。真正有价值的是可恢复、可审计、能反馈、能评测、能安全触发后续动作的完整工程系统。

## 21. 待确定事项

1. 项目正式名称和 GitHub 仓库名称。
2. 项目是否公开，以及演示时使用真实仓库还是脱敏样例仓库。
3. DeepSeek V4 Flash 的具体服务端点、鉴权方式、标称上下文、价格版本和初始预算；阶段三再选择 Embedding 模型。
4. GitHub App 是只安装到 NiuMa，还是从一开始支持多仓库安装。
5. `niuma-2` 的测试入口何时从直接 IP HTTP 切换为域名和 HTTPS，以及何时增加专用部署账号、主机防火墙和数据备份。
6. 飞书自建应用的创建、审批和权限范围。
7. 阶段三先按白名单实时读取飞书文档，还是同步后建立向量索引。
8. NiuMa 哪些规则先转为确定性 CI，哪些保留给 Agent。
9. 阶段五是否创建独立的每 PR 测试环境，还是先只创建候选分支不自动部署。
10. 从哪些历史 PR 和已修复问题开始制作评测集。
11. NiuMa 阶段二的 Review Unit 使用哪些确定性归组规则，以及单元大小和并发上限。
12. 原生审查达到什么评测阈值后继续优化，低于什么阈值时才重新评估外部审查引擎。
13. 从 NiuMa 历史 PR 统计文件数、Token 估算、CI 耗时和问题分布，用于校准阶段二的默认预算。
