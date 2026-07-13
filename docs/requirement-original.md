# GitLab MR Code Review 工具 PRD

## 1. 背景

Web 研发部正在进行 SV（Standard Version）项目开发，通常每 2 到 3 天迭代一个 Sprint 版本。开发团队按版本分支协作，分支名使用版本号，例如 `X.Y.Z` 或项目约定的扩展版本号。

需求与开发任务分别维护在 Jira 中：

- `SVREQ`：需求 Jira space，用于编写 PRD/需求 issue。
- `ECHNL`、`CORE`：开发 Jira space，用于编写 action/development issue。
- Sprint 在 `SVREQ` 与 action Jira space 中都会创建，但名称前缀不同。

示例：

- `SVREQ Sprint 1.4.69`
- `SVREQ Sprint 1.4.70`
- `e-Channel Sprint 1.4.69`
- `e-Channel Sprint 1.4.70`

> 日期说明：原草稿中的“今天是 2026 年 6 月 25 日，周三”不准确。2026 年 6 月 25 日是周四；当前日期为 2026 年 6 月 26 日。PRD 中建议使用“示例交付日”而不是“今天”，避免需求文档随时间失效。

## 2. 项目与分支模式

Web 研发部涉及多个 GitLab 项目，示例项目包括：

- `iTrade Client`：示例版本 `7.5.1.38`，其中 `7.5` 为主版本，`1` 为次版本，`38` 为修订版本。
- `WVAdmin`：示例版本 `1.0.82`，其中 `1` 为主版本，`0` 为次版本，`82` 为修订版本。
- `DPS`：示例版本 `11.2.82`，其中 `11` 为主版本，`2` 为次版本，`82` 为修订版本。

开发时以 Jira issue 创建 issue branch，例如 `ECHNL-8888`。

对于 DPS 中台后端，系统按层分工开发，包括：

- `API`
- `DAO`
- `BIZ`
- `CLI`

其中 `API`、`CLI` 属于调用端。不同成员可针对同一个 Jira issue 创建不同层的分支，例如：

- `API#ECHNL-8888`
- `DAO#ECHNL-8888`
- `BIZ#ECHNL-8888`
- `CLI#ECHNL-8888`

## 3. 版本分支类型

系统需要识别两类版本分支：

### 3.1 项目同名分支

示例：`itrade-client`

用途：

- 构建 SV 环境。
- 部署到 SV Server。
- 支持各小组对接与联调。
- 将测试案例和测试结果补充到 Jira issue comment。

### 3.2 迭代版本分支

示例：`7.5.1.38`

用途：

- 构建正式发布版本。
- 作为 Sprint/版本交付基线。

## 4. Review 场景

当 issue branch 合并到项目同名分支时，系统需要自动执行 code review，并生成 Markdown 格式的 review 报告。

示例报告参考：

- `ECHNL-5539.md`

系统应支持基于 Markdown 报告映射到 GitLab 项目的源文件，并能自动加载当前分支相关代码上下文。Review 逻辑可参考成熟代码审查工具的设计，包括变更范围识别、上下文补全、风险分级和可执行建议。

## 5. GitLab 仓库与构建配置

GitLab 项目分为开发代码仓库与构建代码仓库。每次构建版本时，需要提供：

- `git_version.yml`
- `build.yml`
- 其他配置资源

示例配置文件：

### 5.1 iTrade Client

- 开发代码仓库配置：`itrade-client#git_version-v7.5.1.38.yml`
- 构建代码仓库配置：`itrade-client#build-v7.5.1.38.yml`

### 5.2 WVAdmin

- 开发代码仓库配置：`wvadmin#git_version-v1.0.82.8.yml`
- 构建代码仓库配置：`wvadmin#build-v1.0.82.8.yml`

### 5.3 DPS / DrupalServices

- 开发代码仓库配置：`dps11#git_version-v11.2.82.10.yml`
- 构建代码仓库配置：`dps11#build-v11.2.82.10.yml`

## 6. 产品目标

建设一个面向 Web 研发部的 GitLab MR 自动 code review 工具，支持通过可视化界面和命令行脚本两种方式触发 review。

核心目标：

- 基于 GitLab MR 自动获取代码 diff、源文件和分支上下文。
- 基于 Jira issue、Sprint、项目和版本分支建立 review 范围。
- 自动生成 Markdown review report。
- 支持在人工确认后，将 review 结果回写到 GitLab MR comment 或 Jira issue comment。
- 支持 Web 研发部所有项目，并能处理一个 Sprint 下单个或多个 SVREQ 关联的开发 issue。
- 前端使用 Dart / Flutter 技术栈实现，首期交付 Flutter Web，并保留扩展到移动 App 和桌面 App 的能力。

## 7. 用户角色

- 开发人员：提交 MR，查看 review 报告，修复问题。
- Tech Lead / Reviewer：查看 Sprint、项目、MR 和风险汇总，进行复核。
- Release Manager：关注版本分支、构建配置、交付状态和阻塞问题。
- 管理员：维护 GitLab token、项目配置、Jira space、Sprint 映射关系和 review 规则。

## 8. 功能需求

### 8.1 GitLab 接入

