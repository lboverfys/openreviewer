# Agent 代码审查项目测试基线与实施指南

> **当前实现说明（2026-08-18）**：本文件主体记录的是早期 NiuMa 测试基线和规划，
> 其中出现的 `18081`/HTTP 入口属于历史设计，不是当前 OpenReviewer M2 管理前端入口。
> 当前已部署的 React 管理前端地址为
> `https://107.175.221.182:18443`，使用测试用自签名 HTTPS 证书；API 和 PostgreSQL
> 不直接对公网开放。当前部署细节以 [`deployment/README.md`](../../deployment/README.md)
> 为准。

本文记录为 Agent 代码审查项目准备 NiuMa 测试基线时已经完成的工作、当前可用的
验证链路、尚未实现的能力以及下一阶段的实施顺序。它首先澄清两个项目的边界：

- **NiuMa** 是被审查项目和真实 PR 测试场，用于持续产生功能改动、CI 结果、SQL
  迁移和部署反馈。
- **Agent 代码审查项目** 是接下来需要从零开发的独立系统，负责读取 PR diff、调用
  模型、生成结构化问题并把审查结果写回 GitHub。

截至 2026-08-18，NiuMa 测试场和自动化基础设施已经就绪，`openreviewer` 独立目录
及设计文档也已建立，Agent 测试部署服务器 `niuma-2` 已完成只读资源检查；项目尚未
初始化 Git、运行骨架或源码，也没有 webhook、模型调用或 PR 评论代码。不能把“测试场
和服务器准备完成”理解为“Agent 审查系统已经完成”。详细目标架构、技术选型和分阶段
能力见 [AI 代码审查编排平台方案](code-review-agent-design.md)。

## 1. 目标工作流

最终希望形成两条职责不同但互相补充的 PR 检查：

```text
NiuMa 功能分支
      |
      v
提交 Pull Request 到 master
      |
      +-----------------------+
      |                       |
      v                       v
PR CI                     Agent Review
编译、测试、类型检查       diff 分析、风险判断、审查意见
      |                       |
      +-----------+-----------+
                  |
                  v
           人工确认并合并
                  |
                  v
       Deploy Test 自动部署测试环境
```

`PR CI` 只能证明代码通过了预设的机械检查，不能替代代码审查。`Agent Review` 负责发现
逻辑错误、安全问题、并发风险、SQL 风险、架构越界和测试遗漏，但不应假装执行过编译或
运行测试。两个结果需要分别展示。

部署边界已经确定：NiuMa 业务和测试部署继续运行在 `niuma` 服务器，Agent Review
独立运行在 `niuma-2`。两台服务器不共享应用进程、部署目录或数据库，GitHub PR、Check
和 Actions 状态是两者之间的协作入口。Agent 不需要登录 `niuma` 读取运行目录，因此
拆分部署不会降低审查能力，也不会占用 NiuMa 服务器的内存。

## 2. 已完成的准备工作

### 2.1 独立目录、worktree 和分支

NiuMa 的审查实验使用 `openreview` 下的独立工作目录，不直接占用正在开发的工作区：

```text
openreview/
├── niuma-pr-ci-test/       test/openreview-pr-ci worktree
└── niuma-pr-ci-rollout/    ci/pr-ci-rollout worktree
```

已经维护并同步以下远程分支：

| 分支 | 用途 | 同步规则 |
| --- | --- | --- |
| `master` | 受保护的集成与测试部署基线 | 只通过通过检查的 PR 更新 |
| `ci/pr-ci-rollout` | CI/CD 配置开发与修复 | 当前文件树与 `master` 对齐 |
| `test/openreview-pr-ci` | PR CI 和后续 Agent Review 的受控验证 | 保留故障/恢复测试历史，文件树与 `master` 对齐 |
| `dev_lboverfys` | 真实功能开发 | 已同步基线，但后续开发者仍需在推送前拉取远程更新 |

`yunyu` 和 `wh` 分支没有纳入本轮操作。同步过程使用普通快进或合并，没有强推，也没有
丢弃其他工作区的未提交修改。

