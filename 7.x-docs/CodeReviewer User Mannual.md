# CodeReviewer 7.x User Manual

> 中文名称：CodeReviewer 7.x 用户手册
>
> 当前适用版本：7.0.3
>
> 文档性质：7.x 持续维护主手册
>
> 最后更新：2026-07-16
>
> 开发分支：`20260714`

配套文档：

- [Issue Review 工作流与本地验收（中文）](CodeReviewer-7.0-Issue-Workflow.zh-CN.md)
- [Issue Review Workflow and Local Acceptance (English)](CodeReviewer-7.0-Issue-Workflow.md)

## 1. 文档维护约定

本手册适用于 CodeReviewer 7.x 系列。从 7.0.0 开始，所有影响用户操作、角色权限、审核规则、页面入口、状态、表单、配置默认值或错误处理方式的 7.x 更新，都必须同步更新本文件。

每次发布 7.x 更新时，至少检查并更新：

1. 首页的“当前适用版本”和“最后更新”。
2. 角色权限矩阵。
3. 页面入口和操作步骤。
4. Finding 处理、Re-scan 和 Manual Pass 规则。
5. Pending Jira 与 ADF 支持范围。
6. 配置项及默认值。
7. 常见问题与错误提示。
8. 文末的版本更新记录。

本手册面向日常业务用户。部署、服务管理和服务器配置请参考 `docs` 目录中的部署及运维文档。

## 2. CodeReviewer 7.x 用途

CodeReviewer 用于审核 Jira Issue 关联的 GitLab Merge Request、配置、脚本和发布资源，并将 AI Review 报告转换为可跟进、可重扫、可审计的 Issue Review 工作流。

7.x 的主要工作闭环为：

```text
生成 Review 报告
  → 查看问题列表
  → 逐项填写处理结果
  → 修复项 Re-scan
  → 检查新问题和仍然存在的问题
  → Auditor / Manager 确认
  → Manual Pass
```

系统以 ECHNL Issue 为主要业务对象。一条 ECHNL Issue 可以关联多个 Review Run、多个报告、多个 Finding、处理记录、讨论和待创建 Jira 草稿。

## 3. 登录

本地验收环境默认地址：

```text
http://127.0.0.1:8765
```

登录需要：

- Username；
- Password；
- Robot Check 算术验证码。

新账户使用管理员安全交付的一次性初始密码。首次登录后应尽快更换密码；不要通过 Jira Comment、Teams 群或代码仓库传递密码。

如果验证码过期，请点击登录页面的 `Reset` 获取新验证码。

### 3.1 第一批 Developer 账户

| Responsible 范围 | Developer 账户 |
| --- | --- |
| `wen.yi` | `gerhard.guo`、`bryan.tan` |
| `kevin.tan` | `vincentgr.wang`、`kelvinh.wu` |

Developer 看到的是映射到其 responsible 范围的 Issue Review，不代表 Developer 本身就是项目的 responsible owner。

## 4. 角色与权限

CodeReviewer 7.x 使用三个角色：

- Developer：查看问题、修复代码并提交逐项处理结果。
- Auditor / Leader：审核负责范围内的 Issue、运行单 Issue Review、Re-scan、审批处理结果并 Manual Pass。
- Manager：查看全局范围、运行 Sprint/Filter Review、检查发布覆盖率、执行 Manager Exception 和最终发布审核。

系统中的 `Leader` 对应 `Auditor`，不单独设置第四个角色。

### 4.1 权限矩阵

| 功能 | Developer | Auditor / Leader | Manager | 说明 |
| --- | --- | --- | --- | --- |
| 登录及查看工作台 | Yes | Yes | Yes | 页面内容按角色调整 |
| 查看授权报告 | Yes | Yes | Yes | Developer/Auditor 按 responsible 隔离 |
| 查看 Issue Review History | Yes | Yes | Yes | Manager 可查看全部 |
| Run Issue Review | - | Yes | Yes | Developer 不显示入口，API 也会拒绝 |
| Run Sprint / Filter Review | - | - | Yes | 用于发布前全局 Review |
| Review Coverage | - | Yes | Yes | Auditor 仅限负责范围 |
| 提交 Finding 处理结果 | Yes | Yes | Yes | 支持三种 Result |
| 审批“不是问题” | - | Yes | Yes | Developer 提交后为 Pending |
| Re-scan Issue | - | Yes | Yes | Auditor 仅限负责范围 |
| Manager Exception | - | - | Yes | 必须填写原因并已有 Jira 草稿 |
| Manual Pass | - | Yes | Yes | 必须满足门禁规则 |
| Report Discuss | Yes | Yes | Yes | 必须有报告访问权限 |
| AI Chat | - | Yes | Yes | 用于理解报告及查询待处理问题 |
| Teams Delivery | - | Yes | Yes | 用于发送审核信息 |

