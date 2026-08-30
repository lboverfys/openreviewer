# OpenReviewer 固定多 Agent 与人工发布部署

这是 `niuma-2` 的 M3 部署边界。Compose 运行以下组件：

- `postgres`：OpenReviewer 独享的 PostgreSQL 16，不映射宿主机端口；
- `migrate`：每次发布前执行 `alembic upgrade head`，成功后正常退出；
- `api`：健康检查和认证后的管理 API，只绑定宿主机 `127.0.0.1:18090`；
- `worker`：每进程单并发、可按配置扩展副本的数据库任务 Worker，只出站访问 GitHub 和所选模型 API，无公网端口；
- `web`：React 静态文件、HTTPS 和 API 反向代理，公开宿主机 `18443`。

当前测试环境前端入口：

```text
https://openreviewer.lovecoding.store
```

公网域名由 Cloudflare 代理，源站使用只读挂载的 Origin 证书。

当前已包含 GitHub Webhook 验签、GitHub App 短期身份、PR/diff/CI 读取、CI 轮询、旧提交
失效保护、固定四 Agent 审查、版本化 Markdown RAG 和人工发布。CI 终态任务进入
`ready_for_review` 后由 Worker 自动规划，安全、规范和逻辑 Agent 并行分批调用模型，汇总 Agent
收口；结果停在人工批准门。批准后再次点击发布，API 才向同一 SHA 的 PR 幂等写入 Check Run、
通过评测准入的行内评论和汇总评论。

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
4. 将本次提交的固定部署文件、镜像 digest 和运维入口 digest 通过 SSH 标准输入传给部署入口。

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

一次性安装运维入口时，将 `deployment/deploy.sh`、`deployment/restore.sh` 和
`deployment/backup.sh` 以 `root:root`、`0755` 分别安装到
`/usr/local/libexec/openreviewer-deploy`、`/usr/local/libexec/openreviewer-restore` 和
`/usr/local/libexec/openreviewer-backup`。再把 `openreviewer-backup.service` 与
`openreviewer-backup.timer` 以 `0644 root:root` 安装到 `/etc/systemd/system`，执行
`systemctl daemon-reload && systemctl enable --now openreviewer-backup.timer`。服务器上的
`/opt/openreviewer/current/.env` 仍由管理员维护，权限保持 `0600`，不会上传到 GitHub；
每个新 release 只从上一可用版本复制它，并强制改写两个完整 SHA 镜像值。
每次 CI 发布还会把三份入口脚本的 SHA-256 写入 `helper-digests.env`；部署开始前会核对
服务器已安装副本。若脚本被手工修改、路径缺失或版本落后，发布会在接触容器和数据库前
直接失败。首次安装以及脚本变更后的第一次发布，管理员仍需先按上面的路径更新入口文件。

### 发布失败和回退

部署脚本用 `/opt/openreviewer/deploy.lock` 串行化发布，在切换 `current` 前依次完成 Compose
校验、镜像拉取、PostgreSQL 健康检查、custom-format 数据库备份、临时数据库真实恢复验证，
然后停止并确认旧版 API/Worker/Web 已完全退出，再执行 Alembic 向前迁移、API/Worker/Web
健康检查和本机 `/readyz` 检查。所有检查通过后，才原子更新：

```text
/opt/openreviewer/current -> releases/<commit-sha>
```

候选版本失败时不会更新 `current`；如果应用容器已经被新版本替换，脚本会恢复上一发布的
API、Worker 和 Web 镜像。数据库迁移只向前执行，绝不自动 `downgrade`。若迁移命令失败或
中途异常，脚本会停止应用与监控服务并保持停机，要求管理员先核对迁移日志、数据库版本和
兼容性，再手工选择恢复或启动版本；迁移成功但新应用健康检查失败时仍会按既有流程尝试
恢复上一版本。每个成功版本的镜像 digest、运维入口 digest、迁移版本、备份文件名和部署时间
写入该 release 的 `release.info`，不包含任何密码或会话密钥。默认保留最近 14 份已验证备份，
可用 `OPENREVIEWER_BACKUP_RETENTION_COUNT` 在 3 到 100 之间调整。发布成功后还会保留当前
release 和最近的历史 release，默认共保留 5 个，可用 `OPENREVIEWER_RELEASE_RETENTION_COUNT`
在 2 到 100 之间调整；清理失败只记录告警，不影响已通过健康检查的当前版本。