`test/openreview-pr-ci` 曾实际验证过失败和恢复链路：先提交可预期的 CI 失败，再恢复
绿色基线。该分支保留这段历史，因此它的提交 SHA 不要求和 `master` 相同；判断是否
同步应比较文件树，而不是只比较分支头 SHA。

### 2.2 真实功能和数据库基线

在建立自动部署前，`dev_lboverfys` 中的打手审核、网页推送、云录制与处罚执行闭环已经
通过 PR 合入 `master`。与这些功能对应的正式 Flyway 迁移已经应用到共享测试库，当前
已验证的最新正式版本为：

```text
20260817140100.01  add violation enforcement workflow
```

共享测试库还保留了过去由 `local` Profile 执行的 `R__seed_local_*.sql` repeatable
历史。测试部署使用 `prod` Profile，不会再次执行本地 seed；Flyway 只忽略这些缺失的
repeatable 记录，正式 `V*.sql` 的缺失、失败和校验和变化仍会阻止应用启动。

### 2.3 PR CI

[PR CI 工作流][niuma-pr-ci]在面向 `master` 的 PR 发生以下事件时
自动执行：

- `opened`；
- `synchronize`；
- `reopened`；
- `ready_for_review`。

后端检查使用 GitHub 托管的 Ubuntu 24.04 Runner、Temurin Java 21 和 Maven，执行：

```shell
mvn --batch-mode --no-transfer-progress -Pstrict-build verify
```

前端检查使用 Node.js 22.19.0，依次执行：

```shell
npm ci --no-audit --no-fund
npm run typecheck
npm test
npm run build
```

Ubuntu 24.04 是 GitHub 托管 Runner 的固定、可重复环境，不代表测试服务器必须运行
Ubuntu。Temurin 21 与项目的 Java 21 编译基线一致，避免开发机 JDK 或系统默认 Java
影响检查结果。

后端和前端完成后，汇总任务输出稳定的 `PR CI` 检查名。`master` Ruleset 已将它配置
为必过检查，并要求 PR 审核；管理员保留显式 bypass 能力，用于已经核实的受控操作，
不应把 bypass 当作日常合并流程。

### 2.4 测试环境持续部署

[Deploy Test 工作流][niuma-deploy-test]在 `master` 收到 push 后
自动执行，也支持人工 `workflow_dispatch`。完整顺序为：

```text
Verify Backend + Verify Frontend
              |
              v
        Publish Images
              |
              v
     Deploy To Test Server
```

部署不需要人在 GitHub Actions 完成后再登录服务器执行命令。Actions 在镜像发布成功后
通过 SSH 主动上传本次发布文件并执行脚本，服务器不使用轮询、Watchtower 或定时任务
猜测镜像是否变化。

工作流使用以下镜像标签：

```text
ghcr.io/lboverfys/niuma-backend:<完整 commit SHA>
ghcr.io/lboverfys/niuma-web:<完整 commit SHA>
```

同时维护便于查看的 `master` 标签，但服务器发布只拉取完整 SHA 对应的不可变镜像，
不会依赖可移动标签判断版本。

### 2.5 容器和入口

[后端 Dockerfile][niuma-backend-dockerfile]使用 Maven 3.9.11 与 Temurin 21 构建，在
Temurin 21 JRE 中以非 root 用户运行。[前端 Dockerfile][niuma-frontend-dockerfile]使用 Node.js
22.19.0 构建 Vue 静态文件，再由 Nginx 1.28.0 提供服务。

线上测试环境不运行 Vite 开发服务器，所以 Vite 默认的 `5173` 与当前入口无关：

```text
浏览器 :18081
    -> Docker 宿主端口映射
    -> 前端 Nginx 容器 :8080
       -> Vue 静态文件
       -> /api/** 转发后端 :8080
       -> /ws/** 转发后端 WebSocket
```

`18081` 沿用仓库原有 Nginx 源站端口约定。当前测试环境为直接 HTTP 访问，尚未配置
域名和 TLS；它不是正式公网入口方案。长期应由 HTTPS `443` 入口反向代理到仅对本机
或私网开放的 `18081`。

容器 Nginx 还负责 SPA fallback、静态资源缓存、CSP、安全响应头、登录限流、API 和
WebSocket 代理，并拒绝外部访问 Actuator、Knife4j 和 OpenAPI。

