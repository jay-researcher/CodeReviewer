from __future__ import annotations

import hashlib
import base64
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Protocol

from .adf import adf_json, adf_plain_text, empty_adf, validate_adf
from .config import DATA_DIR, app_config_get


SCHEMA_VERSION = 1
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
    def issue_detail(self, jira_key: str) -> dict[str, Any] | None: ...
    def list_drafts(self, jira_key: str = "") -> list[dict[str, Any]]: ...


def utc_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def workflow_db_path() -> Path:
    return Path(os.getenv("CODEREVIEWER_DB_FILE", str(DATA_DIR / "codereviewer.db"))).expanduser()


def blocking_severities() -> set[str]:
    value = app_config_get("review_workflow.blocking_severities", ["Critical", "High"])
    if not isinstance(value, list):
        value = ["Critical", "High"]
    return {str(item).strip().title() for item in value if str(item).strip()}


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
            db.executescript(
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
            db.execute(
                "INSERT INTO schema_meta(key, value) VALUES('version', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

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
    ) -> str:
        jira_key = jira_key.strip().upper()
        if not jira_key:
            raise ValueError("Jira key is required.")
        timestamp = created_at or utc_now()
        report_identity = report_fingerprint(report_path)
        with self._lock, self.connect() as db:
            db.execute(
                """INSERT INTO review_issues(jira_key, summary, responsible, status, created_at, updated_at)
                   VALUES(?, ?, ?, 'handling', ?, ?)
                   ON CONFLICT(jira_key) DO UPDATE SET
                     summary=CASE WHEN excluded.summary<>'' THEN excluded.summary ELSE review_issues.summary END,
                     responsible=CASE WHEN excluded.responsible<>'' THEN excluded.responsible ELSE review_issues.responsible END,
                     updated_at=excluded.updated_at""",
                (jira_key, summary, responsible, timestamp, timestamp),
            )
            existing = db.execute(
                "SELECT id FROM review_runs WHERE jira_key=? AND report_fingerprint=?",
                (jira_key, report_identity),
            ).fetchone()
            if existing:
                return str(existing["id"])
            run_number = int(db.execute("SELECT COUNT(*) FROM review_runs WHERE jira_key=?", (jira_key,)).fetchone()[0]) + 1
            run_id = str(uuid.uuid4())
            severity_counts: dict[str, int] = {}
            for finding in findings:
                severity = str(finding.get("severity") or "Unknown").title()
                severity_counts[severity] = severity_counts.get(severity, 0) + 1
            db.execute(
                """INSERT INTO review_runs(id, jira_key, report_path, report_fingerprint, run_number, conclusion,
                   severity_counts_json, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, jira_key, report_path, report_identity, run_number, conclusion, json.dumps(severity_counts), timestamp),
            )
            previous = db.execute(
                "SELECT id FROM review_runs WHERE jira_key=? AND id<>? ORDER BY run_number DESC LIMIT 1",
                (jira_key, run_id),
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
                "UPDATE review_issues SET latest_run_id=?, passed_run_id=NULL, status=?, updated_at=? WHERE jira_key=?",
                (run_id, status, timestamp, jira_key),
            )
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
            return {"handling_id": handling_id, "draft_id": draft_id, "approval_status": approval_status}

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

    def pass_readiness(self, jira_key: str) -> dict[str, Any]:
        with self.connect() as db:
            return self._pass_readiness(db, jira_key)

    def manual_pass(self, jira_key: str, actor: str, actor_role: str, note: str) -> dict[str, Any]:
        if actor_role not in {"auditor", "manager"}:
            raise PermissionError("Only Auditor or Manager can record Review Pass.")
        readiness = self.pass_readiness(jira_key)
        if not readiness["ready"]:
            raise ValueError(str(readiness["message"]))
        now = utc_now()
        pass_id = str(uuid.uuid4())
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO pass_records(id, jira_key, run_id, actor, note, policy_json, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (pass_id, jira_key.upper(), readiness["run_id"], actor, note.strip(), json.dumps(readiness, ensure_ascii=False), now),
            )
            db.execute(
                "UPDATE review_issues SET status='passed', passed_run_id=latest_run_id, updated_at=? WHERE jira_key=?",
                (now, jira_key.upper()),
            )
            self._audit(db, jira_key.upper(), actor, "manual-pass", {"pass_id": pass_id, "run_id": readiness["run_id"]})
        return {"pass_id": pass_id, **readiness}

    def add_discussion(self, jira_key: str, actor: str, message: str, *, run_id: str = "", finding_id: str = "", kind: str = "comment") -> str:
        if not message.strip():
            raise ValueError("Message is required.")
        discussion_id = str(uuid.uuid4())
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO discussions(id, jira_key, run_id, finding_id, author, kind, message, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (discussion_id, jira_key.upper(), run_id or None, finding_id or None, actor, kind, message.strip(), utc_now()),
            )
        return discussion_id

    def import_legacy_thread(self, report_suffix: str, thread: dict[str, Any]) -> dict[str, int]:
        suffix = report_suffix.replace("\\", "/").lstrip("/").lower()
        imported = {"handlings": 0, "discussions": 0, "passes": 0}
        if not suffix:
            return imported
        with self._lock, self.connect() as db:
            run = next(
                (
                    row for row in db.execute("SELECT id, jira_key, report_path FROM review_runs ORDER BY created_at DESC")
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
                        "INSERT INTO discussions(id, jira_key, run_id, author, kind, message, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                        (str(uuid.uuid4()), run["jira_key"], run["id"], str(message.get("user") or "legacy"), str(message.get("kind") or "comment"), text, created),
                    )
                    imported["discussions"] += 1
                if message.get("kind") == "manual-pass":
                    passed = db.execute("SELECT 1 FROM pass_records WHERE jira_key=? AND run_id=?", (run["jira_key"], run["id"])).fetchone()
                    if not passed:
                        db.execute(
                            "INSERT INTO pass_records(id, jira_key, run_id, actor, note, policy_json, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                            (str(uuid.uuid4()), run["jira_key"], run["id"], str(message.get("user") or "legacy"), text, '{"legacy":true}', created),
                        )
                        db.execute(
                            "UPDATE review_issues SET status='passed', passed_run_id=?, updated_at=? WHERE jira_key=?",
                            (run["id"], created, run["jira_key"]),
                        )
                        imported["passes"] += 1
        return imported

    def list_issues(self, *, responsibles: list[str] | None = None, view_all: bool = False) -> list[dict[str, Any]]:
        with self.connect() as db:
            params: list[Any] = []
            where = ""
            if not view_all:
                values = [item.strip().lower() for item in (responsibles or []) if item.strip()]
                if not values:
                    return []
                where = "WHERE lower(i.responsible) IN (%s)" % ",".join("?" for _ in values)
                params.extend(values)
            rows = db.execute(
                f"""SELECT i.*, r.run_number, r.conclusion, r.severity_counts_json,
                    (SELECT COUNT(*) FROM review_runs rr WHERE rr.jira_key=i.jira_key) AS run_count,
                    (SELECT COUNT(*) FROM findings f WHERE f.run_id=i.latest_run_id) AS finding_count
                    FROM review_issues i LEFT JOIN review_runs r ON r.id=i.latest_run_id
                    {where} ORDER BY i.updated_at DESC""",
                params,
            ).fetchall()
            return [self._issue_summary(db, row) for row in rows]

    def issue_detail(self, jira_key: str) -> dict[str, Any] | None:
        with self.connect() as db:
            issue = db.execute("SELECT * FROM review_issues WHERE jira_key=?", (jira_key.upper(),)).fetchone()
            if not issue:
                return None
            runs = [self._row(row) for row in db.execute("SELECT * FROM review_runs WHERE jira_key=? ORDER BY run_number DESC", (jira_key.upper(),))]
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
            discussions = [self._row(row) for row in db.execute("SELECT * FROM discussions WHERE jira_key=? ORDER BY created_at", (jira_key.upper(),))]
            drafts = self._drafts(db, jira_key.upper())
            passes = [self._row(row) for row in db.execute("SELECT * FROM pass_records WHERE jira_key=? ORDER BY created_at DESC", (jira_key.upper(),))]
            return {"issue": self._row(issue), "runs": runs, "discussions": discussions, "drafts": drafts, "passes": passes, "pass_readiness": self._pass_readiness(db, jira_key)}

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
        item["severity_counts"] = json.loads(item.pop("severity_counts_json") or "{}") if "severity_counts_json" in item else {}
        handling_counts = {"fixed": 0, "follow-up": 0, "not-issue": 0, "pending": 0}
        if row["latest_run_id"]:
            for result in db.execute(
                """SELECT h.disposition, h.approval_status FROM findings f
                   LEFT JOIN finding_handlings h ON h.id=(SELECT id FROM finding_handlings x WHERE x.finding_id=f.id ORDER BY updated_at DESC LIMIT 1)
                   WHERE f.run_id=?""",
                (row["latest_run_id"],),
            ):
                disposition = str(result["disposition"] or "")
                if disposition in handling_counts:
                    handling_counts[disposition] += 1
                else:
                    handling_counts["pending"] += 1
        item["handling_counts"] = handling_counts
        item["pass_readiness"] = self._pass_readiness(db, str(row["jira_key"]))
        return item

    def _pass_readiness(self, db: sqlite3.Connection, jira_key: str) -> dict[str, Any]:
        issue = db.execute("SELECT * FROM review_issues WHERE jira_key=?", (jira_key.upper(),)).fetchone()
        if not issue or not issue["latest_run_id"]:
            return {"ready": False, "message": "No completed Review Run is available.", "pending_blockers": []}
        run_id = str(issue["latest_run_id"])
        findings = db.execute("SELECT * FROM findings WHERE run_id=?", (run_id,)).fetchall()
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
        ready = not pending
        return {
            "ready": ready,
            "message": "All configured blocking findings are cleared." if ready else f"{len(pending)} blocking finding(s) remain.",
            "pending_blockers": pending,
            "blocking_severities": sorted(blocking_severities()),
            "manager_exceptions": manager_exceptions,
            "run_id": run_id,
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

    def _audit(self, db: sqlite3.Connection, jira_key: str, actor: str, event_type: str, payload: dict[str, Any]) -> None:
        db.execute(
            "INSERT INTO audit_events(id, jira_key, actor, event_type, payload_json, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), jira_key, actor, event_type, json.dumps(payload, ensure_ascii=False), utc_now()),
        )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any]:
        return dict(row) if row is not None else {}


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
