# M3 GitHub 上下文与阶段 D 模型 Worker 部署

这是 `niuma-2` 的 M3 部署边界。Compose 运行以下组件：

- `postgres`：OpenReviewer 独享的 PostgreSQL 16，不映射宿主机端口；
- `migrate`：每次发布前执行 `alembic upgrade head`，成功后正常退出；
- `api`：健康检查和认证后的管理 API，只绑定宿主机 `127.0.0.1:18090`；
- `worker`：单并发数据库任务 Worker，只出站访问 GitHub 和所选模型 API，无公网端口；
- `web`：React 静态文件、HTTPS 和 API 反向代理，公开宿主机 `18443`。

当前测试环境前端入口：

```text
https://openreviewer.lovecoding.store
```

公网域名由 Cloudflare 代理，源站使用只读挂载的 Origin 证书。

当前已包含 GitHub Webhook 验签、GitHub App 短期身份、PR/diff/CI 读取、CI 轮询、旧提交
失效保护和模型审查。CI 终态任务进入 `ready_for_review` 后由 Worker 自动规划并按模型上下文
分批调用 AI；结果保存后进入 `completed`。GitHub Check 发布仍未实现，不影响管理界面查看结果。

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
    ├── github/
    │   └── github-app-private-key.pem  # 0640 root:root，只读挂载给 Worker
    ├── secrets/
    │   └── ai-config-key               # 0640 root:root，API/Worker 共用的 32 字节 Base64 密钥
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
OPENREVIEWER_GITHUB_APP_ID=<GitHub App 数字 ID>
OPENREVIEWER_GITHUB_PRIVATE_KEY_FILE=/opt/openreviewer/shared/github/github-app-private-key.pem
OPENREVIEWER_AI_CONFIG_KEY_FILE=/opt/openreviewer/shared/secrets/ai-config-key
OPENREVIEWER_AI_CONFIG_KEY_VERSION=1
OPENREVIEWER_GITHUB_WEBHOOK_MAX_BYTES=262144
OPENREVIEWER_CI_POLL_SECONDS=30
OPENREVIEWER_CI_WAIT_TIMEOUT_SECONDS=3600
OPENREVIEWER_GITHUB_CONTEXT_LEASE_SECONDS=600
OPENREVIEWER_MODEL_REVIEW_LEASE_SECONDS=600
OPENREVIEWER_API_HOST_PORT=18090
OPENREVIEWER_WEB_HOST_PORT=18443
OPENREVIEWER_TLS_CERT_FILE=/opt/openreviewer/shared/tls/openreviewer.crt
OPENREVIEWER_TLS_KEY_FILE=/opt/openreviewer/shared/tls/openreviewer.key
OPENREVIEWER_ENV=test
OPENREVIEWER_LOG_LEVEL=INFO
```

`.env` 必须是 `0600 root:root`。管理员密码哈希不是明文，但仍不提交 Git。
AI 配置主密钥文件使用 `0600` 或 `0640 root:root`；API 与 Worker 容器都以 group 0 只读挂载。
模型供应商、模型 ID、API Key、上下文窗口、超时、传输大小与价格在管理界面的设置页配置。

### GitHub App 仓库权限

当前 PR 与 CI 上下文读取阶段需要现有安装批准以下仓库权限：

| 权限 | 级别 | 用途 |
| --- | --- | --- |
| Metadata | Read-only | 校验仓库身份 |
| Pull requests | Read-only | 读取 PR 元数据和 changed files |
| Contents | Read-only | 读取私有仓库 PR 的完整 diff 表示 |
| Checks | Read-only | 读取 Check Runs |
| Commit statuses | Read-only | 读取 Commit Statuses |

只在 GitHub App 的 `Permissions & events` 页面保存权限还不够。已有安装会显示权限更新请求，
管理员必须进入安装设置并接受该请求。Worker 会在进程内缓存短期 installation token，因此批准
权限后只重启 Worker，使其重新签发 Token：

```shell
docker restart openreviewer-worker
docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' \
  openreviewer-worker
```

验证结果必须为 `running healthy`。完整权限和请求契约见
[`docs/contracts/github-context.md`](../docs/contracts/github-context.md)。阶段 D 发布 GitHub Check
时，再单独审批把 `Checks` 提升为 `Read and write`；当前阶段不授予其他写权限。

## Cloudflare Origin TLS

源站证书覆盖 `openreviewer.lovecoding.store`，Cloudflare SSL/TLS 模式使用 `Full (strict)`。
证书和私钥分别保存为 `/opt/openreviewer/shared/tls/openreviewer.crt` 与
`/opt/openreviewer/shared/tls/openreviewer.key`，只读挂载到 Web 容器。TLS 目录保持
`0700 root:root`，防止宿主机普通用户遍历；容器仍以非 root `nginx` 用户运行。

Origin 证书只用于 Cloudflare 到源站的连接，不应把源站 IP 当作给普通浏览器使用的正式入口。

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
浏览器 https://openreviewer.lovecoding.store
        -> Web Nginx :8443
            -> 允许的 /api/v1 管理路径 -> API :18090
            -> /webhooks/github（GitHub HMAC 验签）-> API :18090
            -> React 静态文件

宿主机 127.0.0.1:18090 -> API :18090
backend 内部网络        -> PostgreSQL :5432
worker egress 网络       -> api.github.com:443
                          -> api.openai.com:443 或 api.anthropic.com:443
```

- PostgreSQL、Worker 不发布宿主机端口；
- Worker 以 UID `10001`、GID `0` 运行，只为读取宿主机 `0640 root:root` 的 GitHub App
  私钥和模型 API Key；容器仍移除全部 capabilities、使用只读根文件系统且禁止提权；
- API 不监听公网地址；
- Nginx 拒绝未列入白名单的 `/api/` 路径；
- 登录同时受 Nginx IP 限速和 API 失败窗口限制；
- Cookie 为 Secure、HttpOnly、SameSite=Strict；
- Web、API 和 Worker 使用只读根文件系统、移除 Linux capabilities 并启用
  `no-new-privileges`；
- 不在日志中输出密码、Cookie、数据库连接密码、模型 API Key 或完整 Compose 配置。

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
