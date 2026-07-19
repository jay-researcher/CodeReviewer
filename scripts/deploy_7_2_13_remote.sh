#!/usr/bin/env bash
set -Eeuo pipefail

TARGET_VERSION="7.2.13"
PREVIOUS_VERSION="7.2.0"
DEPLOYMENT_COMMIT="${DEPLOYMENT_COMMIT:?DEPLOYMENT_COMMIT is required}"
ARTIFACT_SHA256="${ARTIFACT_SHA256:?ARTIFACT_SHA256 is required}"
STAMP="${DEPLOYMENT_STAMP:-$(date +%Y%m%d-%H%M%S)}"

ARTIFACT="/tmp/codereviewer-7.2.13.tgz"
CONFIG_TEMPLATE="/tmp/config-7.2.13-template.yml"
CONFIG_MERGER="/tmp/merge_release_scope_config.py"
CURRENT="/opt/codereviewer/current"
STAGING="/opt/codereviewer/staging/${TARGET_VERSION}-${STAMP}"
PREVIOUS_DIR="/opt/codereviewer/releases/pre-${TARGET_VERSION}-${STAMP}"
BACKUP_ROOT="/var/backups/codereviewer/${PREVIOUS_VERSION}-to-${TARGET_VERSION}-${STAMP}"
BACKUP_ARCHIVE="${BACKUP_ROOT}/system-backup.tgz"
ROLLBACK_SCRIPT="/usr/local/sbin/codereviewer-rollback-${STAMP}"
PYTHON="/opt/codereviewer/venv/bin/python"
SERVICE="codereviewer.service"
WEB_USERS_PATH=""
DB_PATH=""

require_root() {
  [[ "$(id -u)" -eq 0 ]] || {
    echo "This deployment must run as root." >&2
    exit 1
  }
}

environment_path() {
  local key="$1"
  local default_value="$2"
  local line value
  line="$(grep -E "^${key}=" /etc/codereviewer/codereviewer.env | tail -1 || true)"
  value="${line#*=}"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  printf '%s\n' "${value:-${default_value}}"
}

