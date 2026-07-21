#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_VERSION="7.2.13"
CURRENT="/opt/codereviewer/current"
PYTHON="/opt/codereviewer/venv/bin/python"
environment_path() {
  local key="$1"
  local default_value="$2"
  local line value
  line="$(grep -E "^${key}=" /etc/codereviewer/codereviewer.env | tail -1 || true)"
  value="${line#*=}"
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"
  printf '%s\n' "${value:-${default_value}}"
}
WEB_USERS_PATH="$(realpath -m "$(environment_path WEB_USERS_FILE "${CURRENT}/data/web_users.json")")"
DB_PATH="$(realpath -m "$(environment_path CODEREVIEWER_DB_FILE "${CURRENT}/data/codereviewer.db")")"

systemctl is-active --quiet codereviewer.service
systemctl show codereviewer.service \
  -p ActiveState -p SubState -p MainPID -p ExecMainStatus \
  -p User -p Group -p WorkingDirectory -p EnvironmentFiles --no-pager

version_json="$(curl -fsS http://127.0.0.1:8765/api/version)"
version="$(printf '%s' "${version_json}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin).get("version", ""))')"
[[ "${version}" == "${EXPECTED_VERSION}" ]]
printf '%s\n' "${version_json}"

health_json="$(curl -fsS http://127.0.0.1:8765/api/health)"
printf '%s' "${health_json}" | "${PYTHON}" -c \
  'import json,sys; p=json.load(sys.stdin); assert p.get("ok") and p.get("status")=="healthy", p'
printf '%s\n' "${health_json}"
curl -fsS http://127.0.0.1:8765/login >/dev/null
curl -fsS http://127.0.0.1:8765/api/login-challenge >/dev/null
curl -fsS http://127.0.0.1:8765/assets/login-code-review-bg.png >/dev/null
curl -fsS http://127.0.0.1:8765/assets/ttl-jay-crystal-logo.png >/dev/null

unauth_code="$(curl -sS -o /tmp/codereviewer-admin-users-unauth.json -w '%{http_code}' \
  http://127.0.0.1:8765/api/admin/users)"
[[ "${unauth_code}" == "401" ]]
printf 'unauthenticated-admin-api=%s\n' "${unauth_code}"

if grep -Eq '(^|[[:space:]])[A-Za-z]:[/\\]' "${CURRENT}/config.yml"; then
  echo "Production config contains a Windows path." >&2
  exit 1
fi

"${PYTHON}" - "${DB_PATH}" <<'PY'
import sqlite3
import sys

db = sqlite3.connect(sys.argv[1])
integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
schema_row = db.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
columns = [row[1] for row in db.execute("PRAGMA table_info(review_runs)")]
counts = {
    table: db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    for table in ("review_issues", "review_runs", "findings", "discussions", "review_snapshots")
}
db.close()
if integrity != "ok":
    raise SystemExit(f"SQLite integrity check failed: {integrity}")
if not schema_row or str(schema_row[0]) != "3":
    raise SystemExit(f"Expected schema v3, got {schema_row!r}")
if "release_line" not in columns:
    raise SystemExit("review_runs.release_line is missing")
print(f"sqlite=ok schema=3 counts={counts}")
PY

rollback="$(readlink -f /usr/local/sbin/codereviewer-rollback-latest)"
[[ -x "${rollback}" ]]
bash -n "${rollback}"
backup_root="$(dirname "$(grep '^BACKUP_ARCHIVE=' "${rollback}" | head -1 | cut -d= -f2- | tr -d '"')")"
(
  cd "${backup_root}"
  sha256sum -c system-backup.tgz.sha256
)
tar -tzf "${backup_root}/system-backup.tgz" >/dev/null
printf 'rollback=%s\n' "${rollback}"

stat -c '%a %U:%G %n' \
      "${WEB_USERS_PATH}" \
  /etc/codereviewer/codereviewer.env \
  "${rollback}"

ss -ltnp | grep ':8765'
printf 'deployment_commit='
cat "${CURRENT}/.deployment-commit"
printf 'verification=passed\n'
