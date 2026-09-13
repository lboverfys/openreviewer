# 审查知识整理与来源核对

本文件供维护者核对资料来源和取舍，不进入模型知识检索。`knowledge/` 中的 Markdown 只放可独立理解的审查约束；不放来源链接、操作教程、环境清单和开发日志。

## 本轮结果

2026-09-14 将 14 份知识重写、合并为 10 份：5 份通用规则和 5 份 NiuMa 规则。内容不以“企业规范”为名要求增加无实际用途的抽象、分布式锁、消息队列或重复校验。

每个规则章节包含适用场景、需要保持的约束，以及认定缺陷所需的证据或容易误报的例外。章节长度控制在现有检索摘要的 360 字符限制内，避免只取到引言而丢掉约束和例外。仓库范围作为分片元数据保留，不单独形成可召回的正文。

实际审查先用变更文件名召回最多 4 条主题规则，再补各 Agent 职责规则，去重后仍最多 8 条。词法检索同时保留完整标识符，并拆分驼峰和下划线名称；规则标题中的 `ProfileTag`、`Refund` 等是对应业务概念的代码术语，不是外部跳转链接。这样避免通用包路径和职责词把标签删除、退款份额等业务约束挤出上下文。全部检索仍在已有有界内存索引内完成，不增加数据库或模型调用。

| 文件 | 内容与适用范围 |
| --- | --- |
| `repository-rules.md` | 通用：缺陷证据、变更归因、文档冲突和修复验证 |
| `coding.md` | 通用：接口兼容、前端异步状态、失败反馈和实际维护影响 |
| `database.md` | 通用：批量查询、索引、并发写入、事务和迁移 |
| `security.md` | 通用：授权、撤权、注入、文件、回调和敏感数据 |
| `reliability.md` | 通用：幂等、租约、重试、结果未知、事件和资源上限 |
| `niuma/architecture.md` | NiuMa：模块与表所有权、错误码、序列化、数据生命周期 |
| `niuma/authorization.md` | NiuMa：账号、单一角色、终端、二次验证、资料与会话 |
| `niuma/companion-games.md` | NiuMa：游戏资格、审核与公开快照、报价、个人标签 |
| `niuma/orders-points.md` | NiuMa：需求与点单转换、积分、占用、履约、退款与支付 |
| `niuma/media-messaging.md` | NiuMa：媒体用途、会话授权、消息身份、通知与 TRTC |

原 `account-security.md` 并入账号权限；`database-release.md`、`error-codes.md` 并入通用数据库规则和 NiuMa 架构；`profile-tags.md` 并入游戏与资料。原 `historical-findings.md` 的平台专属经验和“人工确认”描述删除，新增基于具体可靠性机制提炼的通用规则，不声称这些规则是人工裁决结论。

## 核对基线与优先级

NiuMa 本地基线为提交 `bab3e98d7e93c48a79796c83500746229a49b13e`。OpenReviewer 的审查标准参考本仓库 `services/model_review.py` 中 `StructuredReviewPromptBuilder` 的公开项目提示词：只报告本次改动触发的实际问题、给出可定位证据、遵守可见上下文边界、允许没有可靠发现，并按安全、规范、逻辑职责检查。没有把平台输出 Schema、分片行号协议、请求限额等内部实现细节塞入通用知识。

较新的代码用于确认实际入口、数据库约束和状态；它不是业务正确性的自动证明。明确业务决定、当前契约、代码和测试共同核对。无法解决的冲突不编成确定规则，日期新、标题含“当前代码版”也不能替代核实。

### 飞书已读取资料与取舍

