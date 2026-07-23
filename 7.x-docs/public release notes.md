# CodeReviewer Release Notes

本页面仅展示面向用户的版本摘要。详细技术变更、验证结果及部署状态请参阅内部 7.x Release Notes。

## 7.2.16 — 2026-07-23

状态：已于 2026-07-23 部署到 `192.168.3.78:8765`，生产健康检查、配置策略、账户、Workflow 历史数据及回滚入口验证通过。

本机后续验收修订（尚未同步到 `192.168.3.78:8765`）：

- 当前 Cycle 没有可审核 MR 时明确显示 `No Review Required`，不会误显示 Passed，也不会计入 Review Pass、Remaining 或待生成报告统计。
- 当前 Cycle 有 Required Scope 但没有 Run 时，会显示真实应用范围和 `Run Review`；完成审核且无阻断后才允许 `Manual Pass`。
- Issue 列表、详情和 Overview 使用同一份 Cycle 数据；切换 Sprint/Cycle 后不会再沿用上一 Cycle 的 Passed 或旧 Run 状态。
- `No Review Required` 可使用 `Check Again` 重新发现 Scope；后续出现新 MR 时会恢复为 `Awaiting Review`。

- Sprint Scan 会同步 Jira 当前 Sprint 与 MR 状态；Issue 移出 Sprint 或只剩关闭 MR 后，不再保留过期的待生成报告。
- 手动 Scan 总是获取新数据；空 Sprint/Issue 范围不会混入其他历史报告。
- Issues Review History 支持切换 Sprint 与 Delivery Cycle，Overview、Issue 列表和详情使用同一 Cycle 统计口径。
- 历史 Cycle 显示为只读；Re-scan 与 Manual Pass 只作用于当前 Live Cycle。
- Manual Pass 不再复用其他 Sprint 的旧报告，必须完成当前 Cycle 的全部应用和精确交付版本 Scope。
- 同一应用范围重新扫描后，只以最新报告计算问题数、报告数和 Pass；不会再把旧 Run 与新 Run 相加。
- GitLab 历史搜索只接纳标题或源分支明确包含 Jira Key 的 MR，Release merge 与 `Company_GIT_VERSION` 不再误入普通 Issue Review。
- 页面使用更克制的 Live、Historical、Legacy 视觉语义，帮助区分当前交付与历史证据。
- 默认视图只展示 Current cycles；需要审计时再切换 All sprints & history。Issue 标题、操作、Delivery Cycle 与 Readiness 分层排列，没有当前报告时会显示明确的 Awaiting Review 空状态。

## 7.2.15 — 2026-07-22

状态：已于 2026-07-23 部署到 `192.168.3.78:8765`，生产健康检查、配置策略、账户及历史数据完整性验证通过。

- 同一 Jira Issue 的代码与 Company Config/SCR 分属不同应用时，会分别生成应用报告；ECHNL-5757 预期显示 WVAdmin 和 DPS11 两份。
- 仅有 Company Config/SCR 的 Issue 也会生成可下载报告，不再出现“Review completed”但报告数仍为 0。
- 修复多应用报告保存阶段 `review_scope_for_merge_request` 未定义导致 ECHNL-5755/5757 失败的问题。
- DPS Codex 审核超时后会使用收敛到 42,000 字符以内的上下文重试一次，避免单次 300 秒超时直接结束整项审核。
- Codex 使用真实 JSON 流活动作为心跳：持续有 Provider 活动时不再被固定 300 秒终止，同时保留 15 分钟绝对上限防止永久挂起。
- Issues Review History 按精确交付版本区分 `7.5.1.38` 与 `7.5.1.39`；唯一明确的 Legacy Run 会安全归并，不再生成空版本假 Scope。
- Manual Review Pass 会先确认当前 Cycle 的全部应用交付版本 Scope 都已有完成报告；应用筛选后的 Issue 卡片显示当前 Scope 状态。
- Issues Review History 的完整报告证据支持 Markdown 表格，文件清单会按表头和行列展示，并在内容较宽时提供滚动浏览。
- Sprint Overview 扫描完成后会自动收起 Scan Scope，并保留本次范围与结果摘要；需要修改范围时可手动展开。
- 文件清单校验按应用隔离，不再把另一个应用范围的配置误报为当前 MR 缺失。
- Responsible 按 Jira Components 与应用规则确定；Jira Responsible 原值仅供审计查看，不参与范围推断。
- Issues Review History 区分唯一 Issue 与应用审核范围，支持 Jira 外链、完整问题证据和应用级 Reviewer 权限。

