from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EVIDENCE_TABLES = (
    "review_runs",
    "review_run_groups",
    "description_snapshots",
    "review_snapshots",
    "deferred_release_resources",
    "discussions",
    "pass_records",
)


def _csv_keys(raw: str) -> set[str]:
    return {item.strip().upper() for item in raw.split(",") if item.strip()}


def _json(raw: object, default: Any) -> Any:
    try:
        return json.loads(str(raw or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _mr_urls(raw: object) -> set[str]:
    rows = _json(raw, [])
    if not isinstance(rows, list):
        return set()
    return {
        str(row.get("mr_url") or row.get("url") or "").strip()
        for row in rows
        if isinstance(row, dict) and str(row.get("mr_url") or row.get("url") or "").strip()
    }


def _replace_cycle_metadata(value: Any, *, old_cycle: str, new_cycle: str, sprint_id: str, sprint_name: str) -> Any:
    if isinstance(value, list):
        return [
            _replace_cycle_metadata(
                item,
                old_cycle=old_cycle,
                new_cycle=new_cycle,
                sprint_id=sprint_id,
                sprint_name=sprint_name,
            )
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    updated: dict[str, Any] = {}
    for key, item in value.items():
        if key == "cycle_id" and str(item or "") == old_cycle:
            updated[key] = new_cycle
        elif key == "sprint_id" and str(item or "").strip().casefold() == "legacy":
            updated[key] = sprint_id
        elif key in {"sprint", "sprint_name"} and str(item or "").strip().casefold() in {
            "legacy",
            "legacy / unknown sprint",
        }:
            updated[key] = sprint_name
        elif key == "backfilled" and item in {1, True}:
            updated[key] = False
        else:
            updated[key] = _replace_cycle_metadata(
                item,
                old_cycle=old_cycle,
                new_cycle=new_cycle,
                sprint_id=sprint_id,
                sprint_name=sprint_name,
            )
    return updated


def _rewrite_json_column(
    db: sqlite3.Connection,
    table: str,
    key_column: str,
    key_value: str,
    json_column: str,
    *,
    old_cycle: str,
    new_cycle: str,
    sprint_id: str,
    sprint_name: str,
) -> None:
    rows = db.execute(
        f'SELECT rowid, "{json_column}" FROM "{table}" WHERE "{key_column}"=?',
        (key_value,),
    ).fetchall()
    for row in rows:
        payload = _json(row[json_column], None)
        if payload is None:
            continue
        payload = _replace_cycle_metadata(
            payload,
            old_cycle=old_cycle,
            new_cycle=new_cycle,
            sprint_id=sprint_id,
            sprint_name=sprint_name,
        )
        db.execute(
            f'UPDATE "{table}" SET "{json_column}"=? WHERE rowid=?',
            (json.dumps(payload, ensure_ascii=False), row["rowid"]),
        )


def _target_is_empty(db: sqlite3.Connection, cycle_id: str) -> bool:
    return all(
        int(db.execute(f'SELECT COUNT(*) FROM "{table}" WHERE cycle_id=?', (cycle_id,)).fetchone()[0]) == 0
        for table in EVIDENCE_TABLES
    )


def _latest_run(db: sqlite3.Connection, cycle_id: str) -> sqlite3.Row | None:
    return db.execute(
        "SELECT id, run_group_id FROM review_runs WHERE cycle_id=? ORDER BY created_at DESC, run_number DESC LIMIT 1",
        (cycle_id,),
    ).fetchone()


def _pending_findings(db: sqlite3.Connection, cycle_id: str, run_group_id: str) -> int:
    return int(
        db.execute(
            """
            SELECT COUNT(*)
            FROM findings f
            JOIN review_runs r ON r.id=f.run_id
            WHERE r.cycle_id=? AND r.run_group_id=?
              AND NOT EXISTS (
                SELECT 1 FROM finding_handlings h
                WHERE h.finding_id=f.id
                  AND h.disposition IN ('fixed','jira','not-issue')
                  AND h.approval_status IN ('approved','auto-approved')
              )
            """,
            (cycle_id, run_group_id),
        ).fetchone()[0]
    )


def _refresh_issue(db: sqlite3.Connection, jira_key: str, cycle_id: str, pass_status: str) -> None:
    latest = _latest_run(db, cycle_id)
    latest_run_id = str(latest["id"]) if latest else None
    if pass_status == "passed" and latest_run_id:
        status = "passed"
        passed_run_id = latest_run_id
    elif latest:
        pending = _pending_findings(db, cycle_id, str(latest["run_group_id"] or ""))
        status = "handling" if pending else "ready-for-pass"
        passed_run_id = None
    else:
        status = "not-reviewed"
        passed_run_id = None
    db.execute(
        """
        UPDATE review_issues
        SET current_cycle_id=?, latest_run_id=?, passed_run_id=?, status=?, updated_at=?
        WHERE jira_key=?
        """,
        (
            cycle_id,
            latest_run_id,
            passed_run_id,
            status,
            datetime.now(timezone.utc).isoformat(),
            jira_key,
        ),
    )


def _move_legacy_cycle(
    db: sqlite3.Connection,
    source: sqlite3.Row,
    *,
    sprint_id: str,
    sprint_name: str,
    sprint_state: str,
    target: sqlite3.Row | None,
    close_cycle: bool,
) -> dict[str, Any]:
    source_id = str(source["cycle_id"])
    jira_key = str(source["jira_key"])
    target_id = str(target["cycle_id"]) if target else source_id
    source_scope = _mr_urls(source["mr_scope_json"])
    target_scope = _mr_urls(target["mr_scope_json"]) if target else source_scope
    scope_matches = source_scope == target_scope
    pass_status = str(source["pass_status"] or "pending")
    if target and pass_status == "passed" and not scope_matches:
        pass_status = "pending"

    if target:
        if not _target_is_empty(db, target_id):
            raise RuntimeError(f"{jira_key}: destination Cycle {target_id} already contains review evidence")
        db.execute("UPDATE review_runs SET cycle_id=? WHERE cycle_id=?", (target_id, source_id))
        db.execute("UPDATE review_run_groups SET cycle_id=?, backfilled=0 WHERE cycle_id=?", (target_id, source_id))
        db.execute(
            "UPDATE description_snapshots SET cycle_id=?, sprint_id=?, backfilled=0 WHERE cycle_id=?",
            (target_id, sprint_id, source_id),
        )
        db.execute("UPDATE review_snapshots SET cycle_id=? WHERE cycle_id=?", (target_id, source_id))
        db.execute(
            "UPDATE deferred_release_resources SET cycle_id=?, sprint_id=? WHERE cycle_id=?",
            (target_id, sprint_id, source_id),
        )
        db.execute("UPDATE discussions SET cycle_id=? WHERE cycle_id=?", (target_id, source_id))
        db.execute("UPDATE pass_records SET cycle_id=? WHERE cycle_id=?", (target_id, source_id))
        _rewrite_json_column(
            db,
            "pass_records",
            "cycle_id",
            target_id,
            "policy_json",
            old_cycle=source_id,
            new_cycle=target_id,
            sprint_id=sprint_id,
            sprint_name=sprint_name,
        )
        _rewrite_json_column(
            db,
            "review_snapshots",
            "cycle_id",
            target_id,
            "payload_json",
            old_cycle=source_id,
            new_cycle=target_id,
            sprint_id=sprint_id,
            sprint_name=sprint_name,
        )
        db.execute(
            """
            UPDATE review_cycles
            SET current_description_snapshot_id=?,
                pass_status=?,
                release_gate_status=?,
                review_mode=?,
                cycle_started_at=MIN(cycle_started_at, ?),
                created_at=MIN(created_at, ?),
                updated_at=MAX(updated_at, ?)
            WHERE cycle_id=?
            """,
            (
                source["current_description_snapshot_id"],
                pass_status,
                source["release_gate_status"],
                source["review_mode"],
                source["cycle_started_at"],
                source["created_at"],
                source["updated_at"],
                target_id,
            ),
        )
        db.execute("DELETE FROM review_cycles WHERE cycle_id=?", (source_id,))
    else:
        closed_at = source["cycle_closed_at"]
        if close_cycle and not closed_at:
            closed_at = datetime.now(timezone.utc).isoformat()
        if not close_cycle:
            closed_at = None
        db.execute(
            """
            UPDATE review_cycles
            SET sprint_id=?, sprint_name=?, sprint_state=?, cycle_closed_at=?,
                pass_status=?, backfilled=0, updated_at=?
            WHERE cycle_id=?
            """,
            (
                sprint_id,
                sprint_name,
                sprint_state,
                closed_at,
                pass_status,
                datetime.now(timezone.utc).isoformat(),
                source_id,
            ),
        )
        db.execute(
            "UPDATE review_run_groups SET backfilled=0 WHERE cycle_id=?",
            (source_id,),
        )
        db.execute(
            "UPDATE description_snapshots SET sprint_id=?, backfilled=0 WHERE cycle_id=?",
            (sprint_id, source_id),
        )
        db.execute(
            "UPDATE deferred_release_resources SET sprint_id=? WHERE cycle_id=?",
            (sprint_id, source_id),
        )
        _rewrite_json_column(
            db,
            "pass_records",
            "cycle_id",
            source_id,
            "policy_json",
            old_cycle=source_id,
            new_cycle=source_id,
            sprint_id=sprint_id,
            sprint_name=sprint_name,
        )
        _rewrite_json_column(
            db,
            "review_snapshots",
            "cycle_id",
            source_id,
            "payload_json",
            old_cycle=source_id,
            new_cycle=source_id,
            sprint_id=sprint_id,
            sprint_name=sprint_name,
        )

    if close_cycle:
        db.execute(
            "UPDATE review_issues SET current_cycle_id=NULL WHERE jira_key=? AND current_cycle_id=?",
            (jira_key, target_id),
        )
    else:
        _refresh_issue(db, jira_key, target_id, pass_status)
    return {
        "jira_key": jira_key,
        "source_cycle": source_id,
        "target_cycle": target_id,
        "sprint_id": sprint_id,
        "pass_status": pass_status,
        "scope_matches": scope_matches,
        "merged": bool(target),
    }


def migrate(
    database: Path,
    *,
    current_sprint: str,
    current_keys: set[str],
    ad_hoc_keys: set[str],
    dry_run: bool,
) -> dict[str, Any]:
    if current_keys & ad_hoc_keys:
        raise ValueError("Current Sprint keys and Ad hoc keys must not overlap")
    db = sqlite3.connect(database)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity check failed before migration: {integrity}")
        legacy = db.execute(
            "SELECT * FROM review_cycles WHERE lower(trim(sprint_id))='legacy' ORDER BY jira_key, cycle_number"
        ).fetchall()
        actual_keys = {str(row["jira_key"]).upper() for row in legacy}
        unknown = actual_keys - current_keys - ad_hoc_keys
        missing = current_keys - actual_keys - {
            str(row[0]).upper()
            for row in db.execute(
                "SELECT jira_key FROM review_cycles WHERE sprint_id=?",
                (current_sprint,),
            )
        }
        if unknown:
            raise RuntimeError(f"Unclassified Legacy Cycle(s): {', '.join(sorted(unknown))}")
        if missing:
            raise RuntimeError(f"Expected current-week Cycle(s) are missing: {', '.join(sorted(missing))}")

        results: list[dict[str, Any]] = []
        for source in legacy:
            jira_key = str(source["jira_key"]).upper()
            if jira_key in current_keys:
                targets = db.execute(
                    """
                    SELECT * FROM review_cycles
                    WHERE jira_key=? AND sprint_id=? AND cycle_id<>?
                    ORDER BY (cycle_closed_at IS NULL) DESC, cycle_number DESC
                    """,
                    (jira_key, current_sprint, source["cycle_id"]),
                ).fetchall()
                target = next((row for row in targets if row["cycle_closed_at"] is None), None)
                if target:
                    results.append(
                        _move_legacy_cycle(
                            db,
                            source,
                            sprint_id=current_sprint,
                            sprint_name=current_sprint,
                            sprint_state="active",
                            target=target,
                            close_cycle=False,
                        )
                    )
                else:
                    for obsolete in targets:
                        if not _target_is_empty(db, str(obsolete["cycle_id"])):
                            raise RuntimeError(
                                f"{jira_key}: historical destination {obsolete['cycle_id']} is not empty"
                            )
                        db.execute("DELETE FROM review_cycles WHERE cycle_id=?", (obsolete["cycle_id"],))
                    results.append(
                        _move_legacy_cycle(
                            db,
                            source,
                            sprint_id=current_sprint,
                            sprint_name=current_sprint,
                            sprint_state="active",
                            target=None,
                            close_cycle=False,
                        )
                    )
            else:
                results.append(
                    _move_legacy_cycle(
                        db,
                        source,
                        sprint_id="adhoc-2026-w30",
                        sprint_name="Ad hoc Review · 2026-W30",
                        sprint_state="closed",
                        target=None,
                        close_cycle=True,
                    )
                )

        legacy_remaining = int(
            db.execute(
                "SELECT COUNT(*) FROM review_cycles WHERE lower(trim(sprint_id))='legacy' OR backfilled=1"
            ).fetchone()[0]
        )
        legacy_children = {
            "description_snapshots": int(
                db.execute(
                    "SELECT COUNT(*) FROM description_snapshots WHERE lower(trim(sprint_id))='legacy' OR backfilled=1"
                ).fetchone()[0]
            ),
            "deferred_release_resources": int(
                db.execute(
                    "SELECT COUNT(*) FROM deferred_release_resources WHERE lower(trim(sprint_id))='legacy'"
                ).fetchone()[0]
            ),
            "review_run_groups": int(
                db.execute("SELECT COUNT(*) FROM review_run_groups WHERE backfilled=1").fetchone()[0]
            ),
        }
        if legacy_remaining or any(legacy_children.values()):
            raise RuntimeError(
                f"Legacy cleanup incomplete: cycles={legacy_remaining}, children={legacy_children}"
            )
        integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity check failed after migration: {integrity}")
        report = {
            "dry_run": dry_run,
            "database": str(database),
            "migrated": results,
            "legacy_cycles_remaining": legacy_remaining,
            "legacy_children_remaining": legacy_children,
            "integrity": integrity,
            "counts": {
                table: int(db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in (
                    "review_cycles",
                    "review_runs",
                    "findings",
                    "finding_handlings",
                    "pass_records",
                )
            },
        }
        if dry_run:
            db.rollback()
        else:
            db.commit()
        return report
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Move current-week review evidence out of Legacy Cycles before deleting Legacy classification."
    )
    parser.add_argument("database", type=Path)
    parser.add_argument("--current-sprint", required=True)
    parser.add_argument("--current-keys", required=True)
    parser.add_argument("--ad-hoc-keys", required=True)
    parser.add_argument("--apply", action="store_true", help="Commit the migration; default is a rolled-back dry run.")
    args = parser.parse_args()
    report = migrate(
        args.database,
        current_sprint=args.current_sprint.strip(),
        current_keys=_csv_keys(args.current_keys),
        ad_hoc_keys=_csv_keys(args.ad_hoc_keys),
        dry_run=not args.apply,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