系统应支持：

- 通过 `.env` 配置 GitLab URL、认证方式和 token。
- GitLab token 需要具备读取项目、读取 MR、读取 diff、发表评论等权限。
- 按项目、分支、MR ID 或 Jira issue key 查询 MR。
- 获取 MR 的 source branch、target branch、commit、diff、changed files。
- 获取变更文件的完整上下文，支持按需加载同项目其他代码文件。
- 将 review report 或摘要评论回写到 GitLab MR，但回写前必须由用户人工确认。
- 从 GitLab MR overview 中解析 message summary list。每条 message 包含 issue summary、空格和 issue full link，系统需要基于该信息识别 MR 与 Jira issue 的关联关系。

### 8.2 Jira 接入

系统应支持：

- 通过 `.env` 配置 Jira URL、认证方式和 token。
- 配置 `SVREQ`、`ECHNL`、`CORE` 等 Jira space。
- 读取 Sprint、SVREQ issue、action issue。
- 建立 SVREQ issue 与 action issue 的关联关系：SVREQ issue 被 action Jira issue 阻塞；同时 `ECHNL` issue summary 中包含 `#<SVREQ Jira issue key>`，系统应支持基于该规则解析关联。
- 根据 Sprint 查询相关需求和开发 issue。
- 将测试案例、测试结果、review 摘要或报告链接回写到 Jira issue comment，但回写前必须由用户人工确认。

### 8.3 Sprint 与项目视图

系统应提供统一视图，用于查看：

- Sprint 列表。
- Sprint 关联的 SVREQ issue。
- SVREQ 关联的 action issue。
- 每个 action issue 对应的 GitLab 项目、分支、MR。
- 每个 MR 的 review 状态、风险等级和阻塞问题。
- 项目版本分支与同名分支的合并状态。

### 8.4 Code Review 执行

系统应支持以下触发方式：

- 在前端界面中选择项目、Sprint、SVREQ、action issue 或 MR 后手动触发。
- 通过命令行脚本传入 MR URL、项目、分支或 issue key 后快速触发。
- 可选支持 GitLab webhook，在 MR 创建、更新或合并请求时自动触发。

Review 内容至少包括：

- 代码缺陷。
- 逻辑风险。
- 安全风险。
- 性能风险。
- 兼容性风险。
- 代码风格与可维护性问题。
- 缺失测试或测试覆盖不足。
- 与需求/Jira issue 不一致的实现点。
- 对 DPS 分层结构中 `API`、`DAO`、`BIZ`、`CLI` 变更影响的识别。

Review engine 需要支持可配置的 LLM 能力。系统应支持接入 Codex、Claude Code Opus、DeepSeek V4 Pro 等模型，并允许通过配置切换不同 LLM provider。LLM API key 由外部工具或配置管理，例如通过 CC Claude Switch 管理多组 LLM API key，系统不得将 key 明文写入代码仓库。

### 8.5 Review 报告

系统应生成 Markdown 格式报告，文件命名建议：

- `{JIRA_ISSUE_KEY}.md`
- `{PROJECT}_{MR_ID}_{JIRA_ISSUE_KEY}.md`

报告内容建议包括：

- 基本信息：项目、MR、source branch、target branch、commit、review 时间。
- Jira 信息：SVREQ issue、action issue、Sprint。
- 变更摘要：文件列表、主要模块、变更类型。
- Review 结论：通过、需修改、阻塞。
- 问题列表：按严重等级排序。
- 每个问题包含：文件、行号、问题描述、影响、建议修改方式。
- 测试建议：建议补充的单元测试、集成测试、回归测试。
- 风险摘要：发布风险、联调风险、兼容性风险。

Report 需要支持长期存储。长期存储方案使用 GitNexus，系统需要支持将 Markdown report、review 元数据、MR/Jira 关联信息和执行记录保存到 GitNexus，并支持后续查询和追溯。

### 8.6 配置管理

系统应支持维护：

- GitLab 项目列表。
- 开发代码仓库配置。
- 构建代码仓库配置。
- `git_version.yml` 与 `build.yml` 映射关系。
- Jira space 与 Sprint 前缀映射。
- 项目分支命名规则。
- Review 规则和忽略规则。
- `.env` 配置文件，用于维护 GitLab、Jira、LLM 等外部服务的访问地址、认证方式和 token。
- token 与敏感配置不得明文提交到代码仓库，运行时应通过环境变量、`.env` 或外部密钥管理工具注入。
- LLM provider 配置，包括 Codex、Claude Code Opus、DeepSeek V4 Pro 等可选引擎，以及默认 provider、模型名称、超时和重试策略。
- GitNexus 存储配置，包括 report 存储位置、索引字段和查询方式。

## 9. 前端需求：Dart / Flutter

前端使用 Dart 技术栈，建议采用 Flutter 实现。首期交付 Flutter Web，并保留后续统一适配以下平台的能力：

- Web 应用。
- iOS / Android 移动 App。
- Windows / macOS / Linux 桌面 App。

前端应采用响应式布局和平台适配设计。首期重点满足 Web 端研发团队工作台场景：

