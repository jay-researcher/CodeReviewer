#!/usr/bin/env bash
set -Eeuo pipefail

TARGET_VERSION="7.2.0"
PREVIOUS_VERSION="7.0.3"
STAMP="20260717-203915"
ARTIFACT="/tmp/codereviewer-7.2.0-20260717-203354.tgz"
ARTIFACT_SHA256="0cc4db69a3f3f0b08de828bc394dd853a92b82641bca4a0e89c3c686d844968a"
CONFIG_TEMPLATE="/tmp/config-7.2.0-template.yml"
CONFIG_MERGER="/tmp/merge_production_config.py"
BACKUP_ROOT="/var/backups/codereviewer/${PREVIOUS_VERSION}-to-${TARGET_VERSION}-${STAMP}"
BACKUP_ARCHIVE="${BACKUP_ROOT}/system-backup.tgz"
PREVIOUS_DIR="/opt/codereviewer/releases/pre-${TARGET_VERSION}-${STAMP}"
CURRENT="/opt/codereviewer/current"
ROLLBACK_SCRIPT="/usr/local/sbin/codereviewer-rollback-${STAMP}"

require_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "This deployment must run as root." >&2
    exit 1
  fi
}

verify_artifact() {
  [[ -f "${ARTIFACT}" ]]
  [[ -f "${CONFIG_TEMPLATE}" ]]
  [[ -f "${CONFIG_MERGER}" ]]
  echo "${ARTIFACT_SHA256}  ${ARTIFACT}" | sha256sum -c -
  tar -tzf "${ARTIFACT}" >/dev/null
}

record_inventory() {
  mkdir -p "${BACKUP_ROOT}"
  chmod 0700 "${BACKUP_ROOT}"
  {
    date -Iseconds
    hostname
    systemctl show codereviewer.service \
      -p ActiveState -p SubState -p FragmentPath -p User -p Group \
      -p WorkingDirectory -p EnvironmentFiles --no-pager
    curl -fsS http://127.0.0.1:8765/api/version
    /opt/codereviewer/venv/bin/python --version
  } >"${BACKUP_ROOT}/pre-deployment-inventory.txt"
  /opt/codereviewer/venv/bin/pip freeze >"${BACKUP_ROOT}/pre-deployment-pip-freeze.txt"
  find "${CURRENT}/data" -maxdepth 3 -type f -printf '%m %u:%g %s %p\n' \
    | sort >"${BACKUP_ROOT}/pre-deployment-data-inventory.txt"
  /opt/codereviewer/venv/bin/python - <<'PY' >"${BACKUP_ROOT}/web-users-integrity.json"
import hashlib
import json
from pathlib import Path

path = Path("/opt/codereviewer/current/data/web_users.json")
payload = json.loads(path.read_text(encoding="utf-8"))
users = payload.get("users") if isinstance(payload, dict) else {}
projection = {
    str(name).casefold(): hashlib.sha256(
        str(record.get("password_hash") or record.get("password") or "").encode("utf-8")
    ).hexdigest()
    for name, record in users.items()
    if isinstance(record, dict)
}
print(json.dumps({"count": len(projection), "credential_fingerprints": projection}, sort_keys=True))
PY
  chmod 0600 "${BACKUP_ROOT}/web-users-integrity.json"
}

