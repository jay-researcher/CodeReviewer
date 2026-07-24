# CodeReviewer 7.x User Manual

> 中文名称：CodeReviewer 7.x 用户手册
>
> 当前适用版本：7.2.17
>
> 文档性质：7.x 持续维护主手册
>
> 最后更新：2026-07-24 CST
>
> 开发分支：`20260720`

配套文档：

- [Issue Review 工作流与本地验收（中文）](CodeReviewer-7.0-Issue-Workflow.zh-CN.md)
- [Issue Review Workflow and Local Acceptance (English)](CodeReviewer-7.0-Issue-Workflow.md)
- [站点公开版本摘要](public%20release%20notes.md)

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

7.2.14 生产环境地址：

```text
http://192.168.3.78:8765
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
| `kevin.tan` | `vincentgr.wang`、`kelvinh.wu`、`benyq.feng` |

Developer 看到的是映射到其 responsible 范围的 Issue Review，不代表 Developer 本身就是项目的 responsible owner。
`kelvinh.wu` 为普通 Developer，使用 `kevin.tan` responsible scope；`luckxh.chen` 为 Auditor，并使用自身 responsible scope。

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
| AI Assist | - | Yes | Yes | 用于理解报告及查询待处理问题 |
| Sprint Overview / Coverage | - | Yes | Yes | Auditor 按 responsible；Manager 查看全局 |
| 用户管理 | - | - | Yes | 创建、启停、角色及 responsible 范围、重置密码 |
| Configuration | - | - | Yes | 在线维护安全的应用设置、GitLab 项目元数据及恢复备份 |
| Health details | Yes | Yes | Yes | 登录前只显示脱敏摘要；登录后可查看组件状态 |

隐藏按钮不等于唯一的权限保护。即使手工调用 API，服务端仍会校验角色和 responsible 范围。

### 4.2 用户管理

Manager 可从顶部 `Users` 打开用户管理。左侧列表支持按用户名搜索，并按角色或 Active/Inactive 状态筛选；右侧用于创建账户或编辑所选账户。

可管理的字段包括：

- Username：创建后不可修改，忽略大小写保持唯一；`admin`、`root` 为保留名称；
- Role：Developer、Auditor / Leader、Manager；
- Status：Active 或 Inactive；
- Responsible scope：决定 Developer/Auditor 可查看的报告与 Issue Review 范围；它不是组织上下级或 Jira Assignee。

保存采用版本校验；如果其他 Manager 已经更新同一账户，系统会提示刷新，避免静默覆盖。Manager 不能降级或停用自己的账户，受保护的 `root` 必须保持 Active Manager，系统也不会允许没有任何 Active Manager。

点击 `Reset password` 前需要再次确认。成功后临时密码只在当前结果卡显示，不写入页面地址或浏览器存储；请通过安全渠道一次性交付。重置密码、停用账户或修改角色会立即撤销该账户的现有会话。所有用户可从顶部 `Change password` 修改自己的密码，新密码要求 14–128 位，并同时包含大小写字母、数字和符号。

用户管理响应不会返回密码哈希。创建、修改、重置密码与改密会写入独立的脱敏审计记录。

Responsible scope 的匹配对象是报告目录和 Jira Issue 上保存的 Responsible 标识：

- `gerhard.guo → wen.yi`：该 Developer 可查看并处理 `wen.yi` 范围的报告和 Issue Review；
- `vincentgr.wang` / `kelvinh.wu` / `benyq.feng → kevin.tan`：这些 Developer 使用 `kevin.tan` 范围；
- Auditor 可以选择多个 Responsible，以覆盖多个负责团队；
- Manager 始终是 `Global access`，不应用 Responsible mappings，保存时也不要求选择。

Developer/Auditor 没有选择任何 Responsible 时，页面和服务端都会拒绝保存。修改角色、停用账号或改变 scope 会撤销现有会话，重新登录后按新范围加载数据。

### 4.3 Configuration

Manager 可从顶部 `Configuration` 打开配置工作区：

- `Application settings`：按 Report、Review Workflow、Jira、LLM、Local Context 等分类展示可安全在线修改的配置节点；
- `GitLab projects`：维护 DPS9/DPS11、iTrade 7.5.0/7.5.1、Services Terminal、WVAdmin 及子项目的 URL、Responsible、分支、类型、模型和应用归属；可在现有 Group 下添加模块，或删除子项目，删除前会自动生成恢复点；
- `Backups & restore`：查看每次修改前自动生成的备份，并通过 `Restore` 确认后恢复。

Web 修改不会直接格式化或覆盖 `config.yml`，而是写入 `data/web_config_overrides.json`，与部署基线合并为 Effective Config。保存采用 revision 校验、原子写、脱敏审计和自动备份；其他 Manager 已经更新时会提示刷新。新值用于后续创建的 Review Job，正在运行的 Job 不应依赖中途变化的配置。

密码、Token、API Key、`app.web.users` 和任意本地路径不在在线编辑器中显示。用户账户继续只通过 `Users` 管理。

配置页面将 `LLM` 始终按缩写大写展示，不会显示为 `Llm`。弹窗统一使用 S、M、L、XL、Full 五档宽度规范：确认操作使用 M，Release Notes 和报告预览使用 L，Sprint Overview 与 Review Communication 使用 XL，管理工作区使用 Full；窄屏均保留安全边距并响应式降级。

`branch` / `branches` 可以使用精确值，例如 `7.5.1.38`，也可以使用版本通配符，例如 `7.5.1.*`。每个通配符会查询该项目远端 heads，并按数字版本选择最高匹配分支；多个版本线可以同时配置，例如 iTrade Client 的 `7.5.0.*` 和 `7.5.1.*`。通配符没有匹配时任务会显示可诊断错误，不会静默选择其他分支。

知识上下文使用以下明确边界：

- Jira REST 是 Issue 字段和状态的权威来源，也是唯一 Jira 写入渠道；
- Rovo MCP 只读检索相关 Jira、Confluence 和 Teamwork Graph 信息，作为候选上下文；
- JiraReviewer 继续负责字段映射、语义规则、ADF、重复保护与审计；
- 本地 `D:\TTL\jira-prd`/RAG 默认关闭；Rovo 暂不可用时，Review 继续使用 Jira REST 与 GitLab 证据。

### 4.4 Healthy status

登录页和主页均显示带文本的健康状态：

- `Healthy`：核心配置、数据、报告和工作流存储可用；
- `Degraded`：核心服务可用，但 GitLab 或 Review Provider 等可选外部检查异常；
- `Unavailable`：至少一个核心组件不可用。

点击状态可查看检查时间和组件结果。登录前只返回脱敏摘要，不显示内网地址、文件路径或凭据；健康检查异常不会阻止用户登录。

全新安装且尚无 `web_users.json` 时，运维人员必须通过 `WEB_BOOTSTRAP_ADMIN_PASSWORD` 提供符合策略的初始 Manager 密码，可用 `WEB_BOOTSTRAP_ADMIN_USERNAME` 修改默认用户名 `admin`。系统不再生成 `initial_credentials_*.txt` 明文凭据文件。已有用户库会原样兼容迁移，后续账户统一从 `Users` 创建。

生产部署前必须检查旧版本遗留的 `initial_credentials_*.txt`：先通过 User Management 为文件涉及的账户统一轮换密码，确认交付后再由获授权人员安全移除文件。当前 7.2.13 用户管理按单 Web 实例运行；多进程/多副本部署前，应将用户变更、安全审计和幂等记录迁移到同一个数据库事务。

## 5. 页面与入口

### 5.1 Run Review

Auditor 和 Manager 使用 `Run Review` 生成新报告。

可用输入包括：

- Jira Issue，例如 `ECHNL-8888`；
- Sprint 搜索选择，仅 Manager 可用；聚焦时显示已保留的近期 Sprint，格式为 `名称 (ID)`；
- Jira Filter ID，仅 Manager 可用；
- Report Priority。

对已有报告的单 Issue 再次 Review 时，系统会先检查是否存在可复用报告。Progress 会依次显示报告检查、freshness 判断和等待决策；明确选择 Re-scan 后才会创建新的 Review Job。选择 Use Existing、Cancel 或检查失败不会留下虚假的运行中 Job。

Developer 看不到 Run Review 操作。

运行 Sprint Review 前，系统会实时向 Jira 验证 Sprint 是否存在且当前账户可访问，并检查 Issue 状态：

- 仍有 Issue 不是 `Development Done`：确认继续后，本次标记为 `Batch Issue Preview`；
- 全部 Issue 都是 `Development Done`：本次标记为 `Final Sprint Review`，用于 release readiness；
- Final Sprint Review 完成后，GIT_VERSION 仍作为独立 Web Release Gate Job 执行，不会混入普通 Issue Prompt。

Progress 默认跟随最新消息。用户拖动滚动条或滚轮离开底部后会暂停自动跟随，可点击 `Jump to latest` 恢复；多个 Job 可使用 `Maximize` / `Restore`，等待执行锁的 Job 仍可暂停或停止。

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
- GIT_VERSION 是发布级 MR，不会并入普通 Jira/Sprint Review。Manager 在 Web Release Gate 工作区输入明确的 MR URL，或从已完成的 Sprint Job 使用 `Continue to Release Gate`；CLI 只作为运维备用入口。
- 目标分支属于配置的 development-version 分支时，该 MR 会记录为 Dev-branch skipped，避免将开发版本合并误当作正式交付审核。

Sprint 关闭本身不会删除 Issue，也不会阻止 Jira Remote Links 或 Development Panel 查询。系统仍可通过 Sprint ID/名称加载已关闭 Sprint 的 Issue。常见的“关闭 Sprint 后找不到 MR”实际由以下条件造成：

1. Issue 状态已从 `Development Done` 转为其他状态，因状态门禁在发现前被跳过；
2. MR 状态为 `closed`，不在默认的 `opened, merged` 范围；
3. MR 所属项目不在配置的 GitLab 项目范围；
4. MR 属于 Company Config、SCR、GIT_VERSION 或 development-version 分支，被路由到 Skipped/Release Gate，而非普通问题报告；
5. Jira/GitLab Token 无权读取 Remote Links、Development Panel、目标项目或 MR。

任务详情中的 `Discovered`、`State skipped`、`Branch-type skipped`、`Dev-branch skipped`、`Issues without MRs` 和 `Errors` 应结合查看；“发现但被跳过”和“完全没有发现”是不同结果。

#### 5.1.2 同一 Issue 的应用级问题清单

CodeReviewer 将应用作为报告和处理闭环的主要拆分边界，将 `frontend/backend` 作为技术类型元数据。原因是 iTrade Client、Services Terminal 和 WVAdmin 都是 frontend，但各自拥有独立源码仓库、构建仓库和 Release Gate。iTrade Client 的 `7.5` 主版本包含 `7.5.0.x`、`7.5.1.x` 两个并行版本线；DPS 是 backend，并由 DPS9、DPS11 两条主版本线组成。

> 7.2.13 起，报告、LLM 输入、Finding、Handling、Review Snapshot 与 Release Readiness 已统一按 `Application + Release Line` 隔离。`frontend/backend` 只保留为审核规则元数据，不能用于选择 GIT_VERSION MR。

一个 Jira Issue 涉及多个应用时，系统应先执行端到端影响审核，再按应用过滤 Changed Files 和 Findings。一个应用内的多个源码 MR 合并到同一应用报告；不同应用分别生成报告、Finding、Handling 和审核快照。

应用级报告名例如：

```text
ECHNL-8888_WVAdmin_has-issue-high.md
ECHNL-8888_iTrade-Client-7.5.0_has-issue-high.md
ECHNL-8888_iTrade-Client-7.5.1_has-issue-medium.md
ECHNL-8888_Services-Terminal_has-issue-medium.md
ECHNL-8888_DPS9_has-issue-critical.md
ECHNL-8888_DPS11_has-issue-high.md
```

报告 Basic Information 仍显示 `Application`、`Project Type`、源码 repositories、MRs 和结构化 `Responsible Scope`。`type` 用于选择前后端审核规则，但不能作为唯一报告拆分键。无法映射应用的项目标记为 `Unmapped`，不得通过猜测进入最终 Release Gate。

应用、源码仓库、构建仓库及 Release Gate 的完整映射见 [CodeReviewer 构建仓库与应用边界知识](CodeReviewer-Build-Repository-Knowledge.zh-CN.md)。

#### 5.1.3 Web Release Gate

Manager 可以在 `Run Review` 页面完成 Sprint Review 到 GIT_VERSION Release Gate 的完整线上流程，不需要在正常业务操作中手动运行 `review.py`。

操作流程：

1. 在 Sprint 字段选择或输入 Sprint，运行 Sprint Review。
2. 在线查看各应用报告、问题状态和 deferred Company Config/SCR 数量。
3. 如果系统发现 GIT_VERSION MR，在完成的 Sprint Job 点击 `Continue to Release Gate`。
4. 如果未自动发现，在 `GIT_VERSION Release Gate` 卡片输入完整 MR URL。
5. 点击 `Run Release Gate`。
6. 在同一个 Progress 区域查看 MR 加载、锁定仓库预检、LLM 分析和报告保存进度。
7. 查看 Job 卡片中的 Gate status、Locked sources、Build resources/blockers。
8. 在线打开最终报告；BLOCKED 时完成整改后点击 Retry 或重新运行 Release Gate。

Web Release Gate 只对 Manager 显示，API 也会执行相同权限检查。输入必须为包含版本化 `git_version.yml`/`build.yml` 的 GIT_VERSION MR；普通代码、Company Config 或 SCR MR 会在 LLM 执行前被拒绝。

7.2.2 起，MR URL 输入框初始化为单行高度；URL 在当前宽度放不下时自动换行并增长到第二行，不显示垂直滚动条。复制时产生的换行和首尾空白会被自动清理，但 URL 仍必须保持完整的 GitLab MR 结构：

```text
https://gitlab.tx-tech.com/<group>/<project>/-/merge_requests/<IID>
```

Release Gate 一次只处理一个应用的 GIT_VERSION MR。WVAdmin、iTrade Client、Services Terminal 和 DPS 应分别使用其构建项目中的 GIT_VERSION MR；同一 Sprint 发布多个应用时，需要逐个应用运行 Release Gate。

`config.yml #build-repository` 是应用构建项目的权威映射：