Worker 收到停止信号后会在当前任务边界退出，不再领取新任务；Compose 默认提供 16 分钟
的优雅停止宽限，发布脚本还会按 `OPENREVIEWER_DEPLOY_STOP_TIMEOUT_SECONDS`（默认 960 秒，
允许 30 到 3600 秒）等待全部副本停止。该值应大于管理员允许的最长模型请求时长；超时后
容器可能被强制终止，遗留租约会由新 Worker 按过期恢复规则处理。

## 服务器目录

```text
/opt/openreviewer/
├── current -> releases/<commit-sha>
├── releases/<commit-sha>/
│   ├── compose.yml
│   ├── image-digests.env
│   ├── helper-digests.env
│   ├── prometheus-alerts.yml
│   ├── observability/        # Prometheus、Alertmanager 与 Grafana 固定配置
│   ├── .env                 # 0600 root:root，由服务器复制
│   └── release.info         # 成功发布后生成，不含敏感值
├── backups/                 # 0700；备份本体和 SHA-256 校验文件均为 0600
├── restores/                # 0700；每次成功恢复的非敏感记录
└── shared/
    ├── postgres-password    # 已有数据库密码，仅用于生成发布 .env
    ├── github/
    │   └── github-app-private-key.pem  # 0640 root:root，只读挂载给 Worker
    ├── secrets/
    │   ├── ai-config-key               # 0640 root:root，API/Worker 共用的 32 字节 Base64 密钥
    │   ├── auth-users.json             # 0640 root:root，额外账号 JSON 数组
    │   └── alert-webhook-url           # 可选；0640 root:root，Alertmanager HTTPS Webhook 地址
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
OPENREVIEWER_AUTH_USERS_FILE=/opt/openreviewer/shared/secrets/auth-users.json
OPENREVIEWER_SESSION_SECRET=<随机会话签名密钥>
OPENREVIEWER_GITHUB_WEBHOOK_SECRET=<至少 32 字节的 Webhook 密钥>
OPENREVIEWER_GITHUB_APP_ID=<GitHub App 数字 ID>
OPENREVIEWER_GITHUB_PRIVATE_KEY_FILE=/opt/openreviewer/shared/github/github-app-private-key.pem
OPENREVIEWER_GITHUB_ALLOWED_INSTALLATION_IDS=<允许的 installation ID，多个用逗号分隔>
OPENREVIEWER_GITHUB_ALLOWED_ORGANIZATIONS=<允许整组织接入的 owner，多个用逗号分隔>
OPENREVIEWER_GITHUB_ALLOWED_REPOSITORIES=<允许的 owner/repository，多个用逗号分隔>
OPENREVIEWER_AI_CONFIG_KEY_FILE=/opt/openreviewer/shared/secrets/ai-config-key
OPENREVIEWER_AI_CONFIG_KEY_VERSION=1
# 可选；缺失时仍启动监控，但不发送外部通知
OPENREVIEWER_ALERT_WEBHOOK_URL_SOURCE_FILE=/opt/openreviewer/shared/secrets/alert-webhook-url
OPENREVIEWER_GITHUB_WEBHOOK_MAX_BYTES=262144
OPENREVIEWER_CI_POLL_SECONDS=30
OPENREVIEWER_CI_WAIT_TIMEOUT_SECONDS=3600
OPENREVIEWER_GITHUB_CONTEXT_LEASE_SECONDS=600
OPENREVIEWER_MODEL_REVIEW_LEASE_SECONDS=600
OPENREVIEWER_READY_WORKER_MAX_AGE_SECONDS=45
OPENREVIEWER_OUTBOX_BATCH_SIZE=100
OPENREVIEWER_CLEANUP_BATCH_SIZE=500
OPENREVIEWER_REVIEW_RETENTION_DAYS=180
OPENREVIEWER_QUOTA_BUCKET_RETENTION_DAYS=3
OPENREVIEWER_FINDING_EVALUATION_RETENTION_DAYS=730
OPENREVIEWER_BACKUP_RETENTION_COUNT=14
OPENREVIEWER_RELEASE_RETENTION_COUNT=5
OPENREVIEWER_BACKUP_REQUIRE_MIRROR=false
OPENREVIEWER_KNOWLEDGE_ROOT=knowledge
OPENREVIEWER_API_HOST_PORT=18090
OPENREVIEWER_WEB_HOST_PORT=18443
OPENREVIEWER_TLS_CERT_FILE=/opt/openreviewer/shared/tls/openreviewer.crt
OPENREVIEWER_TLS_KEY_FILE=/opt/openreviewer/shared/tls/openreviewer.key
OPENREVIEWER_ENV=test
OPENREVIEWER_LOG_LEVEL=INFO
```

