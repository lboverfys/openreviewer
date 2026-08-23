# OpenReviewer CI/CD 指南

> 本文说明仓库当前可确认的 CI/CD 行为。服务器初始化、密钥配置和日常运维以
> [`deployment/README.md`](../deployment/README.md) 为准；GitHub 后台和服务器实时状态
> 需要在对应环境中单独核对。

## 1. 流程概览

```text
Pull Request -> 后端测试 + 前端检查
                         |
main push ---------------+-> 构建并推送两个 GHCR 镜像
                              -> 受限 SSH 连接 niuma-2
                              -> 数据库迁移
                              -> 替换 API、Worker、Web
                              -> 健康检查并记录成功版本
```

核心规则：

- PR 只执行检查，不发布镜像、不连接服务器。
- `main` 的检查全部通过后才发布和部署。
- 发布使用完整 commit SHA 标签，不依赖会移动的 `main` 标签。
- 数据库迁移只向前执行；应用恢复不等于数据库回滚。
- 新版本通过全部检查后，才更新服务器上的 `current` 记录。

## 2. 相关文件

| 文件 | 职责 |
| --- | --- |
| [`.github/workflows/verify-and-publish.yml`](../.github/workflows/verify-and-publish.yml) | 测试、镜像发布和 SSH 部署 |
| [`Dockerfile`](../Dockerfile) | API、Worker 和迁移共用镜像 |
| [`web/Dockerfile`](../web/Dockerfile) | React 构建和 Nginx 运行镜像 |
| [`deployment/compose.yml`](../deployment/compose.yml) | 服务、网络、健康检查和资源限制 |
| [`deployment/deploy.sh`](../deployment/deploy.sh) | 服务器端发布、验证和失败恢复 |
| [`deployment/.env.example`](../deployment/.env.example) | 服务器配置示例，不含真实秘密 |
| [`web/nginx.conf`](../web/nginx.conf) | HTTPS、静态页面和管理 API 代理 |

## 3. 触发条件和任务依赖

| 事件 | 后端/前端检查 | 发布镜像 | 部署 |
| --- | --- | --- | --- |
| 目标为 `main` 的 PR | 是 | 否 | 否 |
| push 到 `main` | 是 | 是 | 是 |
| push 到其他分支 | 否 | 否 | 否 |
| 手工运行 `main` | 是 | 是 | 是 |
| 手工运行其他分支 | 是 | 否 | 否 |

`test` 和 `web-test` 可以并行；`publish` 等待两者成功，`deploy` 再等待 `publish` 成功。
当前没有路径过滤，因此只修改 `docs/` 后合入 `main`，也会触发完整发布。

## 4. CI 检查

### 后端

环境为 Ubuntu 24.04 和 Python 3.12：

```shell
python -m pip install ".[dev]"
python -m pytest -q -W error
```

`-W error` 会把警告视为失败。当前测试以 SQLite 为主，没有在 CI 中启动真实 PostgreSQL；
同时也没有依赖锁文件、类型检查、覆盖率门槛或独立 Lint。

### 前端

环境为 Ubuntu 24.04 和 Node.js 22.19.0：

```shell
npm ci --no-audit --no-fund
npm run typecheck
npm test
npm run build
```

依赖按 `web/package-lock.json` 安装并使用 npm 缓存。当前没有浏览器端到端测试，
`--no-audit` 也表示安装过程不执行漏洞审计。

## 5. 镜像发布

工作流使用短期 `GITHUB_TOKEN` 登录 GHCR，并推送两个镜像：

| 镜像 | 使用者 |
| --- | --- |
| `ghcr.io/lboverfys/openreviewer:<SHA>` | `api`、`worker`、`migrate` |
| `ghcr.io/lboverfys/openreviewer-web:<SHA>` | `web` |

两个镜像还会写入便于查看的 `main` 标签，但服务器只部署 40 位完整 SHA。
SHA 标签是项目约定，并非 GHCR 强制不可覆盖；基础镜像也尚未锁定 digest。

后端镜像使用 Python 3.12，以非 root 用户运行。Web 镜像先用 Node 构建 React，
再把静态文件放入非 root Nginx 镜像。当前未生成 SBOM、来源证明或签名，也未执行镜像漏洞扫描。

## 6. 部署链路

`deploy` 使用 GitHub Environment `niuma-2`，需要以下 Secrets：

- `OPENREVIEWER_DEPLOY_SSH_PRIVATE_KEY`
- `OPENREVIEWER_DEPLOY_KNOWN_HOSTS`
- `OPENREVIEWER_DEPLOY_BASTION_HOST`
- `OPENREVIEWER_DEPLOY_HOST`
- `OPENREVIEWER_DEPLOY_USER`

Runner 临时创建 SSH 配置，固定主机指纹，并通过 `niuma` 跳板机连接 `niuma-2`。最终请求等价于：

```shell
ssh openreviewer-deploy-target "openreviewer-deploy <完整 SHA>" \
  < deployment/compose.yml
```

部署通道只传 SHA 和 Compose 文件，不传服务器 `.env`、密码或 TLS 私钥。服务器公钥应配置
forced command 和转发限制，使部署密钥不能直接取得任意交互式 Shell。

