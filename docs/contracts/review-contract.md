# 审查契约 v0

本文档描述跨阶段保持稳定的领域边界。各适配器的实际进度以路线图为准。

## 1. 事件范围

第一版只接受 GitHub `pull_request` 事件中的以下动作：

| 动作 | 含义 |
| --- | --- |
| `opened` | 新建 PR |
| `synchronize` | PR 推送了新提交 |
| `reopened` | 重新打开 PR |
| `ready_for_review` | Draft PR 转为可审查 |

其他动作不创建审查任务。Webhook 原始投递使用 `X-GitHub-Delivery` 作为去重键；同一投递重复到达时，只能更新接收记录，不能重复创建任务。

领域对象把 `delivery_id` 原样暴露为 `deduplication_key`。真正的唯一约束由后续 PostgreSQL 持久化层实现。

## 2. 审查版本和线程

审查版本必须绑定稳定的 GitHub 仓库数字 ID，而不是仓库名称：

```text
review_version_key = {repository_id}:{pull_request_number}:{head_sha}
thread_id = {review_version_key}:{review_run_id}
```

`head_sha` 在进入领域模型时规范化为小写的 40 到 64 位十六进制字符串。新提交产生新的审查版本；旧版本不能覆盖新版本的 Check 或评论。

## 3. 状态和结论

代码枚举名使用大写，JSON 序列化值使用小写蛇形命名。

### 执行状态

```text
queued | waiting_for_ci | running | ready_for_review | completed | failed |
timed_out | cancelled | superseded
```

### 审查结论

```text
no_confirmed_findings | findings_present | needs_human |
indeterminate | not_applicable
```

### 覆盖状态

```text
complete | partial | unknown | stale
```

`completed` 运行必须同时提供审查结论。模型失败、上下文超限或 CI 结果缺失不能表示为“审查通过”。

## 4. Finding

每条 Finding 至少包含：

| 字段 | 规则 |
| --- | --- |
| `fingerprint` | 跨提交识别同一问题的稳定指纹，不应依赖原始行号 |
| `head_sha` | 产生该结论的提交 |
| `severity` | `critical`、`high`、`medium` 或 `low` |
| `category` | 架构、鉴权、安全、数据库、业务契约、测试缺口或可靠性 |
| `location` | 可选；无法精确定位时只进入 Summary |
| `title` | 简短的问题标题 |
| `evidence` | 可复查的代码证据 |
| `impact` | 触发条件和行为影响 |
| `suggestion` | 可执行的修复方向 |
| `required_test` | 可选的回归测试建议 |
| `confidence` | 0 到 1 的模型特征，不等同于真实正确概率 |
| `verification_status` | `unverified`、`verified` 或 `rejected` |
| `rule_reference` | 可选的规则或文档依据 |

文件路径的契约要求是仓库相对路径，禁止 Linux 绝对路径、Windows 盘符/UNC 路径、控制字符、
空路径段和 `..` 越界。验证器会把反斜杠转换为正斜杠。行号从 1 开始，结束行不能小于开始行。

## 5. 行内评论准入

只有同时满足以下条件，Finding 才具备行内评论资格：

- `verification_status` 为 `verified`；
- 存在 `location`；
- 位置落在当前提交的 diff 中；
- 位置位于新增代码一侧（`right`）。
- Finding 的 `head_sha` 与 PR 当前 `head_sha` 完全一致；
- 置信度达到仓库策略门槛，M0 默认值为 `0.90`；
- 不是普通测试缺口。

不满足条件的 Finding 只能发布到 Check Summary 或人工确认区域。这里得到的仍然只是“行内评论候选”；风险域的历史评测准入和自动降级由后续策略层实现。GitHub 适配器稍后再把领域层的 `right`/`left` 转换为 GitHub API 所需格式。

## 6. 当前不在契约内的内容

- GitHub App 鉴权和安装 Token 获取；
- PostgreSQL 表、任务租约和 Outbox；
- 模型 Prompt、模型厂商响应格式；
- 飞书通知和人工审批；
- 测试候选分支及自动部署。
