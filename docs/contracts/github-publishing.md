# GitHub 人工发布契约

## 1. 人工门

模型结果保存后，真实工作流停在 `awaiting_approval`。管理员可以先确认或忽略单条 Finding，
然后批准整份审查；批准只进入 `awaiting_publish`，不会访问 GitHub。管理员再次点击“发布到
GitHub”后，API 才执行外部动作。发布成功进入 `completed`，失败回到 `awaiting_publish`，可用
同一幂等键重试。

当前外部载体是一条 Pull Request 汇总评论，不是 Check Run 或行内评论。人工标记为
`rejected` 的 Finding 不进入评论；其余候选包含严重度、类别、位置、证据、影响、建议和补测
建议。没有可发布 Finding 时仍发布明确的“没有需要发布的问题”结论。

## 2. 版本保护与幂等

发布器先分页查找评论首行的隐藏稳定标记：

```text
<!-- openreviewer-review:<review_run_id 的 24 位 SHA-256 摘要> -->
```

找到完整标记行即视为同一运行已经发布，不再次 POST。未找到时，发布器在 POST 前重新读取 PR，
要求编号一致、状态为 open、不是 Draft，且当前 `head.sha` 与审查运行完全一致。版本已变化时
拒绝发布旧结果。GitHub 在检查与 POST 之间不提供跨请求事务，因此仍以稳定标记承担网络超时
后的最终去重。

API 的发布按钮使用 `ui:publish:<review_run_id>` 稳定幂等键。数据库先用短事务保存
`publishing` 和外部动作尝试，提交后才访问 GitHub；成功或失败再各用一个短事务收口。外部 HTTP
不在数据库事务中。卡在 `publishing` 超过五分钟的尝试允许恢复，同一动作成功后重复请求直接
返回成功状态。

## 3. 内容与安全

评论 UTF-8 大小上限为 60 KiB，超过时在字符边界截断并明确注明省略。单字段最多 2000 字符；
模型文本先递归脱敏并压成单行。评论、事件和错误不包含 Token、私钥、Cookie、密码或模型完整
响应。评论查重最多读取 10 页、每页 100 条，所有 GitHub 请求都有响应大小和超时边界。

发布 API 使用 GitHub App 短期 installation token。API 容器与 Worker 都只读挂载同一 App
私钥；Token 只缓存在进程内存。所需仓库权限为：

| 权限 | 级别 | 用途 |
| --- | --- | --- |
| Metadata | Read-only | 校验仓库身份 |
| Pull requests | Read and write | 读取目标 PR 并创建 PR 评论 |
| Contents | Read-only | Worker 读取私有 PR diff |
| Checks | Read-only | Worker 读取 CI Check Runs |
| Commit statuses | Read-only | Worker 读取 Commit Statuses |

不需要 Contents、Checks、Actions、Administration 或 Secrets 写权限。修改 GitHub App 权限后，
已有安装必须由管理员接受更新请求，并重启 API 与 Worker 以丢弃旧 installation token 缓存。
