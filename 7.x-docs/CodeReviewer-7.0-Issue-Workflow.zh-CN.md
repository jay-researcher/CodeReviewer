# CodeReviewer 7.0 Issue Review 工作流

> 语言：中文 | [English](CodeReviewer-7.0-Issue-Workflow.md)

版本：7.2.12

> 7.2.12 UI 补充：Sprint Overview 不继承主页 Jira 字段，并以 Overview / Sprint issues 分开准备度和 Issue 卡片；Issue Review 应用卡在桌面使用可读的三列网格，Problems 分别展示“问题”和“建议”，弹窗统一使用 S/M/L/XL/Full 尺寸规范。
>
> 7.2.11 补充：单 Issue Review 在 Progress 中展示已有报告检查；Problems 显示问题/建议摘要，并从结构化延后资源元数据明确区分 Company Config 与 SCR。分支配置支持精确值与版本通配符；Jira REST 保持权威数据和唯一写入边界，Rovo 仅用于只读候选上下文，本地 jira-prd/RAG 默认关闭。
>
> 7.2.0 权限补充：Manager-only 用户管理可维护角色、启用状态和 responsible 范围。创建/重置只返回一次临时密码；停用、角色变化及密码变化会撤销已有会话。
>
> 7.1.7 UI 补充：登录页 Username、Password、Robot Check 的红色必填标记统一保持在字段名称右侧同行显示。
>
> 7.1.6 UI 补充：Review Communication 使用纯图标复制操作、可纵向拖动的 Reply/Follow-up 字段，并以 information hint 取代常驻处理说明；Release Notes 使用受控高度和独立滚动弹窗；Issue Review 指标展示 Critical、High、Medium 进度以及组合的 Manager exception/阻碍摘要。
>
> 7.1.5 UI 补充：Follow-up Draft 支持复制，Reply/Follow-up 卡片共享右栏可用高度，Report History 筛选区统一右边界，报告“问题列表”中的每个问题可独立展开或折叠。
>
> 7.1.4 UI 补充：GIT_VERSION Release Gate 使用紧凑双栏操作卡。左侧显示 Sprint handoff 上下文；右侧将 MR 字段标题和操作按钮放在同一行，URL 输入框在下一行占满宽度，并保留响应式单列降级。
>
> 7.1.3 补充：Company Config/SCR 延后资源报告使用项目/资源类型命名；每次 GIT_VERSION Release Gate 必须在进入 LLM 前匹配 `config.yml` 中的 GitLab project，同时继续审核该产品锁定的全部源码/构建依赖。独立 Web 站点以 Jira ADF 交换 Description，并可通过本机构建的 Atlaskit bundle 渐进增强；不把 Jira Forge UI 组件误认为可直接嵌入的普通 Web 组件。
>
> 7.1.2 补充：一个 Jira Issue 始终是一条稳定主记录。进入新 Sprint 再处理时关闭旧的活动 Review Cycle，并创建新 Cycle；Web `Overview` 按当前及历史 Sprint/Cycle 聚合，`History & Snapshots` 将 Run 和不可变审核快照嵌套在对应 Sprint Cycle 下。

开发分支：`20260714`

验收地址：`http://127.0.0.1:8765`

生产部署：本地验收通过前明确不部署生产环境。

## 角色与数据范围

| 功能 | Developer | Auditor / Leader | Manager | 说明 |
| --- | --- | --- | --- | --- |
| 查看分配的 Issue Review | Yes | Yes | Yes | Developer 和 Auditor 按 `responsible` 限定范围；Manager 可查看全部 |
| 运行单个 Issue Review | - | Yes | Yes | Developer 不显示入口，API 也会拒绝请求 |
| 运行 Sprint/Filter Review | - | - | Yes | 用于发布管理工作流 |
| 运行 GIT_VERSION Release Gate | - | - | Yes | Web 原生发布门禁；CLI 仅作为运维备用入口 |
| 提交 Finding 处理结果 | Yes | Yes | Yes | 支持已整改、另报 Jira、不是问题 |
| 审批“不是问题” | - | Yes | Yes | Developer 提交后保持待审批状态 |
| Re-scan | - | Yes | Yes | 阻碍项必须从后续 Run 消失，才算完成整改 |
| Manager Exception | - | - | Yes | 必须存在 Pending Jira 草稿并填写审计原因 |
| Manual Pass | - | Yes | Yes | Auditor 只能操作其负责范围 |
| Discuss / Report Preview | Yes | Yes | Yes | 讨论关联 Issue、Run，也可关联具体 Finding |
| 用户管理 | - | - | Yes | 创建、启停、分配角色/范围及重置密码 |

第一批试用 Developer 映射：