| 资料 | 核对结果 | 保留与排除 |
| --- | --- | --- |
| [6.1 配置说明](https://ycn0wlnxzewg.feishu.cn/wiki/BjAzwwVywiStq1kyt3Xc18LpnJc) | 含环境连接与暂未接入能力 | 只提炼凭据隔离、能力不可用应明确失败；不复制配置、密钥或当时的开关状态 |
| [6.2 Git 协作说明](https://ycn0wlnxzewg.feishu.cn/wiki/UFuEwYDdDi94qqkouaGcWOLcnGf) | 团队分支与操作说明 | 阅读以遵守协作规则，全文不作为代码审查知识导入 |
| [6.7 Flyway 协作手册](https://ycn0wlnxzewg.feishu.cn/wiki/DnkZwdif2iSoZhkH6oZcvtaGnLd) | 仍有初始单脚本、业务模型未定和 CI 待接入描述 | 保留已执行迁移不可变、旧数据升级、正式与演示数据隔离；排除现状、成员编号清单、命令教程 |
| [6.3 用户端接口](https://ycn0wlnxzewg.feishu.cn/wiki/TrHGwyfQYiFbxrkk8Qrc4ZNinLd) | 权限与接口方向可用；响应含 `code=0`、`TEST`、泛化示例 | 核对资源归属、幂等、积分、退款和语音授权；不复制示例、接口数量与已变化路由 |
| [6.4 客服端接口](https://ycn0wlnxzewg.feishu.cn/wiki/G3D3wrFWwijrkfkzgFEcLArpnpf) | 需结合服务实现核对可见范围和跨域动作 | 保留工单归属、授权与资金审批边界；不把方法存在当作外部能力已经接通 |
| [6.5 打手端接口](https://ycn0wlnxzewg.feishu.cn/wiki/EXu3wbQv2ij1YZkWaNKc4L4Wnnc) | 仍列出旧认证及自助服务能力入口 | 依据当前 Service、DTO、权限和回归核对，排除恢复旧自助授予资格或改价的要求 |
| [6.6 管理端接口](https://ycn0wlnxzewg.feishu.cn/wiki/BH0fwTif5i0J4Hkx9W3cCQcGn5g) | 清单尚未覆盖当前全部账号、游戏和目录管理 | 只作审核边界背景；最新管理员权限以当前实现核对 |
| [9.11 会议纪要](https://ycn0wlnxzewg.feishu.cn/wiki/D58VwTqboi7FO3kuM5IcbceLned) | 可读取文字明确游戏考核认证转管理端，其余文字不完整 | 与最新游戏资格、报价实现交叉确认；未完整提供的内容不据此补造规则 |
| [7.27 最新项目业务参考文档](https://ycn0wlnxzewg.feishu.cn/wiki/E46hwzzIaiFTUHkcjDCcIEAuncd) | 本次 MCP 只返回标题与空正文 | 不作为有内容的来源，不用名称假装已经有充分业务依据 |

### 本地代码和测试证据

以下路径均相对 NiuMa 仓库。对应固定版本可从 [NiuMa 核对基线](https://github.com/lboverfys/NiuMa/tree/bab3e98d7e93c48a79796c83500746229a49b13e) 查阅；模型知识正文已经包含提炼后的规则，不依赖访问这些链接。

| 主题 | 核对材料 |
| --- | --- |
| 模块与错误码 | `docs/architecture/module-boundaries.md`、`docs/architecture/error-code-conventions.md`、`niuma-bootstrap/src/test/java/com/niuma/bootstrap/architecture/MapperXmlArchitectureTest.java` |
| 单一角色 | `niuma-bootstrap/src/main/resources/db/migration/V20260907012308_01__enforce_single_account_role.sql` 的 `UNIQUE(user_id)`；当前账号管理与鉴权逻辑 |
| 账号安全 | `niuma-business/src/main/java/com/niuma/business/identity/service/impl/` 下 `AdminAccountServiceImpl`、`IdentitySecurityServiceImpl`、`AccountProfileServiceImpl` 及对应测试；并发末位管理员、锁后撤权、密码字节数、凭据消费、会话清理失败路径 |
| 游戏与价格 | `docs/development/companion-game-admission-20260913.md`、`CompanionGameServiceImpl.java`、`CompanionGameMapper.xml`、`CompanionGameServiceImplTest.java`、`CompanionGameDatabaseTest.java`；资格权限、草稿不公开、版本冲突和分游戏卡片 |
| 个人标签 | `ProfileTagDeletionWorkflowServiceImpl.java`、`mapper/catalog/ProfileTagMapper.xml`、对应 Workflow 与数据库测试；共享/独占事务锁、批量清理、版本检查与回滚 |
| 点单与积分 | `OrderRequestPaymentWorkflowServiceImpl.java` 及对应测试；创建订单、支付、绑定结果在同一事务，拒绝不匹配的支付事实 |
| 履约与退款 | `OrderCompletionServiceImpl.java`、`RefundServiceImpl.java`、`AfterSalesServiceImpl.java`、对应测试与资金 Mapper；按参与人释放资源、退款幂等、份额与整单状态区别 |
| 回调与实时通信 | `RechargeCallbackServiceImplTest.java`、`RechargeCallbackEventMapper.xml`、`UserNotificationWebSocketHandlerTest.java`；验签、同事件不同内容、未配置拒绝、批量投递和设备失效 |

本地开发说明只用来定位规则和证据，没有再整段复制。教程、按钮布局、测试结果、接口总数、数据条数、临时目录和某次部署状态全部移出检索正文。

## 更新数据库中的旧文档

已有知识库以数据库为事实来源。替换镜像中的 Markdown 不会自动覆盖它，页面“补充项目资料”也只补缺少的项目文档，不能用来更新或清理这次旧内容。

`knowledge/curation-pack.json` 只登记本轮 10 个目标来源、核对过的旧内容指纹与范围，不参与 Markdown 检索。指纹同时覆盖旧仓库文件和已核对的线上版本；正文或仓库范围被另行修改时，批量更新整包拒绝写入。无关文档、停用状态和已有归档不自动恢复。

在已授权的目标环境中，使用已有 Python 环境运行：

```text
python -m apps.maintenance.sync_knowledge_pack
```

默认只读，输出当前知识库版本，以及新增、更新、归档、保持和冲突的来源列表。确认具体预览后，使用其版本显式应用：

```text
python -m apps.maintenance.sync_knowledge_pack --apply --expected-revision <预览版本>
```

对当前 14 份旧内置文档，预期新增 1 份、更新 9 份、归档 5 份，得到 10 份启用规则。旧内容留在历史版本，归档文档不再参与新检索；命令重复应用不重复升版。新库直接使用 10 份新 Markdown 初始化。

更新使用 `knowledge_library` 单行版本锁；按 `knowledge_documents.source` 唯一索引和 `knowledge_document_versions(document_id, version)` 唯一索引批量联查。文档上限 128 份、启用正文上限 5 MiB，查询次数 O(1)，无循环数据库或外部请求。只读预览两条 SELECT，写入包含容量聚合与批量新增/更新，在单一短事务中完成。

历史审查与固定方案继续保留原知识快照；本轮内容更新不改写旧结果，也不自动恢复或重跑 PR。需要使用新规则时应新建对应方案或按平台的新任务流程准备，不把保存文档误说成已完成效果评测。

## 本轮验证与交付状态

- 89 项知识、工作流、Worker、知识管理接口和方案隔离回归通过；Ruff、Mypy 与 Git 差异空白检查通过。
- 旧版 14 份文件在本地隔离数据库中演练：新增 1 份、更新 9 份、归档 5 份，保留 15 份文档记录，其中启用 10 份。人工改文、范围变化、过时预览和重复执行均有回归覆盖。
- 10 份最终正文共 31,601 字节；本地新检索实现产生 49 个分片，每段正文均不超过现有 360 字符摘要限制，无外部链接、乱码替代字符和单独的仓库范围分片。
- 手动主题查询与实际 Java 文件名查询分别核对；回归确认通用职责词不会再把标签、游戏资格、退款规则全部挤出审查引用。
- 验证使用既有 `D:\rubbish\zhongjian\envs\openreviewer\dev\Scripts\python.exe`，没有安装依赖或构建前端。可控测试数据库、缓存与报告在 `D:\rubbish\zhongjian\temp\openreviewer-knowledge-20260914`，约 86.70 MiB；任务产物未写入 C 盘开发目录。
- 2026-09-14 经用户确认已执行知识同步：线上知识库版本从 4 升到 5，更新 9 份、新增 1 份、归档 5 份，10 份启用文档与本地正文指纹完全一致。旧版本保留，API、Worker、Web 服务正常。
- 同步用已验证的维护程序在现有容器临时目录执行，临时文件已清理；没有重启服务、修改审查任务或飞书原文，也没有发起模型调用。程序版本仍为 `691fc6e`，本地检索逻辑和源码改动尚未提交、推送、部署。
- Chrome DevTools 因已有浏览器实例占用而不可用，本次以网站使用的后台知识服务读取结果和数据库正文指纹验证同步，没有声称完成浏览器视觉验收。
- 原有未跟踪文件 `tests/performance/workspace_preview.py` 保留，NiuMa 源码未修改。