## 7.2.14 — 2026-07-22

状态：已于 2026-07-22 部署到 `192.168.3.78:8765`，生产健康检查及数据完整性验证通过。

- `kelvinh.wu` 已调整为普通 Developer，与 `benyq.feng` 一同使用 `kevin.tan` Responsible 范围。
- 修复 Sprint Overview 将已有报告统计为 `without report` 的问题。
- Overview 分别展示唯一 Issue 覆盖、应用/版本范围报告覆盖和历史生成文件数，跨版本 Issue 的发布准备度更清晰。
- `Run remaining` 可发现仅部分应用或版本范围缺少报告的 Issue。

## 7.2.13 验收反馈更新 — 2026-07-20

状态：已于 2026-07-21 同步到 `192.168.3.78:8765`，生产健康检查及数据完整性验证通过。

- Sprint Overview 支持 Manager 一键启动全部缺少报告的 Issue，并确保单卡片 Run Review 只处理该 Issue。
- Problems 同时展示“问题详情”和“处理建议”的两行摘要，可按需展开完整内容。
- Company Config/SCR 延后资源不再混入普通 MR 的 Jira 文件清单差异表，由 GIT_VERSION Release Gate 独立校验。

## 7.2.13 — 2026-07-20

状态：已部署到 `192.168.3.78:8765`，生产健康检查及数据完整性验证通过。

- 同一 Jira Issue 现在按应用及版本线生成独立报告和问题清单。
- Sprint Overview 可分别查看 iTrade Client 7.5.0/7.5.1、DPS9/DPS11 等范围的发布准备度。
- Issues Review History 汇总同一次 Review 的全部应用报告，并在 Problem 上标明所属范围。
- 多人协作的报告访问与跟进改为使用完整 Responsible Scope。
- 生产仓库分支配置统一使用版本线通配符，例如 `1.0.*`、`7.5.0.*`、`9.3.*`，后续补丁版本无需逐次修改配置。

## 7.2.12 — 2026-07-19 12:31:03 CST

状态：相关能力已随 7.2.13 整体发布到 `192.168.3.78:8765`。

- Sprint Overview 使用 Overview / Sprint issues 分区，打开时不再误带主页 Jira 输入。
- Issue Review 卡片和 Problems 摘要提高可读性，问题与建议可分别预览和展开。
- Review Communication、User Management 和全站弹窗规格完成一致性优化。
- Application Settings 中 LLM 统一按标准缩写显示。

## 7.2.11 — 2026-07-19 01:14:17 CST

状态：相关能力已随 7.2.13 整体发布到 `192.168.3.78:8765`。

- Run Review 在 Progress 中展示已有报告检测、状态校验和等待决策过程。
- Release Notes 修复 Windows/RHEL 时区数据差异导致的加载失败，并提供自动重试与恢复入口。
- User Management、Configuration、Sprint Overview、Review Communication 和报告各 Tab 完成一致性优化。
- Problems 以两行摘要展示问题与建议；延后文件明确标识来自 Company Config 或 SCR。
- 配置分支支持精确值和版本通配符；知识上下文改为 Rovo 只读检索优先，Jira REST 保持权威。

## 7.2.10 — 2026-07-19 00:40:11 CST

状态：相关能力已随 7.2.13 整体发布到 `192.168.3.78:8765`。

- Sprint Overview 改为可恢复的后台扫描，不再因浏览器等待 60 秒而丢失结果。
- 扫描过程展示真实 Jira/MR Discovery 阶段和完成百分比；超过 30 秒后显示长任务提示及超时倒计时。
- 关闭 Sprint Overview 不会停止扫描；重新打开后会恢复进度或自动显示完成结果。

## 7.2.9 — 2026-07-18 14:33:16 CST

状态：相关能力已随 7.2.13 整体发布到 `192.168.3.78:8765`。

