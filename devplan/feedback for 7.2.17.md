# Feedback for 7.2.17

## 本次范围

- Web：继续支持直接输入 Company Config/SCR MR Review。
- Issue Review History：每个 Issue 的报告以 Jira 当前 Sprint/Cycle 身份落库，不依赖先运行 Sprint Review 才能取得 Sprint 名称。
- 当前交付报告缺少明确 Sprint/Cycle 时，只允许唯一 Live Cycle 自动匹配；无法唯一匹配则拒绝注册，禁止回退到 Legacy。
- Sprint Review、Run Review、Report Review、Issue Review History 共用 `Jira Issue × Sprint/Cycle × Application + Release Line` 统计边界。

## 3.78 数据治理

- 版本从 `7.2.16+b202607241318` 升级到 `7.2.17`。
- 本周生成的报告、Finding、Handling 与 Pass 记录必须保留，不得划入 Legacy。
- 属于 `e-Channel Sprint 1.4.78` 的本周数据迁入对应 Live Cycle。
- 不属于当前 Sprint 的本周数据进入 `Ad hoc Review · 2026-W30` 历史 Cycle，不伪装为 Sprint 1.4.78。
- Scope 与当前一致的 Pass 保留；当前 Scope 已增加 MR/应用时保留历史处理证据，但 Pass 回到 Pending，等待新增 Scope 报告。
- 删除错误 Legacy Cycle 与空壳 Cycle 后，要求 `review_cycles.sprint_id=legacy`、所有 `backfilled=1` 业务记录均为 0。
- 配置、Web 用户、密码、角色、Responsible scope、报告文件与本周处理数据不在清理范围。

## 验收门槛

- 本机完整自动化回归：308 项通过，2 项跳过。
- 生产 RHEL9 staging 完整回归：308/308 通过。
- 部署前已完整备份 Workflow DB、报告、账户、配置和 systemd。
- 部署后：外部与服务器本机均返回 `7.2.17 / healthy`；SQLite `integrity_check=ok`、外键检查为 0、12 个账户指纹不变。
- 本周数据：16 个 Run、35 个 Finding、26 个 Handling、5 个 Pass 和 16 个报告路径全部保留；Legacy/Backfilled 业务记录为 0。
- 发布备份：`/var/backups/codereviewer/7.2.16+b202607241318-to-7.2.17-20260724-173933/system-backup.tgz`。
- 清理前数据库备份：`/var/backups/codereviewer/legacy-cleanup-7.2.17-20260724-174132/codereviewer-pre-cleanup.db`。

## 3.78 User Scope 数据完整性修复

- 根因：旧账户缺少持久化 `responsible_scopes`；User Management 可以根据用户名和仓库配置推断展示 Scope，但运行时 Projects/Reports 权限读取到空集合。
- `kevin.tan` 已显式保存 `DPS → kevin.tan`，29 个 DPS 项目和 ECHNL-5750/5791 的 4 份既有报告通过生产权限函数验证可见。
- 全账户审计后，将另外 9 个仍依赖推断的非 Manager 账户按原有效 Scope 原样持久化；`responsible_scope_migration_required` 剩余数量为 0。
- 修复过程未修改密码、角色、Active 状态或账户数量；变更通过 User Management 审计记录落盘。
- Kevin 修复备份：`/var/backups/codereviewer/user-scope-fix-kevin-20260724-182238/`。
- 全账户迁移备份：`/var/backups/codereviewer/user-scope-migration-20260724-182636/`。
