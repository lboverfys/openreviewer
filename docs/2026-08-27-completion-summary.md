# OpenReviewer 实施与上线验收总结（2026-08-27）

## 结论

本轮工作已按 `工作.md` 完成从功能实现、测试、生产部署到真实 GitHub 发布的完整闭环。当前生产服务正常运行，真实审查运行已进入 `completed`，PR 评论已成功写回，暂无遗留阻塞。

## 一、功能实现

### 固定多 Agent 工作流

- 建立固定 DAG：排队 → CI → 规划 → 安全/规范/逻辑三路并行审查 → 汇总 → 人工批准 → 人工发布。
- 三个审查 Agent 和汇总 Agent 使用独立配置；每个配置支持提供商、Base URL、中转协议、模型、上下文窗口、单批上限、推理档位、超时、重试和启用状态。
- 人工批准前不会出现 GitHub 发布动作；批准后仍需用户手动点击发布。
- 状态变更、人工批准、驳回、发布开始、发布失败和发布完成均写入审计事件。

### 分批审查与结果处理

- 完成大提交分批、超大文件切片和原始文件行号映射，跨片段 Finding 仍指向原始路径和行号。
- 完成批次租约、超时、阶段级重试、重复执行保护和失败恢复；已完成批次不会重复调用模型。
- 完成跨批次 Finding 去重、排序、分级和汇总。
- 模型调用兼容 Chat Completions、中转站默认值、`reasoning=auto`、结构化输出和不支持参数时的降级处理。
- 无 Finding 时仍展示明确的“已完成，未发现问题”结果，不显示空白页面。

### RAG 与管理界面

- 建立可版本化 Markdown 知识库，覆盖仓库规则、通用安全规范、编码规范、数据库规范和历史 Finding。
- 实现有界检索接口，Agent 结果会记录并展示引用来源。
- 设置页按安全、规范、逻辑和汇总分区，支持保存、连接测试、启停和安全掩码。
- 审查详情页展示状态时间线、当前节点、Agent 进度、批次、请求状态、日志、耗时、Token、错误、Finding 和人工操作。

### 数据访问与安全

- 详情读取使用固定数量的有界查询；没有引入循环内数据库查询、N+1 查询或无界全表读取。
- 模型密钥、GitHub 私钥、会话信息和 Webhook 密钥不会出现在日志或 API 明文响应中。
- GitHub 发布使用运行 ID 派生的稳定评论标记，支持失败后的安全重试和幂等发布。

## 二、测试与验证

| 验证项目 | 结果 | 说明 |
| --- | --- | --- |
| 后端测试 | `190 passed, 4 skipped` | 跳过项是本机没有真实隔离 PostgreSQL |
| 前端 Vitest | `25 passed` | 通过 |
| TypeScript 检查 | 通过 | 通过 |
| Vite 生产构建 | 通过 | 构建产物位于 `D:\rubbish\zhongjian\build\openreviewer\final-audit-20260827-2` |
| Python 编译、`pip check`、`npm ls` | 通过 | 通过 |
| SQLite 迁移与应用启动检查 | 通过 | 通过 |
| 浏览器桌面/移动端验收 | 通过 | 页面、状态和响应式布局均已检查 |

## 三、提交与生产部署

- Git 提交：`acf5d797f547054f507021b3c7f599dc49d9e4c1`。
- 已推送到 `origin/main`，工作区保持干净。
- 当前生产 release：`/opt/openreviewer/current`，指向上述提交。
- 数据库迁移版本：`20260827_0016 (head)`。
- 只重启了本项目的 API、Worker 和 Web，没有重启 PostgreSQL 或其他服务。
- `api`、`worker`、`web`、`postgres` 四个容器均为 `healthy`。
- API 健康检查：`http://127.0.0.1:18090/healthz`。
- Web 健康检查：`https://127.0.0.1:18443/healthz`。

## 四、真实流程验收

### 测试对象

- 仓库：`lboverfys/NiuMa`
- Pull Request：`#48`，标题为“[测试] 验证 OpenReviewer Webhook 入队链路”
- installation ID：`156153422`
- head SHA：`9eeb75e65a2fb0eef8fff029e7a9c5afd6e317f0`
- 运行 ID：`2741a301-d055-47ee-bdde-a88f64ff6927`
- 任务 ID：`5a6d2a04-0ac4-4e42-a478-9b7902377852`

### 验收过程

1. 发送带有效签名的 `synchronize` Webhook，成功创建审查运行。
2. CI、规划、三个 Agent 批次和汇总全部成功；每个 Agent 均有批次、耗时、Token、请求 ID 和引用记录。
3. 审查结果为 0 个 Finding，但详情明确显示 Agent 已完成且未发现问题。
4. 运行先停在 `awaiting_approval`，未批准前可用操作不包含发布。
5. 执行批准后进入 `awaiting_publish`，并写入批准审计事件。
6. GitHub App 权限最初只有 `pull_requests: read`，第一次发布准确失败并回到 `awaiting_publish`，没有伪造成功。
7. App 权限改为 `pull_requests: write`，并由 installation 所有者接受更新后，installation token 权限同步为 `write`。
8. 使用幂等键 `codex-real-publish-2741a301` 重试，发布成功并进入 `completed`。
9. 使用同一幂等键再次复核返回 `completed`，没有产生重复评论。

### 最终结果

- 数据库包含 `review.manual.publish_completed` 事件，载荷标记为 `published: true`。
- PR #48 当前有 1 条 OpenReviewer 评论，稳定标记匹配：
  [查看 GitHub 评论](https://github.com/lboverfys/NiuMa/pull/48#issuecomment-5433284653)
- 评论内容与详情页一致，包含仓库、PR、提交和“没有需要发布的问题”结论。
- 历史上的 `review.manual.publish_failed` 事件保留在审计记录中，用于说明第一次权限不足；不影响最终成功状态。

## 五、当前状态

- 生产版本、数据库迁移、服务健康检查和真实 GitHub 回写均已完成。
- 当前没有需要用户继续处理的部署或代码问题。
- 管理入口：[https://openreviewer.lovecoding.store/](https://openreviewer.lovecoding.store/)。