- Manager 可以在线维护安全的应用设置和 GitLab 项目元数据，并通过版本化备份一键恢复。
- 登录页和主页增加可查看详情的健康状态；Release Notes 显示文件最后更新时间。
- User Management、Responsible scope、Review Communication 和报告阅读排版完成一轮一致性优化。
- Issues Review History 的 Overview 按 Sprint 和应用展示 Release Readiness，并隔离不同 Review Cycle 的进度。

## 7.2.8 — 2026-07-18

状态：相关能力已随 7.2.13 整体发布到 `192.168.3.78:8765`。

- Sprint Overview 可按 WVAdmin、iTrade Client、Services Terminal、DPS 查看发布准备进度和剩余 Issue。
- 每个应用清晰区分无报告、生成中、处理中、待 Pass、已 Pass 和失败状态。
- 只有应用内全部 Issue Review Pass 后才显示可进入 GIT_VERSION Release Gate；跨应用和未映射记录不会被遗漏。

## 7.2.7 — 2026-07-18

状态：相关能力已随 7.2.13 整体发布到 `192.168.3.78:8765`。

- Sprint Overview 对比有报告和无报告的 Issue 数量及占比。
- 已生成报告的 Issues 分为 Handling、Ready for Pass、Review Pass。
- Issue 卡片增加 Review Cycle、Run、Sprint 和 Snapshot 信息。

## 7.2.6 — 2026-07-18

状态：相关能力已随 7.2.13 整体发布到 `192.168.3.78:8765`。

- Sprint Overview 扫描不会在网络中断后永久卡住，并明确“缺少报告”的统计含义。
- Run Review 后保持弹窗打开，卡片可直接显示生成状态。
- Review Progress 手动滚动时暂停自动跟随，静置 60 秒后恢复。

## 7.2.5 — 2026-07-17

状态：相关能力已随 7.2.13 整体发布到 `192.168.3.78:8765`。

- Sprint Overview 的 Issue 列表改为真正的响应式卡片布局。
- 尚无报告的 Issue 直接显示 Run Review，不再重复显示 No report 状态。

## 7.2.4 — 2026-07-17

状态：相关能力已随 7.2.13 整体发布到 `192.168.3.78:8765`。

- Sprint Overview 使用更宽、更紧凑的卡片式工作台。
- Scan 增加清晰的数据加载阶段反馈。
- No report 的 Jira Issue 可从结果行直接启动 Review。

## 7.2.3 — 2026-07-17

状态：相关能力已随 7.2.13 整体发布到 `192.168.3.78:8765`。

- Run Release Gate 调整到卡片右下角，链接输入区域更加完整、清晰。

## 7.2.2 — 2026-07-17

状态：相关能力已随 7.2.13 整体发布到 `192.168.3.78:8765`。

- GIT_VERSION MR 链接输入框从单行高度起步，内容较长时自动增长到两行。
- 移除输入框垂直滚动条，使 Release Gate 操作区更紧凑稳定。

## 7.2.1 — 2026-07-17

状态：相关能力已随 7.2.13 整体发布到 `192.168.3.78:8765`。

- GIT_VERSION MR 链接支持两行显示，并修复复制链接时可能出现的有效 URL 误判；多应用候选可按项目和分支选择。
- Review Progress 操作改为更简洁的图标按钮。
- Sprint Overview 的筛选和扫描区域采用更清晰的响应式布局。

## 7.2.0 — 2026-07-17

状态：已更新本地验收环境，并部署到 192.168.3.78。

- Manager 可直接在 Web 中创建、搜索和维护用户角色、状态及 responsible scope，并安全重置临时密码。
- 所有用户可修改自己的密码；停用、角色变化和密码重置会立即撤销旧会话。
- 用户管理增加并发保存保护、统一验证、一次性凭据提示和脱敏审计。
- 生产升级保留原有账户、配置和 Review 数据，并提供经过实际回滚验证的一键还原入口。

## 7.1.7 — 2026-07-17

状态：相关能力已随 7.2.13 整体发布到 `192.168.3.78:8765`。

- 登录页 Username、Password 和 Robot Check 的必填标记统一显示在字段名称右侧。

## 7.1.6 — 2026-07-17

状态：相关能力已随 7.2.13 整体发布到 `192.168.3.78:8765`。