| 应用/版本线 | 类型 | GIT_VERSION MR 所在构建项目 |
| --- | --- | --- |
| iTrade Client 7.5.0.x | frontend | `build-repository.itrade-client`；目标/版本分支匹配 `7.5.0.*` |
| iTrade Client 7.5.1.x | frontend | `build-repository.itrade-client`；目标/版本分支匹配 `7.5.1.*` |
| Services Terminal | frontend | `build-repository.services-terminal` |
| WVAdmin | frontend | `build-repository.wvadmin` |
| DPS9 / DPS11 | backend | `build-repository.dps`；由 `9.3.*` / `11.2.*` 版本线区分 |

iTrade Client、Services Terminal、WVAdmin 虽然同为 frontend，但不能共用一个 GIT_VERSION MR。iTrade Client 7.5.0.x 与 7.5.1.x 虽共享应用构建仓库和 7.5 主版本，也必须分别选择与版本线匹配的 GIT_VERSION MR。DPS9、DPS11 共享 DPS 构建仓库，但 Release Gate 必须核对 MR 的版本分支和锁定源码与目标主版本一致。

如果 Sprint handoff 发现两个或以上的 GIT_VERSION MR，页面会显示 `Detected application / GIT_VERSION MR` 下拉框。候选项同时展示 GitLab Project 和 Source Branch；选择候选后会自动回填 URL。

