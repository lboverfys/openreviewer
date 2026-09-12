# 部署与恢复手册

OpenReviewer 独立部署于 niuma-2，公网入口为 https://openreviewer.lovecoding.store/。
数据库、卷、证书和部署凭据均与业务项目分离。已有环境的发布只走 main 的 GitHub Actions。

## 组件与版本

Compose 包含 PostgreSQL、一次性迁移、API、Worker、Web，以及 Prometheus、Alertmanager 和
Grafana。PostgreSQL 需要 pgvector；镜像版本与 digest 以 [compose.yml](compose.yml) 和
[配置模板](.env.example) 为准，不使用普通 PostgreSQL 镜像替代含扩展的版本。

API/Worker 与 Web 使用完整提交 SHA 标签和对应镜像 digest。每次成功发布记录应用提交、
迁移、镜像 digest、备份和时间。可移动标签不能作为实际运行版本依据。

API 只映射宿主机 127.0.0.1:18090，Web HTTPS 映射 18443；数据库和 Worker 不映射公网端口。
Cloudflare 代理公网域名，源站只读挂载 Origin 证书。

## 配置来源

完整变量集中维护在 `.env.example`。真实值保存在服务器 `current/.env` 与 `shared/`，不进入
Git、流水线输出或浏览器存储。重要配置分组：

| 分组 | 需要维护的内容 |
| --- | --- |
| 数据库 | 专用数据库密码、含 pgvector 的镜像，保留已有数据卷 |
| 管理员 | 用户名、Argon2id 密码哈希、至少 32 字节会话签名密钥 |
| GitHub | App ID、私钥文件、Webhook 密钥、允许的安装/组织/仓库 |
| AI | API/Worker 共用的 AES-GCM 主密钥与版本；供应商密钥在管理界面加密保存 |
| 检索 | `OPENREVIEWER_RETRIEVAL_API_DISABLED` 默认 true，保持显式外部调用控制 |
| 容量 | Worker 副本、租约、停止超时、清理批次和保留期 |
| TLS/监控 | 只读证书、可选告警地址文件、监控固定配置 |

普通成员由团队管理页维护；旧额外账号文件只用于一次性导入。配置管理员不能在页面停用。
仓库月度预算、审批时限、并发与方案通过团队管理和协作运营页面维护。

## GitHub App 权限

| 权限 | 安装权限 | 用途 |
| --- | --- | --- |
| Metadata | Read | 仓库身份 |
| Contents | Read | 代码与 diff |
| Pull requests | Read/Write | 读取 PR，显式发布时写评论 |
| Checks | Read/Write | 读取 CI，显式发布时写 Check |
| Commit statuses | Read | 读取状态 |

安装需要接受权限更新。每次签发 token 继续按用途收窄：Worker 读取只申请只读权限，发布
只申请 checks/pull_requests 写权限。令牌短期保存在内存，修改安装权限后的客户端更新交由
下一次受控发布完成。

## 自动发布顺序

1. CI 使用隔离 PostgreSQL 完成 Python 类型、契约、自动化测试、覆盖率和规模检查。
2. 完成前端类型、自动化测试、覆盖率与构建，审查依赖和源代码安全。
3. 验证完整 Compose、构建并扫描不可变镜像，发布 GHCR。
4. 通过受限 SSH 从 niuma 跳板机到 niuma-2，调用固定部署入口。
5. 校验配置和运维入口 digest，拉取镜像，检查 PostgreSQL。
6. 创建 custom-format 备份，并在临时数据库真正恢复验证。
7. 停止并确认旧应用退出，再向前执行 Alembic 迁移，启动新版本并检查健康和 readyz。
8. 全部通过后原子切换 current，并写入 release.info。

应用升级包含新的 Nginx 路径白名单，API 与 Web 必须使用同一提交版本。迁移只能新增后续
修复，已经发布的迁移文件不回改。

## 目录与权限

