# CodeReviewer 7.0 Issue Review 工作流

> 语言：中文 | [English](CodeReviewer-7.0-Issue-Workflow.md)

版本：7.0.0

开发分支：`20260714`

验收地址：`http://127.0.0.1:8765`

生产部署：本地验收通过前明确不部署生产环境。

## 角色与数据范围

| 功能 | Developer | Auditor / Leader | Manager | 说明 |
| --- | --- | --- | --- | --- |
| 查看分配的 Issue Review | Yes | Yes | Yes | Developer 和 Auditor 按 `responsible` 限定范围；Manager 可查看全部 |
| 运行单个 Issue Review | - | Yes | Yes | Developer 不显示入口，API 也会拒绝请求 |
| 运行 Sprint/Filter Review | - | - | Yes | 用于发布管理工作流 |
| 提交 Finding 处理结果 | Yes | Yes | Yes | 支持已整改、另报 Jira、不是问题 |
| 审批“不是问题” | - | Yes | Yes | Developer 提交后保持待审批状态 |
| Re-scan | - | Yes | Yes | 阻碍项必须从后续 Run 消失，才算完成整改 |
| Manager Exception | - | - | Yes | 必须存在 Pending Jira 草稿并填写审计原因 |
| Manual Pass | - | Yes | Yes | Auditor 只能操作其负责范围 |
| Discuss / Report Preview | Yes | Yes | Yes | 讨论关联 Issue、Run，也可关联具体 Finding |

第一批试用 Developer 映射：

| Responsible | Developer 账户 |
| --- | --- |
| `wen.yi` | `gerhard.guo`、`bryan.tan` |
| `kevin.tan` | `vincentgr.wang`、`kelvinh.wu` |

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
2. 确认 `gerhard.guo`/`bryan.tan` 只能看到 `wen.yi` 范围，`vincentgr.wang`/`kelvinh.wu` 只能看到 `kevin.tan` 范围。
3. 分别提交三种 Handling 类型。
4. 验证 Developer 提交“不是问题”后必须由 Leader 审批。
5. 验证 Fixed Critical/High 在干净的 Re-scan 前不能 Pass。
6. 创建 Jira Follow-up，编辑并预览其 ADF，添加 Expand → 表格/列表/截图，然后从 Pending Jira 重新打开。
7. 验证只有 Manager 能看到并使用 Manager Exception。
8. 针对 Issue/Run 发起讨论，并从历史记录重新打开。
9. 验证 Auditor 只能 Pass 其 responsible 范围内的 Issue，Manager 可以查看全部。
10. 确认未连接或部署到 `192.168.3.78`。

## 配套手册

完整的页面操作、角色流程、错误处理和版本维护规则，请参阅 [CodeReviewer 7.x User Manual](CodeReviewer%20User%20Mannual.md)。
