# CodeReviewer RHEL9 Production Deployment Guide

本文档用于将 CodeReviewer 部署到 RHEL9 生产环境，并规范配置边界：应用配置集中到 `config.yml` 管理，`.env` 只保留环境、密钥、部署路径和运行时参数。当前代码仍兼容历史 CLI / `.env` 参数；生产部署时建议按本文逐步收敛，避免配置散落。

## 1. 配置治理原则

### 1.1 目标边界

| 配置位置 | 应放内容 | 不应放内容 |
| --- | --- | --- |
| `config.yml` | GitLab 项目清单、项目负责人、项目显示名、版本分支、本地 working copy、项目级 LLM model、dev branch、Review 策略默认值、报告策略、Jira/MR 发现策略 | token、password、Webhook secret、机器私有路径以外的密钥 |
| `.env` / systemd `EnvironmentFile` | GitLab/Jira/LLM/Teams token、生产 URL、监听端口、IP 白名单、反向代理信任、Linux 目录根路径、超时、可执行文件路径 | 项目清单、负责人、项目分支、报告命名规则、Review 业务策略 |
| CLI | 本次执行范围，例如 `--jira`、`--sprint`、`--jira-filter`、`--mr-url`、`--sync-repositories`、`--list-only` | 长期默认策略、项目映射、负责人、模型选择 |

### 1.2 当前已生效的 config.yml 字段

当前 `D:\TTL\vibe-coding\CodeReviewer\config.yml` 已被用于：

- 通过 `repository_url` 匹配 MR 所属 GitLab project。
- 通过 `responsible` 拆分报告目录和 Web 可见范围。
- 通过 `project_name` 控制报告项目前缀和 Web 项目显示。
- 通过 `llm_model` 控制项目级 LLM model。
- 通过 `branch` / `branches` 控制本地仓库同步分支。
- 通过 `local_working_copy` 指向本地完整代码仓库。
- 通过 `dev_branch` 排除开发版本分支。

### 1.3 建议迁入 config.yml 的应用配置

当前 `.env.example` 中有一批应用策略类参数。生产收敛时建议迁移到 `config.yml` 的顶层配置段，例如：

```yaml
app:
  report:
    language: zh-CN
    min_severity: Medium
    group_by_responsible: true
    history_days: 14
  review:
    jira_allowed_statuses:
      - Development Done
    mr_states:
      - opened
      - merged
    ignored_branch_types:
      - Company_Config
      - Git_Version
    auto_chunk: true
    chunk_prompt_threshold_chars: 120000
    chunk_prompt_threshold_ratio: 0.75
    chunk_near_budget_max_mrs_per_chunk: 2
    chunk_max_mrs_per_chunk: 3
  jira:
    project_key: ECHNL
    spaces:
      - SVREQ
      - ECHNL
      - CORE
  llm:
    provider: auto
    codex_model: gpt-5.6-sol
    reasoning_effort: high
    speed: standard
    require_success: true
    require_structured_output: true
    dps_require_codex: true
    prompt_target_chars: 120000
    prompt_max_chars: 160000
    optimize_web_resources: true
    resource_diff_max_chars: 4000
    resource_diff_total_chars: 16000
  local_context:
    auto: true
    project_context_max_chars: 80000
    resource_context_file_max_chars: 1200
    codebase_memory_enabled: true
```

注意：当前代码已支持读取 `config.yml` 顶层 `app:` 段作为应用策略默认值。CLI 显式传入的参数会作为本次运行的 runtime override；`.env` 仅作为环境兜底和密钥来源。即使生产 Python 环境暂时缺少 `PyYAML`，简单的 `app:` 策略配置也会通过内置解析器读取，避免回退到旧默认值。

### 1.4 生产 .env 只保留环境项

生产 `.env` 或 systemd `EnvironmentFile` 建议仅保留：

