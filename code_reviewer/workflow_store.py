from __future__ import annotations

import ast
import hashlib
import base64
import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Protocol

from .adf import adf_json, adf_plain_text, empty_adf, validate_adf
from .config import DATA_DIR, app_config_get
from .review_scope import delivery_version_for_merge_request, review_scope_for_merge_request


SCHEMA_VERSION = 3
DISPOSITIONS = {"fixed", "follow-up", "not-issue"}
ISSUE_STATUSES = {
    "not-reviewed",
    "generating",
    "handling",
    "rescan-required",
    "rescanning",
    "ready-for-pass",
    "passed",
    "generation-failed",
    "rescan-failed",
    "reopened",
}


class WorkflowRepository(Protocol):
    """Framework/database-neutral contract used by Web and future Flutter clients."""

    def list_issues(self, *, responsibles: list[str] | None = None, view_all: bool = False) -> list[dict[str, Any]]: ...
    def issue_detail(self, jira_key: str, cycle_id: str = "") -> dict[str, Any] | None: ...
    def cycle_detail(self, cycle_id: str) -> dict[str, Any] | None: ...
    def list_drafts(self, jira_key: str = "") -> list[dict[str, Any]]: ...
    def list_cycles(self, jira_key: str) -> list[dict[str, Any]]: ...
    def list_sprint_memberships(self, jira_key: str) -> list[dict[str, Any]]: ...
    def upsert_sprint_membership(self, **kwargs: Any) -> dict[str, Any]: ...
    def upsert_review_cycle(self, **kwargs: Any) -> dict[str, Any]: ...
    def reconcile_sprint_scope(self, **kwargs: Any) -> dict[str, Any]: ...
    def create_run_group(self, **kwargs: Any) -> dict[str, Any]: ...
    def create_description_snapshot(self, **kwargs: Any) -> dict[str, Any]: ...
    def create_review_snapshot(self, **kwargs: Any) -> dict[str, Any]: ...
    def upsert_deferred_resource(self, **kwargs: Any) -> dict[str, Any]: ...


def utc_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def workflow_db_path() -> Path:
    return Path(os.getenv("CODEREVIEWER_DB_FILE", str(DATA_DIR / "codereviewer.db"))).expanduser()


def blocking_severities() -> set[str]:
    value = app_config_get("review_workflow.blocking_severities", ["Critical", "High"])
    if not isinstance(value, list):
        value = ["Critical", "High"]
    return {str(item).strip().title() for item in value if str(item).strip()}


APPLICATION_ORDER = ("WVAdmin", "iTrade Client", "Services Terminal", "DPS", "Unmapped")


def scope_people(value: object) -> set[str]:
    """Return the distinct people encoded by a Responsible Scope value.

    Persisted reports have used plain strings, delimiter-separated strings,
    JSON arrays, and Python-list string representations.  Keeping this parser
    storage-neutral lets both repository filtering and HTTP authorization use
    exactly the same interpretation.
    """
    if value is None:
        return set()
    if isinstance(value, dict):
        people: set[str] = set()
        for item in value.values():
            people.update(scope_people(item))
        return people
    if isinstance(value, (list, tuple, set, frozenset)):
        people = set()
        for item in value:
            people.update(scope_people(item))
        return people
    if not isinstance(value, str):
        return set()

    text = value.strip()
    if not text:
        return set()
    if text[:1] in {"[", "{", "("}:
        parsed: object | None = None
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            try:
                parsed = ast.literal_eval(text)
            except (SyntaxError, ValueError):
                parsed = None
        if parsed is not None and parsed != text:
            return scope_people(parsed)
    return {
        item.strip().strip("\"'")
        for item in re.split(r"[+,;|\r\n]+", text)
        if item.strip().strip("\"'")
    }


def review_scope_label(application: str, release_line: str = "", delivery_version: str = "") -> str:
    """Build the stable display label for an application/release-line scope."""
    application = application.strip() or "Unmapped"
    release_line = release_line.strip()
    delivery_version = delivery_version.strip()
    if not release_line:
        return application
    if application == "iTrade Client":
        return f"{application} {delivery_version or release_line}"
    if application == "DPS":
        return release_line if release_line.casefold().startswith("dps") else f"{application} {release_line}"
    return application


def review_application_from_scope(item: dict[str, Any]) -> str:
    """Map persisted MR discovery metadata to a release application."""
    explicit = str(item.get("application") or "").strip()
    if explicit in APPLICATION_ORDER:
        return explicit
    group = str(item.get("git_tools_group") or "").strip().lower()
    module = str(item.get("git_tools_module") or "").strip().lower()
    project_name = str(item.get("project_name") or "").strip().lower()
    project_path = str(
        item.get("gitlab_project") or item.get("project_path") or item.get("project") or ""
    ).strip().lower()
    identity = " ".join((module, project_name, project_path))
    if group in {"dps9-repository", "dps11-repository"}:
        return "DPS"
    if group == "wvadmin-repository":
        return "WVAdmin"
    if group == "itrade-client":
        return "Services Terminal" if "service-terminal" in identity or "services-terminal" in identity else "iTrade Client"
    if group == "build-repository":
        if "wvadmin" in identity:
            return "WVAdmin"
        if "service-terminal" in identity or "services-terminal" in identity:
            return "Services Terminal"
        if "itrade-client" in identity:
            return "iTrade Client"
        if module == "dps" or "web-sv-build/dps" in project_path:
            return "DPS"
    if "/dps/" in project_path or "/dps11/" in project_path or project_path.endswith("/dps"):
        return "DPS"
    if "/wvadm/" in project_path or project_path.endswith("/wvadmin"):
        return "WVAdmin"
    if "itrade-sv/terminal/" in project_path or "services-terminal" in project_path:
        return "Services Terminal"
    if "itrade-sv/client/" in project_path or "itrade-client" in project_path:
        return "iTrade Client"
    return "Unmapped"