隐藏按钮不等于唯一的权限保护。即使手工调用 API，服务端仍会校验角色和 responsible 范围。

## 5. 页面与入口

### 5.1 Run Review

Auditor 和 Manager 使用 `Run Review` 生成新报告。

可用输入包括：

- Jira Issue，例如 `ECHNL-8888`；
- Sprint ID，仅 Manager 可用；
- Jira Filter ID，仅 Manager 可用；
- Report Priority。

对已有报告的单 Issue 再次 Review 时，系统会先检查是否存在可复用报告。明确选择 Re-scan 后才会创建新的 Review Job。

Developer 看不到 Run Review 操作。

#### 5.1.1 Jira Review 的 MR 发现机制

直接输入 GitLab MR URL 时，系统审核指定 MR，不执行 Jira MR 发现。使用 Jira Issue、Sprint 或 Jira Filter 运行 Review 时，系统先加载 Jira Issue，再按下表聚合关联 MR：

| 顺序 | 来源 | 当前行为 |
| --- | --- | --- |
| 1 | Jira Issue 字段 | 从 Issue Summary 和最终版 Issue Description 中提取 GitLab MR URL；最终版 Description 包含原 Description 与符合 Description 模版的 Comments。 |
| 2 | Jira Remote Links | 调用 Jira Remote Links API，提取 GitLab 集成写入的 MR Web Link。 |
| 3 | Jira Development Panel | 调用 Jira Development Panel 接口，读取 GitLab 集成保存的 merge request 数据。 |
| 4 | GitLab Issue Key 搜索 | 默认补充搜索当前用户 Token 可见的 GitLab MR；以 Jira Key 搜索 MR Title/Description，并校验 MR 元数据确实关联该 Issue。默认同时补充较早的 Open/Merged MR。 |
| 5 | GitLab Branch Discovery | 默认使用 `missing-only`：前述来源仍没有记录时，才在配置范围内搜索名称包含完整 Jira Key 的分支，再查询这些分支创建的 MR。 |

这些来源采用“合并并去重”而非命中第一个来源后立即结束；同一个 MR URL 只保留一条。每条发现结果会保留 `source`，常见值为 `jira-issue-fields`、`jira-remote-link`、`jira-development-panel`、`gitlab-search` 和 `gitlab-branch`，便于在任务详情中判断 MR 来自哪里。

当前默认发现范围和限制如下：

- Jira Issue 必须处于允许审核的状态；7.0.3 默认只有 `Development Done`。状态不符合时，系统会在 MR 发现前跳过 Issue。
- 默认接受 `opened` 和 `merged` MR；`closed`、`locked` 等状态不会进入正式 Review，但会记录为 State skipped。需要特殊复核时可将 MR State 调整为 `all`。
- GitLab 项目范围由 Git Tools/CodeReviewer 配置中的 Repository URL，以及额外配置的项目共同决定。当前 `filter_to_projects: true`，因此即使 Jira 中存在 Web Link，配置范围外的项目也不会进入 Review。
- 默认总 MR 上限为 200；每个 Issue 的 GitLab 历史搜索上限为 100；Branch Discovery 最多扫描 200 个配置项目、每项目 20 个匹配分支、每分支 20 个 MR。达到限制后不再继续追加。
- Company Config 和 SCR 类型 MR 会被发现，但不进入普通 Jira/Sprint Review；它们作为发布资源延后到 GIT_VERSION Release Gate 统一核对。
- GIT_VERSION 是发布级 MR，不会并入普通 Jira/Sprint Review，必须通过明确的 MR URL 单独运行 Release Review。
- 目标分支属于配置的 development-version 分支时，该 MR 会记录为 Dev-branch skipped，避免将开发版本合并误当作正式交付审核。

