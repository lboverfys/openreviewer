# GitHub Webhook 接入契约

## 1. 入口与身份

公开入口固定为 `POST /webhooks/github`。API 必须先对原始请求体校验
`X-Hub-Signature-256` 的 HMAC-SHA256，再解析 JSON。密钥来自
`OPENREVIEWER_GITHUB_WEBHOOK_SECRET` 或对应的 `_FILE` 配置，二者不能同时设置。

请求体默认上限为 256 KiB，应用会同时检查 `Content-Length` 和实际流式读取长度；
Nginx 的专用 location 使用相同上限。超限请求返回 `413`，不会进入数据库。

## 2. 事件白名单

首版只入队 `pull_request` 的以下动作：

- `opened`
- `synchronize`
- `reopened`
- `ready_for_review`

已正确签名但不支持的事件或动作返回 `202` 和 `accepted=false`，不会创建 delivery、
运行、任务或 Outbox。签名无效返回结构化 `401`。

## 3. 幂等与事务

`X-GitHub-Delivery` 是 `github_webhook_deliveries` 的主键。第一次接收支持的 delivery
时，同一个数据库事务会写入或刷新 installation 和 PR 版本，并创建 delivery、
`ReviewRun`、`ReviewTask` 和 Outbox 事件。

重复 delivery 与原始请求体 SHA-256 相同时返回原任务且 `created=false`；同一 delivery
携带不同请求体时返回 `409 webhook_delivery_conflict`。数据库不保存原始 Webhook 请求体。

## 4. GitHub App 最小权限

当前完整闭环需要接收 PR Webhook、读取 PR/CI/Blob 上下文，并在人工批准后发布 Check Run、
行内 Review 和 PR 汇总评论，因此应申请：

| 权限 | 级别 | 用途 |
| --- | --- | --- |
| Metadata | Read-only | GitHub App 默认仓库身份信息 |
| Pull requests | Read and write | 接收事件、读取 PR，并发布人工批准的行内 Review 和汇总评论 |
| Contents | Read-only | 读取私有仓库 PR 的完整 diff 表示 |
| Checks | Read and write | 读取 Check Runs，并在人工发布时创建或更新 OpenReviewer Check Run |
| Commit statuses | Read-only | 读取当前 `head_sha` 的 Commit Statuses |

后续使用 Blob API 补充大文件时复用现有 `Contents: Read-only`。当前发布载体包含 PR 汇总评论、
Check Run 和行内 Review。读取阶段仍只签发 `contents/pull_requests/checks/statuses: read`
Token；人工发布阶段单独签发 `checks/pull_requests: write` Token。当前也不申请 Contents
写权限、Administration、Workflows 或 Secrets。修改 GitHub App 权限后，现有安装必须单独接受
权限更新，API 与 Worker 也必须重新签发 installation token 才能使用新权限。

订阅事件仅启用 Pull request。GitHub App 私钥、Webhook secret 和 installation token
不得写入仓库、日志、任务错误、Dashboard 或 API 响应。

## 5. 查询与数据规模

delivery 通过主键查询，PR 版本通过唯一 `review_version_key` 冲突更新。一次请求使用固定
数量的数据库操作，查询次数为 `O(1)`，没有循环内查询。任务领取使用现有可领取索引；
过期租约恢复每批最多 100 条并一次 JOIN 取回关联运行。