额外账号文件由 root 持有并设为 `0640 root:root`，内容是 JSON 数组。角色可选 `viewer`、
`adjudicator`、`publisher` 或 `administrator`。非管理员账号必须声明资源 `scope`；
scope 可按 GitHub App installation、组织或精确仓库收窄。未声明 scope 的非管理员账号
默认拒绝全部资源，管理员未声明 scope 时保持全量访问。没有额外账号时文件内容写为 `[]`。
例如：

```json
[
  {
    "username": "reviewer",
    "password_hash": "$argon2id$...",
    "role": "adjudicator",
    "scope": {
      "installation_ids": [123456],
      "organizations": ["lboverfys"],
      "repositories": ["lboverfys/special-repository"]
    }
  }
]
```

同一账号的 scope 条件按“交集”计算：配置了 installation ID 时必须先命中该 installation，
配置了组织或仓库时还必须命中其中一个；三个数组都为空表示显式拒绝全部。详情、Dashboard、
Finding 裁决、人工动作和 SSE 都在数据库查询前应用同一范围，越权的运行 ID 统一返回 `404`，
避免通过总数或错误信息枚举其他仓库。

`.env` 必须是 `0600 root:root`。管理员密码哈希不是明文，但仍不提交 Git。
`shared/secrets` 目录使用 `0700 root:root`；其中账号、AI 主密钥和告警地址文件使用
`0640 root:root`。API、Worker 与 Alertmanager 都以非 root UID、GID 0 只读挂载所需文件。
四个 Agent 的供应商、模型 ID、API Key、上下文窗口、单批上限、推理档位、超时和重试数在
管理界面的设置页分别配置。`knowledge/` 已打入 API/Worker 镜像；生产默认使用只读的
`/app/knowledge`，无需额外挂载可写目录。

### GitHub App 仓库权限

当前 PR 与 CI 上下文读取阶段需要现有安装批准以下仓库权限：

| 权限 | 级别 | 用途 |
| --- | --- | --- |
| Metadata | Read-only | 校验仓库身份 |
| Pull requests | Read and write | Worker 只读 PR；API 仅在人工发布时申请评论写权限 |
| Contents | Read-only | 读取私有仓库 PR 的完整 diff 表示 |
| Checks | Read and write | Worker 读取 Check Runs；API 在人工发布时创建或更新 Check Run |
| Commit statuses | Read-only | 读取 Commit Statuses |

只在 GitHub App 的 `Permissions & events` 页面保存权限还不够。已有安装会显示权限更新请求，
管理员必须进入安装设置并接受该请求。Worker 会在进程内缓存短期 installation token，因此批准
权限后重启 API 与 Worker，使它们重新签发 Token：

OpenReviewer 签发 Token 时还会显式缩小权限：Worker 和 API 身份补全只申请
`contents/pull_requests/checks/statuses: read`；人工发布器单独申请
`checks/pull_requests: write`，不会把写权限 Token 复用于后台审查。

```shell
docker compose --project-directory /opt/openreviewer/current \
  --env-file /opt/openreviewer/current/.env \
  --file /opt/openreviewer/current/compose.yml restart api worker
docker compose --project-directory /opt/openreviewer/current \
  --env-file /opt/openreviewer/current/.env \
  --file /opt/openreviewer/current/compose.yml ps api worker
```