def finding_fingerprint(jira_key: str, finding: dict[str, Any]) -> str:
    normalized = "|".join(
        str(value or "").strip().lower()
        for value in (
            jira_key,
            finding.get("project"),
            finding.get("file") or finding.get("path"),
            finding.get("category") or finding.get("rule"),
            finding.get("title"),
        )
    )
    normalized = " ".join(normalized.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def report_fingerprint(report_path: str) -> str:
    try:
        stat = Path(report_path).stat()
        identity = f"{Path(report_path).resolve()}|{stat.st_mtime_ns}|{stat.st_size}"
    except OSError:
        identity = report_path
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class WorkflowStore:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or workflow_db_path()).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.ensure_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=20000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def ensure_schema(self) -> None:
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._execute_script(db,
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_issues (
                    jira_key TEXT PRIMARY KEY,
                    summary TEXT NOT NULL DEFAULT '',
                    responsible TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'not-reviewed',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    latest_run_id TEXT,
                    passed_run_id TEXT
                );
                CREATE TABLE IF NOT EXISTS review_runs (
                    id TEXT PRIMARY KEY,
                    jira_key TEXT NOT NULL REFERENCES review_issues(jira_key) ON DELETE CASCADE,
                    report_path TEXT NOT NULL,
                    report_fingerprint TEXT NOT NULL,
                    run_number INTEGER NOT NULL,
                    conclusion TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'completed',
                    severity_counts_json TEXT NOT NULL DEFAULT '{}',
                    release_line TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(jira_key, report_fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_review_runs_issue ON review_runs(jira_key, run_number DESC);
                CREATE TABLE IF NOT EXISTS findings (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
                    jira_key TEXT NOT NULL REFERENCES review_issues(jira_key) ON DELETE CASCADE,
                    fingerprint TEXT NOT NULL,
                    report_index TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    file_path TEXT NOT NULL DEFAULT '',
                    details_json TEXT NOT NULL DEFAULT '{}',
                    lineage_state TEXT NOT NULL DEFAULT 'new',
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, report_index)
                );
                CREATE INDEX IF NOT EXISTS idx_findings_issue_fingerprint ON findings(jira_key, fingerprint);
                CREATE TABLE IF NOT EXISTS finding_handlings (
                    id TEXT PRIMARY KEY,
                    finding_id TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
                    disposition TEXT NOT NULL,
                    note TEXT NOT NULL,
                    submitted_by TEXT NOT NULL,
                    approval_status TEXT NOT NULL DEFAULT 'not-required',
                    approved_by TEXT,
                    approved_at TEXT,
                    manager_override INTEGER NOT NULL DEFAULT 0,
                    override_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_handlings_finding ON finding_handlings(finding_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS discussions (
                    id TEXT PRIMARY KEY,
                    jira_key TEXT NOT NULL REFERENCES review_issues(jira_key) ON DELETE CASCADE,
                    run_id TEXT REFERENCES review_runs(id) ON DELETE SET NULL,
                    finding_id TEXT REFERENCES findings(id) ON DELETE SET NULL,
                    author TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'comment',
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jira_drafts (
                    id TEXT PRIMARY KEY,
                    jira_key TEXT NOT NULL REFERENCES review_issues(jira_key) ON DELETE CASCADE,
                    source_finding_id TEXT REFERENCES findings(id) ON DELETE SET NULL,
                    summary TEXT NOT NULL,
                    description_adf_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending-create',
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS draft_attachments (
                    id TEXT PRIMARY KEY,
                    draft_id TEXT NOT NULL REFERENCES jira_drafts(id) ON DELETE CASCADE,
                    file_name TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    storage_path TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pass_records (
                    id TEXT PRIMARY KEY,
                    jira_key TEXT NOT NULL REFERENCES review_issues(jira_key) ON DELETE CASCADE,
                    run_id TEXT NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
                    actor TEXT NOT NULL,
                    note TEXT NOT NULL,
                    policy_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id TEXT PRIMARY KEY,
                    jira_key TEXT,
                    actor TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                """
            )
            self._migrate_v2(db)
            self._migrate_v3(db)
            db.execute(
                "INSERT INTO schema_meta(key, value) VALUES('version', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    @staticmethod
    def _column_names(db: sqlite3.Connection, table: str) -> set[str]:
        return {str(row["name"]) for row in db.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _execute_script(db: sqlite3.Connection, script: str) -> None:
        """Execute simple DDL as one caller-owned transaction.

        sqlite3.executescript() commits implicitly before running; executing the
        statements individually preserves rollback semantics for migrations.
        """
        for statement in script.split(";"):
            if statement.strip():
                db.execute(statement)

    def _add_column(self, db: sqlite3.Connection, table: str, definition: str) -> None:
        name = definition.split()[0]
        if name not in self._column_names(db, table):
            db.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    def _migrate_v2(self, db: sqlite3.Connection) -> None:
        """Add cycle-oriented workflow tables and backfill legacy rows atomically.

        Every statement is safe to execute again.  SQLite DDL participates in the
        surrounding transaction, so a failed migration leaves the previous schema
        and data intact.
        """
        self._add_column(db, "review_issues", "current_cycle_id TEXT")
        for definition in (
            "cycle_id TEXT",
            "run_group_id TEXT",
            "project_type TEXT NOT NULL DEFAULT ''",
            "application TEXT NOT NULL DEFAULT ''",
            "responsible_scope TEXT NOT NULL DEFAULT ''",
            "mr_fingerprint TEXT NOT NULL DEFAULT ''",
            "stable_fingerprint TEXT NOT NULL DEFAULT ''",
        ):
            self._add_column(db, "review_runs", definition)
        self._add_column(db, "discussions", "cycle_id TEXT")
        self._add_column(db, "pass_records", "cycle_id TEXT")
        self._add_column(db, "pass_records", "run_group_id TEXT")

        self._execute_script(db,
            """
            CREATE TABLE IF NOT EXISTS review_cycles (
                cycle_id TEXT PRIMARY KEY,
                jira_key TEXT NOT NULL REFERENCES review_issues(jira_key) ON DELETE CASCADE,
                sprint_id TEXT NOT NULL DEFAULT '',
                sprint_name TEXT NOT NULL DEFAULT '',
                sprint_state TEXT NOT NULL DEFAULT 'unknown',
                cycle_number INTEGER NOT NULL,
                cycle_started_at TEXT NOT NULL,
                cycle_closed_at TEXT,
                status_transition_json TEXT NOT NULL DEFAULT '{}',
                review_mode TEXT NOT NULL DEFAULT 'issue',
                current_description_snapshot_id TEXT,
                mr_scope_json TEXT NOT NULL DEFAULT '[]',
                pass_status TEXT NOT NULL DEFAULT 'pending',
                release_gate_status TEXT NOT NULL DEFAULT 'pending',
                backfilled INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(jira_key, sprint_id, cycle_number)
            );
            CREATE INDEX IF NOT EXISTS idx_review_cycles_issue
                ON review_cycles(jira_key, cycle_number DESC);
            CREATE INDEX IF NOT EXISTS idx_review_cycles_sprint
                ON review_cycles(sprint_id, sprint_state, updated_at DESC);

            CREATE TABLE IF NOT EXISTS sprint_memberships (
                id TEXT PRIMARY KEY,
                jira_key TEXT NOT NULL REFERENCES review_issues(jira_key) ON DELETE CASCADE,
                sprint_id TEXT NOT NULL,
                sprint_name TEXT NOT NULL DEFAULT '',
                sprint_state TEXT NOT NULL DEFAULT 'unknown',
                joined_at TEXT,
                left_at TEXT,
                source_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(jira_key, sprint_id)
            );
            CREATE INDEX IF NOT EXISTS idx_sprint_memberships_sprint
                ON sprint_memberships(sprint_id, sprint_state, jira_key);

            CREATE TABLE IF NOT EXISTS review_run_groups (
                id TEXT PRIMARY KEY,
                cycle_id TEXT NOT NULL REFERENCES review_cycles(cycle_id) ON DELETE CASCADE,
                jira_key TEXT NOT NULL REFERENCES review_issues(jira_key) ON DELETE CASCADE,
                review_mode TEXT NOT NULL DEFAULT 'issue',
                status TEXT NOT NULL DEFAULT 'completed',
                stable_fingerprint TEXT NOT NULL DEFAULT '',
                backfilled INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_run_groups_cycle
                ON review_run_groups(cycle_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS description_snapshots (
                id TEXT PRIMARY KEY,
                cycle_id TEXT NOT NULL REFERENCES review_cycles(cycle_id) ON DELETE CASCADE,
                jira_key TEXT NOT NULL REFERENCES review_issues(jira_key) ON DELETE CASCADE,
                sprint_id TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL,
                adf_json TEXT NOT NULL DEFAULT '{}',
                rendered_html TEXT NOT NULL DEFAULT '',
                plain_text TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL DEFAULT '',
                source_created_at TEXT,
                source_updated_at TEXT,
                template_language TEXT NOT NULL DEFAULT '',
                issue_type TEXT NOT NULL DEFAULT '',
                attachments_json TEXT NOT NULL DEFAULT '[]',
                jira_status TEXT NOT NULL DEFAULT '',
                code_mrs_json TEXT NOT NULL DEFAULT '[]',
                deferred_mrs_json TEXT NOT NULL DEFAULT '[]',
                reason TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                backfilled INTEGER NOT NULL DEFAULT 0,
                captured_at TEXT NOT NULL,
                UNIQUE(cycle_id, source_type, source_id, version)
            );
            CREATE INDEX IF NOT EXISTS idx_description_snapshots_cycle
                ON description_snapshots(cycle_id, captured_at DESC);

            CREATE TABLE IF NOT EXISTS review_snapshots (
                id TEXT PRIMARY KEY,
                cycle_id TEXT NOT NULL REFERENCES review_cycles(cycle_id) ON DELETE CASCADE,
                jira_key TEXT NOT NULL REFERENCES review_issues(jira_key) ON DELETE CASCADE,
                revision INTEGER NOT NULL,
                reason TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                actor TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(cycle_id, revision)
            );
            CREATE INDEX IF NOT EXISTS idx_review_snapshots_cycle
                ON review_snapshots(cycle_id, revision DESC);

            CREATE TABLE IF NOT EXISTS deferred_release_resources (
                id TEXT PRIMARY KEY,
                jira_key TEXT NOT NULL REFERENCES review_issues(jira_key) ON DELETE CASCADE,
                sprint_id TEXT NOT NULL DEFAULT '',
                cycle_id TEXT NOT NULL REFERENCES review_cycles(cycle_id) ON DELETE CASCADE,
                gitlab_project TEXT NOT NULL,
                mr_iid TEXT NOT NULL,
                head_sha TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                mr_url TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                gate_run_id TEXT NOT NULL DEFAULT '',
                locked_build_commit TEXT NOT NULL DEFAULT '',
                evidence_json TEXT NOT NULL DEFAULT '{}',
                verified_by TEXT NOT NULL DEFAULT '',
                verified_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(jira_key, sprint_id, cycle_id, gitlab_project, mr_iid, head_sha)
            );
            CREATE INDEX IF NOT EXISTS idx_deferred_release_pending
                ON deferred_release_resources(sprint_id, status, cycle_id, updated_at);

            CREATE TABLE IF NOT EXISTS idempotency_records (
                operation TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(operation, idempotency_key)
            );
            """
        )
        self._execute_script(db,
            """
            CREATE INDEX IF NOT EXISTS idx_review_runs_cycle ON review_runs(cycle_id, run_number DESC);
            CREATE INDEX IF NOT EXISTS idx_review_runs_group ON review_runs(run_group_id, project_type);
            CREATE INDEX IF NOT EXISTS idx_review_runs_application ON review_runs(cycle_id, application, run_number DESC);
            CREATE INDEX IF NOT EXISTS idx_discussions_cycle ON discussions(cycle_id, created_at);
            """
        )
        self._backfill_cycles(db)

    def _migrate_v3(self, db: sqlite3.Connection) -> None:
        """Add the application release-line boundary without rewriting v2 data."""
        self._add_column(db, "review_runs", "release_line TEXT NOT NULL DEFAULT ''")
        self._execute_script(
            db,
            """
            CREATE INDEX IF NOT EXISTS idx_review_runs_release_scope
                ON review_runs(cycle_id, application, release_line, run_number DESC);
            """,
        )

    def _backfill_cycles(self, db: sqlite3.Connection) -> None:
        """Create deterministic legacy ownership without altering business records."""
        for issue in db.execute("SELECT jira_key, created_at, updated_at, current_cycle_id FROM review_issues").fetchall():
            jira_key = str(issue["jira_key"])
            cycle = db.execute(
                "SELECT cycle_id FROM review_cycles WHERE jira_key=? ORDER BY cycle_number LIMIT 1", (jira_key,)
            ).fetchone()
            if not cycle:
                cycle_id = str(uuid.uuid4())
                db.execute(
                    """INSERT INTO review_cycles(cycle_id, jira_key, sprint_id, sprint_name, sprint_state,
                       cycle_number, cycle_started_at, review_mode, backfilled, created_at, updated_at)
                       VALUES(?, ?, 'legacy', 'Legacy / Unknown Sprint', 'unknown', 1, ?, 'issue', 1, ?, ?)""",
                    (cycle_id, jira_key, issue["created_at"], issue["created_at"], issue["updated_at"]),
                )
            else:
                cycle_id = str(cycle["cycle_id"])
            if not issue["current_cycle_id"]:
                db.execute("UPDATE review_issues SET current_cycle_id=? WHERE jira_key=?", (cycle_id, jira_key))

            for run in db.execute(
                "SELECT id, created_at, run_group_id FROM review_runs WHERE jira_key=? AND cycle_id IS NULL", (jira_key,)
            ).fetchall():
                group_id = str(run["run_group_id"] or uuid.uuid4())
                if not run["run_group_id"]:
                    db.execute(
                        """INSERT OR IGNORE INTO review_run_groups
                           (id, cycle_id, jira_key, review_mode, status, backfilled, created_at, completed_at)
                           VALUES(?, ?, ?, 'issue', 'completed', 1, ?, ?)""",
                        (group_id, cycle_id, jira_key, run["created_at"], run["created_at"]),
                    )
                db.execute(
                    "UPDATE review_runs SET cycle_id=?, run_group_id=? WHERE id=?", (cycle_id, group_id, run["id"])
                )
            db.execute(
                """UPDATE discussions SET cycle_id=COALESCE(
                       (SELECT cycle_id FROM review_runs WHERE review_runs.id=discussions.run_id), ?)
                   WHERE jira_key=? AND cycle_id IS NULL""",
                (cycle_id, jira_key),
            )
            db.execute(
                """UPDATE pass_records SET cycle_id=COALESCE(
                       (SELECT cycle_id FROM review_runs WHERE review_runs.id=pass_records.run_id), ?)
                   WHERE jira_key=? AND cycle_id IS NULL""",
                (cycle_id, jira_key),
            )
            db.execute(
                """UPDATE pass_records SET run_group_id=(
                       SELECT run_group_id FROM review_runs WHERE review_runs.id=pass_records.run_id)
                   WHERE jira_key=? AND run_group_id IS NULL""",
                (jira_key,),
            )

    @staticmethod
    def _idempotent_result(db: sqlite3.Connection, operation: str, key: str) -> Any | None:
        if not key:
            return None
        row = db.execute(
            "SELECT response_json FROM idempotency_records WHERE operation=? AND idempotency_key=?",
            (operation, key),
        ).fetchone()
        return json.loads(str(row["response_json"])) if row else None

    @staticmethod
    def _remember_idempotent(
        db: sqlite3.Connection, operation: str, key: str, response: Any, created_at: str
    ) -> None:
        if key:
            db.execute(
                """INSERT INTO idempotency_records(operation, idempotency_key, response_json, created_at)
                   VALUES(?, ?, ?, ?)""",
                (operation, key, json.dumps(response, ensure_ascii=False, sort_keys=True), created_at),
            )

    @staticmethod
    def _json(value: Any, default: Any) -> str:
        return json.dumps(default if value is None else value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _scope_signature(value: Any) -> tuple[str, ...]:
        items = value if isinstance(value, list) else []
        return tuple(
            sorted(
                json.dumps(item, ensure_ascii=False, sort_keys=True)
                for item in items
                if isinstance(item, dict)
            )
        )

    def upsert_sprint_membership(
        self,
        *,
        jira_key: str,
        sprint_id: str,
        sprint_name: str = "",
        sprint_state: str = "unknown",
        joined_at: str = "",
        left_at: str = "",
        source: dict[str, Any] | None = None,
        summary: str = "",
        responsible: str = "",
    ) -> dict[str, Any]:
        jira_key = jira_key.strip().upper()
        sprint_id = str(sprint_id).strip()
        if not jira_key or not sprint_id:
            raise ValueError("Jira key and Sprint ID are required.")
        now = utc_now()
        with self._lock, self.connect() as db:
            db.execute(
                """INSERT INTO review_issues(jira_key, summary, responsible, created_at, updated_at)
                   VALUES(?, ?, ?, ?, ?)
                   ON CONFLICT(jira_key) DO UPDATE SET
                     summary=CASE WHEN excluded.summary<>'' THEN excluded.summary ELSE review_issues.summary END,
                     responsible=CASE WHEN excluded.responsible<>'' THEN excluded.responsible ELSE review_issues.responsible END,
                     updated_at=excluded.updated_at""",
                (jira_key, summary.strip(), responsible.strip(), now, now),
            )
            membership_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO sprint_memberships(id, jira_key, sprint_id, sprint_name, sprint_state,
                   joined_at, left_at, source_json, created_at, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(jira_key, sprint_id) DO UPDATE SET
                     sprint_name=CASE WHEN excluded.sprint_name<>'' THEN excluded.sprint_name ELSE sprint_memberships.sprint_name END,
                     sprint_state=excluded.sprint_state,
                     joined_at=COALESCE(sprint_memberships.joined_at, excluded.joined_at),
                     left_at=COALESCE(excluded.left_at, sprint_memberships.left_at),
                     source_json=excluded.source_json,
                     updated_at=excluded.updated_at""",
                (
                    membership_id, jira_key, sprint_id, sprint_name.strip(), sprint_state.strip().lower() or "unknown",
                    joined_at or None, left_at or None, self._json(source, {}), now, now,
                ),
            )
            row = db.execute(
                "SELECT * FROM sprint_memberships WHERE jira_key=? AND sprint_id=?", (jira_key, sprint_id)
            ).fetchone()
            return self._decoded_row(row, ("source_json",))

    def upsert_review_cycle(
        self,
        *,
        jira_key: str,
        sprint_id: str = "",
        sprint_name: str = "",
        sprint_state: str = "unknown",
        review_mode: str = "issue",
        cycle_id: str = "",
        cycle_started_at: str = "",
        cycle_closed_at: str = "",
        status_transition: dict[str, Any] | None = None,
        mr_scope: list[dict[str, Any]] | None = None,
        pass_status: str = "pending",
        release_gate_status: str = "pending",
        backfilled: bool = False,
        summary: str = "",
        responsible: str = "",
    ) -> dict[str, Any]:
        jira_key = jira_key.strip().upper()
        sprint_id = str(sprint_id).strip() or "legacy"
        if not jira_key:
            raise ValueError("Jira key is required.")
        if review_mode not in {"issue", "batch-preview", "final-sprint"}:
            raise ValueError("Review mode must be issue, batch-preview, or final-sprint.")
        now = utc_now()
        with self._lock, self.connect() as db:
            db.execute(
                """INSERT INTO review_issues(jira_key, summary, responsible, created_at, updated_at)
                   VALUES(?, ?, ?, ?, ?)
                   ON CONFLICT(jira_key) DO UPDATE SET
                     summary=CASE WHEN excluded.summary<>'' THEN excluded.summary ELSE review_issues.summary END,
                     responsible=CASE WHEN excluded.responsible<>'' THEN excluded.responsible ELSE review_issues.responsible END,
                     updated_at=excluded.updated_at""",
                (jira_key, summary.strip(), responsible.strip(), now, now),
            )
            existing = None
            if cycle_id:
                existing = db.execute("SELECT * FROM review_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
                if existing and str(existing["jira_key"]) != jira_key:
                    raise ValueError("Review Cycle belongs to a different Jira issue.")
            if not existing:
                existing = db.execute(
                    """SELECT * FROM review_cycles WHERE jira_key=? AND sprint_id=? AND cycle_closed_at IS NULL
                       ORDER BY cycle_number DESC LIMIT 1""",
                    (jira_key, sprint_id),
                ).fetchone()
            created_cycle = not bool(existing)
            scope_changed = False
            if existing:
                cycle_id = str(existing["cycle_id"])
                if mr_scope is not None:
                    scope_changed = self._scope_signature(mr_scope) != self._scope_signature(
                        json.loads(str(existing["mr_scope_json"]) or "[]")
                    )
                db.execute(
                    """UPDATE review_cycles SET sprint_name=CASE WHEN ?<>'' THEN ? ELSE sprint_name END,
                       sprint_state=?, cycle_closed_at=COALESCE(?, cycle_closed_at), status_transition_json=?,
                       review_mode=?, mr_scope_json=?, pass_status=?, release_gate_status=?, updated_at=?
                       WHERE cycle_id=?""",
                    (
                        sprint_name, sprint_name, sprint_state.lower() or "unknown", cycle_closed_at or None,
                        self._json(status_transition, json.loads(str(existing["status_transition_json"]))), review_mode,
                        self._json(mr_scope, json.loads(str(existing["mr_scope_json"]))), pass_status,
                        release_gate_status, now, cycle_id,
                    ),
                )
            else:
                cycle_id = cycle_id or str(uuid.uuid4())
                # A Jira issue has one active delivery cycle. Starting work in a
                # different Sprint closes the previous cycle while preserving it
                # as immutable history for later traceability.
                db.execute(
                    """UPDATE review_cycles SET cycle_closed_at=?, updated_at=?
                       WHERE jira_key=? AND cycle_closed_at IS NULL""",
                    (cycle_started_at or now, now, jira_key),
                )
                cycle_number = int(
                    db.execute("SELECT COUNT(*) FROM review_cycles WHERE jira_key=?", (jira_key,)).fetchone()[0]
                ) + 1
                db.execute(
                    """INSERT INTO review_cycles(cycle_id, jira_key, sprint_id, sprint_name, sprint_state,
                       cycle_number, cycle_started_at, cycle_closed_at, status_transition_json, review_mode,
                       mr_scope_json, pass_status, release_gate_status, backfilled, created_at, updated_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        cycle_id, jira_key, sprint_id, sprint_name.strip(), sprint_state.lower() or "unknown",
                        cycle_number, cycle_started_at or now, cycle_closed_at or None,
                        self._json(status_transition, {}), review_mode, self._json(mr_scope, []), pass_status,
                        release_gate_status, int(backfilled), now, now,
                    ),
                )
            if sprint_id != "legacy" and (created_cycle or scope_changed):
                issue_status = "no-review-required" if pass_status == "not-required" else "not-reviewed"
                db.execute(
                    """UPDATE review_issues SET current_cycle_id=?, status=?, latest_run_id=NULL,
                       passed_run_id=NULL, updated_at=? WHERE jira_key=?""",
                    (cycle_id, issue_status, now, jira_key),
                )
            else:
                db.execute(
                    "UPDATE review_issues SET current_cycle_id=?, updated_at=? WHERE jira_key=?",
                    (cycle_id, now, jira_key),
                )
            if sprint_id != "legacy":
                membership = db.execute(
                    "SELECT id FROM sprint_memberships WHERE jira_key=? AND sprint_id=?", (jira_key, sprint_id)
                ).fetchone()
                if membership:
                    db.execute(
                        """UPDATE sprint_memberships SET sprint_name=?, sprint_state=?, updated_at=? WHERE id=?""",
                        (sprint_name, sprint_state.lower() or "unknown", now, membership["id"]),
                    )
                else:
                    db.execute(
                        """INSERT INTO sprint_memberships(id, jira_key, sprint_id, sprint_name, sprint_state,
                           joined_at, source_json, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, '{}', ?, ?)""",
                        (str(uuid.uuid4()), jira_key, sprint_id, sprint_name, sprint_state.lower() or "unknown", cycle_started_at or now, now, now),
                    )
            row = db.execute("SELECT * FROM review_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
            return self._decoded_row(row, ("status_transition_json", "mr_scope_json"))

    def create_run_group(
        self,
        *,
        cycle_id: str,
        review_mode: str = "issue",
        status: str = "running",
        stable_fingerprint: str = "",
        run_group_id: str = "",
        created_at: str = "",
    ) -> dict[str, Any]:
        now = created_at or utc_now()
        with self._lock, self.connect() as db:
            cycle = db.execute("SELECT jira_key FROM review_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
            if not cycle:
                raise KeyError("Review Cycle was not found.")
            if run_group_id:
                existing = db.execute("SELECT * FROM review_run_groups WHERE id=?", (run_group_id,)).fetchone()
                if existing:
                    if str(existing["cycle_id"]) != cycle_id:
                        raise ValueError("Run Group belongs to a different Review Cycle.")
                    return self._row(existing)
            run_group_id = run_group_id or str(uuid.uuid4())
            db.execute(
                """INSERT INTO review_run_groups(id, cycle_id, jira_key, review_mode, status,
                   stable_fingerprint, created_at, completed_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_group_id, cycle_id, cycle["jira_key"], review_mode, status, stable_fingerprint,
                    now, now if status == "completed" else None,
                ),
            )
            return self._row(db.execute("SELECT * FROM review_run_groups WHERE id=?", (run_group_id,)).fetchone())

    def list_cycles(self, jira_key: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                self._decoded_row(row, ("status_transition_json", "mr_scope_json"))
                for row in db.execute(
                    "SELECT * FROM review_cycles WHERE jira_key=? ORDER BY cycle_number DESC", (jira_key.upper(),)
                )
            ]

    def list_sprint_memberships(self, jira_key: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                self._decoded_row(row, ("source_json",))
                for row in db.execute(
                    "SELECT * FROM sprint_memberships WHERE jira_key=? ORDER BY joined_at, created_at", (jira_key.upper(),)
                )
            ]

    def reconcile_sprint_scope(
        self,
        *,
        sprint_ref: str,
        issues: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Reconcile one live Jira Sprint scan into persisted Review Cycles.

        ``issues`` is the complete reviewable Jira membership returned by the
        selected Sprint query. Its MR list is authoritative, including an empty
        list when previously linked MRs are now closed or otherwise out of
        review scope.
        """
        ref = sprint_ref.strip()
        if not ref:
            raise ValueError("Sprint reference is required.")
        now = utc_now()
        current_jira_keys: set[str] = set()
        selected_sprint_ids: set[str] = set()
        selected_sprint_names: set[str] = set()
        updated_cycles: list[str] = []
        for item in issues:
            if not isinstance(item, dict):
                continue
            jira_key = str(item.get("jira_key") or "").strip().upper()
            if not jira_key:
                continue
            memberships = [
                membership for membership in (item.get("sprint_memberships") or [])
                if isinstance(membership, dict)
            ]
            selected = next(
                (
                    membership for membership in memberships
                    if str(membership.get("id") or "").strip() == ref
                    or str(membership.get("name") or "").strip().casefold() == ref.casefold()
                ),
                None,
            )
            if not selected:
                selected = next(
                    (
                        membership for membership in memberships
                        if str(membership.get("id") or "").strip()
                        == str(item.get("current_sprint_id") or "").strip()
                    ),
                    memberships[-1] if memberships else {},
                )
            sprint_id = str(selected.get("id") or item.get("current_sprint_id") or ref).strip()
            sprint_name = str(selected.get("name") or item.get("sprint_name") or ref).strip()
            sprint_state = str(selected.get("state") or item.get("current_sprint_state") or "unknown")
            current_jira_keys.add(jira_key)
            selected_sprint_ids.add(sprint_id)
            selected_sprint_names.add(sprint_name.casefold())
            self.upsert_sprint_membership(
                jira_key=jira_key,
                sprint_id=sprint_id,
                sprint_name=sprint_name,
                sprint_state=sprint_state,
                joined_at=str(selected.get("joined_at") or ""),
                source=selected,
                summary=str(item.get("summary") or ""),
            )
            existing_cycle = next(
                (
                    cycle for cycle in self.list_cycles(jira_key)
                    if str(cycle.get("sprint_id") or "") == sprint_id
                    and not cycle.get("cycle_closed_at")
                ),
                {},
            )
            mr_scope = [scope for scope in (item.get("mr_scope") or []) if isinstance(scope, dict)]
            same_scope = self._scope_signature(existing_cycle.get("mr_scope")) == self._scope_signature(mr_scope)
            cycle_pass_status = (
                "not-required"
                if not mr_scope
                else str(existing_cycle.get("pass_status") or "pending") if same_scope
                else "pending"
            )
            cycle = self.upsert_review_cycle(
                jira_key=jira_key,
                sprint_id=sprint_id,
                sprint_name=sprint_name,
                sprint_state=sprint_state,
                review_mode=str(existing_cycle.get("review_mode") or "issue"),
                status_transition={
                    **(
                        existing_cycle.get("status_transition")
                        if isinstance(existing_cycle.get("status_transition"), dict)
                        else {}
                    ),
                    "scope_authoritative": True,
                    "scope_source": "sprint-scan",
                    "scope_reconciled_at": now,
                },
                mr_scope=mr_scope,
                pass_status=cycle_pass_status,
                release_gate_status=str(existing_cycle.get("release_gate_status") or "pending") if same_scope else "pending",
                summary=str(item.get("summary") or ""),
            )
            updated_cycles.append(str(cycle.get("cycle_id") or ""))

        closed_cycles: list[str] = []
        with self._lock, self.connect() as db:
            candidates = db.execute(
                "SELECT cycle_id, jira_key, sprint_id, sprint_name FROM review_cycles WHERE cycle_closed_at IS NULL"
            ).fetchall()
            for cycle in candidates:
                matches_selected_sprint = (
                    str(cycle["sprint_id"] or "") in selected_sprint_ids
                    or str(cycle["sprint_name"] or "").casefold() in selected_sprint_names
                    or str(cycle["sprint_id"] or "") == ref
                    or str(cycle["sprint_name"] or "").casefold() == ref.casefold()
                )
                if not matches_selected_sprint or str(cycle["jira_key"] or "") in current_jira_keys:
                    continue
                cycle_id = str(cycle["cycle_id"])
                db.execute(
                    "UPDATE review_cycles SET cycle_closed_at=?, sprint_state='left', updated_at=? WHERE cycle_id=?",
                    (now, now, cycle_id),
                )
                db.execute(
                    "UPDATE sprint_memberships SET left_at=COALESCE(left_at, ?), updated_at=? WHERE jira_key=? AND sprint_id=?",
                    (now, now, cycle["jira_key"], cycle["sprint_id"]),
                )
                issue = db.execute(
                    "SELECT current_cycle_id FROM review_issues WHERE jira_key=?", (cycle["jira_key"],)
                ).fetchone()
                if issue and str(issue["current_cycle_id"] or "") == cycle_id:
                    db.execute(
                        "UPDATE review_issues SET status='not-reviewed', current_cycle_id=NULL, updated_at=? WHERE jira_key=?",
                        (now, cycle["jira_key"]),
                    )
                closed_cycles.append(cycle_id)
        return {
            "sprint_ref": ref,
            "issue_count": len(current_jira_keys),
            "updated_cycles": updated_cycles,
            "closed_cycles": closed_cycles,
        }

    def register_run(
        self,
        *,
        jira_key: str,
        report_path: str,
        findings: list[dict[str, Any]],
        summary: str = "",
        responsible: str = "",
        conclusion: str = "",
        created_at: str = "",
        cycle_id: str = "",
        run_group_id: str = "",
        project_type: str = "",
        application: str = "",
        release_line: str = "",
        responsible_scope: object = "",
        mr_fingerprint: str = "",
        stable_fingerprint: str = "",
    ) -> str:
        jira_key = jira_key.strip().upper()
        if not jira_key:
            raise ValueError("Jira key is required.")
        timestamp = created_at or utc_now()
        report_identity = report_fingerprint(report_path)
        if isinstance(responsible_scope, str):
            persisted_responsible_scope = responsible_scope.strip()
        else:
            persisted_responsible_scope = json.dumps(
                sorted(scope_people(responsible_scope), key=str.casefold),
                ensure_ascii=False,
            )
        if not scope_people(persisted_responsible_scope):
            persisted_responsible_scope = responsible.strip()
        with self._lock, self.connect() as db:
            db.execute(
                """INSERT INTO review_issues(jira_key, summary, responsible, status, created_at, updated_at)
                   VALUES(?, ?, ?, 'handling', ?, ?)
                   ON CONFLICT(jira_key) DO UPDATE SET
                     summary=CASE WHEN excluded.summary<>'' THEN excluded.summary ELSE review_issues.summary END,
                     responsible=CASE
                       WHEN review_issues.responsible='' AND excluded.responsible<>''
                       THEN excluded.responsible ELSE review_issues.responsible END,
                     updated_at=excluded.updated_at""",
                (jira_key, summary, responsible, timestamp, timestamp),
            )
            existing = db.execute(
                "SELECT id FROM review_runs WHERE jira_key=? AND report_fingerprint=?",
                (jira_key, report_identity),
            ).fetchone()
            if existing:
                return str(existing["id"])
            cycle = db.execute(
                "SELECT * FROM review_cycles WHERE cycle_id=?", (cycle_id,)
            ).fetchone() if cycle_id else None
            if cycle_id and not cycle:
                raise KeyError("Review Cycle was not found.")
            if cycle and str(cycle["jira_key"]) != jira_key:
                raise ValueError("Review Cycle belongs to a different Jira issue.")
            if not cycle:
                cycle = db.execute(
                    """SELECT * FROM review_cycles WHERE jira_key=? AND cycle_closed_at IS NULL
                       ORDER BY cycle_number DESC LIMIT 1""",
                    (jira_key,),
                ).fetchone()
            if not cycle:
                cycle_id = str(uuid.uuid4())
                db.execute(
                    """INSERT INTO review_cycles(cycle_id, jira_key, sprint_id, sprint_name, sprint_state,
                       cycle_number, cycle_started_at, review_mode, backfilled, created_at, updated_at)
                       VALUES(?, ?, 'legacy', 'Legacy / Unknown Sprint', 'unknown', 1, ?, 'issue', 1, ?, ?)""",
                    (cycle_id, jira_key, timestamp, timestamp, timestamp),
                )
            else:
                cycle_id = str(cycle["cycle_id"])
            if run_group_id:
                group = db.execute("SELECT * FROM review_run_groups WHERE id=?", (run_group_id,)).fetchone()
                if not group:
                    raise KeyError("Review Run Group was not found.")
                if str(group["cycle_id"]) != cycle_id:
                    raise ValueError("Review Run Group belongs to a different Review Cycle.")
            else:
                run_group_id = str(uuid.uuid4())
                db.execute(
                    """INSERT INTO review_run_groups(id, cycle_id, jira_key, review_mode, status,
                       stable_fingerprint, created_at, completed_at) VALUES(?, ?, ?, ?, 'completed', ?, ?, ?)""",
                    (
                        run_group_id, cycle_id, jira_key, str(cycle["review_mode"]) if cycle else "issue",
                        stable_fingerprint, timestamp, timestamp,
                    ),
                )
            run_number = int(db.execute("SELECT COUNT(*) FROM review_runs WHERE jira_key=?", (jira_key,)).fetchone()[0]) + 1
            run_id = str(uuid.uuid4())
            severity_counts: dict[str, int] = {}
            for finding in findings:
                severity = str(finding.get("severity") or "Unknown").title()
                severity_counts[severity] = severity_counts.get(severity, 0) + 1
            db.execute(
                """INSERT INTO review_runs(id, jira_key, report_path, report_fingerprint, run_number, conclusion,
                   severity_counts_json, created_at, cycle_id, run_group_id, project_type, application, release_line,
                   responsible_scope, mr_fingerprint, stable_fingerprint)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id, jira_key, report_path, report_identity, run_number, conclusion,
                    json.dumps(severity_counts), timestamp, cycle_id, run_group_id, project_type.strip().lower(),
                    application.strip(), release_line.strip(), persisted_responsible_scope,
                    mr_fingerprint.strip(), stable_fingerprint.strip(),
                ),
            )
            previous = db.execute(
                """SELECT id FROM review_runs
                   WHERE jira_key=? AND cycle_id=? AND application=? AND release_line=? AND id<>?
                   ORDER BY run_number DESC LIMIT 1""",
                (jira_key, cycle_id, application.strip(), release_line.strip(), run_id),
            ).fetchone()
            previous_fingerprints: set[str] = set()
            if previous:
                previous_fingerprints = {
                    str(row["fingerprint"])
                    for row in db.execute("SELECT fingerprint FROM findings WHERE run_id=?", (previous["id"],)).fetchall()
                }
            current_fingerprints: set[str] = set()
            for position, finding in enumerate(findings, 1):
                fingerprint = finding_fingerprint(jira_key, finding)
                current_fingerprints.add(fingerprint)
                db.execute(
                    """INSERT INTO findings(id, run_id, jira_key, fingerprint, report_index, severity, title,
                       file_path, details_json, lineage_state, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()), run_id, jira_key, fingerprint, str(finding.get("index") or position),
                        str(finding.get("severity") or "Unknown").title(), str(finding.get("title") or "Untitled finding"),
                        str(finding.get("file") or finding.get("path") or ""), json.dumps(finding, ensure_ascii=False),
                        "persisting" if fingerprint in previous_fingerprints else "new", timestamp,
                    ),
                )
            blocking = blocking_severities()
            blocking_count = sum(1 for item in findings if str(item.get("severity") or "").title() in blocking)
            status = "handling" if blocking_count else "ready-for-pass"
            if previous:
                unresolved_previous = previous_fingerprints & current_fingerprints
                status = "handling" if unresolved_previous or blocking_count else "ready-for-pass"
            db.execute(
                """UPDATE review_issues SET latest_run_id=?, passed_run_id=NULL, current_cycle_id=?,
                   status=?, updated_at=? WHERE jira_key=?""",
                (run_id, cycle_id, status, timestamp, jira_key),
            )
            db.execute("UPDATE review_cycles SET pass_status='pending', updated_at=? WHERE cycle_id=?", (timestamp, cycle_id))
            self._audit(db, jira_key, "system", "review-run-registered", {"run_id": run_id, "run_number": run_number})
            return run_id

    def has_registered_run(self, jira_key: str, report_path: str) -> bool:
        fingerprint = report_fingerprint(report_path)
        with self.connect() as db:
            return db.execute(
                "SELECT 1 FROM review_runs WHERE jira_key=? AND report_fingerprint=?",
                (jira_key.strip().upper(), fingerprint),
            ).fetchone() is not None

    def registered_run_fingerprints(self) -> set[tuple[str, str]]:
        with self.connect() as db:
            return {
                (str(row["jira_key"]), str(row["report_fingerprint"]))
                for row in db.execute("SELECT jira_key, report_fingerprint FROM review_runs")
            }

    def record_handling(
        self,
        *,
        finding_id: str,
        disposition: str,
        note: str,
        actor: str,
        actor_role: str,
        jira_summary: str = "",
        jira_description_adf: object | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        disposition = disposition.strip().lower()
        if disposition not in DISPOSITIONS:
            raise ValueError("Handling result must be fixed, follow-up, or not-issue.")
        if not note.strip():
            raise ValueError("Handling explanation is required.")
        if disposition == "follow-up":
            if not jira_summary.strip():
                raise ValueError("Issue Summary is required for a Jira follow-up.")
            document = validate_adf(jira_description_adf or empty_adf())
            if not adf_plain_text(document):
                raise ValueError("Issue Description is required for a Jira follow-up.")
        now = utc_now()
        with self._lock, self.connect() as db:
            operation = f"handling:{finding_id}"
            repeated = self._idempotent_result(db, operation, idempotency_key)
            if repeated is not None:
                return dict(repeated)
            finding = db.execute("SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()
            if not finding:
                raise KeyError("Finding was not found.")
            approval_status = "pending" if disposition == "not-issue" and actor_role == "developer" else "approved"
            handling_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO finding_handlings(id, finding_id, disposition, note, submitted_by, approval_status,
                   approved_by, approved_at, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    handling_id, finding_id, disposition, note.strip(), actor, approval_status,
                    actor if approval_status == "approved" else None, now if approval_status == "approved" else None, now, now,
                ),
            )
            draft_id = ""
            if disposition == "follow-up":
                draft_id = str(uuid.uuid4())
                db.execute(
                    """INSERT INTO jira_drafts(id, jira_key, source_finding_id, summary, description_adf_json,
                       created_by, updated_by, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (draft_id, finding["jira_key"], finding_id, jira_summary.strip(), adf_json(jira_description_adf), actor, actor, now, now),
                )
            status = (
                "rescan-required"
                if disposition == "fixed" and str(finding["severity"]).title() in blocking_severities()
                else "handling"
            )
            db.execute("UPDATE review_issues SET status=?, updated_at=? WHERE jira_key=?", (status, now, finding["jira_key"]))
            self._audit(
                db, str(finding["jira_key"]), actor, "finding-handled",
                {"finding_id": finding_id, "handling_id": handling_id, "disposition": disposition, "draft_id": draft_id},
            )
            result = {"handling_id": handling_id, "draft_id": draft_id, "approval_status": approval_status}
            self._remember_idempotent(db, operation, idempotency_key, result, now)
            return result

    def approve_handling(self, handling_id: str, actor: str, actor_role: str, *, approved: bool, reason: str = "") -> None:
        if actor_role not in {"auditor", "manager"}:
            raise PermissionError("Only Auditor or Manager can approve handling results.")
        now = utc_now()
        status = "approved" if approved else "rejected"
        with self._lock, self.connect() as db:
            row = db.execute(
                "SELECT h.*, f.jira_key FROM finding_handlings h JOIN findings f ON f.id=h.finding_id WHERE h.id=?",
                (handling_id,),
            ).fetchone()
            if not row:
                raise KeyError("Handling result was not found.")
            db.execute(
                "UPDATE finding_handlings SET approval_status=?, approved_by=?, approved_at=?, override_reason=?, updated_at=? WHERE id=?",
                (status, actor, now, reason.strip(), now, handling_id),
            )
            self._audit(db, str(row["jira_key"]), actor, f"handling-{status}", {"handling_id": handling_id, "reason": reason})

    def manager_override(self, handling_id: str, actor: str, reason: str) -> None:
        if not reason.strip():
            raise ValueError("Manager override reason is required.")
        now = utc_now()
        with self._lock, self.connect() as db:
            row = db.execute(
                """SELECT h.*, f.jira_key, f.severity FROM finding_handlings h
                   JOIN findings f ON f.id=h.finding_id WHERE h.id=?""",
                (handling_id,),
            ).fetchone()
            if not row:
                raise KeyError("Handling result was not found.")
            if row["disposition"] != "follow-up":
                raise ValueError("Manager override only applies to Jira follow-up handling.")
            draft = db.execute("SELECT id FROM jira_drafts WHERE source_finding_id=? AND status='pending-create'", (row["finding_id"],)).fetchone()
            if not draft:
                raise ValueError("A pending Jira draft is required before Manager override.")
            db.execute(
                """UPDATE finding_handlings SET approval_status='approved', approved_by=?, approved_at=?,
                   manager_override=1, override_reason=?, updated_at=? WHERE id=?""",
                (actor, now, reason.strip(), now, handling_id),
            )
            self._audit(
                db, str(row["jira_key"]), actor, "manager-exception",
                {"handling_id": handling_id, "reason": reason.strip(), "severity": row["severity"], "draft_id": draft["id"]},
            )

    def pass_readiness(self, jira_key: str, cycle_id: str = "") -> dict[str, Any]:
        with self.connect() as db:
            return self._pass_readiness(db, jira_key, cycle_id)

    def manual_pass(
        self,
        jira_key: str,
        actor: str,
        actor_role: str,
        note: str,
        *,
        cycle_id: str = "",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        if actor_role not in {"auditor", "manager"}:
            raise PermissionError("Only Auditor or Manager can record Review Pass.")
        requested_cycle_id = cycle_id.strip()
        operation = f"manual-pass:{jira_key.upper()}:{requested_cycle_id or 'current'}"
        if idempotency_key:
            with self._lock, self.connect() as db:
                repeated = self._idempotent_result(db, operation, idempotency_key)
                if repeated is not None:
                    return dict(repeated)
        readiness = self.pass_readiness(jira_key, requested_cycle_id)
        if not readiness["ready"]:
            raise ValueError(str(readiness["message"]))
        now = utc_now()
        pass_id = str(uuid.uuid4())
        with self._lock, self.connect() as db:
            repeated = self._idempotent_result(db, operation, idempotency_key)
            if repeated is not None:
                return dict(repeated)
            issue = db.execute(
                "SELECT current_cycle_id FROM review_issues WHERE jira_key=?", (jira_key.upper(),)
            ).fetchone()
            current_cycle_id = str(issue["current_cycle_id"] or "") if issue else ""
            target_cycle_id = str(readiness.get("cycle_id") or "")
            if not target_cycle_id or target_cycle_id != current_cycle_id:
                raise ValueError("Only the current Review Cycle can be marked Pass.")
            cycle = db.execute(
                "SELECT cycle_closed_at FROM review_cycles WHERE cycle_id=? AND jira_key=?",
                (target_cycle_id, jira_key.upper()),
            ).fetchone()
            if not cycle or cycle["cycle_closed_at"]:
                raise ValueError("Historical or closed Review Cycles are read-only.")
            db.execute(
                """INSERT INTO pass_records(id, jira_key, run_id, actor, note, policy_json, created_at,
                   cycle_id, run_group_id) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    pass_id, jira_key.upper(), readiness["run_id"], actor, note.strip(),
                    json.dumps(readiness, ensure_ascii=False), now, target_cycle_id,
                    readiness.get("run_group_id") or None,
                ),
            )
            db.execute(
                "UPDATE review_issues SET status='passed', passed_run_id=?, updated_at=? WHERE jira_key=?",
                (readiness["run_id"], now, jira_key.upper()),
            )
            db.execute(
                "UPDATE review_cycles SET pass_status='passed', updated_at=? WHERE cycle_id=?",
                (now, target_cycle_id),
            )
            self._audit(
                db, jira_key.upper(), actor, "manual-pass",
                {"pass_id": pass_id, "run_id": readiness["run_id"], "cycle_id": target_cycle_id},
            )
            result = {"pass_id": pass_id, **readiness}
            self._remember_idempotent(db, operation, idempotency_key, result, now)
            return result

    def add_discussion(
        self,
        jira_key: str,
        actor: str,
        message: str,
        *,
        cycle_id: str = "",
        run_id: str = "",
        finding_id: str = "",
        kind: str = "comment",
        idempotency_key: str = "",
    ) -> str:
        if not message.strip():
            raise ValueError("Message is required.")
        discussion_id = str(uuid.uuid4())
        with self._lock, self.connect() as db:
            operation = f"discussion:{jira_key.upper()}"
            repeated = self._idempotent_result(db, operation, idempotency_key)
            if repeated is not None:
                return str(repeated)
            if not cycle_id and run_id:
                run = db.execute("SELECT cycle_id, jira_key FROM review_runs WHERE id=?", (run_id,)).fetchone()
                if not run:
                    raise KeyError("Review Run was not found.")
                if str(run["jira_key"]) != jira_key.upper():
                    raise ValueError("Review Run belongs to a different Jira issue.")
                cycle_id = str(run["cycle_id"] or "")
            if not cycle_id:
                issue = db.execute(
                    "SELECT current_cycle_id FROM review_issues WHERE jira_key=?", (jira_key.upper(),)
                ).fetchone()
                if not issue:
                    raise KeyError("Jira issue was not found in Review Workflow.")
                cycle_id = str(issue["current_cycle_id"] or "")
            db.execute(
                """INSERT INTO discussions(id, jira_key, run_id, finding_id, author, kind, message, created_at, cycle_id)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (discussion_id, jira_key.upper(), run_id or None, finding_id or None, actor, kind, message.strip(), utc_now(), cycle_id or None),
            )
            self._remember_idempotent(db, operation, idempotency_key, discussion_id, utc_now())
        return discussion_id

    def create_description_snapshot(
        self,
        *,
        cycle_id: str,
        source_type: str,
        reason: str,
        adf_document: object | None = None,
        rendered_html: str = "",
        plain_text: str = "",
        source_id: str = "",
        author: str = "",
        source_created_at: str = "",
        source_updated_at: str = "",
        template_language: str = "",
        issue_type: str = "",
        attachments: list[dict[str, Any]] | None = None,
        jira_status: str = "",
        code_mrs: list[dict[str, Any]] | None = None,
        deferred_mrs: list[dict[str, Any]] | None = None,
        backfilled: bool = False,
        captured_at: str = "",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        if not source_type.strip() or not reason.strip():
            raise ValueError("Description source type and snapshot reason are required.")
        document = validate_adf(adf_document or empty_adf())
        normalized_adf = json.loads(adf_json(document))
        text_index = plain_text or adf_plain_text(document)
        immutable = {
            "adf": normalized_adf,
            "rendered_html": rendered_html,
            "plain_text": text_index,
            "attachments": attachments or [],
            "jira_status": jira_status,
            "code_mrs": code_mrs or [],
            "deferred_mrs": deferred_mrs or [],
            "source_updated_at": source_updated_at,
        }
        content_hash = hashlib.sha256(self._json(immutable, {}).encode("utf-8")).hexdigest()
        now = captured_at or utc_now()
        operation = f"description-snapshot:{cycle_id}"
        with self._lock, self.connect() as db:
            repeated = self._idempotent_result(db, operation, idempotency_key)
            if repeated is not None:
                return dict(repeated)
            cycle = db.execute("SELECT * FROM review_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
            if not cycle:
                raise KeyError("Review Cycle was not found.")
            version = int(
                db.execute(
                    """SELECT COALESCE(MAX(version), 0) FROM description_snapshots
                       WHERE cycle_id=? AND source_type=? AND source_id=?""",
                    (cycle_id, source_type.strip(), source_id.strip()),
                ).fetchone()[0]
            ) + 1
            snapshot_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO description_snapshots(id, cycle_id, jira_key, sprint_id, source_type,
                   source_id, version, adf_json, rendered_html, plain_text, author, source_created_at,
                   source_updated_at, template_language, issue_type, attachments_json, jira_status,
                   code_mrs_json, deferred_mrs_json, reason, content_hash, backfilled, captured_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_id, cycle_id, cycle["jira_key"], cycle["sprint_id"], source_type.strip(),
                    source_id.strip(), version, self._json(normalized_adf, {}), rendered_html, text_index,
                    author, source_created_at or None, source_updated_at or None, template_language,
                    issue_type, self._json(attachments, []), jira_status, self._json(code_mrs, []),
                    self._json(deferred_mrs, []), reason.strip(), content_hash, int(backfilled), now,
                ),
            )
            db.execute(
                """UPDATE review_cycles SET current_description_snapshot_id=?, updated_at=? WHERE cycle_id=?""",
                (snapshot_id, now, cycle_id),
            )
            result = self._description_snapshot(db, snapshot_id)
            self._remember_idempotent(db, operation, idempotency_key, result, now)
            return result

    def create_review_snapshot(
        self,
        *,
        cycle_id: str,
        reason: str,
        actor: str,
        payload: dict[str, Any] | None = None,
        created_at: str = "",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        if not reason.strip() or not actor.strip():
            raise ValueError("Snapshot reason and actor are required.")
        now = created_at or utc_now()
        operation = f"review-snapshot:{cycle_id}"
        with self._lock, self.connect() as db:
            repeated = self._idempotent_result(db, operation, idempotency_key)
            if repeated is not None:
                return dict(repeated)
            cycle = db.execute("SELECT * FROM review_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
            if not cycle:
                raise KeyError("Review Cycle was not found.")
            snapshot_payload = payload if payload is not None else self._review_snapshot_payload(db, cycle)
            encoded = self._json(snapshot_payload, {})
            content_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            revision = int(
                db.execute(
                    "SELECT COALESCE(MAX(revision), 0) FROM review_snapshots WHERE cycle_id=?", (cycle_id,)
                ).fetchone()[0]
            ) + 1
            snapshot_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO review_snapshots(id, cycle_id, jira_key, revision, reason, payload_json,
                   content_hash, actor, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_id, cycle_id, cycle["jira_key"], revision, reason.strip(), encoded,
                    content_hash, actor.strip(), now,
                ),
            )
            result = self._review_snapshot(db, snapshot_id)
            self._remember_idempotent(db, operation, idempotency_key, result, now)
            return result

    def upsert_deferred_resource(
        self,
        *,
        cycle_id: str,
        gitlab_project: str,
        mr_iid: str | int,
        head_sha: str,
        resource_type: str,
        jira_key: str = "",
        sprint_id: str = "",
        mr_url: str = "",
        status: str = "",
        gate_run_id: str = "",
        locked_build_commit: str = "",
        evidence: dict[str, Any] | None = None,
        verified_by: str = "",
        verified_at: str = "",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        project = gitlab_project.strip().rstrip("/")
        iid = str(mr_iid).strip().lstrip("!")
        sha = head_sha.strip().lower()
        kind = resource_type.strip().lower().replace("-", "_")
        if kind not in {"company_config", "scr"}:
            raise ValueError("Deferred resource type must be company_config or scr.")
        if not cycle_id or not project or not iid or not sha:
            raise ValueError("Cycle, GitLab project, MR IID, and Head SHA are required.")
        if status and status not in {"pending", "verified", "blocked", "superseded"}:
            raise ValueError("Deferred resource status is invalid.")
        now = utc_now()
        operation = f"deferred-upsert:{cycle_id}:{project}:{iid}:{sha}"
        with self._lock, self.connect() as db:
            repeated = self._idempotent_result(db, operation, idempotency_key)
            if repeated is not None:
                return dict(repeated)
            cycle = db.execute("SELECT * FROM review_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
            if not cycle:
                raise KeyError("Review Cycle was not found.")
            effective_jira = jira_key.strip().upper() or str(cycle["jira_key"])
            effective_sprint = str(sprint_id).strip() or str(cycle["sprint_id"])
            if effective_jira != str(cycle["jira_key"]) or effective_sprint != str(cycle["sprint_id"]):
                raise ValueError("Deferred resource scope does not match its Review Cycle.")
            resource_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO deferred_release_resources(id, jira_key, sprint_id, cycle_id, gitlab_project,
                   mr_iid, head_sha, resource_type, mr_url, status, gate_run_id, locked_build_commit,
                   evidence_json, verified_by, verified_at, created_at, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(jira_key, sprint_id, cycle_id, gitlab_project, mr_iid, head_sha) DO UPDATE SET
                     resource_type=excluded.resource_type,
                     mr_url=CASE WHEN excluded.mr_url<>'' THEN excluded.mr_url ELSE deferred_release_resources.mr_url END,
                     status=CASE WHEN ?<>'' THEN excluded.status ELSE deferred_release_resources.status END,
                     gate_run_id=CASE WHEN excluded.gate_run_id<>'' THEN excluded.gate_run_id ELSE deferred_release_resources.gate_run_id END,
                     locked_build_commit=CASE WHEN excluded.locked_build_commit<>'' THEN excluded.locked_build_commit ELSE deferred_release_resources.locked_build_commit END,
                     evidence_json=CASE WHEN ? IS NOT NULL THEN excluded.evidence_json ELSE deferred_release_resources.evidence_json END,
                     verified_by=CASE WHEN excluded.verified_by<>'' THEN excluded.verified_by ELSE deferred_release_resources.verified_by END,
                     verified_at=COALESCE(excluded.verified_at, deferred_release_resources.verified_at),
                     updated_at=excluded.updated_at""",
                (
                    resource_id, effective_jira, effective_sprint, cycle_id, project, iid, sha, kind, mr_url,
                    status or "pending", gate_run_id, locked_build_commit, self._json(evidence, {}), verified_by,
                    verified_at or None, now, now, status, int(evidence is not None),
                ),
            )
            row = db.execute(
                """SELECT * FROM deferred_release_resources WHERE jira_key=? AND sprint_id=? AND cycle_id=?
                   AND gitlab_project=? AND mr_iid=? AND head_sha=?""",
                (effective_jira, effective_sprint, cycle_id, project, iid, sha),
            ).fetchone()
            self._refresh_release_gate_status(db, cycle_id, now)
            result = self._decoded_row(row, ("evidence_json",))
            self._remember_idempotent(db, operation, idempotency_key, result, now)
            return result

    def list_deferred_resources(
        self,
        *,
        cycle_id: str = "",
        sprint_id: str = "",
        pending_only: bool = False,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if cycle_id:
            where.append("cycle_id=?")
            params.append(cycle_id)
        if sprint_id:
            where.append("sprint_id=?")
            params.append(str(sprint_id))
        if pending_only:
            where.append("status='pending'")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self.connect() as db:
            return [
                self._decoded_row(row, ("evidence_json",))
                for row in db.execute(
                    f"SELECT * FROM deferred_release_resources {clause} ORDER BY created_at", params
                )
            ]

    def list_description_snapshots(self, cycle_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                self._description_snapshot(db, str(row["id"]))
                for row in db.execute(
                    "SELECT id FROM description_snapshots WHERE cycle_id=? ORDER BY captured_at, version", (cycle_id,)
                )
            ]

    def list_review_snapshots(self, cycle_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                self._review_snapshot(db, str(row["id"]))
                for row in db.execute(
                    "SELECT id FROM review_snapshots WHERE cycle_id=? ORDER BY revision", (cycle_id,)
                )
            ]

    def import_legacy_thread(self, report_suffix: str, thread: dict[str, Any]) -> dict[str, int]:
        suffix = report_suffix.replace("\\", "/").lstrip("/").lower()
        imported = {"handlings": 0, "discussions": 0, "passes": 0}
        if not suffix:
            return imported
        with self._lock, self.connect() as db:
            run = next(
                (
                    row for row in db.execute(
                        "SELECT id, jira_key, report_path, cycle_id, run_group_id FROM review_runs ORDER BY created_at DESC"
                    )
                    if str(row["report_path"]).replace("\\", "/").lower().endswith(suffix)
                ),
                None,
            )
            if not run:
                return imported
            findings = {
                str(row["report_index"]): row
                for row in db.execute("SELECT id, report_index FROM findings WHERE run_id=?", (run["id"],))
            }
            handling_results = thread.get("handling_results") if isinstance(thread.get("handling_results"), dict) else {}
            for index, payload in handling_results.items():
                if not isinstance(payload, dict) or str(index) not in findings:
                    continue
                disposition = str(payload.get("disposition") or "")
                if disposition not in DISPOSITIONS:
                    continue
                finding_id = str(findings[str(index)]["id"])
                exists = db.execute("SELECT 1 FROM finding_handlings WHERE finding_id=?", (finding_id,)).fetchone()
                if exists:
                    continue
                created = str(payload.get("time") or utc_now())
                db.execute(
                    """INSERT INTO finding_handlings(id, finding_id, disposition, note, submitted_by, approval_status,
                       approved_by, approved_at, created_at, updated_at) VALUES(?, ?, ?, ?, ?, 'approved', ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()), finding_id, disposition, str(payload.get("note") or "Legacy handling result"),
                        str(payload.get("user") or "legacy"), str(payload.get("user") or "legacy"), created, created, created,
                    ),
                )
                imported["handlings"] += 1
            messages = thread.get("messages") if isinstance(thread.get("messages"), list) else []
            for message in messages:
                if not isinstance(message, dict) or message.get("kind") == "handling-result":
                    continue
                text = str(message.get("message") or "").strip()
                if not text:
                    continue
                created = str(message.get("time") or utc_now())
                exists = db.execute(
                    "SELECT 1 FROM discussions WHERE jira_key=? AND author=? AND message=? AND created_at=?",
                    (run["jira_key"], str(message.get("user") or "legacy"), text, created),
                ).fetchone()
                if not exists:
                    db.execute(
                        """INSERT INTO discussions(id, jira_key, run_id, author, kind, message, created_at, cycle_id)
                           VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            str(uuid.uuid4()), run["jira_key"], run["id"],
                            str(message.get("user") or "legacy"), str(message.get("kind") or "comment"),
                            text, created, run["cycle_id"],
                        ),
                    )
                    imported["discussions"] += 1
                if message.get("kind") == "manual-pass":
                    passed = db.execute("SELECT 1 FROM pass_records WHERE jira_key=? AND run_id=?", (run["jira_key"], run["id"])).fetchone()
                    if not passed:
                        db.execute(
                            """INSERT INTO pass_records(id, jira_key, run_id, actor, note, policy_json, created_at,
                               cycle_id, run_group_id) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                str(uuid.uuid4()), run["jira_key"], run["id"],
                                str(message.get("user") or "legacy"), text, '{"legacy":true}', created,
                                run["cycle_id"], run["run_group_id"],
                            ),
                        )
                        db.execute(
                            "UPDATE review_issues SET status='passed', passed_run_id=?, updated_at=? WHERE jira_key=?",
                            (run["id"], created, run["jira_key"]),
                        )
                        imported["passes"] += 1
        return imported

    @staticmethod
    def _issue_scope_people(
        db: sqlite3.Connection, jira_key: str, legacy_responsible: object = ""
    ) -> set[str]:
        people = scope_people(legacy_responsible)
        for row in db.execute(
            "SELECT responsible_scope FROM review_runs WHERE jira_key=?",
            (jira_key,),
        ):
            people.update(scope_people(row["responsible_scope"]))
        return people

    @staticmethod
    def _latest_runs_per_scope(run_rows: list[Any]) -> list[Any]:
        """Keep one authoritative report per application/release scope.

        A Run Group may contain sibling reports for different applications, but
        a re-scan of the same application scope supersedes its earlier report.
        Historic imports have occasionally reused a Run Group id; summing every
        row in that group makes stale findings and report counts look current.
        """
        latest: dict[tuple[str, str], Any] = {}
        for run in run_rows:
            application = str(run["application"] or "Unmapped").strip() or "Unmapped"
            release_line = str(run["release_line"] or "").strip()
            key = (application, release_line)
            previous = latest.get(key)
            current_order = (int(run["run_number"] or 0), str(run["created_at"] or ""), str(run["id"] or ""))
            previous_order = (
                int(previous["run_number"] or 0),
                str(previous["created_at"] or ""),
                str(previous["id"] or ""),
            ) if previous is not None else (-1, "", "")
            if current_order >= previous_order:
                latest[key] = run
        return sorted(
            latest.values(),
            key=lambda run: (int(run["run_number"] or 0), str(run["created_at"] or "")),
        )

    def list_issues(self, *, responsibles: list[str] | None = None, view_all: bool = False) -> list[dict[str, Any]]:
        with self.connect() as db:
            allowed: set[str] = set()
            if not view_all:
                allowed = {
                    person.casefold()
                    for value in (responsibles or [])
                    for person in scope_people(value)
                }
                if not allowed:
                    return []
            rows = db.execute(
                f"""SELECT i.*, r.run_number, r.conclusion, r.severity_counts_json,
                    (SELECT COUNT(*) FROM review_runs rr WHERE rr.jira_key=i.jira_key) AS run_count,
                    (SELECT COUNT(*) FROM findings f WHERE f.run_id=i.latest_run_id) AS finding_count
                    FROM review_issues i LEFT JOIN review_runs r ON r.id=i.latest_run_id
                    ORDER BY i.updated_at DESC""",
            ).fetchall()
            if not view_all:
                rows = [
                    row
                    for row in rows
                    if allowed.intersection(
                        person.casefold()
                        for person in self._issue_scope_people(
                            db, str(row["jira_key"]), row["responsible"]
                        )
                    )
                ]
            return [self._issue_summary(db, row) for row in rows]

    def issue_detail(self, jira_key: str, cycle_id: str = "") -> dict[str, Any] | None:
        with self.connect() as db:
            issue = db.execute("SELECT * FROM review_issues WHERE jira_key=?", (jira_key.upper(),)).fetchone()
            if not issue:
                return None
            # The list and detail views must consume the same Cycle projection.
            # Raw review_cycles rows omit application progress, Run counts and
            # readiness, which previously made a real required scope look empty
            # after the user opened the Issue detail.
            issue_summary = self._issue_summary(db, issue)
            cycles = list(issue_summary.get("cycles") or [])
            requested_cycle_id = cycle_id.strip()
            selected_cycle = next(
                (cycle for cycle in cycles if str(cycle.get("cycle_id") or "") == requested_cycle_id),
                None,
            ) if requested_cycle_id else next(
                (cycle for cycle in cycles if str(cycle.get("cycle_id") or "") == str(issue["current_cycle_id"] or "")),
                cycles[0] if cycles else None,
            )
            if requested_cycle_id and not selected_cycle:
                raise KeyError("Review Cycle was not found for this Jira issue.")
            selected_cycle_id = str((selected_cycle or {}).get("cycle_id") or "")
            run_query = "SELECT * FROM review_runs WHERE jira_key=?"
            run_params: list[object] = [jira_key.upper()]
            if selected_cycle_id:
                run_query += " AND cycle_id=?"
                run_params.append(selected_cycle_id)
            run_query += " ORDER BY run_number DESC"
            runs = [self._row(row) for row in db.execute(run_query, run_params)]
            for run in runs:
                run["severity_counts"] = json.loads(run.pop("severity_counts_json") or "{}")
                findings = []
                for finding_row in db.execute("SELECT * FROM findings WHERE run_id=? ORDER BY CAST(report_index AS INTEGER), report_index", (run["id"],)):
                    finding = self._row(finding_row)
                    finding["details"] = json.loads(finding.pop("details_json") or "{}")
                    handling = db.execute("SELECT * FROM finding_handlings WHERE finding_id=? ORDER BY updated_at DESC LIMIT 1", (finding["id"],)).fetchone()
                    finding["handling"] = self._row(handling) if handling else None
                    findings.append(finding)
                run["findings"] = findings
            latest_run = runs[0] if runs else {}
            latest_group_id = str(latest_run.get("run_group_id") or "")
            latest_group_candidates = [
                run
                for run in runs
                if latest_group_id and str(run.get("run_group_id") or "") == latest_group_id
            ] or ([latest_run] if latest_run else [])
            latest_group_runs = self._latest_runs_per_scope(latest_group_candidates)
            latest_group_findings: list[dict[str, Any]] = []
            latest_group_severity: dict[str, int] = {}
            for run in latest_group_runs:
                scope_label = review_scope_label(
                    str(run.get("application") or ""),
                    str(run.get("release_line") or ""),
                )
                for severity, count in (run.get("severity_counts") or {}).items():
                    latest_group_severity[str(severity)] = latest_group_severity.get(str(severity), 0) + int(count or 0)
                for source_finding in run.get("findings") or []:
                    finding = dict(source_finding)
                    finding["application"] = run.get("application") or "Unmapped"
                    finding["release_line"] = run.get("release_line") or ""
                    finding["scope_label"] = scope_label
                    finding["run_id"] = run.get("id") or ""
                    latest_group_findings.append(finding)
            latest_run_group = {
                **latest_run,
                "runs": latest_group_runs,
                "run_count": len(latest_group_runs),
                "findings": latest_group_findings,
                "finding_count": len(latest_group_findings),
                "severity_counts": latest_group_severity,
            } if latest_run else {}
            discussions = [
                self._row(row)
                for row in db.execute(
                    "SELECT * FROM discussions WHERE jira_key=? AND (?='' OR cycle_id=?) ORDER BY created_at",
                    (jira_key.upper(), selected_cycle_id, selected_cycle_id),
                )
            ]
            drafts = self._drafts(db, jira_key.upper())
            passes = [
                self._row(row)
                for row in db.execute(
                    "SELECT * FROM pass_records WHERE jira_key=? AND (?='' OR cycle_id=?) ORDER BY created_at DESC",
                    (jira_key.upper(), selected_cycle_id, selected_cycle_id),
                )
            ]
            memberships = [
                self._decoded_row(row, ("source_json",))
                for row in db.execute(
                    "SELECT * FROM sprint_memberships WHERE jira_key=? ORDER BY joined_at, created_at", (jira_key.upper(),)
                )
            ]
            run_groups = [
                self._row(row)
                for row in db.execute(
                    "SELECT * FROM review_run_groups WHERE jira_key=? AND (?='' OR cycle_id=?) ORDER BY created_at DESC",
                    (jira_key.upper(), selected_cycle_id, selected_cycle_id),
                )
            ]
            description_snapshots = [
                self._description_snapshot(db, str(row["id"]))
                for row in db.execute(
                    "SELECT id FROM description_snapshots WHERE jira_key=? AND (?='' OR cycle_id=?) ORDER BY captured_at DESC",
                    (jira_key.upper(), selected_cycle_id, selected_cycle_id),
                )
            ]
            review_snapshots = [
                self._review_snapshot(db, str(row["id"]))
                for row in db.execute(
                    "SELECT id FROM review_snapshots WHERE jira_key=? AND (?='' OR cycle_id=?) ORDER BY created_at DESC",
                    (jira_key.upper(), selected_cycle_id, selected_cycle_id),
                )
            ]
            deferred_resources = [
                self._decoded_row(row, ("evidence_json",))
                for row in db.execute(
                    "SELECT * FROM deferred_release_resources WHERE jira_key=? AND (?='' OR cycle_id=?) ORDER BY created_at",
                    (jira_key.upper(), selected_cycle_id, selected_cycle_id),
                )
            ]
            issue_item = self._row(issue)
            issue_item["responsible_scope"] = sorted(
                self._issue_scope_people(db, jira_key.upper(), issue["responsible"]),
                key=str.casefold,
            )
            return {
                "issue": issue_item, "cycles": cycles, "selected_cycle": selected_cycle,
                "sprint_memberships": memberships,
                "run_groups": run_groups, "runs": runs, "description_snapshots": description_snapshots,
                "latest_run_group": latest_run_group,
                "review_snapshots": review_snapshots, "deferred_resources": deferred_resources,
                "discussions": discussions, "drafts": drafts, "passes": passes,
                "pass_readiness": self._pass_readiness(db, jira_key, selected_cycle_id),
            }

    def cycle_detail(self, cycle_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            cycle = db.execute("SELECT * FROM review_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
            if not cycle:
                return None
            return self._review_snapshot_payload(db, cycle)

    def list_drafts(self, jira_key: str = "") -> list[dict[str, Any]]:
        with self.connect() as db:
            return self._drafts(db, jira_key.upper())

    def finding_scope(self, finding_id: str) -> dict[str, str] | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT f.id, f.jira_key, i.responsible FROM findings f
                   JOIN review_issues i ON i.jira_key=f.jira_key WHERE f.id=?""",
                (finding_id,),
            ).fetchone()
            return self._row(row) if row else None

    def handling_scope(self, handling_id: str) -> dict[str, str] | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT h.id, f.jira_key, i.responsible FROM finding_handlings h
                   JOIN findings f ON f.id=h.finding_id JOIN review_issues i ON i.jira_key=f.jira_key WHERE h.id=?""",
                (handling_id,),
            ).fetchone()
            return self._row(row) if row else None

    def draft_scope(self, draft_id: str) -> dict[str, str] | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT d.id, d.jira_key, i.responsible FROM jira_drafts d
                   JOIN review_issues i ON i.jira_key=d.jira_key WHERE d.id=?""",
                (draft_id,),
            ).fetchone()
            return self._row(row) if row else None

    def save_draft_attachment(
        self,
        draft_id: str,
        *,
        file_name: str,
        media_type: str,
        content_base64: str,
        actor: str,
    ) -> dict[str, Any]:
        allowed = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
        if media_type not in allowed:
            raise ValueError("Only PNG, JPEG, GIF, and WebP images are supported.")
        try:
            payload = base64.b64decode(content_base64, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("Attachment content is not valid base64.") from exc
        max_bytes = int(app_config_get("review_workflow.attachment_max_bytes", 10 * 1024 * 1024) or 10 * 1024 * 1024)
        if not payload or len(payload) > max_bytes:
            raise ValueError(f"Attachment must be between 1 and {max_bytes} bytes.")
        attachment_id = str(uuid.uuid4())
        target_dir = Path(os.getenv("JIRA_DRAFT_ATTACHMENTS_DIR", str(DATA_DIR / "jira_draft_attachments"))) / draft_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{attachment_id}{allowed[media_type]}"
        target.write_bytes(payload)
        now = utc_now()
        with self._lock, self.connect() as db:
            draft = db.execute("SELECT jira_key FROM jira_drafts WHERE id=?", (draft_id,)).fetchone()
            if not draft:
                target.unlink(missing_ok=True)
                raise KeyError("Jira draft was not found.")
            db.execute(
                """INSERT INTO draft_attachments(id, draft_id, file_name, media_type, size, storage_path, created_by, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (attachment_id, draft_id, Path(file_name).name or f"screenshot{allowed[media_type]}", media_type, len(payload), str(target), actor, now),
            )
            self._audit(db, str(draft["jira_key"]), actor, "jira-draft-attachment-added", {"draft_id": draft_id, "attachment_id": attachment_id})
        return {
            "id": attachment_id,
            "draft_id": draft_id,
            "file_name": Path(file_name).name,
            "media_type": media_type,
            "size": len(payload),
            "url": f"/api/draft-attachments/{attachment_id}",
        }

    def attachment(self, attachment_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT a.*, d.jira_key, i.responsible FROM draft_attachments a
                   JOIN jira_drafts d ON d.id=a.draft_id JOIN review_issues i ON i.jira_key=d.jira_key WHERE a.id=?""",
                (attachment_id,),
            ).fetchone()
            return self._row(row) if row else None

    def update_draft(self, draft_id: str, summary: str, document: object, actor: str, expected_version: int) -> dict[str, Any]:
        if not summary.strip():
            raise ValueError("Issue Summary is required.")
        validate_adf(document)
        if not adf_plain_text(document):
            raise ValueError("Issue Description is required.")
        now = utc_now()
        with self._lock, self.connect() as db:
            current = db.execute("SELECT * FROM jira_drafts WHERE id=?", (draft_id,)).fetchone()
            if not current:
                raise KeyError("Jira draft was not found.")
            if int(current["version"]) != int(expected_version):
                raise RuntimeError("Jira draft was updated by another user. Reload before saving.")
            db.execute(
                "UPDATE jira_drafts SET summary=?, description_adf_json=?, updated_by=?, updated_at=?, version=version+1 WHERE id=?",
                (summary.strip(), adf_json(document), actor, now, draft_id),
            )
            self._audit(db, str(current["jira_key"]), actor, "jira-draft-updated", {"draft_id": draft_id})
            return self._draft(db, draft_id)

    def _issue_summary(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        item = self._row(row)
        item["responsible_scope"] = sorted(
            self._issue_scope_people(db, str(row["jira_key"]), row["responsible"]),
            key=str.casefold,
        )
        item["severity_counts"] = json.loads(item.pop("severity_counts_json") or "{}") if "severity_counts_json" in item else {}
        handling_counts = {"fixed": 0, "follow-up": 0, "not-issue": 0, "pending": 0}
        if row["latest_run_id"]:
            latest = db.execute(
                "SELECT id, run_group_id FROM review_runs WHERE id=?", (row["latest_run_id"],)
            ).fetchone()
            logical_run_ids = [str(row["latest_run_id"])]
            if latest and latest["run_group_id"]:
                group_rows = db.execute(
                    """SELECT * FROM review_runs
                       WHERE cycle_id=(SELECT cycle_id FROM review_runs WHERE id=?) AND run_group_id=?
                       ORDER BY run_number""",
                    (latest["id"], latest["run_group_id"]),
                ).fetchall()
                logical_run_ids = [
                    str(run["id"]) for run in self._latest_runs_per_scope(group_rows)
                ] or logical_run_ids
            placeholders = ",".join("?" for _ in logical_run_ids)
            item["finding_count"] = int(
                db.execute(
                    f"SELECT COUNT(*) FROM findings WHERE run_id IN ({placeholders})",
                    logical_run_ids,
                ).fetchone()[0]
            )
            severity_counts: dict[str, int] = {}
            for severity_row in db.execute(
                f"SELECT severity, COUNT(*) AS count FROM findings WHERE run_id IN ({placeholders}) GROUP BY severity",
                logical_run_ids,
            ):
                severity_counts[str(severity_row["severity"])] = int(severity_row["count"])
            item["severity_counts"] = severity_counts
            for result in db.execute(
                """SELECT h.disposition, h.approval_status FROM findings f
                   LEFT JOIN finding_handlings h ON h.id=(SELECT id FROM finding_handlings x WHERE x.finding_id=f.id ORDER BY updated_at DESC LIMIT 1)
                   WHERE f.run_id IN (""" + placeholders + ")",
                logical_run_ids,
            ):
                disposition = str(result["disposition"] or "")
                if disposition in handling_counts:
                    handling_counts[disposition] += 1
                else:
                    handling_counts["pending"] += 1
        item["handling_counts"] = handling_counts
        cycles = [
            self._decoded_row(cycle_row, ("status_transition_json", "mr_scope_json"))
            for cycle_row in db.execute(
                "SELECT * FROM review_cycles WHERE jira_key=? ORDER BY cycle_number DESC", (row["jira_key"],)
            )
        ]
        for cycle in cycles:
            cycle["review_snapshot_count"] = int(db.execute(
                "SELECT COUNT(*) FROM review_snapshots WHERE cycle_id=?", (cycle["cycle_id"],)
            ).fetchone()[0])
            cycle["application_progress"] = self._cycle_application_progress(db, cycle)
            cycle_latest = db.execute(
                "SELECT id, run_group_id, run_number FROM review_runs WHERE cycle_id=? ORDER BY run_number DESC LIMIT 1",
                (cycle["cycle_id"],),
            ).fetchone()
            cycle_run_ids: list[str] = []
            if cycle_latest:
                cycle_run_ids = [str(cycle_latest["id"])]
                if cycle_latest["run_group_id"]:
                    cycle_run_ids = [
                        str(run["id"])
                        for run in db.execute(
                            "SELECT id FROM review_runs WHERE cycle_id=? AND run_group_id=? ORDER BY run_number",
                            (cycle["cycle_id"], cycle_latest["run_group_id"]),
                        )
                    ] or cycle_run_ids
            cycle_handling = {"fixed": 0, "follow-up": 0, "not-issue": 0, "pending": 0}
            cycle_finding_count = 0
            if cycle_run_ids:
                cycle_placeholders = ",".join("?" for _ in cycle_run_ids)
                cycle_findings = db.execute(
                    f"""SELECT f.id, h.disposition FROM findings f
                        LEFT JOIN finding_handlings h ON h.id=(
                          SELECT id FROM finding_handlings x WHERE x.finding_id=f.id ORDER BY updated_at DESC LIMIT 1
                        ) WHERE f.run_id IN ({cycle_placeholders})""",
                    cycle_run_ids,
                ).fetchall()
                cycle_finding_count = len(cycle_findings)
                for finding in cycle_findings:
                    disposition = str(finding["disposition"] or "")
                    if disposition in cycle_handling:
                        cycle_handling[disposition] += 1
                    else:
                        cycle_handling["pending"] += 1
            cycle["run_number"] = int(cycle_latest["run_number"] or 0) if cycle_latest else 0
            cycle["finding_count"] = cycle_finding_count
            cycle["handling_counts"] = cycle_handling
            cycle["pass_readiness"] = self._pass_readiness(db, str(row["jira_key"]), str(cycle["cycle_id"]))
        current_cycle = next(
            (cycle for cycle in cycles if cycle.get("cycle_id") == item.get("current_cycle_id")), None
        )
        item["current_cycle"] = current_cycle
        item["cycles"] = cycles
        item["cycle_count"] = len(cycles)
        item["review_snapshot_count"] = int(db.execute(
            "SELECT COUNT(*) FROM review_snapshots WHERE jira_key=?", (row["jira_key"],)
        ).fetchone()[0])
        item["pass_readiness"] = self._pass_readiness(db, str(row["jira_key"]))
        return item

    def _cycle_application_progress(
        self, db: sqlite3.Connection, cycle: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Return current-cycle state per application without mixing older Sprints."""
        cycle_id = str(cycle.get("cycle_id") or "")
        scope = cycle.get("mr_scope") if isinstance(cycle.get("mr_scope"), list) else []
        expected: set[tuple[str, str, str]] = set()
        for item in scope:
            if not isinstance(item, dict):
                continue
            review_scope = review_scope_for_merge_request(item)
            release_line = review_scope.release_line
            delivery_version = delivery_version_for_merge_request(item, review_scope)
            if (
                review_scope.application != "Unmapped"
                and not str(item.get("release_line") or "").strip()
                and not any(
                    str(item.get(field) or "").strip()
                    for field in ("target_branch", "source_branch", "branch")
                )
            ):
                # v2 Cycle snapshots predate release-line persistence. Keep their
                # mapped application aligned with legacy runs whose line is empty.
                release_line = ""
            expected.add((review_scope.application, release_line, delivery_version))
        run_rows = db.execute(
            "SELECT * FROM review_runs WHERE cycle_id=? ORDER BY run_number", (cycle_id,)
        ).fetchall()
        status_transition = cycle.get("status_transition") if isinstance(cycle.get("status_transition"), dict) else {}
        legacy_scope_fallback = not bool(status_transition.get("scope_authoritative"))
        latest: dict[tuple[str, str, str], sqlite3.Row] = {}
        report_counts: dict[tuple[str, str, str], int] = {}
        run_scope_keys: dict[str, tuple[str, str, str]] = {}
        for run in run_rows:
            application = str(run["application"] or "").strip()
            if application not in APPLICATION_ORDER:
                expected_applications = {item[0] for item in expected}
                application = (
                    next(iter(expected_applications))
                    if len(expected_applications) == 1
                    else "Unmapped"
                )
            release_line = str(run["release_line"] or "").strip()
            candidates = [
                item
                for item in expected
                if item[0] == application and (not release_line or item[1] == release_line)
            ]
            delivery_version = ""
            if len(candidates) == 1:
                candidate = candidates[0]
                release_line = release_line or candidate[1]
                delivery_version = candidate[2]
            scope_key = (application, release_line, delivery_version)
            # Current Cycle discovery defines required scope. Runs are evidence
            # for that scope and must not resurrect applications from an older
            # Sprint or delivery version. Empty legacy Cycles retain the old
            # run-derived fallback so existing history remains readable.
            if not expected and legacy_scope_fallback:
                expected.add(scope_key)
            elif scope_key not in expected:
                continue
            # Presence is scoped, not revision-counted. Re-scans replace the
            # previous report for this application/release scope.
            report_counts[scope_key] = 1
            latest[scope_key] = run
            run_scope_keys[str(run["id"])] = scope_key
        group_running = bool(db.execute(
            "SELECT 1 FROM review_run_groups WHERE cycle_id=? AND status IN ('queued','running') LIMIT 1",
            (cycle_id,),
        ).fetchone())
        rows: list[dict[str, Any]] = []
        blocking = blocking_severities()
        for application, release_line, delivery_version in sorted(
            expected,
            key=lambda item: (
                APPLICATION_ORDER.index(item[0]) if item[0] in APPLICATION_ORDER else 99,
                item[1].casefold(),
                item[2].casefold(),
            ),
        ):
            scope_key = (application, release_line, delivery_version)
            run = latest.get(scope_key)
            current_runs: list[sqlite3.Row] = []
            if run:
                current_group_id = str(run["run_group_id"] or "")
                current_runs = self._latest_runs_per_scope([
                    candidate
                    for candidate in run_rows
                    if run_scope_keys.get(str(candidate["id"])) == scope_key
                    and str(candidate["run_group_id"] or "") == current_group_id
                ]) or [run]
            finding_count = 0
            handled_count = 0
            pending_blockers = 0
            if current_runs:
                findings = [
                    finding
                    for current_run in current_runs
                    for finding in db.execute(
                        "SELECT * FROM findings WHERE run_id=?", (current_run["id"],)
                    ).fetchall()
                ]
                finding_count = len(findings)
                for finding in findings:
                    handling = db.execute(
                        "SELECT * FROM finding_handlings WHERE finding_id=? ORDER BY updated_at DESC LIMIT 1",
                        (finding["id"],),
                    ).fetchone()
                    if handling:
                        handled_count += 1
                    if str(finding["severity"]).title() not in blocking:
                        continue
                    accepted = False
                    if handling and str(handling["approval_status"] or "") in {"approved", "not-required"}:
                        accepted = str(handling["disposition"] or "") == "not-issue" or bool(handling["manager_override"])
                    if not accepted:
                        pending_blockers += 1
            if not run:
                state = "generating" if group_running else "without-report"
            elif any(
                str(current_run["status"] or "").lower() in {"failed", "generation-failed", "rescan-failed"}
                for current_run in current_runs
            ):
                state = "failed"
            elif str(cycle.get("pass_status") or "").lower() == "passed":
                state = "review-pass"
            elif pending_blockers:
                state = "handling"
            else:
                state = "ready-for-pass"
            rows.append(
                {
                    "application": application,
                    "release_line": release_line,
                    "delivery_version": delivery_version,
                    "scope_label": review_scope_label(application, release_line, delivery_version),
                    "state": state,
                    "report_count": report_counts.get(scope_key, 0),
                    "finding_count": finding_count,
                    "handled_count": handled_count,
                    "pending_blockers": pending_blockers,
                }
            )
        return rows

    def _pass_readiness(
        self, db: sqlite3.Connection, jira_key: str, cycle_id: str = ""
    ) -> dict[str, Any]:
        issue = db.execute("SELECT * FROM review_issues WHERE jira_key=?", (jira_key.upper(),)).fetchone()
        if not issue:
            return {"ready": False, "message": "No completed Review Run is available.", "pending_blockers": []}
        scope_gaps: list[dict[str, Any]] = []
        target_cycle_id = cycle_id.strip() or str(issue["current_cycle_id"] or "")
        cycle_row = None
        if target_cycle_id:
            cycle_row = db.execute(
                "SELECT * FROM review_cycles WHERE cycle_id=? AND jira_key=?",
                (target_cycle_id, jira_key.upper()),
            ).fetchone()
        if not cycle_row:
            return {
                "ready": False, "message": "The selected Review Cycle is unavailable.",
                "pending_blockers": [], "scope_gaps": [], "cycle_id": target_cycle_id,
            }
        cycle = self._decoded_row(cycle_row, ("status_transition_json", "mr_scope_json"))
        application_progress = self._cycle_application_progress(db, cycle)
        status_transition = cycle.get("status_transition") if isinstance(cycle.get("status_transition"), dict) else {}
        if not application_progress and bool(status_transition.get("scope_authoritative")):
            return {
                "ready": False,
                "not_required": True,
                "message": "No review is required for this Cycle. It is excluded from Review Pass readiness.",
                "pending_blockers": [],
                "scope_gaps": [],
                "cycle_id": target_cycle_id,
                "is_current_cycle": target_cycle_id == str(issue["current_cycle_id"] or ""),
            }
        for progress in application_progress:
            state = str(progress.get("state") or "")
            if state in {"without-report", "generating", "failed"}:
                scope_gaps.append(
                    {
                        "application": progress.get("application") or "Unmapped",
                        "release_line": progress.get("release_line") or "",
                        "delivery_version": progress.get("delivery_version") or "",
                        "scope_label": progress.get("scope_label") or "Unmapped",
                        "state": state,
                    }
                )
        latest_run = db.execute(
            "SELECT * FROM review_runs WHERE cycle_id=? ORDER BY run_number DESC LIMIT 1",
            (target_cycle_id,),
        ).fetchone()
        if not latest_run:
            return {
                "ready": False, "message": "No completed Review Run is available for this Cycle.",
                "pending_blockers": [], "scope_gaps": scope_gaps, "cycle_id": target_cycle_id,
            }
        run_id = str(latest_run["id"])
        run_group_id = str(latest_run["run_group_id"] or "") if latest_run else ""
        run_ids = [run_id]
        if run_group_id:
            group_rows = db.execute(
                "SELECT * FROM review_runs WHERE cycle_id=? AND run_group_id=? ORDER BY run_number",
                (target_cycle_id, run_group_id),
            ).fetchall()
            run_ids = [
                str(row["id"]) for row in self._latest_runs_per_scope(group_rows)
            ] or [run_id]
        placeholders = ",".join("?" for _ in run_ids)
        findings = db.execute(f"SELECT * FROM findings WHERE run_id IN ({placeholders})", run_ids).fetchall()
        pending: list[dict[str, Any]] = []
        manager_exceptions = 0
        for finding in findings:
            if str(finding["severity"]).title() not in blocking_severities():
                continue
            handling = db.execute(
                "SELECT * FROM finding_handlings WHERE finding_id=? ORDER BY updated_at DESC LIMIT 1",
                (finding["id"],),
            ).fetchone()
            accepted = False
            if handling and handling["approval_status"] in {"approved", "not-required"}:
                disposition = str(handling["disposition"])
                accepted = disposition == "not-issue" or bool(handling["manager_override"])
                manager_exceptions += int(bool(handling["manager_override"]))
            if not accepted:
                pending.append(self._row(finding))
        ready = not pending and not scope_gaps
        if scope_gaps:
            message = f"{len(scope_gaps)} required application scope(s) have no completed report."
        elif pending:
            message = f"{len(pending)} blocking finding(s) remain."
        else:
            message = "All required application scopes have reports and configured blocking findings are cleared."
        return {
            "ready": ready,
            "message": message,
            "pending_blockers": pending,
            "scope_gaps": scope_gaps,
            "blocking_severities": sorted(blocking_severities()),
            "manager_exceptions": manager_exceptions,
            "run_id": run_id,
            "run_ids": run_ids,
            "run_group_id": run_group_id,
            "cycle_id": target_cycle_id,
            "is_current_cycle": target_cycle_id == str(issue["current_cycle_id"] or ""),
        }

    def _drafts(self, db: sqlite3.Connection, jira_key: str = "") -> list[dict[str, Any]]:
        if jira_key:
            rows = db.execute("SELECT * FROM jira_drafts WHERE jira_key=? ORDER BY updated_at DESC", (jira_key,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM jira_drafts ORDER BY updated_at DESC").fetchall()
        items = []
        for row in rows:
            item = self._row(row)
            item["description_adf"] = json.loads(item.pop("description_adf_json"))
            item["attachments"] = [
                {**self._row(attachment), "url": f"/api/draft-attachments/{attachment['id']}"}
                for attachment in db.execute(
                    "SELECT id, file_name, media_type, size, created_by, created_at FROM draft_attachments WHERE draft_id=? ORDER BY created_at",
                    (item["id"],),
                )
            ]
            items.append(item)
        return items

    def _draft(self, db: sqlite3.Connection, draft_id: str) -> dict[str, Any]:
        row = db.execute("SELECT * FROM jira_drafts WHERE id=?", (draft_id,)).fetchone()
        if not row:
            raise KeyError("Jira draft was not found.")
        item = self._row(row)
        item["description_adf"] = json.loads(item.pop("description_adf_json"))
        return item

    def _description_snapshot(self, db: sqlite3.Connection, snapshot_id: str) -> dict[str, Any]:
        row = db.execute("SELECT * FROM description_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if not row:
            raise KeyError("Description Snapshot was not found.")
        return self._decoded_row(
            row, ("adf_json", "attachments_json", "code_mrs_json", "deferred_mrs_json")
        )

    def _review_snapshot(self, db: sqlite3.Connection, snapshot_id: str) -> dict[str, Any]:
        row = db.execute("SELECT * FROM review_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if not row:
            raise KeyError("Review Snapshot was not found.")
        return self._decoded_row(row, ("payload_json",))

    def _review_snapshot_payload(self, db: sqlite3.Connection, cycle: sqlite3.Row) -> dict[str, Any]:
        cycle_id = str(cycle["cycle_id"])
        jira_key = str(cycle["jira_key"])
        issue = self._row(db.execute("SELECT * FROM review_issues WHERE jira_key=?", (jira_key,)).fetchone())
        groups: list[dict[str, Any]] = []
        for group_row in db.execute(
            "SELECT * FROM review_run_groups WHERE cycle_id=? ORDER BY created_at", (cycle_id,)
        ):
            group = self._row(group_row)
            runs: list[dict[str, Any]] = []
            for run_row in db.execute(
                "SELECT * FROM review_runs WHERE run_group_id=? ORDER BY project_type, run_number", (group["id"],)
            ):
                run = self._decoded_row(run_row, ("severity_counts_json",))
                findings: list[dict[str, Any]] = []
                for finding_row in db.execute(
                    "SELECT * FROM findings WHERE run_id=? ORDER BY CAST(report_index AS INTEGER), report_index",
                    (run["id"],),
                ):
                    finding = self._decoded_row(finding_row, ("details_json",))
                    handling = db.execute(
                        "SELECT * FROM finding_handlings WHERE finding_id=? ORDER BY updated_at DESC LIMIT 1",
                        (finding["id"],),
                    ).fetchone()
                    finding["handling"] = self._row(handling) if handling else None
                    findings.append(finding)
                run["findings"] = findings
                runs.append(run)
            group["runs"] = runs
            groups.append(group)
        descriptions = [
            self._description_snapshot(db, str(row["id"]))
            for row in db.execute(
                "SELECT id FROM description_snapshots WHERE cycle_id=? ORDER BY captured_at, version", (cycle_id,)
            )
        ]
        deferred = [
            self._decoded_row(row, ("evidence_json",))
            for row in db.execute(
                "SELECT * FROM deferred_release_resources WHERE cycle_id=? ORDER BY created_at", (cycle_id,)
            )
        ]
        discussions = [
            self._row(row)
            for row in db.execute(
                """SELECT id, run_id, finding_id, author, kind, created_at FROM discussions
                   WHERE cycle_id=? ORDER BY created_at""",
                (cycle_id,),
            )
        ]
        passes = [
            self._decoded_row(row, ("policy_json",))
            for row in db.execute(
                "SELECT * FROM pass_records WHERE cycle_id=? ORDER BY created_at", (cycle_id,)
            )
        ]
        return {
            "issue": issue,
            "cycle": self._decoded_row(cycle, ("status_transition_json", "mr_scope_json")),
            "run_groups": groups,
            "description_snapshots": descriptions,
            "deferred_resources": deferred,
            "discussion_references": discussions,
            "pending_jira": self._drafts(db, jira_key),
            "passes": passes,
            "pass_readiness": self._pass_readiness(db, jira_key, cycle_id),
        }

    @staticmethod
    def _refresh_release_gate_status(db: sqlite3.Connection, cycle_id: str, now: str) -> None:
        statuses = [
            str(row["status"])
            for row in db.execute(
                "SELECT status FROM deferred_release_resources WHERE cycle_id=? AND status<>'superseded'", (cycle_id,)
            )
        ]
        gate_status = "pending"
        if "blocked" in statuses:
            gate_status = "blocked"
        elif statuses and all(status == "verified" for status in statuses):
            gate_status = "ready"
        db.execute(
            "UPDATE review_cycles SET release_gate_status=?, updated_at=? WHERE cycle_id=?",
            (gate_status, now, cycle_id),
        )

    def _audit(self, db: sqlite3.Connection, jira_key: str, actor: str, event_type: str, payload: dict[str, Any]) -> None:
        db.execute(
            "INSERT INTO audit_events(id, jira_key, actor, event_type, payload_json, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), jira_key, actor, event_type, json.dumps(payload, ensure_ascii=False), utc_now()),
        )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any]:
        return dict(row) if row is not None else {}

    @classmethod
    def _decoded_row(cls, row: sqlite3.Row | None, json_columns: tuple[str, ...]) -> dict[str, Any]:
        item = cls._row(row)
        for column in json_columns:
            raw = item.pop(column, None)
            target = column[:-5] if column.endswith("_json") else column
            try:
                item[target] = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                item[target] = {} if column.endswith("_json") else None
        return item


_STORE: WorkflowStore | None = None
_STORE_LOCK = threading.Lock()


def workflow_store() -> WorkflowStore:
    global _STORE
    path = workflow_db_path()
    with _STORE_LOCK:
        if _STORE is None or _STORE.path != path:
            backend = os.getenv("WORKFLOW_STORAGE_BACKEND", "sqlite").strip().lower()
            if backend != "sqlite":
                raise RuntimeError(f"Unsupported workflow storage backend: {backend}. Install a repository adapter first.")
            _STORE = WorkflowStore(path)
        return _STORE
