import unittest
from unittest.mock import patch

from code_reviewer.jira_client import (
    JiraClient,
    JiraIssue,
    SprintMembership,
    parse_sprint_memberships,
    select_current_sprint,
)
from code_reviewer.llm_provider import _review_prompt
from code_reviewer.models import ChangedFile, ReviewInput
from code_reviewer.review_service import (
    deferred_release_resource_identity,
    reconcile_deferred_release_resources,
    review_sprint_merge_requests,
    run_review_from_payload,
    review_fingerprint_from_merge_requests,
    select_cycle_mr_revisions,
    _select_fetched_cycle_revisions,
)


def _issue(key: str, status: str) -> JiraIssue:
    return JiraIssue(
        key=key,
        summary=f"Summary {key}",
        description="Original requirement",
        assignee="Developer",
        status=status,
        sprint="Current Sprint",
        issue_type="Story",
        labels=[],
        sprint_memberships=[SprintMembership(id="102", name="Current Sprint", state="active")],
        current_sprint_id="102",
        current_sprint_state="active",
    )


class SprintMembershipTests(unittest.TestCase):
    def test_preserves_complete_active_and_future_memberships(self) -> None:
        memberships = parse_sprint_memberships(
            [
                {"id": 100, "name": "Old Sprint", "state": "closed", "completeDate": "2026-06-30"},
                {"id": 101, "name": "Current Sprint", "state": "active", "startDate": "2026-07-01"},
                {"id": 102, "name": "Next Sprint", "state": "future", "startDate": "2026-07-20"},
            ]
        )
        self.assertEqual([item.state for item in memberships], ["complete", "active", "future"])
        self.assertEqual(select_current_sprint(memberships).id, "101")
        self.assertEqual(select_current_sprint(memberships, preferred_id="100").name, "Old Sprint")

    def test_parses_legacy_greenhopper_sprint_and_chooses_recent_complete(self) -> None:
        memberships = parse_sprint_memberships(
            [
                "com.atlassian.greenhopper.service.sprint.Sprint@1[id=8,state=CLOSED,name=Sprint A,completeDate=2026-01-01]",
                "com.atlassian.greenhopper.service.sprint.Sprint@2[id=9,state=CLOSED,name=Sprint B,completeDate=2026-02-01]",
            ]
        )
        self.assertEqual(select_current_sprint(memberships).name, "Sprint B")

    def test_preflight_exposes_batch_preview_and_final_sprint_modes(self) -> None:
        client = object.__new__(JiraClient)
        with patch.object(JiraClient, "search_issues_by_sprint", return_value=[_issue("ECHNL-1", "Development Done")]):
            final = client.sprint_preflight("102", "ECHNL")
        self.assertEqual(final["review_mode"], "final-sprint")
        self.assertTrue(final["all_development_done"])

        with patch.object(
            JiraClient,
            "search_issues_by_sprint",
            return_value=[_issue("ECHNL-1", "Development Done"), _issue("ECHNL-2", "In Progress")],
        ):
            preview = client.sprint_preflight("102", "ECHNL")
        self.assertEqual(preview["review_mode"], "batch-preview")
        self.assertTrue(preview["requires_confirmation"])
        self.assertEqual(preview["not_development_done_issues"][0]["jira_key"], "ECHNL-2")

    def test_sprint_review_preserves_comment_warnings_as_partial_result(self) -> None:
        progress_events: list[dict] = []

        class Client:
            warnings = [
                {
                    "jira_key": "ECHNL-2",
                    "stage": "jira-comments",
                    "endpoint": "/rest/api/3/issue/ECHNL-2/comment",
                    "error": "timed out",
                }
            ]

            def search_issues_by_sprint(self, sprint, project_key="", progress=None):
                self.sprint = sprint
                self.project_key = project_key
                if progress:
                    progress(
                        {
                            "event": "jira-comments-warning",
                            "message": "Loading Jira comments 2/2 · warning for ECHNL-2",
                            "current": 2,
                            "total": 2,
                            "jira_key": "ECHNL-2",
                        }
                    )
                return [_issue("ECHNL-1", "Development Done"), _issue("ECHNL-2", "Development Done")]

        with (
            patch("code_reviewer.review_service.JiraClient", return_value=Client()),
            patch(
                "code_reviewer.review_service._review_issue_collection_merge_requests",
                side_effect=lambda **kwargs: kwargs["source_metadata"],
            ),
        ):
            result = review_sprint_merge_requests("Sprint 1", progress=progress_events.append)

        self.assertTrue(result["partial"])
        self.assertEqual("ECHNL-2", result["jira_warnings"][0]["jira_key"])
        self.assertTrue(any(event["event"] == "jira-comments-warning" for event in progress_events))