```bash
# Runtime
WEB_HOST=127.0.0.1
WEB_PORT=8765
WEB_TRUST_PROXY=1
WEB_IP_WHITELIST=192.168.3.0/24
WEB_IP_WHITELIST_FILE=/var/lib/codereviewer/data/web_ip_whitelist.txt

# External services
GITLAB_URL=https://gitlab.tx-tech.com
GITLAB_TOKEN=REPLACE_WITH_PRODUCTION_TOKEN
JIRA_URL=https://tx-tech.atlassian.net
JIRA_USERNAME=REPLACE_WITH_SERVICE_ACCOUNT
JIRA_TOKEN=REPLACE_WITH_PRODUCTION_TOKEN
DEEPSEEK_API_KEY=REPLACE_WITH_DEEPSEEK_TOKEN
TEAMS_BOT_WEBHOOK_URL=
TEAMS_BOT_CA_BUNDLE=

# Runtime paths
GIT_TOOLS_CONFIG=/opt/codereviewer/current/config.yml
REPORT_OUTPUT_BASE_DIR=/var/lib/codereviewer/code-review
JIRA_PRD_DATA_DIR=/var/lib/codereviewer/jira-prd/data
JIRA_PRD_FETCH_SCRIPT=/opt/jira-prd/fetch_jira.py
WEB_BUILD_TOOLS_DIR=/opt/web-build-tools
CODE_REVIEW_REQUIREMENT_FILE=/var/lib/codereviewer/code-review/code_review_requirement.md
CODEBASE_MEMORY_COMMAND=/opt/codereviewer/bin/codebase-memory-mcp

# Optional: CLIProxyAPI/OpenAI-compatible Responses endpoint
CODEX_CLI_PATH=/usr/local/bin/codex
OPENAI_API_KEY=REPLACE_WITH_CLIPROXY_API_KEY
LLM_CODEX_HTTP_API_KEY_ENV=OPENAI_API_KEY
CODE_REVIEW_OVERRIDE_LLM_CODEX_FORCE_HTTP=1
CODE_REVIEW_OVERRIDE_LLM_CODEX_HTTP_BASE_URL=http://CLI_PROXY_HOST:8318/v1

# Runtime limits and process behavior
LLM_CODEX_TIMEOUT_SECONDS=300
LLM_TIMEOUT_SECONDS=180
LLM_MAX_RETRIES=3
REPOSITORY_FETCH_TIMEOUT_SECONDS=300
REPOSITORY_CLONE_TIMEOUT_SECONDS=900
CODEBASE_MEMORY_TIMEOUT_SECONDS=300
CODEBASE_MEMORY_QUERY_TIMEOUT_SECONDS=60
```

历史 `.env` 策略参数仍保留兼容，但生产不建议继续使用；如必须临时覆盖，请使用 `CODE_REVIEW_OVERRIDE_<ENV_KEY>` 形式，并在变更单中说明原因和有效期。

## 2. RHEL9 目录规划

推荐使用独立系统用户和 Linux 标准目录：

| 路径 | 用途 |
| --- | --- |
| `/opt/codereviewer/current` | 应用代码目录 |
| `/opt/codereviewer/venv` | Python virtualenv |
| `/opt/codereviewer/bin` | Codex CLI、codebase-memory-mcp 等外部可执行文件 |
| `/etc/codereviewer/codereviewer.env` | 生产环境变量和密钥，权限 `600` |
| `/var/lib/codereviewer/git-repos` | config.yml 中 `local_working_copy` 的根目录 |
| `/var/lib/codereviewer/code-review` | Markdown 报告输出根目录 |
| `/var/lib/codereviewer/jira-prd/data` | Jira/PRD 本地缓存 |
| `/var/lib/codereviewer/data` | Web 用户、IP 白名单、讨论记录 |
| `/var/log/codereviewer` | systemd 或应用日志 |

创建目录：

