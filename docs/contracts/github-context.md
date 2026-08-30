# GitHub PR 与 CI 上下文契约

## 1. 身份与凭据

Worker 与 API 使用 `OPENREVIEWER_GITHUB_APP_ID` 和只读挂载的 RSA 私钥签发最长 9 分钟的
GitHub App JWT，再向 `POST /app/installations/{installation_id}/access_tokens` 申请短期
installation token。Token 只存在于 Worker 进程内存，按 installation 缓存，并在距离过期
不足 2 分钟时刷新；数据库、日志、错误详情和任务事件都不保存 Token 或私钥内容。

当前读取阶段需要以下 GitHub App 仓库权限：

| 权限 | 级别 | 用途 |
| --- | --- | --- |
| Metadata | Read-only | 校验仓库稳定数字 ID 和名称 |
| Pull requests | Read and write | 读取 PR；API 在人工发布时创建行内和汇总评论 |
| Contents | Read-only | 读取私有仓库 PR 的完整 diff 表示 |
| Checks | Read and write | 分页读取 Check Runs；API 在人工发布时创建或更新 Check Run |
| Commit statuses | Read-only | 分页读取当前 `head_sha` 的 Commit Statuses |

GitHub 对私有仓库的 PR diff 表示会同时校验 `Pull requests` 和 `Contents: Read-only`。
读取阶段签发的 installation token 只申请 `contents/pull_requests/checks/statuses: read`；人工
发布阶段另签发仅含 `checks/pull_requests: write` 的 Token。当前不需要 Contents 写权限、
Administration、Workflows 或 Secrets。人工发布的版本复核、幂等标记和评论边界见
[github-publishing.md](github-publishing.md)。

## 2. 读取顺序与版本保护

Worker 领取任务后按下面的顺序工作：

```text
一次性延长有界处理租约
  -> 获取 PR 元数据并核对仓库 ID、仓库名和 PR 编号
  -> 比较 GitHub 当前 head_sha 与任务 head_sha
  -> 仅当前、打开且非 Draft 的 PR 继续读取文件和 CI
  -> 短事务保存快照并推进任务状态
```

如果 GitHub 当前 `head_sha` 已变化，任务直接进入 `superseded`，不会下载文件、读取 CI 或
保存新 SHA 的上下文。关闭或重新变为 Draft 的 PR 进入 `cancelled`。同一仓库和 PR 下其他
活动旧版本通过批量更新进入 `superseded`，覆盖状态同时变为 `stale`。

升级前创建、尚未保存完整 PR 身份的历史任务，可以由详情页调用
`POST /api/v1/reviews/{review_run_id}/identity/sync` 单独读取一次 PR 元数据。该操作只回填作者、
PR 链接、来源仓库/分支和目标仓库/分支，并记录 `identity_fetched_at`；即使 GitHub 当前
`head_sha` 已变化，也不会用当前 SHA、标题、状态或文件数改写旧版本快照。

## 3. 文件与 diff

changed files 每页最多 100 条，总数最多 3000 条。完整 diff 使用 PR diff 表示读取，并通过
标准 unified diff 解析器处理新增、修改、删除、重命名和二进制文件。每个文本补丁最多保存
8 MiB；这个边界允许常见的大文件继续进入模型层按行切片。超过上限、缺失或无法完整解析时，
文件仍会保存，但分别标记为 `too_large`、
`missing` 或降低 `diff_complete`，不能静默当成完整覆盖。

文件表只保存路径、旧路径、Git blob SHA、增删行计数、补丁状态和有界补丁文本，不保存整份
GitHub JSON。首次成功准备上下文后，正常 CI 轮询不重复下载文件，只刷新 PR 身份和 CI。
整轮 GitHub 读取默认还有 8 分钟总时间预算；发起下一页前若已超时，会按可重试错误退出，
保证不超过一次性 10 分钟上下文租约。

## 4. CI 汇总

Worker 合并当前 `head_sha` 的 Check Runs 与 Commit Statuses，最多保留 1000 个条目。平台按
GitHub App ID 排除 OpenReviewer 自己的 Check，避免等待自身结果。汇总状态为：

| 状态 | 含义 |
| --- | --- |
| `not_configured` | 已完整读取当前提交，但没有任何可见的 Check Run 或 Commit Status |
| `unknown` | 分页、权限或响应不完整，无法证明当前检查集合完整 |
| `pending` | 至少一项仍在排队或运行，且没有失败终态 |
| `success` | 所有可见检查均为成功、跳过或中性终态 |
| `failure` | 至少一项失败、错误、取消、超时或需要操作 |

`unknown` 和 `pending` 会按默认 30 秒间隔重新入队，正常轮询不增加任务失败尝试次数。默认等待
上限为 1 小时，到期进入 `timed_out`，不得显示为审查通过。`not_configured`、`success` 和
`failure` 都表示 CI 读取已经终止，任务进入 `ready_for_review`；其中 `not_configured` 只表示
没有可见 CI 门禁，不能解释为 CI 通过。`ready_for_review` 只代表可以进入后续模型阶段，不代表
CI 成功。

## 5. 查询与数据规模

- GitHub 请求按页批量执行，文件最多 30 页，单类 CI 最多 10 页，没有逐文件 HTTP 请求；
- 数据库读取任务目标使用任务主键 JOIN 和版本唯一键，查询次数为 `O(1)`；
- 文件和 CI 快照使用批量删除与批量插入，不在循环中查询数据库；
- 旧版本失效使用运行索引 `repository_id + pull_request_number + execution_status` 和两条批量
  `UPDATE`，数据库查询次数为 `O(1)`；
- 文件、CI 的版本删除和读取使用各自复合唯一索引的首列，不需要全表扫描。
