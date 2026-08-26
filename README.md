# OpenReviewer

OpenReviewer 是独立的 AI 代码审查编排平台，首个接入项目是 NiuMa。

当前仓库已完成从 GitHub 提交到人工发布审查结果的可恢复闭环：除 PostgreSQL 任务、单并发
Worker、管理员登录、实时 Dashboard、React 管理前端和 Webhook 安全入口外，Worker 使用
GitHub App 短期身份读取 PR、完整 diff 和当前提交 CI，并安全处理轮询、超时和新提交淘汰。
`AGENTS.md` 规则批量加载和全量 Review Unit 规划后，安全、规范、逻辑三个 Agent 并行审查，
汇总 Agent 再生成最终候选；四路都有独立模型配置、可恢复批次、版本化 Markdown RAG 和严格
结构化输出。模型结果停在人工批准门，批准后还需单独点击发布，API 才会把幂等汇总评论写入
仍绑定同一 SHA 的 GitHub Pull Request。

## 目录

```text
apps/api/           登录、任务和实时 Dashboard API
apps/worker/        单并发数据库 Worker 与心跳健康检查
domain/             审查领域模型和稳定枚举
persistence/        SQLAlchemy 数据模型、队列与查询适配器
services/           认证、任务和管理用例
migrations/         Alembic 数据库迁移
web/                React + TypeScript 管理前端和 Nginx 入口
tests/              单元测试与集成测试
docs/               架构和契约文档
deployment/         niuma-2 Compose 部署配置
```

## 开发边界

- Agent 服务与 NiuMa 业务服务独立部署、独立存储。
- Agent 只读取受限的 PR 上下文，不执行 PR 提供的脚本或构建命令。
- 不在仓库提交 Token、私钥、明文密码、密码哈希或真实部署配置。
- 当前 Worker 会在 CI 终态后自动规划并审查本次提交中所有可审查文件；超出单批上下文时
  自动按文件分批，单文件仍过大时按行切片，不用管理员设置“最多审查多少文件”。

## 已实现能力

- `POST /api/v1/reviews` 幂等创建 `ReviewRun`、`ReviewTask` 和 Outbox 事件。
- PostgreSQL `FOR UPDATE SKIP LOCKED` 单任务领取、租约续期、超时恢复、最多三次尝试和
  指数退避。
- Worker 启动、空闲、忙碌和停止心跳；Dashboard 可区分空闲与离线。
- Argon2id 管理员密码校验、HMAC 签名会话、HttpOnly/SameSite Cookie 和登录限流。
- 登录页可选调用浏览器密码管理器记住账号密码；应用不把明文凭据写入 localStorage。
- 受保护的 Dashboard、任务列表和 SSE 实时事件接口。
- React 登录页、实时状态卡、Worker 状态、最近任务和手工任务创建表单。
- Nginx HTTPS 源站和 Cloudflare 域名入口；API 仍只绑定服务器回环地址，PostgreSQL 不映射端口。
- 结构化任务错误、日志/数据库/API 统一脱敏和跨平台仓库路径边界校验。
- `POST /webhooks/github` HMAC-SHA256 验签、256 KiB 请求限制、PR 事件白名单和 delivery 幂等。
- GitHub installation、PR 版本、Webhook delivery 与外部动作审计数据模型。
- GitHub App JWT 与短期 installation token，Token 只缓存在 Worker 内存。
- 分页读取 PR changed files、完整 diff、Check Runs 和 Commit Statuses。
- 文件/CI 有界快照、CI 定时轮询与超时，以及旧 `head_sha` 批量失效保护。
- `AGENTS.md` 单请求批量加载、目录作用域、全量确定性 Review Plan，以及四表原子持久化和幂等复用。
- OpenAI Responses、OpenAI Chat Completions 与 Anthropic Messages 统一适配、严格 JSON Schema、
  上下文感知分批、模型调用审计、独立重试、Token/耗时/可配置成本和 Finding 原子持久化。
- 安全、规范、逻辑三路并发和汇总 Agent 固定 DAG；四路独立密钥、模型、协议、超时、推理档位
  与连接测试，任一路缺失都不会静默降级成单模型。
- Chat Completions 对明确的 400/422 可选参数不兼容执行受限降级，本地仍用 Pydantic 严格校验。
- 内置有界 Markdown 知识库，按 Agent 职责确定性召回并在详情页展示来源、标题和内容版本。
- `awaiting_approval -> awaiting_publish -> publishing -> completed` 人工门，以及发布前 PR SHA
  复核、稳定隐藏标记查重、评论大小限制和失败可重试。
