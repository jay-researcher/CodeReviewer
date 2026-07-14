# 部署 CodeReviewer 到 192.168.3.78

更新日期：2026-07-14

本文记录本次真实部署结果。通用部署原则和完整生产建议参见 [CodeReviewer-RHEL9-Deployment-Guide.md](CodeReviewer-RHEL9-Deployment-Guide.md)。

## 部署结果

| 项目 | 结果 |
| --- | --- |
| 主机 | `192.168.3.78`，RHEL 9.4 |
| CodeReviewer 版本 | `6.23.0` |
| 部署提交 | `4af19602e8a44e391f783e4925df669e808ca258` |
| Python | 3.11.13 |
| Git | 2.52.0 |
| Codebase Memory | 0.9.0，Linux 本地 CLI 模式 |
| systemd | `codereviewer.service` 已启用且为 `active` |
| 监听地址 | `0.0.0.0:8765` |
| 访问地址 | <http://192.168.3.78:8765> |
| 健康检查 | `GET /api/version` 返回 `{"ok": true, "version": "6.23.0"}` |
| 编译检查 | 通过 |
| RHEL9 测试 | 76 passed |

首次部署时 GitHub token 尚缺少 `Contents: write`，因此本次部署使用 `git archive` 生成同一提交的制品，经 SSH/SFTP 传输后解压；运行目录中的 `.deployment-commit` 固定记录上述提交 SHA。2026-07-14 已补齐 `Contents: Read and write` 并成功推送 `main`，后续更新改用 GitHub 仓库作为发布源。

## 实际目录和权限

```text
/opt/codereviewer/current                 应用代码
/opt/codereviewer/venv                    Python 3.11 虚拟环境
/opt/codereviewer/bin/codebase-memory-mcp Linux MCP CLI
/etc/codereviewer/codereviewer.env        生产环境变量
/var/lib/codereviewer/git-repos           GitLab 工作副本
/var/lib/codereviewer/code-review         Review 报告和需求文档
/var/lib/codereviewer/jira-prd/data       Jira/PRD 缓存
/var/lib/codereviewer/data                运行数据
/var/log/codereviewer                     日志目录
```

服务账号为 `codereviewer`，shell 为 `/sbin/nologin`。`codereviewer.env` 权限为 `0640 root:codereviewer`。生产 token 没有写入应用代码、systemd unit 或本文档。

## 本次安装和配置

RHEL9 原来只有 Python 3.9，已安装：

```bash
dnf install -y python3.11 python3.11-pip git
```

该事务同时按 RHEL 仓库依赖升级了 OpenSSL 和 SQLite 相关包。虚拟环境使用 `/opt/codereviewer/venv`，并通过 `requirements.txt` 安装依赖。

部署中发现 `jira==3.12.0` 不存在，已修正为 `jira==3.10.5`。接入 CLIProxyAPI API-key provider 后，CodeReviewer 版本升级到 6.23.0。

生产 `config.yml` 做了以下主机侧转换：

- `D:/TTL/vibe-coding/git-tools/git-repos` 改为 `/var/lib/codereviewer/git-repos`；
- Review 模板改为 `/opt/codereviewer/current/docs/ECHNL-5539.md`；
- workspace roots 改为 Linux 路径；
- 已安装 `/opt/jira-prd/fetch_jira.py`，并设置 `jira_prd.auto_fetch: true`、按需抓取深度为 2；
- 检查确认生产 `config.yml` 中剩余 `D:/TTL` 路径数量为 0。

生产环境从本机 `.env` 安全传输，然后做了以下覆盖：

- Web：`WEB_HOST=0.0.0.0`、`WEB_PORT=8765`；
- IP 白名单：`192.168.3.0/24,127.0.0.1,::1`；
- 报告根目录：`/var/lib/codereviewer/code-review`；
- 中国大陆工作日历：应用内置 JSON 文件；
- Codebase Memory：`/opt/codereviewer/bin/codebase-memory-mcp`；
- 删除 Windows 客户端使用的全部 `CODEBASE_MEMORY_SSH_*` 和 `CODEBASE_MEMORY_REMOTE_*` 项，避免同机部署再次 SSH 回本机。

## Codebase Memory 接入

原 Linux CLI 位于 root 用户目录，服务账号无法直接安全复用。部署时复制到公共应用目录：

```bash
install -o root -g codereviewer -m 0755 \
  /root/.local/bin/codebase-memory-mcp \
  /opt/codereviewer/bin/codebase-memory-mcp
```

验证结果：

```text
codebase-memory-mcp 0.9.0
```

CodeReviewer 和 MCP 位于同一主机，因此不需要启动 MCP Web 服务、开放 9749，或配置 SSH remote adapter。MCP 索引将使用 `codereviewer` 的 HOME 和缓存目录，避免继续占用 root 用户状态目录。

## systemd 和日常操作

```bash
systemctl status codereviewer.service
systemctl restart codereviewer.service
systemctl enable codereviewer.service
journalctl -u codereviewer.service -n 100 --no-pager
curl -fsS http://127.0.0.1:8765/api/version
cat /opt/codereviewer/current/.deployment-commit
```

重新执行编译和测试：

```bash
cd /opt/codereviewer/current
runuser -u codereviewer -- \
  /opt/codereviewer/venv/bin/python -m compileall -q \
  code_reviewer review.py web.py tools/check_codebase_memory.py

runuser -u codereviewer -- env \
  REPORT_OUTPUT_BASE_DIR=/var/lib/codereviewer/code-review \
  /opt/codereviewer/venv/bin/python -m pytest -q
```