Sprint 关闭本身不会删除 Issue，也不会阻止 Jira Remote Links 或 Development Panel 查询。系统仍可通过 Sprint ID/名称加载已关闭 Sprint 的 Issue。常见的“关闭 Sprint 后找不到 MR”实际由以下条件造成：

1. Issue 状态已从 `Development Done` 转为其他状态，因状态门禁在发现前被跳过；
2. MR 状态为 `closed`，不在默认的 `opened, merged` 范围；
3. MR 所属项目不在配置的 GitLab 项目范围；
4. MR 属于 Company Config、SCR、GIT_VERSION 或 development-version 分支，被路由到 Skipped/Release Gate，而非普通问题报告；
5. Jira/GitLab Token 无权读取 Remote Links、Development Panel、目标项目或 MR。

任务详情中的 `Discovered`、`State skipped`、`Branch-type skipped`、`Dev-branch skipped`、`Issues without MRs` 和 `Errors` 应结合查看；“发现但被跳过”和“完全没有发现”是不同结果。

### 5.2 Report Preview

Report Preview 用于在线阅读 Markdown 报告。

主要操作：

- Preview：打开完整预览；
- Compare：与上一份报告比较；
- Download：下载原始报告；
- Discuss：打开 Review Communication；
- Previous / Next：切换相邻报告。

报告预览和 Issue Review 是两个视角：

- Report Preview 关注某一份具体报告；
- Issue Reviews 关注一条 ECHNL Issue 的完整生命周期。

### 5.3 Report History

Report History 支持：

- 按报告名、Jira 或 responsible 搜索；
- 选择最近 14、30、60 天或全部记录；
- 查看 Markdown Reports；
- 按 responsible 下载报告集合。

### 5.4 Issue Reviews

点击页面顶部的 `Issue Reviews` 打开 Issues Review History。

列表显示：

- ECHNL Issue 和 Summary；
- Responsible；
- 当前状态；
- 最新 Review Run；
- Finding 数量；
- Fixed、Follow-up Jira、Not issue 和 Pending 数量；
- 最后更新时间。

点击一行进入 Issue Review 详情。

### 5.5 Pending Jira

点击页面顶部的 `Pending Jira` 查看“不是阻碍，另报 Jira”产生的草稿。

草稿只表示“待创建”，7.0.0 不会自动在 Jira 中创建真实 Issue。后续版本将通过 JiraReviewer，由 Manager 创建真实 Jira Issue。

### 5.6 Review Coverage

Auditor 和 Manager 可以使用 Review Coverage 检查指定 Jira、Sprint 或 Filter 范围。

Coverage 用于识别：

- 尚未生成报告；
- 正在生成；
- 生成失败；
- 已生成但仍在 Handling；
- 等待 Re-scan；
- 已具备 Pass 条件；
- 已 Review Pass。

Auditor 的 Coverage 结果按 responsible 过滤，Manager 查看全局结果。

## 6. Issue Review 状态

| 状态 | 含义 |
| --- | --- |
| Not Reviewed | 尚未生成 Review Run |
| Generating | 正在生成报告 |
| Handling | 存在需要处理或确认的 Finding |
| Re-scan Required | 已提交修复，需要重新扫描验证 |
| Re-scanning | 正在执行新的 Review Run |
| Ready for Pass | 当前门禁问题已经清除 |
| Passed | Auditor/Manager 已手动通过 |
| Generation Failed | 初次报告生成失败 |
| Re-scan Failed | 重扫失败 |
| Reopened | Pass 后出现新 Run 或代码变化，需要重新审核 |

每个 Review Run 是独立的历史快照。新 Run 不会覆盖旧报告和旧讨论。

## 7. 严重级别与门禁

7.0.0 默认将以下严重级别作为阻碍项：

- Critical；
- High。

默认策略：

```yaml
app:
  review_workflow:
    blocking_severities: [Critical, High]
    require_rescan_for_fixed: true
    follow_up_unblocks_blocking: false
    manager_override_enabled: true
```