状态含义：

| 状态 | 含义 | 后续操作 |
| --- | --- | --- |
| READY | 确定性锁定/资源检查无错误，最终分析也没有配置范围内的 Critical/High 阻碍 | Manager 阅读报告并完成发布决策 |
| BLOCKED | 锁定仓库、构建资源、SCR/数据库资源、LLM 阻碍级 Finding 或其他门禁不满足 | 整改并在 Web 重跑 |
| FAILED | URL/类型错误、认证、网络或运行异常 | 修正输入或环境后 Retry |

Release Gate 会读取 GIT_VERSION MR 的 `git_version.yml`、`build.yml`，拉取锁定的源码与构建仓库 commit，对比前后版本锁，并检查 Company Config、DPSBuild、DBChangeParser、`db_change.yml`、`db_change.scr` 及其引用资源。报告中的 Release Gate 区域是最终证据来源。

CLI 命令仍可用于自动化或故障排查：

```powershell
python review.py --mr-url https://gitlab.tx-tech.com/.../-/merge_requests/<GIT_VERSION_MR_ID>
```

但 CLI 不再是正常 Web 发布流程的必需步骤。

#### 5.1.4 跨 Sprint 再处理与增量边界

同一个 Jira Issue 再次回到 Backlog 并加入新 Sprint 时，系统保留旧 Complete Sprint，同时为新 Sprint 建立新的 Review Cycle。Issue History 仍是一条主记录，下面按 `Sprint → Cycle → Run Group → Application Run` 追溯；`frontend/backend` 保留为每个应用 Run 的技术元数据。

本轮审核以 `GitLab project + MR IID + head SHA` 标识 MR revision：上一 Cycle 已审核且 Head SHA 未变化的 MR 不应重复进入本轮；同一 MR 有新 Head SHA 时按新 revision 处理。LLM 输入明确分为：

- `Current Review Scope`：本轮正式模板 Comment 与本轮 `base_sha → head_sha` 增量，是唯一审核对象；
- `Current Target Context`：目标分支最新相关代码，只用于影响分析；
- `Historical Requirement Context`：原 Description 与以前正式模板 Comments，不包含以前 Cycle 的 MR diff。

Company Config/SCR 记录为 Deferred Release Resources；只有这两类 MR 时显示“无代码变更、Release Gate Pending”，不会误报 Review Failed。

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

每个严重级别卡片同时显示已处理/未处理比值、百分比进度和视觉状态；数字可跳转到 Problems 中该级别的第一项。Issue 详情使用 Overview、Problems、Discussion、History & Snapshots、Pending Jira 组织完整闭环。

一级 `Overview` 按 Jira Issue 所属 Sprint 分组，并在每个 Sprint 内按 WVAdmin、iTrade Client、Services Terminal、DPS 展示应用级 Release Readiness：

- 百分比 = 当前 Review Cycle 中该应用 `Review Pass` 的唯一 Issue 数 ÷ 该应用关联的唯一 Issue 总数；
- 同时显示 Reports、Without report、Generating、Handling、Ready for Pass、Review Pass、Failed、Remaining；
- `0/0` 显示 `N/A`，不会误显示为可发布；
- 无法可靠归属的历史记录进入 `Unmapped` 并保持阻断；
- 点击应用卡进入 `Issues` Tab，并自动带入 Sprint 与应用范围。

桌面宽度下应用卡片每行显示 3 张，并使用与其他工作台一致的正文和数字字号；较窄窗口自动降为两列或一列。

同一个 Issue 再加入新 Sprint 时会形成新的 Review Cycle。Overview 以 `(Sprint / Cycle, Application, Jira Key)` 隔离统计，旧 Sprint 的 Pending 报告不会污染新 Cycle；History & Snapshots 仍可关联查看该 Issue 的所有处理轮次。

Problems 在标题下分别显示“问题详情”和“处理建议”，每项最多两行；点击 `更多` 展示两项完整内容，点击 `收起` 恢复摘要。历史 Workflow Finding 若尚未保存这两个字段，页面会从对应 Markdown 报告按 Finding 编号补齐。`Architecture / No specific file` 表示 Finding 不绑定单一文件；`New in this run`、`Still present since Run N`、`Resolved after re-scan` 表示跨 Run lineage，不是错误信息。

点击一行进入 Issue Review 详情。

### 5.5 Pending Jira

点击页面顶部的 `Pending Jira` 查看“不是阻碍，另报 Jira”产生的草稿。

草稿只表示“待创建”，7.0.0 不会自动在 Jira 中创建真实 Issue。后续版本将通过 JiraReviewer，由 Manager 创建真实 Jira Issue。

### 5.6 Review Coverage