| Responsible | Developer 账户 |
| --- | --- |
| `wen.yi` | `gerhard.guo`、`bryan.tan` |
| `kevin.tan` | `vincentgr.wang` |
| `kelvinh.wu` | `benyq.feng` |

`kelvinh.wu` 和 `luckxh.chen` 为 Auditor，并分别使用自身 responsible scope。

## Review Cycle 与增量边界

同一 Jira Issue 始终保留一条主历史记录。后续加入新 Sprint 处理时创建新的 Review Cycle；一次逻辑审核创建 Run Group，并关联本次 frontend/backend Runs：

```text
Issue → Sprint membership → Review Cycle → Run Group → project-type Runs
```

MR revision 使用 GitLab project、MR IID 和 Head SHA 标识。以前 Cycle 已审核且 Head SHA 未变化的 revision 会被排除；Head SHA 改变则重新审核。只有当前 Cycle 的 `base_sha → head_sha` diff 可以产生本轮 Finding；目标分支最新代码只作为影响上下文，原 Description 与以前正式模板 Comments 作为历史需求上下文，不能夹带旧 Cycle diff。

Company Config、SCR revision 持久化为 Deferred Release Resources。只有 deferred 资源时，本轮结果是“无代码变更需要审核、Release Gate Pending”，不能误报 No MR 或 Review Failed。

最新 Run Group 的全部 Finding 提交处理后，系统生成不可变 Review Snapshot；审批、Manager Exception 和 Manual Pass 生成后续 revision，不覆盖以前证据。

## 状态与门禁规则

Issue 生命周期：

```text
Not Reviewed → Generating → Handling → Re-scan Required
             → Re-scanning → Ready for Pass → Passed
```

默认阻碍严重级别为 Critical 和 High，可在 `config.yml` 中配置：

```yaml
app:
  review_workflow:
    blocking_severities: [Critical, High]
    require_rescan_for_fixed: true
    follow_up_unblocks_blocking: false
    manager_override_enabled: true
```

规则：

1. 阻碍 Finding 标记为 `fixed` 后，Issue 进入 `Re-scan Required`，不会立即解除门禁。
2. 后续 Review Run 中必须不再出现相同 Finding 指纹。
3. Developer 提交 `not-issue` 后，必须由 Auditor 或 Manager 审批。
4. `follow-up` 会创建 ADF Jira 草稿，默认不能解除阻碍 Finding。
5. 只有草稿已经存在且填写了原因，Manager 才能记录 Exception；Pass Readiness 和审计数据中会始终标记为 `Manager Exception`。
6. 新的 Run 会使之前关联的 Pass 失效。

## Issue Reviews

`Issue Reviews` 按 ECHNL Issue 汇总报告历史，展示：

- 当前生命周期状态；
- responsible 范围；
- Critical/High 数量；
- fixed、follow-up、not-issue 和 pending 数量；
- 最新 Run 及 Run 总数；
- 新增及持续存在的 Finding；
- 创建时间和最后更新时间；
- 讨论、Pending Jira 草稿和 Pass 记录。

Finding 关联关系使用稳定指纹，指纹由 Issue、项目/文件、分类/规则及规范化标题生成。报告中的顺序编号仅用于显示，不作为持久身份标识。

GIT_VERSION MR 使用专用的发布门禁 LLM 上下文预算。确定性的 YAML、commit lock 和资源完整性检查由本地规则完成；LLM 上下文优先保留锁定仓库 diff、Release Gate 结果及关键配置变化，避免重复的构建配置耗尽深度审核模型的处理时间。

## Web 原生 Sprint 与 Release Gate 流程

正常业务流程不要求 Manager 单独打开终端运行脚本：

```text
Web Run Review / Sprint
  → 查看普通 frontend/backend 报告及 deferred Company Config/SCR
  → Sprint Job 中 Continue to Release Gate
  → 确认或输入 GIT_VERSION MR URL
  → Run Release Gate
  → READY / BLOCKED、锁定仓库、构建资源和阻碍项
  → 在线打开报告并完成发布决策
```

普通 Sprint Review 发现 Company Config/SCR 后，将其列为 deferred，不把这些构建资源放入普通代码 Prompt。发现 GIT_VERSION MR 时，Job 卡片提供 `Continue to Release Gate`；未自动发现时，Manager 也可在 Run Review 的 Release Gate 工作区直接输入完整 MR URL。

Release Gate 通过与其他 Review 相同的 Web Job 队列执行，支持进度、暂停、停止、重试、报告预览和历史恢复。服务端只允许 Manager 启动，并在进入 LLM 前验证目标 MR 必须包含版本化 `git_version.yml`/`build.yml` 资源。

