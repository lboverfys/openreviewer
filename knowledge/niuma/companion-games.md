# NiuMa 游戏入驻、审核与管理员报价

适用仓库：lboverfys/NiuMa
整理日期：2026-09-13

## 依据与适用边界

- 本地资料：`niuma-server/docs/development/companion-game-admission-20260913.md`，提交 `bab3e98d7e93c48a79796c83500746229a49b13e`。
- 原文：[查看固定提交资料](https://github.com/lboverfys/NiuMa/blob/bab3e98d7e93c48a79796c83500746229a49b13e/docs/development/companion-game-admission-20260913.md)。
- 飞书：[9.11 会议纪要](https://ycn0wlnxzewg.feishu.cn/wiki/D58VwTqboi7FO3kuM5IcbceLned)，明确游戏考核与认证由管理端维护；该文件描述开发分支，不代表已经部署。

本资料用于检查业务与代码是否一致。以目标提交对应的实际接口和较新的明确约定为准，不能仅凭旧文档的示例认定缺陷。

本次实现依据飞书「09-开会纪要 / 9.11会议纪要」正文、两张白板，以及负责人确认的“服务项目和报价由管理员维护”。本文描述开发分支中的实现；新增迁移尚未在共享环境执行。

## 资料与权限

| 内容 | 维护方 | 发布方式 |
| --- | --- | --- |
| 邮箱、密码、账号头像 | 本人，沿用账号设置 | 沿用现有账号与媒体规则 |
| 公开昵称、基础简介 | 打手填写，管理员审核 | 复用现有基础资料草稿与审核 |
| 个人标签 | 打手从管理员字典选择，最多三条 | 保留已确认的即时保存规则，不改变基础审核状态 |
| 游戏资格、认证段位 | 管理员 | 人工核验身份和游戏考核通过后授予，可停用 |
| 游戏介绍、区服、位置、模式 | 打手逐游戏填写，管理员审核 | 草稿与已公开内容分开，逐游戏提交和发布 |
| 服务项目、整数积分单价、服务说明、启停 | 管理员 | 按版本保存立即生效，追加报价历史 |
| 公共展示作品 | 打手提交，管理员审核 | 复用现有作品流程，本次不复制图片或录音 |

继续使用现有全局管理端权限，不增加“每个游戏一套后台”。新增 `admin:companion:manage`，授予现有 admin 角色；读取沿用 `admin:companion-review:read`。打手不能修改游戏资格、段位或报价。

身份证信息采用管理员人工核验：系统只记录核验结论、管理员和操作时间，不新增身份证号、证件照片存储，也不把人工确认描述为第三方实名接口成功。

## 入驻与发布

1. 管理员完成人工身份核验和首个游戏的考核，生成带游戏的一次性注册链接。
2. 新邀请必须传 `assessmentConfirmed=true`；未记录该确认的旧打手邀请需要重新生成，客服邀请规则继续沿用。
3. 打手通过邮箱验证码注册，注册事务一起保存账号、最小档案、首个游戏资格、两类认证结果和审计。
4. 首次进入打手端引导到原有「资料与服务」页面。基础资料和至少一个游戏资料通过审核前，不能进入接单功能。
5. 管理员在「打手审核」处理基础资料，在「打手管理 → 详情 → 游戏资料与服务」处理游戏资料和报价。
6. 第二个游戏由管理员给现有账号增加资格。基础资料共用，每个游戏单独填写、审核和展示。

游戏资料状态：`DRAFT → PENDING → APPROVED / REJECTED`。驳回必须填写原因。修改已发布资料时保留旧公开内容，重新保存使旧审核版本失效。一个游戏未填写或未通过首次审核，不影响另一个已发布游戏。

“资料已通过”“可以在大厅展示”“当前可以立即接单”分别由实际资料、启用服务和现有在线/档期规则决定。至少需要基础资料通过、该游戏已发布、资格有效、账号有效，才能进入对应新单流程。管理员可提前配置服务报价，报价启用本身不会绕过资料审核。

停用游戏后不再展示该游戏卡片，也不能接对应新单。已有订单、已接受的履约、退款和积分流水保留；原有同一打手的档期约束继续共用，不为不同游戏复制账号、钱包或排班。

## 大厅与界面

- 同一人两个游戏均已发布时，在“全部游戏”中显示两张卡片；只通过一个就显示一张。
- 卡片返回 `gameId` 和 `cardId`，`cardId` 由打手 ID 与游戏 ID 组成；分页总数统计卡片，排序最后使用打手 ID 和游戏 ID 保持稳定。
- 卡片起价只来自本游戏的服务。点击卡片携带游戏参数，详情只返回当前游戏的已发布服务和认证。
- 原有大厅样式、全部游戏及游戏筛选保留，不新增独立“切换游戏”弹窗。
- 打手端原“认证”“服务定价”页签改为“游戏资料与服务”，复用现有面板、按钮、下拉框、主题变量和响应式样式。
- 管理端功能嵌入现有打手详情弹窗，继承原管理页亮暗主题。

## 接口

管理端前缀：`/api/admin/companions/{companionId}/games`。

| 方法与后缀 | 作用 | 权限 |
| --- | --- | --- |
| GET 空后缀 | 基础审核状态及全部游戏资料 | admin:companion-review:read |
| PUT `/{gameId}/qualification` | 授予、调整、停用游戏资格 | admin:companion:manage |
| POST `/{gameId}/review` | 审核游戏资料 | admin:companion:manage |
| GET `/{gameId}/service-options` | 读取现有游戏目录的可选服务项目 | admin:companion-review:read |
| GET `/{gameId}/services` | 读取游戏下全部报价 | admin:companion-review:read |
| PUT `/{gameId}/services` | 新增或修改个人服务报价、启停 | admin:companion:manage |

打手端前缀：`/api/companion/game-profiles`。

| 方法与后缀 | 作用 | 权限 |
| --- | --- | --- |
| GET 空后缀 | 本人入驻状态及游戏资料 | companion:profile:read |
| PUT `/{gameId}/draft` | 保存本人游戏草稿 | companion:profile:write |
| POST `/{gameId}/submit` | 提交本人游戏草稿审核 | companion:profile:write |
| GET `/{gameId}/services` | 只读管理员报价 | companion:service:read |

用户端新增 `GET /api/user/companion-profiles/{companionId}/games/{gameId}`，返回 `gameId`、`introduction` 和限定到该游戏的 `profile`。

主要请求：

| 请求 | 字段 |
| --- | --- |
| 游戏资格 | `enabled`、`identityVerified`、`rankId?`、`reason`、`expectedVersion` |
| 游戏草稿 | `introduction`、`regionId?`、`positionIds`、`modeIds`、`expectedVersion` |
| 草稿提交 | `expectedVersion` |
| 审核 | `approved`、`reason?`（驳回必填）、`expectedVersion` |
| 服务报价 | `serviceItemId`、`pricePoints`、`enabled`、`description?`、`expectedVersion` |

游戏授权和服务报价的新建使用 `expectedVersion=-1`，修改使用读取到的实际版本；其他写操作使用当前版本。版本冲突返回 409，前端提示刷新核对，不自动覆盖。积分单价为正整数；`CATALOG` 计价沿用目录单位和已有积分结算，不建立人民币换算规则。

以下打手写接口退役，不再映射到业务方法：`POST /certifications`、`POST /certifications/{id}/materials`、`POST /services`、`PATCH /services/{id}`、`POST /services/{id}/submit`、`POST /services/{id}/deactivate`。对应打手角色写权限撤销。历史认证和服务查询继续只读保留。

## 数据与并发

新增迁移 `V20260913182520_01__manage_companion_game_admission.sql`，增加基础资料审核标记、游戏资料草稿/公开值/审核记录、邀请人工确认字段及管理权限。不修改任何已发布脚本；已有游戏公开资料保持兼容，新注册显式从待完善状态开始。

主要表为 `companion_profile`、`companion_game_skill`、`companion_certification`、`companion_service`、`companion_service_version`、`companion_audit_event`；邀请复用 `identity_registration_invitation`。目录只读 `catalog_game`、`catalog_filter_option`、`catalog_service_item`；跨域只读已在 Mapper 架构测试登记。

当前游戏目录为六款，每名打手的游戏和服务查询上限一百条。按打手和游戏的唯一索引定位资料；一次 IN 查询验证所有区服/位置/模式选项。大厅沿用分页和批量技能查询，查询次数为 O(1)，不会按卡片逐条查库。新增部分索引 `idx_companion_game_skill_published_game`，复用 `uk_companion_game_skill_companion_game` 和服务唯一索引。

资格、游戏草稿、审核和报价统一先锁打手主档案，再按版本更新；审核、报价快照和审计与操作处于同一事务，不包含 HTTP/RPC 或文件上传。需求匹配、客服候选、卡片下单、点单请求、需求转单和接受订单均补充当前游戏资料检查。已有订单保存的历史资料与价格快照继续使用原值。