Auditor 和 Manager 可以使用 Review Coverage 检查指定 Jira、Sprint 或 Filter 范围。

点击主页的 `Sprint Overview` 只打开工作区，不会把主页 `Jira` 字段回填到 `Jira issues`。Sprint 和 Jira Filter 可继续作为便捷范围带入。结果使用两个 Tab：

- `Overview`：报告覆盖、生成报告生命周期和按应用 Release Readiness；
- `Sprint issues`：逐个 Issue 的 Summary、Responsible、应用、Cycle/Run、Handling 和操作。

Coverage 用于识别：

- 尚未生成报告；
- 正在生成；
- 生成失败；
- 已生成但仍在 Handling；
- 等待 Re-scan；
- 已具备 Pass 条件；
- 已 Review Pass。

Auditor 的 Coverage 结果按 responsible 过滤，Manager 查看全局结果。

点击 `Scan` 后，弹窗会显示当前活动阶段，包括 Jira 范围发现、GitLab MR 匹配、Review Job/报告/Handling 核对及 Overview 汇总。该反馈表示系统仍在处理，不代表虚构的完成百分比。

Coverage Scan 使用后台 Job 执行。点击 `Scan` 后页面会展示 Jira Issue 加载、MR Discovery、报告匹配、Handling/Review Cycle 和应用准备度计算等真实阶段；关闭 Sprint Overview 不会停止任务，重新打开或刷新页面后会恢复任务进度或显示完成结果。

前 30 秒显示当前阶段、完成百分比和累计用时。超过 30 秒后切换为长时间扫描提示，并显示到 180 秒预期阈值的超时倒计时；达到阈值只表示扫描耗时超出预期，后台任务仍会继续，不会因为浏览器超时而丢失结果。同一用户重复扫描同一个 Jira、Sprint 或 Filter 范围时，系统会复用正在运行的 Job；刚完成的结果也会短期复用，避免重复访问 Jira/GitLab。

当 Issue 尚未生成报告时，卡片右上角直接显示 `Run Review`，不再重复显示 `No report` 状态徽标：

1. 具有 Run Review 权限的用户点击该行的 `Run Review`。
2. Sprint Overview 自动关闭并返回主页 Progress；系统清空 Sprint/Filter 范围，只以该卡片 Jira Key 启动单 Issue Review。
3. 如果检测到仍然新鲜的报告，系统继续执行既有的复用/重新扫描确认。
4. 新 Job 的完整状态显示在主页 Progress。再次打开 Sprint Overview 并刷新后，卡片会根据实际状态显示 `Generating` 或新报告结果。

Manager 可在 `Issues without reports` 卡片点击 `Run remaining`，一次启动当前扫描范围内全部缺少报告的 Jira Issues；系统仍按 Issue 执行发现与报告切分，不会把单卡片操作误扩展为整个 Sprint Review。

顶部报告覆盖区域按 Jira Issue 去重：

- `Issues with reports`：至少有一份报告的 Issue 数量；同一 Issue 即使存在多个 Run/历史报告也只计一次。
- `Issues without reports`：当前尚无任何报告的 Issue 数量。
- 进度条展示两类 Issue 在当前 Scan 范围内的占比。

普通 Jira/Sprint 报告的 `Jira involved file list does not match MR diff` 只比较本轮普通代码 MR。已识别为 Company Config/SCR 的配置或数据库文件不会出现在该 Problem 的差异表中；这些文件继续保存在 Deferred Release Resources，并由相应应用的 GIT_VERSION Release Gate 使用锁定构建 commit 校验。

已生成报告的 Issues 再按最新报告及 Review Cycle 状态分为：

- `Handling`：仍有 Finding 等待处理或确认。
- `Ready for Pass`：当前问题已经处理完毕，等待 Auditor/Manager Pass。
- `Review Pass`：当前 Review Cycle 已经手动通过。

`Generating` 和 `Failed` 是报告生成过程状态，不纳入上述三个“已生成报告生命周期”分类。

每张 Issue 卡片同时显示当前 `Cycle`、最新 `Run`、所属 Sprint 和 Snapshot 数量；没有持久化 Review Cycle 时显示 `No Review Cycle yet`。

#### 5.6.1 按应用查看 Release Readiness

Sprint Overview 的 `Application release readiness` 会根据本次扫描发现的 Issue/MR 项目归属，分别显示 WVAdmin、iTrade Client、Services Terminal、DPS 的准备进度。应用归属来自 `config.yml` 的 GitLab project、group 和 module 配置，不根据 Issue Summary 猜测。

每张应用卡片包含：

- Release Readiness 百分比：`Review Pass 的 Issue 数 / 当前应用涉及的全部 Issue 数`；
- Report coverage：已有至少一份报告的 Issue 数 / 当前应用全部 Issue 数；
- `Without report`：尚无报告，需要 Run Review；
- `Generating`：Review Job 正在运行；
- `Handling`：已有报告，但 Finding 尚未处理完成；
- `Ready`：问题已处理完，等待 Auditor/Manager 执行 Pass；
- `Pass`：当前 Review Cycle 已手动通过；
- `Failed`：报告生成失败，需要排查或重试；
- Remaining：进入 GIT_VERSION Release Gate 前仍需完成的 Issue 数。

一个 Issue 同时修改多个应用时，会在每个相关应用中各计一次，但不会在同一应用内因多个 MR 重复计数。无法由配置可靠映射的 Issue/MR 会进入 `Unmapped`，并显示 `Project mapping required`，不能被当作已满足发布条件。

只有应用卡片达到 100% 且显示 `Ready for Release Gate`，才表示该应用可以继续运行 GIT_VERSION Release Gate。100% 不等于自动允许合并；Release Gate 仍需验证 GIT_VERSION 锁定源码、Company Config、SCR 和其他发布资源。多个应用必须分别使用各自的 GIT_VERSION MR 完成门禁。

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

## 11. Discuss 与 AI Assist

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

### 11.2 AI Assist

Auditor/Manager 可使用 AI Assist：

- 总结报告中的 Critical/High；
- 查询还有哪些问题未处理；
- 理解 Finding 的风险；
- 整理后续跟进项；
- 汇总后续跟进重点。

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

页面会显示当前使用的编辑引擎：

- `Atlaskit enhanced`：本机已经构建并加载可用的 Atlaskit Editor island；
- `Built-in ADF editor`：使用 CodeReviewer 内置的结构化 block editor；
- `Validated ADF preview`：通过服务端 ADF 校验和渲染进行预览。

Atlaskit 是可选的渐进增强，不代表把 Jira 编辑器或 Forge 组件直接嵌入
CodeReviewer。无论使用哪一种编辑界面，保存和后续 Jira 集成所使用的契约
都是经过校验的 ADF 文档。

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
3. 使用 Discuss 或 AI Assist 理解风险及剩余问题。
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
- Discussion 和 AI Assist 不等同于正式 Handling。
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
- 可以从 Sprint Job 进入 Web Release Gate，也可以直接输入 GIT_VERSION MR。
- 可以查看 READY/BLOCKED、锁定仓库、构建资源和阻碍项，并在线打开最终报告。

