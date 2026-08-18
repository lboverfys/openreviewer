# OpenReviewer

OpenReviewer 是独立的 AI 代码审查编排平台，首个接入项目是 NiuMa。

当前仓库处于 M1 持久化入口阶段：已经固定 Python 3.12 运行时约定、第一版审查契约和可测试的领域模型，并实现可幂等创建异步审查任务的内部 API。Webhook、GitHub App、Worker、模型调用和 LangGraph 工作流将在后续阶段逐步加入。

## 目录

```text
apps/api/       API 与 Webhook 入口
domain/         审查领域模型
persistence/    SQLAlchemy 数据模型与数据库适配器
services/       应用用例与持久化边界
migrations/     Alembic 数据库迁移
tests/unit/     单元测试
tests/integration/ 适配器与迁移集成测试
docs/           架构和契约文档
```

## 开发边界

- Agent 服务与 NiuMa 业务服务独立部署、独立存储。
- Agent 只读取受限的 PR 上下文，不执行 PR 提供的脚本或构建命令。
- 不在仓库提交 Token、私钥、Webhook Secret 或真实部署配置。

## 当前状态

- 已初始化独立 Git 仓库。
- 已创建项目骨架和 Python 项目元数据。
- 已实现审查契约、Pydantic 领域模型和单元测试。
- 已实现不依赖外部服务的 FastAPI `/healthz` 健康检查。
- 已实现 `POST /api/v1/reviews` 内部任务创建接口。
- 已实现 PostgreSQL `ReviewRun`、`ReviewTask`、`OutboxEvent` 首个迁移。
- 同一幂等键重试不会重复创建任务，同键不同内容会返回冲突。

## 本地启动

激活项目专用 Python 环境后执行：

```shell
python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 18090
```

健康检查地址为 `http://127.0.0.1:18090/healthz`。当前版本不公开 OpenAPI、Swagger 或 ReDoc 页面。

## 创建审查任务

`POST /api/v1/reviews` 是内部管理接口，调用前必须配置 PostgreSQL，并先执行：

```shell
python -m alembic upgrade head
```

请求示例：

```http
POST /api/v1/reviews HTTP/1.1
Content-Type: application/json
Idempotency-Key: manual-review-001

{
  "installation_id": 10,
  "repository_id": 42,
  "repository": "lboverfys/NiuMa",
  "pull_request_number": 128,
  "head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

接口返回 `202 Accepted` 和持久化后的 `review_run_id`、`review_task_id`。同一个
`Idempotency-Key` 携带相同内容重试时返回原有 ID；携带不同内容时返回 `409 Conflict`。
当前还没有 Worker，因此任务会保持 `queued`，不会伪装成已经完成审查。

## 容器交付

GitHub Actions 会在 Python 3.12 测试通过后构建镜像，并发布到 GHCR。部署必须使用完整 commit SHA 对应的不可变镜像标签，不能使用可移动的 `main` 标签判断实际版本。

服务器部署说明见 [deployment/README.md](deployment/README.md)。Compose 会启动内部 PostgreSQL、一次性迁移容器和 API；API 仍只绑定服务器本机，尚未部署 Worker、Webhook 或模型调用。

## 验证

使用项目专用 Python 环境执行：

```shell
python -m pytest
```