### 2.6 测试服务器

测试服务器已经具备以下运行条件：

- Debian 12；
- Docker 29.7.2；
- Docker Compose 5.4.0；
- 健康运行的 PostgreSQL 16、Redis 7 和 MinIO；
- 供应用连接基础设施的外部 Docker 网络 `niuma-dev_backend`；
- 专用部署账号和 Docker 权限；
- `/etc/niuma/application.env` 外部配置文件；
- `/opt/niuma/releases` 不可变发布目录。

服务器目录和回退边界详见[测试环境自动部署][niuma-test-deployment]。真实数据库
密码、Redis 密码、对象存储密钥和 SSH 私钥不进入仓库。

GitHub `test` Environment 已限制为只允许 `master` 部署，并配置以下加密 Secrets：

- `NIUMA_TEST_SSH_HOST`；
- `NIUMA_TEST_SSH_PORT`；
- `NIUMA_TEST_SSH_USER`；
- `NIUMA_TEST_SSH_PRIVATE_KEY`；
- `NIUMA_TEST_SSH_KNOWN_HOSTS`。

工作流使用当前运行的短期 `GITHUB_TOKEN` 登录 GHCR，完成拉取后退出，不在服务器
长期保存 Registry Token。

### 2.7 发布、健康检查和回退

[测试 Compose][niuma-test-compose]复用共享 PostgreSQL、Redis 和 MinIO，
只创建 NiuMa 前后端及应用内部网络。发布脚本执行以下步骤：

1. 使用文件锁串行化发布；
2. 校验完整 commit SHA 和 Compose 配置；
3. 拉取本次 SHA 的两个镜像；
4. 启动后端并由 Flyway 校验、执行正式迁移；
5. 等待后端 readiness 和前端健康检查；
6. 通过前端代理访问 `/api/public/ping`；
7. 写入镜像 digest 和 `release.info`；
8. 原子更新 `/opt/niuma/current` 符号链接。

候选版本失败时，脚本尝试恢复上一个健康镜像。首次发布没有旧版本时会停止失败候选，
避免留下无限重启容器。应用镜像可以回退，数据库结构不会自动降级，所以正式迁移必须
保持向后兼容。

### 2.8 Agent 服务部署目标和服务器评估

2026-08-18 已通过现有 SSH 主机别名对 `niuma-2` 完成只读检查，没有安装软件、创建
目录、拉取镜像或修改配置。检查结果为：

| 项目 | `niuma-2` 当前状态 |
| --- | --- |
| 系统 | Debian 13，x86_64 KVM，时间同步正常 |
| CPU 和内存 | 3 vCPU、约 3.8 GiB 内存、2 GiB Swap，检查时可用内存约 3.5 GiB |
| 磁盘 | 根盘约 62 GiB，已用约 4.6 GiB，可用约 57 GiB |
| Docker | Docker 26.1.5、Compose 2.26.1，服务已启动 |
| 现有工作负载 | 没有容器、镜像、数据卷，也没有 PostgreSQL、Redis 或 MinIO 服务 |
| 网络 | GitHub API、GHCR 和 DeepSeek API 域名可达；当前仅 SSH 端口监听 |
| Python | 主机 Python 3.13.5；项目将用容器固定 Python 3.12 |

该服务器适合外部模型 API 模式下的阶段二低并发 MVP。首版部署独立的 FastAPI API、
单并发 Worker 和 PostgreSQL，不复用 NiuMa 的数据库、Redis、MinIO 或 `/opt/niuma`
目录，也不在这台服务器执行 Maven、npm 或其他 PR 代码。Redis 仅在 PostgreSQL 任务表
无法满足锁、限流或短缓存需求时增加。

测试阶段采用以下入口：

```text
http://<niuma-2-public-ip>:18090/webhooks/github
```

真实 IP 不写入仓库，只放在 GitHub App 和服务器部署配置中。当前范围允许暂缓域名、
HTTPS、专用部署账号、主机防火墙和 SSH 加固，但这只是测试环境接受的临时风险；Webhook
原始请求体验签、事件白名单、请求体限制和 `X-GitHub-Delivery` 去重不能省略。公网只
提供 Webhook 和最小健康检查，管理 API、OpenAPI 文档、PostgreSQL 和 Worker 接口不
对外暴露。

