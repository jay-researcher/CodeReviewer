### 角色化工作流，版本升级至 6.16.0。
#### 角色权限
- Manager：Issue、Sprint、Jira Filter Review，全局 Coverage，Re-scan、AI Chat、Teams、Manual Pass。
- Auditor：单 Issue Review；按 responsible 查看项目、报告和 Coverage；支持 AI Chat、Re-scan、Manual Pass。
- Developer：查看分配的报告、Diff 和修复建议，逐条填写 Handling；不能发起 Review 或手动 Pass。
处理闭环
#### Review Communication 新增 Handling Tab，每个问题可填写：
- 已整改，Pass通过
- 不是阻碍，另报 Jira issue 跟进
- 不是问题，Pass通过
必填处理说明，并记录处理人和时间。
服务端现在强制校验：所有 Critical/High 问题必须为“已整改”或“不是问题”，否则拒绝 Manual Review Pass。“另报 Jira”不能解除 High/Critical 发布阻塞。
#### Review Coverage
- 新增 Review Coverage 工作台，支持 Issue、Sprint、Jira Filter，统计：
No report
Generating
Handling
Ready for Pass
Review Pass
Failed
#### Manager 查看全局；Auditor 只查看其 responsible 范围。
- 角色可在 [config.yml (line 15)](D:/TTL/vibe-coding/CodeReviewer/config.yml:15) 的 app.web.users 配置，
- Developer 可绑定一个或多个 responsible。核心实现位于 [web_app.py (line 1130)](D:/TTL/vibe-coding/CodeReviewer/code_reviewer/web_app.py:1130)。


### 上下文优化，版本为 6.18.0。
核心调整：
- 本地上下文优先保留变更文件、直接 PHP/JS 依赖、Drupal 服务/路由/模块描述文件。
- 排除 README、CI、phpcs-report.xml、source map、压缩资源等低信号内容。
- 仓库树样本缩小，单文件与 Codebase Memory 摘要均设上限。
- 合并 Jira Review 现在在各 MR 间均分上下文预算，不再由前几个仓库占满。
- 新增跨 MR 契约摘要，保留项目、分支、变更文件与依赖信号。
- 分块阈值调整为 120K；可安全裁剪到 90K 的审核保持一次端到端 Review。
- 针对 ECHNL-5626 的实际复测结果：
-- 优化前：约 161,422 字符，触发 4 个 chunk。
-- 优化后：预裁剪 117,118 字符，最终 Prompt 90,000 字符。


### 完善 Jira Issue 再处理场景的审查上下文。
#### 主要变更：
- Jira 审查时分页读取全部 comments，并保持时间顺序。
- 仅合并包含完整三列表格模板的 comment。
- 根据 Issue 类型区分普通需求模板和 Bug 模板。
- 测试说明、测试结果等普通 comment 不会被合并。
- 原始 description 与匹配的 comments 合成为最终 jira_description。
- 单 Issue、Sprint、Filter、多 Issue 合并审查均使用最终 description。
- 最终 description 会明确传入 LLM 审查上下文。
- MR 链接发现也会扫描后续模板 comment 中补充的内容。
#### 相关文件：
- [jira_client.py (line 26)](D:/TTL/vibe-coding/CodeReviewer/code_reviewer/jira_client.py:26)
- [review_service.py (line 1005)](D:/TTL/vibe-coding/CodeReviewer/code_reviewer/review_service.py:1005)
- [llm_provider.py (line 716)](D:/TTL/vibe-coding/CodeReviewer/code_reviewer/llm_provider.py:716)
- [test_jira_description_comments.py (line 49)](D:/TTL/vibe-coding/CodeReviewer/tests/test_jira_description_comments.py:49)
### Jira 再处理及 Codebase Memory 资源保护，版本升级至 6.19.0。

- Jira Review 合并原始 description 与按时间排序的完整 description 模版 comments。
- Requirement 与 Bug 使用独立模版规则；测试说明和测试结果 comments 保持分离。
- Codebase Memory 索引增加线程级和跨进程单实例锁，多个 Web Job 不再并行启动高内存索引器。
- 超过 50,000 个 Git 跟踪文件的仓库默认跳过图索引；索引默认超时收紧为 120 秒并清理进程树。
- 图索引被跳过或失败只影响依赖图增强，不阻断 Jira/MR 代码审查。
### 延后交付分支文件清单核对，版本升级至 6.20.0。

- Jira 涉及文件清单核对同时覆盖普通功能 MR，以及延后到 GIT_VERSION 发布闸门的 Company Config、SCR MR。
- discovery 阶段被延后的 MR 会按需补抓 changed files；Review 阶段识别的延后 MR 直接保留文件路径。
- 在延后 MR 中匹配到的 Jira 预期文件不再误报为“未提交”。
- 仍存在差异时，问题列表会区分普通提交文件和延后交付文件，并保留真正缺失或 Jira 未列出的文件告警。
### 中国大陆实际工作周报告目录，版本升级至 6.21.0。

- 默认 `e-channel-sprintYYYYMMDD` 不再固定使用星期五，而是当前周一至周日范围内的最后一个实际工作日。
- 支持法定节假日、周六调休上班和周日调休上班；整周放假时回退到最近的实际工作日。
- 内置 2026 年国务院办公厅节假日及调休工作日数据，并支持通过 `CHINA_WORK_CALENDAR_FILE` 使用统一维护的日历。
- 日历缺失、损坏或尚未覆盖新年份时，安全回退到普通周一至周五。
### 远程 Codebase Memory SSH 接入，版本升级至 6.22.0。

- Windows CodeReviewer 可通过 SSH 使用 Linux 主机上的 `codebase-memory-mcp cli`，无需开放 UI 端口。
- 索引时使用 `git archive` 上传目标 commit 的只读源码快照，远端持久化图谱，查询结果返回本地 Review 上下文。
- 远端 commit marker 用于跨进程/重启复用索引；本地单索引锁和资源上限继续生效。
- 默认严格校验 SSH host key；认证信息可从受保护的 DevOps host config 读取，密码不写入 `.env`。
- 新增 `tools/check_codebase_memory.py` 执行无副作用连接及项目列表检查。
### CLIProxyAPI Codex 账号池接入，版本升级至 6.23.0。

- RHEL9 CodeReviewer 通过官方 Codex CLI 和 CLIProxyAPI Responses endpoint 使用共享 GPT 额度池。
- 自定义 Codex provider 支持通过 `codex_http_api_key_env` 指定 API key 环境变量，无需在服务器执行交互式 OAuth 登录。
- 未配置 API key provider 时继续使用原有 OpenAI 登录方式，保持现有 Windows/ChatGPT 调用兼容。
- 缺少指定 API key 时立即给出配置错误，避免 Codex CLI 静默等待直至超时。
