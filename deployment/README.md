# API 与 PostgreSQL 部署

这是 `niuma-2` 的 M1 部署边界。Compose 运行以下组件：

- `postgres`：OpenReviewer 独享的 PostgreSQL 16，不映射宿主机端口；
- `migrate`：每次发布前执行 `alembic upgrade head`，成功后退出；
- `api`：健康检查与内部审查任务创建接口，仅绑定服务器本机。

当前仍不包含 Worker、GitHub Webhook、模型调用或公网入口。任务创建成功后会保持
`queued`，直到后续版本加入 Worker。

## 镜像规则

GitHub Actions 会发布两个 OpenReviewer 标签：

- `ghcr.io/lboverfys/openreviewer:<完整 commit SHA>`：不可变的部署标签；
- `ghcr.io/lboverfys/openreviewer:main`：只用于查看的可移动标签。

服务器必须使用完整 commit SHA 标签。PostgreSQL 部署时也应记录实际镜像摘要，避免后续
无法确认运行版本。

## 配置和凭据

服务器专用 `.env` 至少包含：

```dotenv
OPENREVIEWER_IMAGE=ghcr.io/lboverfys/openreviewer:<完整-commit-sha>
OPENREVIEWER_POSTGRES_IMAGE=postgres:16.10-bookworm
OPENREVIEWER_POSTGRES_PASSWORD=<服务器生成的强随机密码>
OPENREVIEWER_HOST_PORT=18090
OPENREVIEWER_ENV=test
OPENREVIEWER_LOG_LEVEL=INFO
```

`.env` 必须设置为仅 root 可读，不得提交到 Git。数据库密码只用于 OpenReviewer 自己的
PostgreSQL，不能复用 NiuMa 或其他服务的密码。

配置校验必须使用 `docker compose config --quiet`。不要在日志或共享终端中输出完整的
`docker compose config`，因为 Compose 会把 `.env` 中的数据库密码展开到解析结果里。

## 发布步骤

1. 创建独立发布目录，例如 `/opt/openreviewer/releases/<commit-sha>`；
2. 上传 `compose.yml` 并创建权限为 `0600` 的服务器 `.env`；
3. 执行 `docker compose config --quiet`；
4. 拉取固定 SHA 的 OpenReviewer 镜像和 PostgreSQL 镜像；
5. 先启动 PostgreSQL，并确认健康检查通过；
6. 执行一次性迁移容器，确认退出码为 `0`；
7. 启动 API 并验证健康状态；
8. 从服务器本机调用 `/healthz`，再使用测试请求验证任务确实写入数据库。

Compose 使用固定名称 `openreviewer-postgres-data` 保存数据库数据，因此更换发布目录不会
创建一套空数据库。发布流程只做向前迁移，不自动降级数据库，也不自动删除数据卷。

## 网络边界

API 当前仅监听：

```text
127.0.0.1:18090
```

PostgreSQL 只连接内部 `backend` 网络，没有宿主机端口。API 同时连接 `backend` 和
`edge` 网络；`edge` 用于把 API 端口发布到宿主机回环地址，不会改变只监听
`127.0.0.1` 的限制。可以通过 SSH 在服务器内部验证：

```shell
curl --fail http://127.0.0.1:18090/healthz
```

在 GitHub Webhook 实现原始请求体验签、事件白名单、请求体大小限制和投递去重之前，不得
把 `18090` 暴露到公网。将来开放 Webhook 时，`/api/v1/reviews` 仍应保持内部可见。