create_backup() {
  systemctl stop codereviewer.service
  tar --acls --xattrs --selinux -czf "${BACKUP_ARCHIVE}" -C / \
    opt/codereviewer/current \
    etc/codereviewer \
    etc/systemd/system/codereviewer.service \
    var/lib/codereviewer/data \
    var/lib/codereviewer/code-review \
    var/lib/codereviewer/jira-prd/data
  sha256sum "${BACKUP_ARCHIVE}" >"${BACKUP_ARCHIVE}.sha256"
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

[[ "\$(id -u)" -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
cd "\${BACKUP_ROOT}"
sha256sum -c "\${BACKUP_ARCHIVE}.sha256"
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
  if grep -q "\"version\": \"\${EXPECTED_VERSION}\"" <<<"\${VERSION}"; then
    systemctl is-active --quiet codereviewer.service
    echo "Rollback complete: CodeReviewer \${EXPECTED_VERSION}"
    exit 0
  fi
  sleep 1
done
systemctl status codereviewer.service --no-pager || true
journalctl -u codereviewer.service -n 80 --no-pager || true
echo "Rollback health check failed; deployed files remain at \${FAILED_DIR}." >&2
exit 1
EOF
  chmod 0700 "${ROLLBACK_SCRIPT}"
  ln -sfn "${ROLLBACK_SCRIPT}" /usr/local/sbin/codereviewer-rollback-latest
  printf '%s\n' "${ROLLBACK_SCRIPT}" >"${BACKUP_ROOT}/rollback-command.txt"
}

deploy_release() {
  mkdir -p /opt/codereviewer/releases
  mv "${CURRENT}" "${PREVIOUS_DIR}"
  mkdir -p "${CURRENT}"
  tar -xzf "${ARTIFACT}" -C "${CURRENT}"

  cp -a "${PREVIOUS_DIR}/config.yml" "${CURRENT}/config.yml"
  /opt/codereviewer/venv/bin/python "${CONFIG_MERGER}" \
    "${PREVIOUS_DIR}/config.yml" "${CONFIG_TEMPLATE}" "${CURRENT}/config.yml"
  cp -a "${PREVIOUS_DIR}/data" "${CURRENT}/data"
  if [[ -d "${PREVIOUS_DIR}/reports" ]]; then
    cp -a "${PREVIOUS_DIR}/reports" "${CURRENT}/reports"
  fi
  if [[ -f "${PREVIOUS_DIR}/.env" ]]; then
    cp -a "${PREVIOUS_DIR}/.env" "${CURRENT}/.env"
  fi

  printf '%s\n' "${TARGET_VERSION}" >"${CURRENT}/.deployment-version"
  printf '%s\n' "${ARTIFACT_SHA256}" >"${CURRENT}/.deployment-artifact.sha256"
  printf '%s\n' "${ROLLBACK_SCRIPT}" >"${CURRENT}/.rollback-command"
  chown -R codereviewer:codereviewer "${CURRENT}"

  runuser -u codereviewer -- /opt/codereviewer/venv/bin/pip install -r "${CURRENT}/requirements.txt"
  runuser -u codereviewer -- /opt/codereviewer/venv/bin/python -m compileall -q \
    "${CURRENT}/code_reviewer" "${CURRENT}/review.py" "${CURRENT}/web.py"
  cd "${CURRENT}"
  runuser -u codereviewer -- env REPORT_OUTPUT_BASE_DIR=/var/lib/codereviewer/code-review \
    /opt/codereviewer/venv/bin/python -m pytest -q
}

verify_user_integrity() {
  /opt/codereviewer/venv/bin/python - <<'PY'
import hashlib
import json
from pathlib import Path

before = json.loads(Path(
    "/var/backups/codereviewer/7.0.3-to-7.2.0-20260717-203915/web-users-integrity.json"
).read_text(encoding="utf-8"))
payload = json.loads(Path(
    "/opt/codereviewer/current/data/web_users.json"
).read_text(encoding="utf-8"))
users = payload.get("users") if isinstance(payload, dict) else {}
after = {
    str(name).casefold(): hashlib.sha256(
        str(record.get("password_hash") or record.get("password") or "").encode("utf-8")
    ).hexdigest()
    for name, record in users.items()
    if isinstance(record, dict)
}
if before["credential_fingerprints"] != after:
    raise SystemExit("Web user names or credential hashes changed during deployment.")
print(f"Web user integrity verified: {len(after)} account(s).")
PY
}

start_and_verify() {
  systemctl daemon-reload
  systemctl start codereviewer.service
  for _ in {1..30}; do
    VERSION="$(curl -fsS http://127.0.0.1:8765/api/version 2>/dev/null || true)"
    if grep -q "\"version\": \"${TARGET_VERSION}\"" <<<"${VERSION}"; then
      systemctl is-active --quiet codereviewer.service
      curl -fsS http://127.0.0.1:8765/login >/dev/null
      curl -fsS http://127.0.0.1:8765/api/login-challenge >/dev/null
      verify_user_integrity
      {
        date -Iseconds
        systemctl show codereviewer.service -p ActiveState -p SubState --no-pager
        printf '%s\n' "${VERSION}"
        printf 'rollback=%s\n' "${ROLLBACK_SCRIPT}"
      } >"${BACKUP_ROOT}/post-deployment-verification.txt"
      echo "Deployment complete: CodeReviewer ${TARGET_VERSION}"
      echo "Backup: ${BACKUP_ARCHIVE}"
      echo "One-click rollback: ${ROLLBACK_SCRIPT}"
      return 0
    fi
    sleep 1
  done
  systemctl status codereviewer.service --no-pager || true
  journalctl -u codereviewer.service -n 100 --no-pager || true
  echo "Deployment health check failed." >&2
  return 1
}

rollback_on_error() {
  local rc=$?
  trap - ERR
  echo "Deployment failed (exit ${rc}); starting automatic rollback." >&2
  "${ROLLBACK_SCRIPT}" || true
  exit "${rc}"
}

require_root
verify_artifact
record_inventory
create_backup
create_rollback_script
trap rollback_on_error ERR
deploy_release
start_and_verify
trap - ERR