### ADF

- Edit 和 Preview 可用。
- Expand 可包含表格、有序列表、无序列表和截图。
- 保存并重新打开后内容保持不变。
- 非法 Nested Expand 会被拒绝。
- 并发版本冲突不会静默覆盖。

## 17. 7.x 版本更新记录

### 7.2.17 — 2026-07-24

- Review 报告会保存当前 Jira Sprint/Cycle、Sprint membership 和交付 Scope 元数据，Report History 同步不会再把本周当前交付静默归入 Legacy。
- 当前交付报告缺少唯一 Cycle 身份时，系统会明确拒绝注册并提示修复 Scope；仅当能够唯一匹配一个 Live Cycle 时才自动归属。
- 生产数据治理会把本周报告、Finding Handling 与 Pass 证据迁移到真实 Cycle；不属于当前 Sprint 的本周报告进入显式 Ad hoc 历史 Cycle。
- 已有报告 Scope 与当前 Scope 一致时保留 Pass；当前 Scope 新增 MR/应用时保留报告和处理证据，但必须补齐新增 Scope 后重新 Pass。

### 7.2.16+b202607241318 — 2026-07-24

- Repository Responsible、Jira Component-driven Delivery Responsible 与 Reviewer 权限独立保存；报告最终拆分不再使用仓库 fallback owner 覆盖 Component 推导结果。
- WVAdmin 构建与 Web 公共仓库由 `wen.yi` 负责，MOMD/Trade Middle Office 由 `hieut.tran` 负责，AOP/LCA 仓库由 `victorcz.xu` 负责；无 Component 驱动时才使用仓库配置兜底。
- `wen.yi` 保留 WVAdmin 与 Services Terminal 的 Web Frontend Reviewer 权限，但 Reviewer 身份不会改变报告负责人。
- 启动扫描并自动折叠 Run Review 输入区时保持浏览器 viewport，不再把页面顶部推出视口。
- Progress 在用户拖动滚动条期间无限暂停自动跟随；释放后等待完整 60 秒才恢复。滚轮、键盘与触摸操作同样触发静默期，`Jump latest` 可主动立即恢复。

### 7.2.16+b202607231300 — 2026-07-23

- 部署 7.2.16 的 Cycle 空范围闭环修复：当前 Cycle 没有可审核 MR 时进入 `No Review Required`，不伪装为 Passed，并从 Review Pass、Remaining 与待生成报告分母排除。
- Issue 列表、详情和 Overview 统一读取同一份 Cycle Scope；新 Cycle 或 Scope 实质变化时不再沿用旧 Cycle 的 Run/Pass 状态。
- 本次生产升级按明确授权清除既有报告、Resume 缓存及关联 Workflow 审核数据库；配置、账户与凭据保持不变。清理前创建完整可恢复备份，后续 Scan 将从 Jira 当前 Sprint 与实时 MR 重建 Cycle/Scope。

### 7.2.16 — 2026-07-23

- `7.2.16+b202607231300` 已将后续修订部署到 `192.168.3.78:8765`：当前 Cycle 经权威 Scope 对账确认没有可审核 MR 时进入 `No Review Required`，不生成空报告、不标记 Passed、不开放 Manual Pass，并从 Review Pass、Remaining 和待生成报告分母排除。
- `No Review Required` 可点击 `Check Again`；后续扫描发现新 MR 后会回到 `Awaiting Review`。有 Required Scope 但没有 Run 时显示 `Run Review`，完成当前 Cycle 审核且无阻断后才进入 `Ready for Pass`。
- Issue 列表、详情与 Overview 统一读取同一份 Cycle 数据；新 Cycle或 Required Scope 变化会清除旧 Cycle 遗留的顶层 Run/Pass 引用，单纯 MR 顺序变化不会重置当前进度。
- Sprint Overview 的 Scan 会重新读取 Jira 与 GitLab，并把当前 Sprint 成员、可审核 MR 与精确交付范围同步到 Review Cycle。Issue 移出 Sprint 后旧 Cycle 自动关闭；仅剩关闭 MR 时当前 Scope 变为空，不再提示生成报告。
- 点击 Scan 会强制刷新；恢复最近结果仍可使用短时缓存。若 discovery 达到配置上限，系统只展示结果而不执行持久化对账，避免不完整数据影响历史。
- Issues Review History 顶部可选择 Sprint；进入 Issue 后可选择 Delivery Cycle。所有统计、问题、处理结果和 Pass readiness 都跟随当前选择，不跨 Cycle 混用。
- `Live Cycle` 可执行 Re-scan 与 Manual Pass；`Historical · read-only` 和 `Legacy Cycle` 用于审计浏览，不能修改交付状态。
- Manual Pass 仅检查所选当前 Cycle 的最新逻辑 Run Group，并要求每个 `Application + Release Line + Delivery Version` Scope 都有报告。
- Overview 与 Issue 卡片展示 Cycle 自己的 Run、Finding、Handling 和 Required Scope 数；旧 Run 不再为当前 Cycle 反向制造 Required Scope。
- 同一应用交付范围的 Rescan 会替换该范围的旧报告统计；跨应用的同批报告仍合并为一个逻辑审核。GitLab fallback 只采纳标题或源分支直接包含 Jira Key 的 MR。
- Sprint context 位于 Overview / Issues 标签右侧并默认选择 `Current cycles`；`All sprints & history` 用于审计。Issue 详情将 Jira Key、摘要和主操作分层显示，Delivery Cycle、Live/Historical 标识和 Readiness 放在第二层。
- 当前 Cycle 没有报告时显示 `Awaiting Review`，隐藏无意义的全零严重度指标，并可直接切换到上一 Cycle 查看历史证据。
- 7.2.16 已于 2026-07-23 部署到 `192.168.3.78:8765`。升级不会自动执行 Sprint Scan；部署前的 Legacy Cycle 保留为只读审计证据，扫描当前 Sprint 后系统才会建立对应 Live Cycle 并纳入 `Current cycles`。

### 7.2.15 — 2026-07-22