- 管理界面动态保存 OpenAI/Anthropic 草稿、官方或中转站 API 地址、真实连接测试和单供应商
  激活；API Key 使用 AES-256-GCM 加密，OpenAI 可动态选择接口协议，Worker 按配置 revision
  在下一条任务生效。

## 当前前端入口

当前 `niuma-2` 测试环境的 React 管理前端地址为：

```text
https://openreviewer.lovecoding.store
```

公网通过 Cloudflare 访问，源站使用 Origin 证书。API 和 PostgreSQL 不直接对公网开放。
登录页勾选“记住账号密码”后，Chrome/Edge 等支持 Credential Management API 的浏览器会
把凭据保存到自己的密码库；证书尚未被浏览器接受或使用无痕窗口时，自动保存可能不可用。

## 本地启动

使用 Python 3.12 项目环境安装并迁移数据库：

```shell
python -m pip install -e ".[dev]"
python -m alembic upgrade head
```

本地配置需要数据库连接、管理员用户名、Argon2id 密码哈希、至少 32 字节的会话密钥和
至少 32 字节的 GitHub Webhook secret。
API 和 Worker 还需要同一份 32 字节 AI 配置加密主密钥。Worker 需要 GitHub App ID 和只读
私钥文件路径；API 也需要同一 App 身份用于人工发布。模型供应商、API 地址、模型 ID、API Key、
上下文窗口与调用边界在登录后的设置页分别为四个 Agent 保存。完整
配置见 [`docs/contracts/github-context.md`](docs/contracts/github-context.md) 和
[`docs/contracts/ai-settings.md`](docs/contracts/ai-settings.md)。固定 DAG 与发布语义分别见
[`docs/contracts/agent-workflow.md`](docs/contracts/agent-workflow.md) 和
[`docs/contracts/github-publishing.md`](docs/contracts/github-publishing.md)。
可以使用交互式输入生成哈希，明文不会写入命令历史：

```shell
python -c "from getpass import getpass; from argon2 import PasswordHasher; print(PasswordHasher().hash(getpass('Password: ')))"
```

分别启动 API 和 Worker：

```shell
python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 18090
python -m apps.worker.main
```

React 开发入口由 Vite 代理同源 API：

```shell
cd web
npm install --no-audit --no-fund
npm run dev
```

生产环境不运行 Vite，静态文件由 Web 镜像中的 Nginx 提供。

## 管理接口

除 `/healthz` 和经过 GitHub 签名验证的 `/webhooks/github` 外，管理接口都要求先登录：

- `POST /api/v1/auth/login`、`POST /api/v1/auth/logout`、`GET /api/v1/auth/me`；
- `GET /api/v1/dashboard`；
- `GET /api/v1/reviews`、`POST /api/v1/reviews`；
- `GET /api/v1/reviews/{review_run_id}`、`POST /api/v1/reviews/{review_run_id}/actions`；
- `GET /api/v1/reviews/stream`，使用 SSE 推送最新 Dashboard 快照；
- `GET/PUT/POST /api/v1/settings/ai/agents/...`，管理四个 Agent 的独立配置、连接测试和启停；
- `GET/PUT/POST /api/v1/settings/...`，保留旧单模型配置兼容和配置审计。

任务创建仍要求 `Idempotency-Key`。相同键和相同内容返回原任务；相同键但内容不同返回
`409 Conflict`。

详细契约见 [docs/contracts](docs/contracts/README.md)。

## 容器交付

GitHub Actions 使用 Python 3.12、隔离 PostgreSQL 16 和 Node.js 22.19 验证后端与前端，
并发布两个不可变镜像：

```text
ghcr.io/lboverfys/openreviewer:<完整 commit SHA>
ghcr.io/lboverfys/openreviewer-web:<完整 commit SHA>
```

服务器只能部署完整 SHA 标签，不能用可移动的 `main` 标签判断实际版本。Compose 启动
PostgreSQL、一次性迁移、API、Worker 和 Web；公网只开放 Web HTTPS 端口。

`main` 分支 push 会在测试和 GHCR 发布成功后自动部署到 `niuma-2`。部署通过 `niuma`
跳板机使用受限 SSH key 完成，服务器私有 `.env` 不离开服务器；失败版本不会切换
`current`，数据库迁移也不会自动回退。

完整服务器步骤见 [deployment/README.md](deployment/README.md)。

## 验证

```shell
python -m pytest -q -W error
python -m pip check
cd web
npm run typecheck
npm test
npm run build
```