class RevisionBoundaryTests(unittest.TestCase):
    def test_review_fingerprint_changes_with_head_sha(self) -> None:
        base = {"mr_url": "https://gitlab.example/group/api/-/merge_requests/7", "head_sha": "a" * 40}
        changed = {**base, "head_sha": "b" * 40}
        self.assertNotEqual(
            review_fingerprint_from_merge_requests([base])["stable_fingerprint"],
            review_fingerprint_from_merge_requests([changed])["stable_fingerprint"],
        )

    def test_unchanged_previous_revision_is_excluded_but_new_head_is_included(self) -> None:
        previous = [{"project_path": "group/api", "mr_id": "7", "head_sha": "a" * 40}]
        candidates = [
            {"project_path": "group/api", "mr_id": "7", "head_sha": "a" * 40},
            {"project_path": "group/api", "mr_id": "7", "head_sha": "b" * 40},
        ]
        selection = select_cycle_mr_revisions(candidates, previous)
        self.assertEqual(selection["included_count"], 1)
        self.assertEqual(selection["excluded"][0]["selection_reason"], "reviewed-unchanged-revision")
        self.assertEqual(selection["included"][0]["head_sha"], "b" * 40)

    def test_explicit_decision_is_auditable_and_can_override_history(self) -> None:
        candidate = {"project_path": "group/web", "mr_id": "3", "head_sha": "c" * 40}
        identity = select_cycle_mr_revisions([candidate], [candidate])
        revision_key = identity["excluded"][0]["revision_key"]
        selected = select_cycle_mr_revisions([candidate], [candidate], {revision_key: "include"})
        self.assertEqual(selected["included"][0]["selection_reason"], "explicit-include")

    def test_service_selection_consumes_persisted_cycle_scope(self) -> None:
        class Store:
            def list_cycles(self, _jira_key):
                return [{"cycle_id": "old", "mr_scope_json": [
                    {"project_path": "group/api", "mr_id": "7", "head_sha": "a" * 40}
                ]}]

        old = ReviewInput(
            project="api", mr_id="7", mr_url="https://gitlab.example/group/api/-/merge_requests/7",
            commit="a" * 40, metadata={"gitlab_project_path": "group/api"},
        )
        changed = ReviewInput(
            project="api", mr_id="7", mr_url="https://gitlab.example/group/api/-/merge_requests/7",
            commit="b" * 40, metadata={"gitlab_project_path": "group/api"},
        )
        with patch("code_reviewer.workflow_store.workflow_store", return_value=Store()):
            selected, excluded = _select_fetched_cycle_revisions("ECHNL-1", [old, changed])
        self.assertEqual([item.commit for item in selected], ["b" * 40])
        self.assertEqual(excluded[0]["selection_reason"], "reviewed-unchanged-revision")

    def test_deferred_reconciliation_limits_release_scope_and_head_revision(self) -> None:
        current = {
            "jira_key": "ECHNL-1",
            "sprint_id": "102",
            "cycle_id": "cycle-2",
            "project_path": "build/dps",
            "mr_id": "9",
            "head_sha": "d" * 40,
        }
        old = {**current, "sprint_id": "101", "cycle_id": "cycle-1", "head_sha": "e" * 40}
        result = reconcile_deferred_release_resources(
            [old, current], sprint_id="102", cycle_ids=["cycle-2"], contained_commit_shas=["d" * 40]
        )
        self.assertEqual(result["verified_count"], 1)
        self.assertEqual(result["out_of_scope_count"], 1)
        self.assertEqual(result["release_gate_status"], "verified")
        self.assertTrue(deferred_release_resource_identity(current)["resource_fingerprint"])


class PromptBoundaryTests(unittest.TestCase):
    def test_prompt_labels_current_target_and_historical_context(self) -> None:
        review_input = ReviewInput(
            project="api",
            jira_key="ECHNL-1",
            sprint="Current Sprint",
            source_branch="feature/ECHNL-1",
            target_branch="main",
            commit="f" * 40,
            changed_files=[ChangedFile(path="src/current.php", additions=1, diff="+current_revision")],
            raw_diff="+current_revision",
            metadata={
                "current_review_scope": {"cycle_id": "cycle-2", "current_follow_up_comment": "Current fix"},
                "current_target_context": {"target_branch": "main", "policy": "context only"},
                "historical_requirement_context": {
                    "summary": "Old requirement only",
                    "excludes_previous_cycle_diffs": True,
                },
                "project_context": "target branch dependency context",
            },
        )
        prompt = _review_prompt(review_input)
        self.assertIn("Current Review Scope (the only actionable review target)", prompt)
        self.assertIn("Current Target Context (compatibility/impact context only", prompt)
        self.assertIn("Historical Requirement Context (background only", prompt)
        self.assertIn("Current Review Scope incremental diff", prompt)
        self.assertIn("Old requirement only", prompt)

    def test_payload_sprint_preflight_is_web_callable(self) -> None:
        payload = {"valid": True, "accessible": True, "review_mode": "final-sprint", "issue_count": 1}
        with patch("code_reviewer.review_service.sprint_review_preflight", return_value=payload):
            result = run_review_from_payload({"mode": "sprint-preflight", "sprint": "102"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["preflight"]["review_mode"], "final-sprint")


if __name__ == "__main__":
    unittest.main()