initialize_and_validate_paths() {
  [[ "${STAMP}" =~ ^[0-9]{8}-[0-9]{6}$ ]] || {
    echo "DEPLOYMENT_STAMP must use YYYYMMDD-HHMMSS." >&2
    exit 1
  }
  [[ "$(realpath -m "${STAGING}")" == /opt/codereviewer/staging/* ]]
  [[ "$(realpath -m "${PREVIOUS_DIR}")" == /opt/codereviewer/releases/pre-${TARGET_VERSION}-* ]]
  [[ "$(realpath -m "${BACKUP_ROOT}")" == /var/backups/codereviewer/${PREVIOUS_VERSION}-to-${TARGET_VERSION}-* ]]
  [[ ! -e "${STAGING}" ]]
  [[ ! -e "${PREVIOUS_DIR}" ]]
  [[ ! -e "${BACKUP_ROOT}" ]]
  WEB_USERS_PATH="$(realpath -m "$(environment_path WEB_USERS_FILE "${CURRENT}/data/web_users.json")")"
  DB_PATH="$(realpath -m "$(environment_path CODEREVIEWER_DB_FILE "${CURRENT}/data/codereviewer.db")")"
  case "${WEB_USERS_PATH}" in
    "${CURRENT}"/data/*|/var/lib/codereviewer/data/*) ;;
    *) echo "WEB_USERS_FILE is outside protected production paths: ${WEB_USERS_PATH}" >&2; exit 1 ;;
  esac
  case "${DB_PATH}" in
    "${CURRENT}"/data/*|/var/lib/codereviewer/data/*) ;;
    *) echo "CODEREVIEWER_DB_FILE is outside protected production paths: ${DB_PATH}" >&2; exit 1 ;;
  esac
  [[ -f "${WEB_USERS_PATH}" ]]
  [[ -f "${DB_PATH}" ]]
}

current_version() {
  curl -fsS http://127.0.0.1:8765/api/version 2>/dev/null \
    | "${PYTHON}" -c 'import json,sys; print(json.load(sys.stdin).get("version", ""))'
}

verify_artifact() {
  [[ -f "${ARTIFACT}" ]]
  [[ -f "${CONFIG_TEMPLATE}" ]]
  [[ -f "${CONFIG_MERGER}" ]]
  echo "${ARTIFACT_SHA256}  ${ARTIFACT}" | sha256sum -c -
  tar -tzf "${ARTIFACT}" >/dev/null
  [[ "$(current_version)" == "${PREVIOUS_VERSION}" ]]
  systemctl is-active --quiet "${SERVICE}"
}

write_user_fingerprint() {
  local output="$1"
  "${PYTHON}" - "${WEB_USERS_PATH}" "${output}" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
payload = json.loads(source.read_text(encoding="utf-8"))
users = payload.get("users") if isinstance(payload, dict) else {}
if not isinstance(users, dict):
    raise SystemExit("Unsupported web_users.json structure.")
projection = {}
roles = {}
active = {}
for name, record in users.items():
    if not isinstance(record, dict):
        continue
    normalized = str(name).casefold()
    credential = str(record.get("password_hash") or record.get("password") or "")
    projection[normalized] = hashlib.sha256(credential.encode("utf-8")).hexdigest()
    roles[normalized] = str(record.get("role") or "")
    active[normalized] = bool(record.get("active", True))
target.write_text(
    json.dumps(
        {
            "count": len(projection),
            "credential_fingerprints": projection,
            "roles": roles,
            "active": active,
        },
        sort_keys=True,
    ),
    encoding="utf-8",
)
PY
  chmod 0600 "${output}"
}

write_db_inventory() {
  local database="$1"
  local output="$2"
  "${PYTHON}" - "${database}" "${output}" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

database = Path(sys.argv[1])
target = Path(sys.argv[2])
db = sqlite3.connect(database)
integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
if integrity != "ok":
    raise SystemExit(f"SQLite integrity check failed: {integrity}")
tables = {
    row[0]
    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
}
tracked = (
    "review_issues",
    "review_runs",
    "findings",
    "finding_handlings",
    "discussions",
    "jira_drafts",
    "pass_records",
    "audit_events",
    "review_cycles",
    "review_snapshots",
)
counts = {
    table: db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    for table in tracked
    if table in tables
}
schema = ""
if "schema_meta" in tables:
    row = db.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
    schema = str(row[0]) if row else ""
columns = []
if "review_runs" in tables:
    columns = [row[1] for row in db.execute("PRAGMA table_info(review_runs)")]
db.close()
target.write_text(
    json.dumps(
        {
            "integrity": integrity,
            "schema_version": schema,
            "counts": counts,
            "review_run_columns": columns,
        },
        sort_keys=True,
    ),
    encoding="utf-8",
)
PY
}

prepare_and_test_staging() {
  mkdir -p "${BACKUP_ROOT}" "$(dirname "${STAGING}")"
  chmod 0700 "${BACKUP_ROOT}"
  rm -rf "${STAGING}"
  mkdir -p "${STAGING}"
  tar -xzf "${ARTIFACT}" -C "${STAGING}"
  "${PYTHON}" "${CONFIG_MERGER}" \
    "${CURRENT}/config.yml" "${CONFIG_TEMPLATE}" "${STAGING}/config.yml"
  if grep -Eq '(^|[[:space:]])[A-Za-z]:[/\\]' "${STAGING}/config.yml"; then
    echo "Merged production config contains a Windows path." >&2
    exit 1
  fi
  "${PYTHON}" - "${CURRENT}/requirements.txt" "${STAGING}/requirements.txt" <<'PY'
import sys
from pathlib import Path
current = Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
candidate = Path(sys.argv[2]).read_text(encoding="utf-8").splitlines()
if current != candidate:
    raise SystemExit("requirements.txt dependencies changed; versioned venv deployment is required.")
print("requirements.txt dependencies unchanged (line-ending differences ignored).")
PY
  mkdir -p "${STAGING}/.deployment-test/data" \
    "${STAGING}/.deployment-test/reports" \
    "${STAGING}/.deployment-test/threads" \
    "${STAGING}/.deployment-test/config-backups" \
    "${STAGING}/.deployment-test/jira-prd" \
    "${STAGING}/.deployment-test/gitnexus" \
    "${STAGING}/.deployment-test/codebase-memory" \
    "${STAGING}/.deployment-test/home" \
    "${STAGING}/.deployment-test/tmp"
  chown -R codereviewer:codereviewer "${STAGING}"
  runuser -u codereviewer -- env \
    GIT_TOOLS_CONFIG="${STAGING}/config.yml" \
    CODEREVIEWER_DB_FILE="${STAGING}/.deployment-test/data/codereviewer.db" \
    WEB_USERS_FILE="${STAGING}/.deployment-test/data/web_users.json" \
    WEB_THREADS_DIR="${STAGING}/.deployment-test/threads" \
    WEB_USER_AUDIT_FILE="${STAGING}/.deployment-test/data/web_user_audit.jsonl" \
    WEB_CONFIG_OVERRIDES_FILE="${STAGING}/.deployment-test/data/web_config_overrides.json" \
    WEB_CONFIG_BACKUP_DIR="${STAGING}/.deployment-test/config-backups" \
    WEB_CONFIG_AUDIT_FILE="${STAGING}/.deployment-test/data/web_config_audit.jsonl" \
    REPORT_OUTPUT_BASE_DIR="${STAGING}/.deployment-test/reports" \
    JIRA_PRD_DATA_DIR="${STAGING}/.deployment-test/jira-prd" \
    JIRA_PRD_AUTO_FETCH=0 \
    LOCAL_JIRA_PRD_ENABLED=0 \
    GITNEXUS_STORAGE_PATH="${STAGING}/.deployment-test/gitnexus" \
    CODEBASE_MEMORY_SOURCE_ROOT="${STAGING}/.deployment-test/codebase-memory" \
    LOCAL_WORKSPACE_CONFIG="${STAGING}/.deployment-test/data/local_workspaces.yml" \
    CC_SWITCH_HOME="${STAGING}/.deployment-test/home/.cc-switch" \
    HOME="${STAGING}/.deployment-test/home" \
    TMPDIR="${STAGING}/.deployment-test/tmp" \
    WEB_AUTH_PRUNE_USERS=0 \
    "${PYTHON}" -m compileall -q \
      "${STAGING}/code_reviewer" "${STAGING}/review.py" "${STAGING}/web.py"
  (
    cd "${STAGING}"
    runuser -u codereviewer -- env \
      GIT_TOOLS_CONFIG="${STAGING}/config.yml" \
      CODEREVIEWER_DB_FILE="${STAGING}/.deployment-test/data/codereviewer.db" \
      WEB_USERS_FILE="${STAGING}/.deployment-test/data/web_users.json" \
      WEB_THREADS_DIR="${STAGING}/.deployment-test/threads" \
      WEB_USER_AUDIT_FILE="${STAGING}/.deployment-test/data/web_user_audit.jsonl" \
      WEB_CONFIG_OVERRIDES_FILE="${STAGING}/.deployment-test/data/web_config_overrides.json" \
      WEB_CONFIG_BACKUP_DIR="${STAGING}/.deployment-test/config-backups" \
      WEB_CONFIG_AUDIT_FILE="${STAGING}/.deployment-test/data/web_config_audit.jsonl" \
      REPORT_OUTPUT_BASE_DIR="${STAGING}/.deployment-test/reports" \
      JIRA_PRD_DATA_DIR="${STAGING}/.deployment-test/jira-prd" \
      JIRA_PRD_AUTO_FETCH=0 \
      LOCAL_JIRA_PRD_ENABLED=0 \
      GITNEXUS_STORAGE_PATH="${STAGING}/.deployment-test/gitnexus" \
      CODEBASE_MEMORY_SOURCE_ROOT="${STAGING}/.deployment-test/codebase-memory" \
      LOCAL_WORKSPACE_CONFIG="${STAGING}/.deployment-test/data/local_workspaces.yml" \
      CC_SWITCH_HOME="${STAGING}/.deployment-test/home/.cc-switch" \
      HOME="${STAGING}/.deployment-test/home" \
      TMPDIR="${STAGING}/.deployment-test/tmp" \
      WEB_AUTH_PRUNE_USERS=0 \
      "${PYTHON}" -m unittest discover -s tests -q
  ) | tee "${BACKUP_ROOT}/staging-tests.txt"
  rm -rf "${STAGING}/.deployment-test"
}

record_inventory() {
  {
    date -Iseconds
    hostname
    systemctl show "${SERVICE}" \
      -p ActiveState -p SubState -p FragmentPath -p User -p Group \
      -p WorkingDirectory -p EnvironmentFiles -p ExecStart --no-pager
    curl -fsS http://127.0.0.1:8765/api/version
    "${PYTHON}" --version
    sha256sum "${CURRENT}/requirements.txt"
    find "${CURRENT}/data" -maxdepth 3 -type f -printf '%m %u:%g %s %p\n' | sort
    find /var/lib/codereviewer -maxdepth 3 -type f -printf '%m %u:%g %s %p\n' | sort
  } >"${BACKUP_ROOT}/pre-deployment-inventory.txt"
  /opt/codereviewer/venv/bin/pip freeze >"${BACKUP_ROOT}/pre-deployment-pip-freeze.txt"
  write_user_fingerprint "${BACKUP_ROOT}/web-users-integrity.json"
}

stop_and_backup() {
  systemctl stop "${SERVICE}"
  ! systemctl is-active --quiet "${SERVICE}"
  "${PYTHON}" - "${DB_PATH}" <<'PY'
import sqlite3
import sys
db = sqlite3.connect(sys.argv[1])
db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
db.close()
if integrity != "ok":
    raise SystemExit(f"SQLite integrity check failed: {integrity}")
PY
  write_db_inventory \
    "${DB_PATH}" \
    "${BACKUP_ROOT}/pre-deployment-db.json"
  tar --acls --xattrs --selinux -czf "${BACKUP_ARCHIVE}" -C / \
    opt/codereviewer/current \
    etc/codereviewer \
    etc/systemd/system/codereviewer.service \
    var/lib/codereviewer/data \
    var/lib/codereviewer/code-review \
    var/lib/codereviewer/jira-prd/data
  sha256sum "${BACKUP_ARCHIVE}" >"${BACKUP_ARCHIVE}.sha256"
  (
    cd "${BACKUP_ROOT}"
    sha256sum -c "$(basename "${BACKUP_ARCHIVE}.sha256")"
  )
  tar -tzf "${BACKUP_ARCHIVE}" >/dev/null
  chmod 0600 "${BACKUP_ARCHIVE}" "${BACKUP_ARCHIVE}.sha256"
}

create_rollback_script() {
  cat >"${ROLLBACK_SCRIPT}" <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail
BACKUP_ROOT="${BACKUP_ROOT}"
BACKUP_ARCHIVE="${BACKUP_ARCHIVE}"
EXPECTED_VERSION="${PREVIOUS_VERSION}"
FAILED_DIR="/opt/codereviewer/releases/failed-${TARGET_VERSION}-\$(date +%Y%m%d-%H%M%S)"
PYTHON="${PYTHON}"
WEB_USERS_PATH="${WEB_USERS_PATH}"
DB_PATH="${DB_PATH}"

[[ "\$(id -u)" -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
cd "\${BACKUP_ROOT}"
sha256sum -c "\$(basename "\${BACKUP_ARCHIVE}.sha256")"
tar -tzf "\${BACKUP_ARCHIVE}" >/dev/null
systemctl stop codereviewer.service || true
mkdir -p /opt/codereviewer/releases
if [[ -e /opt/codereviewer/current ]]; then
  mv /opt/codereviewer/current "\${FAILED_DIR}"
fi
rm -rf /etc/codereviewer \
  /var/lib/codereviewer/data \
  /var/lib/codereviewer/code-review \
  /var/lib/codereviewer/jira-prd/data
tar --acls --xattrs --selinux -xzf "\${BACKUP_ARCHIVE}" -C /
systemctl daemon-reload
systemctl start codereviewer.service
for _ in {1..30}; do
  VERSION="\$(curl -fsS http://127.0.0.1:8765/api/version 2>/dev/null || true)"
  ACTUAL_VERSION="\$(printf '%s' "\${VERSION}" | "\${PYTHON}" -c 'import json,sys; print(json.load(sys.stdin).get("version", ""))' 2>/dev/null || true)"
  if [[ "\${ACTUAL_VERSION}" == "\${EXPECTED_VERSION}" ]]; then
    systemctl is-active --quiet codereviewer.service
    "\${PYTHON}" - <<'PY'
import hashlib
import json
import sqlite3
from pathlib import Path
before = json.loads(Path("${BACKUP_ROOT}/web-users-integrity.json").read_text(encoding="utf-8"))
payload = json.loads(Path("${WEB_USERS_PATH}").read_text(encoding="utf-8"))
users = payload.get("users") if isinstance(payload, dict) else {}
credentials = {
    str(name).casefold(): hashlib.sha256(
        str(record.get("password_hash") or record.get("password") or "").encode("utf-8")
    ).hexdigest()
    for name, record in users.items()
    if isinstance(record, dict)
}
after = {
    "count": len(credentials),
    "credential_fingerprints": credentials,
    "roles": {
        str(name).casefold(): str(record.get("role") or "")
        for name, record in users.items() if isinstance(record, dict)
    },
    "active": {
        str(name).casefold(): bool(record.get("active", True))
        for name, record in users.items() if isinstance(record, dict)
    },
}
if before != after:
    raise SystemExit("Restored Web user account projection does not match.")
db = sqlite3.connect("${DB_PATH}")
integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
db.close()
if integrity != "ok":
    raise SystemExit(f"Restored SQLite integrity check failed: {integrity}")
PY
    echo "Rollback complete: CodeReviewer \${EXPECTED_VERSION}"
    exit 0
  fi
  sleep 1
done
systemctl status codereviewer.service --no-pager || true
journalctl -u codereviewer.service -n 100 --no-pager || true
echo "Rollback verification failed; failed release remains at \${FAILED_DIR}." >&2
exit 1
EOF
  chmod 0700 "${ROLLBACK_SCRIPT}"
  bash -n "${ROLLBACK_SCRIPT}"
  ln -sfn "${ROLLBACK_SCRIPT}" /usr/local/sbin/codereviewer-rollback-latest
  printf '%s\n' "${ROLLBACK_SCRIPT}" >"${BACKUP_ROOT}/rollback-command.txt"
}

deploy_release() {
  mkdir -p /opt/codereviewer/releases
  mv "${CURRENT}" "${PREVIOUS_DIR}"
  mv "${STAGING}" "${CURRENT}"
  rm -rf "${CURRENT}/data" "${CURRENT}/reports"
  cp -a "${PREVIOUS_DIR}/data" "${CURRENT}/data"
  if [[ -d "${PREVIOUS_DIR}/reports" ]]; then
    cp -a "${PREVIOUS_DIR}/reports" "${CURRENT}/reports"
  fi
  if [[ -f "${PREVIOUS_DIR}/.env" ]]; then
    cp -a "${PREVIOUS_DIR}/.env" "${CURRENT}/.env"
  fi
  printf '%s\n' "${TARGET_VERSION}" >"${CURRENT}/.deployment-version"
  printf '%s\n' "${DEPLOYMENT_COMMIT}" >"${CURRENT}/.deployment-commit"
  printf '%s\n' "${ARTIFACT_SHA256}" >"${CURRENT}/.deployment-artifact.sha256"
  printf '%s\n' "${ROLLBACK_SCRIPT}" >"${CURRENT}/.rollback-command"
  chown -R codereviewer:codereviewer "${CURRENT}"
  chmod 0600 "${CURRENT}/data/web_users.json"
  runuser -u codereviewer -- "${PYTHON}" -m compileall -q \
    "${CURRENT}/code_reviewer" "${CURRENT}/review.py" "${CURRENT}/web.py"
}

verify_post_deployment_data() {
  write_user_fingerprint "${BACKUP_ROOT}/web-users-after.json"
  "${PYTHON}" - "${BACKUP_ROOT}" "${DB_PATH}" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

root = Path(sys.argv[1])
database = Path(sys.argv[2])
before_users = json.loads((root / "web-users-integrity.json").read_text(encoding="utf-8"))
after_users = json.loads((root / "web-users-after.json").read_text(encoding="utf-8"))
if before_users != after_users:
    raise SystemExit("Web user account, role, active state or credential changed during deployment.")
db = sqlite3.connect(database)
integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
if integrity != "ok":
    raise SystemExit(f"SQLite integrity check failed: {integrity}")
schema_row = db.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
schema = str(schema_row[0]) if schema_row else ""
if schema != "3":
    raise SystemExit(f"Expected workflow schema 3, got {schema!r}.")
columns = [row[1] for row in db.execute("PRAGMA table_info(review_runs)")]
if "release_line" not in columns:
    raise SystemExit("review_runs.release_line is missing.")
before_db = json.loads((root / "pre-deployment-db.json").read_text(encoding="utf-8"))
tables = {
    row[0]
    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
}
for table, before_count in before_db.get("counts", {}).items():
    if table not in tables:
        raise SystemExit(f"Existing workflow table disappeared: {table}")
    after_count = db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    if after_count < before_count:
        raise SystemExit(f"Workflow row count decreased for {table}: {before_count} -> {after_count}")
db.close()
print(f"Web user integrity verified: {after_users['count']} account(s).")
print("Workflow database verified: schema v3, release_line present, historical counts preserved.")
PY
}

start_and_verify() {
  local started_at
  # journalctl on RHEL 9 accepts this local timestamp format reliably; the
  # ISO-8601 offset form from date -Iseconds is not accepted on every build.
  started_at="$(date '+%Y-%m-%d %H:%M:%S')"
  mkdir -p /run/systemd/system/codereviewer.service.d
  cat >/run/systemd/system/codereviewer.service.d/upgrade-7.2.13.conf <<'EOF'
[Service]
Environment=WEB_AUTH_PRUNE_USERS=0
EOF
  systemctl daemon-reload
  systemctl start "${SERVICE}"
  for _ in {1..45}; do
    if [[ "$(current_version 2>/dev/null || true)" == "${TARGET_VERSION}" ]]; then
      systemctl is-active --quiet "${SERVICE}"
      break
    fi
    sleep 1
  done
  [[ "$(current_version)" == "${TARGET_VERSION}" ]]
  systemctl is-active --quiet "${SERVICE}"
  curl -fsS http://127.0.0.1:8765/login >/dev/null
  curl -fsS http://127.0.0.1:8765/api/login-challenge >/dev/null
  curl -fsS http://127.0.0.1:8765/api/health >"${BACKUP_ROOT}/health.json"
  "${PYTHON}" - "${BACKUP_ROOT}/health.json" <<'PY'
import json
import sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
if payload.get("status") != "healthy" or not payload.get("ok"):
    raise SystemExit(f"Application health is not healthy: {payload}")
PY
  curl -fsS http://127.0.0.1:8765/assets/login-code-review-bg.png >/dev/null
  local unauth_code
  unauth_code="$(curl -sS -o "${BACKUP_ROOT}/unauth-admin-users.json" -w '%{http_code}' \
    http://127.0.0.1:8765/api/admin/users)"
  [[ "${unauth_code}" == "401" ]]
  verify_post_deployment_data
  if grep -Eq '(^|[[:space:]])[A-Za-z]:[/\\]' "${CURRENT}/config.yml"; then
    echo "Production config contains a Windows path." >&2
    exit 1
  fi
  rm -f /run/systemd/system/codereviewer.service.d/upgrade-7.2.13.conf
  rmdir /run/systemd/system/codereviewer.service.d 2>/dev/null || true
  systemctl daemon-reload
  {
    date -Iseconds
    systemctl show "${SERVICE}" -p ActiveState -p SubState -p MainPID -p ExecMainStatus --no-pager
    curl -fsS http://127.0.0.1:8765/api/version
    cat "${BACKUP_ROOT}/health.json"
    printf 'deployment_commit=%s\n' "${DEPLOYMENT_COMMIT}"
    printf 'artifact_sha256=%s\n' "${ARTIFACT_SHA256}"
    printf 'rollback=%s\n' "${ROLLBACK_SCRIPT}"
    stat -c '%a %U:%G %n' \
      "${WEB_USERS_PATH}" \
      /etc/codereviewer/codereviewer.env \
      "${ROLLBACK_SCRIPT}"
    journalctl -u "${SERVICE}" --since "${started_at}" --no-pager
  } >"${BACKUP_ROOT}/post-deployment-verification.txt"
  echo "Deployment complete: CodeReviewer ${TARGET_VERSION}"
  echo "Backup: ${BACKUP_ARCHIVE}"
  echo "One-click rollback: ${ROLLBACK_SCRIPT}"
}

handle_error() {
  local rc=$?
  trap - ERR
  rm -f /run/systemd/system/codereviewer.service.d/upgrade-7.2.13.conf 2>/dev/null || true
  rmdir /run/systemd/system/codereviewer.service.d 2>/dev/null || true
  systemctl daemon-reload || true
  if [[ -x "${ROLLBACK_SCRIPT}" ]]; then
    echo "Deployment failed (exit ${rc}); starting automatic rollback." >&2
    "${ROLLBACK_SCRIPT}" || true
  elif [[ -e "${CURRENT}" ]]; then
    echo "Deployment stopped before release switch; restarting ${PREVIOUS_VERSION}." >&2
    systemctl start "${SERVICE}" || true
  fi
  exit "${rc}"
}

require_root
initialize_and_validate_paths
verify_artifact
prepare_and_test_staging
record_inventory
trap handle_error ERR
stop_and_backup
create_rollback_script
deploy_release
start_and_verify
trap - ERR