```bash
sudo useradd --system --create-home --home-dir /opt/codereviewer --shell /sbin/nologin codereviewer
sudo mkdir -p /opt/codereviewer/current /opt/codereviewer/bin /etc/codereviewer
sudo mkdir -p /var/lib/codereviewer/{git-repos,code-review,jira-prd/data,data}
sudo mkdir -p /var/log/codereviewer
sudo chown -R codereviewer:codereviewer /opt/codereviewer /var/lib/codereviewer /var/log/codereviewer
sudo chmod 750 /opt/codereviewer /var/lib/codereviewer /var/log/codereviewer
```

## 3. 系统依赖

CodeReviewer 使用 Python 3.10+ 语法。RHEL9 默认 Python 可能较旧，生产建议安装 Python 3.11 或 3.12。

```bash
sudo dnf install -y git curl tar unzip gcc make openssl
sudo dnf install -y python3.11 python3.11-pip python3.11-devel
```

如果内部 RHEL9 源没有 `python3.11`，请使用公司批准的软件源或 Python 3.12 RPM。上线前确认：

```bash
python3.11 --version
git --version
curl --version
```

## 4. 部署应用代码

示例以 Git clone 为准；也可以使用 CI/CD 解压制品包。

```bash
sudo -u codereviewer git clone <CodeReviewer-git-url> /opt/codereviewer/current
cd /opt/codereviewer/current
sudo -u codereviewer python3.11 -m venv /opt/codereviewer/venv
sudo -u codereviewer /opt/codereviewer/venv/bin/python -m pip install --upgrade pip
sudo -u codereviewer /opt/codereviewer/venv/bin/pip install -r requirements.txt
```

验证 Python 导入：

```bash
sudo -u codereviewer /opt/codereviewer/venv/bin/python -m compileall -q code_reviewer review.py web.py
```

## 5. 迁移 config.yml

### 5.1 路径从 Windows 改成 Linux

生产环境不能保留 `D:\TTL\...` 路径。需要将所有 `local_working_copy` 改成 Linux 绝对路径，例如：

```yaml
itrade-client:
  itrade-client:
    repository_url: https://gitlab.tx-tech.com/itrade-sv/client/web.git
    responsible: "wen.yi"
    llm_model: "gpt-5.6-sol"
    project_name: "itrade-client"
    branch:
      - ITRADE_CLIENT_7.5.0
      - ITRADE_CLIENT_7.5.1
    local_working_copy: /var/lib/codereviewer/git-repos/itrade-client/itrade-client
```

同样需要处理：

- `dps9-repository`
- `dps11-repository`
- `build-repository`
- `itrade-client`
- `services-terminal`
- `wvadmin-repository`
- 所有 `working_copies`

### 5.2 Git 权限

生产服务账号使用的 GitLab token 必须具备：

- `api`
- `read_api`
- `read_repository`

如果本地仓库使用 HTTPS clone，GitLab token 会通过 GitLab API 访问 MR diff；如需 `git clone` 私有仓库，请配置 Git credential helper、deploy token、或公司标准 Git 凭据方式。

### 5.3 同步仓库

首次上线建议先不建 Codebase Memory 索引，只同步 Git 仓库：

```bash
cd /opt/codereviewer/current
sudo -u codereviewer bash -lc '
  set -a
  source /etc/codereviewer/codereviewer.env
  set +a
  /opt/codereviewer/venv/bin/python review.py --sync-repositories --sync-no-index
'
```

确认仓库、分支、origin 均正确后，再开启 Codebase Memory。

## 6. Codebase Memory 注意事项

当前项目目录中 bundled 的 `tools/codebase-memory-mcp/codebase-memory-mcp.exe` 是 Windows 可执行文件，不能在 RHEL9 运行。

生产有两种选择：

1. 安装 Linux 版本 `codebase-memory-mcp`，并设置：

```bash
CODEBASE_MEMORY_ENABLED=1
CODEBASE_MEMORY_COMMAND=/opt/codereviewer/bin/codebase-memory-mcp
```

2. 暂时关闭：

```bash
CODEBASE_MEMORY_ENABLED=0
```

关闭后，CodeReviewer 仍会基于 MR diff、本地完整代码上下文和 Jira/PRD 上下文 Review，只是缺少长期代码图谱增强。