## 3. 已验证的实际效果

以下链路不是只写了配置，而是已经运行验证：

| 验证项 | 结果 |
| --- | --- |
| PR 后端严格构建 | 通过 |
| PR 前端类型检查、188 项测试和生产构建 | 通过 |
| `PR CI` 必过检查 | 已阻止未满足检查的合并，并验证绿色恢复 |
| 后端和前端镜像发布到 GHCR | 通过 |
| Actions 使用专用 SSH 凭据连接服务器 | 通过 |
| 服务器拉取完整 SHA 镜像 | 通过 |
| Flyway 校验共享测试库 | 成功校验 30 条正式迁移 |
| 前后端容器健康检查 | 通过 |
| `/api/public/ping` 代理冒烟 | 返回业务成功码 `00000` |
| PostgreSQL、Redis、MinIO 健康状态 | 通过 |

首次 `Deploy Test` 暴露了共享测试库含 local repeatable 历史的问题，部署按失败退出，
没有把不健康版本标记为 current。随后通过 PR 修正测试环境 Flyway 策略和首次失败清理，
第二次 `Deploy Test` 的四个 Job 全部成功。这次失败和修复证明健康门禁确实参与发布，
而不是工作流无条件显示成功。

现在 `master` 每次变化都会自动完成：

```text
重新验证 -> 构建新镜像 -> 推送 GHCR -> SSH 发布 -> Flyway -> 健康检查 -> 冒烟
```

## 4. 当前明确没有实现的内容

截至 2026-08-18，OpenReviewer 已完成独立仓库、Python 3.12 项目骨架、PostgreSQL
持久化任务、单并发 Worker、租约恢复、管理员登录和 React 实时管理界面。Worker 当前会
把任务可靠地推进到 `waiting_for_ci`，不会把后续未实现节点伪装为成功。

以下能力仍未实现，属于后续 Agent 代码审查闭环，而不是 NiuMa 的 CI/CD：

- 没有 GitHub App、webhook 接收端或 webhook 签名校验；
- 没有 GitHub installation token 获取和权限最小化配置；
- 没有 PR 元数据、changed files、patch 或完整 diff 获取逻辑；
- 没有大 diff、二进制文件、生成文件和截断 patch 的处理策略；
- 没有仓库规则、`AGENTS.md` 和关联上下文装载；
- 没有模型调用、提示词版本、结构化输出 Schema 或结果校验；
- 没有把审查结果发布为 Check Run、PR Review 或 inline comment；
- 没有增量审查、旧评论消解、重复评论抑制和 stale SHA 防护；
- 尚未实现 GitHub API 和模型 API 的分类重试、全链路审计日志与成本统计；
- 没有真阳性、误报率、可执行性和审查耗时的评测数据集。
- `niuma-2` 已运行 M1 API 与 PostgreSQL；M2 Worker 和 Web 管理入口完成本地验证后再按
  完整 commit SHA 发布。

现有 `PR CI` 不会调用 AI，也不会产生 Agent 审查意见。后续 NiuMa 功能 PR 可以作为
真实评测输入，但必须先实现 Agent 审查系统的最小闭环。

## 5. 下一阶段实施路线

### 5.1 阶段一：初始化独立项目和审查契约

Agent 审查系统已经在 `openreview/openreviewer` 建立独立兄弟目录，下一步是在这里
初始化独立 Git 仓库和项目骨架。源码不得写入 `niuma-pr-ci-test` 或
`niuma-pr-ci-rollout`，NiuMa 克隆只作为被测仓库。

[主设计方案](code-review-agent-design.md)已经确定 Python + LangGraph、GitHub App、
PostgreSQL 可靠任务和可替换模型适配器等主线。实现时应把这些决策落实为依赖、配置和
可运行边界；如果后续需要改变技术栈，应先修订设计依据，不能在代码中静默偏离。

