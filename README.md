# OpenReviewer

OpenReviewer 是独立的 AI 代码审查编排平台，首个接入项目是 NiuMa。

当前仓库处于 M0 契约阶段：已经固定 Python 3.12 运行时约定，并建立第一版审查契约和可测试的领域模型。Webhook、GitHub App、模型调用、数据库和 LangGraph 工作流将在后续阶段逐步加入。

## 目录

```text
apps/api/       API 与 Webhook 入口
domain/         审查领域模型
tests/unit/     单元测试
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

## 本地启动

激活项目专用 Python 环境后执行：

```shell
python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 18090
```

健康检查地址为 `http://127.0.0.1:18090/healthz`。当前版本不公开 OpenAPI、Swagger 或 ReDoc 页面。

## 容器交付

GitHub Actions 会在 Python 3.12 测试通过后构建镜像，并发布到 GHCR。部署必须使用完整 commit SHA 对应的不可变镜像标签，不能使用可移动的 `main` 标签判断实际版本。

首次服务器部署说明见 [deployment/README.md](deployment/README.md)。当前 Compose 仅将健康检查 API 绑定到服务器本机，尚未部署数据库、Worker、Webhook 或模型调用。

## 验证

使用项目专用 Python 环境执行：

```shell
python -m pytest
```
