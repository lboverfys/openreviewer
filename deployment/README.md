# M2 Worker、管理前端与 PostgreSQL 部署

这是 `niuma-2` 的 M2 部署边界。Compose 运行以下组件：

- `postgres`：OpenReviewer 独享的 PostgreSQL 16，不映射宿主机端口；
- `migrate`：每次发布前执行 `alembic upgrade head`，成功后正常退出；
- `api`：健康检查和认证后的管理 API，只绑定宿主机 `127.0.0.1:18090`；
- `worker`：单并发数据库任务 Worker，无公网端口；
- `web`：React 静态文件、HTTPS 和 API 反向代理，公开宿主机 `18443`。

当前测试环境前端入口：

```text
https://107.175.221.182:18443
```

该入口使用自签名 HTTPS 证书，仅用于测试和验收；浏览器首次访问时会显示证书警告。

当前仍不包含 GitHub Webhook、PR 上下文、CI 回调或模型调用。任务会从 `queued` 经过
`running` 进入 `waiting_for_ci`，不会伪装为 `completed`。

## 镜像规则

GitHub Actions 发布两类镜像：

```text
ghcr.io/lboverfys/openreviewer:<完整 commit SHA>
ghcr.io/lboverfys/openreviewer-web:<完整 commit SHA>
```

服务器必须使用完整 SHA 标签。可移动的 `main` 标签只用于查看，不能作为发布版本依据。
同时记录镜像摘要，才能在标签之外确认实际运行内容。

## 服务器目录

```text
/opt/openreviewer/
├── current -> releases/<commit-sha>
├── releases/<commit-sha>/
│   ├── compose.yml
│   └── .env                 # 0600 root:root
└── shared/
    ├── postgres-password    # 已有数据库密码，仅用于生成发布 .env
    └── tls/                 # 0700 root:root
        ├── openreviewer.crt
        └── openreviewer.key
```

固定名数据卷 `openreviewer-postgres-data` 跨发布目录复用。发布和回退都不得删除该数据卷。

## 管理员凭据

部署配置只保存 Argon2id 哈希，不保存管理员明文密码。可以在受控终端交互式生成：

```shell
python -c "from getpass import getpass; from argon2 import PasswordHasher; print(PasswordHasher().hash(getpass('Password: ')))"
```

把输出作为 `OPENREVIEWER_ADMIN_PASSWORD_HASH`。因为哈希包含 `$`，`.env` 中必须使用单引号
包住完整值。会话密钥使用密码学安全随机值，至少 32 字节，不得复用数据库密码。

真实 `.env` 至少包含：

```dotenv
OPENREVIEWER_IMAGE=ghcr.io/lboverfys/openreviewer:<完整-commit-sha>
OPENREVIEWER_WEB_IMAGE=ghcr.io/lboverfys/openreviewer-web:<完整-commit-sha>
OPENREVIEWER_POSTGRES_IMAGE=postgres:16.10-bookworm
OPENREVIEWER_POSTGRES_PASSWORD=<服务器数据库密码>
OPENREVIEWER_ADMIN_USERNAME=<管理员用户名>
OPENREVIEWER_ADMIN_PASSWORD_HASH='<Argon2id 哈希>'
OPENREVIEWER_SESSION_SECRET=<随机会话签名密钥>
OPENREVIEWER_API_HOST_PORT=18090
OPENREVIEWER_WEB_HOST_PORT=18443
OPENREVIEWER_TLS_CERT_FILE=/opt/openreviewer/shared/tls/openreviewer.crt
OPENREVIEWER_TLS_KEY_FILE=/opt/openreviewer/shared/tls/openreviewer.key
OPENREVIEWER_ENV=test
OPENREVIEWER_LOG_LEVEL=INFO
```

`.env` 必须是 `0600 root:root`。管理员密码哈希不是明文，但仍不提交 Git。

## 测试 HTTPS 证书

测试入口直接使用公网 IP，因此生成包含该 IP Subject Alternative Name 的自签名证书：

```shell
install -d -m 0700 -o root -g root /opt/openreviewer/shared/tls
openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 365 \
  -keyout /opt/openreviewer/shared/tls/openreviewer.key \
  -out /opt/openreviewer/shared/tls/openreviewer.crt \
  -subj "/CN=<niuma-2-public-ip>" \
  -addext "subjectAltName=IP:<niuma-2-public-ip>"
chmod 0644 /opt/openreviewer/shared/tls/openreviewer.crt
chmod 0644 /opt/openreviewer/shared/tls/openreviewer.key
```

TLS 目录本身保持 `0700 root:root`，所以宿主机普通用户不能读取其中的私钥；文件以只读方式
单独挂载后，非 root Nginx 才能读取。自签名证书仍会让浏览器首次访问显示“不受信任”警告，
测试人员核对 IP 和证书后手工继续即可。正式入口应换成受信任证书和域名。

## 发布步骤

1. 创建 `/opt/openreviewer/releases/<commit-sha>`，不覆盖已有发布目录；
2. 上传 `compose.yml`，创建权限为 `0600` 的 `.env`；
3. 只执行 `docker compose config --quiet` 校验，禁止输出完整展开配置；
4. 拉取完整 SHA 的 API/Worker 和 Web 镜像以及固定 PostgreSQL 镜像；
5. 启动 PostgreSQL并等待健康；
6. 执行一次性迁移并确认退出码为 `0`；
7. 启动 API、Worker 和 Web，等待三个长期容器健康；
8. 从服务器本机验证 API 健康、Web HTTPS、迁移版本和 Worker 心跳；
9. 验证管理员登录、任务创建和 `queued -> running -> waiting_for_ci`；
10. 记录镜像摘要并原子更新 `/opt/openreviewer/current` 符号链接。

候选版本失败时，不更新 `current`；若它影响现有服务，恢复上一发布的完整 SHA 镜像。数据库
迁移只向前执行，不自动降级。

## 网络和安全边界

```text
浏览器 https://107.175.221.182:18443
        -> Web Nginx :8443
            -> 允许的 /api/v1 管理路径 -> API :18090
            -> React 静态文件

宿主机 127.0.0.1:18090 -> API :18090
backend 内部网络        -> PostgreSQL :5432
```

- PostgreSQL、Worker 不发布宿主机端口；
- API 不监听公网地址；
- Nginx 拒绝未列入白名单的 `/api/` 路径；
- 登录同时受 Nginx IP 限速和 API 失败窗口限制；
- Cookie 为 Secure、HttpOnly、SameSite=Strict；
- Web、API 和 Worker 使用只读根文件系统、移除 Linux capabilities 并启用
  `no-new-privileges`；
- 不在日志中输出密码、Cookie、数据库连接密码或完整 Compose 配置。

## 安全验证命令

配置校验必须使用：

```shell
docker compose config --quiet
```

不要执行会展开环境变量的完整 `docker compose config`。运行状态可用：

```shell
docker compose ps
curl --fail http://127.0.0.1:18090/healthz
curl --fail --insecure https://127.0.0.1:18443/healthz
```

认证功能应通过浏览器从 Web 同源入口验证，不通过命令行把真实密码写进 shell 历史。