实现和部署采用同一条小步链路：先完成可启动的 API 和健康检查并部署到 `niuma-2`，
再依次加入 PostgreSQL 迁移、本地 dry-run、Webhook 入库、Worker、模型调用和 GitHub
Check。每一步先在本地通过测试，再更新服务器容器；不等所有阶段一次性完成后才首次
部署，也不把尚未实现的节点伪装成空成功。

第一阶段需要先固定以下契约：

- 支持的 GitHub 事件：`pull_request` 的 opened、synchronize、reopened、
  ready-for-review；
- 一次审查的唯一键：仓库、PR 编号和 head SHA；
- 审查结果字段：规则 ID、严重程度、文件、行号、标题、证据、影响和建议；
- Check Run 生命周期：queued、in_progress、completed；完成结论：success、neutral、
  failure 等；
- 模型错误、GitHub API 错误和“未发现问题”的不同语义；
- 允许读取和写入 GitHub 的最小权限。

严重程度不能只由模型输出自由文本，应使用稳定枚举，例如：

```text
critical  可导致严重安全、资金或不可恢复数据问题
high      明确的功能错误、权限绕过、数据破坏或高概率生产事故
medium    有条件触发的错误、架构违规或明显测试缺口
low       低风险缺陷或值得修正的可维护性问题
```

纯格式偏好不应默认发布为 PR 阻断问题。

### 5.2 阶段二：打通最小可运行闭环

第一个可运行版本只需要完成一条纵向链路：

```text
接收并验签 webhook
        -> 创建幂等审查任务
        -> 获取 PR base/head 和 changed files
        -> 构造受限审查上下文
        -> 调用一个模型
        -> 校验结构化 findings
        -> 发布一个 Check Run 摘要
        -> 对可精确定位的问题发布 inline comment
```

这一阶段应优先保证结果可追踪和可重放，不急于引入多 Agent、自动修复、向量数据库或
复杂工作流编排。每次运行至少记录模型、提示词版本、head SHA、输入摘要、输出 findings、
耗时和 token/成本；日志不得保存 GitHub Token、webhook Secret 或其他凭据。

首版在 `niuma-2` 只启用一个 Worker 执行槽。GitHub 使用直接 IP 的 HTTP `18090`
端口投递 Webhook；服务器只接收允许的事件并快速返回，模型审查由 Worker 异步执行。
NiuMa 编译和测试继续由 GitHub Actions 完成，Agent 服务不直接连接 `niuma` 服务器执行
构建、部署或读取业务数据库。

### 5.3 阶段三：正确处理 PR 生命周期

GitHub PR 会随着新提交不断变化，审查系统必须围绕 head SHA 工作：

- 新 synchronize 事件到达后，旧 SHA 的运行不得覆盖新结果；
- 同一个 delivery ID 和同一个 head SHA 重试时保持幂等；
- 已修复的问题应标记 resolved 或从新摘要中消失，不能无限追加重复评论；
- 无法精确定位到 diff 行的问题只进入汇总，不伪造 inline comment；
- GitHub patch 被截断时应改用 blob 或 compare API 补充，而不是让模型基于残缺代码下结论；
- fork PR 和不可信代码不得获得仓库 Secrets。

### 5.4 阶段四：使用 NiuMa PR 做真实评测

后续 NiuMa 功能继续在功能分支实现并提交 PR。每个 PR 同时保留：

- 人工预期或事后确认的问题；
- `PR CI` 结果；
- Agent findings；
- 人工对每条 finding 的 true positive、false positive、重复或无价值标注；
- 修复提交和 Agent 下一轮是否正确消解问题。

至少统计以下指标：

| 指标 | 含义 |
| --- | --- |
| 精确率 | 发布的问题中有多少被人工确认 |
| 召回率 | 已知重要问题中有多少被 Agent 找到 |
| 高严重度误报率 | high/critical 中错误告警的比例 |
| 行号命中率 | inline comment 是否落在正确 diff 行 |
| 重复率 | 同一根因是否被多次发布 |
| 可执行性 | 建议是否足以指导修复而非泛泛而谈 |
| 增量一致性 | 修复后旧问题是否消失，新问题是否只针对新变化 |
| 时延与成本 | 从 synchronize 到结果完成的时间和模型成本 |