如果 CodeReviewer 运行在另一台 Windows 主机，不要把 `9749` UI 端口当作 MCP API。当前远程模式通过 SSH 调用 Linux CLI，并上传目标 commit 的 Git archive；参见 Runbook 的 `Remote Linux Codebase Memory over SSH`。Linux 主机只需 SSH 端口，无需启动 `codebase-memory-mcp.service` 或开放 `9749`。

## 7. Codex CLI 与 LLM 运行环境

DPS 项目默认强制 Codex：

```bash
DPS_REVIEW_REQUIRE_CODEX=1
LLM_PROVIDER=auto
LLM_CODEX_MODEL=gpt-5.6-sol
LLM_REASONING_EFFORT=high
LLM_SPEED=standard
```

生产服务器必须满足：

- Codex CLI 已安装，并且 `codex` 在 `PATH` 中，或设置 `CODEX_CLI_PATH`。
- 服务器网络能同时访问 Codex 所需 endpoint 与 GitLab。
- GitLab 如果只允许内网或 DIRECT 规则访问，需要在生产网络策略中放通。

验证：

```bash
cd /opt/codereviewer/current
sudo -u codereviewer bash -lc '
  set -a
  source /etc/codereviewer/codereviewer.env
  set +a
  /opt/codereviewer/venv/bin/python review.py --network-check
'

sudo -u codereviewer bash -lc '
  set -a
  source /etc/codereviewer/codereviewer.env
  set +a
  /opt/codereviewer/venv/bin/python review.py --codex-check --codex-check-timeout 180
'
```

如果非 DPS 项目允许 CC Switch fallback，需要配置默认 selector。默认推荐 Claude code opus；如需 DeepSeek，可显式改为 DeepSeek：

```bash
LLM_CC_SWITCH_PROVIDER=Claude code opus
```

## 8. systemd 服务

创建 `/etc/codereviewer/codereviewer.env`：

```bash
sudo install -o root -g codereviewer -m 0640 /dev/null /etc/codereviewer/codereviewer.env
sudo vi /etc/codereviewer/codereviewer.env
```

创建 `/etc/systemd/system/codereviewer.service`：