管理员可以调整 `blocking_severities`。调整后，Issue Review、Pass Readiness 和兼容的 Report Communication 都必须遵循同一门禁范围。

## 8. 三种 Finding 处理方式

### 8.1 已整改，Pass 通过

适用情况：代码、配置或脚本已完成修改。

操作要求：

1. 选择 `已整改，Pass通过`。
2. 填写具体整改说明，不能只写“已处理”。
3. 提交后，Critical/High 所在 Issue 进入 `Re-scan Required`。
4. Auditor/Manager 发起 Re-scan。
5. 只有问题在后续 Review Run 中消失，才视为门禁已解除。

同一份报告上将 Finding 标记为 Fixed，不会立即允许 Manual Pass。

### 8.2 不是阻碍，另报 Jira

适用情况：问题确实存在，但可以作为独立改进项后续跟进。

操作要求：

1. 选择 `不是阻碍，另报 Jira`。
2. 填写 Handling 说明。
3. 填写 Issue Summary。
4. 使用 ADF 编辑器填写 Issue Description。
5. 提交后在 `Pending Jira` 中检查草稿。

该结果默认不能解除 Critical/High 阻碍。

只有 Manager 可以执行 `Manager Exception`。Manager 必须：

- 确认 Pending Jira 草稿已经存在；
- 填写明确的风险接受原因；
- 接受审计记录中永久显示 Manager Exception。

Manager Exception 不会把问题伪装为“已修复”。

### 8.3 不是问题，Pass 通过

适用情况：误报、上下文判断错误或已有其他控制措施证明问题不成立。

操作要求：

1. 选择 `不是问题，Pass通过`。
2. 填写可验证的判断依据。
3. Developer 提交后，状态为待审批。
4. Auditor/Manager 确认后才解除门禁。

Developer 自己提交“不是问题”不能直接使 Issue Ready for Pass。

## 9. Re-scan

Re-scan 会产生新的 Review Run，不覆盖旧 Run。

系统通过稳定 Finding 指纹关联前后结果。指纹综合：

- Jira Issue；
- 项目和文件路径；
- Finding 分类或规则；
- 规范化标题。

新 Run 中常见的关联结果：

- New：之前没有出现；
- Persisting：上一次存在，本次仍然存在；
- Resolved：旧 Run 存在，但新 Run 已不再出现。

当前页面主要显示最新 Run 的 New 和 Persisting；Resolved 通过前后 Run 对比及问题从最新 Run 消失进行确认。

Re-scan 后请重点检查：

1. 已整改的阻碍项是否消失。
2. 是否产生新的 Critical/High。
3. Persisting Finding 是否需要再次处理。
4. Review 使用的代码/MR 是否为最新版本。

## 10. Manual Pass

只有 Auditor/Manager 可以操作 Manual Pass。

允许 Pass 的默认条件：

- 最新 Review Run 已完成；
- 最新 Run 中没有未解除的 Critical/High；
- Fixed 阻碍项已经通过后续 Re-scan 消失；
- Developer 提交的 Not an issue 已经审批；
- Follow-up 阻碍项已经修复，或由 Manager 正式记录 Exception；
- 操作者拥有该 Issue 的访问权限。

Pass 会记录：

- ECHNL Issue；
- 对应 Review Run；
- 操作人；
- 时间；
- Pass Note；
- 当时的门禁策略；
- Manager Exception 数量。

如果 Pass 后产生新 Run、MR commit 变化或新阻碍 Finding，旧 Pass 不再代表新版本已经通过。

## 11. Discuss 与 AI Chat

### 11.1 Discuss

人工讨论可以关联：

- ECHNL Issue；
- Review Run；
- 具体报告；
- 可选的 Finding。

讨论内容会保留在 Review History 中。Re-scan 后，旧 Run 的讨论仍然属于旧快照。

建议 Discussion 写清楚：

- 讨论的是哪个 Finding；
- 当前判断；
- 需要谁确认；
- 预期完成时间或后续动作。

### 11.2 AI Chat

Auditor/Manager 可使用 AI Chat：

- 总结报告中的 Critical/High；
- 查询还有哪些问题未处理；
- 理解 Finding 的风险；
- 整理后续跟进项；
- 生成 Teams 通知措辞。