初期 Agent Review 应使用 shadow mode：结果可见但不作为 `master` 必过检查。积累足够
真实 PR 数据并证明高严重度误报可控后，再考虑把稳定的汇总检查加入 Ruleset。不能在
没有评测基线时让模型直接阻断所有合并。

### 5.5 阶段五：增强能力

最小闭环稳定后，再按实际瓶颈增加：

- 按语言和目录路由的规则包；
- 架构文档、数据库迁移和调用链上下文检索；
- 多阶段审查或多个专长 Agent；
- 静态分析、测试结果和模型 findings 关联；
- 误报反馈学习和仓库级规则配置；
- 审查仪表盘、趋势和成本预算；
- 经人工确认后生成修复建议或候选补丁。

自动提交修复、自动批准或自动合并不属于早期目标。这些动作会扩大权限和错误影响面，
只能在审查准确性、身份权限和审计能力都成熟后单独设计。

## 6. 下一步的具体产物

下一次开始编写 Agent 代码审查项目时，第一批产物应是：

1. 在现有 `openreviewer` 目录初始化 Git 仓库、基础 README 和源码/测试骨架；
2. 把已确定的 Python + LangGraph 技术主线落实为运行时版本、依赖清单和启动方式；
3. Python 3.12 Dockerfile 和面向 `niuma-2` 的 Compose 骨架，包含 API、单并发 Worker、
   PostgreSQL、内部网络、健康检查和日志限制；
4. GitHub App 权限、事件和 webhook 安全设计；
5. PR Review 领域模型与结构化 finding Schema；
6. 可本地重放固定 PR diff 的审查入口；
7. webhook 到 Check Run 的最小纵向实现；
8. 单元测试、契约测试和一个不写真实 PR 的 dry-run 模式；
9. 使用 NiuMa 测试 PR 的首批评测用例。

完成上述最小闭环后，才进入“通过真实功能 PR 检验 Agent 审查效果”的阶段。现有 NiuMa
分支、PR CI、共享数据库和测试部署会继续承担被测输入、机械验证和运行反馈，不需要
重新搭建。

## 7. 操作边界

- 不把 Agent 项目源代码写进 NiuMa 的两个测试 worktree；
- 不在仓库提交服务器地址、真实凭据、私钥、Token 或生产配置；
- Agent 服务不通过 SSH 读取 `niuma` 的运行目录，不复用 NiuMa 的数据库、Redis、MinIO
  或部署目录；代码和 PR 状态以 GitHub 为准；
- 测试期允许通过 `niuma-2` 直接 IP 的 HTTP `18090` 端口接收 Webhook，但必须验签、
  去重并限制事件和请求体；不得对公网暴露管理 API、OpenAPI 文档或数据库端口；
- 域名、HTTPS 和主机级加固可以在测试期暂缓，转为公开演示、长期运行或生产用途前必须
  重新评估并补齐；
- 不修改已经发布的 Flyway 迁移，只增加新版本；
- 不让 Agent Review 冒充编译、测试或安全扫描结果；
- 不依据可移动的 `master` 镜像标签判断实际部署版本；
- 不自动回滚数据库结构；
- 不在评测成熟前把 Agent Review 设置为必过检查；
- 不因同步分支而强推、覆盖或丢弃开发者工作区修改。

这组边界保证 NiuMa 可以继续正常开发功能，同时为 Agent 代码审查系统提供稳定、真实、
可重复验证的 PR 和部署环境。

[niuma-pr-ci]: https://github.com/lboverfys/NiuMa/blob/master/.github/workflows/pr-ci.yml
[niuma-deploy-test]: https://github.com/lboverfys/NiuMa/blob/master/.github/workflows/deploy-test.yml
[niuma-backend-dockerfile]: https://github.com/lboverfys/NiuMa/blob/master/Dockerfile
[niuma-frontend-dockerfile]: https://github.com/lboverfys/NiuMa/blob/master/niuma-web/Dockerfile
[niuma-test-deployment]: https://github.com/lboverfys/NiuMa/blob/master/deploy/test/README.md
[niuma-test-compose]: https://github.com/lboverfys/NiuMa/blob/master/deploy/test/compose.yml
