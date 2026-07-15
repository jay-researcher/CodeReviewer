from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from code_reviewer.adf import text_adf
from code_reviewer.workflow_store import WorkflowStore


class WorkflowStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorkflowStore(Path(self.temp.name) / "workflow.db")
        self.report1 = Path(self.temp.name) / "wen.yi" / "ECHNL-1001_has-issue-high.md"
        self.report1.parent.mkdir()
        self.report1.write_text("run one", encoding="utf-8")
        self.finding = {"index": "1", "severity": "High", "title": "Unsafe call", "file": "src/a.py"}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _register(self, report: Path, findings: list[dict[str, str]]) -> str:
        with patch("code_reviewer.workflow_store.app_config_get", return_value=["Critical", "High"]):
            return self.store.register_run(
                jira_key="ECHNL-1001", report_path=str(report), findings=findings,
                summary="Review workflow", responsible="wen.yi",
            )

    def test_fixed_blocker_requires_clean_rescan(self) -> None:
        run_id = self._register(self.report1, [self.finding])
        detail = self.store.issue_detail("ECHNL-1001")
        finding_id = detail["runs"][0]["findings"][0]["id"]
        self.store.record_handling(
            finding_id=finding_id, disposition="fixed", note="Patched", actor="gerhard.guo", actor_role="developer"
        )
        self.assertFalse(self.store.pass_readiness("ECHNL-1001")["ready"])

        report2 = self.report1.with_name("ECHNL-1001_rescan-clean.md")
        report2.write_text("run two", encoding="utf-8")
        self._register(report2, [])
        self.assertTrue(self.store.pass_readiness("ECHNL-1001")["ready"])

    def test_developer_not_issue_requires_approval(self) -> None:
        self._register(self.report1, [self.finding])
        finding_id = self.store.issue_detail("ECHNL-1001")["runs"][0]["findings"][0]["id"]
        result = self.store.record_handling(
            finding_id=finding_id, disposition="not-issue", note="False positive", actor="gerhard.guo", actor_role="developer"
        )
        self.assertEqual(result["approval_status"], "pending")
        self.assertFalse(self.store.pass_readiness("ECHNL-1001")["ready"])
        self.store.approve_handling(result["handling_id"], "wen.yi", "auditor", approved=True, reason="Verified")
        self.assertTrue(self.store.pass_readiness("ECHNL-1001")["ready"])

    def test_manager_can_override_followup_with_adf_draft(self) -> None:
        self._register(self.report1, [self.finding])
        finding_id = self.store.issue_detail("ECHNL-1001")["runs"][0]["findings"][0]["id"]
        result = self.store.record_handling(
            finding_id=finding_id, disposition="follow-up", note="Accepted risk", actor="gerhard.guo", actor_role="developer",
            jira_summary="Track safer API", jira_description_adf=text_adf("Replace the unsafe call in the next release."),
        )
        self.assertTrue(result["draft_id"])
        self.assertFalse(self.store.pass_readiness("ECHNL-1001")["ready"])
        self.store.manager_override(result["handling_id"], "admin", "Risk accepted for this release")
        readiness = self.store.pass_readiness("ECHNL-1001")
        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["manager_exceptions"], 1)


if __name__ == "__main__":
    unittest.main()
