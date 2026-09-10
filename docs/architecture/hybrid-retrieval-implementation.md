# 混合检索实施记录

本次目标：Java / MyBatis 的版本化代码索引、BM25 / 向量 / 静态关系召回、RRF、精排、证据引用、评测与管理界面。

## 已确认配置
- 向量：阿里云 qwen3.7-text-embedding，1024 维。
- 精排：阿里云 qwen3.7-text-rerank。
- 项目安全、规范、逻辑、汇总 Agent：gpt-5.6-luna，Responses 协议。
- 费用在服务商后台查看；新增观测仅包含数量、Token、耗时和效果。
- 凭据只保存在服务器私有配置中，本文不记录。
- 开发、依赖、缓存、构建和测试全部在 niuma-2 的独立工作目录进行。
- 不占用 niuma 的应用与数据库资源。

## 实施与验收
1. 固定提交快照，使用静态解析提取代码块与直接关系；通过内容哈希复用向量。
2. 查询按索引版本隔离；召回仅返回有界候选，正文按候选 ID 批量读取。
3. 以 RRF 融合三路候选，保留单路和融合基线，精排单独开关。
4. 每条上下文包含文件、行号、Blob、提交、代码块哈希与召回来源。
5. 页面展示索引状态、检索过程与策略评测对比；未知指标不伪造为零或百分比。
6. 合成样本、真实代码样本、自动标注和独立人工标注明确区分，自动标注不能充当独立人工金标。
7. 运行迁移、权限、查询次数、真实模型接口、前端和浏览器检查，记录实际结果。

源码基线：ec5796492088eb5f594df2f0e4f73d197b459150。

## 解析器兼容性验证
Tree-sitter 0.26.0 在 Python 3.12.14 / Linux 对 NiuMa RegistrationProperties.java 的解析中可重复触发进程段错误。0.25.2 已通过 948 个真实仓库文件的逐文件解析，因此锁定 0.25.2；Java grammar 为 0.23.5。

## 外部调用暂停

用户要求先完成工程工作，真实向量与精排测试另行说明规模后再开启。服务器预览运行环境设置 OPENREVIEWER_RETRIEVAL_API_DISABLED=true，同时检索设置 enabled=false。生产 Compose 默认也暂停外部检索请求。暂停期间不重试真实模型、不续建索引；单元和集成测试使用模拟传输。

已持久化向量不得随失败删除。接口错误 insufficient_quota 明确显示为模型额度不足，不能记为模型质量或解析错误。

## 2026-09-10 交付状态

以下为正式发布前的离线验收记录。用户随后要求通过现有 CI/CD 上线，并在上线验证后清理隔离环境；发布版本以 GitHub Actions 和 /opt/openreviewer/current/release.info 为准。

验证记录均位于服务器工作目录 artifacts：

| 检查 | 实际结果 | 记录 |
| --- | --- | --- |
| 后端完整回归（末次局部修正前） | 513 项通过，覆盖率 75.97% | acceptance-tests.log、final-coverage.json |
| 最新 XML、暂停保护、检索与 API 回归 | 27 项通过 | latest-retrieval-check.log |
| 后续 BM25 评测语义修正 | 2 项通过 | report-semantics-check.log |
| 前端完整回归 | 117 项通过 | acceptance-web.log |
| 新增暂停场景及检索页面回归 | 4 项通过 | latest-web-check.log |
| Nginx 路由与拒绝规则 | 51 项通过；现有镜像 nginx -t 通过 | nginx-routes-check.log、nginx-config-check.log |
| 静态检查 | 仓库 CI 范围 Ruff 与新增迁移通过；Mypy 88 个生产文件通过 | latest-retrieval-check.log |
| 浏览器检查 | Chrome DevTools：登录、暂停提示、BM25 搜索、SQL 证据展开、评测和配置页通过 | 隔离预览 |

原有迁移脚本存在 38 个导入排序/旧类型写法提示，位于仓库既有 CI 的 Ruff 检查范围之外。本次没有改写历史迁移。

