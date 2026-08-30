# GitHub 人工发布契约

## 1. 人工门

模型结果保存后，真实工作流停在 `awaiting_approval`。管理员可以先确认或忽略单条 Finding，
然后批准整份审查；批准只进入 `awaiting_publish`，不会访问 GitHub。管理员再次点击“发布到
GitHub”后，API 才执行外部动作。发布成功进入 `completed`，失败回到 `awaiting_publish`，可用
同一幂等键重试。

当前外部载体包含一个 Check Run、最多 50 条行内评论和一条 Pull Request 汇总评论。只有人工
裁决为 `valid` 的 Finding 会进入 Check 与汇总；同时通过自动源码证据核验、历史评测风险域准入、
置信度、当前 SHA 和新增侧 diff 位置校验的 Finding 才进入行内评论。自动核验只证明指定源码
支持证据片段，不替代人工裁决。GitHub 拒绝行内位置时，
本轮会安全降级到 Check 与汇总。没有可发布 Finding 时仍发布明确的“没有需要发布的问题”结论。

## 2. 版本保护与幂等

发布器使用三类稳定身份进行远端对账：

```text
<!-- openreviewer-review:<review_run_id 的 24 位 SHA-256 摘要> -->
<!-- openreviewer-inline:<仓库、PR、SHA 和 Finding 指纹的 24 位 SHA-256 摘要> -->
openreviewer-<审查版本键的 32 位 SHA-256 摘要>  # Check Run external_id
```

发布器在写入前重新读取 PR，要求编号一致、状态为 open、不是 Draft，且当前 `head.sha` 与审查
运行完全一致。随后分页读取已有 Check、Review 和汇总评论：存在时 PATCH Check/汇总，已存在的
行内标记不重复发布。版本已变化时拒绝发布旧结果。GitHub 在对账与写入之间不提供跨请求事务，
因此仍以稳定身份承担网络超时后的最终去重。

API 的发布按钮使用 `ui:publish:<review_run_id>` 稳定幂等键。数据库先用短事务保存
`publishing` 和外部动作尝试，提交后才访问 GitHub；成功或失败再各用一个短事务收口。外部 HTTP
不在数据库事务中。卡在 `publishing` 超过五分钟的尝试允许恢复，同一动作成功后重复请求直接
返回成功状态。

## 3. 内容与安全

Check 文本和汇总评论的 UTF-8 大小上限为 60 KiB，超过时在字符边界截断并明确注明省略。单字段最多 2000 字符；
模型文本先递归脱敏并压成单行。评论、事件和错误不包含 Token、私钥、Cookie、密码或模型完整
响应。评论查重最多读取 10 页、每页 100 条，优先依据 GitHub `Link: rel=next` 判断是否还有
下一页；缺少分页头时，整页结果会继续探测下一页，达到上限且无法证明完整时停止并报错。
Check Runs 另外校验 `total_count`，所有 GitHub 请求都有响应大小和超时边界。

发布 API 使用 GitHub App 短期 installation token。API 容器与 Worker 都只读挂载同一 App
私钥；Token 只缓存在进程内存。所需仓库权限为：

| 权限 | 级别 | 用途 |
| --- | --- | --- |
| Metadata | Read-only | 校验仓库身份 |
| Pull requests | Read and write | 读取目标 PR并创建行内、Review 和汇总评论 |
| Contents | Read-only | Worker 读取私有 PR diff |
| Checks | Read and write | Worker 读取 CI Check Runs；发布器创建或更新 Check Run |
| Commit statuses | Read-only | Worker 读取 Commit Statuses |

应用安装必须具备上述权限，但每次 Token 继续按用途缩小：读取 Token 只申请四项只读权限，发布
Token 只申请 `checks/pull_requests: write`。不需要 Contents、Actions、Administration 或 Secrets
写权限。修改 GitHub App 权限后，已有安装必须由管理员接受更新请求，并重启 API 与 Worker 以
丢弃旧 installation token 缓存。