API 和全部 Worker 副本都必须为 `running healthy`。完整权限和请求契约见
[`docs/contracts/github-context.md`](../docs/contracts/github-context.md) 与
[`docs/contracts/github-publishing.md`](../docs/contracts/github-publishing.md)。发布器只申请 Checks 和
Pull requests 写权限，不申请 Contents、Actions、Administration 或 Secrets 写权限。

## Cloudflare Origin TLS

源站证书覆盖 `openreviewer.lovecoding.store`，Cloudflare SSL/TLS 模式使用 `Full (strict)`。
证书和私钥分别保存为 `/opt/openreviewer/shared/tls/openreviewer.crt` 与
`/opt/openreviewer/shared/tls/openreviewer.key`，只读挂载到 Web 容器。TLS 目录保持
`0700 root:root`，防止宿主机普通用户遍历；容器仍以非 root `nginx` 用户运行。

Origin 证书只用于 Cloudflare 到源站的连接，不应把源站 IP 当作给普通浏览器使用的正式入口。

## 手工发布和排障

正常情况下只需向 `main` 推送，Actions 会自动执行上一节的发布流程。首次安装或排障时，
管理员可以在 `niuma-2` 上以 root 身份手工调用。新版入口接收固定白名单 tar 包，不再接收
单个 Compose 文件：

手工发布也必须携带 CI 构建得到的 digest。先在已登录 GHCR 的受控终端取得目标完整
SHA 的两个 manifest digest，并生成临时清单（不要把清单写入仓库）：

```shell
manifest_dir="$(mktemp -d)"
trap 'rm -rf -- "$manifest_dir"' EXIT
api_digest="$(docker buildx imagetools inspect \
  "ghcr.io/lboverfys/openreviewer:<40 位 commit SHA>" \
  --format '{{.Manifest.Digest}}')"
web_digest="$(docker buildx imagetools inspect \
  "ghcr.io/lboverfys/openreviewer-web:<40 位 commit SHA>" \
  --format '{{.Manifest.Digest}}')"
printf 'OPENREVIEWER_API_IMAGE_DIGEST=%s\nOPENREVIEWER_WEB_IMAGE_DIGEST=%s\n' \
  "$api_digest" "$web_digest" > "$manifest_dir/image-digests.env"
chmod 600 "$manifest_dir/image-digests.env"
printf 'OPENREVIEWER_DEPLOY_HELPER_SHA256=%s\nOPENREVIEWER_BACKUP_HELPER_SHA256=%s\nOPENREVIEWER_RESTORE_HELPER_SHA256=%s\n' \
  "$(sha256sum deployment/deploy.sh | awk '{print $1}')" \
  "$(sha256sum deployment/backup.sh | awk '{print $1}')" \
  "$(sha256sum deployment/restore.sh | awk '{print $1}')" \
  > "$manifest_dir/helper-digests.env"
chmod 600 "$manifest_dir/helper-digests.env"
```

```shell
tar --create --file - \
  --directory "$manifest_dir" image-digests.env \
  helper-digests.env \
  --directory deployment \
  compose.yml prometheus-alerts.yml \
  observability/alertmanager.yml \
  observability/alertmanager-noop.yml \
  observability/alert-webhook-url.placeholder \
  observability/grafana-dashboard.json \
  observability/grafana-dashboards.yml \
  observability/grafana-datasource.yml \
  observability/prometheus.yml \
  | /usr/local/libexec/openreviewer-deploy <40 位 commit SHA>
```

手工调用仍会使用当前 release 的服务器 `.env`，不会接受命令行传入密码。执行前应确认
目标 SHA 的两个 GHCR 镜像已经存在，并保留当前容器和数据库备份信息。

候选版本失败时，不更新 `current`；若它影响现有服务，恢复上一发布的完整 SHA 镜像。数据库
迁移只向前执行，不自动降级。

恢复数据库必须显式选择 `/opt/openreviewer/backups` 内带有效 SHA-256 的备份：

```shell
/usr/local/libexec/openreviewer-restore --restore \
  /opt/openreviewer/backups/<时间>-<40位commit SHA>.dump
```