实际 Git 快照 c7dfb376d49a3c81ed106d001b121def64b6aa13：948 个文件，10,029 个代码块，15,279 条静态关系，无解析错误；每个源文件均通过 Git Blob 哈希校验。XML 注释伪节点修正后，上述数量和关键词基线没有变化。记录见 latest-parser-verification.json。

20 条代理标注样本的 BM25 基线 Recall@8 为 0.85，MRR 为 0.2806547619；不是独立人工金标，不代表缺陷审查准确率。真实混合检索、精排增益和最终缺陷准确率尚未验收。

复用 8,327 条现有向量所做的暖缓存微基准中，精确搜索中位 187.15 ms，HNSW 中位 16.13 ms，10 条查询 overlap@10 平均 1.0。该结果仅属于单模型缓存表实验，不是带快照过滤的生产查询；不能据此声称正式检索获得同样加速。记录见 vector-search-benchmark.json。

上线前已将正式数据库备份恢复到 PostgreSQL 16.15 的独立验证库，65 条审查记录保留，0046–0048 迁移与 Alembic 结构检查通过。发布脚本会先验证旧库备份，再停止应用、切换数据库镜像并迁移。Nginx 新检索路由已纳入白名单，长检索请求使用独立超时配置；原有 API 拒绝规则保留。

## 数据访问与规模

新增访问 code_indexes、code_chunks、code_embeddings、code_index_chunks、code_relations、code_parse_cache、retrieval_settings、retrieval_traces、retrieval_evaluations，并扩展 review_runs / review_findings 的证据关联。

当前真实快照约一万块，MVP 限制两万块。单次检索按主键、快照关联主键和关系复合主键查询；候选正文一次 JOIN 批量取回，SQL 次数与候选数无关，为 O(1)。首次 BM25 加载使用游标，最多两万条词项元数据。索引按文件 50 条、代码块 200 条、向量 20 条分批，写入与模型请求为 O(批次数)，远程调用在事务外。多角色审查只有三个固定检索角色，独立评测则按样本数 O(n) 执行。

## 调用暂停与下一次受控验收

本轮暂停后没有调用真实向量、精排或审查模型。服务端 OPENREVIEWER_RETRIEVAL_API_DISABLED=true、检索配置 enabled=false；界面也禁用真实检索、连接测试、新建和重试索引。NiuMa 开发索引的 8,327 条向量缓存保留，没有自动续建。

全仓仍缺 1,702 条向量，干跑估算 86 批；该估算只查询缓存并解析代码，不请求模型，也不是后续测试计划。

建议用户明确开启后，先用隔离的小型验收样本验证端到端检索：最多 20 个短代码块（合计不超过 64 KB）和 2 条固定查询。代码向量 1 批、查询向量 1 批、两次精排，正常 4 次接口请求，按现有最多三次尝试计算上限 12 次。该轮不续建 NiuMa 全仓、不触发 GPT 审查；结束后恢复暂停。只有这轮结果确认后，才另行安排真实仓库完整评测。

## 发布前的文件与环境清单

源码最终同步回 D:/learning-continuing/niuma/openreview/openreviewer；本地只接收源码、配置、测试、文档和锁文件。未在本地继续安装、运行测试或构建。

服务器工作目录 /opt/openreviewer/workspaces/hybrid-20260910 约 1.2 GB，不含 Docker 镜像。Python 环境约 222 MB，Node 依赖与源码约 149 MB，缓存约 117 MB，临时测试数据约 412 MB，开发 PostgreSQL 数据约 272 MB，报告与构建约 14 MB。前端最终产物位于 artifacts/web-final，约 610 KB。

已精确清理本次解析器崩溃产生的 3 个 core 转储（约 251 MB）。凭据仅留在服务器 private 目录及加密数据库中；临时数据、私有配置、依赖、缓存和构建文件不会同步回本地仓库。

正式发布验证后，将有用的向量缓存和模型配置迁入正式数据库，删除上述开发工作目录、隔离容器与其专用镜像，关闭预览端口。正式数据卷、监控、发布回退版本与备份继续按原部署规则管理。