AI 回答用于辅助判断，不能代替 Handling、审批、Re-scan 或 Manual Pass 记录。

## 12. Pending Jira 与 ADF

Issue Description 的正式存储格式是 Atlassian Document Format（ADF）JSON，不以 Markdown 作为正式主格式。

### 12.1 编辑与预览

Pending Jira 支持：

- 查看 Issue Summary；
- 编辑 ADF Description；
- Edit / Preview 切换；
- 保存草稿；
- 版本冲突检查；
- 上传截图。

如果提示草稿已被其他用户更新，请重新加载后再编辑，避免覆盖他人的修改。

### 12.2 支持的常用 ADF 内容

- Paragraph；
- Heading；
- Ordered List；
- Bullet List；
- Table；
- Expand；
- Nested Expand；
- Panel；
- Code Block；
- Blockquote；
- Link；
- Media / Screenshot。

### 12.3 Expand

Expand 内支持：

- 表格；
- 有序列表；
- 无序列表；
- 图片和截图；
- 段落、标题、Panel、代码块等内容。

建议将复现证据、截图、测试步骤和补充说明放在 Expand 中，使 Jira Description 主体保持易读。

`nestedExpand` 仅用于 Table Cell 或 Table Header，不能在文档根节点任意插入。

### 12.4 截图

7.0.0 的操作顺序：

1. 先提交 Follow-up，生成 Pending Jira 草稿。
2. 从 Pending Jira 打开草稿。
3. 点击 `Screenshot`。
4. 选择 PNG、JPEG、GIF 或 WebP。
5. 图片会作为草稿附件保存，并写入 ADF `mediaSingle/media` 节点。

默认单个请求和附件有大小限制。超出限制时请压缩图片，不要使用超大原图。

未来 JiraReviewer 创建真实 Jira 时，需要先上传附件到 Jira，再将草稿中的本地 Media ID 转换为 Jira Media ID。

## 13. 各角色推荐流程

### 13.1 Developer

1. 登录后打开 `Issue Reviews`。
2. 选择分配范围内的 ECHNL Issue。
3. 阅读最新 Run 的问题列表和修复建议。
4. 修改代码、配置或脚本。
5. 对每个需要跟进的 Finding 选择 Result 并填写说明。
6. 如果选择另报 Jira，完成 ADF Description。
7. 通知 Auditor 已完成处理，等待审批或 Re-scan。

Developer 不运行 Review、不执行 Re-scan、不 Manual Pass。

### 13.2 Auditor / Leader

1. 使用负责范围的 Jira Issue 运行 Review。
2. 在线预览报告并查看 Critical/High。
3. 使用 Discuss 或 AI Chat 理解风险及剩余问题。
4. 要求 Developer 逐项提交 Handling。
5. 审批“不是问题”。
6. 对 Fixed 阻碍项执行 Re-scan。
7. 检查 New、Persisting 和剩余 Blockers。
8. 满足门禁后填写 Pass Note 并 Manual Pass。

### 13.3 Manager

1. 在合并 Company_Config、GIT_VERSION、SCR 等发布分支前运行 Sprint/Filter Review。
2. 使用 Review Coverage 检查遗漏、生成中、失败、Handling 和 Passed 数量。
3. 将含 Critical/High 的 Issue 交给对应 Auditor 跟进。
4. 确认发布前必须修复的问题已经完成 Re-scan。
5. 确认每个门禁 Finding 都有有效处理结论。
6. 如确需延期 Critical/High，检查 Jira 草稿并记录 Manager Exception。
7. 最终检查 Ready for Pass / Passed 状态后再进入发布流程。

## 14. 常见提示与处理

### Authentication required

登录已过期或尚未登录。重新打开登录页并登录。

### Client IP is not whitelisted

当前客户端 IP 不在白名单。联系管理员，不要自行绕过访问控制。

### Your role cannot submit handling results

账户角色或资源范围不允许该操作。确认是否使用正确账户，以及 Issue 是否属于授权 responsible。

### You do not have access to this Issue Review

Developer/Auditor 正在访问其他 responsible 的 Issue。联系 Manager 检查映射，不要使用他人账户。

### Blocking findings remain