恢复入口先在临时数据库完整执行 `pg_restore`，再保存当前生产库并换名切换。当前 release 的
迁移、容器健康和 `/readyz` 全部通过后才删除数据库回退副本；中途失败会尽力自动换回原库，
同时保留一份 `*-pre-restore.dump` 紧急备份。恢复成功后只保留最近若干份紧急备份（沿用
`OPENREVIEWER_BACKUP_RETENTION_COUNT`，请求中使用的备份不会被清理），避免多次恢复造成无限增长。

定时器每天 UTC 03:15（带最多 15 分钟随机延迟）调用备份入口。每份备份都先恢复到临时
数据库并检查表与 Alembic 版本，再原子写入 `.dump` 和 `.sha256`。立即验证一次安装：

```shell
systemctl start openreviewer-backup.service
systemctl status openreviewer-backup.service --no-pager
systemctl list-timers openreviewer-backup.timer --no-pager
```

异地存储由 root 管理的固定可执行文件接入。把绝对路径写入
`OPENREVIEWER_BACKUP_MIRROR_COMMAND`；入口只传入 `<dump> <sha256>` 两个参数，不会 `eval`
配置内容。生产确认异地链路后把 `OPENREVIEWER_BACKUP_REQUIRE_MIRROR=true`，这样上传失败会让
定时服务明确失败并保留本地备份，且不会继续清理旧备份。镜像命令必须位于
`/usr/local/libexec`、由 root 拥有且不可被组或其他用户写入。

## 运维指标和保留期

API 回环端口提供 `/metrics` Prometheus 文本指标和 `/readyz` 就绪探针。它们不在公网 Nginx
白名单中。告警规则位于 `deployment/prometheus-alerts.yml`，覆盖指标不可用、Worker
失联、Outbox 延迟、堆积和连续失败。部署入口检查
`OPENREVIEWER_ALERT_WEBHOOK_URL_SOURCE_FILE` 指向的 root 只读文件：文件存在且非空时使用正式
Alertmanager 配置，告警触发和恢复都会外发；文件缺失或为空时自动使用 noop 配置，告警仍可在
Prometheus 和 Alertmanager 中查看，但不会向外部系统发送。地址不会进入 Compose 或 Git；创建
真实文件后由下一个新 SHA 发布即可启用通知。Worker 每轮最多发布 100 条 Outbox 事件，每五分钟对
每类过期数据最多删除 500 条；默认保留已发布事件 14 天、失效会话和旧心跳 7 天、Webhook
90 天、终态审查及其级联明细 180 天、配额窗口 3 天。人工 Finding 评测样本是独立历史快照，
不会随 Finding 或 ReviewRun 的清理级联删除。所有值都可通过 `.env` 中对应变量收紧或放宽。
每个 Worker 进程一次只领取一个任务；`OPENREVIEWER_WORKER_REPLICAS`（1 到 20）由发布脚本
显式传给 Compose 的 `--scale`，因此非 Swarm 环境也会按配置运行全部副本。

## 网络和安全边界

```text
浏览器 https://openreviewer.lovecoding.store
        -> Web Nginx :8443
            -> 允许的 /api/v1 管理路径 -> API :18090
            -> /webhooks/github（GitHub HMAC 验签）-> API :18090
            -> React 静态文件

宿主机 127.0.0.1:18090 -> API :18090
backend 内部网络        -> PostgreSQL :5432
api egress 网络          -> api.github.com:443（人工发布）
worker egress 网络       -> api.github.com:443
                          -> api.openai.com:443 或 api.anthropic.com:443
```

- PostgreSQL、Worker 不发布宿主机端口；
- API 与 Worker 以 UID `10001`、GID `0` 运行，只读访问宿主机 `0640 root:root` 的 GitHub App
  私钥和 AI 配置主密钥；模型 API Key 只以密文保存在数据库，解密后短暂存在进程内存；
- Web 以 Nginx 的非 root UID、GID `0` 运行；TLS 私钥使用 `0640 root:root`，证书使用
  `0644 root:root`，两者都只读挂载；
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
curl --fail http://127.0.0.1:18090/readyz
curl --fail http://127.0.0.1:18090/metrics
curl --fail --insecure https://127.0.0.1:18443/healthz
```

认证功能应通过浏览器从 Web 同源入口验证，不通过命令行把真实密码写进 shell 历史。
