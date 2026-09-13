# NiuMa 个人标签管理与并发删除

适用仓库：lboverfys/NiuMa
整理日期：2026-09-13

## 依据与适用边界

- 本地资料：`niuma-server/docs/development/profile-tags-20260908.md`，提交 `bab3e98d7e93c48a79796c83500746229a49b13e`。
- 原文：[查看固定提交资料](https://github.com/lboverfys/NiuMa/blob/bab3e98d7e93c48a79796c83500746229a49b13e/docs/development/profile-tags-20260908.md)。

本资料用于检查业务与代码是否一致。以目标提交对应的实际接口和较新的明确约定为准，不能仅凭旧文档的示例认定缺陷。

依据飞书「详细修改界面及功能 / 标签」：管理员配置，打手在个人资料中选择，最多三条，无需审核。

## 已实现

- 管理端「个人标签」提供分页查询、新增、启停、排序与删除。名称不可修改；启停通过右侧按钮立即提交，排序和删除使用独立弹窗。
- 管理员确认删除后，字典和全部打手当前资料中的该标签在同一事务移除，资料版本同步递增。历史审核和公开版本快照保留当时内容，恢复草稿不会带回已删除标签。
- 打手「资料与服务 / 基础资料」中的标签独立保存。零至三条，空数组清空，前后空格去除，重复值按第一次出现顺序去重。
- 新选择必须来自启用字典。本人原来已选的停用或历史标签允许保留；保存移除后不能重新选择。
- 打手本人保存标签时，变动与公开版本、审计、幂等回执处于同一事务。相同选择不新增公开版本；相同幂等键和请求直接回放原结果。
- 昵称、简介继续走草稿审核。审核写入和公开版本快照都保留当前标签；恢复历史草稿也使用当前标签。
- 旧草稿接口的 `tags` 字段保留兼容读取，提交与原草稿不同的标签时明确拒绝并提示使用新入口。
- 用户、客服、管理端公开资料沿用现有 `tags` 返回值。作品标签和订单评价标签不受影响。

## 接口

| 方法和路径 | 请求 | 权限 |
| --- | --- | --- |
| GET `/api/admin/catalog/profile-tags` | `enabled?`、`pageNum?`、`pageSize?`，最大每页 100 | `admin:catalog-profile-tag:read` |
| POST `/api/admin/catalog/profile-tags` | `tagName`（1–32 字）、`sortOrder`（非负整数） | `admin:catalog-profile-tag:manage` |
| PATCH `/api/admin/catalog/profile-tags/{tagId}` | `enabled`、`sortOrder`、`expectedVersion` | `admin:catalog-profile-tag:manage` |
| DELETE `/api/admin/catalog/profile-tags/{tagId}` | JSON `expectedVersion`；返回已移除标签的打手数量 | `admin:catalog-profile-tag:manage` |
| PATCH `/api/companion/profile/tags` | `tags`、`expectedVersion`，请求头 `Idempotency-Key` 必填 | `companion:profile:write` |

管理端同时要求管理终端和 `portal:admin:access`，打手端要求打手终端和 `portal:companion:access`。
本人档案从登录态取得，不接收前端提供的打手 ID。管理端返回完整标签、版本和时间；打手端返回 `tags/version/publicVersion/updatedAt`。

现有 GET `/api/companion/profile` 的 `tagOptions` 改为数据库提供的启用选项，稳定排序，最多返回 1000 项；当前初始化 8 项。字典按小于千条设计。

## 数据与性能

- 新表 `catalog_profile_tag` 保存字典；名称唯一索引、启用加排序索引、全部排序索引分别服务精确校验与分页。
- 复用 `companion_profile.tags`、`companion_profile_version`、`companion_audit_event`、`companion_command_receipt`。
- 更新本人资料使用主键及预期版本，并复用本人档案行锁。校验新增标签最多一条 `IN` 查询和共享行锁，避免校验后被并发停用。
- 档案、有效历史版本、幂等回执都命中现有主键或对应索引；随标签数量增长的查询次数为 O(1)。所有循环只处理内存数据。
- 管理列表复用 `PageQuery/PageResult`，统计加分页两次查询。未引入 Redis 缓存或后台刷新任务。
- 管理员删除通过 `ProfileTagDeletionWorkflowService` 编排目录和打手域，各 Mapper 只写本域表。删除与选择共用事务级读写锁，避免刚删除又被并发保存。清理使用 `tags @> ARRAY[...]` 和新增 GIN 索引，一条 UPDATE 批量完成，不逐人查询、不把全部档案加载进应用内存；查询次数 O(1)，实际更新工作量随受影响人数增长。

## 2026-09-09 操作及界面修正

- 状态列显示“已启用 / 已停用”；行右侧提供“排序、启用/停用、删除”。请求失败保留原状态并显示原因。
- 新增入口置于右上角，表格上方采用状态分段筛选；表头、按钮、圆角、间距及蓝色强调色参考现有“账号与人员”页面。
- 新增、排序、删除共用原生模态窗口，支持键盘关闭、焦点恢复、处理中禁止重复提交；亮暗主题均使用明确的文字与背景色。
- 手机宽度使用卡片式行布局，操作按钮完整显示，删除最后一页的最后一条后自动回到上一页。
- 新增迁移 `V20260909010021_01__index_companion_personal_tags.sql`，仅为当前资料标签增加 GIN 索引；不修改已发布迁移，不自动删除现有标签。
- 浏览器通过 chrome-devtools MCP 在本机模拟数据上检查新增、启停、删除、桌面与 390 像素手机布局，界面没有横向溢出。本轮没有在共享库创建或删除测试标签。
- 本轮针对性后端测试和架构检查通过，前端 16 项测试、类型检查及生产构建通过；真实数据库删除、事务回滚和并发锁测试交由 PR CI 复验。发布前只读核对共享库：17 份打手档案、9 条字典标签。
- 本轮临时文件位于 `D:\rubbish\zhongjian\temp\niuma-tag-actions-20260909`（约 3.9 MiB），前端产物位于 `D:\rubbish\zhongjian\artifacts\niuma-tag-actions-20260909`（约 2.8 MiB），未安装新依赖。

## 迁移与发布边界

新增迁移 `V20260908233639_01__create_catalog_profile_tag.sql`，创建字典、初始化现有八条选项、登记管理员权限，并增加公开档案最多三条标签的数据库约束。

该约束会一次性校验现有 `companion_profile`。如果旧资料已经超过三条，迁移会失败并保留原数据，不自动截断。2026-09-09 提交前通过 SSH MCP 在只读事务中检查共享库：17 份打手档案，超过三条标签的记录为 0；当前最近的正式迁移为 `V20260907012309_01__index_companion_profile_statistics.sql`，状态成功。未执行迁移或修改共享数据。

迁移随代码提交，由 PR CI 的 PostgreSQL 空库验证后，通过 master 既有流水线发布；不手工迁移或启动 local Profile。前后端应一起发布；旧版本审核代码仍会写入标签，不能长期与新版本并行运行。