- Jira Review 以 `Jira Issue × Application + Release Line` 为报告单位；Company Config/SCR 即使没有普通代码 MR，也会形成独立的延后发布范围报告。
- Jira `Components` 决定各应用范围的交付 Responsible；Jira `Responsible` 字段仅保留用于审计展示，不参与 Responsible scope 推断。
- 涉及文件清单仅与当前应用范围比较；其他应用文件会被隔离，延后发布范围中的框架附加文件不会被误报。
- 报告保存到 scope Responsible 目录；运行任务的用户只有在无法确定范围负责人时才作为回退。
- Issues Review History 分开显示 Unique Issues 和 Application Scopes；Issues 详情支持 Jira 外链和完整 Problem 证据。
- Web Frontend Reviewer 按 Application domain 授权，可审核 WVAdmin、Services Terminal，但不会改变报告的 Delivery Responsible。
- iTrade Client Readiness 同时展示 Release Line 与精确交付版本，例如 `7.5.1.38`、`7.5.1.39` 分别统计；Legacy Run 仅在归属唯一时自动兼容。
- Manual Review Pass 要求当前 Cycle 的全部应用交付版本 Scope 均有完成报告，且所有阻断问题满足处理策略。
- Codex Progress 每 15 秒显示运行心跳；300 秒无真实 Provider 活动才会中止，持续活动任务最多运行 900 秒。
- Issues Review History 展开 Problem 后，“完整报告证据”按安全 Markdown 渲染；涉及文件清单等表格保留表头、行列、代码样式与安全链接，宽表格可在证据区域内滚动。
- Sprint Overview 在 Scan 运行中或失败时展开范围面板；完成后自动折叠并显示扫描范围、完成状态与 Issue 数。选择 `Expand scan scope` 可重新查看或修改条件。
- 本版本已于 2026-07-23 部署到 `192.168.3.78:8765`；本机与 RHEL9 staging 自动化回归 `284/284` 通过，生产健康检查、12 个账户、Workflow 数据、配置策略及一键回滚入口验证通过。

### 7.2.14 — 2026-07-22

- Sprint Overview 可正确识别下划线分隔的标准报告名，不再把 iTrade 版本标签误识别为 Jira Key。
- `Unique Issue coverage` 按 Jira Key 去重；`Required release-scope reports` 按 `Jira Issue × Application + Release Line` 统计；历史重跑报告文件单独显示，不再混用三个口径。
- 一个 Issue 同时涉及 7.5.0 和 7.5.1 时，需要两个独立 Scope 报告；任一范围缺少报告都会出现在 `Run remaining`。
- 已部署到 `192.168.3.78:8765`；生产健康检查、报告统计、11 个账户完整性和 Workflow SQLite schema v3 验证通过。

### 7.2.13 验收反馈更新 — 2026-07-20

- Sprint Overview 增加 Manager `Run remaining`，单 Issue `Run Review` 会退出弹窗并只创建该 Jira 的 Job；标题、留白和横向溢出同步优化。
- Problems 展示“问题详情/处理建议”两行摘要并兼容补齐历史报告字段；Company Config/SCR 文件从普通 MR 的 Jira 文件清单差异表移除，继续由 Release Gate 校验。
- 登录页及站点顶栏使用 TTL × Jay 结晶品牌标识；公开 Release Notes 已更新旧版本能力随 7.2.13 上线后的当前可用状态。
- 本轮反馈更新只应用到本地 `127.0.0.1:8765`，尚未同步到 `192.168.3.78:8765`。

### 7.2.13 — 2026-07-20

- 同一 Jira Issue 的 MR 在进入 LLM 前按应用与版本线拆分；同一范围内的多个 MR 合并审核，不同应用或版本线不再共用问题清单。
- 支持 WVAdmin、Services Terminal、iTrade Client 7.5.0/7.5.1、DPS9/DPS11 的稳定报告名与 Release Readiness。
- Issues Review History 使用最新逻辑 Run Group 汇总所有应用报告和 chunk；Problem 显示所属 Application Scope。
- Auditor/Developer 的访问范围改为所有 Review Run 的 `responsible_scope` 并集；Manager 生成但由 Auditor 负责的报告仍可查看和跟进。
- Workflow SQLite 升级到 schema v3，旧 Cycle/Run 自动兼容迁移。
- 已部署到 `192.168.3.78:8765`；生产健康检查、10 个账户完整性、历史工作流数据及 SQLite schema v3 验证通过。一键回滚入口为 `/usr/local/sbin/codereviewer-rollback-latest`。
- 生产 `config.yml` 的源码与构建仓库使用版本线通配分支：WVAdmin `1.0.*`、Services Terminal `5.0.*`、iTrade Client `7.5.0.*`/`7.5.1.*`、DPS `9.3.*`/`11.2.*`。`dev_branch` 仍使用明确的长期开发分支名。

### 7.2.12 — 2026-07-19

- Sprint Overview 拆分为 `Overview` / `Sprint issues`，打开弹窗时不再从主页回填 Jira Issues。
- Issue Review Overview 提升卡片字号，并将应用卡调整为桌面每行 3 张。
- Problems 分别展示“问题”和“建议”两行摘要，使用 `更多` / `收起` 查看完整证据。
- Review Communication 使用 XL 弹窗和更均衡的三栏、Reply/Follow-up 编排。
- User Management 扩大账户列表区域、提升文字可读性，并默认打开首条账户。
- Configuration 将 LLM 统一按缩写大写展示；全站弹窗采用 S/M/L/XL/Full 尺寸规范。
- 当前只更新本地 `127.0.0.1:8765`，未部署或重启 `192.168.3.78:8765`。

### 7.2.11 — 2026-07-19

- Run Review 在 Progress 中展示已有报告 preflight，并完善确认弹窗键盘与焦点管理。
- Release Notes 消除系统 tzdata 依赖，增加自动重试、错误恢复和真实 HTTP 验证。
- User Management、Configuration、Sprint Overview、Reply 及报告 Tab 统一卡片、间距和响应式层级。
- Problems 显示 Problem/Suggestion 两行摘要；Company Config 与 SCR 文件来源分别展示。
- 分支支持精确值与版本通配符；Rovo 作为只读知识检索，Jira REST 保持权威且本地 jira-prd/RAG 默认关闭。
- 当前只更新本地 `127.0.0.1:8765`，生产环境仍为 7.2.0。

### 7.2.10 — 2026-07-19

- Sprint Overview Coverage 改为可恢复的后台 Job，不再被浏览器 60 秒等待上限中断。
- 展示 Jira/MR Discovery、报告、Handling/Review Cycle 和应用准备度的真实进度。
- 超过 30 秒后显示长任务氛围和超时倒计时；关闭弹窗后继续扫描，重新打开自动恢复。
- 同一范围复用运行中任务和近期结果，减少重复访问 Jira/GitLab。
- 当前只更新本地 `127.0.0.1:8765`，生产环境仍为 7.2.0。

### 7.2.9 — 2026-07-18

- 增加安全的在线 Configuration、健康状态和配置备份恢复。
- 完善 User Management、Responsible scope、必填标记、Review Communication 和报告排版。
- Issues Review History Overview 按当前 Sprint/Review Cycle 和应用展示 Release Readiness，支持应用下钻和 Unmapped 阻断。
- 当前只更新本地 `127.0.0.1:8765`，生产环境仍为 7.2.0。

### 7.2.8 — 2026-07-18