```ini
[Unit]
Description=CodeReviewer Web Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=codereviewer
Group=codereviewer
WorkingDirectory=/opt/codereviewer/current
EnvironmentFile=/etc/codereviewer/codereviewer.env
ExecStart=/opt/codereviewer/venv/bin/python web.py --host ${WEB_HOST} --port ${WEB_PORT}
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/var/lib/codereviewer /var/log/codereviewer /opt/codereviewer/current/data /opt/codereviewer/current/reports

[Install]
WantedBy=multi-user.target
```

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now codereviewer
sudo systemctl status codereviewer
```

查看日志：

```bash
journalctl -u codereviewer -f
```

健康检查：

```bash
curl -s http://127.0.0.1:8765/api/version
```

## 9. Nginx HTTPS 反向代理

生产建议不直接暴露 Python Web 服务，对外使用 Nginx + HTTPS。

安装：

```bash
sudo dnf install -y nginx
```

示例 `/etc/nginx/conf.d/codereviewer.conf`：

```nginx
server {
    listen 443 ssl http2;
    server_name codereviewer.example.com;

    ssl_certificate     /etc/pki/tls/certs/codereviewer.crt;
    ssl_certificate_key /etc/pki/tls/private/codereviewer.key;

    client_max_body_size 20m;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 3600;
    }
}
```

环境变量中设置：

```bash
WEB_TRUST_PROXY=1
WEB_HOST=127.0.0.1
WEB_PORT=8765
```

启用：

```bash
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
```

防火墙：

```bash
sudo firewall-cmd --permanent --add-service=https
sudo firewall-cmd --reload
```

如果只在内网使用，也可以限制来源网段。

## 10. Web 登录、IP 白名单与账号

CodeReviewer Web 登录名来自 `responsible`，例如 `wen.yi`、`kevin.tan`。密码随机生成并存放在：

```text
/opt/codereviewer/current/data/web_users.json
```

生产建议将 data 目录迁移到 `/var/lib/codereviewer/data`，或者确保 `/opt/codereviewer/current/data` 在部署发布时不会被覆盖。

IP 白名单：

```bash
sudo -u codereviewer tee /var/lib/codereviewer/data/web_ip_whitelist.txt >/dev/null <<'EOF'
192.168.3.0/24
10.0.0.0/8
EOF
```

权限：

```bash
sudo chown -R codereviewer:codereviewer /var/lib/codereviewer/data
sudo chmod 750 /var/lib/codereviewer/data
sudo chmod 640 /var/lib/codereviewer/data/web_ip_whitelist.txt
```

## 11. Teams 集成

Teams 功能需要 HTTPS 链接和 Teams webhook：

```bash
TEAMS_BOT_WEBHOOK_URL=https://...
TEAMS_BOT_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt
```

当前 Web 支持：

- 在 Review Communication 中准备 Teams 消息。
- 发送 responsible 报告链接或 Markdown report。
- 记录讨论和处理状态。

要实现真正 `@Responsible` 的 Teams mention，需要 Teams Bot / Graph API 能解析公司用户 ID。仅 Incoming Webhook 通常不能保证真实 @ 效果，生产接入前需要确认 Teams Bot 权限模型。

## 12. 上线前检查清单

### 12.1 配置检查

- [ ] `config.yml` 已改为 Linux 路径。
- [ ] 每个 `repository_url` 都能由生产服务器访问。
- [ ] 每个 `local_working_copy` 都在 `/var/lib/codereviewer/git-repos` 下。
- [ ] `responsible` 与 Web 登录用户一致。
- [ ] `project_name` 已配置，便于报告命名。
- [ ] `llm_model` 已按项目配置。
- [ ] `dev_branch` 已配置，避免误审开发版本分支。
- [ ] `Company_Config`、`Git_Version` 分支前缀忽略策略已确认。

### 12.2 环境检查

- [ ] `.env` / `EnvironmentFile` 权限为 `640` 或更严格。
- [ ] GitLab token 具备 `api/read_api/read_repository`。
- [ ] Jira service account 可访问 ECHNL/SVREQ/CORE。
- [ ] Codex CLI 可执行。
- [ ] 如果启用 Codebase Memory，RHEL9 上安装的是 Linux binary。
- [ ] Nginx HTTPS 证书有效。
- [ ] Web IP 白名单已配置。
- [ ] `WEB_TRUST_PROXY=1` 仅在可信反向代理后启用。

### 12.3 功能检查

```bash
cd /opt/codereviewer/current

sudo -u codereviewer bash -lc '
  set -a
  source /etc/codereviewer/codereviewer.env
  set +a
  /opt/codereviewer/venv/bin/python review.py --network-check
'

sudo -u codereviewer bash -lc '
  set -a
  source /etc/codereviewer/codereviewer.env
  set +a
  /opt/codereviewer/venv/bin/python review.py --sync-repositories --sync-no-index
'

sudo -u codereviewer bash -lc '
  set -a
  source /etc/codereviewer/codereviewer.env
  set +a
  /opt/codereviewer/venv/bin/python review.py --jira ECHNL-0000 --jira-mr-list-only
