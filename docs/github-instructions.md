# CodeReviewer GitHub 发布说明

更新日期：2026-07-14

目标仓库：<https://github.com/jay-researcher/CodeReviewer>

## 当前结果

本地仓库已经初始化为 `main` 分支，并完成以下提交：

| 提交 | 内容 |
| --- | --- |
| `9697001` | CodeReviewer 6.22.0 首次源码、配置、测试和文档提交 |
| `7196ac6` | 将不存在的 `jira==3.12.0` 修正为 RHEL9 可安装的 `jira==3.10.5`，版本升级为 6.22.1 |
| `4af1960` | 使测试可同时在 Windows 和 RHEL9 执行 |

提交前已确认以下内容不会上传：

- `.env` 和其中的 GitLab、Jira、LLM 凭据；
- `D:\TTL\devops\config.yml` 及 GitHub/SSH 凭据；
- `.claude/` 本机设置；
- `data/` 下运行数据、报告、日志和缓存；
- Windows 版 `codebase-memory-mcp.exe`；
- 大体积 Codebase Memory 索引数据。

当前 GitHub 仓库仍为空，源码尚未推送成功。实际验证结果如下：

```text
Git HTTPS push: HTTP 403, Permission denied
GitHub Git Data API: 403, Resource not accessible by personal access token
GitHub SSH: Permission denied (publickey)
```

API token 可以读取仓库，并能识别账号对仓库具有管理员权限；但是 token 本身没有仓库内容写权限。这是 token 权限问题，不是本地 Git 分支或提交问题。

## 修正 GitHub token 权限

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
4. 创建提交并推送 `main`；
5. 在 192.168.3.78 拉取明确的提交 SHA，不直接部署未固定版本；
6. 重启服务并验证 `/api/version`、systemd 状态和测试结果。

本次本地验证：Python 编译检查通过，76 项测试全部通过。