仍有 Critical/High 未解除。检查最新 Run、待审批 Not an issue、尚未重扫的 Fixed 和未处理 Follow-up。

### Manager override reason is required

Manager Exception 必须填写原因，且必须存在 Pending Jira 草稿。

### Jira draft was updated by another user

草稿版本冲突。重新加载最新草稿后再修改。

### ADF schema error

ADF 节点、层级或属性不符合规则。重点检查：

- Expand 是否位于顶层；
- Nested Expand 是否位于 Table Cell/Header；
- Media 是否位于 mediaSingle/mediaGroup；
- Expand 是否有 Title；
- 文档是否为 `version: 1`、`type: doc`。

### ADF editor asset is not built

可选的 Atlaskit React/Vite 资源未构建。系统仍会使用内置 ADF 编辑和预览，不影响 ADF JSON、Expand、表格、列表及截图草稿。

### LLM provider `codex-cli` timed out

如果任务已经发现 MR，但在约 300 秒后连续重试并失败，说明问题位于 Codex 通道而不是 Jira 或 GitLab 抓取。Windows 本地环境默认通过 `http://127.0.0.1:8318/v1` 接入 CLIProxyAPI；请确认：

- CLIProxyAPI 正在监听 8318 端口；
- `OPENAI_API_KEY` 保存的是 CLIProxyAPI 客户端 key，且未写入仓库；
- `LLM_CODEX_HTTP_API_KEY_ENV` 或策略配置指向 `OPENAI_API_KEY`；
- CLIProxyAPI 的 `/v1/models` 和 Responses API 均可访问；
- 修复通道后重新运行 Review，失败的旧任务不会自动续跑。

DPS/DPS9/DPS11 项目必须完成 Codex 审查。Codex 连续失败时，系统不会降级为规则扫描或其他 LLM，以免将不完整报告误认为正式 Review 结果。

Windows“用户变量”更新后，已经打开的 PowerShell 或 VS Code 通常仍保留旧环境快照。7.0.2 会在进程中尚无 `OPENAI_API_KEY` 时读取当前 Windows 用户变量，因此不再强制要求关闭旧终端；进程变量或项目 `.env` 中已有的值仍具有更高优先级，用户变量不会覆盖它们。

`gpt-5.6-sol` 使用 High reasoning 审核较大 MR 时，模型阶段可能持续数分钟。7.0.3 默认只通过 Codex CLI 调用 CPA，不执行 CC Switch 自动回退。CPA/Codex 失败时任务直接失败并保留错误；CC Switch 仅在运维人员明确选择 `LLM_PROVIDER=cc-switch` 的独立兼容任务中使用。DPS 项目发生 300 秒硬超时后只执行一次 Codex 尝试。

## 15. 数据与安全注意事项

- 不要将密码、Token 或 Jira/GitLab 凭据写入报告、Discussion 或 Git。
- 一次性初始密码完成交付后应删除明文交付文件。
- 不要手工修改 SQLite 数据库来绕过 Pass 门禁。
- Manager Exception 是正式风险接受记录，不应用于减少正常修复工作。
- Discussion 和 AI Chat 不等同于正式 Handling。
- 删除草稿截图前应确认其未被 ADF 引用。
- 发现数据范围不正确时，停止操作并通知 Manager 检查 responsible 映射。

## 16. 用户验收清单

### Developer

- 能登录并看到正确 responsible 范围。
- 看不到 Run Review。
- 看不到 Manual Pass。
- 可以查看报告和 Issue Review。
- 可以提交三种 Handling。
- 可以创建 Pending Jira 草稿。

### Auditor

- 可以运行单 Issue Review。
- 只能查看负责范围。
- 可以审批 Not an issue。
- 可以 Re-scan。
- 满足门禁后可以 Manual Pass。
- 不能使用 Manager Exception。

### Manager

- 可以查看全局 Issue Review。
- 可以运行 Sprint/Filter Review。
- 可以查看全局 Coverage。
- 可以执行 Manager Exception。
- 可以执行 Manual Pass。

### ADF

- Edit 和 Preview 可用。
- Expand 可包含表格、有序列表、无序列表和截图。
- 保存并重新打开后内容保持不变。
- 非法 Nested Expand 会被拒绝。
- 并发版本冲突不会静默覆盖。