Release Gate 状态：

- `READY`：确定性门禁没有资源/锁定错误，且最终分析没有配置范围内的 Critical/High 阻碍；Manager 仍需查看最终报告。
- `BLOCKED`：缺少源码/构建锁、锁定资源不可读取、SCR/数据库资源不完整、LLM 发现阻碍级问题或存在其他门禁错误；整改后在 Web 中重新运行。
- Job `FAILED`：输入不是 GIT_VERSION MR、认证/网络失败或执行异常，不等同于业务 `BLOCKED`。

CLI `python review.py --mr-url <GIT_VERSION MR>` 继续保留用于自动化和故障排查，但不再是 Web 发布流程的必需步骤。

## Pending Jira 与 ADF

Issue Description 的正式格式为 ADF JSON（`version: 1`、`type: doc`）。API 会验证其结构，并支持编辑和预览。支持的节点包括段落、标题、Panel、代码块、表格、有序/无序列表、Expand、表格单元格中的 Nested Expand，以及 Media。

Expand 可以包含表格、有序列表、无序列表和截图。上传的截图作为草稿附件保存，并生成 `mediaSingle/media` 节点。未来 JiraReviewer 集成创建真实 Issue 时，必须先将附件上传到 Jira，再将本地 Media ID 替换为 Jira Media Services 标识。

相关约束遵循 Atlassian 官方的 [ADF 文档结构](https://developer.atlassian.com/cloud/jira/platform/apis/document/structure/)、[Expand 节点](https://developer.atlassian.com/cloud/jira/platform/apis/document/nodes/expand/)、[Nested Expand 节点](https://developer.atlassian.com/cloud/jira/platform/apis/document/nodes/nestedExpand/) 和 [Media 节点](https://developer.atlassian.com/cloud/jira/platform/apis/document/nodes/media/)。`nestedExpand` 只能位于 `tableCell`/`tableHeader`；普通 `expand` 位于文档顶层。

`frontend/adf-editor` 下的 React/Vite 模块使用 Atlaskit Editor 和 Renderer。REST API 及保存的 ADF 与前端框架解耦，以便后续 Flutter/Dart 客户端复用。

## 存储与迁移

工作流数据库默认为启用 SQLite WAL 和外键的 `data/codereviewer.db`。可通过以下环境变量覆盖路径：

```text
CODEREVIEWER_DB_FILE=/var/lib/codereviewer/data/codereviewer.db
WEB_USERS_FILE=/var/lib/codereviewer/data/web_users.json
WEB_THREADS_DIR=/var/lib/codereviewer/data/web_threads
JIRA_DRAFT_ATTACHMENTS_DIR=/var/lib/codereviewer/data/jira_draft_attachments
```

在本地运行可重复执行的迁移：

```powershell
py -V:Astral/CPython3.14.6 tools\migrate_workflow_data.py
```

迁移会备份用户、`review_history.jsonl` 和旧版 Web Threads；对旧版明文密码进行哈希；注册历史 Review Run/Finding；并导入现有 Handling、Discussion 和 Pass 记录。

SQLite 通过工作流 Repository 边界访问。未来可替换为 MongoDB 或特定用途数据库，而不需要修改 Web/Flutter API 契约。

## 本地验收清单

1. 分别使用每个试用 Developer 登录，确认不显示 Run Review。
2. 确认 `gerhard.guo`/`bryan.tan` 只能看到 `wen.yi` 范围，`vincentgr.wang` 只能看到 `kevin.tan` 范围，`benyq.feng` 只能看到 `kelvinh.wu` 范围。
3. 分别提交三种 Handling 类型。
4. 验证 Developer 提交“不是问题”后必须由 Leader 审批。
5. 验证 Fixed Critical/High 在干净的 Re-scan 前不能 Pass。
6. 创建 Jira Follow-up，编辑并预览其 ADF，添加 Expand → 表格/列表/截图，然后从 Pending Jira 重新打开。
7. 验证只有 Manager 能看到并使用 Manager Exception。
8. 针对 Issue/Run 发起讨论，并从历史记录重新打开。
9. 验证 Auditor 只能 Pass 其 responsible 范围内的 Issue，Manager 可以查看全部。
10. 确认未连接或部署到 `192.168.3.78`。
11. Manager 运行 Sprint Review，并从完成的 Job 跳转到 Release Gate。
12. 验证普通 MR 被拒绝作为 Release Gate 输入，GIT_VERSION MR 能显示 READY/BLOCKED 和资源统计。

## 配套手册

完整的页面操作、角色流程、错误处理和版本维护规则，请参阅 [CodeReviewer 7.x User Manual](CodeReviewer%20User%20Mannual.md)。
