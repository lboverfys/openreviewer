# 审查契约

本目录用于记录 Webhook 事件、审查运行状态、Finding 结构、固定 Agent 工作流和 GitHub 发布边界。

当前版本见 [review-contract.md](review-contract.md)。

内部任务创建接口见 [review-task-api.md](review-task-api.md)。

Worker 的领取、租约、恢复和 PR/CI 状态边界见 [review-worker.md](review-worker.md)。

管理员登录、Dashboard 和实时事件接口见 [management-api.md](management-api.md)。

GitHub Webhook 的验签、过滤、去重和原子入库见 [github-webhook.md](github-webhook.md)。

GitHub App 短期身份、PR/diff/CI 读取和版本保护见 [github-context.md](github-context.md)。

`AGENTS.md` 规则作用域、批量读取和 Review Unit 规划见
[review-planning.md](review-planning.md)。

OpenAI/Anthropic 结构化调用、上下文自动分批、模型用量、成本和 Finding 持久化见
[model-review.md](model-review.md)。

安全、规范、逻辑、汇总四 Agent 固定 DAG、独立配置、可恢复批次和版本化 RAG 见
[agent-workflow.md](agent-workflow.md)。

批准门、稳定幂等键、PR 版本复核和 GitHub 汇总评论见
[github-publishing.md](github-publishing.md)。

管理界面的 AI 草稿、连接测试、激活、密钥加密、revision 和审计契约见
[ai-settings.md](ai-settings.md)。

Markdown 知识库的数据库持久化、文档编辑、不可变版本、归档、检索、Worker 快照和管理 API
见 [knowledge-management.md](knowledge-management.md)。