- Web 端：首期交付，适合研发团队在浏览器中查看 Sprint、MR、报告和项目配置。
- 移动端：后续扩展，适合查看 review 状态、风险摘要和通知，不要求承担复杂配置操作。
- 桌面端：后续扩展，适合 Tech Lead 或 Release Manager 进行批量 review、报告管理和本地文件操作。

核心页面：

- 登录与连接配置页。
- 项目列表页。
- Sprint 工作台。
- SVREQ / action issue 关联视图。
- MR 列表与详情页。
- Review 执行页。
- Markdown report 预览页。
- Review 历史记录页。
- 系统配置页。

前端能力要求：

- 支持 Markdown 渲染。
- 支持代码 diff 展示。
- 支持文件路径、行号、问题等级筛选。
- 支持 report 下载。
- 支持将 report 或摘要提交到 GitLab/Jira。
- 支持不同屏幕尺寸自适应。
- 支持深浅色主题。

## 10. 后端与脚本需求

### 10.1 Python 后端服务

后端建议使用 Python 实现，负责：

- GitLab API 集成。
- Jira API 集成。
- MR diff 与源文件上下文加载。
- Review 任务编排。
- LLM review engine 调用与 provider 切换。
- Review 报告生成。
- Report 存储和查询，长期存储对接 GitNexus。
- 前端 API 服务。
- webhook 接收与任务触发。

可选框架：

- FastAPI。
- Flask。

### 10.2 Python 命令行脚本

命令行脚本用于快速输出 code review report，格式为 Markdown。

示例能力：

```bash
python review.py --mr-url <gitlab_mr_url>
python review.py --project dps11 --source-branch ECHNL-8888 --target-branch dps11
python review.py --jira ECHNL-8888 --output reports/ECHNL-8888.md
```

脚本输出：

- 终端摘要。
- Markdown 报告文件。
- 可选：将 review 摘要提交到 GitLab MR 或 Jira issue；提交前必须提示用户确认。

## 11. 非功能需求

- 安全性：token、密码等敏感信息不得明文写入代码仓库。
- 可配置性：项目、分支、Jira space、Sprint 前缀、review 规则、LLM provider、GitNexus 存储参数可配置。
- 可扩展性：后续可扩展更多 GitLab 项目、更多 Jira space、更多 review 规则。
- 可追溯性：每次 review 需要保存输入参数、commit、报告、执行时间、执行人、LLM provider 和 GitLab/Jira 回写记录。
- 性能：单个 MR review 应在可接受时间内完成，大型 MR 支持异步任务和进度查询。
- 可维护性：核心逻辑需要模块化，GitLab、Jira、review engine、LLM provider、GitNexus storage、report generator 分层清晰。
- 审批控制：所有写回 GitLab/Jira 的操作必须先展示待提交内容，并由用户明确确认后执行。

## 12. 验收标准

- 能配置至少一个 GitLab 项目和一个 Jira space。
- 能通过 `.env` 配置 GitLab、Jira、LLM provider 和 GitNexus 相关参数。
- 能通过 MR URL 拉取 MR diff 和变更文件。
- 能通过 Jira issue key 找到相关 MR 或分支。
- 能根据 action issue blocked SVREQ 关系，以及 `ECHNL` issue summary 中的 `#<SVREQ Jira issue key>` 解析 SVREQ 与 action issue 关联。
- 能从 GitLab MR overview 的 message summary list 中解析 issue summary 和 issue full link，并匹配 Jira issue。
- 能基于 MR 生成 Markdown review report。
- 能调用配置的 LLM provider 执行 review，并在报告中记录实际使用的 provider。
- 能将 Markdown report 和 review 元数据长期保存到 GitNexus。
- 能在前端查看 Sprint、issue、MR 和 report。
- Flutter Web 前端能运行，并具备适配移动 App 与桌面 App 的代码结构。
- Python CLI 能通过命令行生成 Markdown report。
- 能在用户确认后，将 review 摘要回写到 GitLab MR comment 或 Jira issue comment。
- 敏感配置不出现在代码仓库明文文件中。

## 13. 已确认决策

- GitLab 与 Jira 的实际访问地址、认证方式和 token 权限范围：通过 `.env` 配置文件维护，敏感信息不得提交到代码仓库。
- SVREQ 与 action issue 的关联规则：SVREQ issue 被 action Jira issue 阻塞；同时 `ECHNL` issue summary 包含 `#<SVREQ Jira issue key>`，系统需要支持这两种规则解析。
- GitLab MR 与 Jira issue 的匹配规则：GitLab MR overview 中包含 message summary list；每条 message 包含 issue summary、空格和 issue full link，系统基于该结构提取 Jira issue 关联信息。
- Review engine：使用 LLM review engine，支持 Codex、Claude Code Opus、DeepSeek V4 Pro 等可配置 provider。多组 LLM API key 通过 CC Claude Switch 等外部方式管理。
- Report 长期存储：需要长期存储，方案使用 GitNexus。
- GitLab/Jira 回写策略：回写不默认静默执行，必须先展示待提交内容并要求用户确认。
- 前端交付策略：首期实现 Flutter Web，保留扩展到移动 App 和桌面 App 的能力。
