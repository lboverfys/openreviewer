# NiuMa 模块边界与分层

适用仓库：lboverfys/NiuMa
整理日期：2026-09-13

## 依据与适用边界

- 本地资料：`niuma-server/docs/architecture/module-boundaries.md`，提交 `bab3e98d7e93c48a79796c83500746229a49b13e`。
- 原文：[查看固定提交资料](https://github.com/lboverfys/NiuMa/blob/bab3e98d7e93c48a79796c83500746229a49b13e/docs/architecture/module-boundaries.md)。

本资料用于检查业务与代码是否一致。以目标提交对应的实际接口和较新的明确约定为准，不能仅凭旧文档的示例认定缺陷。

本文用于确定代码落位、依赖方向和评审规则。具体类名与可用性见 [业务类目录](business-class-catalog.md)，实现步骤见 [新增功能开发指南](../development/new-feature-guide.md)。

## 1. 四个 Maven 模块

```text
niuma-server
├── niuma-common
├── niuma-framework
├── niuma-business
└── niuma-bootstrap
```

| 模块 | 负责 | 禁止 |
| --- | --- | --- |
| `niuma-common` | `Result`、错误码、分页、时间、TraceId、当前主体和 ID 等稳定通用类型 | Spring Web、MyBatis、Redis、第三方 SDK 和具体业务规则 |
| `niuma-framework` | Web、安全、MyBatis、Redis、对象存储、日志、异步、ID 配置等技术能力 | 业务 Controller、业务状态、价格规则和业务表 Mapper |
| `niuma-business` | 按业务事实组织的纵向 MVC、客户端交付入口和 `shared.workflow` 编排 | 数据源客户端配置、Redis 客户端配置和启动装配 |
| `niuma-bootstrap` | 启动类、Profile、Flyway 迁移、可执行 JAR 和最终装配 | 业务判断、业务 Mapper 和渠道规则 |

内部依赖采用单向 DAG：`framework -> common`、`business -> common + framework`、
`bootstrap -> common + framework + business`。任何底层模块都不得反向依赖上层。
只有 `bootstrap` 生成可执行 Spring Boot JAR。

模块内部同样限制一级目录数量：`common` 只使用 `api`、`context`、`support`；
`framework` 只使用 `config`、`data`、`support`、`web`。`data` 下按 database、cache、
storage 分类，`web` 下承载 security 与 realtime，禁止再为单个实现建立一级包。

## 2. 业务事实域登记

业务代码统一位于 `niuma-business/src/main/java/com/niuma/business`。

| 域 | 包名 | 核心事实 |
| --- | --- | --- |
| 身份与权限 | `identity` | 账号、认证、角色、员工身份、组织关系、终端类型和数据范围 |
| 服务目录 | `catalog` | 游戏、区服、段位、位置、服务项目、计价单位、套餐和上下架 |
| 陪玩师 | `companion` | 入驻、资质、技能、展示资料、可服务状态和个人报价 |
| 需求与匹配 | `demand` | 自主匹配的需求单、需求房、接单大厅/客服派单卡片进入需求房、候选、试音、选人与成交前实际总价确认 |
| 正式订单 | `order` | 卡片指定订单、需求成交转单、交易订单、价格快照、成交参与人、订单创建后的接受/分配/异常转派和履约记录 |
| 客服与售后 | `support` | 客服工作台、人工介入、投诉、申诉、售后和处罚处理 |
| 资金 | `finance` | 支付、充值、钱包、流水、冻结、结算、提现和退款入账 |
| 退款 | `refund` | 售后决定对应的退款单据、审批进度、入账关联和失败记录 |
| 评价 | `review` | 已完成订单的逐陪玩评价、媒体关联、审核与发布状态 |
| 内容 | `content` | 媒体与文件元数据、审核任务、公告正文等可复用内容 |
| 沟通 | `communication` | 与需求房、订单房关联的沟通空间、成员权限、消息、语音会话、录音关联和访问记录 |
| 通知 | `notification` | 逐接收人的站内通知、已读状态、业务提醒、投递记录和失败结果 |
| 运营 | `operation` | 运营配置、规则版本、审批配置、审计查询和经营视图 |
| 跨域流程 | `shared.workflow` | 跨域用例的步骤、顺序、补偿和结果编排，不是业务一级事实域 |

上述事实域名称同时作为 `com.niuma.business` 下的 Java 一级包和事实边界；`shared.workflow` 只作为同级的跨域编排包存在，不创建同名业务一级包。客户端入口直接放在真正拥有事实的域的 `controller/<client>` 下，不再设置一个没有持久化事实的 `portal` 业务包。[业务类目录](business-class-catalog.md) 中的 Controller、Service、ServiceImpl 和 Mapper 结构锚点用于固定开发路径，但不等于已经创建表或实现行为。业务规则没有确定前保留零假行为骨架，不写假接口、假 CRUD 或假数据。

## 3. 按业务域组织的 MVC 骨架

业务 Java 先按事实域分组，再在域内使用必要的 MVC 目录：

```text
com.niuma
├── business
│   └── <domain>
│       ├── controller/<client>
│       ├── service              # Service 接口
│       │   └── impl             # Service 实现
│       ├── mapper
│       └── model
│           ├── dto
│           ├── vo
│           ├── entity
│           └── projection
└── shared
    ├── component/<domain>
    ├── error
    ├── port
    └── workflow                # 跨域用例接口
        └── impl                # 跨域用例实现和适配器
```

`<domain>` 使用上节登记的 `identity`、`catalog`、`demand`、`order`、`support` 等名称。
每个域只创建实际有类的目录，不要求凑齐整套空 MVC 骨架。

复杂 SQL 放在：

```text
niuma-business/src/main/resources/mapper/<domain>/
```

约定如下：

- Controller 只负责协议转换、参数校验、鉴权、调用 Service 和返回统一响应，位于所属域 `controller`。
- Service 接口位于所属域 `service`，实现位于 `service.impl`，命名为 `XxxServiceImpl` 并标注 `@Service`；事务只位于真实 Service 实现。
- Mapper 位于所属域 `mapper`；模型位于所属域 `model.dto`、`model.vo`、`model.entity`、`model.projection`，禁止建立域顶层 `dto`、`vo`、`entity` 或 `mapper.model`。
- 校验器、协调器、处理器、注册表和 WebSocket 处理器等非 MVC 组件统一位于 `shared.component.<domain>`；跨域或外部能力契约位于 `shared.port`。
- 跨域用例接口位于 `shared.workflow`，实现和适配器位于 `shared.workflow.impl`。Workflow 不拥有 Controller、Mapper、Entity、Projection 或数据库表。
- 业务错误码全部位于 `com.niuma.shared.error`，由 `XxxErrorCode` 枚举实现公共 `ErrorCode` 契约；类型名对应错误码登记号段，编码全局唯一且为五位数字。
- 业务 Spring 配置和业务 `@ConfigurationProperties` 不属于业务 MVC，统一放在 `niuma-bootstrap/src/main/java/com/niuma/bootstrap/config`；框架自身的技术配置仍归 `niuma-framework`。业务 Service 只依赖参数接口，不反向依赖 bootstrap 配置类。
- 没有真实类型时不创建空目录、`.gitkeep`、`package-info.java`、占位类或运行日志；JVM 生成的 `replay_pid*.log`、`hs_err_pid*.log` 等文件不得进入项目目录。
- Controller 和跨域调用方只通过构造器注入 Service 接口，不依赖 ServiceImpl、Mapper、Entity、Projection 或 `shared.component`；禁止字段注入。
- Mapper 是标注 `@Mapper` 的接口，只访问本域拥有的表。表与模型确定后，基础 CRUD 才使用 MyBatis-Plus，复杂 SQL 使用 XML。
- 仅与某个 Service 或端口接口紧耦合的嵌套命令、结果和运行上下文可以就近定义；它们不属于 Web DTO/VO、Entity 或 SQL Projection，不得拆成散落在普通业务包中的独立顶层模型。
- 某类模型不存在时不创建空包，也不使用 `.gitkeep`、`package-info.java` 或占位类型维持目录外观。
- 状态转换、金额校验和策略选择等易错逻辑可以拆成 `shared.component.<domain>` 中职责清晰的规则类。
- 不建立 Facade、Command、Query、Repository、DO、Converter 等平行对象体系，除非存在可证明的隔离需求。
- 每个顶层类型和有独立语义的命名嵌套类型必须有中文类级 Javadoc，说明它负责什么、明确不负责什么；接口说明契约，实现类说明实现边界，测试类说明验证目标。注释不得只是重复类名或逐字解释注解。
- 包名必须全小写并表达一个稳定业务语义，禁止使用 `customerservice` 这类拼接包名；业务域事实边界统一维护在本文，具体类职责写入类级 Javadoc。

因此，`com.niuma.business` 一级目录只允许已登记事实域；错误码、组件、端口和 workflow
统一放在同级 `com.niuma.shared`，`config` 不在业务模块，非 MVC 类型不得散落成新的一级包。
没有真实类型的目录不创建，也不保留空壳或垃圾日志。

完整调用链是：

```text
HTTP -> XxxController -> XxxService（接口）
     -> XxxServiceImpl（实现） -> XxxMapper -> PostgreSQL
```

Spring 通过 `XxxServiceImpl` 的 `@Service` 注册实现，并按接口类型完成构造器注入。业务代码不得 `new XxxServiceImpl()`，也不得把构造器参数声明为实现类。

### 注解矩阵

| 类型 | 注解 | 说明 |
| --- | --- | --- |
| Controller | `@RestController`、`@Validated`、`@RequestMapping`、`@Tag` | 有 `final` 依赖时增加 `@RequiredArgsConstructor`；可固定类级基础路径；安全入口增加 `@SecurityRequirement`，真实 API 再增加方法级 Mapping、`@Operation` 和所需 Sa-Token 注解，数据库入口增加 `@ConditionalOnProperty(name = "niuma.infrastructure.database.enabled", havingValue = "true")` |
| Service 接口 | 无 Spring stereotype | 不添加 `@Service`，由实现类注册 Bean |
| ServiceImpl | `@Service`、`@Validated` | 有 `final` 依赖时增加 `@RequiredArgsConstructor`；依赖 Mapper 时增加相同的 `@ConditionalOnProperty`；`@Transactional` 只用于真实事务，不给空骨架制造事务语义 |
| Mapper | `@Mapper` | 模型未定时不继承 `BaseMapper`、不绑定 Entity、不写 SQL |
| Entity | `@TableName` 等按真实表增加 | 未确认表结构时不创建占位 Entity |
| DTO | Jakarta Validation、`@Schema` 按真实请求增加 | 未确认 API 时不创建占位 DTO |
| VO | `@Schema` 按真实响应增加 | 不引用 Entity、Projection 或 Mapper |
| Projection | MyBatis 映射所需的 Getter/Setter | 不添加 Web 校验或直接对外返回 |

业务实现状态以当前 Controller 方法、四端接口文档和数据库迁移为准。尚未实现的结构锚点不暴露请求方法、不声明假业务方法、不绑定占位表，也不返回演示值或抛占位异常。

## 4. Entity 与表语义

`BaseEntity` 只提供 ID 和创建时间，适合追加型记录和不需要通用修改字段的实体。

`MutableEntity` 在 `BaseEntity` 上增加更新时间、乐观锁版本和逻辑删除字段。只有确实允许修改且允许逻辑删除的普通业务实体才继承它。

以下表禁止因为方便而继承逻辑删除语义：

- 钱包和资金流水；
- 支付、退款及渠道回调事件；
- 结算和提现流水；
- 操作审计、审批轨迹和规则发布记录；
- Outbox 和可靠任务执行记录。

这些记录采用追加写或显式冲正。删除展示数据不等于删除财务或审计事实。

数据库迁移遵守以下规则：

- 已发布的 Flyway 迁移不修改，只增加新版本。
- 金额使用 `BIGINT` 分值，费率使用有明确单位的整数。
- 时间事实使用 PostgreSQL `TIMESTAMPTZ` 与 Java `Instant`，按 UTC 保存和传输，展示时再转换为业务时区。
- 业务编号、渠道事件号和幂等键由数据库唯一约束兜底。
- 候选选择、余额扣减和状态变更使用条件更新或乐观锁，不能只在 Java 中先读后写；如果产品引入竞争抢单，同样由数据库决胜。

### 表所有权登记

表所有权按业务事实而不是历史表名前缀判断。所属域 Mapper 可以读写本域表；其他域只能通过 Service/port 使用，确需组合查询时必须在 `MapperXmlArchitectureTest` 中按“业务域 + Mapper 文件 + statementId + 表 + 用途说明”登记为只读边界。登记只放行这一条 statement 的这一张表，不能顺带放行同 Mapper 的其他查询；未登记表、跨域写入以及 `SELECT ... FOR UPDATE` 对外域表的锁定都不接受豁免。测试会递归展开 `<sql>` / `<include>`，不依赖 MyBatis statement 标签识别数据修改 CTE，并拒绝重构后未再使用的过期登记。

| 所属域 | 表 |
| --- | --- |
| `identity` | `identity_user_account`、`identity_role`、`identity_permission`、`identity_user_role`、`identity_role_permission` |
| `catalog` | `catalog_game`、`catalog_filter_option`、`catalog_service_item`、`catalog_profile_tag` |
| `operation` | `operation_user_config` |
| `companion` | `companion_profile`、`companion_game_skill`、`companion_service`、`companion_availability` |
| `content` | `content_media_asset`、`content_media_object_cleanup_task`、`content_media_reference`、`content_media_review_task` |
| `communication` | `communication_conversation`、`communication_message`、`communication_read_position`、`communication_emoji_pack`、`communication_emoji_item`、`communication_emoji_command_receipt` |
| `demand` | `demand_quote`、`demand_request`、`demand_room`、`demand_room_presence`、`demand_candidate`、`demand_trial`、`demand_trial_feedback`、`demand_draft`、`demand_price_confirmation`、`demand_command_receipt` |
| `order` | `order_direct_quote`、`order_service_order`、`order_participant`、`order_companion_reservation`、`order_room`、`order_room_member`、`order_event`、`order_completion_report`、`order_completion_decision`、`order_transfer_task`、`order_transfer_activity` |
| `finance` | `finance_asset_account`、`finance_asset_transaction`、`finance_payment_attempt`、`finance_recharge_product`、`finance_recharge_order`、`finance_order_fund_hold` |
| `support` | `support_assistance_request`、`support_customer_case`、`support_after_sales_ticket`、`support_after_sales_party`、`support_after_sales_material_submission`、`support_after_sales_material`、`support_after_sales_plan`、`support_after_sales_plan_decision`、`support_case_media`、`support_agent_profile`、`support_agent_presence_event`、`support_matching_activity`、`support_customer_case_action`、`support_voice_intervention`、`support_voice_intervention_consent`、`support_refund_application`、`support_refund_application_material`、`support_violation_case`、`support_violation_action`、`support_salary_statement`、`support_commission`、`support_salary_dispute`、`support_audit_event` |
| `refund` | `finance_refund_order`（历史前缀，所有权属于退款域） |
| `review` | `content_order_review`、`content_order_review_media`（历史前缀，所有权属于评价域） |
| `notification` | `notification_user_notification`、`notification_push_subscription`、`notification_realtime_event`、`notification_outbox_event` |

`finance_refund_order` 和 `content_order_review*` 只存在命名歧义，当前没有真实依赖故障，不通过改名迁移制造兼容风险。新增表时必须同时更新本登记和 XML 架构测试；可靠任务、Outbox 等表仍归产生并维护该事实的业务域。

客服订单工作台由 `SupportPortalMapper` 组合读取订单、目录、陪玩、沟通、资金和身份摘要；异常转派由 `OrderTransferMapper` 读取工单关联、候选资格、服务报价和资金摘要。每条跨域读取都按具体 statement 登记在 `MapperXmlArchitectureTest`，只服务于只读展示或提交前复核，不允许借联查写入或锁定外域表。

### 客服门户契约与实施边界

客服门户共登记 117 条 HTTP 契约：3 条登录身份契约复用 `identity`，其余 114 条位于 `/api/support/**`。客服专属 Controller、前端请求清单和后端路由测试必须使用同一方法与路径集合；新增、删除或改名时必须同步三处，不能只让 Knife4j 展示一条没有前端调用入口的孤立接口。

客服接口的已实现含义是“路由、校验、鉴权、数据范围、持久化事实和明确响应均存在”，不代表尚未接入的外部副作用会被伪造成成功。当前边界如下：

- 工单和主动任务的正式写操作只允许当前负责人执行；列表、搜索、订单、用户、陪玩师、房间、会话、媒体和审计查询均先校验客服可见范围。团队数据范围模型尚未建立时，只开放本人已分配资源和可领取的未分配资源。
- 候选邀请和模板提醒只保存幂等事实，返回 `RECORDED` 或 `DELIVERY_PENDING`；通知域真实投递、失败重试和送达回执接入前不得返回“已发送”。
- 退款接口只创建、修改、补充材料并提交审批事实，不直接改积分或执行退款；结算预览在规则未配置时返回 `executable=false` 和阻塞原因；违规接口只保存证据与处罚建议，不直接处罚账号。
- 异常转派会校验负责人、版本、候选方案、用户同意和报价确认；订单域尚未提供参与人替换与房间权限原子命令时，最终 `commit` 明确返回不可执行错误，不修改订单归属。
- 语音介入统一使用腾讯 TRTC；缺少真实凭据或授权链时不返回伪造入房令牌、录音地址或播放地址。
- 工单聊天和证据默认在工单关闭后保留 30 天；退款审批或违规申诉仍在处理时暂停到期倒计时。导出、录音和媒体访问继续受工单归属与用途校验约束。
- 前端以 `route-contracts.ts` 固定 114 条客服路由，并由 Vitest 与后端 `support-endpoints.tsv` 逐条比对；请求和响应类型以当前 Controller DTO/VO 为准，文档示例中的兼容别名只用于输入兼容，不作为新的主字段。

## 5. 两条下单入口与正式订单

产品包含两条明确链路：

```text
自主匹配：发布需求 -> demand 需求单/需求房 -> 候选/试音/选人/确认实际总价 -> order
卡片下单：选择陪玩师 -> 选择服务项目和数量 -> 复核状态与价格 -> 确认下单 -> order
```

- 需求可以结束、撤回或匹配失败，而不产生正式订单。
- 陪玩从接单大厅或客服派单卡片进入需求房后才成为候选；进入动作不等于接单成功或获得订单。
- 自主匹配在客户选人并确认实际总价后创建正式订单。
- 卡片直接下单不创建虚假需求单，订单的来源需求关联应允许为空或使用等价的显式来源模型。
- 卡片指定订单创建不等于陪玩师接受、支付完成或履约开始；订单、接受/分配、支付和履约分别表达状态。
- 成交前客服派单卡片邀请陪玩进入需求房，属于 `demand`；订单拒绝、超时或异常后的转派任务属于 `order` 并关联原订单。
- 正式订单必须保留服务内容、价格、规则版本和参与主体快照，不能依赖后续可变资料还原历史。
- 支付/积分扣减与订单创建的准确先后仍待产品确认，不在公共枚举或假流程中写死。
- 需求、订单、履约、支付、退款、申诉和结算分别维护状态；禁止恢复一条包办所有阶段的总状态机。

## 6. 沟通与通知边界

需求房、订单房、聊天房间和客服介入在页面上可能都被简称为“房间”，但它们不是同一个业务事实。代码落位按下表判断：

| 事实 | 拥有域 | 边界 |
| --- | --- | --- |
| 需求、候选、试音、选人和需求房业务生命周期 | `demand` | 决定需求房能否进入、关闭、匹配失败或成交转单 |
| 正式订单、接受/分配、异常转派和履约生命周期 | `order` | 决定订单房关联的交易与履约是否有效 |
| 房间沟通资源 | `communication` | 保存业务房关联、成员访问权限、消息、语音会话、录音关联和访问记录，不维护需求或订单状态 |
| 客服工单与处理结论 | `support` | 决定是否介入、投诉/申诉结果和处罚处理，通过沟通域访问获授权的消息或录音 |
| 通知与投递事实 | `notification` | 保存通知内容、接收人、已读状态、投递结果和失败原因，不维护来源业务状态 |

`demand` 或 `order` 创建、关闭业务房后，如果需要同步创建、关闭沟通资源，应由 `shared.workflow` 调用各域 Service 接口进行编排。`communication` 可以根据来源标识和授权结果管理沟通资源，但不得直接修改需求、订单或客服工单的状态，也不得把自己的“房间状态”扩张成一套重复的业务状态机。

录音文件本体及媒体元数据属于 `content` 与对象存储边界；`communication` 只保存录音对象与房间、会话、成员权限之间的关联及访问记录。公告正文和可复用媒体属于 `content`，面向具体接收人生成的通知副本、已读状态和投递结果属于 `notification`，两者不能共用一张“消息表”混合建模。

订单完成、退款成功、需求关闭或客服结案后需要通知用户时，由来源用例的 workflow 或可靠任务调用 `notification`。通知域只记录“通知是否生成、是否已读、是否投递成功”；投递失败进入重试或最终失败处理，不能伪造来源业务失败，更不能由通知域反向更新订单、资金、需求或客服状态。

## 7. 跨域调用与 workflow

单域用例遵循：

```text
Controller -> 本域 Service 接口 -> 本域 ServiceImpl -> 本域 Mapper
```

跨域用例遵循：

```text
发起域 Controller -> Workflow Service 接口 -> Workflow ServiceImpl
                  -> 各事实拥有域 Service 接口 -> 各域 ServiceImpl/Mapper
```

`shared.workflow` 的硬边界：

- `shared.workflow` 根包只包含用例 Service 接口，`shared.workflow.impl` 只包含对应实现类和适配组件；
- 不定义 Controller、Entity、Projection、Mapper 或数据库表；
- 适配组件只能做各域 Service/port 的模型转换与调用连接，不声明事务、不访问 MyBatis、Redis、MinIO 或其他技术客户端；
- 不直接操作 Redis、MyBatis Mapper 或第三方 SDK；
- 只通过构造器依赖各业务域 Service 接口，不依赖任何 ServiceImpl；
- 不成为其他业务域的底层依赖；
- 需要持久化的订单、资金、审批或售后事实必须由对应域 Service 写入。

简单只读校验可以直接调用另一个域的 Service。只要出现跨域写入、外部副作用或补偿步骤，就应提升为 workflow 编排，避免域之间形成网状依赖。

## 8. 基础设施边界

### PostgreSQL

PostgreSQL 是订单、资金、候选选择和审批结果的最终事实源。数据库约束和事务负责正确性，应用异常消息只负责解释失败。

### Redis

Redis 用于缓存、限流、短期幂等、候选集合和分布式协调。Redis 丢失或锁过期不能造成重复支付、重复转单、候选人数越界或余额错误。竞争抢单不属于已实现能力；若产品将其纳入范围，也不能只依赖 Redis。

### 可靠异步

脚手架不引入消息队列。关键异步流程使用 PostgreSQL 可靠任务表或 Outbox，在业务事务中同时落库，由调度器领取、重试并记录最终结果。内存 `@Async` 只用于允许丢失的非关键工作。

### 对象存储

业务层依赖 S3 兼容的对象存储能力，不依赖 MinIO SDK 类型。MinIO 是本地实现；生产可以替换为其他兼容服务。公开展示文件与私有履约证据必须分级授权。

### 外部渠道

支付、退款、提现、通知、实名和内容审核按实际渠道分别适配。渠道回调路径、媒体类型、验签、解密和响应格式均由渠道契约决定，不使用一个宽泛的通用回调假实现。

## 9. 入口与安全

HTTP 路径按调用方分组：

- `/api/public/**`：真正无需登录的公开能力；
- `/api/mini/**`：用户和陪玩师小程序入口；
- `/api/admin/**`：客服、财务和运营后台入口；
- `/api/callback/<provider>/**`：明确的外部渠道回调。

回调不能通过一个默认的 `/api/callback/**` 白名单获得信任，必须在具体渠道适配器中完成验签、防重放和数据库幂等。后台功能权限还必须结合本人、团队、全店等数据范围。

Sa-Token 会话在 `local`/`prod` 环境通过 Redisson DAO 写入 Redis，并只从标准 Bearer 请求头读取令牌。`test` 中的内存会话只允许自动化测试隔离使用，不能作为开发运行方案。framework 暴露 `AuthorizationDataSource`，由 `identity` 提供真实角色和权限；缺少实现时默认拒绝权限操作。

业务 Mapper 扫描只在 `niuma.infrastructure.database.enabled=true` 时启用。`local`/`prod` 均开启该配置，源码中的 `@Mapper` 接口会形成运行时 Mapper Bean，依赖 Mapper 的 ServiceImpl、相应 Controller 和 Workflow ServiceImpl 使用同一开关条件进入真实持久化装配。`test` 可关闭数据库能力以隔离不需要数据库的自动化测试，但不得据此提供内存业务实现。开始提供真实持久化行为时必须补齐模型、迁移和 PostgreSQL 集成测试，不能用内存假实现、固定响应或静默成功维持表面可用。

开发启动必须显式选择 `local`。`config/application-local-private.yml` 是本机连接配置入口，由 `application-local.yml` 显式导入；真实文件不提交，只保留示例。PostgreSQL、Redis 或对象存储可以由本地 Compose 提供，也可以在该文件中改为开发或测试服务器地址。Docker Compose 的可选 `.env` 只负责容器参数，不与 Spring 私有 YAML 混用，避免两种配置语法对凭据产生不同解析结果。配置缺失、连接失败或 Flyway 校验失败必须以启动失败或 readiness 异常暴露，不能降级成一个看似健康但没有真实基础设施的应用。

Springdoc 按入口生成 OpenAPI 3.0.1，UI 统一使用 Knife4j 的 `/doc.html`。生产环境固定关闭 OpenAPI JSON 和 Knife4j 分组配置。

## 10. 架构护栏

ArchUnit 和评审至少保持以下约束：

1. Controller、Service、ServiceImpl、Mapper 必须位于约定包并使用固定后缀。
2. Controller 必须标注 `@RestController`，ServiceImpl 必须标注 `@Service`，Mapper 必须标注 `@Mapper`；Service 接口不得标注 `@Service`。
3. 每个 ServiceImpl 必须实现对应 Service 接口；Controller 只能依赖接口和 DTO/VO，不能依赖 Mapper、Entity、Projection 或 ServiceImpl。
4. 业务代码不得访问其他事实域的 Controller、Mapper、Entity、Projection 或 ServiceImpl；跨域只调用 Service 接口，并按契约使用 DTO/VO。集中错误码可以被对应业务调用。
5. 事务注解只能位于 `service.impl` 中的真实 Service 实现方法或实现类。
6. `shared.workflow` 及其 `impl` 子包不得出现 Controller、Mapper、Entity、Projection 或持久化表，也不得访问技术客户端；`workflow.impl` 中的适配组件只能转换模型并调用 Service/port，不得声明事务。
7. 禁止字段 `@Autowired`、`@Resource`；Spring 组件使用 `final` 依赖和 Lombok `@RequiredArgsConstructor` 完成构造器注入。
8. `com.niuma.business` 一级目录只能是已登记事实域；域内只保留 MVC 和模型，非 MVC 类型统一进入同级 `com.niuma.shared`。
9. `framework` 不得依赖 `business`。
10. 新增 Java 类型必须提供可帮助新人判断职责和调用方向的类级 Javadoc。
11. Lombok 只生成无业务逻辑的构造器及必要访问方法；Entity 和配置类不得使用会隐式生成 `equals`、`hashCode`、`toString` 的 `@Data`。
12. 顶层 record、按约定后缀命名的模型和带 `@TableName` 的 Entity 只能位于对应的 `model.dto`、`model.vo`、`model.entity` 或 `model.projection`；架构测试同时拒绝旧包结构和模型类别错位。
13. 业务 `ErrorCode` 实现只能位于统一的 `com.niuma.shared.error`；所有错误码必须是全局唯一的五位数字，并使用按错误码类型登记的业务号段。

业务域只有在需要独立发布、团队已经稳定分组或必须独立部署时，才考虑拆成独立 Maven 模块。

## 11. 评审清单

- 这个事实是否放在真正拥有它的业务域？
- 需求单是否被误当成正式订单？
- 卡片直接下单是否被错误地强制创建需求单？
- 是否把成交前需求房邀请和成交后订单转派错误地复用了同一状态？
- 是否把需求房/订单房的业务生命周期与 `communication` 的沟通资源混成同一套状态？
- 是否让 `notification` 根据投递结果反向修改来源业务状态？
- 是否把支付、退款、申诉或结算状态塞回订单总状态？
- Controller 是否只构造器注入 Service 接口，ServiceImpl 是否实现对应接口？
- Controller、Service、ServiceImpl、Mapper、DTO、VO、Entity 和 Projection 是否位于所属业务域？
- 业务错误码是否位于统一 `com.niuma.shared.error`，并使用已登记且全局唯一的五位编号？
- 跨域写入是否通过 Workflow Service 接口和各域 Service 接口？
- 零假行为骨架是否误加了 API、业务方法、BaseMapper、Entity、SQL、DDL 或演示响应？
- `local`/`prod` 是否装配真实 Mapper 与基础设施，`test` 替身是否严格限制在自动化测试？
- Redis 是否只是优化，数据库是否有最终约束？
- 资金、回调和审计记录是否保持追加写？
- 外部副作用是否具备持久幂等、重试和补偿入口？
- 新规则是否保存版本和业务快照，而不是只读当前配置？
- 文档、迁移、测试和代码包名是否同步更新？
- 新增或修改的类型是否有准确的中文类级 Javadoc，并说明职责、调用方向和禁止边界？
