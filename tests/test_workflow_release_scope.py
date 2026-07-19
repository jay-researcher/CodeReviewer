from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from code_reviewer.workflow_store import WorkflowStore, review_scope_label, scope_people


class WorkflowReleaseScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = WorkflowStore(self.root / "workflow.db")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _report(self, name: str) -> str:
        path = self.root / name
        path.write_text(name, encoding="utf-8")
        return str(path)

    def _register(
        self,
        name: str,
        *,
        cycle_id: str = "",
        responsible: str = "",
        responsible_scope: object = "",
        application: str = "",
        release_line: str = "",
        run_group_id: str = "",
        findings: list[dict[str, str]] | None = None,
    ) -> str:
        return self.store.register_run(
            jira_key="ECHNL-7213",
            report_path=self._report(name),
            findings=findings or [],
            summary="Release scope",
            responsible=responsible,
            responsible_scope=responsible_scope,
            cycle_id=cycle_id,
            run_group_id=run_group_id,
            project_type="frontend",
            application=application,
            release_line=release_line,
        )

    def test_scope_people_accepts_all_persisted_encodings(self) -> None:
        self.assertEqual({"alice", "bob"}, scope_people("alice+bob"))
        self.assertEqual({"alice", "bob"}, scope_people('["alice", "bob"]'))
        self.assertEqual({"alice", "bob"}, scope_people("['alice', 'bob']"))
        self.assertEqual({"alice", "bob"}, scope_people(["alice", "bob"]))

    def test_run_scope_union_prevents_issue_owner_last_writer_wins(self) -> None:
        first = self._register(
            "first.md",
            responsible="alice",
            responsible_scope=["alice", "reviewer"],
        )
        second = self._register(
            "second.md",
            responsible="bob",
            responsible_scope="bob+auditor",
        )

        detail = self.store.issue_detail("ECHNL-7213")
        self.assertIsNotNone(detail)
        self.assertEqual("alice", detail["issue"]["responsible"])
        self.assertEqual(
            {"alice", "reviewer", "bob", "auditor"},
            set(detail["issue"]["responsible_scope"]),
        )
        self.assertEqual(1, len(self.store.list_issues(responsibles=["alice"])))
        self.assertEqual(1, len(self.store.list_issues(responsibles=["bob"])))
        self.assertEqual(1, len(self.store.list_issues(responsibles=["reviewer"])))
        self.assertEqual([], self.store.list_issues(responsibles=["charlie"]))
        with self.store.connect() as db:
            scopes = {
                row["id"]: row["responsible_scope"]
                for row in db.execute(
                    "SELECT id, responsible_scope FROM review_runs WHERE id IN (?, ?)",
                    (first, second),
                )
            }
        self.assertEqual({"alice", "reviewer"}, scope_people(scopes[first]))
        self.assertEqual({"bob", "auditor"}, scope_people(scopes[second]))

    def test_progress_and_finding_lineage_are_release_line_scoped(self) -> None:
        cycle = self.store.upsert_review_cycle(
            jira_key="ECHNL-7213",
            sprint_id="10085",
            mr_scope=[
                {"application": "iTrade Client", "release_line": "7.5.0"},
                {"application": "iTrade Client", "release_line": "7.5.1"},
                {"application": "DPS", "release_line": "DPS9"},
            ],
        )
        finding = {
            "index": "1",
            "severity": "High",
            "title": "Same fingerprint",
            "file": "src/shared.ts",
        }
        first = self._register(
            "itrade-750-first.md",
            cycle_id=str(cycle["cycle_id"]),
            application="iTrade Client",
            release_line="7.5.0",
            findings=[finding],
        )
        parallel = self._register(
            "itrade-751.md",
            cycle_id=str(cycle["cycle_id"]),
            application="iTrade Client",
            release_line="7.5.1",
            findings=[finding],
        )
        rescan = self._register(
            "itrade-750-rescan.md",
            cycle_id=str(cycle["cycle_id"]),
            application="iTrade Client",
            release_line="7.5.0",
            findings=[finding],
        )

        detail = self.store.issue_detail("ECHNL-7213")
        lineage = {
            run["id"]: run["findings"][0]["lineage_state"]
            for run in detail["runs"]
            if run["id"] in {first, parallel, rescan}
        }
        self.assertEqual("new", lineage[first])
        self.assertEqual("new", lineage[parallel])
        self.assertEqual("persisting", lineage[rescan])

        summary = self.store.list_issues(view_all=True)[0]
        progress = {
            (row["application"], row["release_line"]): row
            for row in summary["current_cycle"]["application_progress"]
        }
        self.assertEqual(2, progress[("iTrade Client", "7.5.0")]["report_count"])
        self.assertEqual(1, progress[("iTrade Client", "7.5.1")]["report_count"])
        self.assertEqual(0, progress[("DPS", "DPS9")]["report_count"])
        self.assertEqual(
            "iTrade Client 7.5.0",
            progress[("iTrade Client", "7.5.0")]["scope_label"],
        )
        self.assertEqual("DPS9", progress[("DPS", "DPS9")]["scope_label"])
        self.assertEqual("WVAdmin", review_scope_label("WVAdmin", "1.0"))

    def test_latest_logical_run_group_aggregates_all_application_reports(self) -> None:
        cycle = self.store.upsert_review_cycle(
            jira_key="ECHNL-7213",
            sprint_id="10086",
            mr_scope=[
                {"application": "iTrade Client", "release_line": "7.5.0"},
                {"application": "iTrade Client", "release_line": "7.5.1"},
                {"application": "DPS", "release_line": "DPS11"},
            ],
        )
        group = self.store.create_run_group(cycle_id=str(cycle["cycle_id"]))
        scopes = [
            ("iTrade Client", "7.5.0", "High"),
            ("iTrade Client", "7.5.1", "Critical"),
            ("DPS", "DPS11", "Medium"),
        ]
        for index, (application, release_line, severity) in enumerate(scopes, 1):
            self._register(
                f"scope-{index}.md",
                cycle_id=str(cycle["cycle_id"]),
                run_group_id=str(group["id"]),
                application=application,
                release_line=release_line,
                findings=[
                    {
                        "index": str(index),
                        "severity": severity,
                        "title": f"{application} {release_line}",
                        "file": f"src/{index}.txt",
                    }
                ],
            )

        detail = self.store.issue_detail("ECHNL-7213")
        logical = detail["latest_run_group"]
        self.assertEqual(3, logical["run_count"])
        self.assertEqual(3, logical["finding_count"])
        self.assertEqual(
            {"iTrade Client 7.5.0", "iTrade Client 7.5.1", "DPS11"},
            {finding["scope_label"] for finding in logical["findings"]},
        )
        summary = self.store.list_issues(view_all=True)[0]
        self.assertEqual(3, summary["finding_count"])
        self.assertEqual(
            {"High": 1, "Critical": 1, "Medium": 1},
            summary["severity_counts"],
        )

    def test_legacy_database_adds_release_line_without_losing_runs(self) -> None:
        legacy_path = self.root / "legacy.db"
        now = "2026-07-01T00:00:00+08:00"
        db = sqlite3.connect(legacy_path)
        try:
            db.executescript(
                """
                CREATE TABLE review_issues (
                    jira_key TEXT PRIMARY KEY, summary TEXT NOT NULL DEFAULT '',
                    responsible TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'not-reviewed',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    latest_run_id TEXT, passed_run_id TEXT
                );
                CREATE TABLE review_runs (
                    id TEXT PRIMARY KEY, jira_key TEXT NOT NULL,
                    report_path TEXT NOT NULL, report_fingerprint TEXT NOT NULL,
                    run_number INTEGER NOT NULL, conclusion TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'completed',
                    severity_counts_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(jira_key, report_fingerprint)
                );
                """
            )
            db.execute(
                "INSERT INTO review_issues VALUES('ECHNL-1', '', 'legacy', 'handling', ?, ?, 'run-1', NULL)",
                (now, now),
            )
            db.execute(
                "INSERT INTO review_runs VALUES('run-1', 'ECHNL-1', 'old.md', 'fp', 1, '', 'completed', '{}', ?)",
                (now,),
            )
            db.commit()
        finally:
            db.close()

        WorkflowStore(legacy_path)
        db = sqlite3.connect(legacy_path)
        try:
            columns = {row[1] for row in db.execute("PRAGMA table_info(review_runs)")}
            release_line = db.execute(
                "SELECT release_line FROM review_runs WHERE id='run-1'"
            ).fetchone()[0]
        finally:
            db.close()
        self.assertIn("release_line", columns)
        self.assertEqual("", release_line)


if __name__ == "__main__":
    unittest.main()
