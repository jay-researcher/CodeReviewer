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
- 生产 RHEL9 staging 完整回归必须通过。
- 部署前完整备份 Workflow DB、报告、账户、配置和 systemd。
- 部署后校验版本、健康、SQLite `integrity_check`、账户指纹、本周报告文件及业务数据计数。