'
```

将 `ECHNL-0000` 替换为测试 issue。

Web 验证：

- [ ] 登录页显示版本号。
- [ ] 非白名单 IP 显示 Access denied。
- [ ] Responsible 用户只能看到自己的报告。
- [ ] Admin/root 可以查看 Sprint/Jira Filter 输入。
- [ ] Report History 能搜索最近 2 周报告。
- [ ] Preview / Raw / Download / Discuss / Compare 正常。
- [ ] Pause / Resume / Stop / Retry 正常。

## 13. 备份与恢复

需要备份：

| 路径 | 内容 |
| --- | --- |
| `/etc/codereviewer/codereviewer.env` | 生产环境变量和密钥 |
| `/opt/codereviewer/current/config.yml` | 项目清单与应用配置 |
| `/var/lib/codereviewer/code-review` | Code Review 报告 |
| `/var/lib/codereviewer/data` | Web 用户、白名单、讨论记录 |
| `/var/lib/codereviewer/jira-prd/data` | Jira/PRD 缓存 |
| `/var/lib/codereviewer/git-repos` | 本地仓库缓存，可重建但恢复更快 |

建议每日备份报告和 data，每周备份 Git repos 缓存。密钥文件必须加密备份。

## 14. 升级流程

1. 停止服务：

```bash
sudo systemctl stop codereviewer
```

2. 备份：

```bash
sudo tar -czf /var/backups/codereviewer-$(date +%Y%m%d%H%M).tgz \
  /etc/codereviewer \
  /opt/codereviewer/current/config.yml \
  /var/lib/codereviewer/data \
  /var/lib/codereviewer/code-review
```

3. 更新代码：

```bash
cd /opt/codereviewer/current
sudo -u codereviewer git pull --ff-only
sudo -u codereviewer /opt/codereviewer/venv/bin/pip install -r requirements.txt
sudo -u codereviewer /opt/codereviewer/venv/bin/python -m compileall -q code_reviewer review.py web.py
```

4. 启动并验证：

```bash
sudo systemctl start codereviewer
sudo systemctl status codereviewer
curl -s http://127.0.0.1:8765/api/version
```

## 15. 回滚流程

推荐每次发布生成固定回滚脚本，并维护稳定入口：

```bash
sudo /usr/local/sbin/codereviewer-rollback-latest
```

一键脚本至少必须：

1. 校验备份 SHA-256 和 tar 可读性；
2. 停止 CodeReviewer；
3. 将当前失败版本移动到 `/opt/codereviewer/releases/failed-*`；
4. 恢复应用目录、EnvironmentFile、systemd Unit、用户/工作流数据和报告；
5. 执行 `systemctl daemon-reload` 并启动服务；
6. 验证 `/api/version` 返回备份版本，否则保留日志并以非零状态退出。

1. 停止服务：

```bash
sudo systemctl stop codereviewer
```

2. 回到上一版本代码或恢复制品：

```bash
cd /opt/codereviewer/current
sudo -u codereviewer git checkout <previous-release-tag-or-commit>
sudo -u codereviewer /opt/codereviewer/venv/bin/pip install -r requirements.txt
```

3. 如配置已变更，恢复备份的 `config.yml` 和 `.env`：

```bash
sudo cp /path/to/backup/config.yml /opt/codereviewer/current/config.yml
sudo cp /path/to/backup/codereviewer.env /etc/codereviewer/codereviewer.env
sudo chown root:codereviewer /etc/codereviewer/codereviewer.env
sudo chmod 640 /etc/codereviewer/codereviewer.env
```

4. 启动：

```bash
sudo systemctl start codereviewer
```

## 16. Current Configuration Consolidation

This release completes the main consolidation target: `config.yml` owns application policy, and `.env` owns environment-specific values.

1. `config.yml` top-level `app:` is now the default source for review policy, Jira/MR discovery, report policy, LLM policy, local context, Codebase Memory, and GIT_VERSION deep-review policy.
2. `.env` should be limited to secrets, service URLs, host/port, deployment paths, binary locations, and environment-only fallbacks.
3. CLI strategy options are runtime overrides. When omitted, CodeReviewer uses `config.yml app:`.
4. Temporary production overrides should use `CODE_REVIEW_OVERRIDE_<ENV_KEY>`, for example `CODE_REVIEW_OVERRIDE_REPORT_MIN_SEVERITY=High`.

Before production release, verify:

- `config.yml app:` matches the release policy.
- `/etc/codereviewer/codereviewer.env` contains only environment and secret values.
- `/api/version` returns the expected version.
- `python review.py --sync-repositories` can fetch configured repositories and branches.
