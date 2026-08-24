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

当前已包含 GitHub Webhook 验签、过滤和入队，但仍不包含 installation token、PR 上下文、
CI 回调或模型调用。任务会从 `queued` 经过 `running` 进入 `waiting_for_ci`，不会伪装为
`completed`。

## 镜像规则

GitHub Actions 发布两类镜像：

```text
ghcr.io/lboverfys/openreviewer:<完整 commit SHA>
ghcr.io/lboverfys/openreviewer-web:<完整 commit SHA>
```

服务器必须使用完整 SHA 标签。可移动的 `main` 标签只用于查看，不能作为发布版本依据。
同时记录镜像摘要，才能在标签之外确认实际运行内容。

## 推送后的自动部署

`.github/workflows/verify-and-publish.yml` 在 `main` 分支收到 push 后按下面的顺序运行：

1. 运行 Python 后端测试、React 类型检查、前端测试和生产构建；
2. 将 API/Worker 与 Web 镜像分别发布到 GHCR，并同时写入完整 commit SHA 标签；
3. 通过 `niuma` 跳板机连接 `niuma-2`，执行服务器上的
   `/usr/local/libexec/openreviewer-deploy`；
4. 将本次提交的 `deployment/compose.yml` 通过 SSH 标准输入传给部署入口。

部署 Job 使用 `niuma-2` GitHub Environment，并以
`openreviewer-niuma-2` 为并发组。同一时间只允许一个发布过程，排队中的发布不会互相
覆盖。Pull Request 只验证代码，不会连接生产测试机；`workflow_dispatch` 仅在推送后的
完整提交上运行发布链路。

### GitHub Environment Secrets

以下值应配置在仓库的 `niuma-2` Environment 中。它们只供部署 Job 使用，不要写入仓库：

| Secret | 用途 |
| --- | --- |
| `OPENREVIEWER_DEPLOY_SSH_PRIVATE_KEY` | 仅用于自动部署的 ED25519 私钥，不能复用个人 root 私钥 |
| `OPENREVIEWER_DEPLOY_KNOWN_HOSTS` | `niuma` 跳板机和 `niuma-2` 目标机的固定 SSH 主机指纹 |
| `OPENREVIEWER_DEPLOY_BASTION_HOST` | 跳板机地址（当前为 `niuma` 的公网地址） |
| `OPENREVIEWER_DEPLOY_HOST` | `niuma-2` 目标地址 |
| `OPENREVIEWER_DEPLOY_USER` | 目标登录用户（当前部署入口由 root 执行） |

服务器端对应的公钥必须分别写入跳板机和目标机的 `authorized_keys`。跳板机的条目只允许
转发到 `niuma-2:22`；目标机的条目使用 forced command，只允许执行
`openreviewer-deploy <40 位 SHA>`，并关闭伪终端、Agent 转发、X11 转发和端口转发。这样
GitHub Actions 即使拿到私钥，也不能取得服务器交互式 shell。

一次性安装部署入口时，将本文件中的 `deployment/deploy.sh` 以 `root:root`、`0755` 安装
到 `/usr/local/libexec/openreviewer-deploy`。服务器上的 `/opt/openreviewer/current/.env`
仍由管理员维护，权限保持 `0600`，不会上传到 GitHub；每个新 release 只从上一可用版本
复制它，并强制改写两个完整 SHA 镜像值。

### 发布失败和回退

部署脚本用 `/opt/openreviewer/deploy.lock` 串行化发布，在切换 `current` 前依次完成 Compose
校验、镜像拉取、PostgreSQL 健康检查、Alembic 向前迁移、API/Worker/Web 健康检查和本机
`/healthz` 检查。所有检查通过后，才原子更新：

```text
/opt/openreviewer/current -> releases/<commit-sha>
```

候选版本失败时不会更新 `current`；如果应用容器已经被新版本替换，脚本会恢复上一发布的
API、Worker 和 Web 镜像。数据库迁移只向前执行，绝不自动 `downgrade`，因此需要人工确认
迁移兼容性后再处理数据库问题。每个成功版本的镜像 digest、迁移版本和部署时间写入该
release 的 `release.info`，不包含任何密码或会话密钥。

## 服务器目录

```text
/opt/openreviewer/
├── current -> releases/<commit-sha>
├── releases/<commit-sha>/
│   ├── compose.yml
│   ├── .env                 # 0600 root:root，由服务器复制
│   └── release.info         # 成功发布后生成，不含敏感值
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
OPENREVIEWER_GITHUB_WEBHOOK_SECRET=<至少 32 字节的 Webhook 密钥>
OPENREVIEWER_GITHUB_WEBHOOK_MAX_BYTES=262144
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

## 手工发布和排障

正常情况下只需向 `main` 推送，Actions 会自动执行上一节的发布流程。首次安装或排障时，
管理员可以在 `niuma-2` 上以 root 身份手工调用：

```shell
/usr/local/libexec/openreviewer-deploy <40 位 commit SHA> < deployment/compose.yml
```

手工调用仍会使用当前 release 的服务器 `.env`，不会接受命令行传入密码。执行前应确认
目标 SHA 的两个 GHCR 镜像已经存在，并保留当前容器和数据库备份信息。

候选版本失败时，不更新 `current`；若它影响现有服务，恢复上一发布的完整 SHA 镜像。数据库
迁移只向前执行，不自动降级。

## 网络和安全边界

```text
浏览器 https://107.175.221.182:18443
        -> Web Nginx :8443
            -> 允许的 /api/v1 管理路径 -> API :18090
            -> /webhooks/github（GitHub HMAC 验签）-> API :18090
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