- Review Communication 改用 copy icon，处理说明收纳到信息提示，输入框拖动和三栏空间利用更加自然。
- Release Notes 弹窗使用适中高度和独立正文滚动，关闭交互更加稳定。
- Issue Review 概览新增 Medium 处理进度，并将阻碍与 Manager exceptions 信息重新编排为紧凑摘要。

## 7.1.5 — 2026-07-17

状态：相关能力已随 7.2.13 整体发布到 `192.168.3.78:8765`。

- Review Communication 支持复制 Follow-up Draft，并优化 Reply、Follow-up 与处理说明的空间层级。
- Report History 的筛选控件和 Tab 栏统一右边界。
- 审查报告的问题列表支持逐项展开和折叠。

## 7.1.4 — 2026-07-17

状态：相关能力已随 7.2.13 整体发布到 `192.168.3.78:8765`。

- GIT_VERSION Release Gate 使用更紧凑的双栏卡片，减少重复说明并强化输入与执行操作。
- Sprint handoff 状态、MR URL 和 Release Gate 操作的层级及响应式布局更加清晰。

## 7.1.3 — 2026-07-17

状态：相关能力已随 7.2.13 整体发布到 `192.168.3.78:8765`。

- Company Config/SCR 报告按项目和资源类型使用可读名称并避免覆盖历史；GIT_VERSION Release Gate 按 WVAdmin、iTrade Client、Services Terminal、DPS 分项目审核，未配置项目会被拒绝。
- 改善 Run Review、Release Gate、Progress 和 Issues Review History Tab 的布局及响应式体验。
- Review Communication 使用 History、Follow-up Draft、Reply 三栏闭环布局。
- Pending Jira 支持 Atlaskit 渐进增强，并在资源不可用时稳定回退到内置 ADF 编辑器。

## 7.1.2 — 2026-07-17

状态：相关能力已随 7.2.13 整体发布到 `192.168.3.78:8765`。

- Issue Review 增加按 Sprint 分组的 Overview，并可追溯同一 Issue 的多次处理 Cycle 与审核快照。
- Sprint Review 加强有效性、Development Done 和 Batch/Final 模式校验。
- 改善多任务 Progress、表单防重复提交及 Problems 处理体验。
## 7.1.1 — 2026-07-17

状态：相关能力已随 7.2.13 整体发布到 `192.168.3.78:8765`。

- 统一页面信息提示图标与交互方式，并修复 Release Notes 打开错误。
- 改善 Report History、AI Assist、Discussion 和 Issue Review 顶部的信息层级与响应式布局。
- Reply、Follow-up 和处理说明采用一致的卡片与操作样式。

## 7.1.0 — 2026-07-17

状态：相关能力已随 7.2.13 整体发布到 `192.168.3.78:8765`。

- Issue Review 支持跨 Sprint 的 Review Cycle，并保留每次处理、报告和快照历史。
- 同一 Issue 再处理时只审核本轮 MR 增量，同时结合目标分支最新代码分析影响。
- Company Config 与 SCR 作为 Deferred Release Resources，统一进入 Web GIT_VERSION Release Gate。
- 完善 Problems 处理、Re-scan、审批、Snapshot、Sprint Overview 与发布门禁闭环。
- 改善 Sprint 搜索预检、Review Progress、多任务视图、表单校验和页面可访问性。
- Release Notes 可直接从 CodeReviewer 站点查看。

## 7.0.5 — 2026-07-17

- GIT_VERSION Release Gate 整合到 Web 平台。
- Sprint Review 可以在线转入 Release Gate，并查看 READY/BLOCKED 及资源摘要。
- CLI 保留为运维备用入口，不再是正常发布流程的必需步骤。

## 7.0.4 — 2026-07-16

- 同一个 Jira Issue 的前端与后端问题清单按项目类型分开生成。

## 7.0.3 — 2026-07-16

- 统一 Codex/CPA 审核通道并改善失败处理。
- 完成 7.0.x 生产升级，同时保留用户验证文件和既有数据。

## 7.0.0 — 2026-07-15

- 增加 Developer、Auditor/Leader、Manager 三角色工作流。
- 增加 Issue Review History、Problems Handling、Re-scan、Manual Pass 和 Pending Jira。
- 增加 ADF 编辑、讨论、审计记录和 SQLite 工作流存储。