服务器必须预先准备 Docker、部署入口、`/opt/openreviewer` 初始 release、真实 `.env`、
TLS 文件和镜像读取权限。工作流不会自动完成首次初始化，也不会自动更新服务器已安装的
`/usr/local/libexec/openreviewer-deploy`；修改仓库中的 `deployment/deploy.sh` 后，管理员仍需
单独更新服务器副本。

## 7. 服务器发布步骤

部署脚本按以下顺序执行：

1. 校验运行用户、完整 SHA、必需命令和基础目录。
2. 接收不超过 256 KiB 的 Compose 文件并检查项目名、镜像变量和服务名。
3. 获取服务器文件锁，避免并发发布。
4. 从上一成功 release 复制 `.env`，只替换两个镜像地址。
5. 验证 Compose，拉取 PostgreSQL 和本次 SHA 镜像。
6. 启动 PostgreSQL，执行 `alembic upgrade head`。
7. 原地替换 API、Worker 和 Web 单实例容器。
8. 检查容器健康、宿主机健康接口、实际镜像和迁移版本。
9. 写入 `release.info`，再原子更新 `current` 符号链接。

同一 SHA 重复部署时，脚本会验证已有 release 并复查健康状态，不重复执行成功发布。

服务器目录的关键部分如下：

```text
/opt/openreviewer/
├── current -> releases/<当前成功 SHA>
├── releases/<SHA>/{compose.yml,.env,release.info}
└── shared/{postgres-password,tls/}
```

PostgreSQL 数据位于固定 Docker 数据卷 `openreviewer-postgres-data`，不随 release 切换。
不得把删除该数据卷当作普通回退手段。

## 8. 服务和网络边界

| 服务 | 作用 | 对外暴露 |
| --- | --- | --- |
| `postgres` | 持久化任务、会话和心跳 | 否 |
| `migrate` | 一次性执行 Alembic 迁移 | 否 |
| `api` | 管理和任务 API | 仅宿主机 `127.0.0.1:18090` |
| `worker` | 领取和推进任务 | 否 |
| `web` | React、HTTPS 和 API 代理 | `0.0.0.0:18443` |

`backend` 内部网络连接 PostgreSQL、迁移、API 和 Worker；`edge` 网络只连接 API 与 Web。
公网请求从 Web/Nginx 进入，数据库和 Worker 没有宿主机端口。

健康检查分别确认：

- PostgreSQL 能接受连接；
- API 的 `/healthz` 能响应；
- Worker 心跳未过期；
- Web/Nginx 的 HTTPS `/healthz` 能响应。

这些检查不能证明登录、任务创建或完整审查流程一定可用。当前发布也没有业务级端到端冒烟测试。

## 9. 失败和恢复边界

- 测试或镜像构建失败：不会开始部署。
- Compose、镜像拉取或迁移失败：不会替换应用容器。
- 新应用不健康：脚本尝试恢复上一版 API、Worker 和 Web。
- 恢复动作本身也可能失败，部署失败后仍需核对三个应用容器的实际状态。
- `current` 只在完整成功后更新。

需要特别注意：迁移一旦成功就不会自动降级。旧应用能否在新数据库结构上运行，依赖迁移保持
向后兼容。当前方案也不是蓝绿发布，替换单实例容器时可能有短暂中断。

## 10. 维护注意事项

| 修改内容 | 合入 `main` 后的结果 |
| --- | --- |
| Python、迁移、React 或 Nginx | 完整测试、重建两个镜像并部署 |
| `deployment/compose.yml` | 新文件会随部署请求发送到服务器 |
| `deployment/deploy.sh` | 服务器不会自动更新该脚本 |
| `deployment/.env.example` | 只更新示例，不修改服务器真实 `.env` |
| Workflow | 后续运行按新规则执行，应重点审查权限和 Secret 使用 |

优先改进项：

1. 增加数据库备份和恢复演练，并明确迁移兼容策略。
2. 为关键业务路径增加 PostgreSQL 集成测试和部署后冒烟测试。
3. 锁定依赖和供应链输入，加入漏洞扫描、SBOM、来源证明和签名。
4. 增加发布失败通知、明确的应用回退命令和旧 release 清理策略。
5. 若要求近似零停机，再引入多副本和流量切换机制。

## 11. 排障入口

| 失败位置 | 优先检查 |
| --- | --- |
| `Test Python 3.12` | 依赖安装、首个 Pytest 失败、被提升为错误的警告 |
| `Test React on Node 22.19` | 锁文件、类型检查、Vitest、Vite 构建 |
| `Publish GHCR images` | GHCR 权限、Dockerfile、基础镜像和构建上下文 |
| `Configure restricted SSH connection` | 5 个 Secrets、主机指纹、跳板机网络 |
| `Deploy immutable commit release` | Compose、拉镜像、迁移和健康检查日志 |

仓库无法证明 GitHub 分支保护、Environment 审批、Secrets 有效性、服务器脚本版本、当前运行
SHA、证书、防火墙和数据库备份状态。需要核对实时状态时，使用
[`deployment/README.md`](../deployment/README.md) 中的只读检查命令，不要输出 `.env` 或完整的
`docker compose config`。