## 尚需补齐的生产能力

Web 服务、基础配置、GitLab/Jira token、本地 MCP、Jira/PRD 同步和 Codex CLI 均已部署，但以下外部组件尚不存在：

- `/opt/web-build-tools`，依赖该目录的 Web Build Tools 上下文不可用；
- Nginx 和 HTTPS 终止；当前为内网 HTTP 直连；

当前 `firewalld` 未运行，8765 端口可从局域网访问。应用层已经限制 `192.168.3.0/24` 和 loopback，但生产环境仍建议启用主机防火墙，并使用 Nginx HTTPS 反向代理后把 `WEB_HOST` 改回 `127.0.0.1`。

## Jira/PRD 知识库同步

Jira/PRD 抓取器已安装并验证：

```text
/opt/jira-prd/fetch_jira.py
/opt/jira-prd/.env                      0640 root:codereviewer
/opt/jira-prd/data                      -> /var/lib/codereviewer/jira-prd/data
/var/lib/codereviewer/jira-prd/data     运行数据
```

`codereviewer-jira-prd.timer` 已启用，每周一至周五按 Asia/Shanghai 时区在 08:00、12:00、16:00、20:00 自动同步。CodeReviewer 对缺失 issue 仍可通过 `jira_prd.auto_fetch: true` 即时抓取。

首次同步已成功生成 16 个 Epic 的索引，并验证 CodeReviewer 能从该目录构建上下文。为避免超大 Epic 占用大量内存，`fetch_jira.py` 在读取到 `--max-children + 1` 条子任务时立即停止并跳过该 Epic，而不是先加载全部子任务。

```bash
systemctl status codereviewer-jira-prd.timer
systemctl start codereviewer-jira-prd.service
journalctl -u codereviewer-jira-prd.service -n 100 --no-pager
```

## CLIProxyAPI / Codex 接入

CLIProxyAPI 7.2.71 运行在 Windows 主机 `192.168.3.170:8318`，绑定到该局域网地址而不是 `0.0.0.0`。3.78 已安装 Node.js 22 和官方 `@openai/codex`，Codex CLI 位于 `/usr/local/bin/codex`。

生产 EnvironmentFile 使用以下配置；实际 API key 只保存在 `/etc/codereviewer/codereviewer.env`，不要写入源码或文档：

```bash
CODEX_CLI_PATH=/usr/local/bin/codex
OPENAI_API_KEY=<CLIProxyAPI api-key>
LLM_CODEX_HTTP_API_KEY_ENV=OPENAI_API_KEY
CODE_REVIEW_OVERRIDE_LLM_CODEX_FORCE_HTTP=1
CODE_REVIEW_OVERRIDE_LLM_CODEX_HTTP_BASE_URL=http://192.168.3.170:8318/v1
```

CodeReviewer 6.23.0 在配置 `codex_http_api_key_env` 后，为 Codex 自定义 provider 注入 `env_key=OPENAI_API_KEY` 并关闭交互式 OpenAI 登录要求。未配置该项时，原有 ChatGPT/Codex 登录模式保持不变。

验证结果：

```text
3.78 -> CLIProxyAPI /v1/models: HTTP 200，10 models
3.78 -> CLIProxyAPI /v1/responses: HTTP 200
Codex CLI: /usr/local/bin/codex
Codex check passed.
{"findings":[],"notes":["codex-check-ok"]}
```

验证命令：

```bash
systemd-run --wait --pipe --collect --quiet \
  --uid=codereviewer --gid=codereviewer \
  --property=WorkingDirectory=/opt/codereviewer/current \
  --property=EnvironmentFile=/etc/codereviewer/codereviewer.env \
  /opt/codereviewer/venv/bin/python review.py \
  --codex-check --codex-check-timeout 180
```

Windows 当前已有名为 `cli-proxy-api` 的 Public 入站允许规则。创建仅允许 `192.168.3.78` 的收窄规则需要管理员权限；应在管理员 PowerShell 中移除/禁用宽泛规则，并只允许来源 `192.168.3.78` 访问 TCP 8318。即使 API key 校验已启用，也不建议对整个局域网开放。

CLIProxyAPI 当前依赖 Windows 主机和登录会话持续运行。Windows 重启、休眠、IP 变化或代理进程退出都会使 DPS Review 失败；长期生产建议把 CLIProxyAPI 配置为受控的开机启动服务，或迁移到固定的服务器节点。

## 后续 GitHub 更新流程

GitHub 首次推送已经完成。建议把现有制品目录保留为回滚副本，再部署固定 SHA：

```bash
systemctl stop codereviewer.service
mv /opt/codereviewer/current /opt/codereviewer/releases/previous-4af1960
git clone https://github.com/jay-researcher/CodeReviewer.git /opt/codereviewer/current
cd /opt/codereviewer/current
git checkout <approved-commit-sha>
chown -R codereviewer:codereviewer /opt/codereviewer/current
/opt/codereviewer/venv/bin/pip install -r requirements.txt
systemctl start codereviewer.service
curl -fsS http://127.0.0.1:8765/api/version
```

执行更新前应备份生产 `config.yml` 的 Linux 路径转换，并确认不会被仓库中的 Windows 开发配置覆盖。更稳妥的长期方案是将生产配置独立放到 `/etc/codereviewer`，由环境变量指向该文件。
