# CodeReviewer GitHub 发布说明

更新日期：2026-07-19

目标仓库：<https://github.com/jay-researcher/CodeReviewer>

## 当前结果

本地仓库已经初始化为 `main` 分支，并完成以下提交：

| 提交 | 内容 |
| --- | --- |
| `9697001` | CodeReviewer 6.22.0 首次源码、配置、测试和文档提交 |
| `7196ac6` | 将不存在的 `jira==3.12.0` 修正为 RHEL9 可安装的 `jira==3.10.5`，版本升级为 6.22.1 |
| `4af1960` | 使测试可同时在 Windows 和 RHEL9 执行 |
| `708287d` | 记录 GitHub 发布流程和 192.168.3.78 实际部署结果 |

提交前已确认以下内容不会上传：

- `.env` 和其中的 GitLab、Jira、LLM 凭据；
- `D:\TTL\devops\config.yml` 及 GitHub/SSH 凭据；
- `.claude/` 本机设置；
- `data/` 下运行数据、报告、日志和缓存；
- Windows 版 `codebase-memory-mcp.exe`；
- 大体积 Codebase Memory 索引数据。

2026-07-14 已补充 Fine-grained personal access token 的 `Contents: Read and write` 权限，并成功将本地 `main` 首次推送到 GitHub。首次推送结果：

```text
To https://github.com/jay-researcher/CodeReviewer.git
* [new branch] main -> main
```

此前的 HTTP 403 原因是 token 对 Code/Contents 只有只读权限；`Workflows: Read and write` 和 `Repository hooks: Read and write` 不能代替 `Contents: Read and write`。调整权限后无需把 token 写入 remote URL，临时 credential helper 即可完成推送。

## GitHub token 权限要求

如果使用 Fine-grained personal access token：

1. Resource owner 选择 `jay-researcher`；
2. Repository access 包含 `CodeReviewer`；
3. Repository permissions 至少设置 `Contents: Read and write`；
4. `Metadata: Read-only` 保持默认即可；
5. 如果组织启用了审批或 SSO，需要完成组织审批/授权。

如果使用 Classic personal access token，则至少需要 `repo` 权限。仓库目前为 public，但创建提交和分支仍必须具备写权限。

更新后的 token 继续保存在 `D:\TTL\devops\config.yml` 的 GitHub `api-token` 配置中。不要把 token 写进仓库 remote URL、提交信息、脚本参数或本文档。

## 安全推送流程

在 `D:\TTL\vibe-coding\CodeReviewer` 执行：

```powershell
git status --short
git log --oneline --decorate -5
git remote -v
git ls-remote https://github.com/jay-researcher/CodeReviewer.git
```

使用临时 credential helper 从 `D:\TTL\devops\config.yml` 读取 token，并在 Git 命令中显式清空系统 credential helper，避免 Windows Credential Manager 弹窗。helper 应只在 Git 请求 `get` 时输出 `x-access-token` 和 token，并在推送完成后立即删除。

```powershell
git -c credential.helper= `
  -c credential.helper="!<python> <temporary-credential-helper.py>" `
  push -u origin main
```

推送后核对本地和远端 SHA：

```powershell
git rev-parse HEAD
git ls-remote origin refs/heads/main
git status -sb
```

两个 SHA 必须一致，且工作区应保持干净。首次推送完成后，再通过 GitHub 网页确认 `.env`、token、密码、私钥、运行报告及 MCP 索引均未出现。

## 后续发布约定

1. 修改版本号并更新相关文档；
2. 执行编译检查和全部测试；
3. 使用 `git diff --cached --check` 和凭据扫描检查暂存内容；
4. 创建发布提交，并在同一次交付中推送当前周分支；push 失败时不得宣称版本发布完成；
5. 每周一使用 `YYYYMMDD` 创建一个周分支。新周分支必须基于上周分支，例如 `20260720` 基于 `20260714`，`20260727` 基于 `20260720`；
6. `main` 只在用户明确批准合并时更新，不自动把周分支合并到 `main`；
7. 推送后记录版本号、分支、commit SHA 以及远端核对结果；
8. 如获准部署，在 192.168.3.78 拉取明确的提交 SHA，不直接部署未固定版本；
9. 重启服务并验证版本接口、systemd 状态和测试结果。

当前发布分支：`20260714`。7.2.11 本地验证：Python 编译、Effective Config、内联 JavaScript 语法检查通过，232 项测试全部通过。
