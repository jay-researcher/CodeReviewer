#!/usr/bin/env bash
set -Eeuo pipefail

BACKUP_ROOT="/var/backups/codereviewer/7.0.3-to-7.2.0-20260717-203915"
ROLLBACK="/usr/local/sbin/codereviewer-rollback-20260717-203915"

systemctl is-active codereviewer.service
systemctl show codereviewer.service \
  -p ActiveState -p SubState -p MainPID -p ExecMainStatus --no-pager
curl -fsS http://127.0.0.1:8765/api/version
curl -fsS http://127.0.0.1:8765/login >/dev/null
curl -fsS http://127.0.0.1:8765/api/login-challenge >/dev/null

http_code="$(curl -sS -o /tmp/admin-users-unauth.json -w '%{http_code}' \
  http://127.0.0.1:8765/api/admin/users)"
[[ "${http_code}" == "401" ]]
echo "unauth-admin-users-http-${http_code}"

if grep -Eq '(^|[[:space:]])[A-Za-z]:[/\\]' /opt/codereviewer/current/config.yml; then
  echo "Production config contains a Windows path." >&2
  exit 1
fi
echo "production-config-linux-paths-ok"

cd "${BACKUP_ROOT}"
sha256sum -c system-backup.tgz.sha256
tar -tzf system-backup.tgz >/dev/null
bash -n "${ROLLBACK}"
[[ "$(readlink -f /usr/local/sbin/codereviewer-rollback-latest)" == "${ROLLBACK}" ]]
echo "rollback-entry-ok"

stat -c '%a %U:%G %n' \
  /opt/codereviewer/current/data/web_users.json \
  /etc/codereviewer/codereviewer.env \
  "${ROLLBACK}"

journalctl -u codereviewer.service --since "2026-07-17 20:39:00" --no-pager | tail -80