```text
/opt/openreviewer/
  current -> releases/<commit-sha>
  releases/<commit-sha>/   compose、digest、.env 和 release.info
  backups/                备份与校验文件
  restores/               恢复记录
  shared/github/          GitHub App 私钥
  shared/secrets/         AI 主密钥、旧账号导入、可选告警地址
  shared/tls/             TLS 证书和私钥
```

`.env` 为 0600 root:root，敏感目录为 0700；需要容器读取的密钥文件为 0640 root:root、只读挂载。
API/Worker 以非 root UID 10001、GID 0 运行。Web、API 与 Worker 使用只读根文件系统、移除
capabilities 并启用 no-new-privileges。发布和恢复不得删除 `openreviewer-postgres-data`。

部署入口为 `/usr/local/libexec/openreviewer-deploy`，另有同目录 backup 与 restore 入口。
服务器已安装脚本必须与本提交的 helper digest 一致。更改脚本或首次安装时，应单独安排受控
更新；普通业务发布不手工修改服务器脚本或配置。

GitHub Environment `niuma-2` 维护专用 SSH 私钥、主机指纹、跳板机、目标主机和用户。
目标公钥使用 forced command，关闭交互 shell、PTY 和转发；跳板机仅允许到目标 SSH 端口。

## 失败、备份与恢复

发布由 deploy.lock 串行化。失败版本不更新 current；应用健康检查失败时尝试恢复上一应用
镜像。数据库迁移只向前执行，不自动 downgrade。迁移失败保持应用停止，先核对日志与库
版本，再决定恢复或修复，不能让旧代码盲目启动在未知结构上。

Worker 在停止信号后停止领取新任务，完成当前安全边界；默认停止宽限 960 秒，可配置为
30～3600 秒，应覆盖允许的最长模型请求。强制终止后由新 Worker 恢复过期租约。

定时备份使用 openreviewer-backup.service/timer；每份备份都验证恢复并附带 SHA-256。
默认保留 14 份已验证备份和 5 个应用版本。恢复操作使用 [restore.sh](restore.sh) 的参数与
校验规则，固定备份和目标提交，记录结果；不临时拼接 SQL 或删除原卷。

异地镜像由 root 管理的固定可执行文件提供。配置 `OPENREVIEWER_BACKUP_MIRROR_COMMAND` 后，
入口只传入 dump 与校验文件两个参数；开启 REQUIRE_MIRROR 后，镜像失败保留本地备份并停止
旧备份清理。异地上传命令必须位于 /usr/local/libexec，且不可被非 root 修改。

## 密钥轮换

保留旧解密 key version，将新密钥作为当前版本，使用 `apps.maintenance.rotate_ai_secrets`
分批重加密。供应商、Agent、检索和不可变审查方案均参加轮换。工具先批量读取、在事务外
加解密，再比较旧密文批量更新；不会覆盖管理员同时更换的凭据。

确认所有记录均已迁移后才能移除旧解密密钥。不要直接替换主密钥而丢弃旧版本，否则历史
方案和已有配置将无法解密。部署变量与命令参数以配置模板和该入口的 --help 为准。

## 只读排查顺序

先看对应提交的 Actions 及部署日志、release.info、容器状态和迁移版本，再看任务错误、
Worker 心跳、Outbox 与供应商通道。需要服务器命令时先明确环境和授权范围。

配置检查使用 `docker compose config --quiet`，避免输出展开后的凭据。健康、readyz、metrics
只从回环端口或内部网络读取；公网 Nginx 拒绝未允许的 API 路径。真实密码不放入命令行参数
或 shell 历史。

监控规则覆盖 Worker 失联、Outbox 堆积、延迟和连续失败。告警地址文件为空时使用 noop
接收器；启用外部通知需要明确配置。普通任务、会话与事件按现有保留期分批清理；人工评测、
请求账本、工作项和方案有独立生命周期，不能随源审查一起删除。

审查、预算、检索和评测的准确口径分别见 [文档导航](../docs/README.md)。
