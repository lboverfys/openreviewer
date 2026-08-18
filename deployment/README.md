# 健康检查部署

这是 `niuma-2` 的第一阶段部署边界。当前只运行 API 健康检查容器，不包含 PostgreSQL、Worker、GitHub 凭据、模型凭据或公网 Webhook 入口。

## 镜像规则

GitHub Actions 会发布两个标签：

- `ghcr.io/lboverfys/openreviewer:<完整 commit SHA>`：不可变的部署标签。
- `ghcr.io/lboverfys/openreviewer:main`：只用于查看的可移动标签。

服务器必须使用完整 commit SHA 标签，不能用 `main` 判断实际部署版本。

## 服务器边界

Compose 文件只把 `18090` 绑定到服务器本机。首次部署应通过 SSH 在服务器内部验证：

```shell
curl --fail http://127.0.0.1:18090/healthz
```

在 GitHub Webhook 实现原始请求体验签、事件白名单、请求体大小限制和投递去重之前，不要把 `18090` 暴露到公网。

## 首次部署步骤

1. 创建独立目录，例如 `/opt/openreviewer/releases/<commit-sha>`。
2. 将 `compose.yml` 和服务器专用的 `.env` 文件复制到该目录。
3. 将 `OPENREVIEWER_IMAGE` 设置为对应的完整 SHA 镜像标签。
4. 使用短期 Token 登录 GHCR，拉取镜像后立即退出登录。
5. 执行 `docker compose --env-file .env up -d`。
6. 检查容器健康状态和本机 `/healthz` 响应。

仓库中不得出现服务器地址、Token、私钥或真实部署配置。