- Sprint Overview 增加 WVAdmin、iTrade Client、Services Terminal、DPS 的应用级 Release Readiness。
- 进度按已 Review Pass 的 Issue 比例计算，并展示各应用的无报告、生成中、处理中、待 Pass、已 Pass、失败及剩余数量。
- 跨应用 Issue 分别计入相关应用；未映射项目保持发布阻断。
- Issue 卡片增加应用标签；达到 100% 后仍需执行该应用的 GIT_VERSION Release Gate。
- 当前只更新本地 `127.0.0.1:8765`，生产环境仍为 7.2.0。

### 7.2.7 — 2026-07-18

- Sprint Overview 对比有报告/无报告的 Issue 数量及占比。
- 已生成报告的 Issues 细分为 Handling、Ready for Pass、Review Pass。
- Issue 卡片补充 Review Cycle、Latest Run、Sprint 和 Snapshot 信息。
- 数量按 Jira Issue 去重，同一 Issue 的多个历史报告不会重复计数。
- 当前只更新本地 `127.0.0.1:8765`，生产环境仍为 7.2.0。

### 7.2.6 — 2026-07-18

- Coverage Scan 增加 60 秒超时和网络恢复后的重新扫描能力。
- `Issues without report` 明确统计缺少报告的 Issue 数量。
- Jira 状态显示在卡片右上操作下方；Run Review 后保持 Sprint Overview 打开并刷新为 Generating。
- Progress 手动滚动后暂停自动跟随，静置 60 秒恢复；Jump latest 可立即恢复。
- 当前只更新本地 `127.0.0.1:8765`，生产环境仍为 7.2.0。

### 7.2.5 — 2026-07-17

- Sprint Overview 的 Issue 结果使用宽屏两列、窄屏一列的响应式卡片网格。
- 卡片集中展示 Summary、Responsible、MR/Report/Handling 指标和 Jira 状态。
- 未生成报告时以 `Run Review` 直接替代卡片上的 `No report` 徽标；顶部 No report 数量继续用于整体统计。
- 当前只更新本地 `127.0.0.1:8765`，生产环境仍为 7.2.0。

### 7.2.4 — 2026-07-17

- Sprint Overview 使用更宽的工作台式弹窗和紧凑卡片布局。
- Scan 期间显示 Jira 发现、MR 匹配、Review 活动核对及汇总阶段，不使用虚假的完成百分比。
- Status 为 `No report` 时，具有 Run Review 权限的用户可直接在该行启动单 Issue Review；任务随后显示在主页面 Progress 区域。
- 当前只更新本地 `127.0.0.1:8765`，生产环境仍为 7.2.0。

### 7.2.3 — 2026-07-17

- `Run Release Gate` 位于 Release Gate 卡片右下角；URL 字段使用完整可用宽度。
- 校验或运行状态显示在按钮左侧，窄屏时按钮自动切换为全宽。
- 当前只更新本地 `127.0.0.1:8765`，生产环境仍为 7.2.0。

### 7.2.2 — 2026-07-17

- GIT_VERSION MR URL 输入框使用单行初始高度，内容溢出后自动增长到两行。
- 输入框不显示垂直滚动条；输入、候选回填和窗口变化时自动重新计算高度。
- 当前只更新本地 `127.0.0.1:8765`，生产环境仍为 7.2.0。

### 7.2.1 — 2026-07-17

- Release Gate MR URL 支持两行显示，并统一前后端规范化与结构校验。
- Review Progress 使用可访问的 Jump latest、Maximize/Restore 图标按钮。
- Sprint Overview 的范围说明、筛选字段和 Scan 操作按响应式工具栏重新编排。
- 当前只更新本地 `127.0.0.1:8765`，生产环境仍为 7.2.0。

### 7.2.0 — 2026-07-17

- 新增 Manager-only 用户管理：用户搜索/筛选、创建、角色和 responsible 范围维护、启用/停用及密码重置。
- 临时密码仅在创建或重置成功后显示；列表与接口不返回密码哈希。所有用户可从顶部入口修改自己的密码。
- 用户数据采用进程内及跨进程锁、原子替换与持久化同步，并使用乐观版本校验；角色变更、停用和密码重置立即撤销旧会话，并记录脱敏安全审计。
- 全新用户库使用环境变量安全引导首个 Manager，不再生成明文初始凭据文件。
- 增加自我降级/停用、受保护 root、Active Manager 最小数量、用户名及密码策略等服务端保护。
- 本版本已完成本机验收，并于 2026-07-17 部署到 `192.168.3.78:8765`；RHEL9 自动化测试 `182/182` 通过，线上 10 个账户凭据完整性保持不变。

### 7.1.7 — 2026-07-17

- 登录页 Username、Password、Robot Check 的红色必填星号统一显示在字段名称右侧，不再因 Label 的 Grid 布局掉到独立一行。
- Robot Check 的字段名、必填标记、冒号和题目保持为一个可换行的提示组，Reset 按钮继续独立对齐。
- 本版本仅用于本机 `127.0.0.1:8765` 验收，未部署或重启 `192.168.3.78:8765`。

### 7.1.6 — 2026-07-17

- Review Communication 的复制操作改为固定尺寸 copy icon，增加 hover、按压、成功/失败状态及读屏反馈，复制过程中不再发生按钮宽度跳动。
- Reply 标题使用 information icon 承载完整处理说明，页面移除独立“处理说明”区块；History 与 Follow-up Draft 拉伸至三栏底部。
- Reply 和 Follow-up 文本框允许纵向拖动，操作按钮保持在文本框之后，右栏内容超高时在栏内滚动；按钮简写为 `Reply`、`Follow-up`。
- Release Notes 使用固定安全边距和适中高度的专属弹窗，标题栏常驻、正文独立滚动；支持关闭按钮、背景、Escape 关闭及焦点返回。
- Issues Review History 右侧概览增加 Medium 处理比例卡；Manager exceptions 与 Remaining blockers 合并为摘要卡，阻碍提示移到顶部操作区；指标区按容器宽度自适应为 4/2/1 列。
- Issues Review History 的 Overview 与 Issues 面板显式占用同一内容行，避免 Overview 概览卡被 Issues 列表挤压或裁切。
- 本版本仅用于本机 `127.0.0.1:8765` 验收，未部署或重启 `192.168.3.78:8765`。

### 7.1.5 — 2026-07-17

- Review Communication 的 Follow-up Draft 增加一键复制；Reply 与 Follow-up 输入卡移除重复标题，按右栏可用高度自适应均分，处理说明改用更紧凑的辅助文字字号。
- Report History 的搜索框、日期/Refresh 行、Tab 栏统一宽度和 box model，与报告操作按钮保持同一右边界。
- 代码审查报告“问题列表”中的每个 Critical/High/Medium/Low/Warning 问题支持独立展开和折叠；默认展开，并支持键盘操作。
- 本版本仅用于本机 `127.0.0.1:8765` 验收，未部署或重启 `192.168.3.78:8765`。

