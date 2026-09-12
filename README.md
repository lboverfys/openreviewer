# OpenReviewer

面向团队 GitHub PR 的 AI 代码审查平台。首个接入项目是 NiuMa 的 Java / MyBatis 业务仓库。

![CI](https://github.com/lboverfys/openreviewer/actions/workflows/verify-and-publish.yml/badge.svg)

平台固定代码提交，读取 PR 与 CI，组织安全、规范、逻辑及汇总 Agent，保存可恢复的审查结果。
成员确认问题并批准后，再显式发布到 GitHub。审查、处理、计费和评测各自保留可追溯记录。

## 主要能力

- **可恢复审查**：PostgreSQL 任务队列、SKIP LOCKED、租约代次、心跳、独立批次检查点与 Outbox。
- **跨文件证据**：版本化 Java/MyBatis 索引，BM25、向量和静态关系召回，RRF 与精排，引用位置和内容哈希。
- **团队协作**：成员角色与仓库范围、审批负责人、超期待办、问题处理及修复 PR 关联。
- **费用与故障治理**：月度费用预占、幂等结算、未知用量、仓库并发限制、供应商共享通道与熔断。
- **配置追溯**：不可变模型/Prompt/知识/检索方案，按仓库启用与恢复，运行中任务继续使用原方案。
- **经验积累**：人工处理结论生成未启用的知识草稿，明确仓库范围，支持版本与冲突检测。
- **评测闭环**：固定 PR/SHA 的基线与候选、调参与验收划分、双人复核、聚合报告和置信区间。
- **自动交付**：类型、契约、测试、数据库规模验证、依赖与镜像扫描、备份恢复验证和 CI/CD 部署。

## 技术与结构

Python 3.12、FastAPI、SQLAlchemy、PostgreSQL 16/pgvector、React、TypeScript、Nginx、GitHub Actions。
模型支持 OpenAI Responses、Chat Completions 和 Anthropic Messages；向量与精排支持百炼 API。

```text
apps/api/             认证、接口和依赖组合
apps/worker/          启动、运行循环、租约心跳、审查与 Agent 流程
services/             审查、检索、配置、人工经验和供应商协议
persistence/models/   按职责组织的数据记录
persistence/queue/    租约、规划、批次和结果存储
persistence/management/  查询、人工动作与发布存储
domain/               状态、数据契约与业务约束
web/src/              管理页面、共享请求层和分页
migrations/           兼容的 Alembic 向前迁移
```

## 从这里开始

- [文档导航](docs/README.md)：八份核心说明，按问题阅读。
- [架构与取舍](docs/architecture.md)：模块边界、事务、恢复与一致性。
- [项目陈述与面试准备](docs/interview.md)：简历草案、代码入口和可以展开的设计问题。
- [部署手册](deployment/README.md)：环境、GitHub App、自动发布、备份和恢复。
- [性能与证据](docs/performance.md)：查询次数、十万行验证与历史测量条件。
- [OpenAPI](docs/openapi.json)：服务端与前端共同使用的机器契约。

已部署入口：[OpenReviewer](https://openreviewer.lovecoding.store/)。
登录后从“协作与运营”进入待办、用量、方案和诊断，从“团队管理”配置仓库策略。

## 验证与发布

main 的 push 触发现有流水线。CI 在隔离 PostgreSQL 和 Node/Python 环境运行自动化验证，
通过后构建完整 SHA 镜像、扫描镜像并部署；迁移前会创建数据库备份并验证恢复。
完整参数分别由 `pyproject.toml`、`requirements.lock`、`web/package-lock.json` 和
`.github/workflows/verify-and-publish.yml` 维护，文档不再复制会漂移的命令集合。

新预算、仓库并发上限和审查方案均通过管理界面配置。预算按配置价格估算；真实检索 API 的
暂停开关保持优先级。不要把测试夹具复制到生产充当真实评测样本。

## 效果边界

当前服务一个团队的多个仓库。CI 回归、隔离数据库规模验证、检索相关性评测和真实缺陷评测
有不同的口径。没有真实调用与人工复核支撑时，项目不会声称准确率或审查效率提升百分比。
