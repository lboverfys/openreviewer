# 版本化代码混合检索契约

## 范围与执行
首版索引 Java、MyBatis XML 和 Markdown。文件来源是 GitHub 的指定提交树或经 Git Blob 哈希验证的本地 Git 快照导出。每次任务固定 head_sha；完整索引只有在所有代码块、向量及关系写入成功后才变成 ready。

索引上限：1000 文件、20000 代码块、单文件 256 KiB；GitHub 来源总正文 32 MiB。索引构建使用五分钟可续租租约和随机所有权标识。外部 HTTP 在事务外执行，事务内只完成有界批量写入或状态切换。

解析缓存按 parser_version / file / blob_sha 及 XML 提取规则版本复用；向量缓存按接入域名、模型、维度、切分版本与 embedding_text 内容哈希复用。删除文件不会进入新快照；重命名重新建立位置身份，内容相同的向量仍可复用。

Java 仅做静态语法解析与可确定的直接引用。接口代理、反射、运行期多态和无法解析的接收者保留未解析状态。关系召回最多展开两跳，第一跳是直接引用或 Mapper—XML，第二跳可补 SQL 片段引用。该关系图不声称覆盖完整运行期调用图。

## 三路召回与排序
- BM25：方法名、类名、表名、路径、代码正文，保留原始标识符并拆分驼峰和下划线。
- 向量：固定 1024 维；默认 qwen3.7-text-embedding。SQL 查询限制在指定代码索引快照内，精确余弦距离作为当前基线。
- 关系：从实际变更涉及的方法或显式 seed 出发，一条 JOIN 查询读取最多两跳的有界关系。
- RRF：按各路名次融合，常数 k=60，同路重复结果只计一次；最多保留 30 个融合候选。
- 精排：默认 qwen3.7-text-rerank；只处理融合后的有界代码片段。输入超过当前请求边界时记录明确警告并使用 RRF 次序。
- 最终上下文默认最多 8 条、总正文最多 24 KiB；保持原始片段及哈希，不把原始分数显示成准确率百分比。

任务的三个审查角色按各自职责构造查询，首次成功取得的上下文按 review_run_id / plan_fingerprint / agent 唯一保存。失败重试复用这份快照，不能悄悄换成另一提交或另一模型配置的上下文。

## 证据引用
新增 context_evidence 提供固定 reference_id、文件、SHA、Blob、符号、行号、原文及内容哈希。模型通过 context_references 引用它们；平台拒绝未知引用和内容哈希不一致的片段。

模型的行内评论位置继续绑定本次 diff 的 Review Unit。未变更代码用于支持跨文件判断，不能据此把评论发到不属于本次 diff 的行。源码匹配不等于业务结论正确，人工裁决仍独立保存。

## HTTP 管理接口
沿用同源校验、登录态和 knowledge:manage 权限；审查详情中的检索记录使用 reviews:view，并在 SQL 查询阶段按已有仓库范围过滤。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET / PUT | /api/v1/retrieval/settings | 脱敏设置与版本校验更新 |
| POST | /api/v1/retrieval/settings/test | 小型真实模型连接验证 |
| GET / POST | /api/v1/retrieval/indexes | 列出索引 / 从已有审查任务入队 |
| GET | /api/v1/retrieval/indexes/{id} | 索引详情 |
| POST | /api/v1/retrieval/indexes/{id}/retry | 重试失败索引 |
| POST | /api/v1/retrieval/indexes/{id}/search | 执行有界检索 |
| GET | /api/v1/reviews/{id}/retrieval | 该审查的上下文快照 |
| GET | /api/v1/retrieval/evaluations | 固定样本评测结果 |

Key 使用现有 AES-GCM 密钥环，使用 retrieval_aliyun 独立关联数据；现有轮换工具同时处理新增密钥。接入域名仅允许百炼官方域名，复用 DNS 固定的 HTTPS 传输、禁止重定向，原始错误内容不进入管理接口。

OPENREVIEWER_RETRIEVAL_API_DISABLED=true 时，真实向量和精排请求会在发送前被拒绝；现有缓存、解析、BM25 与模拟接口测试仍可使用。暂停时拒绝新建和重试索引，Worker 不领取排队索引；历史上下文可读取。部署模板默认暂停，得到明确测试范围后才开启。

## 查询规模与索引
- code_indexes：按状态及 lease_until 领取，列表以 repository_id / created_at 索引为基础，最多 50 条。
- code_chunks / code_parse_cache：内容寻址主键，解析缓存按最多 1000 个文件键批量读取，正文仅按最多 150 个候选 ID 一次 JOIN 读取。
- code_embeddings：内容寻址主键、configuration_key 索引，以及 pgvector HNSW 索引。当前实现的候选查询使用快照内精确搜索，HNSW 作为独立对照实验，不能将 HNSW 存在误称为已用于主查询。
- code_index_chunks：主键 (index_id, chunk_id)，外加 embedding_id 索引；快照过滤命中主键前缀。
- code_relations：主键 (index_id, source_id, target_id, kind)，一次有界 JOIN 读取候选关联。
- retrieval_traces：审查与创建时间、索引与创建时间索引，按 review / plan / agent 唯一冻结。
- retrieval_evaluations：索引与创建时间索引，有界列表。
- 检索数据库查询次数为 O(1)，与候选数量无关；全量索引写入和模型请求为 O(批次数)，每批最多 20 条向量 / 200 条代码块。该批处理边界用于控制请求尺寸和崩溃恢复。

## 评测口径
检索数据集明确 annotation_source，代理标注不冒充独立人工金标。所有策略使用同一查询和变更线索，含向量的策略统一预热查询向量后测量；纯 BM25 报告将向量与查询缓存标为未使用。记录 Recall@K、MRR、中位耗时、P95、具体样本排名和警告。

当前 NiuMa 集合包含 20 条 SQL/方法上下文样本，按业务域区分 development / validation。相关性在方法或 SQL 语句级标注；分片命中不代表模型已理解完整业务语义。报告不把检索结果宣称为真实代码缺陷准确率，真实审查准确率保持未评测。

费用由服务商控制台统计。本功能不新增费用计算。

评测执行 n 条独立查询时是 O(n) 次检索，最多 100 条样本；查询向量先批量预热。精排接口每次只接受一个查询，无法合并不同查询的精排请求，因此正式评测必须事先限制样本和请求数量。
