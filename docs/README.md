# 文档导航

先读项目首页，再按当前问题进入对应文档。接口字段以自动生成的 OpenAPI 为准，业务口径以
以下契约为准；阶段性的施工记录不再与当前说明并列维护。

| 需要了解什么 | 文档 |
| --- | --- |
| 系统怎么分层、为什么这样设计 | [架构与取舍](architecture.md) |
| GitHub 事件如何变成可审批、可发布的审查结果 | [审查流程](contracts/review-flow.md) |
| 权限、预算、待办、方案、调度和故障隔离 | [团队运营](contracts/platform.md) |
| 索引、混合检索、知识版本和证据范围 | [检索与知识](contracts/retrieval.md) |
| 如何构建样本、算指标、解释效果边界 | [评测与证据](contracts/evaluation.md) |
| 查询次数、索引、规模验证和性能记录 | [性能说明](performance.md) |
| 部署、备份、恢复、配置与排障 | [部署手册](../deployment/README.md) |
| 简历怎么写、面试怎么展开 | [项目陈述与面试准备](interview.md) |

机器契约保留 [OpenAPI](openapi.json) 和[真实评测 JSON Schema](contracts/real-evaluation.schema.json)。
前端类型由 OpenAPI 生成；生产表结构由 Alembic 迁移维护，不在文档中再复制一份字段清单。

历史设计讨论与阶段报告可以在 Git 历史中查询。历史性能记录必须连同日期、环境和测量条件
引用，不能套用到当前版本或真实模型质量上。