### 7.1.4 — 2026-07-17

- GIT_VERSION Release Gate 改为紧凑双栏卡片：左侧显示流程步骤、标题和 Sprint handoff，右侧显示 MR URL 与 Run Release Gate。
- 字段标题和执行按钮位于同一行，URL 输入框在下一行使用完整宽度；information hint 承载详细说明，避免页面重复展示。
- 680px 以下切换为单列，460px 以下执行按钮使用全宽布局。
- 本版本仅用于本机 `127.0.0.1:8765` 验收，未部署或重启 `192.168.3.78:8765`。

### 7.1.3 — 2026-07-17

- Company Config/SCR 发布资源报告命名为 `<Project>-Company Config_has-issue-<level>.md` / `<Project>-SCR_has-issue-<level>.md`；普通 Jira frontend/backend 报告命名保持不变。
- GIT_VERSION Release Gate 按 `config.yml` 中 MR 所属 GitLab project 分为 WVAdmin、iTrade Client、Services Terminal、DPS；未配置项目在进入 LLM 前拒绝。项目门禁仍审核该产品锁定的全部源码和构建依赖。
- 同一项目重复生成相同 Company Config/SCR 状态报告时，首份保留标准文件名，后续报告添加 rescan 时间戳，避免覆盖历史证据。
- `kelvinh.wu` 已调整为普通 Developer，并与 `vincentgr.wang`、`benyq.feng` 一样使用 `kevin.tan` responsible scope；`luckxh.chen` 继续负责 DPS Config 与 MO Client Config。
- Run Review 重整为主操作、Release Gate、Progress 三层结构，并修复 Issues Review History 的 Overview/Issues 面板同时显示问题。
- Review Communication 使用 History、Follow-up Draft、Reply 三栏结构，处理说明横跨左两栏，窄屏自动堆叠。
- Pending Jira 的正式存储继续使用 Jira ADF；如果本机已构建 Atlaskit bundle，则使用 Editor/Renderer 增强体验，否则使用内置 ADF 编辑器并明确显示当前引擎。
- 本版本完整自动化测试 164 项通过，Python/JavaScript 语法及差异检查通过。
- 本版本仅用于本机 `127.0.0.1:8765` 验收，未部署或重启 `192.168.3.78:8765`。

### 7.1.2 — 2026-07-17

- Problems、Issue History Discussion 与 Pending Jira ADF 的必填校验使用统一错误提示、`aria-invalid` 状态和首个错误字段聚焦；Jira 跟进描述不能以空内容或示例文字提交。

- Issues Review History 增加 `Overview` / `Issues` 一级 Tab；Overview 基于持久化 Review Cycle 按 Sprint 分组，展示 Issue、Passed、Blocker、Pending、Snapshot 和 Cycle 汇总，可直接进入该 Sprint 的 Issue 列表。
- 同一 Jira Issue 加入新 Sprint 时保留一条 Issue 主记录并创建新 Review Cycle；旧 Cycle 自动关闭。`History & Snapshots` 按 Sprint 展示 Cycle，并关联 Run 与不可变审核快照。
- Critical/High 卡片继续展示 handled/unhandled、百分比和进度条；Problems 的 Not issue 理由、两行摘要、Submit、可理解的文件/lineage 文案及两列对齐完成交叉验收。
- Sprint 建议只保留最近 31 天；Run Review 在浏览器和服务端分别执行有效性、访问权限、非空和 Development Done 预检。Batch Preview 必须确认，Final Sprint Review 模式写入审核上下文。
- Progress 同一时间只允许一个 Job 最大化，窄屏操作区自动换行；Run、Release Gate、Coverage、Draft、Communication、Approval、Override 和登录入口增加防重复提交保护。
- 本版本完整自动化测试 148 项通过；仅发布到本机，未部署或重启 `192.168.3.78:8765`。

### 7.1.1 — 2026-07-17

- Run Review 和 Release Gate 的辅助说明改为 information icon；点击或键盘触发后显示提示，点击外部或按 `Esc` 关闭。
- Report History 的搜索、时间范围、刷新和视图切换集中在同一工具卡，报告以卡片展示标题、负责人、目录、大小和时间。
- AI Assist 使用对话角色、时间和 Markdown 排版展示回复；Discussion 的 Reply、Follow-up 与处理说明使用一致卡片层级。
- Open Issue Review 顶部将 Issue 身份、状态与操作、级别指标、门禁状态分区展示；窄屏时自动切换为单列。
- Release Notes 弹窗已修复，可以直接查看站点公开版本摘要。
- 本版本通过 7 项独立 UI 验收及 134 项完整自动化测试；仅发布到本机，未部署或重启 `192.168.3.78:8765`。

### 7.1.0 — 2026-07-17

- 增加 Review Cycle、Sprint membership、Run Group、Description/Review Snapshot 与 Deferred revision 持久化；现有 SQLite 数据幂等迁移。
- 跨 Sprint 再处理只审核本轮 MR revision 增量，目标分支和历史需求分别作为只读上下文。
- frontend/backend Run 归属同一逻辑 Run Group；Company Config/SCR 进入 Web Release Gate 待核验清单。
- Problems 增加完成率、摘要展开、统一必填错误、single-flight 和幂等提交；全部 Finding 提交处理后生成不可变快照。
- Sprint 搜索、Jira preflight、Batch Preview/Final Sprint Review、Progress 暂停跟随和多 Job 最大化可用。
- Report Preview 移除正式 Handling 和 Teams Delivery，正式操作统一进入 Issue Review；站点新增精简 Release Notes 弹窗。
- 本版本仅发布到本机 `127.0.0.1:8765`，未部署或重启 `192.168.3.78:8765`。

### 7.0.5 — 2026-07-17

- 增加 Manager 专用的 Web GIT_VERSION Release Gate 工作区，正常发布流程不再依赖手动 CLI。
- Sprint Job 可将发现的 GIT_VERSION MR 交接到 Release Gate，并继承 Sprint 上下文。
- Release Gate 使用 Web Review Job 队列，支持进度、暂停、停止、重试、历史恢复和报告预览。
- Job 卡片展示 READY/BLOCKED、锁定源码仓库、构建资源及阻碍数量。
- 服务端执行 Manager 权限、MR URL 和 GIT_VERSION 资源验证；普通 MR 在 LLM 前被拒绝。

### 7.0.4 — 2026-07-16

- 支持从扁平 Git Tools 配置及 JiraReviewer 嵌套配置继承 repository 的 `type: frontend/backend`。
- 同一 Jira Issue 的报告按 `responsible + type` 拆分；同一负责人同时涉及前后端时，也会生成相互独立的问题清单。
- 拆分报告文件名增加 `_frontend_` / `_backend_`，并在 Basic Information 中显示 `Project Type`。

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