## 17. 7.x 版本更新记录

### 7.0.3 — 2026-07-16

- 默认 LLM Provider 调整为 `codex-cli`，统一通过本机 CPA Responses API 执行审核。
- 禁止 Codex/CPA 失败后自动回退到 CC Switch；失败任务直接报告 CPA/Codex 错误，避免额外等待和不同模型产生不一致报告。
- CC Switch 适配器继续保留，但只有显式选择 `LLM_PROVIDER=cc-switch` 时才会调用。

### 7.0.2 — 2026-07-16

- 命令行从旧 PowerShell/VS Code 启动时，可自动读取已配置的 Windows 用户级 `OPENAI_API_KEY`，避免环境变量界面已有值但当前进程不可见。
- 优化 Finding Handling：提交操作固定在问题标题右上角；普通处理隐藏空白右栏；“不是阻碍，另报 Jira”使用更紧凑清晰的左右双栏和 Jira Follow-up 卡片。
- 修正 `web-sv-build/dps` 等 DPS 根项目识别；DPS Codex 硬超时后不再执行无效的多轮重试或 CC Switch 回退，最长失败等待由约 24 分钟收敛到单次 300 秒。

### 7.0.1 — 2026-07-16

- Windows 本地 CodeReviewer 默认通过 CLIProxyAPI `127.0.0.1:8318/v1` 调用 Codex Responses API。
- CLIProxyAPI 凭据继续由外部 `OPENAI_API_KEY` 注入，不保存到 Git 仓库。
- 补充 Codex 300 秒超时的诊断说明，明确 MR 已发现时无需重复排查 Jira/GitLab。
- 补充 Jira Issue、Sprint、Filter Review 的 MR 多来源发现顺序、范围过滤、分支类型路由及关闭 Sprint 后的诊断方法。

### 7.0.0 — 2026-07-15

- 建立 Developer、Auditor/Leader、Manager 三角色权限模型。
- 增加第一批 Developer 账户及 responsible 映射。
- 增加以 ECHNL Issue 为中心的 Issues Review History。
- 增加 Review Run、Finding lineage 和状态生命周期。
- 实现 Handling → Re-scan → Problems → Handling 闭环。
- Critical/High Fixed 必须经过后续 Re-scan 验证。
- Developer 的 Not an issue 必须由 Auditor/Manager 审批。
- 增加 Manager Exception 及审计记录。
- 增加 Issue/Run Discussion。
- 增加 Pending Jira 草稿、ADF 编辑/预览和版本控制。
- ADF Expand 支持表格、有序列表、无序列表和截图。
- 增加 SQLite 工作流数据库、历史迁移及未来数据库适配边界。
- 密码升级为 PBKDF2-SHA256 哈希存储。
- GIT_VERSION MR 使用独立的发布门禁 LLM 上下文预算，优先保留锁定仓库、Release Gate 和关键 diff，减少重复构建配置导致的超时。
- 优化 Review Communication 与 Issue Reviews：增加处理说明指引，Finding Handling 使用响应式左右布局，问题标题增加安全留白，Issue History 改为卡片式列表。
- 进一步统一 Issue Review 内容安全间距；Jira Follow-up 使用独立卡片，提供宽版 Issue Summary、Issue Description 摘要回显和紧凑的 `Edit issue` 弹窗入口。
- Critical、High 和 Remaining blockers 指标支持点击定位到 Problem List 中第一个匹配的 Finding。
- ADF Issue Description 默认使用可视化 Block 编辑器，可从工具栏添加组件、拖放排序、编辑或删除；ADF JSON 仅作为隐藏存储格式，不再要求用户直接编辑。
- Jira Follow-up 弹窗使用语义明确的 `Apply description` / `Save draft` 与 `Cancel` 操作。
- Discuss 的 Reply 列与 Issue Review 详情区统一为安全间距、卡片分组、字段标签和右对齐操作布局。
- Issue Review 详情的 Problems、Discuss、History、Pending Jira 支持实际 Tab 切换；指标跳转会自动返回 Problems。

---

后续 7.x 功能变更不得只更新开发说明或 Release Note；必须同时更新本手册中对应的用户操作、权限、规则及版本记录。
