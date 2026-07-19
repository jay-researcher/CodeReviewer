from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from code_reviewer.workflow_store import WorkflowStore


class IssueHistoryApplicationReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.store = WorkflowStore(root / "workflow.db")
        self.report_dir = root / "wen.yi"
        self.report_dir.mkdir()
        self.finding = {
            "index": "1",
            "severity": "High",
            "title": "Blocking issue",
            "file": "src/example.ts",
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _report(self, name: str) -> Path:
        path = self.report_dir / name
        path.write_text(f"# {name}", encoding="utf-8")
        return path

    def _cycle(
        self,
        *,
        sprint_id: str,
        scope: list[dict[str, object]],
    ) -> dict[str, object]:
        return self.store.upsert_review_cycle(
            jira_key="ECHNL-7280",
            sprint_id=sprint_id,
            sprint_name=f"Sprint {sprint_id}",
            sprint_state="active",
            review_mode="issue",
            mr_scope=scope,
        )

    def _register(
        self,
        cycle: dict[str, object],
        *,
        application: str,
        report: str,
        findings: list[dict[str, str]],
    ) -> str:
        group = self.store.create_run_group(
            cycle_id=str(cycle["cycle_id"]),
            stable_fingerprint=f"{cycle['cycle_id']}:{application}:{report}",
        )
        with patch(
            "code_reviewer.workflow_store.app_config_get",
            return_value=["Critical", "High"],
        ):
            return self.store.register_run(
                jira_key="ECHNL-7280",
                report_path=str(self._report(report)),
                findings=findings,
                summary="Application readiness",
                responsible="wen.yi",
                cycle_id=str(cycle["cycle_id"]),
                run_group_id=str(group["id"]),
                application=application,
                stable_fingerprint=f"stable:{application}:{report}",
            )

    @staticmethod
    def _progress_by_application(cycle: dict[str, object]) -> dict[str, dict[str, object]]:
        return {
            str(item["application"]): item
            for item in cycle.get("application_progress", [])
        }

    def _summary(self) -> dict[str, object]:
        rows = self.store.list_issues(view_all=True)
        self.assertEqual(1, len(rows))
        return rows[0]

    def test_application_progress_is_isolated_by_review_cycle(self) -> None:
        first = self._cycle(
            sprint_id="10085",
            scope=[{"application": "iTrade Client", "iid": 11}],
        )
        self._register(
            first,
            application="iTrade Client",
            report="ECHNL-7280_cycle1.md",
            findings=[self.finding],
        )

        second = self._cycle(
            sprint_id="10086",
            scope=[{"application": "DPS", "iid": 12}],
        )
        self._register(
            second,
            application="DPS",
            report="ECHNL-7280_cycle2.md",
            findings=[],
        )
        with self.store.connect() as database:
            database.execute(
                "UPDATE review_cycles SET pass_status='passed' WHERE cycle_id=?",
                (second["cycle_id"],),
            )

        summary = self._summary()
        cycles = {str(item["sprint_id"]): item for item in summary["cycles"]}
        first_progress = self._progress_by_application(cycles["10085"])
        second_progress = self._progress_by_application(cycles["10086"])

        self.assertEqual({"iTrade Client"}, set(first_progress))
        self.assertEqual("handling", first_progress["iTrade Client"]["state"])
        self.assertEqual({"DPS"}, set(second_progress))
        self.assertEqual("review-pass", second_progress["DPS"]["state"])

    def test_cross_application_issue_keeps_independent_states(self) -> None:
        cycle = self._cycle(
            sprint_id="10087",
            scope=[
                {"application": "iTrade Client", "iid": 21},
                {"application": "Services Terminal", "iid": 22},
            ],
        )
        self._register(
            cycle,
            application="iTrade Client",
            report="ECHNL-7280_itrade.md",
            findings=[],
        )
        self._register(
            cycle,
            application="Services Terminal",
            report="ECHNL-7280_terminal.md",
            findings=[self.finding],
        )

        summary = self._summary()
        current = self._progress_by_application(summary["current_cycle"])
        self.assertEqual("ready-for-pass", current["iTrade Client"]["state"])
        self.assertEqual("handling", current["Services Terminal"]["state"])
        self.assertEqual(0, current["iTrade Client"]["pending_blockers"])
        self.assertEqual(1, current["Services Terminal"]["pending_blockers"])

    def test_explicit_application_run_does_not_create_false_unmapped_scope(self) -> None:
        cycle = self._cycle(sprint_id="10088", scope=[])
        self._register(
            cycle,
            application="WVAdmin",
            report="ECHNL-7280_wvadmin.md",
            findings=[],
        )

        summary = self._summary()
        current = self._progress_by_application(summary["current_cycle"])
        self.assertEqual({"WVAdmin"}, set(current))
        self.assertNotIn("Unmapped", current)

    def test_scope_without_report_is_not_ready_and_unmapped_stays_blocking(self) -> None:
        cycle = self._cycle(
            sprint_id="10089",
            scope=[
                {"application": "DPS", "iid": 31},
                {"iid": 32, "project_path": "unknown/team/project"},
            ],
        )
        summary = self._summary()
        current = self._progress_by_application(summary["current_cycle"])
        self.assertEqual("without-report", current["DPS"]["state"])
        self.assertEqual("without-report", current["Unmapped"]["state"])
        self.assertEqual(0, current["DPS"]["report_count"])
        self.assertEqual(0, current["Unmapped"]["report_count"])

    def test_generating_and_failed_states_follow_the_current_cycle_attempt(self) -> None:
        cycle = self._cycle(
            sprint_id="10091",
            scope=[{"application": "WVAdmin", "iid": 51}],
        )
        group = self.store.create_run_group(
            cycle_id=str(cycle["cycle_id"]),
            status="running",
            stable_fingerprint="wvadmin-running",
        )
        generating = self._progress_by_application(self._summary()["current_cycle"])
        self.assertEqual("generating", generating["WVAdmin"]["state"])

        with patch(
            "code_reviewer.workflow_store.app_config_get",
            return_value=["Critical", "High"],
        ):
            run_id = self.store.register_run(
                jira_key="ECHNL-7280",
                report_path=str(self._report("ECHNL-7280_failed.md")),
                findings=[],
                summary="Application readiness",
                responsible="wen.yi",
                cycle_id=str(cycle["cycle_id"]),
                run_group_id=str(group["id"]),
                application="WVAdmin",
            )
        with self.store.connect() as database:
            database.execute(
                "UPDATE review_runs SET status='generation-failed' WHERE id=?",
                (run_id,),
            )
            database.execute(
                "UPDATE review_run_groups SET status='failed' WHERE id=?",
                (group["id"],),
            )
        failed = self._progress_by_application(self._summary()["current_cycle"])
        self.assertEqual("failed", failed["WVAdmin"]["state"])

    def test_application_column_is_persisted_on_review_run(self) -> None:
        cycle = self._cycle(
            sprint_id="10090",
            scope=[{"application": "Services Terminal", "iid": 41}],
        )
        run_id = self._register(
            cycle,
            application="Services Terminal",
            report="ECHNL-7280_persisted.md",
            findings=[],
        )
        with self.store.connect() as database:
            row = database.execute(
                "SELECT application, cycle_id FROM review_runs WHERE id=?",
                (run_id,),
            ).fetchone()
        self.assertEqual("Services Terminal", row["application"])
        self.assertEqual(cycle["cycle_id"], row["cycle_id"])


if __name__ == "__main__":
    unittest.main()
