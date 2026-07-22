from __future__ import annotations

import html
import hmac
import hashlib
import io
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import socket
import ssl
import threading
import time
import traceback
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import request as urlrequest
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import __version__
from .config import (
    DATA_DIR,
    ROOT_DIR,
    app_config_get,
    app_config_int,
    ensure_directories,
    gitnexus_config,
    git_tools_config_path,
    jira_spaces,
    llm_config,
    load_projects,
    report_min_severity,
    report_output_dir,
    sprint_prefixes,
    clear_config_cache,
    load_base_config_payload,
    load_effective_config_payload,
)
from .config_store import (
    ConfigRevisionConflict,
    EffectiveConfigStore,
    load_web_config_overrides,
)
from .local_workspaces import git_tools_project_entries
from .network import check_network_dict
from .report import handling_result_filename, render_handling_result_template_from_markdown
from .review_scope import ReviewScope, review_scope_for_merge_request
from .review_service import (
    ReviewCancelled,
    jira_issue_review_fingerprint,
    review_fingerprint_from_merge_requests,
    review_jira_filter_merge_requests,
    review_jira_issue_merge_requests,
    review_sprint_merge_requests,
    run_review_from_payload,
    sprint_review_preflight,
)
from .storage import load_review_history
from .adf import ADFValidationError, empty_adf, render_adf_html, validate_adf
from .workflow_store import (
    blocking_severities,
    report_fingerprint,
    review_scope_label,
    scope_people,
    workflow_store,
)


WEB_USERS_FILE = Path(os.getenv("WEB_USERS_FILE", str(DATA_DIR / "web_users.json"))).expanduser()
WEB_USER_AUDIT_FILE = Path(os.getenv("WEB_USER_AUDIT_FILE", str(DATA_DIR / "web_user_audit.jsonl"))).expanduser()
WEB_IP_WHITELIST_FILE = Path(os.getenv("WEB_IP_WHITELIST_FILE", str(DATA_DIR / "web_ip_whitelist.txt"))).expanduser()
WEB_THREADS_DIR = Path(os.getenv("WEB_THREADS_DIR", str(DATA_DIR / "web_threads"))).expanduser()
WEB_STATIC_DIR = ROOT_DIR / "code_reviewer" / "static"
WEB_SESSION_COOKIE = "code_reviewer_session"
WEB_SESSIONS: dict[str, dict[str, object]] = {}
ROBOT_CHALLENGES: dict[str, dict[str, object]] = {}
WEB_REVIEW_JOBS: dict[str, dict[str, object]] = {}
WEB_REVIEW_JOBS_LOCK = threading.Lock()
WEB_COVERAGE_JOBS: dict[str, dict[str, object]] = {}
WEB_COVERAGE_JOBS_LOCK = threading.Lock()
WEB_COVERAGE_EXECUTION_LOCK = threading.Lock()
WEB_USERS_LOCK = threading.RLock()
WEB_SESSIONS_LOCK = threading.RLock()
WEB_USER_IDEMPOTENCY: dict[str, dict[str, object]] = {}
WEB_USER_IDEMPOTENCY_LOCK = threading.RLock()
WEB_USER_IDEMPOTENCY_TTL_SECONDS = 10 * 60
# Review execution mutates process environment for provider/report settings.  Keep
# jobs visible and cancellable while queued, but execute only one review per Web
# process so concurrent requests cannot leak configuration into each other.
WEB_REVIEW_EXECUTION_LOCK = threading.Lock()
PASSWORD_SYMBOLS = "!@#$%&*?"
PASSWORD_HASH_ITERATIONS = 310_000
SESSION_TTL_SECONDS = 8 * 60 * 60
CHALLENGE_TTL_SECONDS = 10 * 60
JOB_TTL_SECONDS = 2 * 60 * 60
COVERAGE_JOB_TTL_SECONDS = 30 * 60
COVERAGE_RESULT_CACHE_SECONDS = 2 * 60
WEB_ROLES = {"manager", "auditor", "developer"}


def _normalize_merge_request_url(value: object) -> str:
    """Normalize a pasted MR URL without weakening its structural validation."""
    return re.sub(r"\s+", "", _text(value)).strip()


def _is_valid_merge_request_url(value: object) -> bool:
    mr_url = _normalize_merge_request_url(value)
    parsed = urlparse(mr_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return False
    return re.fullmatch(r"/.+/-/merge_requests/\d+/?", parsed.path, re.I) is not None


class CodeReviewerHandler(BaseHTTPRequestHandler):
    server_version = "CodeReviewer/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if not self._client_ip_allowed():
            self._send_ip_forbidden(parsed.path)
            return
        if parsed.path == "/assets/login-code-review-bg.png":
            asset = WEB_STATIC_DIR / "login-code-review-bg.png"
            if not asset.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            content = asset.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)
            return
        if parsed.path == "/assets/ttl-jay-crystal-logo.png":
            asset = WEB_STATIC_DIR / "ttl-jay-crystal-logo.png"
            if not asset.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            content = asset.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)
            return
        if parsed.path == "/login":
            self._send_html(render_login())
            return
        if parsed.path == "/api/login-challenge":
            self._send_json({"ok": True, "challenge": new_robot_challenge()})
            return
        if parsed.path == "/api/version":
            self._send_json({"ok": True, "version": app_version()})
            return
        if parsed.path == "/api/health":
            # Login-page health is deliberately sanitized.  Detailed external
            # connectivity hints are only available after authentication.
            self._send_json(web_health_snapshot(details=False))
            return
        if parsed.path == "/logout":
            self._logout()
            return
        user = self._current_user()
        if not user:
            if parsed.path.startswith("/api/") or parsed.path.startswith("/reports/") or parsed.path.startswith("/download/"):
                self._send_json({"ok": False, "error": "Authentication required"}, status=HTTPStatus.UNAUTHORIZED)
            else:
                self._redirect("/login")
            return
        if parsed.path == "/":
            self._send_html(render_index(user))
            return
        if parsed.path == "/api/me":
            role = _web_user_role(user)
            must_change_password = bool(_web_user_record(user).get("must_change_password"))
            self._send_json(
                {
                    "username": user,
                    "role": role,
                    "permissions": _web_user_permissions(user),
                    "is_admin": role == "manager",
                    "must_change_password": must_change_password,
                    "version": app_version(),
                }
            )
            return
        if bool(_web_user_record(user).get("must_change_password")):
            self._send_json(
                {"ok": False, "error": "Password change required before continuing.", "password_change_required": True},
                status=HTTPStatus.FORBIDDEN,
            )
            return
        if parsed.path == "/api/health-details":
            self._send_json(web_health_snapshot(details=True))
            return
        if parsed.path == "/api/admin/users":
            if _web_user_role(user) != "manager":
                self._send_json({"ok": False, "error": "User management is only available to Manager users."}, status=HTTPStatus.FORBIDDEN)
                return
            self._send_json(
                {
                    "ok": True,
                    "users": list_managed_web_users(),
                    "roles": ["developer", "auditor", "manager"],
                    "responsible_options": managed_responsible_options(),
                }
            )
            return
        if parsed.path == "/api/admin/configuration":
            if _web_user_role(user) != "manager":
                self._send_json({"ok": False, "error": "Configuration is only available to Manager users."}, status=HTTPStatus.FORBIDDEN)
                return
            try:
                self._send_json(web_configuration_payload())
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/public-release-notes":
            notes_path = ROOT_DIR / "7.x-docs" / "public release notes.md"
            source_error = ""
            try:
                content = notes_path.read_text(encoding="utf-8")
                updated_at = datetime.fromtimestamp(
                    notes_path.stat().st_mtime, tz=timezone(timedelta(hours=8))
                ).isoformat(timespec="seconds")
            except (OSError, UnicodeError) as exc:
                # Release Notes are auxiliary content. A missing/unreadable file
                # must never tear down the HTTP response and surface as the
                # browser's opaque "Failed to fetch" network error.
                content = (
                    "# CodeReviewer Release Notes\n\n"
                    "The public release summary is temporarily unavailable. "
                    "Core review functions are not affected."
                )
                updated_at = ""
                source_error = type(exc).__name__
            self._send_json(
                {
                    "ok": True,
                    "markdown": content,
                    "updated_at": updated_at,
                    "source_available": not source_error,
                    "source_error": source_error,
                }
            )
            return
        if parsed.path == "/assets/adf-editor.js":
            asset = WEB_STATIC_DIR / "adf-editor.js"
            if not asset.is_file():
                self._send_json({"ok": False, "error": "ADF editor asset is not built."}, status=HTTPStatus.NOT_FOUND)
                return
            content = asset.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            self.wfile.write(content)
            return
        if parsed.path == "/api/projects":
            self._send_json({"projects": list_gitlab_projects_for_user(user)})
            return
        if parsed.path == "/api/reports":
            query = parse_qs(parsed.query)
            self._send_json(
                {
                    "reports": list_reports(
                        _text((query.get("output_dir") or [""])[0]),
                        user=user,
                        search=_text((query.get("q") or [""])[0]),
                        days=_int_query(query, "days", _default_report_history_days()),
                    )
                }
            )
            return
        if parsed.path == "/api/report-check":
            query = parse_qs(parsed.query)
            self._send_json(
                report_reuse_check(
                    jira_key=_text((query.get("jira") or [""])[0]),
                    output_dir=_text((query.get("output_dir") or [""])[0]),
                    user=user,
                    state=_text((query.get("state") or [""])[0]),
                )
            )
            return
        if parsed.path == "/api/report-thread":
            query = parse_qs(parsed.query)
            self._send_report_thread(
                report_name=_text((query.get("report") or [""])[0]),
                output_dir=_text((query.get("output_dir") or [""])[0]),
                user=user,
            )
            return
        if parsed.path == "/api/report-markdown":
            query = parse_qs(parsed.query)
            self._send_report_markdown(
                report_name=_text((query.get("report") or [""])[0]),
                output_dir=_text((query.get("output_dir") or [""])[0]),
                user=user,
            )
            return
        if parsed.path == "/api/report-compare":
            query = parse_qs(parsed.query)
            self._send_report_compare(
                report_name=_text((query.get("report") or [""])[0]),
                output_dir=_text((query.get("output_dir") or [""])[0]),
                user=user,
            )
            return
        if parsed.path == "/api/responsibles":
            query = parse_qs(parsed.query)
            self._send_json(
                {
                    "responsibles": list_responsibles(
                        _text((query.get("output_dir") or [""])[0]),
                        user=user,
                        days=_int_query(query, "days", _default_report_history_days()),
                    )
                }
            )
            return
        if parsed.path == "/api/history":
            query = parse_qs(parsed.query)
            limit = int((query.get("limit") or ["100"])[0])
            try:
                history = [_web_history_item(item) for item in _filter_history_for_user(load_review_history(limit=limit), user)]
                self._send_json({"history": history})
            except Exception as exc:
                self._send_json({"history": [], "warning": f"Review history unavailable: {exc}"})
            return
        if parsed.path == "/api/issue-reviews":
            try:
                _sync_workflow_history()
                self._send_json({"ok": True, "issues": _workflow_issues_for_user(user)})
            except Exception as exc:
                self._send_json({"ok": False, "issues": [], "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if parsed.path.startswith("/api/issue-reviews/"):
            jira_key = unquote(parsed.path.removeprefix("/api/issue-reviews/")).strip().upper()
            try:
                _sync_workflow_history()
                _require_issue_access(user, jira_key)
                detail = workflow_store().issue_detail(jira_key)
                if not detail:
                    self._send_json({"ok": False, "error": "Issue review was not found."}, status=HTTPStatus.NOT_FOUND)
                else:
                    _enrich_issue_review_finding_details(detail)
                    self._send_json({"ok": True, **detail, "permissions": _web_user_permissions(user), "role": _web_user_role(user)})
            except PermissionError as exc:
                self._send_json({"ok": False, "error": str(exc) or "Forbidden"}, status=HTTPStatus.FORBIDDEN)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/jira-drafts":
            try:
                _sync_workflow_history()
                drafts = workflow_store().list_drafts()
                visible = [item for item in drafts if _issue_access_allowed(user, str(item.get("jira_key") or ""))]
                self._send_json({"ok": True, "drafts": visible})
            except Exception as exc:
                self._send_json({"ok": False, "drafts": [], "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if parsed.path.startswith("/api/draft-attachments/"):
            attachment_id = unquote(parsed.path.removeprefix("/api/draft-attachments/")).strip()
            try:
                attachment = workflow_store().attachment(attachment_id)
                if not attachment:
                    self._send_json({"ok": False, "error": "Attachment was not found."}, status=HTTPStatus.NOT_FOUND)
                    return
                _require_issue_access(user, str(attachment.get("jira_key") or ""))
                target = Path(str(attachment.get("storage_path") or ""))
                if not target.is_file():
                    self._send_json({"ok": False, "error": "Attachment file was not found."}, status=HTTPStatus.NOT_FOUND)
                    return
                content = target.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", str(attachment.get("media_type") or "application/octet-stream"))
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers()
                self.wfile.write(content)
            except PermissionError as exc:
                self._send_json({"ok": False, "error": str(exc) or "Forbidden"}, status=HTTPStatus.FORBIDDEN)
            return
        if parsed.path == "/api/review-coverage":
            query = parse_qs(parsed.query)
            try:
                self._send_json(
                    {
                        "ok": True,
                        **build_review_coverage(
                            user=user,
                            jira_keys=_text((query.get("jira") or [""])[0]),
                            sprint=_text((query.get("sprint") or [""])[0]),
                            jira_filter=_text((query.get("jira_filter") or [""])[0]),
                        ),
                    }
                )
            except PermissionError as exc:
                self._send_json({"ok": False, "error": str(exc) or "Forbidden"}, status=HTTPStatus.FORBIDDEN)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/review-coverage-jobs":
            self._send_json({"ok": True, "job": latest_coverage_job_snapshot(user)})
            return
        if parsed.path.startswith("/api/review-coverage-jobs/"):
            job_id = parsed.path.removeprefix("/api/review-coverage-jobs/").strip("/")
            job = coverage_job_snapshot(job_id, user)
            if not job:
                self._send_json({"ok": False, "error": "Coverage scan job was not found."}, status=HTTPStatus.NOT_FOUND)
            else:
                self._send_json({"ok": True, "job": job})
            return
        if parsed.path == "/api/sprints":
            if not _web_user_permissions(user)["run_sprint_review"]:
                self._send_json({"ok": False, "error": "Sprint search is only available to Manager users."}, status=HTTPStatus.FORBIDDEN)
                return
            query = parse_qs(parsed.query)
            self._send_json({"ok": True, "sprints": recent_workflow_sprints(_text((query.get("q") or [""])[0]))})
            return
        if parsed.path == "/api/sprint-preflight":
            if not _web_user_permissions(user)["run_sprint_review"]:
                self._send_json({"ok": False, "error": "Sprint preflight is only available to Manager users."}, status=HTTPStatus.FORBIDDEN)
                return
            query = parse_qs(parsed.query)
            try:
                self._send_json({"ok": True, **sprint_review_preflight(_text((query.get("sprint") or [""])[0]))})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/config":
            self._send_json(
                {
                    "llm": llm_config(),
                    "gitnexus": gitnexus_config(),
                    "report_output_dir": str(report_output_dir()),
                    "report_min_severity": report_min_severity(),
                    "jira_spaces": jira_spaces(),
                    "sprint_prefixes": sprint_prefixes(),
                }
            )
            return
        if parsed.path == "/api/network-check":
            self._send_json(check_network_dict())
            return
        if parsed.path == "/api/reviews":
            query = parse_qs(parsed.query)
            limit = int((query.get("limit") or ["50"])[0])
            self._send_json({"ok": True, "jobs": list_review_job_snapshots(user, limit=limit)})
            return
        if parsed.path.startswith("/api/reviews/"):
            self._send_review_job(parsed.path.removeprefix("/api/reviews/"), user=user)
            return
        if parsed.path.startswith("/reports/"):
            query = parse_qs(parsed.query)
            self._send_report(
                parsed.path.removeprefix("/reports/"),
                output_dir=_text((query.get("output_dir") or [""])[0]),
                user=user,
            )
            return
        if parsed.path.startswith("/download/report/"):
            query = parse_qs(parsed.query)
            self._send_report(
                parsed.path.removeprefix("/download/report/"),
                output_dir=_text((query.get("output_dir") or [""])[0]),
                attachment=True,
                user=user,
            )
            return
        if parsed.path.startswith("/download/handling/"):
            query = parse_qs(parsed.query)
            self._send_handling_result(
                parsed.path.removeprefix("/download/handling/"),
                output_dir=_text((query.get("output_dir") or [""])[0]),
                user=user,
            )
            return
        if parsed.path.startswith("/download/responsible/"):
            query = parse_qs(parsed.query)
            self._send_responsible_zip(
                parsed.path.removeprefix("/download/responsible/"),
                output_dir=_text((query.get("output_dir") or [""])[0]),
                user=user,
            )
            return
        self._send_json({"ok": False, "error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not self._client_ip_allowed():
            self._send_ip_forbidden(parsed.path)
            return
        if not self._request_origin_allowed():
            self._send_json({"ok": False, "error": "Cross-origin request rejected."}, status=HTTPStatus.FORBIDDEN)
            return
        if parsed.path == "/api/login":
            self._handle_login()
            return
        if not self._current_user():
            self._send_json({"ok": False, "error": "Authentication required"}, status=HTTPStatus.UNAUTHORIZED)
            return
        if parsed.path != "/api/change-password" and bool(_web_user_record(self._current_user()).get("must_change_password")):
            self._send_json(
                {"ok": False, "error": "Password change required before continuing.", "password_change_required": True},
                status=HTTPStatus.FORBIDDEN,
            )
            return
        if parsed.path == "/api/reviews":
            self._handle_review()
            return
        if parsed.path == "/api/review-coverage-jobs":
            self._handle_coverage_job()
            return
        if parsed.path.startswith("/api/reviews/") and parsed.path.endswith("/stop"):
            user = self._current_user()
            self._handle_review_stop(parsed.path.removeprefix("/api/reviews/").removesuffix("/stop"), user)
            return
        if parsed.path.startswith("/api/reviews/") and parsed.path.endswith("/pause"):
            user = self._current_user()
            self._handle_review_pause(parsed.path.removeprefix("/api/reviews/").removesuffix("/pause"), user)
            return
        if parsed.path.startswith("/api/reviews/") and parsed.path.endswith("/resume"):
            user = self._current_user()
            self._handle_review_resume(parsed.path.removeprefix("/api/reviews/").removesuffix("/resume"), user)
            return
        if parsed.path == "/api/report-thread/message":
            self._handle_thread_message()
            return
        if parsed.path == "/api/report-thread/followups":
            self._handle_thread_followups()
            return
        if parsed.path == "/api/report-thread/ai-chat":
            self._handle_thread_ai_chat()
            return
        if parsed.path == "/api/workflow/handling":
            self._handle_workflow_handling()
            return
        if parsed.path == "/api/workflow/handling/approve":
            self._handle_workflow_approval()
            return
        if parsed.path == "/api/workflow/handling/manager-override":
            self._handle_workflow_manager_override()
            return
        if parsed.path == "/api/workflow/pass":
            self._handle_workflow_pass()
            return
        if parsed.path == "/api/workflow/discussion":
            self._handle_workflow_discussion()
            return
        if parsed.path == "/api/jira-drafts/update":
            self._handle_jira_draft_update()
            return
        if parsed.path == "/api/jira-drafts/attachment":
            self._handle_jira_draft_attachment()
            return
        if parsed.path == "/api/adf/render":
            self._handle_adf_render()
            return
        if parsed.path == "/api/change-password":
            self._handle_change_password()
            return
        if parsed.path == "/api/admin/users/save":
            self._handle_admin_user_save()
            return
        if parsed.path == "/api/admin/users/reset-password":
            self._handle_admin_user_password_reset()
            return
        if parsed.path == "/api/admin/configuration/save":
            try:
                self._send_json(save_web_configuration(self._current_user(), self._read_json_body()))
            except PermissionError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.FORBIDDEN)
            except ConfigRevisionConflict as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/admin/configuration/restore":
            try:
                self._send_json(restore_web_configuration(self._current_user(), self._read_json_body()))
            except PermissionError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.FORBIDDEN)
            except ConfigRevisionConflict as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/admin/configuration/project":
            try:
                self._send_json(mutate_web_configuration_project(self._current_user(), self._read_json_body()))
            except PermissionError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.FORBIDDEN)
            except ConfigRevisionConflict as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"ok": False, "error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def _read_json_body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        max_length = int(app_config_get("review_workflow.request_max_bytes", 16 * 1024 * 1024) or 16 * 1024 * 1024)
        if length < 0 or length > max_length:
            raise ValueError(f"Request body exceeds {max_length} bytes.")
        payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        if not isinstance(payload, dict):
            raise ValueError("JSON request body must be an object.")
        return payload

    def _request_origin_allowed(self) -> bool:
        """Accept same-origin browser POSTs and non-browser clients without Origin.

        Browsers attach Origin to fetch/form mutations.  A missing Origin remains
        supported for CLI automation and existing API tests; session cookies are
        SameSite and an explicit foreign Origin is always rejected.
        """
        origin = self.headers.get("Origin", "").strip()
        if not origin or origin.lower() == "null":
            return not origin
        try:
            parsed = urlparse(origin)
        except ValueError:
            return False
        host = self.headers.get("Host", "").strip().lower()
        return parsed.scheme in {"http", "https"} and bool(host) and parsed.netloc.lower() == host

    def _handle_workflow_handling(self) -> None:
        try:
            payload = self._read_json_body()
            user = self._current_user()
            if not _web_user_permissions(user)["submit_handling"]:
                raise PermissionError("Your role cannot submit handling results.")
            finding_id = _text(payload.get("finding_id")).strip()
            scope = workflow_store().finding_scope(finding_id)
            if not scope:
                self._send_json({"ok": False, "error": "Finding was not found."}, status=HTTPStatus.NOT_FOUND)
                return
            _require_issue_access(user, str(scope.get("jira_key") or ""))
            result = workflow_store().record_handling(
                finding_id=finding_id,
                disposition=_text(payload.get("disposition")),
                note=_text(payload.get("note")),
                actor=user,
                actor_role=_web_user_role(user),
                jira_summary=_text(payload.get("jira_summary")),
                jira_description_adf=payload.get("jira_description_adf"),
                idempotency_key=self.headers.get("Idempotency-Key", "").strip(),
            )
            snapshot = _snapshot_completed_handling(
                str(scope.get("jira_key") or ""),
                user,
                request_key=self.headers.get("Idempotency-Key", "").strip(),
            )
            if snapshot:
                result["review_snapshot"] = snapshot
            self._send_json({"ok": True, **result})
        except PermissionError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.FORBIDDEN)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _handle_workflow_approval(self) -> None:
        try:
            payload = self._read_json_body()
            user = self._current_user()
            handling_id = _text(payload.get("handling_id")).strip()
            scope = workflow_store().handling_scope(handling_id)
            if not scope:
                self._send_json({"ok": False, "error": "Handling result was not found."}, status=HTTPStatus.NOT_FOUND)
                return
            _require_issue_access(user, str(scope.get("jira_key") or ""))
            workflow_store().approve_handling(
                handling_id, user, _web_user_role(user),
                approved=bool(payload.get("approved", True)), reason=_text(payload.get("reason")),
            )
            snapshot = _snapshot_completed_handling(
                str(scope.get("jira_key") or ""), user, request_key=f"approval:{handling_id}"
            )
            self._send_json({"ok": True, "review_snapshot": snapshot})
        except PermissionError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.FORBIDDEN)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _handle_workflow_manager_override(self) -> None:
        try:
            payload = self._read_json_body()
            user = self._current_user()
            if _web_user_role(user) != "manager":
                raise PermissionError("Only Manager can record a blocking exception.")
            if not bool(app_config_get("review_workflow.manager_override_enabled", True)):
                raise PermissionError("Manager exception is disabled by workflow policy.")
            handling_id = _text(payload.get("handling_id")).strip()
            scope = workflow_store().handling_scope(handling_id)
            if not scope:
                self._send_json({"ok": False, "error": "Handling result was not found."}, status=HTTPStatus.NOT_FOUND)
                return
            workflow_store().manager_override(handling_id, user, _text(payload.get("reason")))
            snapshot = _snapshot_completed_handling(
                str(scope.get("jira_key") or ""), user, request_key=f"manager-override:{handling_id}"
            )
            self._send_json({"ok": True, "review_snapshot": snapshot})
        except PermissionError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.FORBIDDEN)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _handle_workflow_pass(self) -> None:
        try:
            payload = self._read_json_body()
            user = self._current_user()
            jira_key = _text(payload.get("jira_key")).strip().upper()
            _require_issue_access(user, jira_key)
            result = workflow_store().manual_pass(
                jira_key, user, _web_user_role(user), _text(payload.get("note")),
                idempotency_key=self.headers.get("Idempotency-Key", "").strip(),
            )
            cycle_id = _current_cycle_id(jira_key)
            if cycle_id:
                result["review_snapshot"] = workflow_store().create_review_snapshot(
                    cycle_id=cycle_id,
                    reason="manual-pass",
                    actor=user,
                    idempotency_key=f"manual-pass:{result.get('pass_id') or self.headers.get('Idempotency-Key', '').strip()}",
                )
            self._send_json({"ok": True, **result})
        except PermissionError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.FORBIDDEN)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)

    def _handle_workflow_discussion(self) -> None:
        try:
            payload = self._read_json_body()
            user = self._current_user()
            jira_key = _text(payload.get("jira_key")).strip().upper()
            _require_issue_access(user, jira_key)
            discussion_id = workflow_store().add_discussion(
                jira_key, user, _text(payload.get("message")),
                run_id=_text(payload.get("run_id")), finding_id=_text(payload.get("finding_id")), kind=_text(payload.get("kind")) or "comment",
                idempotency_key=self.headers.get("Idempotency-Key", "").strip(),
            )
            self._send_json({"ok": True, "discussion_id": discussion_id})
        except PermissionError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.FORBIDDEN)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _handle_jira_draft_update(self) -> None:
        try:
            payload = self._read_json_body()
            user = self._current_user()
            draft_id = _text(payload.get("draft_id")).strip()
            scope = workflow_store().draft_scope(draft_id)
            if not scope:
                self._send_json({"ok": False, "error": "Jira draft was not found."}, status=HTTPStatus.NOT_FOUND)
                return
            _require_issue_access(user, str(scope.get("jira_key") or ""))
            result = workflow_store().update_draft(
                draft_id, _text(payload.get("summary")), payload.get("description_adf"), user, int(payload.get("version") or 0),
            )
            self._send_json({"ok": True, "draft": result})
        except PermissionError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.FORBIDDEN)
        except RuntimeError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _handle_jira_draft_attachment(self) -> None:
        try:
            payload = self._read_json_body()
            user = self._current_user()
            draft_id = _text(payload.get("draft_id")).strip()
            scope = workflow_store().draft_scope(draft_id)
            if not scope:
                self._send_json({"ok": False, "error": "Jira draft was not found."}, status=HTTPStatus.NOT_FOUND)
                return
            _require_issue_access(user, str(scope.get("jira_key") or ""))
            result = workflow_store().save_draft_attachment(
                draft_id,
                file_name=_text(payload.get("file_name")),
                media_type=_text(payload.get("media_type")),
                content_base64=_text(payload.get("content_base64")),
                actor=user,
            )
            self._send_json({"ok": True, "attachment": result})
        except PermissionError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.FORBIDDEN)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _handle_adf_render(self) -> None:
        try:
            payload = self._read_json_body()
            document = validate_adf(payload.get("document"))
            media_urls = payload.get("media_urls") if isinstance(payload.get("media_urls"), dict) else {}
            self._send_json({"ok": True, "html": render_adf_html(document, media_urls)})
        except ADFValidationError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _handle_change_password(self) -> None:
        try:
            payload = self._read_json_body()
            user = self._current_user()
            current = _text(payload.get("current_password"))
            new_password = _text(payload.get("new_password"))
            if len(new_password) < 14 or len(new_password) > 128 or not _strong_password(new_password):
                raise ValueError("New password must contain 14–128 characters with upper/lower case, number, and symbol.")
            with WEB_USERS_LOCK:
                with _exclusive_web_users_file_lock():
                    users = _load_web_users()
                    canonical, record = _find_web_user(users, user)
                    if not record or record.get("active") is False:
                        raise ValueError("User was not found.")
                    encoded = str(record.get("password_hash") or "")
                    legacy = str(record.get("password") or "")
                    if not (verify_web_password(current, encoded) if encoded else hmac.compare_digest(current, legacy)):
                        raise ValueError("Current password is incorrect.")
                    original_users = json.loads(json.dumps(users))
                    record["password_hash"] = hash_web_password(new_password)
                    record.pop("password", None)
                    record["must_change_password"] = False
                    record["password_changed_at"] = datetime.now().isoformat(timespec="seconds")
                    record["updated_at"] = record["password_changed_at"]
                    record["revision"] = int(record.get("revision") or 1) + 1
                    _write_web_users(users)
                    try:
                        _append_web_user_audit(canonical, "change-password", canonical, {"sessions_revoked": True})
                    except Exception:
                        _write_web_users(original_users)
                        raise
                    _revoke_web_user_sessions(canonical)
            self._send_json({"ok": True, "username": canonical})
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _admin_user_idempotent_response(self, operation: str, payload: dict[str, object]) -> dict[str, object] | None:
        key = self.headers.get("Idempotency-Key", "").strip()
        if not key:
            return None
        actor = self._current_user()
        cache_key = f"{actor}:{operation}:{key}"
        entry = WEB_USER_IDEMPOTENCY.get(cache_key)
        if not entry:
            return None
        if int(entry.get("expires_at") or 0) < int(time.time()):
            WEB_USER_IDEMPOTENCY.pop(cache_key, None)
            return None
        fingerprint = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if not hmac.compare_digest(str(entry.get("fingerprint") or ""), fingerprint):
            raise ValueError("Idempotency-Key was already used with a different request.")
        response = entry.get("response")
        return dict(response) if isinstance(response, dict) else None

    def _remember_admin_user_response(
        self, operation: str, payload: dict[str, object], response: dict[str, object]
    ) -> None:
        key = self.headers.get("Idempotency-Key", "").strip()
        if not key:
            return
        actor = self._current_user()
        cache_key = f"{actor}:{operation}:{key}"
        fingerprint = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        replay_response = dict(response)
        if replay_response.get("temporary_password"):
            replay_response["temporary_password"] = ""
            replay_response["credential_already_delivered"] = True
        WEB_USER_IDEMPOTENCY[cache_key] = {
            "expires_at": int(time.time()) + WEB_USER_IDEMPOTENCY_TTL_SECONDS,
            "fingerprint": fingerprint,
            "response": replay_response,
        }
        while len(WEB_USER_IDEMPOTENCY) > 200:
            WEB_USER_IDEMPOTENCY.pop(next(iter(WEB_USER_IDEMPOTENCY)))

    def _handle_admin_user_save(self) -> None:
        try:
            payload = self._read_json_body()
            with WEB_USER_IDEMPOTENCY_LOCK:
                cached = self._admin_user_idempotent_response("save", payload)
                if cached is not None:
                    response = cached
                else:
                    result = save_managed_web_user(self._current_user(), payload)
                    response = {"ok": True, **result}
                    self._remember_admin_user_response("save", payload, response)
            self._send_json(response)
        except PermissionError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.FORBIDDEN)
        except Exception as exc:
            status = HTTPStatus.CONFLICT if "updated by another session" in str(exc) else HTTPStatus.BAD_REQUEST
            self._send_json({"ok": False, "error": str(exc)}, status=status)

    def _handle_admin_user_password_reset(self) -> None:
        try:
            payload = self._read_json_body()
            with WEB_USER_IDEMPOTENCY_LOCK:
                cached = self._admin_user_idempotent_response("reset-password", payload)
                if cached is not None:
                    response = cached
                else:
                    result = reset_managed_web_user_password(self._current_user(), _text(payload.get("username")))
                    response = {"ok": True, **result}
                    self._remember_admin_user_response("reset-password", payload, response)
            self._send_json(response)
        except PermissionError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.FORBIDDEN)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")

    def _client_ip(self) -> str:
        if os.getenv("WEB_TRUST_PROXY", "0").strip().lower() in {"1", "true", "yes", "on"}:
            forwarded = self.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",", 1)[0].strip()
            real_ip = self.headers.get("X-Real-IP", "")
            if real_ip:
                return real_ip.strip()
        return str(self.client_address[0] or "")

    def _client_ip_allowed(self) -> bool:
        return is_client_ip_allowed(self._client_ip())

    def _send_ip_forbidden(self, path: str) -> None:
        client_ip = self._client_ip()
        if path.startswith("/api/") or path.startswith("/reports/") or path.startswith("/download/"):
            self._send_json({"ok": False, "error": f"Client IP is not whitelisted: {client_ip}"}, status=HTTPStatus.FORBIDDEN)
            return
        content = render_ip_forbidden(client_ip)
        data = content.encode("utf-8")
        self.send_response(HTTPStatus.FORBIDDEN)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_review(self) -> None:
        try:
            payload = self._read_json_body()
            user = self._current_user()
            permissions = _web_user_permissions(user)
            if not permissions["run_issue_review"]:
                self._send_json({"ok": False, "error": "Your role cannot start code review jobs."}, status=HTTPStatus.FORBIDDEN)
                return
            mode = _text(payload.get("mode")).strip().lower() or "jira"
            if mode == "release-gate":
                if not permissions["run_release_gate"]:
                    self._send_json({"ok": False, "error": "Release Gate is only available to Manager users."}, status=HTTPStatus.FORBIDDEN)
                    return
                mr_url = _normalize_merge_request_url(payload.get("mr_url"))
                if not _is_valid_merge_request_url(mr_url):
                    self._send_json(
                        {"ok": False, "error": "Enter a valid GIT_VERSION merge request URL."},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return
                payload["mr_url"] = mr_url
            payload["web_report_owner"] = user
            if _text(payload.get("sprint")).strip() and not permissions["run_sprint_review"]:
                self._send_json({"ok": False, "error": "Sprint review is only available to Manager users."}, status=HTTPStatus.FORBIDDEN)
                return
            if _text(payload.get("jira_filter")).strip() and not permissions["run_sprint_review"]:
                self._send_json({"ok": False, "error": "Jira filter review is only available to Manager users."}, status=HTTPStatus.FORBIDDEN)
                return
            jira_key = _text(payload.get("jira_key")).strip().upper()
            is_single_jira_review = (
                len(_jira_keys_from_text(jira_key)) == 1
                and not _text(payload.get("sprint")).strip()
                and not _text(payload.get("jira_filter")).strip()
            )
            if is_single_jira_review and not bool(payload.get("rerun_confirmed")):
                reuse_check = report_reuse_check(
                    jira_key=jira_key,
                    output_dir=_text(payload.get("output_dir")),
                    user=user,
                    state=_text(payload.get("state")),
                )
                if reuse_check.get("reports"):
                    self._send_json(
                        {
                            **reuse_check,
                            "ok": False,
                            "error": "Existing Code Review Report confirmation is required.",
                            "confirmation_required": True,
                        },
                        status=HTTPStatus.CONFLICT,
                    )
                    return
            job = create_review_job(payload, user)
            self._send_json({"ok": True, "job_id": job["id"], "status": job["status"]}, status=HTTPStatus.ACCEPTED)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _handle_coverage_job(self) -> None:
        try:
            payload = self._read_json_body()
            user = self._current_user()
            job, reused = create_coverage_job(payload, user)
            self._send_json(
                {
                    "ok": True,
                    "job": coverage_job_snapshot(str(job["id"]), user),
                    "reused": reused,
                },
                status=HTTPStatus.OK if reused else HTTPStatus.ACCEPTED,
            )
        except PermissionError as exc:
            self._send_json({"ok": False, "error": str(exc) or "Forbidden"}, status=HTTPStatus.FORBIDDEN)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _send_review_job(self, job_id: str, user: str = "") -> None:
        job = review_job_snapshot(job_id, user)
        if not job:
            self._send_json({"ok": False, "error": "Review job not found"}, status=HTTPStatus.NOT_FOUND)
            return
        self._send_json({"ok": True, "job": job})

    def _handle_review_stop(self, job_id: str, user: str = "") -> None:
        result = stop_review_job(job_id, user)
        status = HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST
        if result.get("error") == "Review job not found":
            status = HTTPStatus.NOT_FOUND
        if result.get("error") == "Forbidden":
            status = HTTPStatus.FORBIDDEN
        self._send_json(result, status=status)

    def _handle_review_pause(self, job_id: str, user: str = "") -> None:
        result = pause_review_job(job_id, user)
        status = HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST
        if result.get("error") == "Review job not found":
            status = HTTPStatus.NOT_FOUND
        if result.get("error") == "Forbidden":
            status = HTTPStatus.FORBIDDEN
        self._send_json(result, status=status)

    def _handle_review_resume(self, job_id: str, user: str = "") -> None:
        result = resume_review_job(job_id, user)
        status = HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST
        if result.get("error") == "Review job not found":
            status = HTTPStatus.NOT_FOUND
        if result.get("error") == "Forbidden":
            status = HTTPStatus.FORBIDDEN
        self._send_json(result, status=status)

    def _handle_login(self) -> None:
        try:
            payload = self._read_json_body()
            username = _text(payload.get("username")).strip()
            password = _text(payload.get("password"))
            challenge_id = _text(payload.get("challenge_id"))
            robot_answer = _text(payload.get("robot_answer")).strip()
            ok, error, canonical_username = authenticate_web_user(username, password, challenge_id, robot_answer)
            if not ok:
                self._send_json({"ok": False, "error": error, "challenge": new_robot_challenge()}, status=HTTPStatus.UNAUTHORIZED)
                return
            token = _create_session(canonical_username)
            data = json.dumps({"ok": True, "username": canonical_username}, ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Set-Cookie", f"{WEB_SESSION_COOKIE}={token}; HttpOnly; SameSite=Lax; Path=/")
            self._send_security_headers(sensitive=True)
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _current_user(self) -> str:
        token = _cookie_value(self.headers.get("Cookie", ""), WEB_SESSION_COOKIE)
        with WEB_SESSIONS_LOCK:
            session = WEB_SESSIONS.get(token)
            if not session:
                return ""
            if int(time.time()) > int(session.get("expires_at", 0)):
                WEB_SESSIONS.pop(token, None)
                return ""
            username = str(session.get("username") or "")
        try:
            _, record = _find_web_user(_load_web_users(), username)
        except RuntimeError:
            return ""
        if not record or record.get("active") is False:
            with WEB_SESSIONS_LOCK:
                WEB_SESSIONS.pop(token, None)
            return ""
        return username

    def _logout(self) -> None:
        token = _cookie_value(self.headers.get("Cookie", ""), WEB_SESSION_COOKIE)
        if token:
            with WEB_SESSIONS_LOCK:
                WEB_SESSIONS.pop(token, None)
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", "/login")
        self.send_header("Set-Cookie", f"{WEB_SESSION_COOKIE}=; Max-Age=0; HttpOnly; SameSite=Lax; Path=/")
        self.end_headers()

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.end_headers()

    def _send_report(self, report_name: str, output_dir: str = "", attachment: bool = False, user: str = "") -> None:
        try:
            base, target = _resolve_report_for_user(report_name, output_dir, user)
        except PermissionError:
            self._send_json({"ok": False, "error": "Forbidden"}, status=HTTPStatus.FORBIDDEN)
            return
        except FileNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc) or "Report not found"}, status=HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(target.name)[0] or "text/plain; charset=utf-8"
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if attachment:
            self.send_header("Content-Disposition", f"attachment; filename=\"{_download_name(target.name)}\"")
        self.end_headers()
        self.wfile.write(data)

    def _send_handling_result(self, report_name: str, output_dir: str = "", user: str = "") -> None:
        try:
            _base, target = _resolve_report_for_user(report_name, output_dir, user)
        except PermissionError:
            self._send_json({"ok": False, "error": "Forbidden"}, status=HTTPStatus.FORBIDDEN)
            return
        except FileNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc) or "Report not found"}, status=HTTPStatus.NOT_FOUND)
            return
        markdown = target.read_text(encoding="utf-8", errors="ignore")
        data = render_handling_result_template_from_markdown(markdown, target.name).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f"attachment; filename=\"{handling_result_filename(target.name)}\"")
        self.end_headers()
        self.wfile.write(data)

    def _send_report_thread(self, report_name: str, output_dir: str = "", user: str = "") -> None:
        try:
            base, target = _resolve_report_for_user(report_name, output_dir, user)
        except PermissionError:
            self._send_json({"ok": False, "error": "Forbidden"}, status=HTTPStatus.FORBIDDEN)
            return
        except FileNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc) or "Report not found"}, status=HTTPStatus.NOT_FOUND)
            return
        self._send_json({"ok": True, **_report_thread_payload(base, target, user)})

    def _send_report_markdown(self, report_name: str, output_dir: str = "", user: str = "") -> None:
        try:
            base, target = _resolve_report_for_user(report_name, output_dir, user)
        except PermissionError:
            self._send_json({"ok": False, "error": "Forbidden"}, status=HTTPStatus.FORBIDDEN)
            return
        except FileNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc) or "Report not found"}, status=HTTPStatus.NOT_FOUND)
            return
        stat = target.stat()
        self._send_json(
            {
                "ok": True,
                "report": target.relative_to(base).as_posix(),
                "report_name": target.name,
                "markdown": target.read_text(encoding="utf-8", errors="ignore"),
                "modified": stat.st_mtime,
                "size": stat.st_size,
            }
        )

    def _send_report_compare(self, report_name: str, output_dir: str = "", user: str = "") -> None:
        try:
            base, target = _resolve_report_for_user(report_name, output_dir, user)
        except PermissionError:
            self._send_json({"ok": False, "error": "Forbidden"}, status=HTTPStatus.FORBIDDEN)
            return
        except FileNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc) or "Report not found"}, status=HTTPStatus.NOT_FOUND)
            return
        self._send_json({"ok": True, **compare_report_with_previous(base, target, user)})

    def _handle_thread_message(self) -> None:
        try:
            payload = self._read_json_body()
            user = self._current_user()
            base, target = _resolve_report_for_user(_text(payload.get("report")), _text(payload.get("output_dir")), user)
            message = _text(payload.get("message")).strip()
            kind = _text(payload.get("kind")).strip() or "comment"
            if not message:
                self._send_json({"ok": False, "error": "Message is required."}, status=HTTPStatus.BAD_REQUEST)
                return
            if kind != "comment":
                self._send_json(
                    {"ok": False, "error": "Report Preview is read-only evidence. Use Issue Review for handling and Pass decisions."},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            _append_report_thread_message(base, target, user, kind, message)
            self._send_json({"ok": True, **_report_thread_payload(base, target, user)})
        except PermissionError:
            self._send_json({"ok": False, "error": "Forbidden"}, status=HTTPStatus.FORBIDDEN)
        except FileNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc) or "Report not found"}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _handle_thread_followups(self) -> None:
        try:
            payload = self._read_json_body()
            user = self._current_user()
            if not _web_user_permissions(user)["ai_chat"]:
                self._send_json({"ok": False, "error": "Only Auditor or Manager users can generate follow-up drafts."}, status=HTTPStatus.FORBIDDEN)
                return
            base, target = _resolve_report_for_user(_text(payload.get("report")), _text(payload.get("output_dir")), user)
            instruction = _text(payload.get("instruction")).strip()
            _generate_report_followups(base, target, user, instruction)
            self._send_json({"ok": True, **_report_thread_payload(base, target, user)})
        except PermissionError:
            self._send_json({"ok": False, "error": "Forbidden"}, status=HTTPStatus.FORBIDDEN)
        except FileNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc) or "Report not found"}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _handle_thread_ai_chat(self) -> None:
        try:
            payload = self._read_json_body()
            user = self._current_user()
            if not _web_user_permissions(user)["ai_chat"]:
                self._send_json({"ok": False, "error": "AI Chat is available to Auditor and Manager users."}, status=HTTPStatus.FORBIDDEN)
                return
            base, target = _resolve_report_for_user(_text(payload.get("report")), _text(payload.get("output_dir")), user)
            prompt = _text(payload.get("prompt")).strip()
            if not prompt:
                self._send_json({"ok": False, "error": "Message is required."}, status=HTTPStatus.BAD_REQUEST)
                return
            thread = _load_report_thread(base, target)
            report_text = target.read_text(encoding="utf-8", errors="ignore")
            reply = _build_report_chat_reply(target.name, report_text, thread, prompt)
            self._send_json({"ok": True, "reply": reply})
        except PermissionError:
            self._send_json({"ok": False, "error": "Forbidden"}, status=HTTPStatus.FORBIDDEN)
        except FileNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc) or "Report not found"}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _send_responsible_zip(self, responsible: str, output_dir: str = "", user: str = "") -> None:
        base = _report_dir(output_dir)
        folder_name = unquote(responsible).strip("/")
        if not _can_access_responsible(folder_name, user):
            self._send_json({"ok": False, "error": "Forbidden"}, status=HTTPStatus.FORBIDDEN)
            return
        try:
            target_dir = base if folder_name == "__root__" else _safe_child_path(base, folder_name)
        except FileNotFoundError:
            self._send_json({"ok": False, "error": "Invalid responsible folder"}, status=HTTPStatus.NOT_FOUND)
            return
        if not target_dir.exists() or not target_dir.is_dir():
            self._send_json({"ok": False, "error": "Responsible folder not found"}, status=HTTPStatus.NOT_FOUND)
            return
        iterator = target_dir.glob("*.md") if folder_name == "__root__" else target_dir.rglob("*.md")
        markdown_files = sorted(iterator, key=lambda item: item.name.lower())
        if not markdown_files:
            self._send_json({"ok": False, "error": "No markdown reports in responsible folder"}, status=HTTPStatus.NOT_FOUND)
            return
        data = _zip_reports(target_dir, markdown_files)
        zip_name = "root-reports.zip" if folder_name == "__root__" else f"{_download_name(Path(folder_name).name)}.zip"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f"attachment; filename=\"{zip_name}\"")
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, content: str) -> None:
        data = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._send_security_headers(sensitive=False)
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._send_security_headers(sensitive=True)
        self.end_headers()
        self.wfile.write(data)

    def _send_security_headers(self, sensitive: bool = True) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Cache-Control", "no-store" if sensitive else "private, no-cache")


def _coverage_scope_from_payload(payload: dict[str, object]) -> dict[str, str]:
    scope = {
        "jira": _text(payload.get("jira")).strip(),
        "sprint": _text(payload.get("sprint")).strip(),
        "jira_filter": _text(payload.get("jira_filter")).strip(),
    }
    if not any(scope.values()):
        raise ValueError("Enter Jira issues, a Sprint, or a Jira Filter ID.")
    return scope


def _coverage_scope_key(user: str, scope: dict[str, str]) -> str:
    normalized = {
        "user": user.strip().lower(),
        "jira": ",".join(_jira_keys_from_text(scope.get("jira", ""))),
        "sprint": scope.get("sprint", "").strip().lower(),
        "jira_filter": scope.get("jira_filter", "").strip().lower(),
    }
    return hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def create_coverage_job(payload: dict[str, object], user: str) -> tuple[dict[str, object], bool]:
    _purge_coverage_jobs()
    scope = _coverage_scope_from_payload(payload)
    permissions = _web_user_permissions(user)
    if (scope["sprint"] or scope["jira_filter"]) and not permissions["scan_coverage"]:
        raise PermissionError("Your role cannot scan Sprint or Jira Filter coverage.")
    scope_key = _coverage_scope_key(user, scope)
    now = time.time()
    with WEB_COVERAGE_JOBS_LOCK:
        candidates = sorted(
            (
                job
                for job in WEB_COVERAGE_JOBS.values()
                if str(job.get("user") or "").lower() == user.lower()
                and str(job.get("scope_key") or "") == scope_key
            ),
            key=lambda item: float(item.get("created_at") or 0),
            reverse=True,
        )
        for existing in candidates:
            status = str(existing.get("status") or "")
            if status in {"queued", "running"}:
                return existing, True
            if (
                status == "done"
                and now - float(existing.get("finished_at") or existing.get("updated_at") or 0)
                <= COVERAGE_RESULT_CACHE_SECONDS
            ):
                return existing, True

        job_id = uuid.uuid4().hex
        job: dict[str, object] = {
            "id": job_id,
            "user": user,
            "scope_key": scope_key,
            "scope": scope,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "started_at": 0,
            "finished_at": 0,
            "progress": {
                "event": "queued",
                "message": "Coverage scan queued.",
                "current": 0,
                "total": 0,
                "percent": 0,
            },
            "events": [],
            "result": None,
            "error": "",
        }
        WEB_COVERAGE_JOBS[job_id] = job
    _append_coverage_event(job_id, "queued", "Coverage scan queued.", {"percent": 0})
    threading.Thread(target=_run_coverage_job, args=(job_id, scope, user), daemon=True).start()
    return job, False


def coverage_job_snapshot(job_id: str, user: str) -> dict[str, object] | None:
    _purge_coverage_jobs()
    with WEB_COVERAGE_JOBS_LOCK:
        job = WEB_COVERAGE_JOBS.get(job_id)
        if not job or str(job.get("user") or "").lower() != user.lower():
            return None
        return {
            "id": job["id"],
            "status": job["status"],
            "scope": dict(job.get("scope") or {}),
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "progress": dict(job.get("progress") or {}),
            "events": list(job.get("events") or []),
            "result": job.get("result"),
            "error": job.get("error") or "",
        }


def latest_coverage_job_snapshot(user: str) -> dict[str, object] | None:
    _purge_coverage_jobs()
    now = time.time()
    with WEB_COVERAGE_JOBS_LOCK:
        candidates = [
            job
            for job in WEB_COVERAGE_JOBS.values()
            if str(job.get("user") or "").lower() == user.lower()
            and (
                str(job.get("status") or "") in {"queued", "running"}
                or (
                    str(job.get("status") or "") == "done"
                    and now - float(job.get("finished_at") or job.get("updated_at") or 0)
                    <= COVERAGE_RESULT_CACHE_SECONDS
                )
            )
        ]
        candidates.sort(
            key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0),
            reverse=True,
        )
        job_id = str(candidates[0].get("id") or "") if candidates else ""
    return coverage_job_snapshot(job_id, user) if job_id else None


def _run_coverage_job(job_id: str, scope: dict[str, str], user: str) -> None:
    try:
        with WEB_COVERAGE_EXECUTION_LOCK:
            _update_coverage_job(job_id, status="running", started_at=time.time())
            _append_coverage_event(
                job_id,
                "started",
                "Loading the selected Sprint, Filter, or Jira issue scope.",
                {"percent": 3},
            )
            result = build_review_coverage(
                user=user,
                jira_keys=scope["jira"],
                sprint=scope["sprint"],
                jira_filter=scope["jira_filter"],
                progress=lambda event: _coverage_job_progress(job_id, event),
            )
            _update_coverage_job(
                job_id,
                status="done",
                result=result,
                finished_at=time.time(),
                progress={
                    "event": "completed",
                    "message": f"Coverage scan completed for {len(result.get('issues') or [])} issue(s).",
                    "current": len(result.get("issues") or []),
                    "total": len(result.get("issues") or []),
                    "percent": 100,
                },
            )
            _append_coverage_event(job_id, "completed", "Coverage scan completed.", {"percent": 100})
    except Exception as exc:
        _update_coverage_job(
            job_id,
            status="failed",
            error=str(exc),
            finished_at=time.time(),
            progress={
                "event": "failed",
                "message": str(exc) or "Coverage scan failed.",
                "current": 0,
                "total": 0,
                "percent": 100,
            },
        )
        _append_coverage_event(job_id, "failed", str(exc) or "Coverage scan failed.", {"percent": 100})


def _coverage_job_progress(job_id: str, event: dict[str, object]) -> None:
    event_name = str(event.get("event") or "progress")
    current = int(event.get("index") or event.get("current") or 0)
    total = int(event.get("total") or 0)
    if event_name == "start":
        percent = 5
    elif event_name == "jira":
        percent = 12
    elif event_name == "discovery-issue":
        percent = 12 + round((max(0, current - 1) / max(1, total)) * 66)
    elif event_name == "discover":
        percent = 80
    elif event_name == "coverage-reports":
        percent = 88
    elif event_name == "coverage-workflow":
        percent = 94
    elif event_name == "coverage-finalizing":
        percent = 97
    else:
        percent = int(event.get("percent") or 15)
    progress = {
        "event": event_name,
        "message": str(event.get("message") or "Coverage scan is running."),
        "current": current,
        "total": total,
        "percent": max(0, min(percent, 99)),
        "jira_key": str(event.get("jira_key") or ""),
    }
    _update_coverage_job(job_id, progress=progress)
    _append_coverage_event(job_id, event_name, progress["message"], progress)


def _append_coverage_event(job_id: str, event: str, message: str, data: dict[str, object]) -> None:
    with WEB_COVERAGE_JOBS_LOCK:
        job = WEB_COVERAGE_JOBS.get(job_id)
        if not job:
            return
        events = job.setdefault("events", [])
        assert isinstance(events, list)
        events.append(
            {
                "id": len(events) + 1,
                "time": time.time(),
                "event": event,
                "message": message,
                "data": data,
            }
        )
        if len(events) > 100:
            del events[:-100]
        job["updated_at"] = time.time()


def _update_coverage_job(job_id: str, **updates: object) -> None:
    with WEB_COVERAGE_JOBS_LOCK:
        job = WEB_COVERAGE_JOBS.get(job_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = time.time()


def _purge_coverage_jobs() -> None:
    cutoff = time.time() - COVERAGE_JOB_TTL_SECONDS
    with WEB_COVERAGE_JOBS_LOCK:
        for job_id, job in list(WEB_COVERAGE_JOBS.items()):
            if (
                float(job.get("updated_at") or 0) < cutoff
                and str(job.get("status") or "") in {"done", "failed"}
            ):
                WEB_COVERAGE_JOBS.pop(job_id, None)


def create_review_job(payload: dict[str, object], user: str) -> dict[str, object]:
    _purge_review_jobs()
    job_id = uuid.uuid4().hex
    now = time.time()
    job: dict[str, object] = {
        "id": job_id,
        "user": user,
        "status": "queued",
        "created_at": now,
        "updated_at": now,
        "started_at": 0,
        "finished_at": 0,
        "payload": _safe_job_payload(payload),
        "events": [],
        "result": None,
        "error": "",
        "cancel_requested": False,
        "cancel_requested_at": 0,
        "pause_requested": False,
        "pause_requested_at": 0,
    }
    with WEB_REVIEW_JOBS_LOCK:
        WEB_REVIEW_JOBS[job_id] = job
    _append_job_event(job_id, "queued", "Review job queued.")
    thread = threading.Thread(target=_run_review_job, args=(job_id, dict(payload)), daemon=True)
    thread.start()
    return job


def review_job_snapshot(job_id: str, user: str) -> dict[str, object] | None:
    _purge_review_jobs()
    with WEB_REVIEW_JOBS_LOCK:
        job = WEB_REVIEW_JOBS.get(job_id)
        if not job:
            return None
        if not _is_admin_user(user) and str(job.get("user") or "") != user:
            return None
        return {
            "id": job["id"],
            "status": job["status"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "payload": job["payload"],
            "events": list(job.get("events") or []),
            "result": job.get("result"),
            "error": job.get("error") or "",
            "cancel_requested": bool(job.get("cancel_requested")),
            "cancel_requested_at": job.get("cancel_requested_at") or 0,
            "pause_requested": bool(job.get("pause_requested")),
            "pause_requested_at": job.get("pause_requested_at") or 0,
        }


def list_review_job_snapshots(user: str, limit: int = 50) -> list[dict[str, object]]:
    _purge_review_jobs()
    safe_limit = max(1, min(int(limit or 50), 200))
    with WEB_REVIEW_JOBS_LOCK:
        jobs = [
            job
            for job in WEB_REVIEW_JOBS.values()
            if _is_admin_user(user) or str(job.get("user") or "") == user
        ]
        jobs.sort(key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0), reverse=True)
        selected = jobs[:safe_limit]
        return [
            {
                "id": job["id"],
                "status": job["status"],
                "created_at": job["created_at"],
                "updated_at": job["updated_at"],
                "started_at": job["started_at"],
                "finished_at": job["finished_at"],
                "payload": job["payload"],
                "events": list(job.get("events") or []),
                "result": job.get("result"),
                "error": job.get("error") or "",
                "cancel_requested": bool(job.get("cancel_requested")),
                "cancel_requested_at": job.get("cancel_requested_at") or 0,
                "pause_requested": bool(job.get("pause_requested")),
                "pause_requested_at": job.get("pause_requested_at") or 0,
            }
            for job in selected
        ]


def _run_review_job(job_id: str, payload: dict[str, object]) -> None:
    acquired = False
    try:
        # A queued job stays cancellable while waiting for the single execution
        # slot.  Timed acquire avoids an uninterruptible wait during shutdown.
        while not acquired:
            if _job_cancel_requested(job_id):
                raise ReviewCancelled("Review job stopped while queued.")
            if _job_pause_requested(job_id):
                _set_job_status_if_current(job_id, "paused", {"queued", "pausing", "paused"})
                time.sleep(0.2)
                continue
            _set_job_status_if_current(job_id, "queued", {"paused", "pausing"})
            acquired = WEB_REVIEW_EXECUTION_LOCK.acquire(timeout=0.2)
        if _job_cancel_requested(job_id):
            raise ReviewCancelled("Review job stopped while queued.")
        _update_job(job_id, status="running", started_at=time.time())
        _append_job_event(job_id, "started", "Review job started.")
        result = run_review_from_payload(payload, progress=lambda event: _job_progress(job_id, event))
        if _job_cancel_requested(job_id):
            raise ReviewCancelled("Review job stopped by user.")
        _wait_while_job_paused(job_id)
        if _job_cancel_requested(job_id):
            raise ReviewCancelled("Review job stopped by user.")
        _update_job(job_id, status="done", result=result, finished_at=time.time())
        _append_job_event(job_id, "completed", str(result.get("conclusion") or "Review completed."), {"result": _compact_job_result(result)})
        try:
            _sync_workflow_history()
        except Exception as exc:
            # The report remains valid even if workflow indexing is temporarily
            # unavailable; surface the recoverable condition in the Job log and
            # let the next History request retry the idempotent sync.
            _append_job_event(job_id, "workflow-sync-warning", f"Workflow history sync will retry: {exc}")
    except ReviewCancelled as exc:
        _update_job(job_id, status="canceled", error=str(exc), finished_at=time.time())
        _append_job_event(job_id, "canceled", str(exc) or "Review job stopped by user.")
    except Exception as exc:
        _update_job(job_id, status="failed", error=str(exc), finished_at=time.time())
        _append_job_event(job_id, "failed", str(exc), {"traceback": traceback.format_exc(limit=8)})
    finally:
        if acquired:
            WEB_REVIEW_EXECUTION_LOCK.release()


def stop_review_job(job_id: str, user: str) -> dict[str, object]:
    with WEB_REVIEW_JOBS_LOCK:
        job = WEB_REVIEW_JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "Review job not found"}
        if not _is_admin_user(user) and str(job.get("user") or "") != user:
            return {"ok": False, "error": "Forbidden"}
        status = str(job.get("status") or "")
        if status in {"done", "failed", "canceled"}:
            return {"ok": False, "error": f"Review job is already {status}."}
        job["cancel_requested"] = True
        job["cancel_requested_at"] = time.time()
        if status in {"queued", "running", "pausing", "paused"}:
            job["status"] = "stopping"
        job["updated_at"] = time.time()
    _append_job_event(job_id, "stop-requested", "Stop requested. The review will stop at the next safe checkpoint.")
    return {"ok": True, "status": "stopping", "job": review_job_snapshot(job_id, user)}


def pause_review_job(job_id: str, user: str) -> dict[str, object]:
    with WEB_REVIEW_JOBS_LOCK:
        job = WEB_REVIEW_JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "Review job not found"}
        if not _is_admin_user(user) and str(job.get("user") or "") != user:
            return {"ok": False, "error": "Forbidden"}
        status = str(job.get("status") or "")
        if status in {"done", "failed", "canceled"}:
            return {"ok": False, "error": f"Review job is already {status}."}
        if status in {"paused", "pausing"} or job.get("pause_requested"):
            already_status = status or "pausing"
            return {"ok": True, "status": already_status}
        if status == "stopping" or job.get("cancel_requested"):
            return {"ok": False, "error": "Review job is stopping."}
        job["pause_requested"] = True
        job["pause_requested_at"] = time.time()
        if status in {"queued", "running"}:
            job["status"] = "pausing"
        job["updated_at"] = time.time()
    _append_job_event(job_id, "pause-requested", "Pause requested. The review will pause at the next safe checkpoint.")
    return {"ok": True, "status": "pausing", "job": review_job_snapshot(job_id, user)}


def resume_review_job(job_id: str, user: str) -> dict[str, object]:
    with WEB_REVIEW_JOBS_LOCK:
        job = WEB_REVIEW_JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "Review job not found"}
        if not _is_admin_user(user) and str(job.get("user") or "") != user:
            return {"ok": False, "error": "Forbidden"}
        status = str(job.get("status") or "")
        if status in {"done", "failed", "canceled"}:
            return {"ok": False, "error": f"Review job is already {status}."}
        if status == "stopping" or job.get("cancel_requested"):
            return {"ok": False, "error": "Review job is stopping."}
        job["pause_requested"] = False
        if status in {"paused", "pausing"}:
            job["status"] = "running" if job.get("started_at") else "queued"
        job["updated_at"] = time.time()
    _append_job_event(job_id, "resume-requested", "Resume requested.")
    snapshot = review_job_snapshot(job_id, user)
    return {"ok": True, "status": str((snapshot or {}).get("status") or "queued"), "job": snapshot}


def _job_cancel_requested(job_id: str) -> bool:
    with WEB_REVIEW_JOBS_LOCK:
        job = WEB_REVIEW_JOBS.get(job_id)
        return bool(job and job.get("cancel_requested"))


def _job_pause_requested(job_id: str) -> bool:
    with WEB_REVIEW_JOBS_LOCK:
        job = WEB_REVIEW_JOBS.get(job_id)
        return bool(job and job.get("pause_requested"))


def _set_job_status_if_current(job_id: str, status: str, current_statuses: set[str]) -> None:
    with WEB_REVIEW_JOBS_LOCK:
        job = WEB_REVIEW_JOBS.get(job_id)
        if not job:
            return
        if str(job.get("status") or "") in current_statuses:
            job["status"] = status
            job["updated_at"] = time.time()


def _wait_while_job_paused(job_id: str) -> None:
    if not _job_pause_requested(job_id):
        return
    _set_job_status_if_current(job_id, "paused", {"queued", "running", "pausing", "paused"})
    _append_job_event(job_id, "paused", "Review job paused.")
    while _job_pause_requested(job_id):
        if _job_cancel_requested(job_id):
            raise ReviewCancelled("Review job stopped by user.")
        time.sleep(0.5)
    _set_job_status_if_current(job_id, "running", {"paused", "pausing"})
    _append_job_event(job_id, "resumed", "Review job resumed.")


def _job_progress(job_id: str, event: dict[str, object]) -> None:
    if _job_cancel_requested(job_id):
        raise ReviewCancelled("Review job stopped by user.")
    _wait_while_job_paused(job_id)
    _append_job_event(job_id, str(event.get("event") or "progress"), str(event.get("message") or ""), event)
    if _job_cancel_requested(job_id):
        raise ReviewCancelled("Review job stopped by user.")
    _wait_while_job_paused(job_id)


def _append_job_event(job_id: str, event: str, message: str, data: dict[str, object] | None = None) -> None:
    with WEB_REVIEW_JOBS_LOCK:
        job = WEB_REVIEW_JOBS.get(job_id)
        if not job:
            return
        events = job.setdefault("events", [])
        assert isinstance(events, list)
        item = {
            "id": len(events) + 1,
            "time": time.time(),
            "event": event,
            "message": message,
            "data": data or {},
        }
        events.append(item)
        if len(events) > 500:
            del events[:-500]
        job["updated_at"] = time.time()


def _update_job(job_id: str, **updates: object) -> None:
    with WEB_REVIEW_JOBS_LOCK:
        job = WEB_REVIEW_JOBS.get(job_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = time.time()


def _safe_job_payload(payload: dict[str, object]) -> dict[str, object]:
    allowed = {"mode", "mr_url", "jira_key", "jira_filter", "sprint", "output_dir", "report_language", "report_min_severity", "speed", "state", "limit", "rerun_confirmed", "retry_of", "web_report_owner", "review_mode", "batch_preview_confirmed"}
    return {key: payload.get(key) for key in sorted(allowed) if key in payload}


def _compact_job_result(result: dict[str, object]) -> dict[str, object]:
    return {
        "conclusion": result.get("conclusion"),
        "finding_count": result.get("finding_count"),
        "severity_counts": result.get("severity_counts"),
        "report": result.get("report"),
        "report_name": result.get("report_name"),
        "mode": result.get("mode"),
        "release_gate": result.get("release_gate"),
    }


def _purge_review_jobs() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    with WEB_REVIEW_JOBS_LOCK:
        for job_id, job in list(WEB_REVIEW_JOBS.items()):
            updated = float(job.get("updated_at") or 0)
            status = str(job.get("status") or "")
            if updated < cutoff and status in {"done", "failed", "canceled"}:
                WEB_REVIEW_JOBS.pop(job_id, None)


def is_client_ip_allowed(client_ip: str) -> bool:
    normalized_ip = _normalize_client_ip(client_ip)
    if not normalized_ip:
        return False
    ip_obj = _parse_ip_address(normalized_ip)
    if ip_obj and ip_obj.is_loopback and os.getenv("WEB_IP_WHITELIST_INCLUDE_LOOPBACK", "1").strip().lower() not in {"0", "false", "no", "off"}:
        return True
    entries = web_ip_whitelist_entries()
    if not entries:
        return False
    for entry in entries:
        if _ip_matches_entry(normalized_ip, ip_obj, entry):
            return True
    return False


def web_ip_whitelist_entries() -> list[str]:
    entries: list[str] = []
    raw = os.getenv("WEB_IP_WHITELIST", "").strip()
    if raw:
        entries.extend(_split_whitelist(raw))
    file_path = Path(os.getenv("WEB_IP_WHITELIST_FILE", str(WEB_IP_WHITELIST_FILE))).expanduser()
    if file_path.exists():
        try:
            entries.extend(_split_whitelist(file_path.read_text(encoding="utf-8")))
        except Exception:
            pass
    if os.getenv("WEB_IP_WHITELIST_INCLUDE_LOOPBACK", "1").strip().lower() not in {"0", "false", "no", "off"}:
        entries.extend(["127.0.0.1", "::1"])
    deduped: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        normalized = entry.strip()
        if not normalized or normalized.startswith("#"):
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
    return deduped


def _split_whitelist(raw: str) -> list[str]:
    values: list[str] = []
    for line in raw.replace(";", ",").splitlines():
        line = line.split("#", 1)[0]
        values.extend(item.strip() for item in line.split(",") if item.strip())
    return values


def _normalize_client_ip(value: str) -> str:
    text = (value or "").strip()
    if text.startswith("::ffff:"):
        return text.removeprefix("::ffff:")
    return text


def _parse_ip_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _ip_matches_entry(client_ip: str, ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address | None, entry: str) -> bool:
    normalized = _normalize_client_ip(entry)
    if normalized.lower() == "localhost":
        return client_ip in {"127.0.0.1", "::1"}
    if "*" in normalized:
        pattern = "^" + re.escape(normalized).replace("\\*", r"[0-9a-fA-F:.]+") + "$"
        return re.match(pattern, client_ip) is not None
    if "/" in normalized and ip_obj:
        try:
            return ip_obj in ipaddress.ip_network(normalized, strict=False)
        except ValueError:
            return False
    return client_ip.lower() == normalized.lower()


def list_reports(output_dir: str = "", user: str = "", search: str = "", days: int | None = None) -> list[dict[str, str | int]]:
    ensure_directories()
    reports = []
    cutoff = _report_history_cutoff(days)
    for directory in _report_history_directories(output_dir):
        query = f"?output_dir={quote(str(directory), safe='')}"
        for path in directory.rglob("*.md"):
            stat = path.stat()
            if cutoff and stat.st_mtime < cutoff:
                continue
            relative = path.relative_to(directory).as_posix()
            owner = path.relative_to(directory).parts[0] if len(path.relative_to(directory).parts) > 1 else ""
            metadata = _read_report_metadata(path)
            if not _can_access_report(directory, path, user, metadata=metadata):
                continue
            metadata_people = sorted(
                scope_people(metadata.get("responsible_scope") or metadata.get("responsible")),
                key=str.casefold,
            )
            responsible = "+".join(metadata_people) or owner
            if not _report_matches_search(relative, path.name, responsible, search):
                continue
            reports.append(
                {
                    "name": path.name,
                    "relative_path": relative,
                    "responsible": responsible,
                    "report_owner": owner,
                    "application": str(metadata.get("application") or ""),
                    "release_line": str(metadata.get("release_line") or ""),
                    "scope_label": review_scope_label(
                        str(metadata.get("application") or ""),
                        str(metadata.get("release_line") or ""),
                    ),
                    "output_dir": str(directory),
                    "output_dir_name": directory.name,
                    "size": stat.st_size,
                    "modified": int(stat.st_mtime),
                    "url": f"/reports/{quote(relative, safe='/')}{query}",
                    "download_url": f"/download/report/{quote(relative, safe='/')}{query}",
                    "handling_url": f"/download/handling/{quote(relative, safe='/')}{query}",
                    "thread_report": relative,
                    "thread_count": _report_thread_message_count(directory, path),
                }
            )
    return sorted(reports, key=lambda item: int(item.get("modified") or 0), reverse=True)


def _report_history_directories(output_dir: str = "") -> list[Path]:
    if output_dir:
        directory = _report_dir(output_dir)
        return [directory] if directory.exists() else []
    current = report_output_dir().expanduser().resolve()
    directories: list[Path] = []
    if current.exists():
        directories.append(current)
    parent = current.parent
    if parent.exists():
        directories.extend(path.resolve() for path in parent.glob("e-channel-sprint*") if path.is_dir())
    seen: set[Path] = set()
    result: list[Path] = []
    for directory in directories:
        if directory in seen:
            continue
        seen.add(directory)
        result.append(directory)
    return sorted(result, key=lambda item: item.name, reverse=True)


def _default_report_history_days() -> int:
    return app_config_int("report.history_days", "WEB_REPORT_HISTORY_DAYS", 14)


def _report_history_cutoff(days: int | None) -> float | None:
    if days is None:
        days = _default_report_history_days()
    if days <= 0:
        return None
    return time.time() - (days * 24 * 60 * 60)


def _report_matches_search(relative: str, name: str, responsible: str, search: str = "") -> bool:
    query = (search or "").strip().lower()
    if not query:
        return True
    haystack = " ".join([relative, name, responsible]).lower()
    tokens = [item for item in re.split(r"\s+", query) if item]
    return all(token in haystack for token in tokens)


REVIEW_APPLICATION_ORDER = ("WVAdmin", "iTrade Client", "Services Terminal", "DPS", "Unmapped")


def _coverage_issue_row(jira_key: str, summary: str = "", jira_status: str = "") -> dict[str, object]:
    return {
        "jira_key": jira_key,
        "summary": summary,
        "jira_status": jira_status,
        "responsibles": set(),
        "applications": set(),
        "review_scopes": set(),
        "project_paths": set(),
        "mr_count": 0,
    }


def _review_application_from_discovery(item: dict[str, object]) -> str:
    """Map an MR to its release application using configured GitLab metadata."""
    return _review_scope_from_discovery(item).application


def _review_scope_from_discovery(item: dict[str, object]) -> ReviewScope:
    normalized = dict(item)
    normalized.setdefault("project_path", item.get("gitlab_project") or item.get("project_path"))
    return review_scope_for_merge_request(normalized)


def _application_review_progress(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    scoped: dict[tuple[str, str], list[dict[str, object]]] = {}
    for row in rows:
        statuses = row.get("scope_statuses")
        if isinstance(statuses, list) and statuses:
            for item in statuses:
                if not isinstance(item, dict):
                    continue
                key = (
                    str(item.get("application") or "Unmapped"),
                    str(item.get("release_line") or ""),
                )
                scoped.setdefault(key, []).append({**row, **item})
            continue
        applications = row.get("applications")
        values = applications if isinstance(applications, list) else []
        normalized = list(dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip())) or ["Unmapped"]
        for application in normalized:
            scoped.setdefault((application, ""), []).append(row)

    result: list[dict[str, object]] = []
    status_names = ("missing", "running", "pending", "ready", "passed", "failed")
    for (application, release_line), application_rows in scoped.items():
        counts = {name: 0 for name in status_names}
        for row in application_rows:
            status = str(row.get("scope_status") or row.get("workflow_status") or "missing")
            counts[status if status in counts else "missing"] += 1
        total = len(application_rows)
        passed = counts["passed"]
        remaining = max(0, total - passed)
        report_count = sum(1 for row in application_rows if int(row.get("report_count") or 0) > 0)
        readiness_percent = round((passed / total) * 100) if total else 0
        report_coverage_percent = round((report_count / total) * 100) if total else 0
        result.append(
            {
                "application": application,
                "release_line": release_line,
                "scope_label": review_scope_label(application, release_line),
                "issue_count": total,
                "issues_with_reports": report_count,
                "issues_without_reports": total - report_count,
                "report_coverage_percent": report_coverage_percent,
                "readiness_percent": readiness_percent,
                "remaining": remaining,
                "gate_ready": application != "Unmapped" and total > 0 and remaining == 0,
                "counts": counts,
            }
        )
    order = {name: index for index, name in enumerate(REVIEW_APPLICATION_ORDER)}
    return sorted(
        result,
        key=lambda item: (
            order.get(str(item["application"]), len(order)),
            str(item.get("release_line") or ""),
            str(item["application"]),
        ),
    )


def _emit_coverage_progress(progress: object, event: str, message: str, **data: object) -> None:
    if not callable(progress):
        return
    try:
        progress({"event": event, "message": message, **data})
    except Exception:
        pass


def build_review_coverage(
    user: str,
    jira_keys: str = "",
    sprint: str = "",
    jira_filter: str = "",
    progress: object = None,
) -> dict[str, object]:
    permissions = _web_user_permissions(user)
    requested_keys = _jira_keys_from_text(jira_keys)
    if (sprint or jira_filter) and not permissions["scan_coverage"]:
        raise PermissionError("Your role cannot scan Sprint or Jira Filter coverage.")
    _emit_coverage_progress(progress, "start", "Loading the selected review scope.")

    discovery: dict[str, Any] = {}
    direct_issue_details: list[dict[str, str]] = []
    if jira_filter:
        discovery = review_jira_filter_merge_requests(
            filter_id=jira_filter,
            list_only=True,
            report_owner=user,
            progress=progress,
        )
    elif sprint:
        discovery = review_sprint_merge_requests(
            sprint=sprint,
            list_only=True,
            report_owner=user,
            progress=progress,
        )
    elif requested_keys:
        discovery = {
            "items": [],
            "issues_without_mrs": [],
            "discovery_errors": [],
        }
        for issue_index, key in enumerate(requested_keys, 1):
            _emit_coverage_progress(
                progress,
                "discovery-issue",
                f"Discovering merge requests for {key} ({issue_index}/{len(requested_keys)}).",
                index=issue_index,
                total=len(requested_keys),
                jira_key=key,
            )
            try:
                result = review_jira_issue_merge_requests(
                    key,
                    list_only=True,
                    report_owner=user,
                    progress=progress,
                )
                discovery["items"].extend(result.get("items") or [])
                discovery["issues_without_mrs"].extend(result.get("issues_without_mrs") or [])
                discovery["discovery_errors"].extend(result.get("errors") or [])
                direct_issue_details.append(
                    {
                        "jira_key": str(result.get("jira_key") or key).upper(),
                        "summary": str(result.get("jira_summary") or ""),
                        "jira_status": str(result.get("jira_status") or ""),
                    }
                )
            except Exception as exc:
                discovery["discovery_errors"].append({"jira_key": key, "error": str(exc)})

    issue_rows: dict[str, dict[str, object]] = {key: _coverage_issue_row(key) for key in requested_keys}
    for detail in direct_issue_details:
        key = detail["jira_key"]
        row = issue_rows.setdefault(key, _coverage_issue_row(key))
        row["summary"] = detail["summary"] or str(row.get("summary") or "")
        row["jira_status"] = detail["jira_status"] or str(row.get("jira_status") or "")
    for item in discovery.get("items") or []:
        if not isinstance(item, dict):
            continue
        responsible = str(item.get("responsible") or "")
        if _web_user_role(user) != "manager" and not _can_access_project_responsible(responsible, user):
            continue
        key = str(item.get("jira_key") or "").upper()
        if not key:
            continue
        row = issue_rows.setdefault(key, _coverage_issue_row(key))
        row["summary"] = str(item.get("jira_summary") or row.get("summary") or "")
        row["jira_status"] = str(item.get("jira_status") or row.get("jira_status") or "")
        if responsible:
            row["responsibles"].update(_split_people(responsible))
        review_scope = _review_scope_from_discovery(item)
        row["applications"].add(review_scope.application)
        row["review_scopes"].add((review_scope.application, review_scope.release_line))
        project_path = str(item.get("gitlab_project") or item.get("project_path") or "").strip()
        if project_path:
            row["project_paths"].add(project_path)
        row["mr_count"] = int(row.get("mr_count") or 0) + 1

    if _web_user_role(user) == "manager":
        for item in discovery.get("issues_without_mrs") or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("jira_key") or "").upper()
            if key:
                issue_rows.setdefault(key, _coverage_issue_row(key, summary=str(item.get("summary") or "")))

    scoped_keys = set(issue_rows)
    _emit_coverage_progress(
        progress,
        "coverage-reports",
        "Matching generated reports and active Review jobs.",
        total=len(scoped_keys),
    )
    reports = list_reports(user=user, days=0 if scoped_keys else _default_report_history_days())
    reports_by_issue: dict[str, list[dict[str, object]]] = {}
    for report in reports:
        key = _jira_key_from_report_name(str(report.get("relative_path") or report.get("name") or ""))
        if not key or (scoped_keys and key not in scoped_keys):
            continue
        reports_by_issue.setdefault(key, []).append(report)
        issue_rows.setdefault(key, _coverage_issue_row(key))

    jobs_by_issue: dict[str, list[dict[str, object]]] = {}
    for job in list_review_job_snapshots(user, limit=500):
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        key = str(payload.get("jira_key") or "").upper()
        if not key or (scoped_keys and key not in scoped_keys):
            continue
        jobs_by_issue.setdefault(key, []).append(job)
        issue_rows.setdefault(key, _coverage_issue_row(key))

    _emit_coverage_progress(
        progress,
        "coverage-workflow",
        "Loading handling results, Review Cycles, and Pass readiness.",
        total=len(issue_rows),
    )
    try:
        workflow_issues = {
            str(item.get("jira_key") or "").upper(): item
            for item in workflow_store().list_issues(view_all=True)
            if isinstance(item, dict) and item.get("jira_key")
        }
    except Exception:
        workflow_issues = {}

    _emit_coverage_progress(
        progress,
        "coverage-finalizing",
        "Calculating application readiness and final Sprint coverage.",
        total=len(issue_rows),
    )
    rows: list[dict[str, object]] = []
    for key, row in issue_rows.items():
        report_states = [_coverage_report_state(item) for item in reports_by_issue.get(key, [])]
        report_review_status = _aggregate_coverage_report_status(report_states) if report_states else ""
        jobs = jobs_by_issue.get(key, [])
        active_jobs = [item for item in jobs if str(item.get("status") or "") in {"queued", "running", "pausing", "paused", "stopping"}]
        failed_jobs = [item for item in jobs if str(item.get("status") or "") in {"failed", "canceled"}]
        if active_jobs:
            workflow_status = "running"
        elif report_states:
            workflow_status = _aggregate_coverage_report_status(report_states)
        elif failed_jobs:
            workflow_status = "failed"
        else:
            workflow_status = "missing"
        workflow_issue = workflow_issues.get(key) or {}
        current_cycle = workflow_issue.get("current_cycle") if isinstance(workflow_issue.get("current_cycle"), dict) else {}
        review_scopes = set(row.get("review_scopes") or set())
        review_scopes.update(
            (
                str(item.get("application") or "Unmapped"),
                str(item.get("release_line") or ""),
            )
            for item in report_states
        )
        if not review_scopes:
            review_scopes = {("Unmapped", "")}
        scope_statuses: list[dict[str, object]] = []
        for application, release_line in sorted(
            review_scopes,
            key=lambda name: (
                REVIEW_APPLICATION_ORDER.index(name[0]) if name[0] in REVIEW_APPLICATION_ORDER else len(REVIEW_APPLICATION_ORDER),
                str(name[1]),
            ),
        ):
            scoped_reports = [
                item
                for item in report_states
                if str(item.get("application") or "Unmapped") == application
                and str(item.get("release_line") or "") == release_line
            ]
            if active_jobs:
                scope_status = "running"
            elif scoped_reports:
                scope_status = _aggregate_coverage_report_status(scoped_reports)
            elif failed_jobs:
                scope_status = "failed"
            else:
                scope_status = "missing"
            scope_statuses.append(
                {
                    "application": application,
                    "release_line": release_line,
                    "scope_label": review_scope_label(application, release_line),
                    "scope_status": scope_status,
                    "has_report": bool(scoped_reports),
                    "report_count": len(scoped_reports),
                }
            )
        applications = list(dict.fromkeys(item["application"] for item in scope_statuses))
        rows.append(
            {
                **{
                    name: value
                    for name, value in row.items()
                    if name not in {"responsibles", "applications", "review_scopes", "project_paths"}
                },
                "responsible": "+".join(sorted(row.get("responsibles") or [], key=str.lower)) or "-",
                "applications": applications,
                "application_scopes": [
                    {
                        "application": item["application"],
                        "release_line": item["release_line"],
                        "scope_label": item["scope_label"],
                    }
                    for item in scope_statuses
                ],
                "scope_statuses": scope_statuses,
                "project_paths": sorted(row.get("project_paths") or [], key=str.lower),
                "workflow_status": workflow_status,
                "report_review_status": report_review_status,
                "report_count": len(report_states),
                "running_jobs": len(active_jobs),
                "finding_count": sum(int(item.get("finding_count") or 0) for item in report_states),
                "handled_count": sum(int(item.get("handled_count") or 0) for item in report_states),
                "blocking_pending": sum(int(item.get("blocking_pending") or 0) for item in report_states),
                "latest_report": str((reports_by_issue.get(key) or [{}])[0].get("relative_path") or ""),
                "latest_output_dir": str((reports_by_issue.get(key) or [{}])[0].get("output_dir") or ""),
                "review_cycle_number": int(current_cycle.get("cycle_number") or 0),
                "review_cycle_count": int(workflow_issue.get("cycle_count") or 0),
                "review_cycle_sprint": str(current_cycle.get("sprint_name") or current_cycle.get("sprint_id") or ""),
                "latest_run_number": int(workflow_issue.get("run_number") or 0),
                "review_snapshot_count": int(workflow_issue.get("review_snapshot_count") or 0),
            }
        )

    order = {"running": 0, "failed": 1, "missing": 2, "pending": 3, "ready": 4, "passed": 5}
    rows.sort(key=lambda item: (order.get(str(item.get("workflow_status") or ""), 9), str(item.get("jira_key") or "")))
    counts: dict[str, int] = {name: 0 for name in order}
    for row in rows:
        status = str(row.get("workflow_status") or "")
        counts[status] = counts.get(status, 0) + 1
    application_progress = _application_review_progress(rows)
    report_coverage = {
        **_coverage_report_summary(rows, counts),
        **_coverage_scope_summary(rows, application_progress),
    }
    return {
        "role": _web_user_role(user),
        "scope": {"jira": requested_keys, "sprint": sprint, "jira_filter": jira_filter},
        "counts": counts,
        "report_coverage": report_coverage,
        "application_progress": application_progress,
        "issues": rows,
        "discovery_errors": discovery.get("discovery_errors") or [],
    }


def _jira_keys_from_text(value: str) -> list[str]:
    # Report names use an underscore immediately after the Jira key, for
    # example ECHNL-5747_iTrade-Client-7.5.1_has-issue-critical.md.  ``\b``
    # does not match before an underscore because both digits and underscores
    # are regex word characters.  Use explicit Jira-token boundaries so the
    # leading ECHNL key is retained and a later release label such as CLIENT-7
    # is not mistaken for the report's issue key.
    return list(
        dict.fromkeys(
            match.upper()
            for match in re.findall(
                r"(?<![A-Z0-9])[A-Z][A-Z0-9]+-\d+(?![A-Z0-9])",
                (value or "").upper(),
            )
        )
    )


def _jira_key_from_report_name(value: str) -> str:
    keys = _jira_keys_from_text(value)
    return keys[0] if keys else ""


def _coverage_report_state(report: dict[str, object]) -> dict[str, object]:
    base = Path(str(report.get("output_dir") or ""))
    path = _safe_child_path(base, str(report.get("relative_path") or ""))
    text = path.read_text(encoding="utf-8", errors="ignore")
    findings = _extract_report_findings(text)
    thread = _load_report_thread(base, path)
    messages = thread.get("messages") if isinstance(thread.get("messages"), list) else []
    passed = any(isinstance(item, dict) and item.get("kind") == "manual-pass" for item in messages)
    handling = thread.get("handling_results") if isinstance(thread.get("handling_results"), dict) else {}
    summary = _handling_summary(findings, handling)
    metadata = _extract_report_metadata(text)
    scope = review_scope_for_merge_request(metadata)
    if passed:
        status = "passed"
    elif not findings or (summary["pending"] == 0 and summary["blocking_pending"] == 0):
        status = "ready"
    else:
        status = "pending"
    return {
        "status": status,
        "finding_count": len(findings),
        "handled_count": summary["completed"],
        "blocking_pending": summary["blocking_pending"],
        "application": scope.application,
        "release_line": scope.release_line,
        "scope_label": review_scope_label(scope.application, scope.release_line),
    }


def _aggregate_coverage_report_status(states: list[dict[str, object]]) -> str:
    values = [str(item.get("status") or "") for item in states]
    if "pending" in values:
        return "pending"
    if values and all(value == "passed" for value in values):
        return "passed"
    return "ready"


def _coverage_report_summary(
    rows: list[dict[str, object]], counts: dict[str, int]
) -> dict[str, object]:
    issues_with_reports = sum(1 for row in rows if int(row.get("report_count") or 0) > 0)
    return {
        "issues_with_reports": issues_with_reports,
        "issues_without_reports": len(rows) - issues_with_reports,
        "generated_breakdown": {
            "handling": sum(1 for row in rows if row.get("report_review_status") == "pending"),
            "ready": sum(1 for row in rows if row.get("report_review_status") == "ready"),
            "passed": sum(1 for row in rows if row.get("report_review_status") == "passed"),
        },
        "generating": counts.get("running", 0),
        "failed": counts.get("failed", 0),
    }


def _coverage_scope_summary(
    rows: list[dict[str, object]],
    application_progress: list[dict[str, object]],
) -> dict[str, int]:
    """Summarize required Application + Release Line report coverage.

    A Jira issue can legitimately belong to more than one release scope.  The
    unique-Issue summary remains useful, but release readiness must count each
    required scope independently and must not confuse reruns with additional
    required reports.
    """
    scope_count = sum(int(item.get("issue_count") or 0) for item in application_progress)
    scopes_with_reports = sum(int(item.get("issues_with_reports") or 0) for item in application_progress)
    return {
        "application_scope_count": scope_count,
        "application_scopes_with_reports": scopes_with_reports,
        "application_scopes_without_reports": max(0, scope_count - scopes_with_reports),
        "generated_report_files": sum(int(row.get("report_count") or 0) for row in rows),
    }


def find_reports_for_jira(jira_key: str, output_dir: str = "", user: str = "", owner_only: bool = False) -> list[dict[str, str | int]]:
    key = (jira_key or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", key):
        return []
    pattern = re.compile(rf"(?<![A-Z0-9]){re.escape(key)}(?![A-Z0-9])", re.I)
    matches: list[dict[str, str | int]] = []
    for report in list_reports(output_dir, user=user, days=0):
        if owner_only and user and str(report.get("responsible") or "").lower() != user.lower():
            continue
        relative_path = str(report.get("relative_path") or report.get("name") or "")
        if pattern.search(relative_path):
            matches.append(report)
    return matches


def report_reuse_check(jira_key: str, output_dir: str = "", user: str = "", state: str = "") -> dict[str, object]:
    key = (jira_key or "").strip().upper()
    reports = find_reports_for_jira_across_owners(key, output_dir=output_dir, user=user)
    current: dict[str, object] = {}
    freshness_error = ""
    if key and reports:
        try:
            current = jira_issue_review_fingerprint(key, state=state)
        except Exception as exc:
            freshness_error = str(exc)
    current_fingerprint = current.get("fingerprint") if isinstance(current.get("fingerprint"), dict) else {}
    current_stable = str(current_fingerprint.get("stable_fingerprint") or "") if isinstance(current_fingerprint, dict) else ""
    current_full = str(current_fingerprint.get("fingerprint") or "") if isinstance(current_fingerprint, dict) else ""
    for report in reports:
        report_signature = _report_signature_for_row(report)
        report["reuse_status"] = _report_reuse_status(report_signature, current_stable, current_full, freshness_error)
        report["reuse_reason"] = _report_reuse_reason(str(report["reuse_status"]), bool(report_signature), freshness_error)
        report["fresh"] = report["reuse_status"] == "fresh"
        report["fingerprint_source"] = report_signature.get("source", "") if report_signature else ""
    own_reports = [item for item in reports if bool(item.get("accessible"))]
    own_fresh = [item for item in own_reports if bool(item.get("fresh"))]
    other_fresh = [item for item in reports if bool(item.get("fresh")) and not bool(item.get("accessible"))]
    status = "none"
    recommendation = "run"
    if own_fresh:
        status = "fresh"
        recommendation = "reuse"
    elif other_fresh:
        status = "fresh-other-owner"
        recommendation = "contact-owner-or-rescan"
    elif reports and current_stable:
        status = "changed"
        recommendation = "rescan"
    elif reports:
        status = "unknown"
        recommendation = "confirm"
    return {
        "ok": True,
        "jira_key": key,
        "reports": reports,
        "own_reports": own_reports,
        "other_reports": [item for item in reports if not bool(item.get("accessible"))],
        "reuse": {
            "status": status,
            "recommendation": recommendation,
            "current_fingerprint": current_fingerprint,
            "fresh_accessible_count": len(own_fresh),
            "fresh_other_count": len(other_fresh),
            "freshness_error": freshness_error,
        },
    }


def find_reports_for_jira_across_owners(jira_key: str, output_dir: str = "", user: str = "") -> list[dict[str, object]]:
    key = (jira_key or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", key):
        return []
    pattern = re.compile(rf"(?<![A-Z0-9]){re.escape(key)}(?![A-Z0-9])", re.I)
    rows: list[dict[str, object]] = []
    for directory in _report_history_directories(output_dir):
        query = f"?output_dir={quote(str(directory), safe='')}"
        for path in directory.rglob("*.md"):
            relative = path.relative_to(directory).as_posix()
            if not pattern.search(relative):
                continue
            stat = path.stat()
            responsible = path.relative_to(directory).parts[0] if len(path.relative_to(directory).parts) > 1 else ""
            accessible = _can_access_report(directory, path, user)
            row: dict[str, object] = {
                "name": path.name,
                "relative_path": relative,
                "responsible": responsible,
                "output_dir": str(directory),
                "output_dir_name": directory.name,
                "size": stat.st_size,
                "modified": int(stat.st_mtime),
                "accessible": accessible,
            }
            if accessible:
                row.update(
                    {
                        "url": f"/reports/{quote(relative, safe='/')}{query}",
                        "download_url": f"/download/report/{quote(relative, safe='/')}{query}",
                        "thread_report": relative,
                        "thread_count": _report_thread_message_count(directory, path),
                    }
                )
            rows.append(row)
    return sorted(rows, key=lambda item: int(item.get("modified") or 0), reverse=True)


def _report_signature_for_row(report: dict[str, object]) -> dict[str, object]:
    try:
        base = Path(str(report.get("output_dir") or "")).expanduser().resolve()
        target = _safe_child_path(base, str(report.get("relative_path") or ""))
        if not target.exists():
            return {}
        return _extract_report_signature(target.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}


def _extract_report_signature(markdown: str) -> dict[str, object]:
    metadata = _extract_report_metadata(markdown)
    if metadata:
        stable = str(metadata.get("review_stable_fingerprint") or "")
        full = str(metadata.get("review_fingerprint") or "")
        if stable or full:
            return {
                "source": "metadata",
                "stable_fingerprint": stable,
                "fingerprint": full,
                "items": metadata.get("review_fingerprint_items") or [],
                "stable_items": metadata.get("review_stable_fingerprint_items") or [],
            }
    rows = _extract_report_related_mr_rows(markdown)
    if not rows:
        return {}
    fingerprint = review_fingerprint_from_merge_requests(rows)
    return {
        "source": "related-mr-table",
        "stable_fingerprint": fingerprint.get("stable_fingerprint", ""),
        "fingerprint": fingerprint.get("fingerprint", ""),
        "items": fingerprint.get("items", []),
        "stable_items": fingerprint.get("stable_items", []),
    }


def _extract_report_metadata(markdown: str) -> dict[str, object]:
    match = re.search(r"<!--\s*code_reviewer_metadata:\s*(\{.*?\})\s*-->", markdown or "", re.S)
    if not match:
        return {}
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _extract_report_related_mr_rows(markdown: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for line in (markdown or "").splitlines():
        if "/-/merge_requests/" not in line or not line.lstrip().startswith("|"):
            continue
        columns = [part.strip() for part in line.strip().strip("|").split("|")]
        if len(columns) < 6:
            continue
        link = re.search(r"\[[^\]]+\]\((https?://[^)]+/-/merge_requests/\d+)\)", columns[0])
        if not link:
            continue
        mr_url = link.group(1)
        records.append(
            {
                "mr_url": mr_url,
                "state": _strip_markdown_cell(columns[1]),
                "project_path": _strip_markdown_cell(columns[2]),
                "source_branch": _strip_markdown_cell(columns[3]),
                "target_branch": _strip_markdown_cell(columns[4]),
                "commit": _strip_markdown_cell(columns[5]),
            }
        )
    return records


def _strip_markdown_cell(value: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", value or "")
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return html.unescape(text).strip()


def _report_reuse_status(signature: dict[str, object], current_stable: str, current_full: str, error: str = "") -> str:
    if error:
        return "unknown"
    if not signature or not (current_stable or current_full):
        return "unknown"
    report_stable = str(signature.get("stable_fingerprint") or "")
    report_full = str(signature.get("fingerprint") or "")
    if report_stable and current_stable and report_stable == current_stable:
        return "fresh"
    if report_full and current_full and report_full == current_full:
        return "fresh"
    return "changed"


def _report_reuse_reason(status: str, has_signature: bool, error: str = "") -> str:
    if status == "fresh":
        return "MR set and commits match current GitLab/Jira metadata."
    if status == "changed":
        return "Current GitLab/Jira metadata differs from this report."
    if error:
        return f"Could not verify freshness: {error}"
    if not has_signature:
        return "Report has no reusable MR fingerprint."
    return "Freshness could not be verified."


def compare_report_with_previous(base: Path, target: Path, user: str = "") -> dict[str, object]:
    base = base.resolve()
    target = target.resolve()
    jira_key = _jira_key_from_text(target.name)
    if not jira_key:
        raise ValueError("No Jira issue key was detected from the report name.")
    current_relative = target.relative_to(base).as_posix()
    current_owner = _report_owner_from_relative(current_relative)
    reports = [
        item
        for item in find_reports_for_jira(jira_key, str(base), user=user)
        if _report_owner_from_relative(str(item.get("relative_path") or "")) == current_owner
    ]
    reports = [item for item in reports if str(item.get("relative_path") or "") != current_relative or int(item.get("modified") or 0) <= int(target.stat().st_mtime)]
    reports.sort(key=lambda item: int(item.get("modified") or 0), reverse=True)
    current_index = next((index for index, item in enumerate(reports) if str(item.get("relative_path") or "") == current_relative), -1)
    previous = reports[current_index + 1] if current_index >= 0 and current_index + 1 < len(reports) else None
    current_findings = _parse_report_findings(target.read_text(encoding="utf-8", errors="ignore"))
    previous_findings: list[dict[str, str]] = []
    previous_relative = ""
    if previous:
        previous_relative = str(previous.get("relative_path") or "")
        previous_path = _safe_child_path(base, previous_relative)
        if previous_path.exists() and _can_access_report(base, previous_path, user):
            previous_findings = _parse_report_findings(previous_path.read_text(encoding="utf-8", errors="ignore"))
    comparison = _compare_finding_sets(current_findings, previous_findings)
    markdown = _render_report_comparison_markdown(jira_key, current_relative, previous_relative, comparison)
    return {
        "jira_key": jira_key,
        "current_report": current_relative,
        "previous_report": previous_relative,
        "comparison": comparison,
        "markdown": markdown,
    }


def _jira_key_from_text(value: str) -> str:
    match = re.search(r"(?<![A-Z0-9])([A-Z][A-Z0-9]+-\d+)(?![A-Z0-9])", value or "", re.I)
    return match.group(1).upper() if match else ""


def _report_owner_from_relative(value: str) -> str:
    parts = str(value or "").replace("\\", "/").split("/")
    return parts[0].lower() if len(parts) > 1 else ""


def _parse_report_findings(markdown: str) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    pattern = re.compile(r"^###\s+\d+\.\s+\[([^\]]+)\]\s+(.+?)\s*$", re.M)
    for match in pattern.finditer(markdown or ""):
        severity = re.sub(r"\s+", " ", match.group(1).strip())
        title = re.sub(r"\s+", " ", re.sub(r"`([^`]+)`", r"\1", match.group(2).strip()))
        if title:
            findings.append({"severity": severity, "title": title, "key": _finding_compare_key(title)})
    return findings


def _finding_compare_key(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def _compare_finding_sets(current: list[dict[str, str]], previous: list[dict[str, str]]) -> dict[str, object]:
    current_by_key = {item["key"]: item for item in current if item.get("key")}
    previous_by_key = {item["key"]: item for item in previous if item.get("key")}
    added = [current_by_key[key] for key in sorted(current_by_key.keys() - previous_by_key.keys())]
    resolved = [previous_by_key[key] for key in sorted(previous_by_key.keys() - current_by_key.keys())]
    unchanged = [current_by_key[key] for key in sorted(current_by_key.keys() & previous_by_key.keys()) if current_by_key[key].get("severity") == previous_by_key[key].get("severity")]
    changed = [
        {
            "title": current_by_key[key].get("title", ""),
            "previous_severity": previous_by_key[key].get("severity", ""),
            "current_severity": current_by_key[key].get("severity", ""),
        }
        for key in sorted(current_by_key.keys() & previous_by_key.keys())
        if current_by_key[key].get("severity") != previous_by_key[key].get("severity")
    ]
    return {
        "current_count": len(current),
        "previous_count": len(previous),
        "added": added,
        "resolved": resolved,
        "unchanged": unchanged,
        "severity_changed": changed,
    }


def _render_report_comparison_markdown(jira_key: str, current_report: str, previous_report: str, comparison: dict[str, object]) -> str:
    lines = [
        f"# {jira_key} Code Review Report Compare",
        "",
        f"- Current: `{current_report}`",
        f"- Previous: `{previous_report or '-'}`",
        f"- Current finding count: {comparison.get('current_count', 0)}",
        f"- Previous finding count: {comparison.get('previous_count', 0)}",
        "",
        "## 新增问题",
        "",
        *_finding_lines(comparison.get("added")),
        "",
        "## 已减少问题",
        "",
        *_finding_lines(comparison.get("resolved")),
        "",
        "## 严重级别变化",
        "",
        *_severity_change_lines(comparison.get("severity_changed")),
        "",
        "## 仍存在问题",
        "",
        *_finding_lines(comparison.get("unchanged")),
    ]
    return "\n".join(lines).strip() + "\n"


def _finding_lines(value: object) -> list[str]:
    items = value if isinstance(value, list) else []
    if not items:
        return ["- 无"]
    return [f"- [{item.get('severity', '-')}] {item.get('title', '-')}" for item in items if isinstance(item, dict)]


def _severity_change_lines(value: object) -> list[str]:
    items = value if isinstance(value, list) else []
    if not items:
        return ["- 无"]
    return [
        f"- {item.get('title', '-')}：{item.get('previous_severity', '-')} -> {item.get('current_severity', '-')}"
        for item in items
        if isinstance(item, dict)
    ]


def _resolve_report_for_user(report_name: str, output_dir: str = "", user: str = "") -> tuple[Path, Path]:
    base = _report_dir(output_dir)
    try:
        target = _safe_child_path(base, unquote(report_name))
    except FileNotFoundError as exc:
        raise FileNotFoundError("Invalid report path") from exc
    if not target.exists():
        if not output_dir:
            fallback = _find_report_in_known_output_dirs(unquote(report_name), user)
            if fallback:
                return fallback
        raise FileNotFoundError("Report not found")
    if not _can_access_report(base, target, user):
        raise PermissionError("Forbidden")
    return base, target


def _find_report_in_known_output_dirs(report_name: str, user: str = "") -> tuple[Path, Path] | None:
    current = report_output_dir().expanduser().resolve()
    candidates: list[Path] = [current]
    parent = current.parent
    if parent.exists():
        candidates.extend(
            sorted(
                [path for path in parent.glob("e-channel-sprint*") if path.is_dir()],
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        )
    seen: set[Path] = set()
    for base in candidates:
        base = base.expanduser().resolve()
        if base in seen:
            continue
        seen.add(base)
        try:
            target = _safe_child_path(base, report_name)
        except FileNotFoundError:
            continue
        if target.exists() and _can_access_report(base, target, user):
            return base, target
    return None


def _report_thread_key(base: Path, report_path: Path) -> str:
    try:
        relative = report_path.relative_to(base).as_posix()
    except ValueError:
        relative = report_path.name
    raw = f"{base.resolve()}::{relative}".encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()


def _report_thread_path(base: Path, report_path: Path) -> Path:
    return WEB_THREADS_DIR / f"{_report_thread_key(base, report_path)}.json"


def _load_report_thread(base: Path, report_path: Path) -> dict[str, object]:
    WEB_THREADS_DIR.mkdir(parents=True, exist_ok=True)
    thread_path = _report_thread_path(base, report_path)
    relative = report_path.relative_to(base).as_posix()
    data: dict[str, object] = {
        "report": relative,
        "report_name": report_path.name,
        "messages": [],
        "handling_results": {},
        "followup_draft": "",
        "followup_instruction": "",
        "updated_at": "",
    }
    if thread_path.exists():
        try:
            loaded = json.loads(thread_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(loaded)
        except Exception:
            pass
    data["report"] = relative
    data["report_name"] = report_path.name
    return data


def _save_report_thread(base: Path, report_path: Path, data: dict[str, object]) -> dict[str, object]:
    WEB_THREADS_DIR.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _report_thread_path(base, report_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def _report_thread_payload(base: Path, report_path: Path, user: str) -> dict[str, object]:
    data = _load_report_thread(base, report_path)
    report_text = report_path.read_text(encoding="utf-8", errors="ignore")
    findings = _extract_report_findings(report_text)
    handling = data.get("handling_results") if isinstance(data.get("handling_results"), dict) else {}
    readiness = _manual_pass_readiness(report_path, data, findings=findings)
    title_match = re.search(r"(?m)^#\s+(.+?)\s*$", report_text)
    issue_title = (title_match.group(1).strip() if title_match else report_path.stem.replace("_", " "))
    key_match = re.search(r"\b[A-Z][A-Z0-9]+-\d+\b", issue_title.upper()) or re.search(
        r"\b[A-Z][A-Z0-9]+-\d+\b", report_path.name.upper()
    )
    return {
        **data,
        "jira_key": key_match.group(0) if key_match else "",
        "issue_title": issue_title,
        "findings": findings,
        "handling_results": handling,
        "handling_summary": _handling_summary(findings, handling),
        "pass_readiness": readiness,
        "permissions": _web_user_permissions(user),
        "role": _web_user_role(user),
    }


def _record_finding_handling(
    base: Path,
    report_path: Path,
    user: str,
    finding_index: str,
    disposition: str,
    note: str,
) -> dict[str, object]:
    allowed = {"fixed", "follow-up", "not-issue"}
    if disposition not in allowed:
        raise ValueError("Handling result must be fixed, follow-up, or not-issue.")
    report_text = report_path.read_text(encoding="utf-8", errors="ignore")
    findings = {item["index"]: item for item in _extract_report_findings(report_text)}
    if finding_index not in findings:
        raise ValueError(f"Finding #{finding_index or '-'} was not found in this report.")
    if not note:
        raise ValueError("Handling explanation is required.")
    data = _load_report_thread(base, report_path)
    handling = data.setdefault("handling_results", {})
    if not isinstance(handling, dict):
        handling = {}
        data["handling_results"] = handling
    finding = findings[finding_index]
    handling[finding_index] = {
        "finding_index": finding_index,
        "severity": finding.get("severity", ""),
        "title": finding.get("title", ""),
        "disposition": disposition,
        "note": note[:4000],
        "user": user,
        "time": datetime.now().isoformat(timespec="seconds"),
    }
    messages = data.setdefault("messages", [])
    if not isinstance(messages, list):
        messages = []
        data["messages"] = messages
    label = {"fixed": "已整改，Pass通过", "follow-up": "不是阻碍，另报 Jira issue 跟进", "not-issue": "不是问题，Pass通过"}[disposition]
    messages.append(
        {
            "id": len(messages) + 1,
            "time": datetime.now().isoformat(timespec="seconds"),
            "user": user,
            "kind": "handling-result",
            "message": f"#{finding_index} [{finding.get('severity', '-')}] {finding.get('title', '-')}\n{label}\n说明：{note[:4000]}",
        }
    )
    return _save_report_thread(base, report_path, data)


def _handling_summary(findings: list[dict[str, str]], handling: dict[str, object]) -> dict[str, int]:
    blocking = [item for item in findings if str(item.get("severity") or "").title() in blocking_severities()]
    completed = [item for item in findings if item.get("index") in handling]
    blocking_completed = [
        item
        for item in blocking
        if isinstance(handling.get(item.get("index", "")), dict)
        and str(handling[item["index"]].get("disposition") or "") == "not-issue"
        and _web_user_role(str(handling[item["index"]].get("user") or "")) in {"auditor", "manager"}
    ]
    return {
        "total": len(findings),
        "completed": len(completed),
        "pending": max(0, len(findings) - len(completed)),
        "blocking": len(blocking),
        "blocking_completed": len(blocking_completed),
        "blocking_pending": max(0, len(blocking) - len(blocking_completed)),
    }


def _manual_pass_readiness(
    report_path: Path,
    thread: dict[str, object],
    findings: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    findings = findings if findings is not None else _extract_report_findings(report_path.read_text(encoding="utf-8", errors="ignore"))
    handling = thread.get("handling_results") if isinstance(thread.get("handling_results"), dict) else {}
    summary = _handling_summary(findings, handling)
    pending = []
    for item in findings:
        if str(item.get("severity") or "").title() not in blocking_severities():
            continue
        result = handling.get(item.get("index", ""))
        disposition = str(result.get("disposition") or "") if isinstance(result, dict) else ""
        actor_role = _web_user_role(str(result.get("user") or "")) if isinstance(result, dict) else ""
        if disposition != "not-issue" or actor_role not in {"auditor", "manager"}:
            pending.append(item)
    ready = not pending
    return {
        "ready": ready,
        "message": (
            "All Critical/High findings are absent after re-scan or approved as Not an issue."
            if ready
            else f"{len(pending)} Critical/High finding(s) still require a clean re-scan or Auditor/Manager Not an issue approval."
        ),
        "pending_blockers": pending,
        **summary,
    }


def _report_thread_message_count(base: Path, report_path: Path) -> int:
    data = _load_report_thread(base, report_path)
    messages = data.get("messages") or []
    return len(messages) if isinstance(messages, list) else 0


def _append_report_thread_message(base: Path, report_path: Path, user: str, kind: str, message: str) -> dict[str, object]:
    data = _load_report_thread(base, report_path)
    messages = data.setdefault("messages", [])
    if not isinstance(messages, list):
        messages = []
        data["messages"] = messages
    messages.append(
        {
            "id": len(messages) + 1,
            "time": datetime.now().isoformat(timespec="seconds"),
            "user": user,
            "kind": kind,
            "message": message[:8000],
        }
    )
    return _save_report_thread(base, report_path, data)


def _generate_report_followups(base: Path, report_path: Path, user: str, instruction: str) -> dict[str, object]:
    data = _load_report_thread(base, report_path)
    report_text = report_path.read_text(encoding="utf-8", errors="ignore")
    messages = data.get("messages") if isinstance(data.get("messages"), list) else []
    draft = _build_followup_draft(report_path.name, report_text, messages, instruction)
    data["followup_instruction"] = instruction
    data["followup_draft"] = draft
    messages = data.setdefault("messages", [])
    if isinstance(messages, list):
        messages.append(
            {
                "id": len(messages) + 1,
                "time": datetime.now().isoformat(timespec="seconds"),
                "user": user,
                "kind": "followup-draft",
                "message": f"生成后续跟进清单。整理说明：{instruction or '-'}",
            }
        )
    return _save_report_thread(base, report_path, data)


def _build_followup_draft(report_name: str, report_text: str, messages: list[object], instruction: str) -> str:
    findings = _extract_report_findings(report_text)
    message_texts = [str(item.get("message") or "") for item in messages if isinstance(item, dict)]
    followups: list[dict[str, str]] = []
    passed: list[dict[str, str]] = []
    clarified: list[dict[str, str]] = []
    pending: list[dict[str, str]] = []
    for finding in findings:
        related_text = _related_thread_text_for_finding(finding, message_texts)
        lowered = related_text.lower()
        if _contains_any(related_text, ["另报issue", "另报 issue", "另报Jira", "另报 Jira", "后续跟进", "改善项", "not blocking", "follow up"]):
            followups.append(finding)
        elif _contains_any(related_text, ["已整改", "pass通过", "pass 通过", "fixed", "已修复"]):
            passed.append(finding)
        elif _contains_any(related_text, ["不是问题", "不构成问题", "not an issue", "误报", "false positive"]):
            clarified.append(finding)
        elif instruction and ("全部" in instruction or "整理" in instruction or "follow" in lowered):
            pending.append(finding)
        else:
            pending.append(finding)

    lines = [
        f"# {report_name} 后续跟进功能清单",
        "",
        f"- 报告：{report_name}",
        f"- 整理说明：{instruction or '-'}",
        f"- 沟通记录数：{len(message_texts)}",
        "",
        "## 需另报 Jira issue 跟进",
        "",
    ]
    lines.extend(_followup_items(followups, "暂无明确需另报 Jira issue 的事项。"))
    lines.extend(["", "## 待澄清或待处理", ""])
    lines.extend(_followup_items(pending, "暂无待澄清事项。"))
    lines.extend(["", "## 已整改或确认通过", ""])
    lines.extend(_followup_items(passed, "暂无已整改记录。"))
    lines.extend(["", "## 判定不是问题", ""])
    lines.extend(_followup_items(clarified, "暂无不是问题的澄清记录。"))
    return "\n".join(lines).strip() + "\n"


def _build_report_chat_reply(report_name: str, report_text: str, thread: dict[str, object], prompt: str) -> str:
    findings = _extract_report_findings(report_text)
    messages = thread.get("messages") if isinstance(thread.get("messages"), list) else []
    severity_order = {"Critical": 4, "High": 3, "Medium": 2, "Low": 1, "Warning": 1}
    blocking = [item for item in findings if severity_order.get(item.get("severity", ""), 0) >= 3]
    followup_draft = _text(thread.get("followup_draft")).strip()
    handling = thread.get("handling_results") if isinstance(thread.get("handling_results"), dict) else {}
    handling_summary = _handling_summary(findings, handling)
    pending_findings = [item for item in findings if item.get("index") not in handling]
    prompt_lower = prompt.lower()
    lines = [
        f"Report: {report_name}",
        f"Findings: {len(findings)} total; blocking candidates: {len(blocking)} Critical/High.",
        f"Communication records: {len(messages)}.",
        (
            f"Handling results: {handling_summary['completed']}/{handling_summary['total']} completed; "
            f"{handling_summary['blocking_pending']} Critical/High blocker(s) still pending acceptable handling."
        ),
    ]
    if "pass" in prompt_lower or "通过" in prompt:
        if handling_summary["blocking_pending"]:
            lines.append("Manual pass must wait until every Critical/High item is marked Fixed or Not an issue, or a re-scan removes it.")
        else:
            lines.append("All parsed Critical/High items have acceptable handling results. An Auditor or Manager can consider Manual Review Pass.")
    elif "teams" in prompt_lower or "群" in prompt or "发送" in prompt:
        lines.append("Suggested Teams message: include the report link, responsible owner, current blocker count, and request handling result by issue number.")
    elif "follow" in prompt_lower or "跟进" in prompt:
        lines.append("Use Generate Follow-ups to classify items into separate Jira follow-up, fixed/pass, and clarified-not-issue buckets.")
    else:
        lines.append("Suggested next step: ask the responsible lead to classify each issue as fixed/pass, follow-up Jira, or clarified-not-issue.")
    if blocking:
        lines.append("")
        lines.append("Top blocking items:")
        for item in blocking[:8]:
            lines.append(f"- [{item.get('severity', '-')}] {item.get('title', '-')}")
    if pending_findings and ("未处理" in prompt or "pending" in prompt_lower or "还有" in prompt):
        lines.append("")
        lines.append("Pending handling items:")
        for item in pending_findings[:20]:
            lines.append(f"- #{item.get('index', '-')} [{item.get('severity', '-')}] {item.get('title', '-')}")
    if followup_draft:
        lines.append("")
        lines.append("Existing follow-up draft is available in the Follow-up tab.")
    return "\n".join(lines)


def _extract_report_findings(report_text: str) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    pattern = re.compile(r"^###\s+(\d+)\.\s+\[([^\]]+)\]\s+(.+?)\s*$", re.M)
    matches = list(pattern.finditer(report_text or ""))
    for position, match in enumerate(matches):
        section_end = matches[position + 1].start() if position + 1 < len(matches) else len(report_text or "")
        section = (report_text or "")[match.end() : section_end]
        problem = _report_finding_labeled_text(
            section,
            r"(?:问题(?:描述|详情)?|Problem(?:\s+(?:Description|Detail))?|Detail)",
        )
        suggestion = _report_finding_labeled_text(
            section,
            r"(?:建议|处理建议|解决建议|Recommendation|Suggestion)",
        )
        findings.append(
            {
                "index": match.group(1),
                "severity": match.group(2).strip(),
                "title": match.group(3).strip(),
                "problem": problem,
                "detail": problem,
                "suggestion": suggestion,
                "recommendation": suggestion,
            }
        )
    return findings


def _report_finding_labeled_text(section: str, label_pattern: str) -> str:
    match = re.search(
        rf"^\s*[-*]\s*(?:{label_pattern})\s*[:：]\s*(.+?)\s*$",
        section or "",
        re.I | re.M,
    )
    if not match:
        return ""
    value = re.sub(r"`([^`]+)`", r"\1", match.group(1).strip())
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def _enrich_issue_review_finding_details(detail: dict[str, object]) -> None:
    """Add report problem/recommendation text to legacy workflow rows at read time."""
    parsed_by_run: dict[str, dict[str, dict[str, str]]] = {}
    for run in detail.get("runs") or []:
        if not isinstance(run, dict):
            continue
        report_path = Path(str(run.get("report_path") or ""))
        parsed = _extract_report_findings(
            report_path.read_text(encoding="utf-8", errors="ignore")
        ) if report_path.is_file() else []
        by_index = {str(item.get("index") or ""): item for item in parsed}
        parsed_by_run[str(run.get("id") or "")] = by_index
        for finding in run.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            source = by_index.get(str(finding.get("report_index") or ""), {})
            details = finding.get("details") if isinstance(finding.get("details"), dict) else {}
            finding["details"] = {**source, **details}
    latest_group = detail.get("latest_run_group")
    if not isinstance(latest_group, dict):
        return
    for finding in latest_group.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        source = parsed_by_run.get(str(finding.get("run_id") or ""), {}).get(
            str(finding.get("report_index") or ""),
            {},
        )
        details = finding.get("details") if isinstance(finding.get("details"), dict) else {}
        finding["details"] = {**source, **details}


def _related_thread_text_for_finding(finding: dict[str, str], messages: list[str]) -> str:
    title = finding.get("title", "")
    index = finding.get("index", "")
    related: list[str] = []
    title_key = title[:18]
    for message in messages:
        if (index and re.search(rf"(^|\D){re.escape(index)}(\D|$)", message)) or (title_key and title_key in message) or title in message:
            related.append(message)
    return "\n".join(related or messages)


def _contains_any(text: str, tokens: list[str]) -> bool:
    lowered = text.lower()
    return any(token.lower() in lowered for token in tokens)


def _followup_items(items: list[dict[str, str]], empty_text: str) -> list[str]:
    if not items:
        return [f"- {empty_text}"]
    lines: list[str] = []
    for item in items:
        lines.append(f"- [{item.get('severity', '-')}] {item.get('title', '-')}（原问题 #{item.get('index', '-')}）")
    return lines


def list_responsibles(output_dir: str = "", user: str = "", days: int | None = None) -> list[dict[str, str | int]]:
    ensure_directories()
    grouped: dict[str, dict[str, str | int]] = {}
    cutoff = _report_history_cutoff(days)
    multi_dir = not output_dir
    for directory in _report_history_directories(output_dir):
        query = f"?output_dir={quote(str(directory), safe='')}"
        for path in directory.rglob("*.md"):
            stat = path.stat()
            if cutoff and stat.st_mtime < cutoff:
                continue
            relative_parts = path.relative_to(directory).parts
            responsible = relative_parts[0] if len(relative_parts) > 1 else "__root__"
            if not _can_access_report(directory, path, user):
                continue
            label = "root" if responsible == "__root__" else responsible
            group_key = f"{directory}::{responsible}" if multi_dir else responsible
            entry = grouped.setdefault(
                group_key,
                {
                    "name": f"{label} · {directory.name}" if multi_dir else label,
                    "responsible": label,
                    "output_dir": str(directory),
                    "output_dir_name": directory.name,
                    "count": 0,
                    "size": 0,
                    "modified": 0,
                    "download_url": f"/download/responsible/{quote(responsible, safe='')}{query}",
                },
            )
            entry["count"] = int(entry["count"]) + 1
            entry["size"] = int(entry["size"]) + stat.st_size
            entry["modified"] = max(int(entry["modified"]), int(stat.st_mtime))
    return sorted(grouped.values(), key=lambda item: int(item["modified"]), reverse=True)


def list_gitlab_projects_for_user(user: str) -> list[dict[str, object]]:
    projects: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in git_tools_project_entries():
        if not _can_access_project_responsible(entry.responsible, user):
            continue
        key = (entry.group, entry.module, entry.project_path)
        if key in seen:
            continue
        seen.add(key)
        projects.append(
            {
                "group": entry.group,
                "group_display": _display_group_name(entry.group),
                "module": entry.module,
                "display_name": entry.project_name or entry.module or entry.project_path.split("/")[-1],
                "project_path": entry.project_path,
                "repository_url": entry.repository_url,
                "responsible": _canonical_people_text(entry.responsible),
                "llm_model": entry.llm_model,
                "application": entry.application,
                "dev_branch": entry.dev_branch,
            }
        )
    return sorted(
        projects,
        key=lambda item: (
            str(item.get("group") or "").lower(),
            str(item.get("display_name") or "").lower(),
            str(item.get("project_path") or "").lower(),
        ),
    )


def _report_dir(output_dir: str = "") -> Path:
    return (Path(output_dir).expanduser() if output_dir else report_output_dir()).resolve()


def _can_access_report(
    base: Path,
    report_path: Path,
    user: str,
    *,
    metadata: dict[str, object] | None = None,
) -> bool:
    if not user:
        return False
    try:
        parts = report_path.resolve().relative_to(base.resolve()).parts
    except ValueError:
        return False
    responsible = parts[0] if len(parts) > 1 else "__root__"
    if _can_access_responsible(responsible, user):
        return True
    metadata = metadata if metadata is not None else _read_report_metadata(report_path)
    report_people = {
        item.casefold()
        for item in scope_people(metadata.get("responsible_scope") or metadata.get("responsible"))
    }
    allowed = {item.casefold() for item in _web_user_responsibles(user)}
    return bool(report_people & allowed)


def _read_report_metadata(path: Path, max_chars: int = 65536) -> dict[str, object]:
    """Read only the report header where the hidden metadata contract lives."""
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as stream:
            return _extract_report_metadata(stream.read(max(4096, max_chars)))
    except OSError:
        return {}


def _can_access_responsible(responsible: str, user: str) -> bool:
    if not user:
        return False
    if _web_user_role(user) == "manager":
        return True
    normalized = responsible if responsible != "__root__" else "root"
    allowed = {item.lower() for item in _web_user_responsibles(user)}
    return bool(allowed.intersection(item.lower() for item in _split_people(normalized)))


def _can_access_project_responsible(responsible: str, user: str) -> bool:
    if _web_user_role(user) == "manager":
        return True
    allowed = {item.lower() for item in _web_user_responsibles(user)}
    return bool(allowed.intersection(item.lower() for item in _split_people(responsible)))


def _split_people(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[+,;]+", value or "") if item.strip()]


def _is_admin_user(user: str) -> bool:
    return _web_user_role(user) == "manager"


def _default_web_user_role(user: str) -> str:
    return "manager" if (user or "").strip().lower() in {"admin", "root"} else "auditor"


def _web_user_record(user: str) -> dict[str, object]:
    username = (user or "").strip()
    stored = _load_web_users().get(username)
    configured = _configured_web_user_profiles().get(username)
    return {
        **(configured if isinstance(configured, dict) else {}),
        **(stored if isinstance(stored, dict) else {}),
    }


def _configured_web_user_profiles() -> dict[str, dict[str, object]]:
    value = app_config_get("web.users", {})
    if not isinstance(value, dict):
        return {}
    return {
        str(username).strip(): profile
        for username, profile in value.items()
        if str(username).strip() and isinstance(profile, dict)
    }


def _web_user_role(user: str) -> str:
    value = str(_web_user_record(user).get("role") or _default_web_user_role(user)).strip().lower()
    return value if value in WEB_ROLES else _default_web_user_role(user)


def _web_user_responsibles(user: str) -> list[str]:
    record = _web_user_record(user)
    configured = record.get("responsible") or record.get("responsibles")
    if isinstance(configured, list):
        values = [str(item).strip() for item in configured if str(item).strip()]
    else:
        values = _split_people(str(configured or ""))
    return values or [(user or "").strip()]


def _web_user_permissions(user: str) -> dict[str, bool]:
    role = _web_user_role(user)
    return {
        "run_issue_review": role in {"manager", "auditor"},
        "run_sprint_review": role == "manager",
        "run_release_gate": role == "manager",
        "scan_coverage": role in {"manager", "auditor"},
        "submit_handling": role in {"manager", "auditor", "developer"},
        "ai_chat": role in {"manager", "auditor"},
        "manual_pass": role in {"manager", "auditor"},
        "view_all": role == "manager",
        "manage_users": role == "manager",
    }


def _issue_access_allowed(user: str, jira_key: str) -> bool:
    if _web_user_permissions(user)["view_all"]:
        return True
    detail = workflow_store().issue_detail((jira_key or "").strip().upper())
    if not detail:
        return False
    issue = detail.get("issue") if isinstance(detail.get("issue"), dict) else {}
    owners = {
        item.casefold()
        for item in scope_people(issue.get("responsible_scope") or issue.get("responsible"))
    }
    allowed = {item.casefold() for item in _web_user_responsibles(user)}
    return bool(owners & allowed)


def _require_issue_access(user: str, jira_key: str) -> None:
    if not _issue_access_allowed(user, jira_key):
        raise PermissionError("You do not have access to this Issue Review.")


def _workflow_issues_for_user(user: str) -> list[dict[str, object]]:
    permissions = _web_user_permissions(user)
    return workflow_store().list_issues(
        responsibles=_web_user_responsibles(user),
        view_all=permissions["view_all"],
    )


def recent_workflow_sprints(query: str = "", limit: int = 30) -> list[dict[str, str]]:
    """Return deduplicated Sprint choices retained by workflow history.

    Jira remains authoritative at preflight time.  This local index makes focus
    suggestions instant and continues to work if Jira search is momentarily slow.
    """
    needle = (query or "").strip().casefold()
    cutoff = time.time() - (31 * 24 * 60 * 60)
    choices: dict[str, dict[str, str]] = {}
    store = workflow_store()
    try:
        issues = store.list_issues(view_all=True)
    except TypeError:
        issues = store.list_issues()
    for issue in issues:
        jira_key = str(issue.get("jira_key") or "")
        detail = store.issue_detail(jira_key) or {}
        cycles = detail.get("cycles") if isinstance(detail.get("cycles"), list) else []
        for cycle in cycles:
            if not isinstance(cycle, dict):
                continue
            sprint_id = str(cycle.get("sprint_id") or "").strip()
            name = str(cycle.get("sprint_name") or "").strip()
            if not sprint_id and not name:
                continue
            key = sprint_id or name.casefold()
            label = f"{name} ({sprint_id})" if name and sprint_id else (name or sprint_id)
            if needle and needle not in label.casefold():
                continue
            candidate = {
                "id": sprint_id,
                "name": name,
                "label": label,
                "state": str(cycle.get("sprint_state") or cycle.get("state") or ""),
                "updated_at": str(cycle.get("updated_at") or cycle.get("cycle_started_at") or ""),
            }
            try:
                candidate_time = datetime.fromisoformat(candidate["updated_at"].replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                candidate_time = 0
            if candidate_time < cutoff:
                continue
            previous = choices.get(key)
            if previous is None or candidate["updated_at"] > previous["updated_at"]:
                choices[key] = candidate
    return sorted(choices.values(), key=lambda item: item["updated_at"], reverse=True)[: max(1, min(limit, 100))]


def _current_cycle_id(jira_key: str) -> str:
    detail = workflow_store().issue_detail((jira_key or "").strip().upper()) or {}
    issue = detail.get("issue") if isinstance(detail.get("issue"), dict) else {}
    return str(issue.get("current_cycle_id") or "")


def _snapshot_completed_handling(jira_key: str, actor: str, *, request_key: str = "") -> dict[str, object] | None:
    """Create one immutable snapshot when the latest Run Group is fully handled.

    Pass readiness intentionally remains a separate blocking-severity policy.
    Snapshot readiness is stricter: every finding in the latest logical
    frontend/backend Run Group must have one submitted handling result.
    """
    store = workflow_store()
    detail = store.issue_detail((jira_key or "").strip().upper()) or {}
    issue = detail.get("issue") if isinstance(detail.get("issue"), dict) else {}
    cycle_id = str(issue.get("current_cycle_id") or "")
    groups = [
        item for item in (detail.get("run_groups") or [])
        if isinstance(item, dict) and str(item.get("cycle_id") or "") == cycle_id
    ]
    if not cycle_id or not groups:
        return None
    latest_group = max(groups, key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")))
    group_id = str(latest_group.get("id") or "")
    runs = [
        item for item in (detail.get("runs") or [])
        if isinstance(item, dict) and str(item.get("run_group_id") or "") == group_id
    ]
    findings = [
        finding
        for run in runs
        for finding in (run.get("findings") or [])
        if isinstance(finding, dict)
    ]
    if not findings or any(not isinstance(finding.get("handling"), dict) for finding in findings):
        return None
    handling_ids = sorted(str(finding["handling"].get("id") or "") for finding in findings)
    # Idempotency is tied to the completed business state, not the HTTP request.
    # Approval or retry requests must not create a duplicate all-handled snapshot.
    stable_key = hashlib.sha256("|".join(handling_ids).encode("utf-8")).hexdigest()
    return store.create_review_snapshot(
        cycle_id=cycle_id,
        reason="all-findings-handled",
        actor=actor or "system",
        idempotency_key=f"all-handled:{group_id}:{stable_key}",
    )


def _plain_text_adf(value: str) -> dict[str, object]:
    text_value = (value or "").strip()
    paragraph: dict[str, object] = {"type": "paragraph", "content": []}
    if text_value:
        paragraph["content"] = [{"type": "text", "text": text_value}]
    return {"version": 1, "type": "doc", "content": [paragraph]}


def _workflow_cycle_from_history_entry(
    store: object,
    entry: dict[str, object],
    metadata: dict[str, object],
    jira_key: str,
    summary: str,
    responsible: str,
) -> tuple[str, str]:
    memberships = metadata.get("jira_sprint_memberships")
    membership_rows = [item for item in memberships if isinstance(item, dict)] if isinstance(memberships, list) else []
    current_scope = metadata.get("current_review_scope") if isinstance(metadata.get("current_review_scope"), dict) else {}
    preferred_id = str(current_scope.get("sprint_id") or metadata.get("jira_current_sprint_id") or "").strip()
    preferred_name = str(current_scope.get("sprint") or entry.get("sprint") or "").strip()
    selected: dict[str, object] = {}
    for membership in membership_rows:
        sprint_id = str(membership.get("id") or "").strip()
        sprint_name = str(membership.get("name") or "").strip()
        if preferred_id and sprint_id == preferred_id:
            selected = membership
        elif not selected and preferred_name and sprint_name.casefold() == preferred_name.casefold():
            selected = membership
        if sprint_id:
            store.upsert_sprint_membership(
                jira_key=jira_key,
                sprint_id=sprint_id,
                sprint_name=sprint_name,
                sprint_state=str(membership.get("state") or "unknown"),
                joined_at=str(membership.get("joined_at") or ""),
                source=membership,
                summary=summary,
                responsible=responsible,
            )
    sprint_id = str(selected.get("id") or preferred_id or "legacy")
    sprint_name = str(selected.get("name") or preferred_name or ("Legacy / Unknown Sprint" if sprint_id == "legacy" else ""))
    sprint_state = str(selected.get("state") or current_scope.get("sprint_state") or metadata.get("jira_current_sprint_state") or "unknown")
    review_mode = str(metadata.get("workflow_review_mode") or current_scope.get("review_mode") or "issue")
    if review_mode not in {"issue", "batch-preview", "final-sprint"}:
        review_mode = "issue"
    related_mrs = metadata.get("related_merge_requests")
    mr_scope = [item for item in related_mrs if isinstance(item, dict)] if isinstance(related_mrs, list) else []
    for existing_cycle in store.list_cycles(jira_key):
        if str(existing_cycle.get("sprint_id") or "") != sprint_id or existing_cycle.get("cycle_closed_at"):
            continue
        existing_scope = existing_cycle.get("mr_scope_json") or existing_cycle.get("mr_scope") or []
        if isinstance(existing_scope, list):
            by_revision: dict[str, dict[str, object]] = {}
            for item in [*existing_scope, *mr_scope]:
                if not isinstance(item, dict):
                    continue
                identity = "|".join(
                    [
                        str(item.get("project_path") or item.get("project") or "").casefold(),
                        str(item.get("mr_id") or item.get("iid") or ""),
                        str(item.get("head_sha") or item.get("commit") or "").casefold(),
                    ]
                )
                by_revision[identity] = item
            mr_scope = list(by_revision.values())
        break
    cycle = store.upsert_review_cycle(
        jira_key=jira_key,
        sprint_id=sprint_id,
        sprint_name=sprint_name,
        sprint_state=sprint_state,
        review_mode=review_mode,
        cycle_started_at=str(entry.get("reviewed_at") or ""),
        mr_scope=mr_scope,
        backfilled=sprint_id == "legacy",
        summary=summary,
        responsible=responsible,
    )
    return str(cycle.get("cycle_id") or ""), sprint_id


def _sync_workflow_history(limit: int = 10000) -> None:
    entries = sorted(load_review_history(limit=limit), key=lambda item: str(item.get("reviewed_at") or ""))
    store = workflow_store()
    registered = store.registered_run_fingerprints()
    for entry in entries:
        report_path = Path(str(entry.get("report_path") or ""))
        if not report_path.is_file():
            continue
        jira_key = str(entry.get("jira_key") or "").strip().upper()
        if not jira_key:
            match = re.search(r"\b[A-Z][A-Z0-9]+-\d+\b", report_path.name.upper())
            jira_key = match.group(0) if match else ""
        if not jira_key:
            continue
        fingerprint = report_fingerprint(str(report_path))
        if (jira_key, fingerprint) in registered:
            continue
        metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
        summary = str(metadata.get("jira_summary") or "")
        responsible_values = scope_people(
            metadata.get("responsible_scope")
            or metadata.get("responsible")
            or metadata.get("web_report_owner")
            or report_path.parent.name
        )
        responsible = "+".join(sorted(responsible_values, key=str.casefold))
        findings = _extract_report_findings(report_path.read_text(encoding="utf-8", errors="ignore"))
        cycle_id, sprint_id = _workflow_cycle_from_history_entry(
            store, entry, metadata, jira_key, summary, responsible
        )
        run_group_id = str(metadata.get("run_group_id") or "")
        related_mrs = metadata.get("related_merge_requests")
        related_scopes = {
            _review_scope_from_discovery(item)
            for item in related_mrs
            if isinstance(item, dict)
        } if isinstance(related_mrs, list) else set()
        metadata_scope = _review_scope_from_discovery(
            {
                **metadata,
                "project_path": metadata.get("git_tools_project_path") or entry.get("project"),
            }
        )
        if not related_scopes:
            related_scopes.add(metadata_scope)
        if len(related_scopes) == 1:
            report_scope = next(iter(related_scopes))
        else:
            report_scope = ReviewScope("Unmapped", "Unmapped release line")
        if run_group_id:
            store.create_run_group(
                cycle_id=cycle_id,
                review_mode="issue",
                status="completed",
                stable_fingerprint=str(metadata.get("review_stable_fingerprint") or ""),
                run_group_id=run_group_id,
                created_at=str(entry.get("reviewed_at") or ""),
            )
        store.register_run(
            jira_key=jira_key,
            report_path=str(report_path),
            findings=findings,
            summary=summary,
            responsible=responsible,
            conclusion=str(entry.get("conclusion") or ""),
            created_at=str(entry.get("reviewed_at") or ""),
            cycle_id=cycle_id,
            run_group_id=run_group_id,
            project_type=str(metadata.get("split_report_project_type") or metadata.get("project_type") or ""),
            application=report_scope.application,
            release_line=report_scope.release_line,
            responsible_scope=metadata.get("responsible_scope") or responsible,
            mr_fingerprint=str(metadata.get("review_fingerprint") or ""),
            stable_fingerprint=str(metadata.get("review_stable_fingerprint") or ""),
        )
        effective_description = str(metadata.get("jira_description") or "").strip()
        if effective_description:
            description_key = hashlib.sha256(effective_description.encode("utf-8")).hexdigest()
            store.create_description_snapshot(
                cycle_id=cycle_id,
                source_type="effective-description",
                source_id="jira-effective-description",
                reason="review-run-captured",
                adf_document=_plain_text_adf(effective_description),
                plain_text=effective_description,
                jira_status=str(metadata.get("jira_status") or ""),
                code_mrs=[item for item in (metadata.get("related_merge_requests") or []) if isinstance(item, dict)],
                deferred_mrs=[item for item in (metadata.get("deferred_release_gate_resources") or []) if isinstance(item, dict)],
                backfilled=True,
                captured_at=str(entry.get("reviewed_at") or ""),
                idempotency_key=f"description:{cycle_id}:{description_key}",
            )
        for resource in metadata.get("deferred_release_gate_resources") or []:
            if not isinstance(resource, dict):
                continue
            project = str(resource.get("project_path") or resource.get("gitlab_project") or "").strip()
            mr_iid = str(resource.get("mr_iid") or resource.get("mr_id") or "").strip()
            head_sha = str(resource.get("head_sha") or resource.get("commit") or "").strip()
            resource_type = str(resource.get("release_gate_role") or resource.get("resource_type") or "").strip().lower().replace("-", "_").replace(" ", "_")
            if not project or not mr_iid or not head_sha or resource_type not in {"company_config", "scr"}:
                continue
            store.upsert_deferred_resource(
                cycle_id=cycle_id,
                jira_key=jira_key,
                sprint_id=sprint_id,
                gitlab_project=project,
                mr_iid=mr_iid,
                head_sha=head_sha,
                resource_type=resource_type,
                mr_url=str(resource.get("mr_url") or ""),
                status="pending",
                evidence=resource,
                idempotency_key=f"deferred:{cycle_id}:{project}:{mr_iid}:{head_sha}",
            )
        if not findings:
            store.create_review_snapshot(
                cycle_id=cycle_id,
                reason="review-completed-no-findings",
                actor="system",
                idempotency_key=f"clean-run:{cycle_id}:{fingerprint}",
            )
        registered.add((jira_key, fingerprint))
    if WEB_THREADS_DIR.is_dir():
        for thread_path in WEB_THREADS_DIR.glob("*.json"):
            try:
                thread = json.loads(thread_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(thread, dict):
                store.import_legacy_thread(str(thread.get("report") or ""), thread)


def _canonical_people_text(value: str) -> str:
    return "+".join(sorted(dict.fromkeys(_split_people(value)), key=str.lower))


def _display_group_name(value: str) -> str:
    labels = {
        "build-repository": "Build Repository",
        "dps9-repository": "DPS9",
        "dps11-repository": "DPS11",
        "itrade-client": "iTrade Client",
        "wvadmin-repository": "WVAdmin",
    }
    normalized = (value or "").strip()
    return labels.get(normalized, normalized or "Other")


def render_projects_markup(projects: list[dict[str, object]]) -> str:
    grouped: dict[str, list[dict[str, object]]] = {}
    for project in projects:
        group = str(project.get("group_display") or project.get("group") or "Other")
        grouped.setdefault(group, []).append(project)
    parts: list[str] = []
    for group, items in grouped.items():
        project_html = []
        for project in items:
            search_text = " ".join(
                str(project.get(key) or "")
                for key in ("display_name", "group", "module", "repository_url", "responsible")
            ).lower()
            dev_branch = project.get("dev_branch") or []
            dev_text = ""
            if isinstance(dev_branch, list) and dev_branch:
                dev_text = f'<div class="meta">Dev branches: {html.escape(", ".join(str(item) for item in dev_branch))}</div>'
            project_html.append(
                f"""
            <div class="project" data-search="{html.escape(search_text)}">
              <strong>{html.escape(str(project.get("display_name") or ""))}</strong>
              <div class="meta">{html.escape(str(project.get("group") or ""))} / {html.escape(str(project.get("module") or ""))}</div>
              <div class="meta">{html.escape(str(project.get("repository_url") or project.get("project_path") or ""))}</div>
              <div class="meta">Responsible: {html.escape(str(project.get("responsible") or "-"))}</div>
              {dev_text}
            </div>"""
            )
        parts.append(
            f"""
        <div class="project-group" data-group="{html.escape(group)}">
          <div class="project-group-title">
            <span>{html.escape(group)}</span>
            <span class="count-pill">{len(items)}</span>
          </div>
          {''.join(project_html)}
        </div>"""
        )
    return "".join(parts) or "No project config found."


def _filter_history_for_user(history: list[dict[str, object]], user: str) -> list[dict[str, object]]:
    if _is_admin_user(user):
        return history
    filtered: list[dict[str, object]] = []
    for item in history:
        report_path = Path(str(item.get("report_path") or ""))
        if _history_report_belongs_to_user(report_path, user):
            filtered.append(item)
    return filtered


def _web_history_item(item: dict[str, object]) -> dict[str, object]:
    counts = item.get("severity_counts")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return {
        "reviewed_at": item.get("reviewed_at") or "",
        "report_path": item.get("report_path") or "",
        "project": item.get("project") or "",
        "mr_url": item.get("mr_url") or "",
        "mr_id": item.get("mr_id") or "",
        "jira_key": item.get("jira_key") or "",
        "sprint": item.get("sprint") or "",
        "conclusion": item.get("conclusion") or "",
        "finding_count": item.get("finding_count") or 0,
        "severity_counts": counts if isinstance(counts, dict) else {},
        "application": metadata.get("application") or "",
        "release_line": metadata.get("release_line") or "",
        "scope_label": review_scope_label(
            str(metadata.get("application") or ""),
            str(metadata.get("release_line") or ""),
        ),
        "responsible_scope": sorted(
            scope_people(metadata.get("responsible_scope") or metadata.get("responsible")),
            key=str.casefold,
        ),
    }


def _history_report_belongs_to_user(report_path: Path, user: str) -> bool:
    if not user or not report_path:
        return False
    try:
        target = report_path.expanduser().resolve()
        for base in _report_history_directories():
            if base == target or base in target.parents:
                return _can_access_report(base, target, user)
        if target.is_file():
            metadata = _read_report_metadata(target)
            report_people = {
                item.casefold()
                for item in scope_people(metadata.get("responsible_scope") or metadata.get("responsible"))
            }
            allowed = {item.casefold() for item in _web_user_responsibles(user)}
            if report_people & allowed:
                return True
    except Exception:
        pass
    return user.lower() in {part.lower() for part in report_path.parts}


def _safe_child_path(base: Path, relative: str) -> Path:
    base_resolved = base.expanduser().resolve()
    relative_path = Path(relative.replace("\\", "/"))
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise FileNotFoundError("Invalid report path")
    target = (base_resolved / relative_path).resolve()
    if target != base_resolved and base_resolved not in target.parents:
        raise FileNotFoundError("Invalid report path")
    return target


def _zip_reports(base: Path, markdown_files: list[Path]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in markdown_files:
            archive.write(path, path.relative_to(base).as_posix())
    return buffer.getvalue()


def _download_name(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._+-" else "_" for char in value)
    return safe.strip("._") or "download"


@contextmanager
def _exclusive_web_users_file_lock():
    lock_path = WEB_USERS_FILE.parent / f".{WEB_USERS_FILE.name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0)
        if handle.tell() == 0 and lock_path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def ensure_web_users() -> dict[str, dict[str, object]]:
    with WEB_USERS_LOCK:
        ensure_directories()
        WEB_USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_web_users_file_lock():
            store_exists = WEB_USERS_FILE.exists()
            users = _load_web_users()
            changed = False
            if not store_exists:
                bootstrap_password = os.getenv("WEB_BOOTSTRAP_ADMIN_PASSWORD", "")
                if not bootstrap_password or len(bootstrap_password) < 14 or not _strong_password(bootstrap_password):
                    raise RuntimeError(
                        "WEB_BOOTSTRAP_ADMIN_PASSWORD (14+ characters with upper/lower case, number, and symbol) "
                        "is required to initialize a new user store."
                    )
                bootstrap_username = os.getenv("WEB_BOOTSTRAP_ADMIN_USERNAME", "admin").strip() or "admin"
                users[bootstrap_username] = {
                    "username": bootstrap_username,
                    "password_hash": hash_web_password(bootstrap_password),
                    "role": "manager",
                    "active": True,
                    "must_change_password": True,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                }
                changed = True
            expected = _responsible_usernames()
            if os.getenv("WEB_AUTH_PRUNE_USERS", "1").strip().lower() not in {"0", "false", "no", "off"}:
                for username in list(users):
                    explicit_role = str(users[username].get("role") or "").strip().lower()
                    if username not in expected and explicit_role not in WEB_ROLES:
                        users.pop(username, None)
                        changed = True
            for username in sorted(expected):
                if username not in users:
                    continue
                configured_role = str((_configured_web_user_profiles().get(username) or {}).get("role") or "").strip().lower()
                assigned_role = configured_role if configured_role in WEB_ROLES else _default_web_user_role(username)
                if not str(users[username].get("role") or "").strip():
                    users[username]["role"] = assigned_role
                    changed = True
            for record in users.values():
                legacy_password = str(record.get("password") or "")
                if legacy_password and not record.get("password_hash"):
                    record["password_hash"] = hash_web_password(legacy_password)
                    record.pop("password", None)
                    changed = True
                if "active" not in record:
                    record["active"] = True
                    changed = True
            if changed:
                _write_web_users(users)
            return users


def _load_web_users() -> dict[str, dict[str, object]]:
    if not WEB_USERS_FILE.exists():
        return {}
    try:
        payload = json.loads(WEB_USERS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"User store is not valid JSON: {WEB_USERS_FILE}") from exc
    users = payload.get("users") if isinstance(payload, dict) else {}
    return users if isinstance(users, dict) else {}


def _write_web_users(users: dict[str, dict[str, object]]) -> None:
    WEB_USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = WEB_USERS_FILE.parent / f".{WEB_USERS_FILE.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"users": users}, ensure_ascii=False, indent=2))
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, WEB_USERS_FILE)
    if os.name != "nt":
        directory_fd = os.open(WEB_USERS_FILE.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _append_web_user_audit(actor: str, action: str, target: str, changes: dict[str, object]) -> None:
    WEB_USER_AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "actor": actor,
        "action": action,
        "target": target,
        "changes": changes,
        "event_id": secrets.token_hex(10),
    }
    with WEB_USER_AUDIT_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _public_web_user(username: str, record: dict[str, object]) -> dict[str, object]:
    configured = record.get("responsible") or record.get("responsibles")
    responsibles = (
        [str(item).strip() for item in configured if str(item).strip()]
        if isinstance(configured, list)
        else _split_people(str(configured or ""))
    )
    role = str(record.get("role") or _default_web_user_role(username)).lower()
    return {
        "username": username,
        "role": role,
        "active": record.get("active") is not False,
        "responsibles": responsibles or ([] if role == "manager" else [username]),
        "must_change_password": bool(record.get("must_change_password")),
        "created_at": str(record.get("created_at") or ""),
        "updated_at": str(record.get("updated_at") or record.get("password_changed_at") or record.get("created_at") or ""),
        "protected": username.lower() == "root",
        "revision": int(record.get("revision") or 1),
    }


def list_managed_web_users() -> list[dict[str, object]]:
    users = ensure_web_users()
    return [
        _public_web_user(username, record)
        for username, record in sorted(users.items(), key=lambda item: item[0].casefold())
        if isinstance(record, dict)
    ]


def managed_responsible_options() -> list[str]:
    values = set(_responsible_usernames())
    for profile in _configured_web_user_profiles().values():
        configured = profile.get("responsible") or profile.get("responsibles")
        if isinstance(configured, list):
            values.update(str(item).strip() for item in configured if str(item).strip())
        else:
            values.update(_split_people(str(configured or "")))
    for record in _load_web_users().values():
        configured = record.get("responsible") or record.get("responsibles")
        if isinstance(configured, list):
            values.update(str(item).strip() for item in configured if str(item).strip())
        else:
            values.update(_split_people(str(configured or "")))
    return sorted((item for item in values if item), key=str.casefold)


def _normalize_managed_responsibles(value: object) -> list[str]:
    raw = value if isinstance(value, list) else _split_people(str(value or ""))
    result = list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))
    if len(result) > 50:
        raise ValueError("Responsible scope supports at most 50 entries.")
    for item in result:
        if len(item) > 80 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", item):
            raise ValueError(f"Invalid responsible identifier: {item}")
    return result


def _revoke_web_user_sessions(username: str) -> None:
    requested = username.casefold()
    with WEB_SESSIONS_LOCK:
        for token, session in list(WEB_SESSIONS.items()):
            if str(session.get("username") or "").casefold() == requested:
                WEB_SESSIONS.pop(token, None)


def save_managed_web_user(actor: str, payload: dict[str, object]) -> dict[str, object]:
    if _web_user_role(actor) != "manager":
        raise PermissionError("User management is only available to Manager users.")
    requested = str(payload.get("username") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,63}", requested):
        raise ValueError("Username must contain 3–64 letters, numbers, dots, underscores, or hyphens.")
    role = str(payload.get("role") or "").strip().lower()
    if role not in WEB_ROLES:
        raise ValueError("Role must be Developer, Auditor, or Manager.")
    active_value = payload.get("active", True)
    active = active_value if isinstance(active_value, bool) else str(active_value).strip().lower() not in {"0", "false", "no", "off"}
    responsibles = _normalize_managed_responsibles(payload.get("responsibles"))
    now = datetime.now().isoformat(timespec="seconds")
    with WEB_USERS_LOCK:
        ensure_web_users()
        with _exclusive_web_users_file_lock():
            users = _load_web_users()
            actor_name, actor_record = _find_web_user(users, actor)
            if (
                not actor_record
                or actor_record.get("active") is False
                or str(actor_record.get("role") or "").lower() != "manager"
            ):
                raise PermissionError("User management is only available to active Manager users.")
            canonical, record = _find_web_user(users, requested)
            creating = record is None
            username = requested if creating else canonical
            if creating and username.casefold() in {"admin", "root"}:
                raise ValueError("The admin and root usernames are reserved.")
            original_users = json.loads(json.dumps(users))
            previous = dict(record or {})
            if username.casefold() == "root" and (role != "manager" or not active):
                raise ValueError("The protected root account must remain an active Manager.")
            if username.casefold() == actor_name.casefold() and (role != "manager" or not active):
                raise ValueError("You cannot demote or deactivate your own Manager account.")
            current_revision = int(previous.get("revision") or 1)
            if not creating:
                expected_revision = int(payload.get("revision") or 0)
                if expected_revision <= 0 or expected_revision != current_revision:
                    raise ValueError("This user was updated by another session. Refresh and try again.")
            if role != "manager" and not responsibles:
                raise ValueError("Developer and Auditor accounts require at least one Responsible mapping.")
            temporary_password = ""
            if creating:
                temporary_password = generate_strong_password(14)
                record = {
                    "username": username,
                    "password_hash": hash_web_password(temporary_password),
                    "must_change_password": True,
                    "created_at": now,
                    "created_by": actor_name,
                }
                users[username] = record
            record["role"] = role
            record["active"] = active
            record["responsible"] = [] if role == "manager" else responsibles
            record["updated_at"] = now
            record["updated_by"] = actor_name
            if active:
                record.pop("disabled_at", None)
                record.pop("disabled_by", None)
            elif previous.get("active", True) is not False:
                record["disabled_at"] = now
                record["disabled_by"] = actor_name
            record["revision"] = 1 if creating else current_revision + 1
            active_managers = [
                key for key, item in users.items()
                if item.get("active") is not False and str(item.get("role") or "").lower() == "manager"
            ]
            if not active_managers:
                raise ValueError("At least one active Manager account is required.")
            changes = {
                "created": creating,
                "role": {"from": previous.get("role"), "to": role},
                "active": {"from": previous.get("active", True), "to": active},
                "responsibles": {"from": previous.get("responsible") or previous.get("responsibles") or [], "to": record["responsible"]},
            }
            _write_web_users(users)
            try:
                _append_web_user_audit(actor_name, "create" if creating else "update", username, changes)
            except Exception:
                _write_web_users(original_users)
                raise
            if not active or (not creating and str(previous.get("role") or "").lower() != role):
                _revoke_web_user_sessions(username)
            return {"user": _public_web_user(username, record), "temporary_password": temporary_password}


def reset_managed_web_user_password(actor: str, username: str) -> dict[str, object]:
    if _web_user_role(actor) != "manager":
        raise PermissionError("User management is only available to Manager users.")
    with WEB_USERS_LOCK:
        ensure_web_users()
        with _exclusive_web_users_file_lock():
            users = _load_web_users()
            actor_name, actor_record = _find_web_user(users, actor)
            if (
                not actor_record
                or actor_record.get("active") is False
                or str(actor_record.get("role") or "").lower() != "manager"
            ):
                raise PermissionError("User management is only available to active Manager users.")
            canonical, record = _find_web_user(users, username)
            if not record:
                raise ValueError("User was not found.")
            if canonical.casefold() == "root" and actor_name.casefold() != "root":
                raise PermissionError("Only the protected root account can reset its own password.")
            original_users = json.loads(json.dumps(users))
            temporary_password = generate_strong_password(14)
            record["password_hash"] = hash_web_password(temporary_password)
            record.pop("password", None)
            record["must_change_password"] = True
            record["password_reset_at"] = datetime.now().isoformat(timespec="seconds")
            record["updated_at"] = record["password_reset_at"]
            record["updated_by"] = actor_name
            record["revision"] = int(record.get("revision") or 1) + 1
            _write_web_users(users)
            try:
                _append_web_user_audit(actor_name, "reset-password", canonical, {"sessions_revoked": True})
            except Exception:
                _write_web_users(original_users)
                raise
            _revoke_web_user_sessions(canonical)
            return {"user": _public_web_user(canonical, record), "temporary_password": temporary_password}


def _responsible_usernames() -> set[str]:
    names: set[str] = set()
    for entry in git_tools_project_entries():
        for item in re.split(r"[+,;]+", entry.responsible or ""):
            value = item.strip()
            if value:
                names.add(value)
    for directory in (report_output_dir(),):
        if not directory.exists():
            continue
        for path in directory.iterdir():
            if path.is_dir() and not path.name.startswith(".") and ("." in path.name or "+" in path.name):
                names.add(path.name)
    return names


def generate_strong_password(length: int = 12) -> str:
    if length < 8:
        raise ValueError("Password length must be at least 8.")
    groups = [
        "ABCDEFGHJKLMNPQRSTUVWXYZ",
        "abcdefghijkmnopqrstuvwxyz",
        "23456789",
        PASSWORD_SYMBOLS,
    ]
    chars = [secrets.choice(group) for group in groups]
    alphabet = "".join(groups)
    chars.extend(secrets.choice(alphabet) for _ in range(length - len(chars)))
    secrets.SystemRandom().shuffle(chars)
    password = "".join(chars)
    return password if _strong_password(password) else generate_strong_password(length)


def _strong_password(password: str) -> bool:
    return (
        len(password) >= 8
        and any(char.isupper() for char in password)
        and any(char.islower() for char in password)
        and any(char.isdigit() for char in password)
        and any(char in PASSWORD_SYMBOLS for char in password)
    )


def hash_web_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_HASH_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_HASH_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_web_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        rounds = int(iterations)
        if rounds < 100_000 or rounds > 1_000_000:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), rounds)
        return hmac.compare_digest(digest.hex(), expected)
    except (TypeError, ValueError):
        return False


def new_robot_challenge() -> dict[str, str]:
    _purge_challenges()
    left = secrets.randbelow(8) + 2
    right = secrets.randbelow(8) + 2
    challenge_id = secrets.token_urlsafe(18)
    ROBOT_CHALLENGES[challenge_id] = {
        "answer": str(left + right),
        "expires_at": int(time.time()) + CHALLENGE_TTL_SECONDS,
    }
    return {"id": challenge_id, "question": f"{left} + {right} = ?"}


def authenticate_web_user(username: str, password: str, challenge_id: str, robot_answer: str) -> tuple[bool, str, str]:
    users = ensure_web_users()
    challenge = ROBOT_CHALLENGES.get(challenge_id)
    if not challenge:
        return False, "Robot verification expired. Refresh the login page.", ""
    if int(time.time()) > int(challenge.get("expires_at", 0)):
        ROBOT_CHALLENGES.pop(challenge_id, None)
        return False, "Robot verification expired. Refresh the login page.", ""
    if not hmac.compare_digest(str(challenge.get("answer") or ""), robot_answer):
        ROBOT_CHALLENGES.pop(challenge_id, None)
        return False, "Robot verification failed.", ""
    ROBOT_CHALLENGES.pop(challenge_id, None)
    canonical_username, user = _find_web_user(users, username)
    if not user:
        return False, "Invalid username or password.", ""
    if user.get("active") is False:
        return False, "Invalid username or password.", ""
    encoded = str(user.get("password_hash") or "")
    legacy = str(user.get("password") or "")
    password_matches = verify_web_password(password, encoded) if encoded else hmac.compare_digest(legacy, password)
    if not password_matches:
        return False, "Invalid username or password.", ""
    return True, "", canonical_username


def _find_web_user(users: dict[str, dict[str, object]], username: str) -> tuple[str, dict[str, object] | None]:
    requested = (username or "").strip()
    if not requested:
        return "", None
    direct = users.get(requested)
    if direct:
        return str(direct.get("username") or requested), direct
    requested_lower = requested.lower()
    for key, user in users.items():
        if key.lower() == requested_lower or str(user.get("username") or "").lower() == requested_lower:
            return str(user.get("username") or key), user
    return "", None


def _create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    with WEB_SESSIONS_LOCK:
        WEB_SESSIONS[token] = {
            "username": username,
            "expires_at": int(time.time()) + SESSION_TTL_SECONDS,
        }
    return token


def _cookie_value(header: str, name: str) -> str:
    for part in (header or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.strip().split("=", 1)
        if key == name:
            return value
    return ""


def _purge_challenges() -> None:
    now = int(time.time())
    for key in list(ROBOT_CHALLENGES):
        if int(ROBOT_CHALLENGES[key].get("expires_at", 0)) < now:
            ROBOT_CHALLENGES.pop(key, None)


def _text(value: object) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def _int_query(query: dict[str, list[str]], name: str, default: int) -> int:
    try:
        return int((query.get(name) or [str(default)])[0] or str(default))
    except (TypeError, ValueError):
        return default


def app_version() -> str:
    return os.getenv("CODE_REVIEW_APP_VERSION", __version__).strip() or __version__


def web_health_snapshot(*, details: bool = False) -> dict[str, object]:
    """Return a sanitized service-health snapshot suitable for login and home."""
    ensure_directories()
    config_path = git_tools_config_path().expanduser()
    checks: list[dict[str, object]] = []

    def add_check(name: str, ok: bool, message: str, *, required: bool = True) -> None:
        checks.append({"name": name, "ok": bool(ok), "message": message, "required": required})

    config_ok = config_path.is_file() and os.access(config_path, os.R_OK)
    add_check("Configuration", config_ok, "Configuration is readable." if config_ok else "Configuration cannot be read.")
    data_ok = DATA_DIR.is_dir() and os.access(DATA_DIR, os.R_OK | os.W_OK)
    add_check("Application data", data_ok, "Application data storage is available." if data_ok else "Application data storage is unavailable.")
    reports = report_output_dir().expanduser()
    try:
        reports.mkdir(parents=True, exist_ok=True)
        reports_ok = reports.is_dir() and os.access(reports, os.R_OK | os.W_OK)
    except OSError:
        reports_ok = False
    add_check("Report storage", reports_ok, "Report storage is available." if reports_ok else "Report storage is unavailable.")
    try:
        with workflow_store().connect() as database:
            database.execute("SELECT 1").fetchone()
        workflow_ok = True
    except Exception:
        workflow_ok = False
    add_check("Review workflow", workflow_ok, "Workflow database is ready." if workflow_ok else "Workflow database check failed.")

    network: dict[str, object] = {}
    if details:
        try:
            network = check_network_dict()
            gitlab_ok = bool(network.get("gitlab_port_open"))
            codex_ok = bool(network.get("codex_available"))
            add_check("GitLab", gitlab_ok, str(network.get("gitlab_hint") or "GitLab connectivity check completed."), required=False)
            add_check("Review provider", codex_ok, str(network.get("codex_hint") or "Review provider check completed."), required=False)
        except Exception:
            add_check("External services", False, "External service checks could not be completed.", required=False)

    required_ok = all(bool(item["ok"]) for item in checks if item["required"])
    optional_failures = any(not bool(item["ok"]) for item in checks if not item["required"])
    status = "healthy" if required_ok and not optional_failures else ("degraded" if required_ok else "unhealthy")
    return {
        "ok": required_ok,
        "status": status,
        "version": app_version(),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "checks": checks,
    }


_CONFIG_SENSITIVE_KEY = re.compile(r"(?:password|secret|api[_-]?key|private[_-]?key|credential|token)", re.I)
_PROJECT_EDITABLE_FIELDS = {
    "repository_url", "responsible", "project_name", "llm_model",
    "branch", "branches", "dev_branch", "type", "project_type", "application",
    "release_line", "release_lines",
}


def _safe_config_leaf(path: list[str], value: object) -> bool:
    if not path or any(_CONFIG_SENSITIVE_KEY.search(part) for part in path):
        return False
    if path[:3] == ["app", "web", "users"]:
        return False
    if isinstance(value, (str, int, float, bool)) or value is None:
        return True
    return isinstance(value, list) and all(isinstance(item, (str, int, float, bool)) or item is None for item in value)


def _configuration_app_fields(payload: dict[str, object]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    app = payload.get("app")
    if not isinstance(app, dict):
        return result

    def display_name(value: str) -> str:
        words = value.replace("_", " ").split()
        return " ".join("LLM" if word.lower() == "llm" else word.title() for word in words)

    def visit(value: object, path: list[str]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, [*path, str(key)])
            return
        if not _safe_config_leaf(path, value):
            return
        field_type = "boolean" if isinstance(value, bool) else "number" if isinstance(value, (int, float)) else "list" if isinstance(value, list) else "text"
        result.append(
            {
                "path": path,
                "key": ".".join(path[1:]),
                "category": display_name(path[1]) if len(path) > 1 else "Application",
                "label": display_name(path[-1]),
                "type": field_type,
                "value": value,
            }
        )

    visit(app, ["app"])
    return result


def _configuration_projects(payload: dict[str, object]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []

    def visit(group: str, value: object, path: list[str], inherited: dict[str, object]) -> None:
        if not isinstance(value, dict):
            return
        current = dict(inherited)
        for key in _PROJECT_EDITABLE_FIELDS:
            if key in value and value.get(key) not in (None, "", []):
                current[key] = value.get(key)
        if value.get("repository_url"):
            editable = {
                key: value.get(key, current.get(key, ""))
                for key in _PROJECT_EDITABLE_FIELDS
                if key in value or key in current
            }
            editable.setdefault(
                "application",
                _review_application_from_discovery(
                    {
                        "git_tools_group": group,
                        "git_tools_module": path[-1] if len(path) > 1 else group,
                        "project_name": editable.get("project_name"),
                        "project_path": editable.get("repository_url"),
                    }
                ),
            )
            result.append(
                {
                    "id": "/".join(path),
                    "group": group,
                    "module": path[-1] if len(path) > 1 else group,
                    "path": path,
                    "values": editable,
                }
            )
        for key, child in value.items():
            if isinstance(child, dict) and key != "working_copies":
                visit(group, child, [*path, str(key)], current)

    for group, value in payload.items():
        if group == "app" or not isinstance(value, dict):
            continue
        visit(str(group), value, [str(group)], {})
    return result


def web_configuration_payload() -> dict[str, object]:
    base = load_base_config_payload()
    effective = load_effective_config_payload()
    store = EffectiveConfigStore()
    return {
        "ok": True,
        "revision": store.revision(base),
        "app_fields": _configuration_app_fields(effective),
        "projects": _configuration_projects(effective),
        "backups": store.list_backups(),
        "storage": "Web override",
    }


def _nested_value(payload: dict[str, object], path: list[str]) -> object:
    value: object = payload
    for part in path:
        if not isinstance(value, dict) or part not in value:
            raise ValueError("Configuration field no longer exists. Refresh and try again.")
        value = value[part]
    return value


def _set_nested_override(overrides: dict[str, object], path: list[str], value: object) -> None:
    target = overrides
    for part in path[:-1]:
        child = target.get(part)
        if not isinstance(child, dict) or child.get("$delete") is True:
            child = {}
            target[part] = child
        target = child
    target[path[-1]] = value


def save_web_configuration(actor: str, payload: dict[str, object]) -> dict[str, object]:
    record = _web_user_record(actor)
    if _web_user_role(actor) != "manager" or record.get("active") is False:
        raise PermissionError("Configuration is only available to active Manager users.")
    raw_path = payload.get("path")
    path = [str(item).strip() for item in raw_path] if isinstance(raw_path, list) else []
    if not path or any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", item) for item in path):
        raise ValueError("A valid configuration field path is required.")
    effective = load_effective_config_payload()
    try:
        current = _nested_value(effective, path)
    except ValueError:
        if path[0] != "app" and path[-1] in _PROJECT_EDITABLE_FIELDS:
            current = ""
        else:
            raise
    value = payload.get("value")
    if path[0] == "app":
        if not _safe_config_leaf(path, current):
            raise PermissionError("This application setting is not editable in Web.")
    else:
        if path[-1] not in _PROJECT_EDITABLE_FIELDS:
            raise PermissionError("This GitLab project field is not editable in Web.")
    if isinstance(current, bool) and not isinstance(value, bool):
        raise ValueError("This field requires a true or false value.")
    if isinstance(current, (int, float)) and not isinstance(current, bool) and (
        not isinstance(value, (int, float)) or isinstance(value, bool)
    ):
        raise ValueError("This field requires a numeric value.")
    if isinstance(current, list) and (
        not isinstance(value, list) or len(value) > 100 or not all(isinstance(item, (str, int, float, bool)) for item in value)
    ):
        raise ValueError("This field requires a list with no more than 100 simple values.")
    if isinstance(value, str) and len(value) > 4000:
        raise ValueError("Configuration values cannot exceed 4000 characters.")
    if path[-1] == "repository_url":
        parsed = urlparse(str(value or ""))
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Repository URL must be a credential-free HTTPS URL.")
    if path[-1] == "application" and value not in REVIEW_APPLICATION_ORDER:
        raise ValueError("Application must be WVAdmin, iTrade Client, Services Terminal, DPS, or Unmapped.")
    overrides = load_web_config_overrides()
    _set_nested_override(overrides, path, value)
    store = EffectiveConfigStore()
    result = store.save_overrides(
        load_base_config_payload(),
        overrides,
        actor=actor,
        expected_revision=str(payload.get("revision") or ""),
        request_id=str(payload.get("request_id") or ""),
    )
    clear_config_cache()
    return {
        "ok": True,
        "change": {key: result.get(key) for key in ("revision", "previous_revision", "backup", "changed_paths")},
        **web_configuration_payload(),
    }


def restore_web_configuration(actor: str, payload: dict[str, object]) -> dict[str, object]:
    record = _web_user_record(actor)
    if _web_user_role(actor) != "manager" or record.get("active") is False:
        raise PermissionError("Configuration is only available to active Manager users.")
    store = EffectiveConfigStore()
    result = store.restore_backup(
        load_base_config_payload(),
        str(payload.get("backup") or ""),
        actor=actor,
        expected_revision=str(payload.get("revision") or ""),
        request_id=str(payload.get("request_id") or ""),
    )
    clear_config_cache()
    return {
        "ok": True,
        "change": {key: result.get(key) for key in ("revision", "previous_revision", "backup", "changed_paths")},
        **web_configuration_payload(),
    }


def mutate_web_configuration_project(actor: str, payload: dict[str, object]) -> dict[str, object]:
    record = _web_user_record(actor)
    if _web_user_role(actor) != "manager" or record.get("active") is False:
        raise PermissionError("Configuration is only available to active Manager users.")
    action = str(payload.get("action") or "").strip().lower()
    raw_path = payload.get("path")
    path = [str(item).strip() for item in raw_path] if isinstance(raw_path, list) else []
    if len(path) < 2 or path[0] == "app" or any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", item) for item in path):
        raise ValueError("A valid GitLab project group and module path is required.")
    effective = load_effective_config_payload()
    if path[0] not in effective or not isinstance(effective.get(path[0]), dict):
        raise ValueError("Select an existing GitLab project group.")
    overrides = load_web_config_overrides()
    if action == "delete":
        _nested_value(effective, path)
        _set_nested_override(overrides, path, {"$delete": True})
    elif action == "upsert":
        values = payload.get("values")
        if not isinstance(values, dict):
            raise ValueError("GitLab project values are required.")
        project = {str(key): value for key, value in values.items() if str(key) in _PROJECT_EDITABLE_FIELDS}
        repository_url = str(project.get("repository_url") or "").strip()
        parsed = urlparse(repository_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Repository URL must be a credential-free HTTPS URL.")
        application = str(project.get("application") or "")
        if application not in REVIEW_APPLICATION_ORDER:
            raise ValueError("Select a valid release application.")
        project_type = str(project.get("type") or project.get("project_type") or "").strip().lower()
        if project_type and project_type not in {"frontend", "backend", "build"}:
            raise ValueError("Project type must be frontend, backend, or build.")
        responsible = project.get("responsible", "")
        if isinstance(responsible, list):
            people = [str(item).strip() for item in responsible if str(item).strip()]
            if len(people) > 50:
                raise ValueError("Responsible scope cannot exceed 50 identifiers.")
            project["responsible"] = people
        elif len(str(responsible or "")) > 1000:
            raise ValueError("Responsible value is too long.")
        _set_nested_override(overrides, path, project)
    else:
        raise ValueError("Project action must be upsert or delete.")
    store = EffectiveConfigStore()
    result = store.save_overrides(
        load_base_config_payload(),
        overrides,
        actor=actor,
        expected_revision=str(payload.get("revision") or ""),
        request_id=str(payload.get("request_id") or ""),
    )
    clear_config_cache()
    return {
        "ok": True,
        "change": {key: result.get(key) for key in ("revision", "previous_revision", "backup", "changed_paths")},
        **web_configuration_payload(),
    }


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    ensure_directories()
    load_projects()
    users = ensure_web_users()
    server = ThreadingHTTPServer((host, port), CodeReviewerHandler)
    print(f"CodeReviewer web app: http://{host}:{port}")
    if host in {"0.0.0.0", "::"}:
        for address in _lan_addresses():
            print(f"LAN URL: http://{address}:{port}")
        print("If other computers cannot connect, allow this port in Windows Firewall.")
    elif host in {"127.0.0.1", "localhost"}:
        print("Local-only mode. Use --lan or --host 0.0.0.0 for other computers.")
    print(f"Workspace: {ROOT_DIR}")
    print(f"Web users: {WEB_USERS_FILE} ({len(users)} user(s))")
    print(f"Web IP whitelist: {', '.join(web_ip_whitelist_entries()) or '<empty>'}")
    server.serve_forever()


def _lan_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        hostname = socket.gethostname()
        for address in socket.gethostbyname_ex(hostname)[2]:
            if address and not address.startswith("127."):
                addresses.add(address)
    except OSError:
        pass
    return sorted(addresses) or ["<this-computer-ip>"]


def render_ip_forbidden(client_ip: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CodeReviewer Access Denied</title>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: #f6f7f9;
      color: #1d232b;
      font-family: Arial, "Microsoft YaHei", sans-serif;
    }}
    main {{
      width: min(520px, calc(100vw - 32px));
      border: 1px solid #d9dee7;
      border-radius: 8px;
      background: #fff;
      padding: 24px;
    }}
    h1 {{ margin: 0 0 12px; font-size: 22px; }}
    p {{ line-height: 1.55; }}
    code {{ background: #eef2f7; padding: 2px 5px; border-radius: 4px; }}
    .help {{
      margin-top: 18px;
      padding-top: 16px;
      border-top: 1px solid #e5e9f0;
      color: #53606f;
    }}
  </style>
</head>
<body>
  <main>
    <h1>Access denied</h1>
    <p>Your client IP is not in the CodeReviewer Web whitelist.</p>
    <p>Client IP: <code>{html.escape(client_ip)}</code></p>
    <p class="help">Please contact the CodeReviewer administrator to request access.</p>
  </main>
</body>
</html>"""


def render_login() -> str:
    challenge = new_robot_challenge()
    version = app_version()
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CodeReviewer Login</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #071a33;
      --panel: rgba(255, 255, 255, .96);
      --text: #10243e;
      --muted: #5b6f87;
      --line: #cddcf0;
      --accent: #0b6bcb;
      --accent-strong: #0754a3;
      --accent-soft: #eaf4ff;
      --danger: #b42318;
      --ok: #137333;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #06152a;
        --panel: rgba(9, 28, 52, .95);
        --text: #edf6ff;
        --muted: #a9bfd8;
        --line: #29486d;
        --accent: #5aa7ff;
        --accent-strong: #8fc4ff;
        --accent-soft: #102f53;
        --danger: #ff8a7a;
        --ok: #73d18a;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center start;
      overflow: hidden;
      padding: clamp(24px, 5vw, 80px);
      background-color: var(--bg);
      background-image:
        linear-gradient(90deg, rgba(3, 17, 36, .12) 0%, rgba(3, 17, 36, .04) 48%, rgba(3, 17, 36, .2) 100%),
        url("/assets/login-code-review-bg.png");
      background-position: center;
      background-size: cover;
      background-repeat: no-repeat;
      color: var(--text);
      font-family: Arial, "Microsoft YaHei", sans-serif;
    }}
    main {{
      position: relative;
      width: min(448px, calc(100vw - 32px));
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 28px;
      box-shadow: 0 28px 70px rgba(2, 13, 29, .34), 0 0 0 1px rgba(255, 255, 255, .18) inset;
      backdrop-filter: blur(18px) saturate(125%);
      -webkit-backdrop-filter: blur(18px) saturate(125%);
    }}
    h1 {{ margin: 2px 0 0; font-size: 27px; letter-spacing: -.02em; }}
    .login-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; margin: 0; }}
    .login-brand {{ display: flex; align-items: center; gap: 12px; min-width: 0; }}
    .brand-mark {{
      width: 44px;
      height: 44px;
      flex: 0 0 auto;
      display: block;
      object-fit: contain;
      filter: drop-shadow(0 8px 14px color-mix(in srgb, var(--accent) 32%, transparent));
    }}
    .login-kicker {{
      color: var(--accent-strong);
      font-size: 10px;
      font-weight: 800;
      letter-spacing: .12em;
      text-transform: uppercase;
    }}
    .login-subtitle {{
      margin: 12px 0 24px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.55;
    }}
    label {{ display: grid; gap: 7px; margin-bottom: 16px; color: var(--muted); font-size: 14px; }}
    .field-label {{
      display: inline-flex;
      align-items: baseline;
      gap: 4px;
      width: fit-content;
      min-width: 0;
      line-height: 1.35;
    }}
    input, button {{ width: 100%; min-height: 44px; border-radius: 8px; font: inherit; }}
    input {{
      border: 1px solid var(--line);
      background: color-mix(in srgb, var(--panel) 86%, var(--accent-soft));
      color: var(--text);
      padding: 10px 12px;
      transition: border-color .16s ease, box-shadow .16s ease, background .16s ease;
    }}
    input:hover {{ border-color: color-mix(in srgb, var(--accent) 42%, var(--line)); }}
    input:focus {{ border-color: var(--accent); outline: 0; box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 16%, transparent); }}
    button {{
      border: 1px solid var(--accent);
      background: linear-gradient(135deg, var(--accent), #167ee5);
      color: white;
      font-weight: 650;
      cursor: pointer;
      transition: transform .16s ease, box-shadow .16s ease, filter .16s ease;
    }}
    button:hover {{ filter: brightness(1.04); box-shadow: 0 8px 18px color-mix(in srgb, var(--accent) 20%, transparent); }}
    button:active {{ transform: translateY(1px); }}
    button:focus-visible {{ outline: 0; box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 20%, transparent); }}
    .password-field {{
      position: relative;
      display: flex;
      align-items: center;
    }}
    .password-field input {{
      padding-right: 76px;
    }}
    .inline-button {{
      width: auto;
      min-height: 30px;
      border-radius: 5px;
      padding: 4px 10px;
      font-size: 12px;
      line-height: 1;
      white-space: nowrap;
    }}
    .password-toggle {{
      position: absolute;
      right: 6px;
      border-color: var(--line);
      background: color-mix(in srgb, var(--panel) 88%, var(--accent-soft));
      color: var(--accent-strong);
      font-weight: 650;
    }}
    .challenge-line {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }}
    .challenge-prompt {{
      display: inline-flex;
      align-items: baseline;
      gap: 4px;
      min-width: 0;
      flex-wrap: wrap;
    }}
    .challenge-line strong {{
      color: var(--text);
    }}
    .challenge-reset {{
      flex: 0 0 auto;
      border-color: var(--line);
      background: color-mix(in srgb, var(--panel) 88%, var(--accent-soft));
      color: var(--accent-strong);
    }}
    .status {{ min-height: 24px; margin-top: 12px; color: var(--muted); }}
    .status.error {{ color: var(--danger); }}
    .required-mark {{ color: var(--danger); font-weight: 700; }}
    [aria-invalid="true"] {{ border-color: var(--danger); box-shadow: 0 0 0 2px color-mix(in srgb, var(--danger) 15%, transparent); }}
    .version-line {{
      margin-top: 16px;
      padding-top: 14px;
      border-top: 1px solid var(--line);
      color: var(--muted);
      font-size: 12px;
      text-align: center;
    }}
    .health-indicator {{
      width: auto;
      min-height: 30px;
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 4px 9px;
      border-color: var(--line);
      border-radius: 999px;
      background: color-mix(in srgb, var(--panel) 88%, var(--accent-soft));
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }}
    .health-indicator:hover, .health-indicator:focus-visible {{ border-color: var(--accent); outline: 0; }}
    .health-dot {{ width: 8px; height: 8px; border-radius: 50%; background: var(--muted); box-shadow: 0 0 0 3px color-mix(in srgb, var(--muted) 14%, transparent); }}
    .health-indicator[data-status="healthy"] {{ color: var(--ok); }}
    .health-indicator[data-status="healthy"] .health-dot {{ background: var(--ok); box-shadow: 0 0 0 3px color-mix(in srgb, var(--ok) 16%, transparent); }}
    .health-indicator[data-status="degraded"], .health-indicator[data-status="unhealthy"] {{ color: var(--danger); }}
    .health-indicator[data-status="degraded"] .health-dot, .health-indicator[data-status="unhealthy"] .health-dot {{ background: var(--danger); }}
    .health-backdrop {{ position: fixed; inset: 0; z-index: 20; display: grid; place-items: center; padding: 16px; background: rgba(17,24,39,.48); }}
    .health-backdrop[hidden] {{ display: none; }}
    .health-dialog {{ width: min(560px, calc(100vw - 32px)); max-height: calc(100vh - 32px); overflow: auto; padding: 20px; border: 1px solid var(--line); border-radius: 10px; background: var(--panel); box-shadow: 0 20px 55px rgba(0,0,0,.24); }}
    .health-dialog-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }}
    .health-dialog-head h2 {{ margin: 0; font-size: 18px; }}
    .health-dialog-head button {{ width: 34px; min-height: 34px; border-color: var(--line); background: transparent; color: var(--text); }}
    .health-checks {{ display: grid; gap: 8px; margin-top: 14px; }}
    .health-check {{ display: grid; grid-template-columns: 10px minmax(0,1fr); gap: 9px; padding: 10px; border: 1px solid var(--line); border-radius: 7px; }}
    .health-check .health-dot {{ margin-top: 4px; }}
    .health-check strong {{ display: block; font-size: 13px; }}
    .health-check span {{ display: block; margin-top: 2px; color: var(--muted); font-size: 12px; line-height: 1.45; }}
    @media (max-width: 640px) {{
      body {{
        display: flex;
        align-items: center;
        justify-content: center;
        overflow: auto;
        padding: 16px;
        background-position: 62% center;
      }}
      main {{ width: calc(100vw - 32px); max-width: 448px; flex: 0 0 auto; padding: 22px; border-radius: 14px; }}
      .login-head {{ align-items: center; }}
      .health-indicator {{ width: 34px; min-width: 34px; padding: 4px; justify-content: center; }}
      .health-indicator > span:last-child {{ position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; }}
      .login-kicker {{ display: none; }}
      h1 {{ font-size: 24px; }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      input, button {{ transition: none; }}
    }}
  </style>
</head>
<body>
  <main>
    <div class="login-head">
      <div class="login-brand">
        <img class="brand-mark" src="/assets/ttl-jay-crystal-logo.png" alt="">
        <div><div class="login-kicker">Secure review workspace</div><h1>CodeReviewer</h1></div>
      </div>
      <button id="loginHealthBtn" class="health-indicator" data-status="checking" type="button" aria-haspopup="dialog"><span class="health-dot" aria-hidden="true"></span><span id="loginHealthLabel">Checking</span></button>
    </div>
    <p class="login-subtitle">Review changes, resolve findings, and move every release forward with confidence.</p>
    <label><span class="field-label">Username <span class="required-mark" aria-hidden="true">*</span></span>
      <input id="username" autocomplete="username" placeholder="responsible" required>
    </label>
    <label><span class="field-label">Password <span class="required-mark" aria-hidden="true">*</span></span>
      <span class="password-field">
        <input id="password" type="password" autocomplete="current-password" placeholder="Password" required>
        <button id="togglePassword" class="inline-button password-toggle" type="button" aria-label="Show password" aria-pressed="false">Show</button>
      </span>
    </label>
    <label>
      <span class="challenge-line">
        <span class="challenge-prompt"><span class="field-label">Robot Check <span class="required-mark" aria-hidden="true">*</span></span><span aria-hidden="true">:</span> <strong id="robotQuestion">{html.escape(challenge["question"])}</strong></span>
        <button id="refreshChallengeBtn" class="inline-button challenge-reset" type="button">Reset</button>
      </span>
      <input id="robotAnswer" inputmode="numeric" autocomplete="off" required>
    </label>
    <input id="challengeId" type="hidden" value="{html.escape(challenge["id"])}">
    <button id="loginBtn">Login</button>
    <div id="status" class="status"></div>
    <div class="version-line">
      Version <strong>{html.escape(version)}</strong>
    </div>
  </main>
  <div id="loginHealthModal" class="health-backdrop" hidden>
    <section class="health-dialog" role="dialog" aria-modal="true" aria-labelledby="loginHealthTitle">
      <div class="health-dialog-head"><div><h2 id="loginHealthTitle">CodeReviewer health</h2><p id="loginHealthMeta" class="status">Loading service checks…</p></div><button id="closeLoginHealthBtn" type="button" aria-label="Close health details">&#x2715;</button></div>
      <div id="loginHealthChecks" class="health-checks"></div>
    </section>
  </div>
  <script>
    const $ = (id) => document.getElementById(id);
    let challengeRefreshTimer = 0;
    let loginInFlight = false;
    const CHALLENGE_REFRESH_MS = 60000;

    function escapeLoginHtml(value) {{
      return String(value || '').replace(/[&<>"']/g, char => ({{
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
      }}[char]));
    }}

    function renderLoginHealth(data, detailed = false) {{
      const status = String(data?.status || 'unhealthy');
      $('loginHealthBtn').dataset.status = status;
      $('loginHealthLabel').textContent = status === 'healthy' ? 'Healthy' : (status === 'degraded' ? 'Degraded' : 'Unavailable');
      if (!detailed) return;
      $('loginHealthMeta').textContent = `Version ${{data.version || '-'}} · Updated ${{String(data.updated_at || '').replace('T', ' ')}}`;
      const checks = Array.isArray(data.checks) ? data.checks : [];
      $('loginHealthChecks').innerHTML = checks.map(check => `<div class="health-check"><span class="health-dot" style="background:${{check.ok ? 'var(--ok)' : 'var(--danger)'}}" aria-hidden="true"></span><div><strong>${{escapeLoginHtml(check.name || '-')}}</strong><span>${{escapeLoginHtml(check.message || '')}}</span></div></div>`).join('');
    }}

    async function loadLoginHealth(details = false) {{
      const response = await fetch('/api/health', {{cache:'no-store'}});
      const data = await response.json();
      renderLoginHealth(data, details);
    }}

    function applyChallenge(challenge) {{
      if (!challenge) return;
      $('challengeId').value = challenge.id || '';
      $('robotQuestion').textContent = challenge.question || '';
      $('robotAnswer').value = '';
      resetChallengeTimer();
    }}

    async function refreshChallenge(statusText = '') {{
      const response = await fetch('/api/login-challenge', {{ cache: 'no-store' }});
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || 'Robot check reset failed');
      applyChallenge(data.challenge);
      if (statusText) {{
        $('status').className = 'status';
        $('status').textContent = statusText;
      }}
    }}

    function resetChallengeTimer() {{
      window.clearTimeout(challengeRefreshTimer);
      challengeRefreshTimer = window.setTimeout(() => {{
        refreshChallenge().catch((error) => {{
          $('status').className = 'status error';
          $('status').textContent = error.message;
        }});
      }}, CHALLENGE_REFRESH_MS);
    }}

    async function login() {{
      if (loginInFlight) return;
      const required = [$('username'), $('password'), $('robotAnswer')];
      required.forEach(field => field.removeAttribute('aria-invalid'));
      const missing = required.find(field => !field.value.trim());
      if (missing) {{
        missing.setAttribute('aria-invalid', 'true');
        $('status').className = 'status error';
        $('status').textContent = 'Complete all required fields.';
        missing.focus();
        return;
      }}
      loginInFlight = true;
      $('loginBtn').disabled = true;
      $('loginBtn').setAttribute('aria-busy', 'true');
      $('status').className = 'status';
      $('status').textContent = 'Signing in...';
      try {{
        const response = await fetch('/api/login', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            username: $('username').value.trim(),
            password: $('password').value,
            challenge_id: $('challengeId').value,
            robot_answer: $('robotAnswer').value.trim()
          }})
        }});
        const data = await response.json();
        if (!data.ok && data.challenge) applyChallenge(data.challenge);
        if (!data.ok && !data.challenge) await refreshChallenge();
        if (!data.ok) throw new Error(data.error || 'Login failed');
        window.location.href = '/';
      }} catch (error) {{
        $('status').className = 'status error';
        $('status').textContent = error.message;
      }} finally {{
        loginInFlight = false;
        $('loginBtn').disabled = false;
        $('loginBtn').removeAttribute('aria-busy');
      }}
    }}
    $('togglePassword').addEventListener('click', () => {{
      const password = $('password');
      const button = $('togglePassword');
      const shouldShow = password.type === 'password';
      password.type = shouldShow ? 'text' : 'password';
      button.textContent = shouldShow ? 'Hide' : 'Show';
      button.setAttribute('aria-label', shouldShow ? 'Hide password' : 'Show password');
      button.setAttribute('aria-pressed', shouldShow ? 'true' : 'false');
      password.focus();
    }});
    $('refreshChallengeBtn').addEventListener('click', () => {{
      refreshChallenge('Robot check reset.').catch((error) => {{
        $('status').className = 'status error';
        $('status').textContent = error.message;
      }});
    }});
    $('loginBtn').addEventListener('click', login);
    document.addEventListener('keydown', (event) => {{
      if (event.key === 'Enter' && !['togglePassword', 'refreshChallengeBtn'].includes(event.target?.id || '')) login();
    }});
    $('loginHealthBtn').addEventListener('click', async () => {{
      $('loginHealthModal').hidden = false;
      $('loginHealthChecks').innerHTML = '';
      $('loginHealthMeta').textContent = 'Running service checks…';
      try {{ await loadLoginHealth(true); }} catch (error) {{ $('loginHealthMeta').textContent = error.message; }}
      $('closeLoginHealthBtn').focus();
    }});
    $('closeLoginHealthBtn').addEventListener('click', () => {{ $('loginHealthModal').hidden = true; $('loginHealthBtn').focus(); }});
    $('loginHealthModal').addEventListener('click', event => {{ if (event.target === $('loginHealthModal')) $('closeLoginHealthBtn').click(); }});
    document.addEventListener('keydown', event => {{ if (event.key === 'Escape' && !$('loginHealthModal').hidden) $('closeLoginHealthBtn').click(); }});
    loadLoginHealth().catch(() => {{ $('loginHealthBtn').dataset.status = 'unhealthy'; $('loginHealthLabel').textContent = 'Unavailable'; }});
    resetChallengeTimer();
  </script>
</body>
</html>"""


def render_index(user: str = "") -> str:
    role = _web_user_role(user)
    is_admin = role == "manager"
    initial_projects = render_projects_markup(list_gitlab_projects_for_user(user))
    user_management_button = (
        '        <button id="configurationBtn" class="secondary workflow-launch" type="button">Configuration</button>\n'
        '        <button id="userManagementBtn" class="secondary workflow-launch" type="button">Users</button>'
        if is_admin else ""
    )
    admin_fields = """          <label><span class="field-title-row"><span>Sprint <span class="required-when-active" aria-hidden="true">*</span></span><span class="info-hint"><button class="information-icon" type="button" aria-label="Sprint field guidance" aria-expanded="false" aria-controls="sprintHintPopover">i</button><span id="sprintHintPopover" class="information-hint-popover" role="tooltip" hidden>Focus to view recent workflow Sprints. Jira access and Issue status are verified before Run Review.</span></span></span>
            <input id="sprint" list="sprintOptions" maxlength="255" size="50" placeholder="Search Sprint ID or name" autocomplete="off" aria-describedby="sprintValidation">
            <datalist id="sprintOptions"></datalist>
            <span id="sprintValidation" class="field-message" role="status" aria-live="polite"></span>
          </label>
          <label>Jira Filter ID
            <input id="jiraFilter" placeholder="12345">
          </label>""" if is_admin else ""
    input_grid_class = "grid" if is_admin else "grid jira-only"
    release_gate_panel = """          <div id="releaseGatePanel" class="release-gate-panel" aria-labelledby="releaseGateTitle">
            <div class="release-gate-copy">
              <div class="release-gate-kicker">Release workflow · Step 2</div>
              <div class="section-title-row"><h3 id="releaseGateTitle">GIT_VERSION Release Gate</h3><span class="info-hint"><button class="information-icon" type="button" aria-label="Release Gate guidance" aria-expanded="false" aria-controls="releaseGateHintPopover">i</button><span id="releaseGateHintPopover" class="information-hint-popover" role="tooltip" hidden>Run the final release gate after Sprint review. Company Config and SCR are verified from the immutable build commit locked by GIT_VERSION.</span></span></div>
              <div id="releaseGateContext" class="release-gate-context">No Sprint handoff selected. You can enter a GIT_VERSION MR directly.</div>
            </div>
            <div class="release-gate-form">
              <div class="release-gate-field">
                <div class="release-gate-field-head">
                  <label for="releaseGateMrUrl"><span class="field-title-row"><span>GIT_VERSION MR URL <span class="required-mark" aria-hidden="true">*</span></span><span class="info-hint"><button class="information-icon" type="button" aria-label="GIT_VERSION MR URL guidance" aria-expanded="false" aria-controls="releaseGateMrHintPopover">i</button><span id="releaseGateMrHintPopover" class="information-hint-popover" role="tooltip" hidden>Only a GIT_VERSION MR containing versioned git_version.yml/build.yml resources is accepted.</span></span></span></label>
                </div>
                <label id="releaseGateCandidateField" class="release-gate-candidates" for="releaseGateCandidateSelect" hidden>Detected application / GIT_VERSION MR
                  <select id="releaseGateCandidateSelect"></select>
                </label>
                <textarea id="releaseGateMrUrl" rows="1" maxlength="2048" spellcheck="false" placeholder="https://gitlab.tx-tech.com/group/project/-/merge_requests/123" autocomplete="off" aria-describedby="releaseGateStatus"></textarea>
              </div>
              <div class="release-gate-footer">
                <span id="releaseGateStatus" class="status release-gate-status" role="status" aria-live="polite"></span>
                <div class="release-gate-actions">
                  <button id="runReleaseGateBtn" type="button">Run Release Gate</button>
                </div>
              </div>
            </div>
          </div>""" if is_admin else ""
    adf_asset = '<script src="/assets/adf-editor.js"></script>' if (WEB_STATIC_DIR / "adf-editor.js").is_file() else ""
    run_hint = (
        "Input a Jira filter ID, Sprint, or one Jira issue for consolidated MR review."
        if is_admin
        else (
            "Input one Jira issue for consolidated MR review. Use Review Coverage to scan your responsible scope."
            if role == "auditor"
            else "Developer access: review assigned reports and submit a handling result for every finding in Review Communication."
        )
    )
    admin_trace = """      <section class="panel wide-panel admin-only" hidden>
        <h2>Review Trace</h2>
        <div class="actions" style="margin-bottom: 10px;">
          <button class="secondary" id="networkBtn" type="button">Check Network</button>
        </div>
        <div id="network" class="meta">Loading network status...</div>
        <div id="config" class="meta">Loading config...</div>
        <div id="history" class="meta">Loading history...</div>
      </section>""" if _is_admin_user(user) else ""
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CodeReviewer</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #edf5ff;
      --panel: #ffffff;
      --text: #10243e;
      --muted: #5c7088;
      --line: #cfdded;
      --accent: #0b6bcb;
      --accent-strong: #0754a3;
      --accent-soft: #e8f3ff;
      --danger: #b42318;
      --ok: #137333;
      --code: #08172b;
      --dialog-s: 560px;
      --dialog-m: 760px;
      --dialog-l: 1160px;
      --dialog-xl: 1480px;
      --dialog-full: 1680px;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #071426;
        --panel: #0d1c30;
        --text: #edf6ff;
        --muted: #a8bdd5;
        --line: #294561;
        --accent: #58a6ff;
        --accent-strong: #91c6ff;
        --accent-soft: #123253;
        --danger: #ff8a7a;
        --ok: #73d18a;
        --code: #050e1c;
      }
    }
    * { box-sizing: border-box; }
    .dialog-size-s { width: min(var(--dialog-s), calc(100vw - 32px)); }
    .dialog-size-m { width: min(var(--dialog-m), calc(100vw - 32px)); }
    .dialog-size-l { width: min(var(--dialog-l), calc(100vw - 32px)); }
    .dialog-size-xl { width: min(var(--dialog-xl), calc(100vw - 32px)); }
    .dialog-size-full { width: min(var(--dialog-full), calc(100vw - 32px)); }
    .sr-only { position: absolute !important; width: 1px !important; height: 1px !important; padding: 0 !important; margin: -1px !important; overflow: hidden !important; clip: rect(0, 0, 0, 0) !important; white-space: nowrap !important; border: 0 !important; }
    html {
      height: 100%;
    }
    body {
      margin: 0;
      height: 100%;
      overflow: hidden;
      font-family: Arial, "Microsoft YaHei", sans-serif;
      background:
        radial-gradient(circle at 16% 0%, color-mix(in srgb, var(--accent) 10%, transparent), transparent 34%),
        linear-gradient(145deg, color-mix(in srgb, var(--bg) 92%, white), var(--bg));
      color: var(--text);
    }
    header {
      border-bottom: 1px solid var(--line);
      background: color-mix(in srgb, var(--panel) 92%, var(--accent-soft));
      box-shadow: 0 4px 18px rgba(7, 49, 92, .06);
      backdrop-filter: blur(12px);
    }
    .topbar, main {
      width: min(1880px, calc(100vw - 32px));
      margin: 0 auto;
    }
    .topbar {
      min-height: 64px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }
    .topbar-brand {
      display: inline-flex;
      align-items: center;
      gap: 9px;
      min-width: 0;
    }
    .topbar-brand img {
      width: 32px;
      height: 38px;
      flex: 0 0 auto;
      object-fit: contain;
      filter: drop-shadow(0 5px 8px color-mix(in srgb, var(--accent) 24%, transparent));
    }
    .topbar-meta {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
      flex-wrap: wrap;
    }
    h1 {
      margin: 0;
      font-size: 22px;
      font-weight: 700;
      letter-spacing: 0;
    }
    main {
      display: grid;
      --projects-col: minmax(280px, 320px);
      --history-col: minmax(260px, 300px);
      grid-template-columns: var(--projects-col) minmax(360px, 1fr) minmax(420px, 1fr) var(--history-col);
      gap: 16px;
      height: calc(100vh - 64px);
      padding: 16px 0;
      align-items: stretch;
      overflow: hidden;
    }
    main.projects-collapsed {
      --projects-col: 48px;
    }
    main.history-collapsed {
      --history-col: 48px;
    }
    section, aside {
      min-height: 0;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 10px;
      box-shadow: 0 4px 18px rgba(7, 49, 92, .055);
    }
    aside {
      min-width: 0;
      padding: 16px;
      height: 100%;
      overflow: auto;
    }
    .side-panel {
      position: relative;
      transition: border-color 0.15s ease, background 0.15s ease;
    }
    .side-panel.collapsed {
      padding: 0;
      overflow: hidden;
      display: grid;
      place-items: stretch;
    }
    .side-content {
      min-width: 0;
    }
    .side-panel.collapsed .side-content {
      display: none;
    }
    .side-toggle {
      min-width: 0;
      min-height: 0;
      border: 0;
      border-radius: 0;
      background: transparent;
      color: var(--accent-strong);
      font-weight: 700;
    }
    .side-panel:not(.collapsed) > .side-toggle {
      position: sticky;
      top: -16px;
      z-index: 5;
      float: right;
      width: 32px;
      height: 32px;
      min-height: 32px;
      margin: -6px -6px 6px 8px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      padding: 0;
      line-height: 1;
    }
    .side-panel.collapsed > .side-toggle {
      width: 100%;
      height: 100%;
      padding: 12px 0;
      writing-mode: vertical-rl;
      text-orientation: mixed;
      letter-spacing: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 10px;
      border-radius: 8px;
    }
    .side-panel.collapsed > .side-toggle:hover,
    .side-panel:not(.collapsed) > .side-toggle:hover {
      background: color-mix(in srgb, var(--accent) 10%, transparent);
    }
    .side-toggle .collapsed-label {
      display: none;
    }
    .side-panel.collapsed .side-toggle .expanded-label {
      display: none;
    }
    .side-panel.collapsed .side-toggle .collapsed-label {
      display: inline;
    }
    .workspace {
      display: contents;
    }
    .panel {
      min-width: 0;
      padding: 16px;
      min-height: 0;
    }
    .wide-panel {
      grid-column: 1 / -1;
    }
    .run-panel {
      grid-column: 2;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      container-name: run-review;
      container-type: inline-size;
    }
    .run-panel-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex: 0 0 auto;
    }
    .run-panel-head h2 {
      margin-bottom: 0;
    }
    .run-form-summary {
      display: none;
      margin-top: 3px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }
    .run-form-toggle {
      flex: 0 0 auto;
      width: 32px;
      height: 30px;
      min-width: 32px;
      min-height: 30px;
      padding: 0;
      font-size: 15px;
    }
    .run-form-toggle .toggle-glyph {
      display: block;
      transition: transform 160ms ease;
    }
    .run-form-body {
      margin-top: 12px;
      opacity: 1;
      overflow: visible;
      transition: margin-top 180ms ease, opacity 120ms ease;
    }
    .run-primary-actions {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-top: 2px;
      flex-wrap: wrap;
    }
    .run-primary-actions button { flex: 0 0 auto; }
    .run-action-status {
      min-height: 0;
      margin-top: 2px;
    }
    .release-gate-panel {
      display: grid;
      grid-template-columns: minmax(250px, .78fr) minmax(360px, 1.22fr);
      gap: 16px;
      margin-top: 16px;
      padding: 16px 18px 18px;
      border: 1px solid color-mix(in srgb, var(--accent) 24%, var(--line));
      border-radius: 10px;
      background: color-mix(in srgb, var(--accent) 2.5%, var(--panel));
      align-items: start;
    }
    .release-gate-copy h3 { margin: 3px 0 0; font-size: 15px; }
    .release-gate-kicker { color: var(--accent-strong); font-size: 11px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }
    .release-gate-context { margin-top: 8px; padding: 9px 10px; border-left: 3px solid var(--accent); border-radius: 4px; background: color-mix(in srgb, var(--accent) 7%, var(--panel)); color: var(--muted); font-size: 12px; line-height: 1.45; }
    .release-gate-form {
      min-width: 0;
      display: grid;
      gap: 8px;
      align-content: start;
    }
    .release-gate-field { display: grid; min-width: 0; gap: 18px; }
    .release-gate-field-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
    }
    .release-gate-field-head label { min-width: 0; margin: 0; }
    .release-gate-field > textarea {
      width: 100%;
      min-width: 0;
      min-height: 38px;
      max-height: 60px;
      line-height: 1.45;
      resize: none;
      overflow: hidden;
      overflow-wrap: anywhere;
      transition: height 120ms ease, border-color 120ms ease, box-shadow 120ms ease;
    }
    .release-gate-candidates {
      margin: -7px 0 0;
      color: var(--muted);
      font-size: 12px;
    }
    .release-gate-candidates[hidden] { display: none; }
    .release-gate-candidates select {
      margin-top: 5px;
      color: var(--text);
      font-size: 13px;
    }
    .release-gate-actions { display: flex; align-items: center; gap: 12px; margin: 0; flex-wrap: wrap; }
    .release-gate-actions button { flex: 0 0 auto; }
    .release-gate-footer {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      min-width: 0;
    }
    .release-gate-status {
      flex: 1 1 auto;
      min-height: 0;
      margin: 0;
    }
    .required-mark { color: var(--danger); font-weight: 700; }
    .gate-result-strip { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-top: 8px; }
    .gate-result-metric { padding: 8px 10px; border: 1px solid var(--line); border-radius: 7px; background: color-mix(in srgb, var(--bg) 35%, var(--panel)); }
    .gate-result-metric span { display: block; color: var(--muted); font-size: 11px; }
    .gate-result-metric strong { display: block; margin-top: 3px; font-size: 14px; }
    .gate-handoff { margin-top: 8px; min-height: 34px; padding: 6px 10px; }
    .run-panel.form-collapsed .run-form-body {
      max-height: 0;
      margin-top: 0;
      opacity: 0;
      overflow: hidden;
      pointer-events: none;
    }
    .run-panel.form-collapsed .run-form-summary {
      display: block;
    }
    .run-panel.form-collapsed .run-form-toggle .toggle-glyph {
      transform: rotate(180deg);
    }
    @media (prefers-reduced-motion: reduce) {
      .run-form-body,
      .run-form-toggle .toggle-glyph {
        transition: none;
      }
    }
    @container run-review (max-width: 680px) {
      .release-gate-panel { grid-template-columns: 1fr; gap: 14px; }
    }
    @container run-review (max-width: 460px) {
      .run-primary-actions { align-items: stretch; }
      .run-primary-actions button { width: 100%; }
      .release-gate-field-head { align-items: stretch; flex-direction: column; }
      .release-gate-footer { align-items: stretch; flex-direction: column; }
      .release-gate-actions,
      .release-gate-actions button { width: 100%; }
    }
    .preview-panel {
      grid-column: 3;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    .history-panel {
      grid-column: 4;
      height: 100%;
      overflow: auto;
    }
    h2 {
      margin: 0 0 12px;
      font-size: 16px;
      letter-spacing: 0;
    }
    label {
      display: grid;
      gap: 6px;
      margin-bottom: 12px;
      color: var(--muted);
      font-size: 13px;
    }
    .field-label { display: inline-flex; align-items: baseline; gap: 4px; width: fit-content; }
    input, select, textarea, button {
      font: inherit;
      border-radius: 6px;
    }
    input, select, textarea {
      width: 100%;
      border: 1px solid var(--line);
      background: color-mix(in srgb, var(--panel) 95%, var(--accent-soft));
      color: var(--text);
      padding: 10px;
      transition: border-color .15s ease, box-shadow .15s ease, background .15s ease;
    }
    input:hover, select:hover, textarea:hover { border-color: color-mix(in srgb, var(--accent) 38%, var(--line)); }
    input:focus, select:focus, textarea:focus {
      border-color: var(--accent);
      outline: 0;
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 14%, transparent);
      background: var(--panel);
    }
    textarea {
      min-height: 180px;
      resize: vertical;
      font-family: Consolas, "Courier New", monospace;
      font-size: 13px;
    }
    button {
      min-height: 40px;
      border: 1px solid var(--accent);
      background: var(--accent);
      color: white;
      padding: 8px 14px;
      cursor: pointer;
      box-shadow: 0 3px 9px color-mix(in srgb, var(--accent) 12%, transparent);
      transition: transform .15s ease, box-shadow .15s ease, filter .15s ease;
    }
    button:hover:not(:disabled) { filter: brightness(1.035); box-shadow: 0 6px 16px color-mix(in srgb, var(--accent) 20%, transparent); }
    button:active:not(:disabled) { transform: translateY(1px); }
    button:focus-visible { outline: 0; box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 20%, transparent); }
    button:disabled {
      cursor: wait;
      opacity: 0.64;
    }
    button.secondary {
      background: color-mix(in srgb, var(--panel) 94%, var(--accent-soft));
      color: var(--accent-strong);
      box-shadow: none;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      align-items: start;
    }
    .run-panel input, .run-panel select { min-height: 40px; }
    .release-gate-form .information-hint-popover { right: 0; left: auto; }
    .grid.jira-only {
      grid-template-columns: minmax(0, 1fr);
      max-width: 520px;
    }
    .project-toolbar {
      position: sticky;
      top: -16px;
      z-index: 2;
      margin: 0 -16px 8px;
      padding: 0 16px 12px;
      background: var(--panel);
      border-bottom: 1px solid var(--line);
    }
    .project-toolbar h2 {
      margin-bottom: 10px;
    }
    .search-input {
      min-height: 36px;
      padding: 8px 10px;
      font-size: 13px;
    }
    .field-title-row, .section-title-row, .inline-hint-row {
      display: flex;
      align-items: center;
      gap: 7px;
      min-width: 0;
    }
    .info-hint {
      position: relative;
      display: inline-flex;
      flex: 0 0 auto;
      align-items: center;
    }
    button.information-icon {
      width: 20px;
      min-width: 20px;
      height: 20px;
      min-height: 20px;
      display: inline-grid;
      place-items: center;
      border: 1px solid color-mix(in srgb, var(--accent) 48%, var(--line));
      border-radius: 50%;
      padding: 0;
      background: color-mix(in srgb, var(--accent) 6%, var(--panel));
      color: var(--accent-strong);
      font-family: Georgia, serif;
      font-size: 13px;
      font-weight: 700;
      font-style: italic;
      line-height: 1;
    }
    button.information-icon:hover,
    button.information-icon:focus-visible,
    button.information-icon[aria-expanded="true"] {
      border-color: var(--accent);
      background: color-mix(in srgb, var(--accent) 12%, var(--panel));
      outline: 0;
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 15%, transparent);
    }
    .information-hint-popover {
      position: absolute;
      z-index: 80;
      top: calc(100% + 8px);
      left: 0;
      width: min(320px, calc(100vw - 48px));
      padding: 10px 12px;
      border: 1px solid color-mix(in srgb, var(--accent) 28%, var(--line));
      border-radius: 8px;
      background: var(--panel);
      color: var(--text);
      box-shadow: 0 12px 30px rgba(15, 23, 42, .16);
      font-size: 12px;
      font-weight: 400;
      line-height: 1.5;
    }
    .information-hint-popover[hidden] { display: none; }
    .run-guidance {
      display: flex;
      align-items: center;
      min-height: 30px;
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }
    .run-guidance .information-hint-popover { top: auto; bottom: calc(100% + 8px); }
    .history-tools-card {
      display: grid;
      gap: 9px;
      width: 100%;
      min-width: 0;
      box-sizing: border-box;
      margin: 0 0 14px;
      padding: 11px;
      border: 1px solid var(--line);
      border-radius: 9px;
      background: color-mix(in srgb, var(--bg) 42%, var(--panel));
    }
    .report-filter {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 8px;
      width: 100%;
      min-width: 0;
      box-sizing: border-box;
      margin: 0;
    }
    .report-filter > .search-input,
    .report-filter-row {
      width: 100%;
      min-width: 0;
      box-sizing: border-box;
    }
    .report-filter-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 76px;
      gap: 8px;
      align-items: stretch;
    }
    .history-panel .search-input {
      min-height: 34px;
      padding: 7px 10px;
    }
    .history-panel select.search-input {
      min-width: 0;
      padding-right: 28px;
    }
    #refreshDownloadsBtn {
      min-height: 34px;
      width: 76px;
      padding: 6px 8px;
      font-size: 13px;
      line-height: 1;
    }
    .actions {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .project-group {
      border-top: 1px solid var(--line);
      padding: 14px 0 4px;
    }
    .project-group:first-of-type { border-top: 0; padding-top: 0; }
    .project-group-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin: 0 0 8px;
      font-size: 14px;
      font-weight: 700;
      color: var(--accent-strong);
    }
    .count-pill {
      min-width: 24px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 2px 8px;
      color: var(--muted);
      text-align: center;
      font-size: 12px;
      font-weight: 700;
    }
    .project, .report-row {
      border-top: 1px solid var(--line);
      padding: 12px 0;
    }
    .project-group-title + .project { border-top: 0; }
    .project:first-of-type, .report-row:first-of-type { border-top: 0; }
    .project strong {
      display: block;
      color: var(--text);
      text-decoration: none;
      font-weight: 700;
      overflow-wrap: anywhere;
    }
    .report-row a {
      display: block;
      color: var(--text);
      text-decoration: none;
      font-weight: 700;
      line-height: 1.35;
      overflow-wrap: break-word;
      word-break: normal;
    }
    .project.hidden, .project-group.hidden { display: none; }
    .report-main {
      min-width: 0;
      display: grid;
      gap: 4px;
    }
    .report-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .report-row .actions {
      flex: 0 0 auto;
    }
    .report-actions {
      display: flex;
      flex: 0 0 auto;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .history-panel .report-row {
      align-items: stretch;
      flex-direction: column;
      gap: 8px;
      margin-bottom: 10px;
      padding: 11px;
      border: 1px solid var(--line);
      border-radius: 9px;
      background: var(--panel);
      box-shadow: 0 2px 8px rgba(15, 23, 42, .035);
    }
    .history-panel .report-row:first-of-type { border-top: 1px solid var(--line); }
    .history-panel .report-row:hover { border-color: color-mix(in srgb, var(--accent) 35%, var(--line)); }
    .history-panel .report-title { display: -webkit-box; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
    .report-meta-line { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; }
    .report-meta-chip { display: inline-flex; align-items: center; min-height: 21px; padding: 1px 6px; border-radius: 999px; background: color-mix(in srgb, var(--bg) 75%, var(--panel)); color: var(--muted); font-size: 11px; }
    .history-panel .report-main {
      width: 100%;
    }
    .history-panel .report-actions, .history-panel .report-row .actions {
      width: 100%;
      justify-content: flex-start;
    }
    .history-panel .report-actions > *, .history-panel .report-row .actions > * {
      flex: 1 1 84px;
      justify-content: center;
      text-align: center;
    }
    .history-tabs, .thread-tabs {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 3px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: color-mix(in srgb, var(--bg) 70%, var(--panel));
    }
    .history-tabs {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      width: 100%;
      min-width: 0;
      box-sizing: border-box;
      margin: 0;
      gap: 4px;
      padding: 3px;
    }
    .history-tabs .history-tab {
      width: 100%;
      min-width: 0;
    }
    .history-tab, .thread-tab {
      min-height: 30px;
      border: 0;
      border-radius: 5px;
      padding: 5px 9px;
      background: transparent;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
    }
    .history-tab {
      flex: 1 1 0;
      min-height: 38px;
      padding: 6px 8px;
      line-height: 1.1;
      white-space: normal;
    }
    .history-tab.active, .thread-tab.active {
      background: var(--panel);
      color: var(--accent-strong);
      box-shadow: 0 1px 3px rgba(15, 23, 42, 0.12);
    }
    .history-pane[hidden], .thread-pane[hidden] {
      display: none;
    }
    .download-link {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 32px;
      border: 1px solid var(--accent);
      border-radius: 6px;
      padding: 6px 10px;
      color: var(--accent-strong);
      text-decoration: none;
      font-weight: 700;
      white-space: nowrap;
    }
    .history-panel .download-link,
    .history-panel .small-action,
    .history-panel button.secondary {
      min-height: 34px;
      border-radius: 6px;
      padding: 6px 10px;
      font-size: 13px;
      line-height: 1.15;
    }
    .small-action {
      min-height: 32px;
      border-radius: 6px;
      padding: 6px 10px;
      font-size: 13px;
    }
    .danger-action {
      border-color: var(--danger);
      background: var(--danger);
      color: white;
    }
    .danger-action:disabled {
      cursor: not-allowed;
    }
    .confirm-backdrop {
      position: fixed;
      inset: 0;
      z-index: 20;
      display: grid;
      place-items: center;
      padding: 24px;
      background: rgba(17, 24, 39, 0.46);
    }
    .confirm-backdrop[hidden] {
      display: none;
    }
    #userResetConfirmModal, #temporaryPasswordModal { z-index: 1300; }
    .release-notes-dialog {
      width: min(var(--dialog-l), calc(100vw - 32px));
      height: clamp(420px, 72vh, 720px);
      height: clamp(420px, 72dvh, 720px);
      max-height: calc(100vh - 32px);
      max-height: calc(100dvh - 32px);
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--panel);
      box-shadow: 0 24px 72px rgba(15, 23, 42, .28);
    }
    .release-notes-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
      padding: 16px 20px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    .release-notes-head h2 { margin: 0; font-size: 18px; }
    .release-notes-head .icon-action {
      width: 36px;
      min-width: 36px;
      height: 36px;
      min-height: 36px;
      border-radius: 8px;
      transition: background 140ms ease, border-color 140ms ease, transform 100ms ease;
    }
    .release-notes-head .icon-action:hover { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 7%, var(--panel)); }
    .release-notes-head .icon-action:active { transform: translateY(1px) scale(.97); }
    .release-notes-content {
      min-height: 0;
      margin: 0;
      padding: 18px 20px 24px;
      overflow-y: auto;
      overscroll-behavior: contain;
      scrollbar-gutter: stable;
    }
    .release-notes-content > :first-child { margin-top: 0; }
    .release-notes-error {
      display: grid;
      align-content: start;
      justify-items: start;
      gap: 8px;
      min-height: 160px;
      padding: 18px;
      border: 1px solid color-mix(in srgb, var(--danger) 34%, var(--line));
      border-radius: 9px;
      background: color-mix(in srgb, var(--danger) 4%, var(--panel));
      color: var(--text);
    }
    .release-notes-error strong { color: var(--danger); }
    @media (max-width: 600px) {
      .release-notes-backdrop { padding: 8px; }
      .release-notes-dialog {
        width: calc(100vw - 16px);
        height: calc(100vh - 16px);
        height: calc(100dvh - 16px);
        max-height: calc(100vh - 16px);
        max-height: calc(100dvh - 16px);
        border-radius: 9px;
      }
      .release-notes-head { padding: 13px 14px; }
      .release-notes-content { padding: 14px 14px 20px; }
    }
    .coverage-dialog {
      width: min(1480px, calc(100vw - 24px));
      max-height: calc(100vh - 24px);
      display: flex;
      flex-direction: column;
      gap: 12px;
      overflow: hidden;
      padding: 20px;
      font-size: 14px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--panel);
      box-shadow: 0 18px 48px rgba(0, 0, 0, 0.22);
    }
    .coverage-filters {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) minmax(150px, 0.45fr) minmax(150px, 0.45fr) auto;
      gap: 8px 12px;
      align-items: end;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: color-mix(in srgb, var(--bg) 45%, var(--panel));
      box-shadow: 0 2px 10px rgba(15, 23, 42, .035);
    }
    .coverage-filters .field-help {
      grid-column: 1 / -1;
      margin: 0 0 2px;
    }
    .coverage-filters label {
      margin: 0;
      font-size: 14px;
    }
    .coverage-filters #coverageScanBtn {
      min-width: 96px;
      height: 38px;
    }
    @media (max-width: 760px) {
      .coverage-filters { grid-template-columns: 1fr 1fr; }
      .coverage-filters label:first-of-type { grid-column: 1 / -1; }
      .coverage-filters #coverageScanBtn { width: 100%; }
    }
    @media (max-width: 520px) {
      .coverage-dialog { width: calc(100vw - 16px); padding: 14px; }
      .coverage-filters { grid-template-columns: 1fr; }
      .coverage-filters label:first-of-type { grid-column: auto; }
    }
    .coverage-summary {
      display: grid;
      grid-template-columns: repeat(3, minmax(280px, 1fr));
      gap: 10px;
      min-width: 0;
    }
    .coverage-view-tabs {
      display: flex;
      gap: 6px;
      margin: 0;
      border-bottom: 1px solid var(--line);
    }
    .coverage-view-tab {
      min-height: 38px;
      padding: 7px 12px;
      border: 0;
      border-radius: 6px 6px 0 0;
      background: transparent;
      color: var(--muted);
      font-weight: 700;
    }
    .coverage-view-tab.active {
      border-bottom: 2px solid var(--accent);
      color: var(--accent-strong);
    }
    .coverage-view-panel {
      min-height: 0;
      overflow-x: hidden;
      overflow-y: auto;
    }
    .coverage-view-panel[hidden] { display: none; }
    .coverage-overview-panel {
      display: grid;
      gap: 12px;
    }
    .coverage-report-totals,
    .coverage-scope-totals,
    .coverage-report-lifecycle {
      min-width: 0;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--panel);
      box-shadow: 0 2px 9px rgba(15, 23, 42, .035);
    }
    .coverage-report-totals,
    .coverage-scope-totals {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }
    .coverage-totals-title {
      grid-column: 1 / -1;
      display: grid;
      gap: 2px;
      min-width: 0;
    }
    .coverage-totals-title strong { font-size: 16px; line-height: 1.3; }
    .coverage-totals-title span { color: var(--muted); font-size: 12px; line-height: 1.35; }
    .coverage-report-total {
      display: grid;
      gap: 4px;
      min-width: 0;
      padding: 9px 10px;
      border-radius: 8px;
      background: color-mix(in srgb, var(--bg) 68%, var(--panel));
    }
    .coverage-report-total-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      min-width: 0;
    }
    .coverage-report-total-head > span {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .coverage-run-missing {
      min-height: 28px;
      padding: 4px 9px;
      border-radius: 6px;
      font-size: 12px;
      white-space: nowrap;
    }
    .coverage-report-total span,
    .coverage-lifecycle-head span {
      color: var(--muted);
      font-size: 13px;
    }
    .coverage-report-total strong { font-size: 24px; line-height: 1.2; }
    .coverage-report-total small { color: var(--muted); font-size: 12px; }
    .coverage-ratio-track {
      grid-column: 1 / -1;
      display: flex;
      height: 5px;
      overflow: hidden;
      border-radius: 999px;
      background: color-mix(in srgb, var(--line) 70%, transparent);
    }
    .coverage-ratio-with { background: var(--accent); }
    .coverage-ratio-without { background: color-mix(in srgb, var(--muted) 62%, var(--line)); }
    .coverage-lifecycle-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 8px;
    }
    .coverage-lifecycle-head > div:first-child {
      display: grid;
      gap: 3px;
      min-width: 0;
    }
    .coverage-lifecycle-head strong {
      display: block;
      font-size: 16px;
      line-height: 1.3;
    }
    .coverage-lifecycle-head > div:first-child span {
      display: block;
      line-height: 1.4;
    }
    .coverage-operational {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }
    .coverage-operational span {
      padding: 3px 7px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: color-mix(in srgb, var(--bg) 68%, var(--panel));
      white-space: nowrap;
    }
    .coverage-lifecycle-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 7px;
    }
    .coverage-lifecycle-stat {
      min-width: 0;
      padding: 9px 10px;
      border-radius: 8px;
      background: color-mix(in srgb, var(--bg) 68%, var(--panel));
    }
    .coverage-lifecycle-stat span { display: block; color: var(--muted); font-size: 13px; }
    .coverage-lifecycle-stat strong { display: block; margin-top: 3px; font-size: 20px; }
    @media (max-width: 1180px) {
      .coverage-summary { grid-template-columns: 1fr; }
    }
    @media (max-width: 520px) {
      .coverage-report-totals,
      .coverage-scope-totals,
      .coverage-lifecycle-grid { grid-template-columns: 1fr; }
      .coverage-ratio-track { grid-column: 1; }
      .coverage-totals-title { grid-column: 1; }
    }
    .coverage-applications {
      display: grid;
      gap: 9px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: color-mix(in srgb, var(--bg) 38%, var(--panel));
    }
    .coverage-applications:empty { display: none; }
    .coverage-applications-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 14px;
    }
    .coverage-applications-head strong { display: block; font-size: 16px; }
    .coverage-applications-head span {
      display: block;
      margin-top: 2px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.4;
      text-align: right;
    }
    .coverage-application-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 9px;
    }
    .coverage-application-card {
      display: grid;
      gap: 8px;
      min-width: 0;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 9px;
      background: var(--panel);
      box-shadow: 0 2px 9px rgba(15, 23, 42, .035);
    }
    .coverage-application-card.gate-ready {
      border-color: color-mix(in srgb, var(--ok) 42%, var(--line));
      background: linear-gradient(135deg, color-mix(in srgb, var(--ok) 5%, var(--panel)), var(--panel) 55%);
    }
    .coverage-application-card.unmapped {
      border-color: color-mix(in srgb, var(--danger) 32%, var(--line));
    }
    .coverage-application-title,
    .coverage-application-progress-copy,
    .coverage-application-footer {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      min-width: 0;
    }
    .coverage-application-title strong {
      overflow: hidden;
      font-size: 16px;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .coverage-application-percent {
      flex: 0 0 auto;
      color: var(--accent-strong);
      font-size: 21px;
      font-weight: 750;
    }
    .coverage-application-progress-copy {
      color: var(--muted);
      font-size: 13px;
    }
    .coverage-application-progress-copy strong { color: var(--text); font-size: 13px; }
    .coverage-application-track {
      height: 6px;
      overflow: hidden;
      border-radius: 999px;
      background: color-mix(in srgb, var(--line) 72%, transparent);
    }
    .coverage-application-track span {
      display: block;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--accent), var(--ok));
      transition: width .25s ease;
    }
    .coverage-application-report-coverage {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
    }
    .coverage-application-report-coverage span {
      padding: 4px 7px;
      border-radius: 999px;
      background: color-mix(in srgb, var(--bg) 65%, var(--panel));
    }
    .coverage-application-states {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 5px;
    }
    .coverage-application-state {
      min-width: 0;
      padding: 6px 5px;
      border-radius: 7px;
      background: color-mix(in srgb, var(--bg) 65%, var(--panel));
      text-align: center;
    }
    .coverage-application-state span {
      display: block;
      overflow: hidden;
      color: var(--muted);
      font-size: 12px;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .coverage-application-state strong { display: block; margin-top: 2px; font-size: 15px; }
    .coverage-application-footer {
      padding-top: 7px;
      border-top: 1px solid color-mix(in srgb, var(--line) 75%, transparent);
      color: var(--muted);
      font-size: 12px;
    }
    .coverage-application-footer .ready { color: var(--ok); font-weight: 700; }
    .coverage-application-footer .blocked { color: var(--danger); font-weight: 650; }
    @media (max-width: 880px) {
      .coverage-application-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 620px) {
      .coverage-applications-head { display: grid; }
      .coverage-applications-head span { text-align: left; }
      .coverage-application-states { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    }
    .coverage-progress {
      display: grid;
      grid-template-columns: auto minmax(180px, 1fr) auto;
      align-items: center;
      gap: 10px 14px;
      padding: 10px 12px;
      border: 1px solid color-mix(in srgb, var(--accent) 24%, var(--line));
      border-radius: 9px;
      background: color-mix(in srgb, var(--accent) 5%, var(--panel));
    }
    .coverage-progress.long-running {
      border-color: color-mix(in srgb, #b45309 34%, var(--line));
      background: color-mix(in srgb, #f59e0b 7%, var(--panel));
    }
    .coverage-progress.long-running .coverage-progress-spinner { border-top-color: #b45309; }
    .coverage-progress[hidden] { display: none; }
    .coverage-progress-spinner {
      width: 18px;
      height: 18px;
      border: 2px solid color-mix(in srgb, var(--accent) 22%, var(--line));
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: coverage-spin .8s linear infinite;
    }
    .coverage-progress-copy { min-width: 0; }
    .coverage-progress-copy strong { display: block; font-size: 13px; }
    .coverage-progress-copy span { display: block; margin-top: 2px; color: var(--muted); font-size: 12px; }
    .coverage-progress-timing {
      min-width: 152px;
      color: var(--muted);
      font-size: 12px;
      font-variant-numeric: tabular-nums;
      text-align: right;
    }
    .coverage-progress.long-running .coverage-progress-timing {
      color: #92400e;
      font-weight: 650;
    }
    .coverage-progress-track {
      grid-column: 1 / -1;
      height: 4px;
      overflow: hidden;
      border-radius: 999px;
      background: color-mix(in srgb, var(--accent) 12%, var(--line));
    }
    .coverage-progress-track > span {
      display: block;
      width: 0;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, color-mix(in srgb, var(--accent) 72%, white), var(--accent));
      transition: width .35s ease;
    }
    @keyframes coverage-spin { to { transform: rotate(360deg); } }
    @media (max-width: 620px) {
      .coverage-progress { grid-template-columns: auto minmax(0, 1fr); }
      .coverage-progress-timing { grid-column: 2; min-width: 0; text-align: left; }
    }
    @media (prefers-reduced-motion: reduce) {
      .coverage-progress-spinner { animation: none; }
      .coverage-progress-track > span { transition: none; }
    }
    .coverage-results {
      min-height: 240px;
      overflow-x: hidden;
      overflow-y: auto;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px;
      background: color-mix(in srgb, var(--bg) 38%, var(--panel));
    }
    .coverage-card-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      align-content: start;
      gap: 10px;
    }
    .coverage-issue-card {
      display: grid;
      gap: 9px;
      min-width: 0;
      padding: 13px 14px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--panel);
      box-shadow: 0 2px 9px rgba(15, 23, 42, .035);
    }
    .coverage-issue-card:hover {
      border-color: color-mix(in srgb, var(--accent) 34%, var(--line));
      box-shadow: 0 5px 16px rgba(15, 23, 42, .07);
    }
    .coverage-card-head,
    .coverage-card-footer {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      min-width: 0;
    }
    .coverage-card-state {
      display: grid;
      justify-items: end;
      gap: 5px;
      flex: 0 0 auto;
      color: var(--muted);
      font-size: 12px;
      text-align: right;
    }
    .coverage-card-key {
      color: var(--accent-strong);
      font-size: 16px;
      overflow-wrap: anywhere;
    }
    .coverage-card-summary {
      min-height: 36px;
      overflow: hidden;
      display: -webkit-box;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 2;
      color: var(--text);
      font-size: 14px;
      line-height: 1.42;
    }
    .coverage-card-owner {
      overflow: hidden;
      color: var(--muted);
      font-size: 13px;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .coverage-card-applications {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
    }
    .coverage-card-application {
      padding: 2px 7px;
      border: 1px solid color-mix(in srgb, var(--accent) 24%, var(--line));
      border-radius: 999px;
      background: color-mix(in srgb, var(--accent) 5%, var(--panel));
      color: var(--accent-strong);
      font-size: 11px;
      font-weight: 650;
    }
    .coverage-card-cycle {
      display: flex;
      align-items: center;
      gap: 6px;
      min-width: 0;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .coverage-card-cycle::before {
      content: "";
      flex: 0 0 auto;
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: color-mix(in srgb, var(--accent) 65%, var(--line));
    }
    .coverage-card-metrics {
      display: grid;
      grid-template-columns: .7fr .7fr 1.25fr;
      gap: 7px;
    }
    .coverage-card-metric {
      min-width: 0;
      padding: 7px 8px;
      border-radius: 7px;
      background: color-mix(in srgb, var(--bg) 70%, var(--panel));
    }
    .coverage-card-metric span {
      display: block;
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: .04em;
    }
    .coverage-card-metric strong {
      display: block;
      margin-top: 2px;
      font-size: 16px;
    }
    .coverage-card-metric small {
      display: block;
      margin-top: 2px;
      color: var(--muted);
      font-size: 12px;
    }
    .coverage-card-footer {
      min-height: 30px;
      padding-top: 2px;
      border-top: 1px solid color-mix(in srgb, var(--line) 75%, transparent);
    }
    .coverage-card-actions { display: flex; align-items: center; justify-content: flex-end; gap: 6px; }
    .coverage-run-review {
      min-height: 28px;
      padding: 4px 9px;
      font-size: 13px;
      white-space: nowrap;
    }
    .coverage-empty {
      grid-column: 1 / -1;
      min-height: 180px;
      display: grid;
      place-items: center;
      color: var(--muted);
      text-align: center;
    }
    @media (min-width: 961px) and (max-width: 1180px) {
      .thread-layout {
        grid-template-columns: minmax(260px, .9fr) minmax(300px, 1.1fr);
        grid-template-rows: minmax(340px, 1fr) auto;
        overflow-y: auto;
      }
      .thread-layout > .thread-column:first-child { grid-column: 1; grid-row: 1; }
      .thread-layout > .thread-followup-column { grid-column: 2; grid-row: 1; }
      .thread-layout > .thread-reply-column { grid-column: 1 / -1; grid-row: 2; }
      .thread-reply-column .thread-form { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); overflow: visible; }
    }
    @media (max-width: 960px) {
      .coverage-card-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 520px) {
      .coverage-card-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .coverage-card-metric:last-child { grid-column: 1 / -1; }
      .coverage-card-footer { align-items: flex-start; flex-direction: column; }
      .coverage-card-actions,
      .coverage-card-actions button { width: 100%; }
    }
    .workflow-badge {
      display: inline-flex;
      padding: 2px 7px;
      border: 1px solid var(--line);
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }
    .workflow-badge.passed,
    .workflow-badge.ready { color: var(--ok); border-color: var(--ok); }
    .workflow-badge.pending,
    .workflow-badge.failed { color: var(--danger); border-color: var(--danger); }
    .workflow-badge.running { color: var(--accent-strong); border-color: var(--accent); }
    .confirm-dialog {
      width: min(var(--dialog-m), calc(100vw - 32px));
      max-height: min(720px, calc(100vh - 48px));
      display: flex;
      flex-direction: column;
      gap: 14px;
      padding: 18px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: 0 18px 48px rgba(0, 0, 0, 0.22);
    }
    .confirm-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 14px;
    }
    .confirm-head h2 {
      margin-bottom: 4px;
    }
    .confirm-report-list {
      display: grid;
      gap: 8px;
      max-height: 220px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
    }
    .confirm-report {
      display: grid;
      gap: 3px;
      padding: 8px 0;
      border-top: 1px solid var(--line);
    }
    .confirm-report a {
      color: var(--text);
      text-decoration: none;
      font-weight: 700;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .confirm-report:first-child {
      border-top: 0;
      padding-top: 0;
    }
    .reuse-badge {
      display: inline-flex;
      align-items: center;
      width: fit-content;
      min-height: 22px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 12px;
      font-weight: 700;
      color: var(--muted);
      background: #f8fafc;
    }
    .reuse-badge.fresh {
      border-color: #86efac;
      color: var(--ok);
      background: #f0fdf4;
    }
    .reuse-badge.changed {
      border-color: #fbbf24;
      color: #92400e;
      background: #fffbeb;
    }
    .reuse-badge.unknown {
      border-color: #cbd5e1;
      color: var(--muted);
      background: #f8fafc;
    }
    .confirm-step {
      display: grid;
      gap: 10px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
    }
    .confirm-step[hidden] {
      display: none;
    }
    .confirm-actions {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 10px;
      flex-wrap: wrap;
    }
    .thread-modal-backdrop {
      position: fixed;
      inset: 0;
      z-index: 30;
      display: grid;
      place-items: center;
      padding: 16px;
      background: rgba(17, 24, 39, 0.48);
    }
    .thread-modal-backdrop[hidden] {
      display: none;
    }
    .thread-modal-dialog {
      width: min(var(--dialog-xl), calc(100vw - 32px));
      height: min(900px, calc(100vh - 32px));
      max-height: calc(100vh - 32px);
      display: flex;
      flex-direction: column;
      min-height: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: 0 24px 60px rgba(0, 0, 0, 0.28);
      padding: 18px;
    }
    .thread-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
      flex: 0 0 auto;
    }
    .thread-head h2 { font-size: 20px; line-height: 1.3; }
    .thread-modal-dialog .meta { font-size: 13px; }
    .thread-layout {
      display: grid;
      grid-template-columns: minmax(300px, 0.9fr) minmax(360px, 1.08fr) minmax(380px, 1.02fr);
      grid-template-rows: minmax(0, 1fr);
      gap: 14px;
      min-height: 0;
      height: 100%;
      flex: 1 1 auto;
    }
    .thread-tabs {
      margin-bottom: 12px;
      flex: 0 0 auto;
    }
    .thread-pane {
      min-height: 0;
      flex: 1 1 auto;
      overflow: hidden;
    }
    .thread-pane:not(.tools):not([hidden]) {
      display: flex;
      flex-direction: column;
    }
    .thread-pane.tools {
      display: grid;
      grid-template-columns: minmax(360px, 1.12fr) minmax(340px, .88fr);
      gap: 14px;
      height: 100%;
      padding: 2px;
    }
    .thread-pane.tools[hidden] {
      display: none;
    }
    .thread-column {
      min-width: 0;
      min-height: 0;
      height: 100%;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 12px;
      background: color-mix(in srgb, var(--bg) 48%, var(--panel));
    }
    .thread-column h3 {
      margin: 0 0 10px;
      font-size: 14px;
    }
    .thread-reply-column { display: grid; grid-template-rows: auto minmax(0, 1fr); padding: 18px; background: var(--panel); }
    .thread-layout > .thread-column:first-child { grid-column: 1; grid-row: 1; }
    .thread-layout > .thread-followup-column { grid-column: 2; grid-row: 1; }
    .thread-layout > .thread-reply-column { grid-column: 3; grid-row: 1; }
    .thread-section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; margin: 0 0 14px; padding: 0 2px 12px; border-bottom: 1px solid var(--line); }
    .thread-section-heading h3 { margin: 0 0 4px; font-size: 18px; line-height: 1.35; }
    .thread-section-heading p { margin: 0; line-height: 1.45; }
    .thread-section-heading .information-hint-popover { right: 0; left: auto; width: min(380px, calc(100vw - 48px)); }
    .thread-reply-column .thread-form {
      display: grid;
      grid-template-rows: repeat(2, minmax(250px, 1fr));
      gap: 12px;
      height: 100%;
      padding: 0 2px 2px;
      overflow-x: hidden;
      overflow-y: auto;
    }
    .thread-reply-card { display: grid; grid-template-rows: auto minmax(0, 1fr) auto auto; flex: 1 1 0; align-content: stretch; gap: 10px; min-height: 270px; padding: 14px; border: 1px solid var(--line); border-radius: 9px; background: color-mix(in srgb, var(--bg) 35%, var(--panel)); }
    .thread-reply-card label { display: grid; gap: 6px; margin: 0; color: var(--muted); font-size: 12px; }
    .thread-reply-card textarea { width: 100%; height: 100%; min-height: 140px; max-height: 320px; background: var(--panel); resize: vertical; }
    .thread-reply-card-head p { margin: 0; color: var(--muted); font-size: 12px; line-height: 1.45; }
    .thread-reply-actions { display: flex; justify-content: flex-end; gap: 8px; }
    .thread-reply-actions button { min-width: min(148px, 100%); max-width: 100%; }
    .thread-card-action { min-height: 38px; border-color: var(--accent); background: var(--accent); color: white; font-weight: 700; }
    .thread-reply-card {
      grid-template-rows: auto minmax(0, 1fr) auto auto;
      min-height: 250px;
      padding: 15px;
      background: var(--panel);
      box-shadow: 0 4px 14px rgba(15, 23, 42, .045);
    }
    .thread-reply-card-head { display: grid; gap: 3px; padding-bottom: 9px; border-bottom: 1px solid var(--line); }
    .thread-reply-card-head strong { font-size: 13px; line-height: 1.35; }
    .thread-reply-card label { min-height: 0; }
    .thread-reply-card textarea { height: 100%; min-height: 118px; max-height: none; }
    .thread-reply-actions { padding-top: 1px; }
    .thread-card-action { min-width: 118px; box-shadow: 0 2px 5px rgba(25, 113, 194, .16); }
    .thread-context {
      display: grid;
      gap: 8px;
      min-width: 0;
      margin-top: 5px;
    }
    .thread-issue-title {
      max-width: min(880px, 72vw);
      color: var(--text);
      font-size: 14px;
      font-weight: 700;
      line-height: 1.35;
      display: -webkit-box;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 2;
      overflow: hidden;
    }
    .thread-context-metrics { display: flex; flex-wrap: wrap; gap: 6px; }
    .thread-context-metric { padding: 4px 9px; border: 1px solid var(--line); border-radius: 999px; color: var(--muted); font-size: 12px; background: var(--panel); }
    .thread-context-metric strong { color: var(--text); }
    .thread-column-heading {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin: 0 0 10px;
    }
    .thread-column-heading h3 { margin: 0; }
    .copy-draft-button {
      width: 32px;
      min-width: 32px;
      height: 30px;
      min-height: 30px;
      display: inline-grid;
      place-items: center;
      padding: 0;
      border-color: color-mix(in srgb, var(--accent) 35%, var(--line));
      border-radius: 7px;
      background: var(--panel);
      box-shadow: 0 2px 6px rgba(15, 23, 42, .08);
      transition: transform 100ms ease, box-shadow 140ms ease, border-color 140ms ease, background 140ms ease;
    }
    .copy-draft-button svg { width: 16px; height: 16px; pointer-events: none; }
    .copy-draft-button:hover { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 7%, var(--panel)); box-shadow: 0 4px 10px rgba(15, 23, 42, .12); }
    .copy-draft-button:active { transform: translateY(1px) scale(.97); box-shadow: 0 1px 3px rgba(15, 23, 42, .1); }
    .copy-draft-button:focus-visible { outline: 0; box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 18%, transparent); }
    .copy-draft-button.copied { border-color: var(--ok); background: color-mix(in srgb, var(--ok) 9%, var(--panel)); color: var(--ok); }
    .copy-draft-button.copy-error { border-color: var(--danger); background: color-mix(in srgb, var(--danger) 7%, var(--panel)); color: var(--danger); }
    .copy-draft-button:disabled { cursor: not-allowed; opacity: .52; }
    .thread-open-review { margin-left: auto; border: 1px solid var(--accent); color: var(--accent-strong); }
    .thread-messages {
      display: grid;
      gap: 8px;
      flex: 1 1 auto;
      min-height: 0;
      max-height: none;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      background: var(--panel);
    }
    .thread-message {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px;
      background: var(--panel);
    }
    .thread-message.system {
      border-color: var(--accent);
    }
    .handling-list {
      display: grid;
      gap: 10px;
      min-height: 0;
      overflow: auto;
      padding-right: 4px;
    }
    .handling-item {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(170px, 0.28fr);
      gap: 12px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
    }
    .handling-item-head {
      display: flex;
      align-items: flex-start;
      gap: 8px;
      margin-bottom: 6px;
    }
    .handling-severity {
      flex: 0 0 auto;
      font-size: 12px;
      font-weight: 700;
      color: var(--danger);
    }
    .handling-controls {
      display: grid;
      gap: 8px;
      align-content: start;
    }
    .handling-controls label {
      margin: 0;
    }
    .handling-controls textarea {
      min-height: 76px;
      resize: vertical;
    }
    .handling-summary {
      margin-bottom: 10px;
      padding: 9px 11px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: color-mix(in srgb, var(--accent) 5%, var(--panel));
    }
    .thread-form {
      display: grid;
      gap: 8px;
      min-height: 0;
      align-content: start;
      overflow: auto;
      flex: 1 1 auto;
    }
    .thread-form textarea {
      min-height: 96px;
      resize: vertical;
    }
    .thread-form input, .thread-form select {
      width: 100%;
    }
    .chat-messages {
      display: grid;
      align-content: start;
      gap: 8px;
      min-height: 0;
      flex: 1 1 auto;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 14px;
      background: linear-gradient(180deg, color-mix(in srgb, var(--accent) 3%, var(--panel)), var(--panel) 160px);
    }
    .chat-message {
      display: grid;
      grid-template-columns: 32px minmax(0, 1fr);
      gap: 10px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--panel);
      box-shadow: 0 3px 12px rgba(15, 23, 42, .045);
    }
    .chat-message.assistant {
      border-color: color-mix(in srgb, var(--accent) 45%, var(--line));
      background: linear-gradient(135deg, color-mix(in srgb, var(--accent) 7%, var(--panel)), var(--panel) 42%);
    }
    .chat-message.user { background: color-mix(in srgb, var(--bg) 42%, var(--panel)); }
    .chat-avatar { width: 32px; height: 32px; display: grid; place-items: center; border-radius: 9px; background: color-mix(in srgb, var(--line) 50%, var(--bg)); color: var(--muted); font-size: 11px; font-weight: 800; letter-spacing: .03em; }
    .chat-message.assistant .chat-avatar { background: var(--accent); color: white; }
    .chat-message-content { min-width: 0; }
    .chat-message-role { font-size: 13px; font-weight: 700; }
    .chat-message-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 6px; }
    .chat-message-head strong { font-size: 13px; }
    .chat-message-head span { color: var(--muted); font-size: 11px; }
    .chat-message-body { color: var(--text); font-size: 13px; line-height: 1.58; overflow-wrap: anywhere; }
    .chat-message-body > :first-child { margin-top: 0; }
    .chat-message-body > :last-child { margin-bottom: 0; }
    .chat-message-body p { margin: 0 0 8px; }
    .chat-message-body ul, .chat-message-body ol { margin: 6px 0 10px; padding-left: 21px; }
    .chat-message-body li { margin: 4px 0; }
    .chat-message-body h1, .chat-message-body h2, .chat-message-body h3 { margin: 12px 0 6px; font-size: 14px; }
    .chat-message-body table { display: block; max-width: 100%; overflow-x: auto; border-collapse: collapse; }
    .chat-message-body th, .chat-message-body td { padding: 6px 8px; border: 1px solid var(--line); white-space: normal; }
    .ai-ask-card { display: grid; gap: 12px; padding: 14px; border: 1px solid var(--line); border-radius: 10px; background: var(--panel); }
    .ai-ask-card textarea { min-height: 150px; background: color-mix(in srgb, var(--bg) 32%, var(--panel)); }
    .ai-assist-note { margin: -2px 0 2px; padding: 9px 11px; border-left: 3px solid var(--accent); border-radius: 5px; background: color-mix(in srgb, var(--accent) 5%, var(--panel)); color: var(--muted); font-size: 12px; line-height: 1.5; }
    .teams-status, .review-status-box {
      min-height: 0;
      flex: 1 1 auto;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      background: var(--panel);
      white-space: pre-wrap;
    }
    .followup-draft {
      min-height: 0;
      max-height: none;
      flex: 1 1 auto;
      margin: 0;
      overflow: auto;
    }
    .meta {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
      overflow-wrap: anywhere;
    }
    .status {
      color: var(--muted);
      min-height: 24px;
    }
    .status.running { color: var(--accent-strong); }
    .status.error { color: var(--danger); }
    .status.ok { color: var(--ok); }
    .progress-panel {
      margin-top: 16px;
      flex: 1 1 auto;
      min-height: 0;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: color-mix(in srgb, var(--bg) 62%, var(--panel));
      padding: 14px;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    .progress-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }
    .progress-title {
      margin: 0;
      font-size: 14px;
      font-weight: 700;
    }
    .progress-state {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 9px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }
    .progress-state.running {
      border-color: var(--accent);
      color: var(--accent-strong);
    }
    .progress-state.ok {
      border-color: var(--ok);
      color: var(--ok);
    }
    .progress-state.error {
      border-color: var(--danger);
      color: var(--danger);
    }
    .progress-state.canceled {
      border-color: var(--muted);
      color: var(--muted);
    }
    .progress-list {
      display: grid;
      gap: 12px;
      margin: 0;
      padding: 0;
      list-style: none;
      overflow: auto;
      min-height: 0;
      flex: 1 1 auto;
      align-content: start;
    }
    .review-preflight-card {
      position: relative;
      display: grid;
      gap: 11px;
      overflow: hidden;
      padding: 13px 14px;
      border: 1px solid color-mix(in srgb, var(--accent) 34%, var(--line));
      border-radius: 8px;
      background: color-mix(in srgb, var(--accent) 4%, var(--panel));
      box-shadow: 0 5px 18px rgba(15, 23, 42, .06);
    }
    .review-preflight-card::before {
      content: "";
      position: absolute;
      inset: 0 auto 0 0;
      width: 3px;
      background: var(--accent);
    }
    .review-preflight-card[data-state="ok"]::before { background: var(--ok); }
    .review-preflight-card[data-state="error"]::before { background: var(--danger); }
    .review-preflight-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .review-preflight-head > span:first-child { display: flex; align-items: center; gap: 9px; min-width: 0; }
    .review-preflight-head > span:first-child > span:last-child { display: grid; gap: 2px; min-width: 0; }
    .review-preflight-spinner {
      width: 16px;
      height: 16px;
      flex: 0 0 auto;
      border: 2px solid color-mix(in srgb, var(--accent) 24%, var(--line));
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: review-preflight-spin .8s linear infinite;
    }
    .review-preflight-card[data-state="ok"] .review-preflight-spinner,
    .review-preflight-card[data-state="error"] .review-preflight-spinner {
      animation: none;
      border: 0;
      display: grid;
      place-items: center;
      color: var(--ok);
    }
    .review-preflight-card[data-state="ok"] .review-preflight-spinner::before { content: "✓"; font-weight: 800; }
    .review-preflight-card[data-state="error"] .review-preflight-spinner { color: var(--danger); }
    .review-preflight-card[data-state="error"] .review-preflight-spinner::before { content: "!"; font-weight: 800; }
    .review-preflight-track { height: 4px; overflow: hidden; border-radius: 999px; background: var(--line); }
    .review-preflight-track > span { display: block; width: 38%; height: 100%; border-radius: inherit; background: var(--accent); transition: width 180ms ease; }
    .review-preflight-card[data-state="ok"] .review-preflight-track > span { width: 100%; background: var(--ok); }
    .review-preflight-card[data-state="error"] .review-preflight-track > span { width: 100%; background: var(--danger); }
    @keyframes review-preflight-spin { to { transform: rotate(360deg); } }
    .job-card {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      padding: 12px;
    }
    .job-card.maximized {
      position: fixed; inset: 18px; z-index: 80; display: flex; flex-direction: column;
      padding: 18px; box-shadow: 0 24px 80px rgba(0,0,0,.35);
    }
    .job-card.maximized .job-events { max-height: none; flex: 1 1 auto; }
    .job-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 10px;
    }
    .job-leading {
      display: flex;
      align-items: flex-start;
      gap: 10px;
      min-width: 0;
    }
    .job-title-block {
      min-width: 0;
    }
    .job-title-block strong {
      display: block;
      overflow-wrap: anywhere;
      line-height: 1.25;
    }
    .job-status-actions {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 6px;
      flex: 0 0 auto;
      margin-left: auto;
      padding-left: 8px;
      min-width: max-content;
    }
    .job-control {
      min-height: 32px;
      padding: 5px 10px;
      line-height: 1;
    }
    .job-status-icon {
      width: 14px;
      height: 14px;
      min-width: 14px;
      min-height: 14px;
      margin-top: 3px;
      padding: 0;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 1px solid currentColor;
      border-radius: 999px;
      color: var(--muted);
      background: transparent;
      font-size: 9px;
      font-weight: 700;
    }
    .job-status-icon.running {
      border-color: var(--accent);
      color: var(--accent-strong);
    }
    .job-status-icon.running::before {
      content: "";
      width: 6px;
      height: 6px;
      border-radius: 999px;
      background: var(--accent);
    }
    .job-status-icon.ok {
      border-color: var(--ok);
      color: var(--ok);
    }
    .job-status-icon.ok::before {
      content: "\\2713";
    }
    .job-status-icon.error {
      border-color: var(--danger);
      color: var(--danger);
    }
    .job-status-icon.error::before {
      content: "!";
    }
    .job-status-icon.canceled {
      border-color: var(--muted);
      color: var(--muted);
    }
    .job-status-icon.canceled::before {
      content: "\\25A0";
    }
    .job-events {
      display: grid;
      gap: 7px;
      max-height: min(280px, 36vh);
      margin: 0;
      padding: 0 4px 0 0;
      overflow: auto;
      list-style: none;
    }
    .progress-item {
      display: grid;
      grid-template-columns: 18px minmax(0, 1fr);
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }
    .progress-dot {
      width: 10px;
      height: 10px;
      margin-top: 4px;
      border: 1px solid var(--line);
      border-radius: 50%;
      background: var(--panel);
    }
    .progress-item.active {
      color: var(--text);
      font-weight: 700;
    }
    .progress-item.active .progress-dot {
      border-color: var(--accent);
      background: var(--accent);
    }
    .progress-item.done .progress-dot {
      border-color: var(--ok);
      background: var(--ok);
    }
    .progress-item.error .progress-dot {
      border-color: var(--danger);
      background: var(--danger);
    }
    .progress-detail {
      margin-top: 12px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
      overflow-wrap: anywhere;
    }
    .preview-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }
    .preview-title {
      min-width: 0;
      display: grid;
      gap: 4px;
    }
    .preview-actions {
      display: flex;
      flex: 0 0 auto;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .preview-actions button:disabled {
      cursor: not-allowed;
    }
    .icon-action {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 34px;
      min-height: 32px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 4px 8px;
      background: color-mix(in srgb, var(--bg) 70%, var(--panel));
      color: var(--text);
      font-size: 17px;
      line-height: 1;
    }
    .icon-action:hover {
      background: color-mix(in srgb, var(--accent) 14%, transparent);
    }
    .icon-action svg {
      width: 17px;
      height: 17px;
      fill: none;
      stroke: currentColor;
      stroke-width: 1.8;
      stroke-linecap: round;
      stroke-linejoin: round;
    }
    .visually-hidden {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }
    .markdown-preview {
      flex: 1 1 auto;
      min-height: 0;
      overflow: auto;
      padding: 18px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--text);
      line-height: 1.68;
      font-size: 15px;
    }
    .markdown-preview.empty {
      display: grid;
      place-items: start;
      color: #e5e7eb;
      background: var(--code);
      border-color: transparent;
      font-family: Consolas, "Courier New", monospace;
      font-size: 13px;
    }
    .markdown-preview h1,
    .markdown-preview h2,
    .markdown-preview h3 {
      margin: 18px 0 10px;
      padding-bottom: 6px;
      border-bottom: 1px solid var(--line);
      line-height: 1.28;
    }
    .markdown-preview h1 { font-size: 22px; }
    .markdown-preview h2 { font-size: 18px; }
    .markdown-preview h3 { font-size: 16px; }
    .markdown-preview p {
      margin: 0 0 12px;
    }
    .markdown-preview ul,
    .markdown-preview ol {
      margin: 0 0 12px 22px;
      padding: 0;
    }
    .markdown-preview li {
      margin: 6px 0;
    }
    .markdown-preview code {
      background: color-mix(in srgb, var(--bg) 76%, var(--panel));
      border: 1px solid var(--line);
      border-radius: 4px;
      padding: 1px 4px;
      font-family: Consolas, "Courier New", monospace;
      font-size: 0.92em;
    }
    .markdown-preview pre {
      min-height: auto;
      max-height: none;
      margin: 10px 0 14px;
      white-space: pre;
    }
    .markdown-preview pre code {
      border: 0;
      background: transparent;
      padding: 0;
      color: inherit;
    }
    .markdown-preview table {
      width: 100%;
      border-collapse: collapse;
      margin: 12px 0 16px;
      font-size: 13px;
    }
    .markdown-preview th,
    .markdown-preview td {
      border: 1px solid var(--line);
      padding: 8px;
      vertical-align: top;
      overflow-wrap: anywhere;
    }
    .markdown-preview th {
      background: color-mix(in srgb, var(--bg) 70%, var(--panel));
      text-align: left;
    }
    .markdown-preview blockquote {
      margin: 10px 0;
      padding: 8px 12px;
      border-left: 3px solid var(--accent);
      background: color-mix(in srgb, var(--bg) 78%, var(--panel));
      color: var(--muted);
    }
    .markdown-preview a {
      color: var(--accent-strong);
      overflow-wrap: anywhere;
    }
    .markdown-preview details {
      margin: 10px 0 14px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: color-mix(in srgb, var(--bg) 52%, var(--panel));
      overflow: hidden;
    }
    .markdown-preview details > summary {
      cursor: pointer;
      padding: 9px 12px;
      color: var(--text);
      font-weight: 700;
      background: color-mix(in srgb, var(--bg) 74%, var(--panel));
      border-bottom: 1px solid var(--line);
      user-select: none;
    }
    .markdown-preview details:not([open]) > summary {
      border-bottom: 0;
    }
    .markdown-preview details > :not(summary) {
      margin-left: 12px;
      margin-right: 12px;
    }
    .markdown-preview .anchor-target {
      display: block;
      position: relative;
      top: -12px;
      height: 0;
      overflow: hidden;
    }
    .report-tabbed-preview {
      display: flex;
      flex-direction: column;
      gap: 12px;
      height: 100%;
      overflow: hidden;
    }
    .report-tab-title {
      flex: 0 0 auto;
      color: var(--text);
      font-size: 18px;
      font-weight: 800;
      line-height: 1.25;
      padding: 2px 4px 0;
    }
    .report-tabbar {
      flex: 0 0 auto;
      display: flex;
      gap: 6px;
      padding: 3px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: color-mix(in srgb, var(--bg) 70%, var(--panel));
      overflow-x: auto;
    }
    .report-tab {
      min-height: 32px;
      border: 0;
      border-radius: 5px;
      padding: 6px 10px;
      background: transparent;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
      white-space: nowrap;
    }
    .report-tab.active {
      background: var(--panel);
      color: var(--accent-strong);
      box-shadow: 0 1px 3px rgba(15, 23, 42, 0.12);
    }
    .report-tab-panel {
      flex: 1 1 auto;
      min-height: 0;
      overflow: auto;
      padding: 4px 12px 18px;
      scrollbar-gutter: stable;
    }
    .report-tab-panel[hidden] {
      display: none;
    }
    .report-finding-details {
      margin: 0 0 12px;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }
    .report-finding-summary {
      display: flex;
      align-items: flex-start;
      gap: 10px;
      padding: 12px 14px;
      color: var(--text);
      font-weight: 800;
      line-height: 1.4;
      cursor: pointer;
      list-style: none;
      user-select: none;
    }
    .report-finding-summary::-webkit-details-marker { display: none; }
    .report-finding-summary-main { min-width: 0; display: grid; gap: 7px; flex: 1 1 auto; }
    .report-finding-title { display: block; overflow-wrap: anywhere; }
    .report-finding-preview { display: grid; gap: 4px; color: var(--muted); font-size: 12px; font-weight: 400; line-height: 1.45; }
    .report-finding-preview-line {
      display: -webkit-box;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 2;
      overflow: hidden;
    }
    .report-finding-preview-line strong { color: var(--text); font-weight: 700; }
    .report-finding-more { flex: 0 0 auto; color: var(--accent-strong); font-size: 12px; font-weight: 700; white-space: nowrap; }
    .report-finding-details[open] .report-finding-more::before { content: "Less"; }
    .report-finding-details:not([open]) .report-finding-more::before { content: "More"; }
    .report-finding-body { padding: 2px 14px 14px; border-top: 1px solid var(--line); }
    .report-origin-badge {
      display: inline-flex;
      align-items: center;
      margin-right: 5px;
      padding: 2px 7px;
      border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--line));
      border-radius: 999px;
      background: color-mix(in srgb, var(--accent) 6%, var(--panel));
      color: var(--accent-strong);
      font-size: 11px;
      font-weight: 800;
      white-space: nowrap;
    }
    .report-finding-summary::-webkit-details-marker { display: none; }
    .report-finding-summary::before {
      content: "";
      flex: 0 0 8px;
      width: 8px;
      height: 8px;
      margin-top: 5px;
      border-right: 2px solid var(--accent-strong);
      border-bottom: 2px solid var(--accent-strong);
      transform: rotate(-45deg);
      transition: transform 140ms ease;
    }
    .report-finding-details[open] > .report-finding-summary {
      border-bottom: 1px solid var(--line);
    }
    .report-finding-details[open] > .report-finding-summary::before {
      transform: rotate(45deg);
    }
    .report-finding-summary:hover,
    .report-finding-summary:focus-visible {
      background: color-mix(in srgb, var(--accent) 6%, var(--panel));
    }
    .report-finding-body { padding: 2px 14px 14px; }
    .report-finding-body > :last-child { margin-bottom: 0; }
    .preview-modal-backdrop {
      position: fixed;
      inset: 0;
      z-index: 30;
      display: grid;
      place-items: center;
      padding: 24px;
      background: rgba(17, 24, 39, 0.56);
    }
    .preview-modal-backdrop[hidden] {
      display: none;
    }
    .preview-modal-dialog {
      width: min(var(--dialog-l), calc(100vw - 48px));
      height: min(860px, calc(100vh - 48px));
      display: flex;
      flex-direction: column;
      min-height: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: 0 18px 54px rgba(0, 0, 0, 0.30);
    }
    .preview-modal-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
    }
    .preview-modal-body {
      flex: 1 1 auto;
      min-height: 0;
      margin: 16px;
    }
    pre {
      margin: 0;
      padding: 14px;
      overflow: auto;
      min-height: 0;
      flex: 1 1 auto;
      max-height: none;
      border-radius: 6px;
      background: var(--code);
      color: #e5e7eb;
      font-family: Consolas, "Courier New", monospace;
      font-size: 13px;
      line-height: 1.5;
      white-space: pre-wrap;
    }
    /* Four-column desktop stays unchanged above this point; below it the
       minimum column widths no longer fit without horizontal compression. */
    @media (max-width: 1399px) {
      body {
        overflow: hidden;
      }
      main {
        --projects-col: minmax(260px, 320px);
        grid-template-columns: var(--projects-col) minmax(0, 1fr);
        height: calc(100vh - 64px);
        min-height: 0;
        overflow: hidden;
        align-items: stretch;
      }
      main.projects-collapsed {
        --projects-col: 48px;
      }
      .workspace {
        grid-column: 2;
        display: grid;
        grid-template-columns: minmax(280px, 0.95fr) minmax(340px, 1.05fr);
        grid-template-rows: minmax(0, 1fr) var(--history-row, minmax(220px, 0.55fr));
        gap: 16px;
        min-height: 0;
        height: 100%;
        overflow: hidden;
      }
      main.history-collapsed .workspace {
        --history-row: 48px;
      }
      .run-panel {
        grid-column: 1;
        grid-row: 1 / span 2;
      }
      .preview-panel {
        grid-column: 2;
        grid-row: 1;
      }
      .history-panel {
        grid-column: 2;
        grid-row: 2;
        height: auto;
        min-height: 0;
      }
      .wide-panel {
        grid-column: 1 / -1;
      }
      aside {
        height: 100%;
        max-height: none;
      }
      pre {
        min-height: 0;
      }
    }
    @media (max-width: 960px) {
      .job-card.maximized { inset: 8px; padding: 12px; }
      .job-head { flex-direction: column; }
      .job-status-actions { width: 100%; min-width: 0; margin-left: 0; padding-left: 0; justify-content: flex-start; flex-wrap: wrap; }
      body {
        overflow: auto;
      }
      main {
        grid-template-columns: 1fr;
        height: auto;
        min-height: calc(100vh - 64px);
        overflow: visible;
      }
      .workspace {
        display: contents;
      }
      aside, .run-panel, .preview-panel, .history-panel, .wide-panel {
        grid-column: auto;
        grid-row: auto;
        height: auto;
        max-height: none;
      }
      .side-panel.collapsed {
        min-height: 48px;
        height: 48px;
      }
      .thread-modal-backdrop {
        padding: 12px;
        align-items: stretch;
      }
      .thread-modal-dialog {
        width: calc(100vw - 24px);
        height: calc(100vh - 24px);
        max-height: calc(100vh - 24px);
        padding: 14px;
      }
      .thread-layout, .thread-pane.tools {
        grid-template-columns: 1fr;
        grid-template-rows: auto;
        overflow: auto;
      }
      .thread-layout > .thread-column:first-child,
      .thread-layout > .thread-reply-column,
      .thread-layout > .thread-followup-column { grid-column: 1; grid-row: auto; }
      .thread-messages, .followup-draft {
        min-height: 220px;
      }
      pre { min-height: 320px; }
      .grid { grid-template-columns: 1fr; }
    }
    .workflow-launch { min-height: 34px; padding: 6px 10px; }
    .app-health {
      min-height: 30px;
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 4px 9px;
      border-color: var(--line);
      border-radius: 999px;
      color: var(--muted);
      font-size: 12px;
    }
    .health-dot {
      width: 8px;
      height: 8px;
      flex: 0 0 auto;
      border-radius: 50%;
      background: var(--muted);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--muted) 14%, transparent);
    }
    .app-health[data-status="healthy"] { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 38%, var(--line)); }
    .app-health[data-status="healthy"] .health-dot { background: var(--ok); box-shadow: 0 0 0 3px color-mix(in srgb, var(--ok) 16%, transparent); }
    .app-health[data-status="degraded"], .app-health[data-status="unhealthy"] { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 38%, var(--line)); }
    .app-health[data-status="degraded"] .health-dot, .app-health[data-status="unhealthy"] .health-dot { background: var(--danger); }
    .health-detail-dialog { width: min(640px, calc(100vw - 32px)); }
    .health-detail-status { display: flex; align-items: center; gap: 8px; margin: 2px 0 0; color: var(--muted); }
    .health-detail-checks { display: grid; gap: 8px; min-height: 0; overflow: auto; }
    .health-detail-check {
      display: grid;
      grid-template-columns: 10px minmax(0, 1fr);
      gap: 9px;
      padding: 11px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: color-mix(in srgb, var(--bg) 42%, var(--panel));
    }
    .health-detail-check .health-dot { margin-top: 4px; }
    .health-detail-check strong { display: block; font-size: 13px; }
    .health-detail-check span { display: block; margin-top: 2px; color: var(--muted); font-size: 12px; line-height: 1.45; }
    .workflow-modal {
      position: fixed; inset: 0; z-index: 1200; padding: 20px;
      background: color-mix(in srgb, #07111f 68%, transparent);
    }
    .workflow-dialog {
      width: min(var(--dialog-full), 100%); height: calc(100vh - 40px); margin: 0 auto;
      display: grid; grid-template-rows: auto 1fr; overflow: hidden;
      border: 1px solid var(--line); border-radius: 12px; background: var(--panel);
      box-shadow: 0 24px 80px rgba(0,0,0,.28);
    }
    .issue-review-dialog { grid-template-rows: auto auto 1fr; }
    .issue-history-tabs { display: flex; gap: 6px; padding: 8px 20px 0; border-bottom: 1px solid var(--line); }
    .issue-history-tab { min-height: 38px; border: 0; border-radius: 6px 6px 0 0; background: transparent; color: var(--muted); }
    .issue-history-tab.active { color: var(--accent-strong); border-bottom: 2px solid var(--accent); }
    .issue-history-overview { grid-row: 3; min-width: 0; min-height: 0; overflow: auto; padding: 20px; background: color-mix(in srgb, var(--bg) 55%, var(--panel)); }
    .issue-history-overview[hidden],
    .workflow-body[hidden] { display: none; }
    .sprint-overview-grid { display: grid; grid-template-columns: 1fr; gap: 14px; }
    .sprint-overview-card { display: grid; gap: 12px; min-width: 0; padding: 16px; border: 1px solid var(--line); border-radius: 10px; background: var(--panel); box-shadow: 0 3px 12px rgba(15,23,42,.04); }
    .sprint-overview-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
    .sprint-overview-footer button { width: auto; min-width: 148px; }
    .sprint-overview-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
    .sprint-overview-head strong { overflow-wrap: anywhere; font-size: 18px; line-height: 1.3; }
    .sprint-overview-head .meta,
    .sprint-overview-footer .meta { font-size: 13px; }
    .sprint-overview-head .count-pill { font-size: 12px; }
    .sprint-overview-metrics { display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 8px; }
    .sprint-overview-metric { padding: 13px 14px; border: 1px solid color-mix(in srgb, var(--line) 72%, transparent); border-radius: 9px; background: color-mix(in srgb, var(--bg) 65%, var(--panel)); }
    .sprint-overview-metric .meta { font-size: 14px; }
    .sprint-overview-metric strong { display: block; margin-top: 4px; font-size: 24px; line-height: 1.2; }
    .sprint-application-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
    .sprint-application-card {
      min-width: 0; display: grid; gap: 9px; padding: 12px;
      border: 1px solid var(--line); border-radius: 8px;
      background: color-mix(in srgb, var(--bg) 42%, var(--panel)); color: var(--text); text-align: left;
    }
    .sprint-application-card:hover, .sprint-application-card:focus-visible { border-color: var(--accent); outline: 0; box-shadow: 0 4px 12px rgba(15,23,42,.08); }
    .sprint-application-head, .sprint-application-progress-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-width: 0; }
    .sprint-application-head strong { min-width: 0; overflow-wrap: anywhere; font-size: 16px; }
    .sprint-application-head .status-chip { font-size: 12px; }
    .sprint-application-progress-head .meta { font-size: 13px; }
    .sprint-application-percent { color: var(--accent-strong); font-size: 22px; font-weight: 800; }
    .sprint-application-progress { height: 5px; overflow: hidden; border-radius: 999px; background: var(--line); }
    .sprint-application-progress span { display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg, #1971c2, #16845b); }
    .sprint-application-stats { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 5px 8px; }
    .sprint-application-stat { min-width: 0; color: var(--muted); font-size: 13px; line-height: 1.45; }
    .sprint-application-stat strong { margin-left: 3px; color: var(--text); }
    .sprint-application-card[data-ready="true"] { border-color: color-mix(in srgb, var(--ok) 42%, var(--line)); background: color-mix(in srgb, var(--ok) 5%, var(--panel)); }
    .sprint-application-card[data-application="Unmapped"] { border-color: color-mix(in srgb, var(--danger) 42%, var(--line)); }
    @media (max-width: 1180px) { .sprint-application-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
    @media (max-width: 660px) { .sprint-application-grid { grid-template-columns: 1fr; } }
    .cycle-history-card > .timeline-card { margin: 10px 0 0 14px; background: color-mix(in srgb, var(--bg) 55%, var(--panel)); }
    .cycle-history-card h5 { margin: 14px 0 6px 14px; }
    .workflow-head {
      display: flex; align-items: center; justify-content: space-between; gap: 16px;
      padding: 16px 20px; border-bottom: 1px solid var(--line);
    }
    .workflow-head h2, .workflow-head p { margin: 0; }
    .workflow-body { grid-row: 3; min-height: 0; display: grid; grid-template-columns: minmax(340px, 38%) 1fr; }
    .workflow-list-pane { min-height: 0; overflow: auto; padding: 16px; border-right: 1px solid var(--line); }
    .workflow-detail-pane { min-height: 0; overflow: auto; padding: 20px; background: color-mix(in srgb, var(--bg) 55%, var(--panel)); container-name: issue-detail; container-type: inline-size; }
    .workflow-toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 14px; }
    .workflow-toolbar input { flex: 1; }
    .issue-review-cards { display: grid; gap: 10px; }
    .issue-review-card {
      display: grid; gap: 10px; padding: 13px 14px; cursor: pointer;
      border: 1px solid var(--line); border-radius: 9px; background: var(--panel);
      transition: border-color .16s ease, box-shadow .16s ease, transform .16s ease;
    }
    .issue-review-card:hover, .issue-review-card:focus-visible {
      outline: 0; border-color: color-mix(in srgb, var(--accent) 62%, var(--line));
      box-shadow: 0 5px 16px rgba(15, 23, 42, .08); transform: translateY(-1px);
    }
    .issue-review-card.selected { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 5%, var(--panel)); }
    .issue-review-card-head, .issue-review-card-foot {
      display: flex; align-items: center; justify-content: space-between; gap: 10px;
    }
    .issue-review-key { color: var(--accent-strong); font-size: 14px; }
    .issue-review-summary {
      display: -webkit-box; overflow: hidden; color: var(--text); line-height: 1.45;
      -webkit-box-orient: vertical; -webkit-line-clamp: 2;
    }
    .issue-review-progress { display: flex; gap: 6px; flex-wrap: wrap; }
    .issue-review-progress .handling-chip { min-height: 22px; padding: 1px 7px; }
    .issue-review-updated { white-space: nowrap; }
    .status-chip, .severity-chip, .handling-chip {
      display: inline-flex; align-items: center; min-height: 24px; padding: 2px 8px;
      border-radius: 999px; border: 1px solid var(--line); font-size: 12px; white-space: nowrap;
    }
    .status-chip[data-status="passed"] { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 45%, var(--line)); }
    .status-chip[data-status="handling"], .status-chip[data-status="rescan-required"] { color: #b54708; }
    .severity-chip.critical { color: #b42318; font-weight: 700; }
    .severity-chip.high { color: #c2410c; font-weight: 700; }
    .issue-overview { margin: 0 0 16px; padding: 18px; border: 1px solid var(--line); border-radius: 12px; background: var(--panel); box-shadow: 0 5px 18px rgba(15, 23, 42, .05); }
    .issue-review-header { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: start; gap: 10px 18px; padding-bottom: 14px; border-bottom: 1px solid var(--line); }
    .issue-review-identity { min-width: 0; }
    .issue-review-controls { display: grid; justify-items: end; gap: 10px; }
    .issue-review-actions { display: flex; align-items: center; justify-content: flex-end; gap: 10px; flex-wrap: wrap; }
    .issue-review-header h2 { margin: 0; font-size: 21px; line-height: 1.34; overflow-wrap: anywhere; }
    .issue-review-header .status-chip { margin-top: 2px; }
    .issue-hero { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: start; gap: 18px; padding-bottom: 14px; border-bottom: 1px solid var(--line); }
    .issue-hero-copy { min-width: 0; }
    .issue-hero-title-row { display: flex; align-items: flex-start; gap: 10px; min-width: 0; }
    .issue-hero h2 { margin: 0; font-size: 21px; line-height: 1.34; overflow-wrap: anywhere; }
    .issue-hero-meta { display: flex; gap: 7px 14px; flex-wrap: wrap; margin-top: 7px; }
    .issue-hero .status-chip { margin-top: 2px; }
    .metric-grid { display: grid; grid-template-columns: repeat(4,minmax(0,1fr)); gap: 10px; margin: 14px 0 0; }
    .metric-card { min-height: 96px; overflow: hidden; padding: 12px; border: 1px solid var(--line); border-radius: 9px; background: color-mix(in srgb, var(--bg) 24%, var(--panel)); }
    button.metric-card { width: 100%; color: var(--text); text-align: left; cursor: pointer; }
    button.metric-card:hover, button.metric-card:focus-visible { border-color: var(--accent); box-shadow: 0 4px 14px rgba(15, 23, 42, .08); outline: 0; }
    button.metric-card:disabled { cursor: default; opacity: 1; }
    .metric-card-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
    .metric-card-head strong { margin: 0; font-size: 20px; }
    .metric-ratio { display: flex; justify-content: space-between; gap: 8px; margin-top: 7px; color: var(--muted); font-size: 12px; }
    .metric-bar { position: relative; display: block; width: 100%; height: 6px; min-height: 6px; margin-top: 8px; overflow: hidden; border-radius: 999px; background: color-mix(in srgb, var(--line) 70%, transparent); }
    .metric-bar > span { position: absolute; inset: 0 auto 0 0; display: block; width: var(--completion, 0%); max-width: 100%; border-radius: inherit; background: linear-gradient(90deg, var(--accent), var(--ok)); transition: width .25s ease; }
    .issue-readiness { display: flex; align-items: center; gap: 8px; min-width: 0; padding: 8px 10px; border-radius: 8px; background: color-mix(in srgb, var(--bg) 55%, var(--panel)); }
    .issue-readiness-dot { width: 8px; height: 8px; flex: 0 0 auto; border-radius: 50%; background: #d97706; }
    .issue-readiness.ready .issue-readiness-dot { background: var(--ok); }
    .metric-summary-card { display: grid; grid-template-rows: repeat(2, minmax(0, 1fr)); padding: 0; }
    .metric-summary-row {
      width: 100%;
      min-height: 0;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 10px 12px;
      border: 0;
      border-radius: 0;
      background: transparent;
      color: var(--text);
      text-align: left;
    }
    button.metric-summary-row { cursor: pointer; }
    button.metric-summary-row:hover, button.metric-summary-row:focus-visible { background: color-mix(in srgb, var(--accent) 7%, var(--panel)); outline: 0; }
    button.metric-summary-row:disabled { cursor: default; opacity: 1; }
    .metric-summary-row + .metric-summary-row { border-top: 1px solid var(--line); }
    .metric-summary-row strong { flex: 0 0 auto; font-size: 18px; }
    @container issue-detail (max-width: 760px) {
      .issue-review-header { grid-template-columns: 1fr; }
      .issue-review-controls { justify-items: start; }
      .issue-review-actions { justify-content: flex-start; }
      .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @container issue-detail (max-width: 420px) {
      .metric-grid { grid-template-columns: 1fr; }
      .issue-review-actions > button { flex: 1 1 auto; }
    }
    .workflow-tabs {
      display: flex;
      gap: 6px;
      margin: 16px 0 10px;
      overflow-x: auto;
      border-bottom: 1px solid var(--line);
      scrollbar-width: thin;
    }
    .workflow-tabs .workflow-tab { flex: 0 0 auto; }
    .workflow-tab { border: 0; border-radius: 6px 6px 0 0; background: transparent; color: var(--muted); }
    .workflow-tab.active { color: var(--accent-strong); border-bottom: 2px solid var(--accent); }
    .workflow-tab:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
    .workflow-section { margin: 0 0 22px; padding: 18px; border: 1px solid var(--line); border-radius: 10px; background: var(--panel); }
    .workflow-section[hidden] { display: none; }
    .workflow-section-title { margin: 0 0 14px; padding: 0; font-size: 18px; line-height: 1.35; }
    .finding-card, .timeline-card, .draft-card, .discussion-card {
      margin-bottom: 10px; padding: 14px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel);
    }
    .finding-head, .draft-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
    .finding-head-main { min-width: 0; }
    .finding-summary { display: -webkit-box; margin: 10px 0 0; overflow: hidden; color: var(--muted); line-height: 1.5; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
    .finding-summary.expanded { display: block; white-space: pre-wrap; }
    .finding-evidence-preview {
      display: grid;
      gap: 7px;
      margin-top: 11px;
      padding: 10px 11px;
      border: 1px solid color-mix(in srgb, var(--accent) 12%, var(--line));
      border-radius: 7px;
      background: color-mix(in srgb, var(--accent) 3%, var(--panel));
    }
    .finding-evidence-line {
      display: grid;
      grid-template-columns: 68px minmax(0, 1fr);
      gap: 8px;
      color: var(--muted);
      line-height: 1.5;
    }
    .finding-evidence-label { color: var(--accent-strong); font-size: 12px; font-weight: 700; }
    .finding-evidence-text {
      display: -webkit-box;
      overflow: hidden;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 2;
      white-space: normal;
    }
    .finding-evidence-preview.expanded .finding-evidence-text { display: block; white-space: pre-wrap; }
    .finding-summary-toggle { min-height: 28px; margin-top: 4px; padding: 3px 7px; border: 0; background: transparent; color: var(--accent-strong); }
    .finding-head-action { flex: 0 0 auto; min-width: 132px; }
    .finding-actions { display: flex; gap: 7px; flex-wrap: wrap; margin-top: 10px; }
    .finding-actions textarea { min-height: 74px; }
    .finding-handling-form { display: grid; grid-template-columns: minmax(280px, .68fr) minmax(420px, 1.32fr); gap: 18px; margin-top: 16px; }
    .finding-handling-form:not(.followup-active) { grid-template-columns: minmax(280px, 460px); }
    .finding-handling-primary, .finding-handling-secondary { display: grid; gap: 9px; align-content: start; min-width: 0; }
    .finding-handling-secondary { min-height: 100%; padding-left: 18px; border-left: 1px solid var(--line); }
    .finding-handling-form:not(.followup-active) .finding-handling-secondary { display: none; }
    .finding-handling-primary label, .finding-handling-secondary label { display: grid; gap: 6px; color: var(--muted); font-size: 12px; }
    .finding-handling-primary textarea { min-height: 132px; }
    .finding-handling-primary, .finding-handling-secondary { align-self: stretch; grid-auto-rows: min-content; }
    .finding-handling-form.followup-active .finding-handling-primary,
    .finding-handling-form.followup-active .finding-handling-secondary { min-height: 250px; }
    .summary-input { min-height: 58px; resize: vertical; line-height: 1.45; }
    .required-mark, .required-when-active { color: var(--danger); font-weight: 700; }
    .field-help, .field-message { display: block; margin-top: 4px; color: var(--muted); font-size: 12px; line-height: 1.4; }
    .field-message.error, .validation-summary.error { color: var(--danger); }
    [aria-invalid="true"] { border-color: var(--danger) !important; box-shadow: 0 0 0 2px color-mix(in srgb, var(--danger) 15%, transparent); }
    .validation-summary { margin: 10px 0 0; padding: 10px 12px; border: 1px solid color-mix(in srgb, var(--danger) 35%, var(--line)); border-radius: 7px; background: color-mix(in srgb, var(--danger) 5%, var(--panel)); }
    .followup-fields { display: grid; gap: 14px; padding: 16px; border: 1px solid var(--line); border-radius: 10px; background: color-mix(in srgb, var(--accent) 2.5%, var(--panel)); }
    .followup-fields[hidden] { display: none; }
    .followup-fields-head { display: grid; gap: 6px; }
    .followup-fields-head input { width: 100%; min-width: min(50ch, 100%); }
    .followup-card-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding-top: 2px; }
    .followup-card-head strong { display: block; margin-bottom: 4px; font-size: 14px; }
    .followup-card-head button { flex: 0 0 auto; min-height: 34px; padding: 6px 12px; }
    .followup-adf-state { min-width: 0; line-height: 1.45; }
    .followup-adf-preview { display: -webkit-box; overflow: hidden; margin-top: 3px; color: var(--muted); -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow-wrap: anywhere; }
    .finding-card.finding-flash { border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 18%, transparent); }
    .handling-guidance { margin-top: 14px; padding: 14px; border: 1px solid var(--line); border-radius: 7px; background: var(--panel); }
    .handling-guidance h4 { margin: 0 0 7px; font-size: 14px; }
    .handling-guidance p { margin: 0 0 9px; color: var(--muted); line-height: 1.5; }
    .handling-guidance ul { margin: 0; padding-left: 19px; color: var(--muted); line-height: 1.55; }
    .adf-editor-shell { border: 1px solid var(--line); border-radius: 8px; overflow: hidden; background: var(--panel); }
    .adf-editor-intro { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin: 0 0 10px; padding: 11px 12px; border: 1px solid var(--line); border-radius: 8px; background: color-mix(in srgb, var(--accent) 4%, var(--panel)); }
    .adf-editor-intro p { margin: 3px 0 0; color: var(--muted); font-size: 12px; line-height: 1.5; }
    .adf-editor-engine { flex: 0 0 auto; min-height: 24px; padding: 2px 8px; border: 1px solid var(--line); border-radius: 999px; color: var(--muted); font-size: 11px; font-weight: 700; white-space: nowrap; }
    .adf-editor-engine.enhanced { border-color: color-mix(in srgb, var(--ok) 45%, var(--line)); color: var(--ok); }
    .adf-toolbar { display: flex; gap: 5px; flex-wrap: wrap; padding: 8px; border-bottom: 1px solid var(--line); }
    .adf-toolbar button { min-height: 30px; padding: 4px 8px; font-size: 12px; }
    .adf-source { min-height: 260px; border: 0; border-radius: 0; font-family: Consolas,monospace; }
    .adf-block-editor { display: grid; gap: 10px; min-height: 300px; padding: 14px; background: color-mix(in srgb, var(--bg) 35%, var(--panel)); }
    .adf-block-editor[hidden] { display: none; }
    .adf-block-empty { display: grid; place-items: center; min-height: 240px; padding: 24px; border: 1px dashed var(--line); border-radius: 8px; color: var(--muted); text-align: center; }
    .adf-block { display: grid; grid-template-columns: 34px minmax(0, 1fr) auto; gap: 10px; align-items: start; padding: 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); }
    .adf-block.dragging { opacity: .48; border-color: var(--accent); }
    .adf-block.drop-target { box-shadow: 0 -3px 0 var(--accent); }
    .adf-block-grip { min-height: 32px; padding: 4px; border: 0; background: transparent; color: var(--muted); cursor: grab; font-size: 20px; line-height: 1; }
    .adf-block-grip:active { cursor: grabbing; }
    .adf-block-body { display: grid; gap: 6px; min-width: 0; }
    .adf-block-type { color: var(--muted); font-size: 11px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }
    .adf-block-input { min-height: 42px; resize: vertical; font-family: inherit; font-size: 14px; line-height: 1.5; }
    .adf-block-complex { padding: 10px; border: 1px dashed var(--line); border-radius: 6px; color: var(--muted); line-height: 1.45; }
    .adf-block-delete { min-height: 32px; padding: 4px 8px; border-color: var(--line); background: transparent; color: var(--danger); }
    .adf-preview { min-height: 260px; padding: 16px; overflow: auto; }
    .adf-preview table { width: 100%; border-collapse: collapse; }
    .adf-preview th, .adf-preview td { border: 1px solid var(--line); padding: 8px; }
    .adf-expand { margin: 10px 0; border: 1px solid var(--line); border-radius: 7px; padding: 8px 10px; }
    .adf-media { display: block; max-width: 100%; height: auto; border-radius: 6px; }
    .draft-editor-grid { display: grid; grid-template-columns: minmax(0,1fr); gap: 14px; }
    .user-admin-dialog { grid-template-rows: auto minmax(0, 1fr); }
    .user-admin-body {
      min-height: 0;
      display: grid;
      grid-template-columns: minmax(390px, 42%) minmax(0, 1fr);
    }
    .user-admin-list-pane {
      min-width: 0;
      min-height: 0;
      display: flex;
      flex-direction: column;
      padding: 16px;
      border-right: 1px solid var(--line);
      background: color-mix(in srgb, var(--bg) 42%, var(--panel));
    }
    .user-admin-toolbar {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 12px;
    }
    .user-admin-toolbar > input { grid-column: 1 / -1; }
    .user-admin-list {
      min-height: 0;
      display: grid;
      align-content: start;
      gap: 8px;
      overflow: auto;
      padding-right: 3px;
      scrollbar-gutter: stable;
    }
    .user-admin-card {
      width: 100%;
      min-width: 0;
      display: grid;
      grid-template-rows: auto auto;
      align-content: center;
      gap: 7px;
      min-height: 82px;
      padding: 13px 14px;
      border-color: var(--line);
      background: var(--panel);
      color: var(--text);
      text-align: left;
    }
    .user-admin-card:hover, .user-admin-card:focus-visible, .user-admin-card.selected {
      border-color: var(--accent);
      background: color-mix(in srgb, var(--accent) 5%, var(--panel));
      outline: 0;
      box-shadow: 0 4px 14px rgba(15,23,42,.08);
    }
    .user-admin-card-head, .user-admin-card-meta, .user-admin-detail-head, .user-admin-form-actions {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }
    .user-admin-card-head strong { min-width: 0; overflow: hidden; font-size: 15px; text-overflow: ellipsis; white-space: nowrap; }
    .user-admin-card-head .status-chip { flex: 0 0 auto; }
    .user-admin-card-meta { justify-content: flex-start; flex-wrap: nowrap; min-width: 0; }
    .user-admin-card-meta .meta { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .user-admin-scope-label { color: var(--muted); font-size: 12px; white-space: nowrap; }
    .user-admin-scope-value { min-width: 0; overflow: hidden; color: var(--text); font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
    .user-status-chip[data-status="active"] { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 45%, var(--line)); }
    .user-status-chip[data-status="suspended"] { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 45%, var(--line)); }
    .user-status-chip[data-status="password-change"] { color: #b54708; border-color: color-mix(in srgb, #b54708 45%, var(--line)); }
    .user-admin-detail-pane {
      min-width: 0;
      min-height: 0;
      overflow: auto;
      padding: 22px;
    }
    .configuration-dialog { grid-template-rows: auto 1fr; }
    .configuration-body { min-height: 0; display: grid; grid-template-columns: 230px minmax(0, 1fr); }
    .configuration-nav {
      min-height: 0; display: flex; flex-direction: column; gap: 6px;
      padding: 16px; border-right: 1px solid var(--line);
      background: color-mix(in srgb, var(--bg) 42%, var(--panel));
    }
    .configuration-nav button { justify-content: flex-start; width: 100%; text-align: left; border-color: transparent; background: transparent; color: var(--muted); }
    .configuration-nav button.active { border-color: color-mix(in srgb, var(--accent) 36%, var(--line)); background: color-mix(in srgb, var(--accent) 8%, var(--panel)); color: var(--accent-strong); }
    .configuration-source { margin-top: auto; padding: 10px; border: 1px solid var(--line); border-radius: 7px; color: var(--muted); font-size: 11px; line-height: 1.45; }
    .configuration-main { min-width: 0; min-height: 0; display: grid; grid-template-rows: auto 1fr; padding: 18px 20px; }
    .configuration-toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
    .configuration-toolbar input { flex: 1; }
    .configuration-content { min-height: 0; overflow: auto; padding-right: 4px; scrollbar-gutter: stable; }
    .configuration-section {
      margin: 0 0 16px;
      padding: 15px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: color-mix(in srgb, var(--bg) 38%, var(--panel));
      box-shadow: 0 3px 12px rgba(15, 23, 42, .035);
    }
    .configuration-section h3 {
      margin: 0 0 12px;
      padding: 0 2px 10px;
      border-bottom: 1px solid var(--line);
      font-size: 15px;
      line-height: 1.35;
    }
    .configuration-field-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 360px), 1fr)); gap: 9px; }
    .configuration-field-card {
      min-width: 0; display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px 10px;
      padding: 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel);
    }
    .configuration-field-card label { min-width: 0; margin: 0; }
    .configuration-field-card .meta { overflow-wrap: anywhere; }
    .configuration-field-card button { align-self: end; min-width: 64px; }
    .configuration-field-card textarea { min-height: 40px; max-height: 84px; resize: vertical; }
    .configuration-project-card { margin-bottom: 12px; padding: 14px; border: 1px solid var(--line); border-radius: 9px; background: var(--panel); }
    .configuration-project-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; margin-bottom: 11px; }
    .configuration-project-head h3 { margin: 0; overflow-wrap: anywhere; }
    .configuration-project-create { margin-bottom: 14px; padding: 14px; border: 1px solid color-mix(in srgb, var(--accent) 38%, var(--line)); border-radius: 9px; background: color-mix(in srgb, var(--accent) 4%, var(--panel)); }
    .configuration-project-create[hidden] { display: none; }
    .configuration-project-create-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
    .configuration-project-create-grid label { margin: 0; }
    .configuration-project-create-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
    .configuration-project-delete { color: var(--danger) !important; border-color: color-mix(in srgb, var(--danger) 40%, var(--line)) !important; }
    .configuration-empty { padding: 28px; border: 1px dashed var(--line); border-radius: 8px; color: var(--muted); text-align: center; }
    .configuration-backup { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 11px; border-bottom: 1px solid var(--line); }
    .configuration-backup:last-child { border-bottom: 0; }
    @media (max-width: 800px) {
      .configuration-body { grid-template-columns: 1fr; overflow: auto; }
      .configuration-nav { min-height: auto; flex-direction: row; flex-wrap: wrap; border-right: 0; border-bottom: 1px solid var(--line); }
      .configuration-source { width: 100%; margin-top: 4px; }
      .configuration-main { min-height: 520px; }
      .configuration-project-create-grid { grid-template-columns: 1fr; }
    }
    .user-admin-detail-head { align-items: flex-start; margin-bottom: 16px; }
    .user-admin-detail-head h3, .user-admin-detail-head p { margin: 0; }
    .user-admin-form { display: grid; gap: 14px; max-width: 780px; }
    .user-admin-form[hidden], .user-admin-empty[hidden] { display: none; }
    .user-admin-form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
    .user-admin-form label, .user-responsible-fieldset { margin: 0; }
    .user-responsible-fieldset > .field-help {
      display: block;
      margin-bottom: 10px;
      padding: 9px 11px;
      border-left: 3px solid var(--accent);
      border-radius: 0 6px 6px 0;
      background: color-mix(in srgb, var(--accent) 6%, var(--panel));
      line-height: 1.5;
    }
    .user-responsible-fieldset.manager-global .user-responsible-add,
    .user-responsible-fieldset.manager-global #managedResponsibleSearch,
    .user-responsible-fieldset.manager-global .user-responsible-options,
    .user-responsible-fieldset.manager-global #managedResponsibleAddError { display: none; }
    .user-active-control {
      min-height: 42px;
      display: flex !important;
      grid-template-columns: none !important;
      flex-direction: row;
      align-items: center;
      gap: 9px !important;
      padding: 9px 11px;
      border: 1px solid var(--line);
      border-radius: 7px;
    }
    .user-active-control input { width: auto; margin: 0; }
    .user-responsible-fieldset { min-width: 0; padding: 14px; border: 1px solid var(--line); border-radius: 9px; }
    .user-responsible-fieldset legend { padding: 0 6px; font-weight: 700; }
    .user-responsible-add { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; margin-top: 10px; }
    .user-responsible-add button { min-width: 76px; }
    .user-responsible-options {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 7px;
      max-height: 230px;
      overflow: auto;
      margin-top: 10px;
      padding: 2px;
    }
    .user-responsible-option {
      min-width: 0;
      display: flex !important;
      grid-template-columns: none !important;
      flex-direction: row;
      align-items: center;
      gap: 8px !important;
      padding: 7px 9px;
      border: 1px solid var(--line);
      border-radius: 7px;
      overflow-wrap: anywhere;
    }
    .user-responsible-option input { width: auto; flex: 0 0 auto; margin: 0; }
    .user-admin-form-actions { justify-content: flex-end; padding-top: 4px; border-top: 1px solid var(--line); }
    .user-admin-empty { min-height: 220px; display: grid; place-items: center; color: var(--muted); text-align: center; }
    .temporary-password {
      width: 100%;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--code);
      color: #f8fafc;
      font-family: Consolas, "Courier New", monospace;
      font-size: 15px;
      letter-spacing: .04em;
    }
    .credential-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
    .user-reset-confirmation { margin-top: 12px; }
    .user-reset-confirmation input { margin-top: 7px; }
    @media (max-width: 900px) {
      .issue-review-header { grid-template-columns: 1fr; }
      .issue-review-controls { justify-items: start; }
      .issue-review-actions { justify-content: flex-start; }
      .workflow-modal { padding: 8px; }
      .workflow-dialog { height: calc(100vh - 16px); }
      .workflow-body { grid-template-columns: 1fr; }
      .workflow-list-pane { max-height: 42vh; border-right: 0; border-bottom: 1px solid var(--line); }
      .metric-grid { grid-template-columns: repeat(2,1fr); }
      .issue-command-bar { align-items: stretch; flex-direction: column; }
      .draft-editor-grid { grid-template-columns: 1fr; }
      .finding-handling-form { grid-template-columns: 1fr; }
      .finding-handling-form:not(.followup-active) { grid-template-columns: 1fr; }
      .finding-handling-secondary { padding-left: 0; padding-top: 14px; border-left: 0; border-top: 1px solid var(--line); }
      .followup-fields-head { grid-template-columns: 1fr; }
      .finding-head-action { min-width: 0; }
      .adf-editor-intro { align-items: stretch; flex-direction: column; }
      .adf-editor-engine { width: fit-content; }
      .user-admin-body { grid-template-columns: 1fr; overflow: auto; }
      .user-admin-list-pane { max-height: 43vh; border-right: 0; border-bottom: 1px solid var(--line); }
      .user-admin-detail-pane { overflow: visible; }
    }
    @media (max-width: 560px) {
      .user-admin-toolbar, .user-admin-form-grid { grid-template-columns: 1fr; }
      .user-admin-detail-pane, .user-admin-list-pane { padding: 14px; }
      .user-admin-form-actions, .credential-actions { align-items: stretch; flex-direction: column-reverse; }
      .user-admin-form-actions button, .credential-actions button { width: 100%; }
    }
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <div class="topbar-brand"><img src="/assets/ttl-jay-crystal-logo.png" alt=""><h1>CodeReviewer</h1></div>
      <div class="meta topbar-meta">
        <button id="issueReviewsBtn" class="secondary workflow-launch" type="button">Issue Reviews</button>
        <button id="pendingJiraBtn" class="secondary workflow-launch" type="button">Pending Jira</button>
        <button id="releaseNotesBtn" class="secondary workflow-launch" type="button">Release Notes</button>
        <button id="changePasswordBtn" class="secondary workflow-launch" type="button">Change password</button>
__USER_MANAGEMENT_BUTTON__
        <button id="appHealthBtn" class="secondary app-health" data-status="checking" type="button" aria-haspopup="dialog"><span class="health-dot" aria-hidden="true"></span><span id="appHealthLabel">Checking</span></button>
        <span>Version <strong id="appVersion">__APP_VERSION__</strong></span>
        <span><strong id="currentRole">__CURRENT_ROLE__</strong> · Signed in as <strong id="currentUser">__CURRENT_USER__</strong> · <a href="/logout">Logout</a></span>
      </div>
    </div>
  </header>

  <main id="appMain" class="projects-collapsed history-collapsed">
    <aside id="projectsAside" class="side-panel project-sidebar collapsed">
      <button id="projectsToggle" class="side-toggle" type="button" aria-expanded="false" title="Show projects">
        <span class="expanded-label">&#x2039;</span>
        <span class="collapsed-label">Projects</span>
      </button>
      <div class="side-content">
        <div class="project-toolbar">
          <h2>Projects</h2>
          <input id="projectSearch" class="search-input" placeholder="Filter projects">
        </div>
        <div id="projects" class="meta">__INITIAL_PROJECTS__</div>
      </div>
    </aside>

    <div class="workspace">
      <section id="runPanel" class="panel run-panel">
        <div class="run-panel-head">
          <div>
            <h2>Run Review</h2>
            <div id="runFormSummary" class="run-form-summary">Review inputs</div>
          </div>
          <button id="runFormToggle" class="icon-action run-form-toggle" type="button" title="Collapse review inputs" aria-label="Collapse review inputs" aria-controls="runFormBody" aria-expanded="true">
            <span class="toggle-glyph" aria-hidden="true">&#x2303;</span>
          </button>
        </div>
        <div id="runFormBody" class="run-form-body">
          <div class="__INPUT_GRID_CLASS__">
            <label>Jira
              <input id="jira" placeholder="ECHNL-8888">
            </label>
__SPRINT_FIELD__
            <label>Report Priority
              <select id="reportMinSeverity">
                <option value="Medium">Medium and above</option>
                <option value="High">High and above</option>
                <option value="Critical">Critical only</option>
                <option value="Low">Low and above</option>
                <option value="Warning">Warning and above</option>
              </select>
            </label>
          </div>
          <div class="actions run-primary-actions" role="group" aria-label="Review actions">
            <button id="runBtn">Run Review</button>
            <button class="secondary" id="refreshBtn" type="button">Refresh Reports</button>
            <button class="secondary" id="coverageBtn" type="button">Sprint Overview</button>
          </div>
          <div id="status" class="status run-action-status" role="status" aria-live="polite"></div>
          <div id="runHint" class="run-guidance"><span class="inline-hint-row"><span class="info-hint"><button class="information-icon" type="button" aria-label="Run Review input guidance" aria-expanded="false" aria-controls="runReviewHintPopover">i</button><span id="runReviewHintPopover" class="information-hint-popover" role="tooltip" hidden>__RUN_HINT__</span></span><span>Review scope <span class="required-mark" aria-hidden="true">*</span> — complete Jira, Sprint, or Filter</span></span></div>
__RELEASE_GATE_PANEL__
        </div>
        <div id="progressPanel" class="progress-panel" tabindex="-1">
          <div class="progress-header">
            <h3 class="progress-title">Progress</h3>
            <span id="progressState" class="progress-state">Idle</span>
          </div>
          <ul id="progressList" class="progress-list"></ul>
          <div id="progressDetail" class="progress-detail">Waiting for a Jira issue or Sprint review.</div>
        </div>
      </section>

      <section class="panel preview-panel">
        <div class="preview-head">
          <div class="preview-title">
            <h2>Report Preview</h2>
          <div id="previewName" class="meta">No report selected.</div>
          </div>
          <div class="preview-actions">
            <button id="previewMaximizeBtn" class="icon-action" type="button" title="Maximize preview" aria-label="Maximize preview">&#x26F6;</button>
            <button id="previewCompareBtn" class="secondary small-action" type="button" disabled>Compare</button>
            <button id="previewOpenBtn" class="secondary small-action" type="button" disabled>Preview</button>
            <a id="previewDownloadLink" class="download-link" href="#" hidden>Download</a>
          </div>
        </div>
        <div id="preview" class="markdown-preview empty">No report generated yet.</div>
      </section>

      <section id="historyAside" class="panel side-panel history-panel collapsed">
        <button id="historyToggle" class="side-toggle" type="button" aria-expanded="false" title="Show report history">
          <span class="expanded-label">&#x203A;</span>
          <span class="collapsed-label">Reports</span>
        </button>
        <div class="side-content">
          <h2>Report History</h2>
          <div class="history-tools-card">
            <div class="report-filter">
              <input id="reportSearch" class="search-input" placeholder="Search report, Jira or responsible" aria-label="Search report history">
              <div class="report-filter-row">
                <select id="reportDays" class="search-input" title="Report history period" aria-label="Report history period">
                  <option value="14" selected>Last 2 weeks</option>
                  <option value="30">Last 30 days</option>
                  <option value="60">Last 60 days</option>
                  <option value="0">All records</option>
                </select>
                <button class="secondary" id="refreshDownloadsBtn" type="button">Refresh</button>
              </div>
            </div>
            <div class="history-tabs" role="tablist" aria-label="Report history type">
              <button class="history-tab active" type="button" data-history-tab="reports" role="tab" aria-selected="true">By Report</button>
              <button class="history-tab" type="button" data-history-tab="responsibles" role="tab" aria-selected="false">By Responsible</button>
            </div>
          </div>
          <div id="reportsPane" class="history-pane" role="tabpanel">
            <div id="reports" class="meta">Loading reports...</div>
          </div>
          <div id="responsiblesPane" class="history-pane" role="tabpanel" hidden>
            <div id="responsibles" class="meta">Loading responsible folders...</div>
          </div>
        </div>
      </section>

__ADMIN_TRACE_SECTION__
    </div>
  </main>

  <div id="issueReviewModal" class="workflow-modal" hidden>
    <div class="workflow-dialog dialog-size-full issue-review-dialog" role="dialog" aria-modal="true" aria-labelledby="issueReviewTitle">
      <div class="workflow-head">
        <div><h2 id="issueReviewTitle">Issues Review History</h2><p class="meta">ECHNL Issue lifecycle, handling, re-scan and Pass readiness.</p></div>
        <div class="actions"><button id="refreshIssueReviewsBtn" class="secondary small-action" type="button">Refresh</button><button id="closeIssueReviewsBtn" class="icon-action" type="button" aria-label="Close">&#x2715;</button></div>
      </div>
      <div class="issue-history-tabs" role="tablist" aria-label="Issues Review History views"><button id="issueOverviewTab" class="issue-history-tab active" data-issue-review-view="overview" type="button" role="tab" aria-selected="true" aria-controls="issueReviewOverviewPanel">Overview</button><button id="issueListTab" class="issue-history-tab" data-issue-review-view="issues" type="button" role="tab" aria-selected="false" aria-controls="issueReviewIssuesView">Issues</button></div>
      <section id="issueReviewOverviewPanel" class="issue-history-overview" role="tabpanel"><div class="markdown-preview empty">Loading Sprint overview...</div></section>
      <div id="issueReviewIssuesView" class="workflow-body" role="tabpanel" hidden>
        <section class="workflow-list-pane">
          <div class="workflow-toolbar"><input id="issueReviewSearch" placeholder="Search ECHNL, summary, responsible"><span id="issueReviewScope" class="meta" hidden></span><span id="issueReviewCount" class="count-pill">0</span></div>
          <div id="issueReviewList" class="meta">Loading Issue Reviews...</div>
        </section>
        <section id="issueReviewDetail" class="workflow-detail-pane"><div class="markdown-preview empty">Select an ECHNL Issue to inspect its Review history.</div></section>
      </div>
    </div>
  </div>

  <div id="configurationModal" class="workflow-modal" hidden>
    <div class="workflow-dialog dialog-size-full configuration-dialog" role="dialog" aria-modal="true" aria-labelledby="configurationTitle" aria-describedby="configurationDescription">
      <div class="workflow-head">
        <div><h2 id="configurationTitle">Configuration</h2><p id="configurationDescription" class="meta">Maintain safe application settings and GitLab project metadata without rewriting the deployment YAML.</p></div>
        <div class="actions"><span id="configurationRevision" class="count-pill">Not loaded</span><button id="refreshConfigurationBtn" class="secondary small-action" type="button">Refresh</button><button id="closeConfigurationBtn" class="icon-action" type="button" aria-label="Close configuration">&#x2715;</button></div>
      </div>
      <div class="configuration-body">
        <nav class="configuration-nav" aria-label="Configuration sections">
          <button class="active" type="button" data-configuration-view="application">Application settings</button>
          <button type="button" data-configuration-view="projects">GitLab projects</button>
          <button type="button" data-configuration-view="backups">Backups &amp; restore</button>
          <div class="configuration-source"><strong>Safe Web override</strong><br>Changes are revisioned, validated, audited and stored separately from config.yml. Secrets and user authentication are never exposed here.</div>
        </nav>
        <section class="configuration-main">
          <div class="configuration-toolbar"><input id="configurationSearch" type="search" placeholder="Search setting, project, category" aria-label="Search configuration"><button id="addConfigurationProjectBtn" class="secondary" type="button" hidden>Add project</button><span id="configurationStatus" class="status" role="status" aria-live="polite"></span></div>
          <div id="configurationContent" class="configuration-content"><div class="configuration-empty">Open Configuration to load effective settings.</div></div>
        </section>
      </div>
    </div>
  </div>

  <div id="userAdminModal" class="workflow-modal" hidden>
    <div class="workflow-dialog dialog-size-full user-admin-dialog" role="dialog" aria-modal="true" aria-labelledby="userAdminTitle" aria-describedby="userAdminDescription">
      <div class="workflow-head">
        <div><h2 id="userAdminTitle">User Management</h2><p id="userAdminDescription" class="meta">Create accounts and manage role, access status, Responsible scope, and password recovery.</p></div>
        <div class="actions"><button id="createUserBtn" type="button">Create user</button><button id="refreshUsersBtn" class="secondary small-action" type="button">Refresh</button><button id="closeUserAdminBtn" class="icon-action" type="button" aria-label="Close user management">&#x2715;</button></div>
      </div>
      <div class="user-admin-body">
        <section class="user-admin-list-pane" aria-label="Users">
          <div class="user-admin-toolbar">
            <input id="userAdminSearch" class="search-input" type="search" placeholder="Search user or Responsible" aria-label="Search users">
            <select id="userAdminRoleFilter" class="search-input" aria-label="Filter by role"><option value="">All roles</option><option value="developer">Developer</option><option value="auditor">Auditor</option><option value="manager">Manager</option></select>
            <select id="userAdminStatusFilter" class="search-input" aria-label="Filter by status"><option value="">All statuses</option><option value="active">Active</option><option value="suspended">Suspended</option><option value="password-change">Password change required</option></select>
          </div>
          <div class="meta" style="margin-bottom:8px"><span id="userAdminCount">0</span> user(s)</div>
          <div id="userAdminList" class="user-admin-list"><div class="meta">Open User Management to load accounts.</div></div>
        </section>
        <section id="userAdminDetail" class="user-admin-detail-pane" aria-live="polite">
          <div id="userAdminEmpty" class="user-admin-empty">Select a user or create a new account.</div>
          <form id="userAdminForm" class="user-admin-form" novalidate hidden>
            <div class="user-admin-detail-head">
              <div><h3 id="userAdminFormTitle">Edit user</h3><p id="userAdminFormMeta" class="meta"></p></div>
              <span id="userAdminFormStatusChip" class="status-chip user-status-chip"></span>
            </div>
            <div id="userAdminValidation" class="validation-summary" role="alert" tabindex="-1" hidden></div>
            <div class="user-admin-form-grid">
              <label><span>Username <span class="required-mark" aria-hidden="true">*</span></span><input id="managedUsername" maxlength="64" autocomplete="off" aria-describedby="managedUsernameError" required><span id="managedUsernameError" class="field-message" role="alert"></span></label>
              <label><span>Role <span class="required-mark" aria-hidden="true">*</span></span><select id="managedRole" aria-describedby="managedRoleError" required><option value="">Select role</option><option value="developer">Developer</option><option value="auditor">Auditor</option><option value="manager">Manager</option></select><span id="managedRoleError" class="field-message" role="alert"></span></label>
            </div>
            <label class="user-active-control"><input id="managedActive" type="checkbox"><span><strong>Active account</strong><span class="field-help">Suspending an account revokes its active sessions.</span></span></label>
            <fieldset id="managedResponsibleFieldset" class="user-responsible-fieldset" aria-describedby="managedResponsiblesHelp managedResponsiblesError">
              <legend><span id="managedResponsibleLegend">Responsible scope</span> <span id="managedResponsibleRequired" class="required-mark" aria-hidden="true">*</span></legend>
              <span id="managedResponsiblesHelp" class="field-help"><strong>Access scope, not a reporting line.</strong> It matches Responsible identifiers on reports and Jira Issues. Developer and Auditor require at least one scope; Manager has global access and ignores mappings.</span>
              <div class="user-responsible-add"><input id="managedResponsibleAdd" maxlength="80" placeholder="Add Responsible identifier" aria-describedby="managedResponsibleAddError"><button id="addManagedResponsibleBtn" class="secondary" type="button">Add</button></div>
              <span id="managedResponsibleAddError" class="field-message" role="alert"></span>
              <input id="managedResponsibleSearch" class="search-input" type="search" placeholder="Filter Responsible options" aria-label="Filter Responsible mappings">
              <div id="managedResponsibleOptions" class="user-responsible-options"></div>
              <span id="managedResponsiblesError" class="field-message" role="alert"></span>
            </fieldset>
            <div id="userAdminSaveStatus" class="status" role="status" aria-live="polite"></div>
            <div class="user-admin-form-actions">
              <button id="resetManagedPasswordBtn" class="secondary" type="button">Reset password</button>
              <button id="saveManagedUserBtn" type="submit">Save user</button>
            </div>
          </form>
        </section>
      </div>
    </div>
  </div>

  <div id="userResetConfirmModal" class="confirm-backdrop" hidden>
    <div class="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="userResetConfirmTitle" aria-describedby="userResetConfirmDescription">
      <h2 id="userResetConfirmTitle">Reset password</h2>
      <p id="userResetConfirmDescription">The current password and all active sessions will be invalidated. The user must change the temporary password after signing in.</p>
      <label class="user-reset-confirmation">Type <strong id="userResetConfirmName"></strong> to confirm<input id="userResetConfirmInput" autocomplete="off" aria-describedby="userResetConfirmError"></label>
      <div id="userResetConfirmError" class="field-message" role="alert"></div>
      <div class="confirm-actions"><button id="cancelUserResetBtn" class="secondary" type="button">Cancel</button><button id="confirmUserResetBtn" class="danger-action" type="button" disabled>Reset password</button></div>
    </div>
  </div>

  <div id="temporaryPasswordModal" class="confirm-backdrop" hidden>
    <div class="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="temporaryPasswordTitle" aria-describedby="temporaryPasswordDescription">
      <h2 id="temporaryPasswordTitle">Temporary password</h2>
      <p id="temporaryPasswordDescription">Copy this password now. It is shown only once and the user will be required to change it after signing in.</p>
      <input id="temporaryPasswordValue" class="temporary-password" readonly aria-label="Temporary password">
      <div id="temporaryPasswordStatus" class="status" role="status" aria-live="polite"></div>
      <div class="credential-actions"><button id="copyTemporaryPasswordBtn" class="secondary" type="button">Copy password</button><button id="closeTemporaryPasswordBtn" type="button">Done</button></div>
    </div>
  </div>

  <div id="changePasswordModal" class="confirm-backdrop" hidden>
    <div class="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="changePasswordTitle" aria-describedby="changePasswordDescription">
      <h2 id="changePasswordTitle">Change password</h2>
      <p id="changePasswordDescription">Use 14–128 characters with uppercase, lowercase, number, and symbol.</p>
      <form id="changePasswordForm" class="user-admin-form" novalidate>
        <div id="changePasswordValidation" class="validation-summary" role="alert" tabindex="-1" hidden></div>
        <label><span class="field-label">Current password <span class="required-mark" aria-hidden="true">*</span></span><input id="currentPasswordField" type="password" autocomplete="current-password" required></label>
        <label><span class="field-label">New password <span class="required-mark" aria-hidden="true">*</span></span><input id="newPasswordField" type="password" autocomplete="new-password" required></label>
        <label><span class="field-label">Confirm new password <span class="required-mark" aria-hidden="true">*</span></span><input id="confirmNewPasswordField" type="password" autocomplete="new-password" required></label>
        <div id="changePasswordStatus" class="status" role="status" aria-live="polite"></div>
        <div class="confirm-actions"><button id="cancelChangePasswordBtn" class="secondary" type="button">Cancel</button><button id="saveChangedPasswordBtn" type="submit">Change password</button></div>
      </form>
    </div>
  </div>

  <div id="healthDetailModal" class="confirm-backdrop" hidden>
    <section class="confirm-dialog health-detail-dialog" role="dialog" aria-modal="true" aria-labelledby="healthDetailTitle">
      <div class="confirm-head">
        <div><h2 id="healthDetailTitle">CodeReviewer health</h2><p id="healthDetailMeta" class="health-detail-status">Loading service checks…</p></div>
        <button id="closeHealthDetailBtn" class="icon-action" type="button" aria-label="Close health details">&#x2715;</button>
      </div>
      <div id="healthDetailChecks" class="health-detail-checks"></div>
      <div class="confirm-actions"><button id="refreshHealthDetailBtn" class="secondary" type="button">Refresh checks</button><button id="doneHealthDetailBtn" type="button">Done</button></div>
    </section>
  </div>

  <div id="draftEditorModal" class="workflow-modal" hidden>
    <div class="workflow-dialog" role="dialog" aria-modal="true" aria-labelledby="draftEditorTitle">
      <div class="workflow-head">
        <div><h2 id="draftEditorTitle">Pending Jira</h2><p id="draftEditorMeta" class="meta">ADF-compatible Issue Description draft</p></div>
        <div class="actions"><button id="saveDraftBtn" type="button">Save draft</button><button id="closeDraftEditorBtn" class="secondary small-action" type="button">Cancel</button></div>
      </div>
      <div class="workflow-detail-pane">
        <div id="pendingDraftList"></div>
        <div id="draftEditorForm" hidden>
          <label><span>Issue Summary <span class="required-mark" aria-hidden="true">*</span></span><input id="draftSummary" maxlength="255" required aria-describedby="draftStatus"></label>
          <div class="adf-field-label" id="draftDescriptionLabel">Issue Description <span class="required-mark" aria-hidden="true">*</span></div>
          <div class="adf-editor-intro">
            <div><strong>Structured Jira description</strong><p>Stores Jira-compatible ADF. Atlaskit is used as an optional progressive enhancement when its local bundle is available; otherwise the built-in ADF block editor remains active.</p></div>
            <span id="adfEditorEngine" class="adf-editor-engine">Built-in ADF editor</span>
          </div>
          <div class="adf-editor-shell">
            <div class="adf-toolbar">
              <button class="secondary" type="button" data-adf-insert="paragraph">Paragraph</button>
              <button class="secondary" type="button" data-adf-insert="heading">Heading</button>
              <button class="secondary" type="button" data-adf-insert="bulletList">Bullet list</button>
              <button class="secondary" type="button" data-adf-insert="orderedList">Ordered list</button>
              <button class="secondary" type="button" data-adf-insert="table">Table</button>
              <button class="secondary" type="button" data-adf-insert="expand">Expand</button>
              <button class="secondary" type="button" data-adf-insert="nestedExpand">Nested Expand</button>
              <label class="secondary small-action" style="display:inline-flex;margin:0;cursor:pointer">Screenshot<input id="draftImageInput" type="file" accept="image/png,image/jpeg,image/gif,image/webp" hidden></label>
              <button id="adfEditModeBtn" class="secondary" type="button">Edit</button>
              <button id="adfPreviewModeBtn" class="secondary" type="button">Preview</button>
            </div>
            <div class="draft-editor-grid">
              <textarea id="draftAdfSource" class="adf-source" spellcheck="false" hidden></textarea>
              <div id="draftBlockEditor" class="adf-block-editor" role="textbox" aria-labelledby="draftDescriptionLabel" aria-describedby="draftStatus" aria-required="true" tabindex="-1"></div>
              <div id="atlaskitAdfEditor" class="adf-preview" role="region" aria-label="Atlaskit enhanced ADF editor" tabindex="-1" hidden></div>
              <div id="draftAdfPreview" class="adf-preview meta" hidden>Choose Preview to render the ADF document.</div>
            </div>
          </div>
          <div id="draftStatus" class="status"></div>
        </div>
      </div>
    </div>
  </div>

  <div id="previewModal" class="preview-modal-backdrop" hidden>
    <div class="preview-modal-dialog dialog-size-l" role="dialog" aria-modal="true" aria-labelledby="previewModalTitle">
      <div class="preview-modal-head">
        <div class="preview-title">
          <h2 id="previewModalTitle">Report Preview</h2>
          <div id="previewModalName" class="meta">No report selected.</div>
        </div>
        <div class="preview-actions">
          <button id="previewPrevBtn" class="secondary small-action" type="button">Previous</button>
          <button id="previewNextBtn" class="secondary small-action" type="button">Next</button>
          <a id="previewModalDownloadLink" class="download-link" href="#" hidden>Download</a>
          <button id="previewRestoreBtn" class="icon-action" type="button" title="Restore preview" aria-label="Restore preview">&#x2715;</button>
        </div>
      </div>
      <div id="previewModalBody" class="markdown-preview preview-modal-body empty">No report generated yet.</div>
    </div>
  </div>

  <div id="coverageModal" class="confirm-backdrop" hidden>
    <div class="coverage-dialog dialog-size-xl" role="dialog" aria-modal="true" aria-labelledby="coverageTitle">
      <div class="thread-head">
        <div>
          <h2 id="coverageTitle">Sprint Overview</h2>
          <div id="coverageRoleHint" class="meta">Review Sprint coverage, handling readiness, blockers and Pass status.</div>
        </div>
        <button id="coverageCloseBtn" class="icon-action" type="button" title="Close coverage" aria-label="Close coverage">&#x2715;</button>
      </div>
      <div class="coverage-filters">
        <div class="field-help">Review scope <span class="required-mark" aria-hidden="true">*</span> — complete at least one field.</div>
        <label>Jira issues
          <input id="coverageJira" placeholder="ECHNL-1001, ECHNL-1002" aria-describedby="coverageValidation">
        </label>
        <label>Sprint
          <input id="coverageSprint" placeholder="10068" aria-describedby="coverageValidation">
        </label>
        <label>Jira Filter ID
          <input id="coverageFilter" placeholder="12345" aria-describedby="coverageValidation">
        </label>
        <button id="coverageScanBtn" type="button">Scan</button>
      </div>
      <div id="coverageValidation" class="field-message" role="alert"></div>
      <div id="coverageProgress" class="coverage-progress" role="status" aria-live="polite" hidden>
        <span class="coverage-progress-spinner" aria-hidden="true"></span>
        <div class="coverage-progress-copy">
          <strong id="coverageProgressTitle">Preparing Sprint overview</strong>
          <span id="coverageProgressDetail">Starting coverage discovery…</span>
        </div>
        <div id="coverageProgressTiming" class="coverage-progress-timing">Elapsed 0s</div>
        <div class="coverage-progress-track" aria-hidden="true"><span id="coverageProgressBar"></span></div>
      </div>
      <div id="coverageStatus" class="status"></div>
      <div class="coverage-view-tabs" role="tablist" aria-label="Sprint Review views">
        <button id="coverageOverviewTab" class="coverage-view-tab active" type="button" role="tab" aria-selected="true" aria-controls="coverageOverviewPanel" data-coverage-view="overview">Overview</button>
        <button id="coverageIssuesTab" class="coverage-view-tab" type="button" role="tab" aria-selected="false" aria-controls="coverageIssuesPanel" data-coverage-view="issues">Sprint issues</button>
      </div>
      <div id="coverageOverviewPanel" class="coverage-view-panel coverage-overview-panel" role="tabpanel" data-coverage-panel="overview">
        <div id="coverageSummary" class="coverage-summary"></div>
        <div id="coverageApplications" class="coverage-applications"></div>
        <div id="coverageOverviewEmpty" class="coverage-empty">Select a scope and scan to view Sprint readiness.</div>
      </div>
      <div id="coverageIssuesPanel" class="coverage-view-panel" role="tabpanel" data-coverage-panel="issues" hidden>
        <div class="coverage-results">
          <div id="coverageRows" class="coverage-card-grid">
            <div class="coverage-empty">Select a scope and scan.</div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <div id="threadModal" class="thread-modal-backdrop" hidden>
    <div class="thread-modal-dialog dialog-size-xl" role="dialog" aria-modal="true" aria-labelledby="threadModalTitle">
      <div class="thread-head">
        <div class="preview-title">
          <h2 id="threadModalTitle">Review Communication</h2>
          <div id="threadReportName" class="meta"></div>
          <div id="threadContext" class="thread-context" hidden>
            <div id="threadIssueTitle" class="thread-issue-title"></div>
            <div id="threadContextMetrics" class="thread-context-metrics" aria-label="Review handling summary"></div>
          </div>
        </div>
        <div class="preview-actions">
          <button id="threadPrevBtn" class="secondary small-action" type="button">Previous</button>
          <button id="threadNextBtn" class="secondary small-action" type="button">Next</button>
          <a id="threadRawLink" class="download-link" href="#" target="_blank" rel="noopener" hidden>Raw</a>
          <a id="threadDownloadLink" class="download-link" href="#" hidden>Download</a>
          <button class="icon-action" id="closeThreadBtn" type="button" title="Close discussion" aria-label="Close discussion">&#x2715;</button>
        </div>
      </div>
      <div class="thread-tabs" role="tablist" aria-label="Review communication tools">
        <button class="thread-tab active" type="button" data-thread-tab="discussion" role="tab" aria-selected="true">Discussion</button>
        <button class="thread-tab" type="button" data-thread-tab="chat" role="tab" aria-selected="false">AI Assist</button>
        <button id="openIssueReviewFromReportBtn" class="thread-tab thread-open-review" type="button">Open Issue Review</button>
      </div>
      <div id="discussionPane" class="thread-pane">
        <div class="thread-layout">
        <section class="thread-column">
          <h3>History</h3>
          <div id="threadMessages" class="thread-messages meta"></div>
        </section>
        <section class="thread-column thread-followup-column">
          <div class="thread-column-heading">
            <h3>Follow-up Draft</h3>
            <button id="copyFollowupDraftBtn" class="secondary copy-draft-button" type="button" aria-label="Copy follow-up draft" title="Copy follow-up draft" disabled>
              <svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="8" width="11" height="11" rx="2"></rect><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"></path></svg>
            </button>
            <span id="copyFollowupStatus" class="sr-only" role="status" aria-live="polite"></span>
          </div>
          <pre id="followupDraft" class="followup-draft">No follow-up draft yet.</pre>
        </section>
        <section class="thread-column thread-reply-column">
          <div class="thread-section-heading">
            <div><h3>Reply</h3><p class="meta">记录沟通结论，或整理需要后续跟进的事项。</p></div>
            <span class="info-hint">
              <button class="information-icon" type="button" aria-label="Handling guidance" aria-expanded="false" aria-controls="replyGuidancePopover">i</button>
              <span id="replyGuidancePopover" class="information-hint-popover" role="tooltip" hidden>
                请结合问题证据说明处理决定和验证结果，避免只填写“已处理”。<br><br>
                <strong>已整改：</strong>说明修改内容、影响文件和测试结果；Critical/High 需等待 Re-scan 验证。<br>
                <strong>另报 Jira：</strong>说明为何不阻碍当前交付，并补充跟进范围和验收目标。<br>
                <strong>不是问题：</strong>提供可核验依据，Developer 提交后需 Auditor/Manager 确认。
              </span>
            </span>
          </div>
          <div class="thread-form">
            <div class="thread-reply-card">
              <div class="thread-reply-card-head">
                <strong>Reply message</strong>
                <p>记录需要保留在本次 Review 中的沟通结论。</p>
              </div>
              <label><span>Message <span class="required-mark" aria-hidden="true">*</span></span>
                <textarea id="threadMessage" required aria-describedby="threadMessageError" placeholder="记录组长处理说明，例如：1 已整改，Pass通过；2 不是阻碍，另报 issue 跟进。"></textarea>
              </label>
              <div id="threadMessageError" class="field-message" role="alert"></div>
              <div class="thread-reply-actions">
                <button class="thread-card-action" id="sendThreadMessageBtn" type="button">Reply</button>
              </div>
            </div>
            <div class="thread-reply-card">
              <div class="thread-reply-card-head">
                <strong>Follow-up</strong>
                <p>定义整理范围，生成的内容会显示在中间的 Follow-up Draft。</p>
              </div>
              <label>Scope
                <textarea id="followupInstruction" placeholder="例如：只整理“不是阻碍，另报 Jira”的改善项，并按优先级列出验收目标。"></textarea>
              </label>
              <div class="thread-reply-actions">
                <button class="thread-card-action" id="generateFollowupsBtn" type="button">Follow-up</button>
              </div>
            </div>
          </div>
        </section>
        </div>
      </div>
      <div id="chatPane" class="thread-pane tools" hidden>
        <section class="thread-column">
          <h3>AI Assist</h3>
          <div id="aiChatMessages" class="chat-messages" data-empty="true"><div class="ai-assist-note">Ask for evidence explanations, summaries, or suggested follow-ups. AI cannot record handling or Pass decisions.</div></div>
        </section>
        <section class="thread-column">
          <div class="thread-section-heading"><h3>Ask AI</h3><p class="meta">聚焦证据、影响范围和待处理项；AI 不会代替正式审批。</p></div>
          <div class="thread-form ai-ask-card">
            <label><span class="field-label">Message <span class="required-mark" aria-hidden="true">*</span></span>
              <textarea id="aiChatInput" required aria-describedby="aiChatError" placeholder="例如：请解释这项 High/Critical 问题的证据和可能影响。"></textarea>
            </label>
            <div id="aiChatError" class="field-message" role="alert"></div>
            <div class="thread-reply-actions">
              <button class="thread-card-action" id="sendAiChatBtn" type="button">Ask AI</button>
            </div>
          </div>
        </section>
      </div>
    </div>
  </div>

  <div id="rerunConfirmModal" class="confirm-backdrop" hidden>
    <div class="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="rerunConfirmTitle">
      <div class="confirm-head">
        <div>
          <h2 id="rerunConfirmTitle">已有 Code Review Report</h2>
          <div id="rerunConfirmSummary" class="meta"></div>
        </div>
        <button class="secondary small-action" id="rerunConfirmClose" type="button">Close</button>
      </div>
      <div id="rerunReportList" class="confirm-report-list"></div>
      <div id="rerunStepOne" class="confirm-step">
        <strong>确认一</strong>
        <div id="rerunReuseHint" class="meta">Re-scan 会生成一份新的报告，并保留当前已有报告；完成后可使用 Compare 查看新增、减少和仍存在的问题。</div>
        <div class="confirm-actions">
          <button class="secondary" id="rerunCancelOne" type="button">Cancel</button>
          <button class="secondary" id="rerunUseExisting" type="button" hidden>Use Existing</button>
          <button id="rerunFirstConfirm" type="button">Re-scan</button>
        </div>
      </div>
      <div id="rerunStepTwo" class="confirm-step" hidden>
        <strong>确认二</strong>
        <label>Type Jira issue key
          <input id="rerunConfirmInput" autocomplete="off">
        </label>
        <div class="confirm-actions">
          <button class="secondary" id="rerunBack" type="button">Back</button>
          <button class="danger-action" id="rerunFinalConfirm" type="button" disabled>Re-scan</button>
        </div>
      </div>
    </div>
  </div>

  <div id="releaseNotesModal" class="confirm-backdrop release-notes-backdrop" hidden>
    <div class="release-notes-dialog" data-dialog-size="l" role="dialog" aria-modal="true" aria-labelledby="releaseNotesTitle" aria-describedby="releaseNotesDescription">
      <div class="release-notes-head">
        <div class="preview-title"><h2 id="releaseNotesTitle">CodeReviewer Release Notes</h2><div id="releaseNotesDescription" class="meta">Public summary of CodeReviewer 7.x updates.</div><time id="releaseNotesUpdated" class="meta" datetime=""></time></div>
        <button id="closeReleaseNotesBtn" class="icon-action" type="button" aria-label="Close release notes">&#x2715;</button>
      </div>
      <div id="releaseNotesContent" class="markdown-preview release-notes-content" tabindex="0">Loading release notes…</div>
    </div>
  </div>

  __ADF_ASSET__
  <script>
    const $ = (id) => document.getElementById(id);
    let currentOutputDir = '';
    let currentUserIsAdmin = false;
    let releaseNotesReturnFocus = null;
    let releaseNotesCachedMarkdown = '';
    let healthReturnFocus = null;
    let currentUserRole = 'auditor';
    let currentPermissions = {};
    let managedUsers = [];
    let managedUserRoles = ['developer', 'auditor', 'manager'];
    let managedResponsibleOptions = [];
    let selectedManagedUsername = '';
    let managedUserCreating = false;
    let userAdminReturnFocus = null;
    let userAdminSearchTimer = 0;
    let configurationData = null;
    let configurationView = 'application';
    let configurationReturnFocus = null;
    let configurationSearchTimer = 0;
    let configurationEditors = [];
    let configurationCreateVisible = false;
    let changePasswordRequired = false;
    let selectedThreadReport = '';
    let currentPreviewMarkdown = '';
    let currentPreviewName = '';
    let currentPreviewRawUrl = '';
    let currentPreviewDownloadUrl = '';
    let currentPreviewReportPath = '';
    let currentPreviewOutputDir = '';
    let selectedThreadOutputDir = '';
    let currentReportItems = [];
    let reportSearchTimer = 0;
    const jobPollers = new Map();
    const jobSnapshots = new Map();
    const COVERAGE_LONG_SCAN_SECONDS = 30;
    const COVERAGE_TIMEOUT_THRESHOLD_SECONDS = 180;
    let coverageJobId = '';
    let coverageJobSnapshot = null;
    let coveragePollTimer = 0;
    let coverageTimingTimer = 0;
    let coverageView = 'overview';
    const submissionFlights = new Map();
    const jobAutoScroll = new Map();
    const jobAutoScrollResumeTimers = new Map();
    const maximizedJobs = new Set();
    const activeJobStatuses = new Set(['queued', 'running', 'pausing', 'paused', 'stopping']);
    let reviewLifecycleActive = false;
    let lastReviewPayload = null;
    const sidebars = {
      projects: { panel: 'projectsAside', toggle: 'projectsToggle', mainClass: 'projects-collapsed', showTitle: 'Show projects', hideTitle: 'Hide projects' },
      history: { panel: 'historyAside', toggle: 'historyToggle', mainClass: 'history-collapsed', showTitle: 'Show report history', hideTitle: 'Hide report history' }
    };

    function initSidebars() {
      for (const [key, config] of Object.entries(sidebars)) {
        setSidebarCollapsed(key, true);
        const button = $(config.toggle);
        if (button) button.addEventListener('click', () => setSidebarCollapsed(key, !$(config.panel).classList.contains('collapsed')));
      }
    }

    function initInformationHints() {
      const closeAll = except => {
        document.querySelectorAll('.info-hint').forEach(wrapper => {
          if (wrapper === except) return;
          const button = wrapper.querySelector('.information-icon');
          const popover = wrapper.querySelector('.information-hint-popover');
          if (button) button.setAttribute('aria-expanded', 'false');
          if (popover) popover.hidden = true;
        });
      };
      document.addEventListener('click', event => {
        const button = event.target.closest('.information-icon');
        if (!button) {
          if (!event.target.closest('.information-hint-popover')) closeAll(null);
          return;
        }
        event.preventDefault();
        event.stopPropagation();
        const wrapper = button.closest('.info-hint');
        const popover = wrapper && wrapper.querySelector('.information-hint-popover');
        if (!wrapper || !popover) return;
        const willOpen = popover.hidden;
        closeAll(wrapper);
        popover.hidden = !willOpen;
        button.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
      });
      document.addEventListener('keydown', event => {
        if (event.key === 'Escape') {
          const expanded = document.querySelector('.information-icon[aria-expanded="true"]');
          if (expanded) {
            event.preventDefault();
            event.stopImmediatePropagation();
          }
          closeAll(null);
          if (expanded) expanded.focus();
        }
      });
    }

    function setSidebarCollapsed(key, collapsed) {
      const config = sidebars[key];
      if (!config) return;
      const panel = $(config.panel);
      const button = $(config.toggle);
      const main = $('appMain');
      if (!panel || !button || !main) return;
      panel.classList.toggle('collapsed', collapsed);
      main.classList.toggle(config.mainClass, collapsed);
      button.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      button.title = collapsed ? config.showTitle : config.hideTitle;
    }

    function reviewInputSummary(payload = null) {
      const currentJira = ($('jira')?.value || '').trim();
      const currentSprint = ($('sprint')?.value || '').trim();
      const currentFilter = ($('jiraFilter')?.value || '').trim();
      const source = payload || ((currentJira || currentSprint || currentFilter) ? null : lastReviewPayload);
      const jira = source ? String(source.jira_key || '').trim() : currentJira;
      const sprint = source ? String(source.sprint || '').trim() : currentSprint;
      const jiraFilter = source ? String(source.jira_filter || '').trim() : currentFilter;
      const mrUrl = source ? String(source.mr_url || '').trim() : '';
      const mode = source ? String(source.mode || '').trim() : '';
      const priority = source
        ? String(source.report_min_severity || 'Medium')
        : ($('reportMinSeverity')?.selectedOptions?.[0]?.textContent || 'Medium and above');
      const subject = mode === 'release-gate'
        ? `Release Gate ${mrUrl ? mrUrl.split('/').pop() : ''}`.trim()
        : (jira ? `Jira ${jira}` : (jiraFilter ? `Jira Filter ${jiraFilter}` : (sprint ? `Sprint ${sprint}` : 'Review inputs')));
      const priorityLabel = priority.includes('above') || priority.includes('only') ? priority : `${priority} and above`;
      return `${subject} · ${priorityLabel}`;
    }

    function setRunFormCollapsed(collapsed, options = {}) {
      const panel = $('runPanel');
      const body = $('runFormBody');
      const button = $('runFormToggle');
      const summary = $('runFormSummary');
      if (!panel || !body || !button) return;
      panel.classList.toggle('form-collapsed', collapsed);
      body.setAttribute('aria-hidden', collapsed ? 'true' : 'false');
      button.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      button.title = collapsed ? 'Expand review inputs' : 'Collapse review inputs';
      button.setAttribute('aria-label', button.title);
      if (summary) summary.textContent = reviewInputSummary(options.payload || null);
      if (options.focusProgress) {
        requestAnimationFrame(() => {
          const progress = $('progressPanel');
          if (!progress) return;
          progress.focus({ preventScroll: true });
          progress.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
      } else if (!collapsed && options.focusInput) {
        requestAnimationFrame(() => $('jira')?.focus({ preventScroll: true }));
      }
    }

    function toggleRunForm() {
      const collapsed = $('runPanel')?.classList.contains('form-collapsed') || false;
      setRunFormCollapsed(!collapsed, { focusInput: collapsed });
    }

    function beginReviewLifecycle(payload, options = {}) {
      lastReviewPayload = payload;
      reviewLifecycleActive = true;
      const state = $('progressState');
      const detail = $('progressDetail');
      if (state) {
        state.textContent = 'Starting';
        state.className = 'progress-state running';
      }
      if (detail) detail.textContent = `Creating ${reviewInputSummary(payload)} review job...`;
      setRunFormCollapsed(true, { payload, focusProgress: options.focusProgress !== false });
    }

    function restoreRunFormAfterAbortedStart() {
      reviewLifecycleActive = Array.from(jobSnapshots.values()).some((job) => activeJobStatuses.has(job.status));
      setRunFormCollapsed(false);
      updateProgressSummary();
    }

    function reportOutputQuery() {
      return currentOutputDir ? `?output_dir=${encodeURIComponent(currentOutputDir)}` : '';
    }

    function reportHistoryQuery() {
      const params = new URLSearchParams();
      const query = ($('reportSearch')?.value || '').trim();
      if (query) params.set('q', query);
      params.set('days', $('reportDays')?.value || '14');
      const text = params.toString();
      return text ? `?${text}` : '';
    }

    async function fetchJson(url, options = {}) {
      const response = await fetch(url, options);
      let data = {};
      try {
        data = await response.json();
      } catch (error) {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        throw error;
      }
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || `HTTP ${response.status}`);
      }
      return data;
    }

    async function singleFlight(key, button, action) {
      if (submissionFlights.has(key)) return submissionFlights.get(key);
      const startedAt = Date.now();
      if (button) { button.disabled = true; button.setAttribute('aria-busy', 'true'); }
      const promise = Promise.resolve().then(action).finally(async () => {
        const remaining = Math.max(0, 350 - (Date.now() - startedAt));
        if (remaining) await new Promise(resolve => window.setTimeout(resolve, remaining));
        submissionFlights.delete(key);
        if (button && document.contains(button)) { button.disabled = false; button.removeAttribute('aria-busy'); }
      });
      submissionFlights.set(key, promise);
      return promise;
    }

    async function loadMe() {
      const data = await fetchJson('/api/me');
      $('currentUser').textContent = data.username || '-';
      currentUserRole = data.role || 'auditor';
      currentPermissions = data.permissions || {};
      if ($('currentRole')) $('currentRole').textContent = currentUserRole.charAt(0).toUpperCase() + currentUserRole.slice(1);
      if ($('appVersion') && data.version) $('appVersion').textContent = data.version;
      currentUserIsAdmin = Boolean(data.is_admin);
      if ($('runBtn')) $('runBtn').hidden = !currentPermissions.run_issue_review;
      if ($('runPanel') && !currentPermissions.run_issue_review) {
        $('runPanel').hidden = true;
        const previewPanel = document.querySelector('.preview-panel');
        if (previewPanel) previewPanel.style.gridColumn = '2 / 4';
      }
      if ($('coverageBtn')) $('coverageBtn').hidden = !currentPermissions.scan_coverage;
      for (const button of document.querySelectorAll('[data-thread-tab="chat"]')) button.hidden = !currentPermissions.ai_chat;
      if ($('manualPassBtn')) $('manualPassBtn').hidden = !currentPermissions.manual_pass;
      if ($('rescanReportBtn')) $('rescanReportBtn').hidden = !currentPermissions.run_issue_review;
      if ($('generateFollowupsBtn')) $('generateFollowupsBtn').hidden = !currentPermissions.ai_chat;
      return data;
    }

    function adminRequestId(prefix) {
      const value = (window.crypto && window.crypto.randomUUID) ? window.crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
      return `${prefix}:${value}`;
    }

    function managedUserByName(username) {
      const key = String(username || '').toLowerCase();
      return managedUsers.find(item => String(item.username || '').toLowerCase() === key) || null;
    }

    function managedUserStatus(user) {
      if (!user || user.active === false) return 'suspended';
      if (user.must_change_password) return 'password-change';
      return 'active';
    }

    function managedUserStatusLabel(user) {
      const status = managedUserStatus(user);
      return status === 'suspended' ? 'Suspended' : (status === 'password-change' ? 'Password change required' : 'Active');
    }

    function renderManagedUsers() {
      const query = ($('userAdminSearch')?.value || '').trim().toLowerCase();
      const role = $('userAdminRoleFilter')?.value || '';
      const status = $('userAdminStatusFilter')?.value || '';
      const rows = managedUsers.filter(user => {
        const haystack = [user.username, ...(user.responsibles || [])].join(' ').toLowerCase();
        return (!query || haystack.includes(query))
          && (!role || user.role === role)
          && (!status || managedUserStatus(user) === status);
      });
      $('userAdminCount').textContent = String(rows.length);
      $('userAdminList').innerHTML = rows.length ? rows.map(user => {
        const selected = String(user.username || '').toLowerCase() === selectedManagedUsername.toLowerCase() ? ' selected' : '';
        const scope = (user.responsibles || []).slice(0, 2).join(', ');
        const extra = Math.max(0, (user.responsibles || []).length - 2);
        const allScopes = (user.responsibles || []).join(', ');
        const scopeDisplay = allScopes ? `${scope}${extra ? ` +${extra}` : ''}` : (user.role === 'manager' ? 'Global scope' : 'Not assigned');
        const statusValue = managedUserStatus(user);
        return `<button class="user-admin-card${selected}" type="button" data-managed-user="${escapeHtml(user.username)}" aria-pressed="${selected ? 'true' : 'false'}">
          <span class="user-admin-card-head"><strong>${escapeHtml(user.username)}</strong><span class="status-chip user-status-chip" data-status="${statusValue}">${escapeHtml(managedUserStatusLabel(user))}</span></span>
          <span class="user-admin-card-meta"><span class="handling-chip">${escapeHtml(statusLabel(user.role || ''))}</span><span class="user-admin-scope-label">${user.role === 'manager' && !allScopes ? '' : 'Responsible ·'}</span><span class="user-admin-scope-value" title="${escapeHtml(allScopes || scopeDisplay)}">${escapeHtml(scopeDisplay)}</span></span>
        </button>`;
      }).join('') : '<div class="user-admin-empty">No users match the selected filters.</div>';
      $('userAdminList').querySelectorAll('[data-managed-user]').forEach(button => button.addEventListener('click', () => editManagedUser(button.dataset.managedUser || '')));
    }

    function selectedManagedResponsibles() {
      return Array.from($('managedResponsibleOptions').querySelectorAll('input[type="checkbox"]:checked')).map(input => input.value);
    }

    function renderManagedResponsibleOptions(query = '', selectedValues = null) {
      const selected = new Set(selectedValues || selectedManagedResponsibles());
      const needle = String(query || '').trim().toLowerCase();
      const options = managedResponsibleOptions.filter(item => !needle || item.toLowerCase().includes(needle));
      $('managedResponsibleOptions').innerHTML = options.length ? options.map((item, index) => {
        const id = `managed-responsible-${index}`;
        return `<label class="user-responsible-option" for="${id}"><input id="${id}" type="checkbox" value="${escapeHtml(item)}" ${selected.has(item) ? 'checked' : ''}><span>${escapeHtml(item)}</span></label>`;
      }).join('') : '<div class="meta">No Responsible options match.</div>';
    }

    function addManagedResponsible() {
      const field = $('managedResponsibleAdd');
      const value = field.value.trim();
      $('managedResponsibleAddError').className = 'field-message';
      $('managedResponsibleAddError').textContent = '';
      field.removeAttribute('aria-invalid');
      if (!value || value.length > 80 || !/^[A-Za-z0-9][A-Za-z0-9._+-]*$/.test(value)) {
        field.setAttribute('aria-invalid', 'true');
        $('managedResponsibleAddError').className = 'field-message error';
        $('managedResponsibleAddError').textContent = 'Use letters, numbers, dots, underscores, plus signs, or hyphens.';
        field.focus();
        return;
      }
      const selected = new Set(selectedManagedResponsibles());
      const existing = managedResponsibleOptions.find(item => item.toLowerCase() === value.toLowerCase());
      const canonical = existing || value;
      if (!existing) {
        managedResponsibleOptions.push(value);
        managedResponsibleOptions.sort((a, b) => a.localeCompare(b, undefined, {sensitivity:'base'}));
      }
      selected.add(canonical);
      $('managedResponsibleSearch').value = '';
      renderManagedResponsibleOptions('', selected);
      field.value = '';
      field.focus();
    }

    function clearManagedUserValidation() {
      $('userAdminValidation').hidden = true;
      $('userAdminValidation').textContent = '';
      for (const id of ['managedUsername', 'managedRole']) {
        $(id).removeAttribute('aria-invalid');
      }
      for (const id of ['managedUsernameError', 'managedRoleError', 'managedResponsiblesError', 'managedResponsibleAddError']) {
        $(id).className = 'field-message';
        $(id).textContent = '';
      }
      $('userAdminSaveStatus').className = 'status';
      $('userAdminSaveStatus').textContent = '';
    }

    function updateManagedResponsibleMode() {
      const manager = $('managedRole').value === 'manager';
      $('managedResponsibleFieldset').classList.toggle('manager-global', manager);
      $('managedResponsibleRequired').hidden = manager;
      $('managedResponsibleLegend').textContent = manager ? 'Access scope · Global' : 'Responsible scope';
      $('managedResponsiblesHelp').innerHTML = manager
        ? '<strong>Global access.</strong> Manager can view and operate all Responsible scopes; mappings are not applied.'
        : '<strong>Access scope, not a reporting line.</strong> It matches Responsible identifiers on report folders and Jira Issues. Select at least one scope.';
      if (manager) {
        $('managedResponsiblesError').textContent = '';
        $('managedResponsiblesError').className = 'field-message';
      }
    }

    function showManagedUserForm(user = null, focusField = true) {
      managedUserCreating = !user;
      selectedManagedUsername = user ? String(user.username || '') : '';
      $('userAdminEmpty').hidden = true;
      $('userAdminForm').hidden = false;
      $('userAdminFormTitle').textContent = user ? `Edit ${user.username}` : 'Create user';
      $('userAdminFormMeta').textContent = user
        ? `Revision ${user.revision || 1} · Updated ${formatDateTime(user.updated_at)}`
        : 'A strong temporary password will be generated after the account is created.';
      $('managedUsername').value = user ? user.username : '';
      $('managedUsername').readOnly = Boolean(user);
      $('managedRole').value = user ? user.role : 'developer';
      updateManagedResponsibleMode();
      $('managedActive').checked = user ? user.active !== false : true;
      $('userAdminForm').dataset.revision = String(user ? (user.revision || 1) : 0);
      const selected = new Set(user ? (user.responsibles || []) : []);
      renderManagedResponsibleOptions('', selected);
      $('managedResponsibleSearch').value = '';
      $('managedResponsibleAdd').value = '';
      $('resetManagedPasswordBtn').hidden = !user;
      const statusValue = user ? managedUserStatus(user) : 'active';
      $('userAdminFormStatusChip').dataset.status = statusValue;
      $('userAdminFormStatusChip').textContent = user ? managedUserStatusLabel(user) : 'New account';
      clearManagedUserValidation();
      renderManagedUsers();
      if (focusField) window.setTimeout(() => (user ? $('managedRole') : $('managedUsername')).focus(), 0);
    }

    function editManagedUser(username) {
      const user = managedUserByName(username);
      if (user) showManagedUserForm(user);
    }

    function validateManagedUserForm() {
      clearManagedUserValidation();
      const username = $('managedUsername').value.trim();
      const role = $('managedRole').value;
      const responsibles = selectedManagedResponsibles();
      const errors = [];
      const setError = (fieldId, errorId, message) => {
        $(fieldId)?.setAttribute('aria-invalid', 'true');
        $(errorId).className = 'field-message error';
        $(errorId).textContent = message;
        errors.push({fieldId, message});
      };
      if (!/^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$/.test(username)) {
        setError('managedUsername', 'managedUsernameError', 'Use 3–64 letters, numbers, dots, underscores, or hyphens.');
      } else if (managedUserCreating && managedUserByName(username)) {
        setError('managedUsername', 'managedUsernameError', 'This username already exists.');
      }
      if (!managedUserRoles.includes(role)) setError('managedRole', 'managedRoleError', 'Select Developer, Auditor, or Manager.');
      if (role !== 'manager' && !responsibles.length) {
        $('managedResponsiblesError').className = 'field-message error';
        $('managedResponsiblesError').textContent = 'Select at least one Responsible mapping.';
        errors.push({fieldId: 'managedResponsibleSearch', message: 'Responsible mapping is required.'});
      }
      if (errors.length) {
        $('userAdminValidation').hidden = false;
        $('userAdminValidation').className = 'validation-summary error';
        $('userAdminValidation').textContent = `Please correct ${errors.length} field${errors.length === 1 ? '' : 's'} before saving.`;
        $(errors[0].fieldId)?.focus();
        return null;
      }
      return {
        username,
        role,
        active: $('managedActive').checked,
        responsibles,
        revision: Number($('userAdminForm').dataset.revision || 0)
      };
    }

    async function saveManagedUser() {
      const payload = validateManagedUserForm();
      if (!payload) return;
      $('userAdminSaveStatus').className = 'status running';
      $('userAdminSaveStatus').textContent = managedUserCreating ? 'Creating user…' : 'Saving user…';
      const data = await fetchJson('/api/admin/users/save', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'Idempotency-Key': adminRequestId('user-save')},
        body: JSON.stringify(payload)
      });
      const created = managedUserCreating;
      const index = managedUsers.findIndex(item => String(item.username || '').toLowerCase() === String(data.user.username || '').toLowerCase());
      if (index >= 0) managedUsers[index] = data.user; else managedUsers.push(data.user);
      managedUsers.sort((a, b) => String(a.username).localeCompare(String(b.username), undefined, {sensitivity:'base'}));
      showManagedUserForm(data.user, !data.temporary_password);
      $('userAdminSaveStatus').className = 'status ok';
      $('userAdminSaveStatus').textContent = created ? 'User created.' : 'User saved.';
      if (data.temporary_password) showTemporaryPassword(data.temporary_password, data.user.username);
    }

    async function loadManagedUsers() {
      $('userAdminList').innerHTML = '<div class="meta">Loading users…</div>';
      const data = await fetchJson('/api/admin/users');
      managedUsers = Array.isArray(data.users) ? data.users : [];
      managedUserRoles = Array.isArray(data.roles) ? data.roles : managedUserRoles;
      managedResponsibleOptions = Array.isArray(data.responsible_options) ? data.responsible_options : [];
      renderManagedUsers();
      if (selectedManagedUsername) {
        const selected = managedUserByName(selectedManagedUsername);
        if (selected) showManagedUserForm(selected);
      } else if (managedUsers.length) {
        showManagedUserForm(managedUsers[0], false);
      }
    }

    async function openUserManagement() {
      if (currentUserRole !== 'manager') return;
      userAdminReturnFocus = document.activeElement;
      $('userAdminModal').hidden = false;
      $('userAdminEmpty').hidden = false;
      $('userAdminForm').hidden = true;
      selectedManagedUsername = '';
      await loadManagedUsers();
      $('userAdminSearch').focus();
    }

    function closeUserManagement() {
      $('userAdminModal').hidden = true;
      if (userAdminReturnFocus && document.contains(userAdminReturnFocus)) userAdminReturnFocus.focus();
      userAdminReturnFocus = null;
    }

    function openManagedPasswordReset() {
      const username = $('managedUsername').value.trim();
      if (!username || managedUserCreating) return;
      $('userResetConfirmName').textContent = username;
      $('userResetConfirmInput').value = '';
      $('userResetConfirmError').textContent = '';
      $('confirmUserResetBtn').disabled = true;
      $('userResetConfirmModal').hidden = false;
      $('userResetConfirmInput').focus();
    }

    function closeManagedPasswordReset() {
      $('userResetConfirmModal').hidden = true;
      $('resetManagedPasswordBtn').focus();
    }

    async function resetManagedPassword() {
      const username = $('managedUsername').value.trim();
      if ($('userResetConfirmInput').value.trim() !== username) {
        $('userResetConfirmError').className = 'field-message error';
        $('userResetConfirmError').textContent = 'Enter the exact username to continue.';
        $('userResetConfirmInput').focus();
        return;
      }
      const data = await fetchJson('/api/admin/users/reset-password', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'Idempotency-Key': adminRequestId('user-password-reset')},
        body: JSON.stringify({username})
      });
      $('userResetConfirmModal').hidden = true;
      const index = managedUsers.findIndex(item => String(item.username || '').toLowerCase() === String(data.user.username || '').toLowerCase());
      if (index >= 0) managedUsers[index] = data.user;
      showManagedUserForm(data.user, false);
      showTemporaryPassword(data.temporary_password, data.user.username);
    }

    function showTemporaryPassword(password, username) {
      $('temporaryPasswordDescription').textContent = `Copy the temporary password for ${username} now. It is shown only once and must be changed after sign-in.`;
      $('temporaryPasswordValue').value = password || '';
      $('temporaryPasswordStatus').className = 'status';
      $('temporaryPasswordStatus').textContent = '';
      $('temporaryPasswordModal').hidden = false;
      $('copyTemporaryPasswordBtn').focus();
    }

    function closeTemporaryPassword() {
      $('temporaryPasswordValue').value = '';
      $('temporaryPasswordModal').hidden = true;
      $('saveManagedUserBtn').focus();
    }

    async function copyTemporaryPassword() {
      const field = $('temporaryPasswordValue');
      const value = field.value;
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(value);
      } else {
        field.select();
        if (!document.execCommand('copy')) throw new Error('Copy is not supported by this browser.');
      }
      $('temporaryPasswordStatus').className = 'status ok';
      $('temporaryPasswordStatus').textContent = 'Temporary password copied.';
    }

    function openChangePassword(required = false) {
      changePasswordRequired = Boolean(required);
      $('changePasswordForm').reset();
      $('changePasswordValidation').hidden = true;
      $('changePasswordValidation').textContent = '';
      $('changePasswordStatus').className = 'status';
      $('changePasswordStatus').textContent = '';
      for (const id of ['currentPasswordField', 'newPasswordField', 'confirmNewPasswordField']) $(id).removeAttribute('aria-invalid');
      $('cancelChangePasswordBtn').hidden = changePasswordRequired;
      $('changePasswordDescription').textContent = changePasswordRequired
        ? 'You must replace the temporary password before using CodeReviewer. Use 14–128 characters with uppercase, lowercase, number, and symbol.'
        : 'Use 14–128 characters with uppercase, lowercase, number, and symbol.';
      $('changePasswordModal').hidden = false;
      $('currentPasswordField').focus();
    }

    function closeChangePassword() {
      if (changePasswordRequired) return;
      $('changePasswordForm').reset();
      $('changePasswordModal').hidden = true;
      $('changePasswordBtn').focus();
    }

    async function changeOwnPassword() {
      const currentPassword = $('currentPasswordField').value;
      const newPassword = $('newPasswordField').value;
      const confirmation = $('confirmNewPasswordField').value;
      const errors = [];
      for (const id of ['currentPasswordField', 'newPasswordField', 'confirmNewPasswordField']) $(id).removeAttribute('aria-invalid');
      if (!currentPassword) errors.push({id:'currentPasswordField', message:'Current password is required.'});
      if (newPassword.length < 14 || newPassword.length > 128 || !/[A-Z]/.test(newPassword) || !/[a-z]/.test(newPassword) || !/[0-9]/.test(newPassword) || !/[^A-Za-z0-9]/.test(newPassword)) {
        errors.push({id:'newPasswordField', message:'New password must contain 14–128 characters with uppercase, lowercase, number, and symbol.'});
      }
      if (newPassword !== confirmation) errors.push({id:'confirmNewPasswordField', message:'Password confirmation does not match.'});
      if (errors.length) {
        errors.forEach(item => $(item.id).setAttribute('aria-invalid', 'true'));
        $('changePasswordValidation').hidden = false;
        $('changePasswordValidation').className = 'validation-summary error';
        $('changePasswordValidation').textContent = errors.map(item => item.message).join(' ');
        $(errors[0].id).focus();
        return;
      }
      $('changePasswordValidation').hidden = true;
      $('changePasswordStatus').className = 'status running';
      $('changePasswordStatus').textContent = 'Changing password…';
      await fetchJson('/api/change-password', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({current_password:currentPassword, new_password:newPassword})
      });
      $('changePasswordStatus').className = 'status ok';
      $('changePasswordStatus').textContent = 'Password changed. Redirecting to sign in…';
      window.setTimeout(() => window.location.assign('/login'), 900);
    }

    function trapDialogFocus(event, modal, closeAction) {
      if (event.key === 'Escape') {
        event.preventDefault();
        closeAction();
        return;
      }
      if (event.key !== 'Tab') return;
      const focusable = Array.from(modal.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), [href], [tabindex]:not([tabindex="-1"])')).filter(item => !item.hidden && item.offsetParent !== null);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }

    async function loadReleaseNotes({ retry = true } = {}) {
      $('releaseNotesContent').className = 'markdown-preview release-notes-content';
      $('releaseNotesContent').textContent = 'Loading release notes…';
      $('releaseNotesContent').scrollTop = 0;
      try {
        let data;
        try {
          data = await fetchJson('/api/public-release-notes', {cache:'no-store'});
        } catch (firstError) {
          if (!retry) throw firstError;
          await new Promise(resolve => window.setTimeout(resolve, 350));
          data = await fetchJson('/api/public-release-notes', {cache:'no-store'});
        }
        const markdown = String(data.markdown || 'No release notes available.').replace(/^#\\s+[^\\n]+\\n+/, '');
        releaseNotesCachedMarkdown = markdown;
        $('releaseNotesContent').innerHTML = renderMarkdown(markdown, {anchorPrefix: 'release-notes-'});
        const updated = String(data.updated_at || '');
        $('releaseNotesUpdated').dateTime = updated;
        $('releaseNotesUpdated').textContent = updated
          ? `Last updated ${updated.replace('T', ' ')} (Asia/Shanghai)`
          : (data.source_available === false ? 'Release summary source temporarily unavailable.' : '');
      } catch (error) {
        const cached = releaseNotesCachedMarkdown
          ? `<details><summary>Show the last successfully loaded summary</summary>${renderMarkdown(releaseNotesCachedMarkdown, {anchorPrefix:'release-notes-cache-'})}</details>`
          : '';
        $('releaseNotesContent').className = 'markdown-preview release-notes-content';
        $('releaseNotesContent').innerHTML = `<div class="release-notes-error" role="alert">
          <strong>Release Notes are temporarily unavailable</strong>
          <span class="meta">The connection was interrupted while loading this optional content. Review functions are not affected.</span>
          <button id="retryReleaseNotesBtn" class="secondary" type="button">Retry</button>
        </div>${cached}`;
        $('retryReleaseNotesBtn').addEventListener('click', event => singleFlight('release-notes-retry', event.currentTarget, () => loadReleaseNotes({retry:false})).catch(() => {}));
      }
    }

    async function openReleaseNotes() {
      releaseNotesReturnFocus = document.activeElement;
      $('releaseNotesModal').hidden = false;
      await loadReleaseNotes();
      requestAnimationFrame(() => $('closeReleaseNotesBtn').focus());
    }

    function renderAppHealth(data, detailed = false) {
      const status = String(data?.status || 'unhealthy');
      const label = status === 'healthy' ? 'Healthy' : (status === 'degraded' ? 'Degraded' : 'Unavailable');
      $('appHealthBtn').dataset.status = status;
      $('appHealthLabel').textContent = label;
      $('appHealthBtn').setAttribute('aria-label', `CodeReviewer health: ${label}`);
      if (!detailed) return;
      $('healthDetailMeta').textContent = `${label} · Version ${data.version || '-'} · Checked ${String(data.updated_at || '').replace('T', ' ')}`;
      const checks = Array.isArray(data.checks) ? data.checks : [];
      $('healthDetailChecks').innerHTML = checks.map(check => `
        <div class="health-detail-check">
          <span class="health-dot" style="background:${check.ok ? 'var(--ok)' : 'var(--danger)'}" aria-hidden="true"></span>
          <div><strong>${escapeHtml(check.name || '-')}</strong><span>${escapeHtml(check.message || '')}${check.required === false ? ' · Optional' : ''}</span></div>
        </div>`).join('');
    }

    async function loadAppHealth(details = false) {
      const data = await fetchJson(details ? '/api/health-details' : '/api/health');
      renderAppHealth(data, details);
      return data;
    }

    async function openHealthDetails() {
      healthReturnFocus = document.activeElement;
      $('healthDetailModal').hidden = false;
      $('healthDetailChecks').innerHTML = '';
      $('healthDetailMeta').textContent = 'Running service checks…';
      try { await loadAppHealth(true); }
      catch (error) { $('healthDetailMeta').textContent = error.message; }
      requestAnimationFrame(() => $('closeHealthDetailBtn').focus());
    }

    function closeHealthDetails() {
      $('healthDetailModal').hidden = true;
      const target = healthReturnFocus;
      healthReturnFocus = null;
      if (target && typeof target.focus === 'function') requestAnimationFrame(() => target.focus());
    }

    function configurationInputMarkup(editor, index) {
      const value = editor.value;
      if (editor.path?.at(-1) === 'application') {
        const options = ['WVAdmin', 'iTrade Client', 'Services Terminal', 'DPS', 'Unmapped'];
        return `<select id="configurationInput${index}">${options.map(option => `<option value="${escapeHtml(option)}" ${value === option ? 'selected' : ''}>${escapeHtml(option)}</option>`).join('')}</select>`;
      }
      if (editor.type === 'boolean') {
        return `<select id="configurationInput${index}"><option value="true" ${value === true ? 'selected' : ''}>Enabled</option><option value="false" ${value === false ? 'selected' : ''}>Disabled</option></select>`;
      }
      if (editor.type === 'list' || Array.isArray(value)) {
        return `<textarea id="configurationInput${index}" rows="1" spellcheck="false">${escapeHtml((Array.isArray(value) ? value : []).join(', '))}</textarea>`;
      }
      const inputType = editor.type === 'number' || typeof value === 'number' ? 'number' : 'text';
      return `<input id="configurationInput${index}" type="${inputType}" value="${escapeHtml(value ?? '')}" autocomplete="off">`;
    }

    function configurationFieldCard(editor, index) {
      return `<div class="configuration-field-card" data-configuration-haystack="${escapeHtml(editor.haystack || '')}">
        <label><span class="field-label">${escapeHtml(editor.label || editor.path.at(-1))}</span><span class="meta">${escapeHtml(editor.key || editor.path.join('.'))}</span>${configurationInputMarkup(editor, index)}</label>
        <button class="secondary" type="button" data-save-configuration="${index}">Save</button>
      </div>`;
    }

    function renderConfiguration() {
      if (!configurationData) return;
      const query = ($('configurationSearch').value || '').trim().toLowerCase();
      configurationEditors = [];
      $('configurationRevision').textContent = `Revision ${String(configurationData.revision || '').slice(0, 8)}`;
      document.querySelectorAll('[data-configuration-view]').forEach(button => {
        const active = button.dataset.configurationView === configurationView;
        button.classList.toggle('active', active);
        button.setAttribute('aria-current', active ? 'page' : 'false');
      });
      $('addConfigurationProjectBtn').hidden = configurationView !== 'projects';
      let markup = '';
      if (configurationView === 'application') {
        const groups = new Map();
        (configurationData.app_fields || []).forEach(field => {
          const haystack = `${field.category} ${field.label} ${field.key}`.toLowerCase();
          if (query && !haystack.includes(query)) return;
          if (!groups.has(field.category)) groups.set(field.category, []);
          groups.get(field.category).push({...field, haystack});
        });
        markup = [...groups.entries()].map(([category, fields]) => {
          const cards = fields.map(field => {
            const index = configurationEditors.push(field) - 1;
            return configurationFieldCard(field, index);
          }).join('');
          return `<section class="configuration-section"><h3>${escapeHtml(category)}</h3><div class="configuration-field-grid">${cards}</div></section>`;
        }).join('');
      } else if (configurationView === 'projects') {
        const allProjects = configurationData.projects || [];
        const projects = allProjects.filter(project => {
          const haystack = `${project.group} ${project.module} ${project.id} ${JSON.stringify(project.values || {})}`.toLowerCase();
          return !query || haystack.includes(query);
        });
        const groups = [...new Set(allProjects.map(project => project.group).filter(Boolean))].sort();
        const createCard = `<section id="configurationProjectCreate" class="configuration-project-create" ${configurationCreateVisible ? '' : 'hidden'}>
          <div class="configuration-project-head"><div><h3>Add GitLab project</h3><div class="meta">Add one module beneath an existing project group. Values are validated before the atomic save.</div></div></div>
          <div class="configuration-project-create-grid">
            <label><span class="field-label">Group <span class="required-mark" aria-hidden="true">*</span></span><select id="newConfigurationProjectGroup">${groups.map(group => `<option value="${escapeHtml(group)}">${escapeHtml(group)}</option>`).join('')}</select></label>
            <label><span class="field-label">Module key <span class="required-mark" aria-hidden="true">*</span></span><input id="newConfigurationProjectModule" maxlength="80" placeholder="module-name"></label>
            <label><span class="field-label">Application <span class="required-mark" aria-hidden="true">*</span></span><select id="newConfigurationProjectApplication">${['WVAdmin','iTrade Client','Services Terminal','DPS'].map(item => `<option>${escapeHtml(item)}</option>`).join('')}</select></label>
            <label style="grid-column:span 2"><span class="field-label">Repository URL <span class="required-mark" aria-hidden="true">*</span></span><input id="newConfigurationProjectUrl" placeholder="https://gitlab.example.com/group/project.git"></label>
            <label><span class="field-label">Responsible</span><input id="newConfigurationProjectResponsible" placeholder="kevin.tan+wen.yi"></label>
            <label><span class="field-label">Project type</span><select id="newConfigurationProjectType"><option value="">Inherited / unspecified</option><option value="frontend">Frontend</option><option value="backend">Backend</option><option value="build">Build</option></select></label>
          </div>
          <div class="configuration-project-create-actions"><button id="cancelConfigurationProjectBtn" class="secondary" type="button">Cancel</button><button id="saveConfigurationProjectBtn" type="button">Add project</button></div>
        </section>`;
        markup = createCard + projects.map(project => {
          const fields = Object.entries(project.values || {}).map(([key, value]) => {
            const field = {
              path:[...(project.path || []), key],
              key,
              label:key.replaceAll('_', ' ').replace(/\\b\\w/g, char => char.toUpperCase()),
              value,
              type:Array.isArray(value) ? 'list' : (typeof value === 'boolean' ? 'boolean' : typeof value === 'number' ? 'number' : 'text'),
              haystack:`${project.id} ${key} ${value}`.toLowerCase()
            };
            const index = configurationEditors.push(field) - 1;
            return configurationFieldCard(field, index);
          }).join('');
          const deleteAction = (project.path || []).length > 1 ? `<button class="secondary small-action configuration-project-delete" type="button" data-delete-configuration-project="${escapeHtml(project.id)}">Delete</button>` : '';
          return `<article class="configuration-project-card"><div class="configuration-project-head"><div><h3>${escapeHtml(project.module || project.id)}</h3><div class="meta">${escapeHtml(project.group)} · ${escapeHtml(project.id)}</div></div><div class="actions"><span class="status-chip">GitLab</span>${deleteAction}</div></div><div class="configuration-field-grid">${fields}</div></article>`;
        }).join('');
      } else {
        const backups = configurationData.backups || [];
        markup = backups.length ? `<section class="configuration-project-card">${backups.map(backup => `<div class="configuration-backup"><div><strong>${escapeHtml(backup.created_at || backup.name)}</strong><div class="meta">${escapeHtml(backup.name)} · Revision ${escapeHtml(String(backup.revision || '').slice(0, 8))}</div></div><button class="secondary" type="button" data-restore-configuration="${escapeHtml(backup.name)}">Restore</button></div>`).join('')}</section>` : '<div class="configuration-empty">No Web configuration backups yet. A backup is created before each saved change.</div>';
      }
      $('configurationContent').innerHTML = markup || '<div class="configuration-empty">No configuration records match this search.</div>';
      $('configurationContent').querySelectorAll('[data-save-configuration]').forEach(button => button.addEventListener('click', () => {
        const index = Number(button.dataset.saveConfiguration);
        singleFlight(`save-configuration-${index}`, button, () => saveConfigurationField(index)).catch(error => {
          $('configurationStatus').className = 'status error';
          $('configurationStatus').textContent = error.message;
        });
      }));
      $('configurationContent').querySelectorAll('[data-restore-configuration]').forEach(button => button.addEventListener('click', () => {
        singleFlight(`restore-configuration-${button.dataset.restoreConfiguration}`, button, () => restoreConfigurationBackup(button.dataset.restoreConfiguration || '')).catch(error => {
          $('configurationStatus').className = 'status error';
          $('configurationStatus').textContent = error.message;
        });
      }));
      $('configurationContent').querySelectorAll('[data-delete-configuration-project]').forEach(button => button.addEventListener('click', () => {
        singleFlight(`delete-configuration-project-${button.dataset.deleteConfigurationProject}`, button, () => deleteConfigurationProject(button.dataset.deleteConfigurationProject || '')).catch(error => {
          $('configurationStatus').className = 'status error';
          $('configurationStatus').textContent = error.message;
        });
      }));
      $('cancelConfigurationProjectBtn')?.addEventListener('click', () => { configurationCreateVisible = false; renderConfiguration(); });
      $('saveConfigurationProjectBtn')?.addEventListener('click', () => singleFlight('add-configuration-project', $('saveConfigurationProjectBtn'), addConfigurationProject).catch(error => {
        $('configurationStatus').className = 'status error';
        $('configurationStatus').textContent = error.message;
      }));
    }

    function readConfigurationEditorValue(editor, index) {
      const field = $(`configurationInput${index}`);
      if (editor.type === 'boolean') return field.value === 'true';
      if (editor.type === 'number' || typeof editor.value === 'number') {
        const value = Number(field.value);
        if (!Number.isFinite(value)) throw new Error('Enter a valid number.');
        return value;
      }
      if (editor.type === 'list' || Array.isArray(editor.value)) {
        return field.value.split(/[\\n,]+/).map(item => item.trim()).filter(Boolean);
      }
      return field.value.trim();
    }

    async function saveConfigurationField(index) {
      const editor = configurationEditors[index];
      if (!editor || !configurationData) return;
      $('configurationStatus').className = 'status running';
      $('configurationStatus').textContent = `Validating ${editor.label || editor.key}…`;
      const data = await fetchJson('/api/admin/configuration/save', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          path:editor.path,
          value:readConfigurationEditorValue(editor, index),
          revision:configurationData.revision,
          request_id:adminRequestId('configuration-save')
        })
      });
      configurationData = data;
      $('configurationStatus').className = 'status ok';
      $('configurationStatus').textContent = 'Configuration saved and applied to new Review jobs.';
      renderConfiguration();
      await loadProjects();
      await loadRuntimeConfig();
    }

    async function addConfigurationProject() {
      if (!configurationData) return;
      const group = $('newConfigurationProjectGroup').value;
      const module = $('newConfigurationProjectModule').value.trim();
      const repositoryUrl = $('newConfigurationProjectUrl').value.replace(/\\s+/g, '').trim();
      if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(module)) throw new Error('Module key may contain letters, numbers, dots, underscores, or hyphens.');
      const data = await fetchJson('/api/admin/configuration/project', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          action:'upsert',
          path:[group, module],
          values:{
            repository_url:repositoryUrl,
            application:$('newConfigurationProjectApplication').value,
            responsible:$('newConfigurationProjectResponsible').value.trim(),
            type:$('newConfigurationProjectType').value
          },
          revision:configurationData.revision,
          request_id:adminRequestId('configuration-project-add')
        })
      });
      configurationData = data;
      configurationCreateVisible = false;
      $('configurationStatus').className = 'status ok';
      $('configurationStatus').textContent = 'GitLab project added.';
      renderConfiguration();
      await loadProjects();
    }

    async function deleteConfigurationProject(id) {
      if (!configurationData || !id) return;
      const project = (configurationData.projects || []).find(item => item.id === id);
      if (!project) throw new Error('Project no longer exists. Refresh and try again.');
      if (!window.confirm(`Delete ${project.module || project.id} from the Effective Config? A restore point will be created first.`)) return;
      const data = await fetchJson('/api/admin/configuration/project', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          action:'delete',
          path:project.path,
          revision:configurationData.revision,
          request_id:adminRequestId('configuration-project-delete')
        })
      });
      configurationData = data;
      $('configurationStatus').className = 'status ok';
      $('configurationStatus').textContent = 'GitLab project deleted. Use Backups & restore to undo.';
      renderConfiguration();
      await loadProjects();
    }

    async function restoreConfigurationBackup(name) {
      if (!configurationData || !name) return;
      if (!window.confirm(`Restore configuration backup ${name}? The current Web override will be backed up first.`)) return;
      $('configurationStatus').className = 'status running';
      $('configurationStatus').textContent = 'Validating and restoring backup…';
      const data = await fetchJson('/api/admin/configuration/restore', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({backup:name, revision:configurationData.revision, request_id:adminRequestId('configuration-restore')})
      });
      configurationData = data;
      $('configurationStatus').className = 'status ok';
      $('configurationStatus').textContent = 'Configuration restored.';
      renderConfiguration();
      await loadProjects();
      await loadRuntimeConfig();
    }

    async function loadConfiguration() {
      $('configurationStatus').className = 'status running';
      $('configurationStatus').textContent = 'Loading effective configuration…';
      configurationData = await fetchJson('/api/admin/configuration');
      $('configurationStatus').className = 'status';
      $('configurationStatus').textContent = '';
      renderConfiguration();
    }

    async function openConfiguration() {
      configurationReturnFocus = document.activeElement;
      $('configurationModal').hidden = false;
      await loadConfiguration();
      requestAnimationFrame(() => $('configurationSearch').focus());
    }

    function closeConfiguration() {
      $('configurationModal').hidden = true;
      const target = configurationReturnFocus;
      configurationReturnFocus = null;
      if (target && typeof target.focus === 'function') requestAnimationFrame(() => target.focus());
    }

    function closeReleaseNotes() {
      $('releaseNotesModal').hidden = true;
      const target = releaseNotesReturnFocus;
      releaseNotesReturnFocus = null;
      if (target && typeof target.focus === 'function') requestAnimationFrame(() => target.focus());
    }

    function handleReleaseNotesKeydown(event) {
      if ($('releaseNotesModal').hidden) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        closeReleaseNotes();
        return;
      }
      if (event.key !== 'Tab') return;
      const focusable = Array.from($('releaseNotesModal').querySelectorAll('button:not([disabled]), [href], [tabindex]:not([tabindex="-1"])')).filter(element => !element.hidden);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    async function loadProjects() {
      const data = await fetchJson('/api/projects');
      renderProjects(Array.isArray(data.projects) ? data.projects : []);
    }

    function openCoverage() {
      $('coverageModal').hidden = false;
      if (!coverageJobSnapshot || !coverageJobIsActive(coverageJobSnapshot)) {
        $('coverageJira').value = '';
        $('coverageSprint').value = ($('sprint')?.value || '').trim();
        $('coverageFilter').value = ($('jiraFilter')?.value || '').trim();
      }
      setCoverageView('overview');
      $('coverageRoleHint').textContent = currentUserRole === 'manager'
        ? 'Manager view: all configured responsible teams and Sprint issues.'
        : 'Auditor view: only GitLab projects and reports matching your responsible scope.';
      resumeCoverageJob({ preserveJiraInput: true });
      requestAnimationFrame(() => $('coverageJira').focus());
    }

    function closeCoverage() {
      $('coverageModal').hidden = true;
    }

    function setCoverageView(view) {
      coverageView = view === 'issues' ? 'issues' : 'overview';
      document.querySelectorAll('[data-coverage-view]').forEach(tab => {
        const active = tab.dataset.coverageView === coverageView;
        tab.classList.toggle('active', active);
        tab.setAttribute('aria-selected', active ? 'true' : 'false');
      });
      document.querySelectorAll('[data-coverage-panel]').forEach(panel => {
        panel.hidden = panel.dataset.coveragePanel !== coverageView;
      });
    }

    function coverageJobIsActive(job) {
      return ['queued', 'running'].includes(String(job?.status || ''));
    }

    function syncCoverageControls() {
      const active = coverageJobIsActive(coverageJobSnapshot);
      $('coverageScanBtn').disabled = active || submissionFlights.has('coverage-scan');
      if (active) $('coverageScanBtn').setAttribute('aria-busy', 'true');
      else $('coverageScanBtn').removeAttribute('aria-busy');
      $('coverageScanBtn').textContent = active ? 'Scanning…' : 'Scan';
    }

    function rememberCoverageJob(jobId) {
      coverageJobId = jobId || '';
    }

    function coverageElapsedSeconds(job = coverageJobSnapshot) {
      if (!job) return 0;
      const started = Number(job.started_at || job.created_at || 0);
      const finished = Number(job.finished_at || 0);
      if (!started) return 0;
      return Math.max(0, Math.floor((finished || Date.now() / 1000) - started));
    }

    function formatCoverageDuration(seconds) {
      const safe = Math.max(0, Number(seconds || 0));
      const minutes = Math.floor(safe / 60);
      const remainder = Math.floor(safe % 60);
      return `${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`;
    }

    function updateCoverageTiming() {
      const job = coverageJobSnapshot;
      if (!job || !coverageJobIsActive(job)) return;
      const elapsed = coverageElapsedSeconds(job);
      const progress = $('coverageProgress');
      if (elapsed < COVERAGE_LONG_SCAN_SECONDS) {
        progress.classList.remove('long-running');
        $('coverageProgressTiming').textContent = job.status === 'queued'
          ? `Waiting for scan slot · ${formatCoverageDuration(elapsed)}`
          : `Elapsed ${formatCoverageDuration(elapsed)}`;
        return;
      }
      progress.classList.add('long-running');
      const remaining = COVERAGE_TIMEOUT_THRESHOLD_SECONDS - elapsed;
      $('coverageProgressTiming').textContent = remaining > 0
        ? `Elapsed ${formatCoverageDuration(elapsed)} · Timeout countdown ${formatCoverageDuration(remaining)}`
        : `Elapsed ${formatCoverageDuration(elapsed)} · Timeout threshold exceeded; still running`;
    }

    function startCoverageTiming() {
      window.clearInterval(coverageTimingTimer);
      updateCoverageTiming();
      if (coverageJobIsActive(coverageJobSnapshot)) {
        coverageTimingTimer = window.setInterval(updateCoverageTiming, 1000);
      }
    }

    function applyCoverageJob(job, options = {}) {
      if (!job) return;
      coverageJobSnapshot = job;
      rememberCoverageJob(job.id || '');
      const scope = job.scope || {};
      if (!options.preserveJiraInput && scope.jira != null) $('coverageJira').value = scope.jira || '';
      if (scope.sprint != null) $('coverageSprint').value = scope.sprint || '';
      if (scope.jira_filter != null) $('coverageFilter').value = scope.jira_filter || '';
      const status = String(job.status || '');
      const progress = job.progress || {};
      const percent = Math.max(0, Math.min(100, Number(progress.percent || 0)));
      const progressTitles = {
        queued: 'Waiting for scan slot',
        started: 'Loading review scope',
        start: 'Loading review scope',
        jira: 'Loading Jira issues',
        'discovery-issue': 'Matching merge requests',
        discover: 'Merge request discovery completed',
        'coverage-reports': 'Checking generated reports',
        'coverage-workflow': 'Checking Review handling',
        'coverage-finalizing': 'Calculating application readiness',
        completed: 'Coverage scan completed',
        failed: 'Coverage scan failed'
      };
      $('coverageProgressBar').style.width = `${percent}%`;
      $('coverageProgressTitle').textContent = progressTitles[String(progress.event || status)]
        || (status === 'running' ? 'Preparing Sprint overview' : 'Coverage scan');
      $('coverageProgressDetail').textContent = progress.message || (
        status === 'queued'
          ? 'Another coverage scan is using the discovery slot. This scan will start automatically.'
          : 'The scan continues in the background. You may safely close this window.'
      );
      $('coverageProgress').hidden = !coverageJobIsActive(job);
      $('coverageProgress').classList.toggle('long-running', coverageJobIsActive(job) && coverageElapsedSeconds(job) >= COVERAGE_LONG_SCAN_SECONDS);
      $('coverageStatus').className = status === 'failed' ? 'status error' : (status === 'done' ? 'status ok' : 'status');
      if (status === 'done') {
        renderCoverage(job.result || {});
        const issueCount = Array.isArray(job.result?.issues) ? job.result.issues.length : 0;
        $('coverageStatus').textContent = options.reused
          ? `Recent coverage result restored · ${issueCount} issue(s)`
          : `Coverage scan completed · ${issueCount} issue(s)`;
      } else if (status === 'failed') {
        $('coverageStatus').textContent = job.error || progress.message || 'Coverage scan failed.';
      } else {
        $('coverageStatus').textContent = 'Coverage discovery is running in the background. Closing this window will not stop it.';
      }
      syncCoverageControls();
      startCoverageTiming();
    }

    async function pollCoverageJob(jobId, options = {}) {
      window.clearTimeout(coveragePollTimer);
      if (!jobId) return;
      try {
        const data = await fetchJson(`/api/review-coverage-jobs/${encodeURIComponent(jobId)}`);
        applyCoverageJob(data.job || {}, options);
        if (coverageJobIsActive(data.job)) {
          coveragePollTimer = window.setTimeout(() => pollCoverageJob(jobId, options), 1500);
        }
      } catch (error) {
        if (jobId !== coverageJobId) return;
        $('coverageStatus').className = 'status error';
        $('coverageStatus').textContent = `Unable to refresh scan status: ${error.message}. Retrying automatically…`;
        coveragePollTimer = window.setTimeout(() => pollCoverageJob(jobId), 3000);
      }
    }

    async function resumeCoverageJob(options = {}) {
      try {
        const data = await fetchJson(
          coverageJobId
            ? `/api/review-coverage-jobs/${encodeURIComponent(coverageJobId)}`
            : '/api/review-coverage-jobs'
        );
        if (!data.job) return;
        applyCoverageJob(data.job, { reused: true, preserveJiraInput: Boolean(options.preserveJiraInput) });
        if (coverageJobIsActive(data.job)) pollCoverageJob(data.job.id || '', options);
      } catch (error) {
        rememberCoverageJob('');
      }
    }

    async function scanCoverage() {
      const jira = $('coverageJira').value.trim();
      const sprint = $('coverageSprint').value.trim();
      const jiraFilter = $('coverageFilter').value.trim();
      const fields = [$('coverageJira'), $('coverageSprint'), $('coverageFilter')];
      fields.forEach(field => field.removeAttribute('aria-invalid'));
      $('coverageValidation').className = 'field-message';
      $('coverageValidation').textContent = '';
      if (!jira && !sprint && !jiraFilter) {
        fields.forEach(field => field.setAttribute('aria-invalid', 'true'));
        $('coverageValidation').className = 'field-message error';
        $('coverageValidation').textContent = 'Enter Jira issues, a Sprint, or a Jira Filter ID.';
        $('coverageJira').focus();
        return;
      }
      $('coverageStatus').className = 'status';
      $('coverageStatus').textContent = 'Creating background coverage scan…';
      $('coverageProgress').hidden = false;
      $('coverageProgress').classList.remove('long-running');
      $('coverageProgressTitle').textContent = 'Starting coverage scan';
      $('coverageProgressDetail').textContent = 'Creating a resumable background task.';
      $('coverageProgressTiming').textContent = 'Elapsed 00:00';
      $('coverageProgressBar').style.width = '1%';
      try {
        const data = await fetchJson('/api/review-coverage-jobs', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ jira, sprint, jira_filter: jiraFilter })
        });
        const job = data.job || {};
        applyCoverageJob(job, { reused: Boolean(data.reused) });
        if (coverageJobIsActive(job)) pollCoverageJob(job.id || '');
      } catch (error) {
        $('coverageStatus').className = 'status error';
        $('coverageStatus').textContent = error.message;
        $('coverageProgress').hidden = true;
        syncCoverageControls();
      }
    }

    function renderCoverage(data = {}) {
      const counts = data.counts || {};
      const rows = Array.isArray(data.issues) ? data.issues : [];
      const reportCoverage = data.report_coverage || {};
      const withReports = Number(reportCoverage.issues_with_reports ?? rows.filter(item => Number(item.report_count || 0) > 0).length);
      const withoutReports = Number(reportCoverage.issues_without_reports ?? Math.max(0, rows.length - withReports));
      const reportTotal = withReports + withoutReports;
      const withPercent = reportTotal ? Math.round((withReports / reportTotal) * 100) : 0;
      const withoutPercent = reportTotal ? 100 - withPercent : 0;
      const breakdown = reportCoverage.generated_breakdown || {};
      const applicationProgress = Array.isArray(data.application_progress) ? data.application_progress : [];
      const scopeTotal = Number(reportCoverage.application_scope_count ?? applicationProgress.reduce((total, item) => total + Number(item.issue_count || 0), 0));
      const scopesWithReports = Number(reportCoverage.application_scopes_with_reports ?? applicationProgress.reduce((total, item) => total + Number(item.issues_with_reports || 0), 0));
      const scopesWithoutReports = Number(reportCoverage.application_scopes_without_reports ?? Math.max(0, scopeTotal - scopesWithReports));
      const scopeWithPercent = scopeTotal ? Math.round((scopesWithReports / scopeTotal) * 100) : 0;
      const scopeWithoutPercent = scopeTotal ? 100 - scopeWithPercent : 0;
      const generatedReportFiles = Number(reportCoverage.generated_report_files ?? rows.reduce((total, item) => total + Number(item.report_count || 0), 0));
      const missingIssueKeys = rows
        .filter(item => item.jira_key && (
          item.workflow_status === 'missing'
          || (Array.isArray(item.scope_statuses) && item.scope_statuses.some(scope => !Boolean(scope.has_report)))
        ))
        .map(item => String(item.jira_key).trim().toUpperCase())
        .filter(Boolean);
      $('coverageOverviewEmpty').hidden = true;
      $('coverageSummary').innerHTML = `
        <section class="coverage-report-totals" aria-label="Issue report coverage">
          <div class="coverage-totals-title"><strong>Unique Issue coverage</strong><span>Each Jira Issue is counted once, even when it spans multiple releases.</span></div>
          <div class="coverage-report-total"><div class="coverage-report-total-head"><span>Issues with reports</span></div><strong>${withReports}</strong><small>${withPercent}% of ${rows.length} unique Issue(s)</small></div>
          <div class="coverage-report-total"><div class="coverage-report-total-head"><span>Issues without reports</span></div><strong>${withoutReports}</strong><small>${withoutPercent}% of ${rows.length} unique Issue(s)</small></div>
          <div class="coverage-ratio-track" aria-hidden="true"><span class="coverage-ratio-with" style="width:${withPercent}%"></span><span class="coverage-ratio-without" style="width:${withoutPercent}%"></span></div>
        </section>
        <section class="coverage-scope-totals" aria-label="Application release report coverage">
          <div class="coverage-totals-title"><strong>Required release-scope reports</strong><span>One required report per Jira Issue × Application + Release Line; ${generatedReportFiles} historical report file(s) generated.</span></div>
          <div class="coverage-report-total"><div class="coverage-report-total-head"><span>Scope reports present</span></div><strong>${scopesWithReports}</strong><small>${scopeWithPercent}% of ${scopeTotal} required scope(s)</small></div>
          <div class="coverage-report-total"><div class="coverage-report-total-head"><span>Scope reports missing</span>${missingIssueKeys.length && currentPermissions.run_sprint_review ? `<button class="secondary coverage-run-missing" type="button" data-coverage-run-missing="${escapeHtml(missingIssueKeys.join(','))}">Run remaining</button>` : ''}</div><strong>${scopesWithoutReports}</strong><small>${scopeWithoutPercent}% of ${scopeTotal} required scope(s)</small></div>
          <div class="coverage-ratio-track" aria-hidden="true"><span class="coverage-ratio-with" style="width:${scopeWithPercent}%"></span><span class="coverage-ratio-without" style="width:${scopeWithoutPercent}%"></span></div>
        </section>
        <section class="coverage-report-lifecycle" aria-label="Generated report review lifecycle">
          <div class="coverage-lifecycle-head"><div><strong>Generated report lifecycle</strong><span>Unique Issue counts by latest Review Cycle state</span></div><div class="coverage-operational"><span>Generating ${Number(reportCoverage.generating ?? counts.running ?? 0)}</span><span>Failed ${Number(reportCoverage.failed ?? counts.failed ?? 0)}</span></div></div>
          <div class="coverage-lifecycle-grid">
            <div class="coverage-lifecycle-stat"><span>Handling</span><strong>${Number(breakdown.handling || 0)}</strong></div>
            <div class="coverage-lifecycle-stat"><span>Ready for Pass</span><strong>${Number(breakdown.ready || 0)}</strong></div>
            <div class="coverage-lifecycle-stat"><span>Review Pass</span><strong>${Number(breakdown.passed || 0)}</strong></div>
          </div>
        </section>`;
      $('coverageApplications').innerHTML = applicationProgress.length ? `
        <div class="coverage-applications-head">
          <div><strong>Application release readiness</strong><span>Issue-level progress for each application in the selected Sprint scope.</span></div>
          <span>100% means every scoped Issue is Review Pass and may enter its GIT_VERSION Release Gate.</span>
        </div>
        <div class="coverage-application-grid">
          ${applicationProgress.map((application) => {
            const stateCounts = application.counts || {};
            const name = String(application.scope_label || application.application || 'Unmapped');
            const unmapped = String(application.application || 'Unmapped') === 'Unmapped';
            const ready = Boolean(application.gate_ready);
            const percent = Math.max(0, Math.min(100, Number(application.readiness_percent || 0)));
            return `
              <article class="coverage-application-card${ready ? ' gate-ready' : ''}${unmapped ? ' unmapped' : ''}">
                <div class="coverage-application-title">
                  <strong title="${escapeHtml(name)}">${escapeHtml(name)}</strong>
                  <span class="coverage-application-percent">${percent}%</span>
                </div>
                <div class="coverage-application-progress-copy">
                  <strong>${Number(stateCounts.passed || 0)}/${Number(application.issue_count || 0)} Issue(s) Review Pass</strong>
                  <span>${Number(application.remaining || 0)} remaining</span>
                </div>
                <div class="coverage-application-track" aria-label="${escapeHtml(name)} release readiness ${percent}%"><span style="width:${percent}%"></span></div>
                <div class="coverage-application-report-coverage">
                  <span>Scope reports ${Number(application.issues_with_reports || 0)}/${Number(application.issue_count || 0)}</span>
                  <span>Without report ${Number(application.issues_without_reports || 0)}</span>
                </div>
                <div class="coverage-application-states" aria-label="Application Issue review states">
                  ${[
                    ['Generating', stateCounts.running],
                    ['Handling', stateCounts.pending],
                    ['Ready', stateCounts.ready],
                    ['Pass', stateCounts.passed],
                    ['Failed', stateCounts.failed],
                  ].map(([label, value]) => `<div class="coverage-application-state"><span title="${label}">${label}</span><strong>${Number(value || 0)}</strong></div>`).join('')}
                </div>
                <div class="coverage-application-footer">
                  <span>Report coverage ${Number(application.report_coverage_percent || 0)}%</span>
                  ${ready
                    ? '<span class="ready">Ready for Release Gate</span>'
                    : unmapped
                      ? '<span class="blocked">Project mapping required</span>'
                      : `<span class="blocked">${Number(application.remaining || 0)} Issue(s) require action</span>`}
                </div>
              </article>`;
          }).join('')}
        </div>` : '';
      $('coverageRows').innerHTML = rows.length ? rows.map((item) => `
        <article class="coverage-issue-card">
          <div class="coverage-card-head">
            <strong class="coverage-card-key">${escapeHtml(item.jira_key || '-')}</strong>
            <div class="coverage-card-state">
              ${item.workflow_status === 'missing' && currentPermissions.run_issue_review
                ? `<button class="secondary coverage-run-review" type="button" data-coverage-run-review="${escapeHtml(item.jira_key || '')}">Run Review</button>`
                : `<span class="workflow-badge ${escapeHtml(item.workflow_status || '')}">${escapeHtml(coverageStatusLabel(item.workflow_status))}</span>`}
              <span>${escapeHtml(item.jira_status || 'Status unavailable')}</span>
            </div>
          </div>
          <div class="coverage-card-summary" title="${escapeHtml(item.summary || '-')}">${escapeHtml(item.summary || '-')}</div>
          <div class="coverage-card-owner" title="${escapeHtml(item.responsible || '-')}">Responsible · ${escapeHtml(item.responsible || '-')}</div>
          <div class="coverage-card-applications" aria-label="Release applications">${(Array.isArray(item.applications) ? item.applications : ['Unmapped']).map((application) => `<span class="coverage-card-application">${escapeHtml(application || 'Unmapped')}</span>`).join('')}</div>
          <div class="coverage-card-cycle">${item.review_cycle_number
            ? `Cycle ${Number(item.review_cycle_number)} · Run ${Number(item.latest_run_number || 0) || '-'} · ${escapeHtml(item.review_cycle_sprint || 'Unknown Sprint')} · ${Number(item.review_snapshot_count || 0)} snapshot(s)`
            : 'No Review Cycle yet'}</div>
          <div class="coverage-card-metrics">
            <div class="coverage-card-metric"><span>MRs</span><strong>${item.mr_count || 0}</strong></div>
            <div class="coverage-card-metric"><span>Reports</span><strong>${item.report_count || 0}</strong></div>
            <div class="coverage-card-metric"><span>Handling</span><strong>${item.handled_count || 0}/${item.finding_count || 0}</strong><small>${item.blocking_pending || 0} blocker(s) pending</small></div>
          </div>
          ${item.latest_report ? `<div class="coverage-card-footer"><span class="meta">Latest generated report</span><div class="coverage-card-actions"><button class="secondary small-action coverage-preview" type="button" data-report="${escapeHtml(item.latest_report)}" data-output-dir="${escapeHtml(item.latest_output_dir || '')}">Preview</button></div></div>` : ''}
        </article>
      `).join('') : '<div class="coverage-empty">No issues found for this scope.</div>';
      for (const button of document.querySelectorAll('.coverage-preview')) {
        button.addEventListener('click', () => {
          closeCoverage();
          openReportPreview(button.dataset.report || '', '', '', { outputDir: button.dataset.outputDir || '' });
        });
      }
      for (const button of document.querySelectorAll('[data-coverage-run-review]')) {
        button.addEventListener('click', () => runCoverageIssueReview(button.dataset.coverageRunReview || '', button));
      }
      for (const button of document.querySelectorAll('[data-coverage-run-missing]')) {
        button.addEventListener('click', () => runCoverageMissingReviews(button.dataset.coverageRunMissing || '', button));
      }
      setCoverageView('overview');
    }

    async function runCoverageIssueReview(jiraKey, button) {
      const key = String(jiraKey || '').trim().toUpperCase();
      if (!key || !currentPermissions.run_issue_review || button?.disabled) return;
      if (button) button.textContent = 'Starting…';
      if ($('jira')) $('jira').value = key;
      if ($('sprint')) $('sprint').value = '';
      if ($('jiraFilter')) $('jiraFilter').value = '';
      closeCoverage();
      await singleFlight(`coverage-run-review-${key}`, button, () => runReview());
    }

    async function runCoverageMissingReviews(rawKeys, button) {
      const keys = Array.from(new Set(String(rawKeys || '').match(/\\b[A-Z][A-Z0-9]+-\\d+\\b/gi) || []))
        .map(key => key.toUpperCase());
      if (!keys.length || !currentPermissions.run_sprint_review || button?.disabled) return;
      if (!window.confirm(`Start Code Review for ${keys.length} Issue(s) without reports?`)) return;
      if (button) button.textContent = 'Starting…';
      if ($('jira')) $('jira').value = keys.join(', ');
      if ($('sprint')) $('sprint').value = '';
      if ($('jiraFilter')) $('jiraFilter').value = '';
      closeCoverage();
      await singleFlight(`coverage-run-missing-${keys.join('-')}`, button, () => runReview());
    }

    function coverageStatusLabel(value) {
      return ({ missing: 'No report', running: 'Generating', pending: 'Handling', ready: 'Ready for Pass', passed: 'Review Pass', failed: 'Failed' })[value] || value || '-';
    }

    function renderProjects(projects) {
      const container = $('projects');
      if (!container) return;
      if (!projects.length) {
        container.textContent = 'No project config found for current user.';
        return;
      }
      const grouped = new Map();
      for (const project of projects) {
        const group = project.group_display || project.group || 'Other';
        if (!grouped.has(group)) grouped.set(group, []);
        grouped.get(group).push(project);
      }
      container.innerHTML = Array.from(grouped.entries()).map(([group, items]) => `
        <div class="project-group" data-group="${escapeHtml(group)}">
          <div class="project-group-title">
            <span>${escapeHtml(group)}</span>
            <span class="count-pill">${items.length}</span>
          </div>
          ${items.map((project) => renderProject(project)).join('')}
        </div>
      `).join('');
      filterProjects();
    }

    function renderProject(project) {
      const devBranch = Array.isArray(project.dev_branch) ? project.dev_branch : [];
      const searchText = [
        project.display_name,
        project.group,
        project.module,
        project.repository_url,
        project.project_path,
        project.responsible,
        devBranch.join(' ')
      ].filter(Boolean).join(' ').toLowerCase();
      return `
        <div class="project" data-search="${escapeHtml(searchText)}">
          <strong>${escapeHtml(project.display_name || project.module || project.project_path || '-')}</strong>
          <div class="meta">${escapeHtml(project.group || '-')} / ${escapeHtml(project.module || '-')}</div>
          <div class="meta">${escapeHtml(project.repository_url || project.project_path || '-')}</div>
          <div class="meta">Responsible: ${escapeHtml(project.responsible || '-')}</div>
          ${devBranch.length ? `<div class="meta">Dev branches: ${escapeHtml(devBranch.join(', '))}</div>` : ''}
        </div>
      `;
    }

    function filterProjects() {
      const query = ($('projectSearch')?.value || '').trim().toLowerCase();
      for (const group of document.querySelectorAll('.project-group')) {
        let visibleCount = 0;
        for (const project of group.querySelectorAll('.project')) {
          const visible = !query || (project.dataset.search || '').includes(query);
          project.classList.toggle('hidden', !visible);
          if (visible) visibleCount += 1;
        }
        group.classList.toggle('hidden', visibleCount === 0);
        const pill = group.querySelector('.count-pill');
        if (pill) pill.textContent = String(visibleCount);
      }
    }

    async function loadReports() {
      const data = await fetchJson(`/api/reports${reportHistoryQuery()}`);
      renderReports(Array.isArray(data.reports) ? data.reports : []);
    }

    function renderReports(reports) {
      const container = $('reports');
      if (!container) return;
      currentReportItems = Array.isArray(reports) ? reports.map(normalizeReportItem).filter((item) => item.report) : [];
      updatePreviewNavigation();
      updateThreadNavigation();
      if (!reports.length) {
        container.textContent = 'No reports found in the selected range.';
        return;
      }
      container.innerHTML = reports.map((report) => `
        <div class="report-row">
          <div class="report-main">
            <a href="#" class="report-preview-open report-title" data-report="${escapeHtml(report.thread_report || report.relative_path || '')}" data-output-dir="${escapeHtml(report.output_dir || currentOutputDir || '')}" data-raw="${escapeHtml(report.url || '#')}" data-download="${escapeHtml(report.download_url || '#')}">${breakableText(report.relative_path || report.name || '-')}</a>
            <div class="report-meta-line"><span class="report-meta-chip">${escapeHtml(report.responsible || 'root')}</span>${report.output_dir_name ? `<span class="report-meta-chip">${escapeHtml(report.output_dir_name)}</span>` : ''}<span class="report-meta-chip">${formatBytes(report.size || 0)}</span><span class="report-meta-chip">${formatTime(report.modified)}</span></div>
          </div>
          <div class="report-actions">
            <button class="secondary small-action report-preview-btn" type="button" data-report="${escapeHtml(report.thread_report || report.relative_path || '')}" data-output-dir="${escapeHtml(report.output_dir || currentOutputDir || '')}" data-raw="${escapeHtml(report.url || '#')}" data-download="${escapeHtml(report.download_url || '#')}">Preview</button>
            <button class="secondary small-action thread-open" type="button" data-report="${escapeHtml(report.thread_report || report.relative_path || '')}" data-output-dir="${escapeHtml(report.output_dir || currentOutputDir || '')}">Discuss${report.thread_count ? ` (${report.thread_count})` : ''}</button>
            <button class="secondary small-action report-compare-btn" type="button" data-report="${escapeHtml(report.thread_report || report.relative_path || '')}" data-output-dir="${escapeHtml(report.output_dir || currentOutputDir || '')}">Compare</button>
            <button class="secondary small-action report-regenerate-btn" type="button" data-report="${escapeHtml(report.thread_report || report.relative_path || '')}" data-output-dir="${escapeHtml(report.output_dir || currentOutputDir || '')}">Regenerate</button>
            <a class="download-link" href="${escapeHtml(report.download_url || report.url || '#')}">Download</a>
          </div>
        </div>
      `).join('');
      bindReportThreadActions();
    }

    function breakableText(value) {
      return escapeHtml(value || '').replace(/([\\/_.+-])/g, '$1<wbr>');
    }

    function bindReportThreadActions() {
      for (const element of document.querySelectorAll('.report-preview-open, .report-preview-btn')) {
        element.addEventListener('click', (event) => {
          event.preventDefault();
          openReportPreview(element.dataset.report || '', element.dataset.raw || '', element.dataset.download || '', { openModal: !$('previewModal').hidden, outputDir: element.dataset.outputDir || '' });
        });
      }
      for (const button of document.querySelectorAll('.thread-open')) {
        button.addEventListener('click', () => openThread(button.dataset.report || '', button.dataset.outputDir || ''));
      }
      for (const button of document.querySelectorAll('.report-compare-btn')) {
        button.addEventListener('click', () => compareReport(button.dataset.report || '', button.dataset.outputDir || ''));
      }
      for (const button of document.querySelectorAll('.report-regenerate-btn')) {
        button.addEventListener('click', () => regenerateReport(button.dataset.report || '', button.dataset.outputDir || ''));
      }
    }

function jiraKeyFromReportPath(reportPath) {
      const match = String(reportPath || '').toUpperCase().match(/(^|[^A-Z0-9])([A-Z][A-Z0-9]+-\\d+)(?![A-Z0-9])/);
      if (match && match[2]) return match[2];
      return match ? match[0] : '';
    }

    async function regenerateReport(reportPath, outputDir = '') {
      const jiraKey = jiraKeyFromReportPath(reportPath);
      if (!jiraKey) {
        $('status').className = 'status error';
        $('status').textContent = 'Cannot regenerate: no Jira issue key was detected from the report name.';
        return;
      }
      const confirmed = window.confirm(`Regenerate Code Review Report for ${jiraKey}?`);
      if (!confirmed) return;
      const payload = {
        mode: 'jira',
        jira_key: jiraKey,
        output_dir: outputDir || currentOutputDir,
        report_min_severity: $('reportMinSeverity') ? $('reportMinSeverity').value : 'Medium',
        rerun_confirmed: true
      };
      $('status').className = 'status running';
      $('status').textContent = `Creating regenerate job for ${jiraKey}...`;
      try {
        const data = await fetchJson('/api/reviews', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const placeholder = {
          id: data.job_id,
          status: data.status || 'queued',
          payload,
          events: [{ event: 'queued', message: `Regenerate job queued for ${jiraKey}.`, data: { report: reportPath }, time: Date.now() / 1000 }],
          started_at: 0,
          finished_at: 0,
          result: null,
          error: ''
        };
        renderJobProgress(placeholder);
        pollReviewJob(data.job_id);
      } catch (error) {
        $('status').className = 'status error';
        $('status').textContent = `Regenerate failed: ${error.message}`;
      }
    }

    async function compareReport(reportPath, outputDir = '') {
      if (!reportPath) return;
      const preview = $('preview');
      preview.className = 'markdown-preview empty';
      preview.textContent = 'Loading report comparison...';
      $('previewName').textContent = `${reportPath} · Compare`;
      try {
        const query = [
          `report=${encodeURIComponent(reportPath)}`,
          outputDir ? `output_dir=${encodeURIComponent(outputDir)}` : ''
        ].filter(Boolean).join('&');
        const data = await fetchJson(`/api/report-compare?${query}`);
        currentPreviewReportPath = reportPath;
        currentPreviewOutputDir = outputDir || currentOutputDir || '';
        setPreviewMarkdown(data.markdown || 'No comparison content.', `${reportPath} · Compare`, '', '');
      } catch (error) {
        preview.className = 'markdown-preview empty';
        preview.textContent = `Failed to compare reports: ${error.message}`;
      }
    }

    function normalizeReportItem(report) {
      return {
        report: report.thread_report || report.relative_path || '',
        raw: report.url || '#',
        download: report.download_url || '#',
        outputDir: report.output_dir || currentOutputDir || '',
        name: report.relative_path || report.name || report.thread_report || ''
      };
    }

    async function openReportPreview(reportPath, rawUrl = '', downloadUrl = '', options = {}) {
      if (!reportPath) return;
      const preview = $('preview');
      preview.className = 'markdown-preview empty';
      preview.textContent = 'Loading report preview...';
      $('previewName').textContent = reportPath;
      currentPreviewReportPath = reportPath;
      currentPreviewOutputDir = options.outputDir || currentOutputDir || '';
      updatePreviewNavigation();
      try {
        const reportOutputDir = currentPreviewOutputDir;
        const query = [
          `report=${encodeURIComponent(reportPath)}`,
          reportOutputDir ? `output_dir=${encodeURIComponent(reportOutputDir)}` : ''
        ].filter(Boolean).join('&');
        const data = await fetchJson(`/api/report-markdown?${query}`);
        setPreviewMarkdown(data.markdown || '', data.report || reportPath, rawUrl, downloadUrl);
        if (options.openModal) openPreviewModal();
      } catch (error) {
        preview.className = 'markdown-preview empty';
        const jiraKey = jiraKeyFromReportPath(reportPath);
        preview.innerHTML = `
          <div>Failed to load report preview: ${escapeHtml(error.message)}</div>
          ${jiraKey ? `<div style="margin-top: 12px;"><button class="secondary small-action" id="previewRegenerateBtn" type="button">Regenerate ${escapeHtml(jiraKey)}</button></div>` : ''}
        `;
        if (jiraKey && $('previewRegenerateBtn')) {
          $('previewRegenerateBtn').addEventListener('click', () => regenerateReport(reportPath, options.outputDir || currentOutputDir || ''));
        }
      }
    }

    function setPreviewMarkdown(markdown, name = '', rawUrl = '', downloadUrl = '') {
      currentPreviewMarkdown = markdown || 'No report content.';
      currentPreviewName = name || 'Generated review report';
      currentPreviewRawUrl = rawUrl || '';
      currentPreviewDownloadUrl = downloadUrl || '';
      const preview = $('preview');
      preview.className = 'markdown-preview';
      preview.innerHTML = renderReportMarkdownPreview(currentPreviewMarkdown);
      bindReportTabs(preview);
      $('previewName').textContent = currentPreviewName;
      const downloadLink = $('previewDownloadLink');
      const previewOpenButton = $('previewOpenBtn');
      const compareButton = $('previewCompareBtn');
      if (previewOpenButton) previewOpenButton.disabled = !currentPreviewMarkdown;
      downloadLink.hidden = !currentPreviewDownloadUrl;
      downloadLink.href = currentPreviewDownloadUrl || '#';
      if (compareButton) compareButton.disabled = !currentPreviewReportPath;
      preview.scrollTop = 0;
      updatePreviewNavigation();
      syncPreviewModalContent();
    }

    function openPreviewModal() {
      const modal = $('previewModal');
      if (!currentPreviewMarkdown) {
        currentPreviewMarkdown = 'No report generated yet.';
        currentPreviewName = 'No report selected.';
      }
      syncPreviewModalContent();
      modal.hidden = false;
      document.addEventListener('keydown', closePreviewModalOnEscape);
    }

    function closePreviewModal() {
      $('previewModal').hidden = true;
      document.removeEventListener('keydown', closePreviewModalOnEscape);
    }

    function closePreviewModalOnEscape(event) {
      if (event.key === 'Escape') closePreviewModal();
      if (event.key === 'ArrowLeft' && !$('previewModal').hidden) navigatePreviewReport(-1);
      if (event.key === 'ArrowRight' && !$('previewModal').hidden) navigatePreviewReport(1);
    }

    function currentPreviewIndex() {
      return currentReportItems.findIndex((item) => item.report === currentPreviewReportPath && (!currentPreviewOutputDir || item.outputDir === currentPreviewOutputDir));
    }

    function updatePreviewNavigation() {
      const prevBtn = $('previewPrevBtn');
      const nextBtn = $('previewNextBtn');
      if (!prevBtn || !nextBtn) return;
      const index = currentPreviewIndex();
      const total = currentReportItems.length;
      prevBtn.disabled = index <= 0;
      nextBtn.disabled = index < 0 || index >= total - 1;
      const suffix = index >= 0 && total ? ` · ${index + 1}/${total}` : '';
      if ($('previewModalName')) $('previewModalName').textContent = `${currentPreviewName || 'No report selected.'}${suffix}`;
    }

    function navigatePreviewReport(delta) {
      const index = currentPreviewIndex();
      if (index < 0) return;
      const next = currentReportItems[index + delta];
      if (!next) return;
      openReportPreview(next.report, next.raw, next.download, { openModal: true, outputDir: next.outputDir || '' });
    }

    function syncPreviewModalContent() {
      const modalBody = $('previewModalBody');
      if (!modalBody) return;
      if (!currentPreviewMarkdown) {
        modalBody.className = 'markdown-preview preview-modal-body empty';
        modalBody.textContent = 'No report generated yet.';
      } else {
        modalBody.className = 'markdown-preview preview-modal-body';
        modalBody.innerHTML = renderReportMarkdownPreview(currentPreviewMarkdown, { anchorPrefix: 'modal-' });
        bindReportTabs(modalBody);
      }
      updatePreviewNavigation();
      const downloadLink = $('previewModalDownloadLink');
      downloadLink.hidden = !currentPreviewDownloadUrl;
      downloadLink.href = currentPreviewDownloadUrl || '#';
      modalBody.scrollTop = 0;
    }

    function renderMarkdown(markdown, options = {}) {
      const lines = String(markdown || '').replace(/\\r\\n/g, '\\n').replace(/\\r/g, '\\n').split('\\n');
      const htmlParts = [];
      let inCode = false;
      let codeLines = [];
      let listType = '';

      const closeList = () => {
        if (listType) {
          htmlParts.push(`</${listType}>`);
          listType = '';
        }
      };
      const openList = (type) => {
        if (listType !== type) {
          closeList();
          htmlParts.push(`<${type}>`);
          listType = type;
        }
      };

      for (let index = 0; index < lines.length; index += 1) {
        const line = lines[index];
        if (/^\\s*```/.test(line)) {
          if (inCode) {
            htmlParts.push(`<pre><code>${escapeHtml(codeLines.join('\\n'))}</code></pre>`);
            codeLines = [];
            inCode = false;
          } else {
            closeList();
            inCode = true;
          }
          continue;
        }
        if (inCode) {
          codeLines.push(line);
          continue;
        }
        if (isInternalMarkdownComment(line)) {
          continue;
        }
        if (!line.trim()) {
          closeList();
          continue;
        }
        const safeHtml = renderSafeMarkdownHtmlLine(line, options);
        if (safeHtml) {
          closeList();
          htmlParts.push(safeHtml);
          continue;
        }
        const nextLine = lines[index + 1] || '';
        if (isMarkdownTableHeader(line, nextLine)) {
          closeList();
          const headers = splitMarkdownTableRow(line);
          index += 1;
          const rows = [];
          while (index + 1 < lines.length && looksLikeMarkdownTableRow(lines[index + 1])) {
            index += 1;
            rows.push(splitMarkdownTableRow(lines[index]));
          }
          htmlParts.push(renderMarkdownTable(headers, rows, options));
          continue;
        }
        const heading = line.match(/^(#{1,3})\\s+(.+)$/);
        if (heading) {
          closeList();
          const level = heading[1].length;
          htmlParts.push(`<h${level}>${renderInlineMarkdown(heading[2], options)}</h${level}>`);
          continue;
        }
        const unordered = line.match(/^\\s*[-*]\\s+(.+)$/);
        if (unordered) {
          openList('ul');
          htmlParts.push(`<li>${renderInlineMarkdown(unordered[1], options)}</li>`);
          continue;
        }
        const ordered = line.match(/^\\s*\\d+\\.\\s+(.+)$/);
        if (ordered) {
          openList('ol');
          htmlParts.push(`<li>${renderInlineMarkdown(ordered[1], options)}</li>`);
          continue;
        }
        const quote = line.match(/^\\s*>\\s?(.*)$/);
        if (quote) {
          closeList();
          htmlParts.push(`<blockquote>${renderInlineMarkdown(quote[1], options)}</blockquote>`);
          continue;
        }
        closeList();
        htmlParts.push(`<p>${renderInlineMarkdown(line, options)}</p>`);
      }
      if (inCode) {
        htmlParts.push(`<pre><code>${escapeHtml(codeLines.join('\\n'))}</code></pre>`);
      }
      closeList();
      return htmlParts.join('\\n');
    }

    function isInternalMarkdownComment(line) {
      return /^\\s*<!--\\s*code_reviewer_metadata:/i.test(String(line || ''));
    }

    function renderReportMarkdownPreview(markdown, options = {}) {
      const parsed = splitMarkdownIntoReportSections(markdown);
      const sections = parsed.sections || [];
      if (sections.length <= 1) return renderMarkdown(markdown, options);
      const prefix = options.anchorPrefix || '';
      const tabs = sections.map((section, index) => {
        const id = sanitizeHtmlId(`${prefix}report-tab-${index}-${section.title}`);
        return { ...section, id };
      });
      const tabButtons = tabs.map((section, index) => `
        <button class="report-tab ${index === 0 ? 'active' : ''}" type="button" data-report-tab="${section.id}" aria-selected="${index === 0 ? 'true' : 'false'}">${escapeHtml(section.title)}</button>
      `).join('');
      const panels = tabs.map((section, index) => `
        <section class="report-tab-panel" data-report-tab-panel="${section.id}" ${index === 0 ? '' : 'hidden'}>
          ${renderMarkdown(section.markdown, options)}
        </section>
      `).join('');
      const title = parsed.title ? `<div class="report-tab-title">${escapeHtml(parsed.title)}</div>` : '';
      return `<div class="report-tabbed-preview">${title}<div class="report-tabbar" role="tablist">${tabButtons}</div>${panels}</div>`;
    }

    function splitMarkdownIntoReportSections(markdown) {
      const lines = String(markdown || '').replace(/\\r\\n/g, '\\n').replace(/\\r/g, '\\n').split('\\n');
      const sections = [];
      const preface = [];
      let reportTitle = '';
      let current = null;
      let inCode = false;
      for (const line of lines) {
        if (/^\\s*```/.test(line)) {
          inCode = !inCode;
          (current ? current.lines : preface).push(line);
          continue;
        }
        if (!inCode && isInternalMarkdownComment(line)) {
          continue;
        }
        const topHeading = !inCode ? line.match(/^#\\s+(.+)$/) : null;
        if (topHeading && !reportTitle && !current && !preface.some((item) => item.trim())) {
          reportTitle = plainMarkdownLabel(topHeading[1], 120);
          continue;
        }
        const heading = !inCode ? line.match(/^##\\s+(.+)$/) : null;
        if (heading) {
          if (current && current.lines.some((item) => item.trim())) sections.push(current);
          current = { title: canonicalReportSectionTitle(plainMarkdownLabel(heading[1])), lines: [] };
          if (sections.length === 0 && preface.some((item) => item.trim())) {
            current.lines.push(...preface);
          }
          continue;
        }
        (current ? current.lines : preface).push(line);
      }
      if (current && current.lines.some((item) => item.trim())) sections.push(current);
      if (!sections.length && preface.some((item) => item.trim())) {
        sections.push({ title: '概览', lines: preface });
      }
      return {
        title: reportTitle,
        sections: orderReportSections(mergeReportSections(sections)).map((section) => ({ title: section.title || 'Section', markdown: section.lines.join('\\n').trim() })),
      };
    }

    function plainMarkdownLabel(value, limit = 32) {
      return String(value || '')
        .replace(/`([^`]+)`/g, '$1')
        .replace(/\\[([^\\]]+)\\]\\([^)]+\\)/g, '$1')
        .replace(/[*_#]/g, '')
        .trim()
        .slice(0, limit) || 'Section';
    }

    function canonicalReportSectionTitle(value) {
      const title = String(value || '').trim();
      const compact = title.replace(/\\s+/g, '').toLowerCase();
      if (compact.includes('问题处理结果说明模板') || compact.includes('处理结果说明模板') || compact.includes('结果说明模板') || compact.includes('findinghandlingresulttemplate') || compact.includes('handlingtemplate')) {
        return /[A-Za-z]/.test(title) && !/[\\u4e00-\\u9fff]/.test(title) ? 'Handling Template' : '处理模版';
      }
      if (compact.includes('审核结论') || compact.includes('总体结论') || compact.includes('风险统计') || compact.includes('风险摘要') || compact.includes('reviewconclusion') || compact === 'conclusion' || compact.includes('riskstats') || compact.includes('risksummary')) {
        return /[A-Za-z]/.test(title) && !/[\\u4e00-\\u9fff]/.test(title) ? 'Review Conclusion' : '审核结论';
      }
      if (compact.includes('关联mr') || compact.includes('relatedmrs')) {
        return /[A-Za-z]/.test(title) && !/[\\u4e00-\\u9fff]/.test(title) ? 'Related MRs' : '关联MR';
      }
      if (compact.includes('文件diff') || compact.includes('filediff') || compact.includes('filediffs')) {
        return /[A-Za-z]/.test(title) && !/[\\u4e00-\\u9fff]/.test(title) ? 'File Diff' : '文件 Diff';
      }
      if (compact.includes('llm执行记录') || compact.includes('jira/gitlab关联') || compact.includes('jiragitlab关联') || compact.includes('llmexecutionnotes') || compact.includes('jiragitlablinks')) {
        return /[A-Za-z]/.test(title) && !/[\\u4e00-\\u9fff]/.test(title) ? 'Other' : '其他';
      }
      if (compact.includes('git_version') || compact.includes('gitversion') || compact.includes('发布闸门') || compact.includes('releasegate')) {
        return /[A-Za-z]/.test(title) && !/[\\u4e00-\\u9fff]/.test(title) ? 'Other' : '其他';
      }
      return title;
    }

    function reportSectionOrder(title) {
      const compact = String(title || '').replace(/\\s+/g, '').toLowerCase();
      const order = [
        ['基本信息', 'basicinfo'],
        ['审核结论', 'reviewconclusion', 'conclusion'],
        ['变更摘要', 'changesummary'],
        ['关联mr', 'relatedmrs'],
        ['文件diff', 'filediffs'],
        ['问题列表', 'findings'],
        ['处理模版', 'handlingtemplate'],
        ['测试建议', 'testsuggestions'],
        ['其他', 'other']
      ];
      for (let index = 0; index < order.length; index += 1) {
        if (order[index].some((token) => compact.includes(token))) return index;
      }
      return 100;
    }

    function orderReportSections(sections) {
      return sections
        .map((section, index) => ({ ...section, originalIndex: index }))
        .sort((left, right) => {
          const delta = reportSectionOrder(left.title) - reportSectionOrder(right.title);
          return delta || left.originalIndex - right.originalIndex;
        })
        .map(({ originalIndex, ...section }) => section);
    }

    function mergeReportSections(sections) {
      const merged = [];
      const indexByTitle = new Map();
      for (const section of sections) {
        const title = section.title || 'Section';
        if (indexByTitle.has(title)) {
          const existing = merged[indexByTitle.get(title)];
          existing.lines.push('', ...section.lines);
        } else {
          indexByTitle.set(title, merged.length);
          merged.push({ title, lines: [...section.lines] });
        }
      }
      return merged;
    }

    function bindReportTabs(container) {
      if (!container) return;
      enhanceReportFindingCollapse(container);
      decorateDeferredOriginLabels(container);
      for (const button of container.querySelectorAll('.report-tab')) {
        button.addEventListener('click', () => activateReportTab(container, button.dataset.reportTab || ''));
      }
      container.addEventListener('click', (event) => {
        const link = event.target && event.target.closest ? event.target.closest('a[href^="#"]') : null;
        if (!link) return;
        const rawId = String(link.getAttribute('href') || '').slice(1);
        let id = rawId;
        try {
          id = decodeURIComponent(rawId);
        } catch (_error) {
          id = rawId;
        }
        const target = findElementByIdIn(container, id);
        if (!target) return;
        const panel = target.closest('.report-tab-panel');
        if (panel && panel.hidden) {
          event.preventDefault();
          activateReportTab(container, panel.dataset.reportTabPanel || '');
          requestAnimationFrame(() => target.scrollIntoView({ block: 'start' }));
        }
      });
    }

    function reportFindingPreviewText(body, title = '') {
      const blocks = Array.from(body.querySelectorAll('p, li, td'))
        .map(node => String(node.textContent || '').replace(/\\s+/g, ' ').trim())
        .filter(Boolean);
      const full = blocks.join(' · ').replace(/\\s+/g, ' ').trim();
      const problemMatch = full.match(/(?:问题(?:描述|详情)?|说明|Problem(?: Description)?|Detail)\\s*[:：]\\s*(.+?)(?=(?:建议|解决建议|Recommendation|Suggestion)\\s*[:：]|$)/i);
      const suggestionMatch = full.match(/(?:建议|解决建议|Recommendation|Suggestion)\\s*[:：]\\s*(.+)$/i);
      const contentBlocks = blocks.filter(value => !/^(?:类型|位置|Type|Location)\\s*[:：]/i.test(value) && !/^(?:Expected File Lists|Commit File Lists|Remarks)/i.test(value));
      const mismatchDetail = /involved file list|涉及文件清单/i.test(title)
        ? contentBlocks.find(value => /(?:未列出|未提交|不匹配|not listed|not changed|mismatch)/i.test(value))
        : '';
      const problem = String(problemMatch?.[1] || mismatchDetail || contentBlocks[0] || full || 'Open to inspect the complete finding evidence.').slice(0, 360);
      const suggestion = String(suggestionMatch?.[1] || blocks.find(value => /(?:建议|recommend|should|需要|应当)/i.test(value)) || '').replace(/^(?:建议|解决建议|Recommendation|Suggestion)\\s*[:：]\\s*/i, '').slice(0, 360);
      return { problem, suggestion };
    }

    function decorateDeferredOriginLabels(container) {
      for (const cell of container.querySelectorAll('td')) {
        if (cell.querySelector('.report-origin-badge')) continue;
        const text = String(cell.textContent || '');
        let label = '';
        if (/\\bCompany Config\\b/i.test(text) && !/Company Config\\s*\\/\\s*SCR/i.test(text)) label = 'Company Config';
        else if (/\\bSCR\\b/i.test(text) && !/Company Config\\s*\\/\\s*SCR/i.test(text)) label = 'SCR';
        if (!label) continue;
        const badge = document.createElement('span');
        badge.className = 'report-origin-badge';
        badge.textContent = label;
        badge.title = `Deferred MR source: ${label}`;
        cell.prepend(badge);
      }
    }

    function enhanceReportFindingCollapse(container) {
      if (!container) return;
      const problemPanels = Array.from(container.querySelectorAll('.report-tab-panel')).filter((panel) => {
        const button = container.querySelector(`[data-report-tab="${panel.dataset.reportTabPanel || ''}"]`);
        const label = String(button ? button.textContent : '').replace(/\\s+/g, '').toLowerCase();
        return label.includes('问题列表') || label.includes('problemlist') || label === 'problems';
      });
      for (const panel of problemPanels) {
        const headings = Array.from(panel.children).filter((element) => {
          if (!/^H[23]$/.test(element.tagName)) return false;
          return /^\\s*\\d+\\s*[.、]\\s*\\[(critical|high|medium|low|warning)\\]/i.test(element.textContent || '');
        });
        for (let index = 0; index < headings.length; index += 1) {
          const heading = headings[index];
          if (!heading.parentNode || heading.closest('.report-finding-details')) continue;
          const nextHeading = headings[index + 1] || null;
          const details = document.createElement('details');
          details.className = 'report-finding-details';
          details.open = false;
          const summary = document.createElement('summary');
          summary.className = 'report-finding-summary';
          if (heading.id) summary.id = heading.id;
          const body = document.createElement('div');
          body.className = 'report-finding-body';
          heading.parentNode.insertBefore(details, heading);
          heading.remove();
          details.appendChild(summary);
          details.appendChild(body);
          while (details.nextSibling && details.nextSibling !== nextHeading) {
            body.appendChild(details.nextSibling);
          }
          const preview = reportFindingPreviewText(body, heading.textContent || '');
          summary.innerHTML = `<span class="report-finding-summary-main"><span class="report-finding-title">${heading.innerHTML}</span><span class="report-finding-preview" aria-hidden="true"><span class="report-finding-preview-line"><strong>Problem ·</strong> ${escapeHtml(preview.problem)}</span>${preview.suggestion ? `<span class="report-finding-preview-line"><strong>Suggestion ·</strong> ${escapeHtml(preview.suggestion)}</span>` : ''}</span></span><span class="report-finding-more" aria-hidden="true"></span>`;
        }
      }
    }

    function activateReportTab(container, tabId) {
      if (!container || !tabId) return;
      for (const button of container.querySelectorAll('.report-tab')) {
        const selected = button.dataset.reportTab === tabId;
        button.classList.toggle('active', selected);
        button.setAttribute('aria-selected', selected ? 'true' : 'false');
      }
      for (const panel of container.querySelectorAll('.report-tab-panel')) {
        panel.hidden = panel.dataset.reportTabPanel !== tabId;
      }
    }

    function findElementByIdIn(container, id) {
      for (const element of container.querySelectorAll('[id]')) {
        if (element.id === id) return element;
      }
      return null;
    }

    function renderSafeMarkdownHtmlLine(line, options = {}) {
      const value = String(line || '').trim();
      const anchor = value.match(/^<a\\s+id=["']([^"']+)["']\\s*><\\/a>$/i);
      if (anchor) {
        return `<a id="${sanitizeHtmlId(`${options.anchorPrefix || ''}${anchor[1]}`)}" class="anchor-target"></a>`;
      }
      const detailsSummary = value.match(/^<details(\\s+open)?\\s*>\\s*<summary>\\s*(.*?)\\s*<\\/summary>\\s*$/i);
      if (detailsSummary) {
        return `<details${detailsSummary[1] ? ' open' : ''}><summary>${renderInlineMarkdown(detailsSummary[2], options)}</summary>`;
      }
      const detailsOpen = value.match(/^<details(\\s+open)?\\s*>$/i);
      if (detailsOpen) {
        return `<details${detailsOpen[1] ? ' open' : ''}>`;
      }
      const summary = value.match(/^<summary>\\s*(.*?)\\s*<\\/summary>$/i);
      if (summary) {
        return `<summary>${renderInlineMarkdown(summary[1], options)}</summary>`;
      }
      if (/^<\\/details>$/i.test(value)) {
        return '</details>';
      }
      if (/^<br\\s*\\/?>$/i.test(value)) {
        return '<br>';
      }
      return '';
    }

    function sanitizeHtmlId(value) {
      return escapeHtml(String(value || '').replace(/[^A-Za-z0-9_.:-]/g, '-').slice(0, 180));
    }

    function isMarkdownTableHeader(line, nextLine) {
      return looksLikeMarkdownTableRow(line) && /^\\s*\\|?\\s*:?-{3,}:?\\s*(\\|\\s*:?-{3,}:?\\s*)+\\|?\\s*$/.test(nextLine || '');
    }

    function looksLikeMarkdownTableRow(line) {
      return /\\|/.test(line || '') && !/^\\s*```/.test(line || '');
    }

    function splitMarkdownTableRow(line) {
      return String(line || '').trim().replace(/^\\|/, '').replace(/\\|$/, '').split('|').map((cell) => cell.trim());
    }

    function renderMarkdownTable(headers, rows, options = {}) {
      const headerHtml = headers.map((cell) => `<th>${renderInlineMarkdown(cell, options)}</th>`).join('');
      const rowsHtml = rows.map((row) => `<tr>${row.map((cell) => `<td>${renderInlineMarkdown(cell, options)}</td>`).join('')}</tr>`).join('');
      return `<table><thead><tr>${headerHtml}</tr></thead><tbody>${rowsHtml}</tbody></table>`;
    }

    function renderInlineMarkdown(value, options = {}) {
      const codeTokens = [];
      let text = String(value || '').replace(/`([^`]+)`/g, (_match, code) => {
        const token = `\\u0000CODE${codeTokens.length}\\u0000`;
        codeTokens.push(`<code>${escapeHtml(code)}</code>`);
        return token;
      });
      text = escapeHtml(text);
      text = text.replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>');
      text = text.replace(/\\[([^\\]]+)\\]\\(([^)]+)\\)/g, (_match, label, url) => {
        const href = safeMarkdownUrl(url, options);
        return `<a href="${href}" target="_blank" rel="noopener">${label}</a>`;
      });
      codeTokens.forEach((html, index) => {
        text = text.replace(`\\u0000CODE${index}\\u0000`, html);
      });
      return text;
    }

    function safeMarkdownUrl(value, options = {}) {
      const url = String(value || '').trim();
      if (url.startsWith('#')) return `#${sanitizeHtmlId(`${options.anchorPrefix || ''}${url.slice(1)}`)}`;
      if (/^(https?:\\/\\/|#|\\/|mailto:)/i.test(url)) return escapeHtml(url);
      return '#';
    }

    function setHistoryTab(tab) {
      const active = tab === 'reports' ? 'reports' : 'responsibles';
      for (const button of document.querySelectorAll('.history-tab')) {
        const selected = button.dataset.historyTab === active;
        button.classList.toggle('active', selected);
        button.setAttribute('aria-selected', selected ? 'true' : 'false');
      }
      if ($('responsiblesPane')) $('responsiblesPane').hidden = active !== 'responsibles';
      if ($('reportsPane')) $('reportsPane').hidden = active !== 'reports';
    }

    function setThreadTab(tab) {
      const active = ['discussion', 'chat'].includes(tab) ? tab : 'discussion';
      for (const button of document.querySelectorAll('.thread-tab')) {
        const selected = button.dataset.threadTab === active;
        button.classList.toggle('active', selected);
        button.setAttribute('aria-selected', selected ? 'true' : 'false');
      }
      for (const pane of ['discussion', 'chat']) {
        const element = $(`${pane}Pane`);
        if (element) element.hidden = pane !== active;
      }
    }

    function selectedReportDownloadLink() {
      if (!selectedThreadReport) return '';
      const path = selectedThreadReport.split('/').map((part) => encodeURIComponent(part)).join('/');
      const url = new URL(`/download/report/${path}`, window.location.origin);
      if (selectedThreadOutputDir) url.searchParams.set('output_dir', selectedThreadOutputDir);
      return url.toString();
    }

    function selectedReportRawLink() {
      if (!selectedThreadReport) return '';
      const path = selectedThreadReport.split('/').map((part) => encodeURIComponent(part)).join('/');
      const url = new URL(`/reports/${path}`, window.location.origin);
      if (selectedThreadOutputDir) url.searchParams.set('output_dir', selectedThreadOutputDir);
      return url.toString();
    }

    function currentThreadIndex() {
      return currentReportItems.findIndex((item) => item.report === selectedThreadReport && (!selectedThreadOutputDir || item.outputDir === selectedThreadOutputDir));
    }

    function updateThreadNavigation() {
      const prevBtn = $('threadPrevBtn');
      const nextBtn = $('threadNextBtn');
      const rawLink = $('threadRawLink');
      const downloadLink = $('threadDownloadLink');
      const index = currentThreadIndex();
      const total = currentReportItems.length;
      if (prevBtn) prevBtn.disabled = index <= 0;
      if (nextBtn) nextBtn.disabled = index < 0 || index >= total - 1;
      const currentItem = index >= 0 ? currentReportItems[index] : null;
      const raw = currentItem && currentItem.raw ? currentItem.raw : selectedReportRawLink();
      const download = currentItem && currentItem.download ? currentItem.download : selectedReportDownloadLink();
      if (rawLink) {
        rawLink.hidden = !raw;
        rawLink.href = raw || '#';
      }
      if (downloadLink) {
        downloadLink.hidden = !download;
        downloadLink.href = download || '#';
      }
      const suffix = index >= 0 && total ? ` · ${index + 1}/${total}` : '';
      if ($('threadReportName')) $('threadReportName').textContent = `${selectedThreadReport || ''}${suffix}`;
    }

    function navigateThreadReport(delta) {
      const index = currentThreadIndex();
      if (index < 0) return;
      const next = currentReportItems[index + delta];
      if (!next) return;
      openThread(next.report, next.outputDir || '');
    }

    function reportResponsibleFromPath(reportPath) {
      const first = String(reportPath || '').split(/[\\\\/]/)[0] || '';
      return first && first.toLowerCase().endsWith('.md') ? '' : first;
    }

    function appendAiChatMessage(role, text) {
      const box = $('aiChatMessages');
      if (!box) return;
      if (box.dataset.empty === 'true') { box.dataset.empty = 'false'; box.textContent = ''; }
      const message = document.createElement('div');
      const assistant = role === 'assistant';
      message.className = `chat-message ${role === 'assistant' ? 'assistant' : 'user'}`;
      const body = role === 'assistant' ? renderMarkdown(text || '') : `<p>${escapeHtml(text || '').replace(/\\n/g, '<br>')}</p>`;
      message.innerHTML = `<div class="chat-avatar" aria-hidden="true">${assistant ? 'AI' : 'YOU'}</div><div class="chat-message-content"><div class="chat-message-head"><strong class="chat-message-role">${assistant ? 'AI Assistant' : 'You'}</strong><span>${new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})}</span></div><div class="chat-message-body">${body}</div></div>`;
      box.appendChild(message);
      box.scrollTop = box.scrollHeight;
    }

    function updateReviewPassStatus(data = {}) {
      const messages = Array.isArray(data.messages) ? data.messages : [];
      const passMessages = messages.filter((message) => String(message.kind || '').includes('pass'));
      const followups = data.followup_draft ? 'Follow-up draft exists.' : 'No follow-up draft generated yet.';
      const readiness = data.pass_readiness || {};
      const handling = data.handling_summary || {};
      $('reviewPassStatus').textContent = [
        `Report: ${selectedThreadReport || '-'}`,
        'Blocking policy: High and Critical items must be fixed, clarified, or re-scanned clean before manual pass.',
        `Handling: ${handling.completed || 0}/${handling.total || 0} completed · ${handling.blocking_pending || 0} blocking pending`,
        `Pass readiness: ${readiness.ready ? 'Ready for Auditor/Manager decision' : (readiness.message || 'Not ready')}`,
        `Communication records: ${messages.length}`,
        `Manual pass records: ${passMessages.length}`,
        followups
      ].join('\\n');
    }

    async function openThread(reportPath, outputDir = '') {
      if (!reportPath) return;
      selectedThreadReport = reportPath;
      selectedThreadOutputDir = outputDir || currentOutputDir || '';
      $('threadModal').hidden = false;
      document.addEventListener('keydown', closeThreadOnEscape);
      $('threadReportName').textContent = reportPath;
      updateThreadNavigation();
      $('threadMessages').textContent = 'Loading communication history...';
      $('followupDraft').textContent = 'Loading follow-up draft...';
      $('copyFollowupDraftBtn').disabled = true;
      if ($('aiChatMessages')) {
        $('aiChatMessages').dataset.empty = 'true';
        $('aiChatMessages').innerHTML = '<div class="ai-assist-note">Ask for evidence explanations, summaries, or suggested follow-ups. AI cannot record handling or Pass decisions.</div>';
      }
      setThreadTab('discussion');
      $('threadMessage').focus();
      try {
        const data = await fetchJson(`/api/report-thread?report=${encodeURIComponent(reportPath)}${selectedThreadOutputDir ? `&output_dir=${encodeURIComponent(selectedThreadOutputDir)}` : ''}`);
        renderThread(data);
        updateThreadNavigation();
      } catch (error) {
        $('threadMessages').textContent = error.message;
        $('followupDraft').textContent = 'No follow-up draft yet.';
        $('copyFollowupDraftBtn').disabled = true;
        updateThreadNavigation();
      }
    }

    function closeThreadModal() {
      selectedThreadReport = '';
      selectedThreadOutputDir = '';
      $('threadModal').hidden = true;
      document.removeEventListener('keydown', closeThreadOnEscape);
    }

    function closeThreadOnEscape(event) {
      if (event.key === 'Escape') closeThreadModal();
    }

    function openIssueReviewFromReport() {
      const match = String(selectedThreadReport || '').toUpperCase().match(/\\b[A-Z][A-Z0-9]+-\\d+\\b/);
      if (!match) {
        $('threadMessages').textContent = 'This report does not contain a Jira key that can be opened in Issue Review.';
        return;
      }
      closeThreadModal();
      selectedIssueReview = match[0];
      openIssueReviews().then(() => loadIssueReviewDetail(match[0])).catch(error => {
        $('issueReviewDetail').innerHTML = `<div class="status error">${escapeHtml(error.message)}</div>`;
      });
    }

    function renderThread(data) {
      const messages = Array.isArray(data.messages) ? data.messages : [];
      const summary = data.handling_summary || {};
      const readiness = data.pass_readiness || {};
      const issueTitle = String(data.issue_title || '').trim();
      $('threadContext').hidden = !issueTitle;
      $('threadIssueTitle').textContent = issueTitle;
      $('threadContextMetrics').innerHTML = issueTitle ? [
        `<span class="thread-context-metric">Findings <strong>${Number(summary.total || 0)}</strong></span>`,
        `<span class="thread-context-metric">Handled <strong>${Number(summary.completed || 0)}/${Number(summary.total || 0)}</strong></span>`,
        `<span class="thread-context-metric">Critical/High remaining <strong>${Number(summary.blocking_pending || 0)}</strong></span>`,
        `<span class="thread-context-metric">Pass readiness <strong>${readiness.ready ? 'Ready' : 'Blocked'}</strong></span>`
      ].join('') : '';
      $('threadMessages').innerHTML = messages.length ? messages.map((message) => `
        <div class="thread-message ${message.kind === 'followup-draft' ? 'system' : ''}">
          <strong>${escapeHtml(message.user || '-')}</strong>
          <div class="meta">${escapeHtml(message.kind || 'comment')} · ${escapeHtml(message.time || '')}</div>
          <div>${escapeHtml(message.message || '').replace(/\\n/g, '<br>')}</div>
        </div>
      `).join('') : 'No communication yet.';
      $('followupInstruction').value = data.followup_instruction || '';
      $('followupDraft').textContent = data.followup_draft || 'No follow-up draft yet.';
      $('copyFollowupDraftBtn').disabled = !String(data.followup_draft || '').trim();
      requestAnimationFrame(() => {
        const box = $('threadMessages');
        if (box) box.scrollTop = box.scrollHeight;
      });
    }

    async function sendThreadMessage() {
      const field = $('threadMessage');
      const message = field.value.trim();
      field.removeAttribute('aria-invalid');
      $('threadMessageError').className = 'field-message';
      $('threadMessageError').textContent = '';
      if (!selectedThreadReport || !message) {
        field.setAttribute('aria-invalid', 'true');
        $('threadMessageError').className = 'field-message error';
        $('threadMessageError').textContent = 'Reply message is required.';
        field.focus();
        return;
      }
      const data = await fetchJson('/api/report-thread/message', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          report: selectedThreadReport,
          output_dir: selectedThreadOutputDir,
          kind: 'comment',
          message
        })
      });
      $('threadMessage').value = '';
      renderThread(data);
      await loadReports();
    }

    async function generateFollowups() {
      if (!selectedThreadReport) return;
      $('followupDraft').textContent = 'Generating follow-up draft...';
      const data = await fetchJson('/api/report-thread/followups', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          report: selectedThreadReport,
          output_dir: selectedThreadOutputDir,
          instruction: $('followupInstruction').value.trim()
        })
      });
      renderThread(data);
      await loadReports();
    }

    async function copyFollowupDraft() {
      const draft = $('followupDraft');
      const button = $('copyFollowupDraftBtn');
      const value = String(draft ? draft.textContent : '').trim();
      if (!value || value === 'No follow-up draft yet.' || value === 'Loading follow-up draft...') {
        throw new Error('No follow-up draft is available to copy.');
      }
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(value);
      } else {
        const field = document.createElement('textarea');
        field.value = value;
        field.setAttribute('readonly', '');
        field.style.position = 'fixed';
        field.style.opacity = '0';
        document.body.appendChild(field);
        field.select();
        const copied = document.execCommand('copy');
        field.remove();
        if (!copied) throw new Error('Copy is not supported by this browser.');
      }
      if (button) {
        button.classList.remove('copy-error');
        button.classList.add('copied');
        button.setAttribute('aria-label', 'Follow-up draft copied');
        button.title = 'Copied';
        $('copyFollowupStatus').textContent = 'Follow-up draft copied to clipboard.';
        window.setTimeout(() => {
          button.classList.remove('copied');
          button.setAttribute('aria-label', 'Copy follow-up draft');
          button.title = 'Copy follow-up draft';
        }, 1400);
      }
    }

    async function sendAiChat() {
      const field = $('aiChatInput');
      const prompt = field.value.trim();
      field.removeAttribute('aria-invalid');
      $('aiChatError').className = 'field-message';
      $('aiChatError').textContent = '';
      if (!selectedThreadReport || !prompt) {
        field.setAttribute('aria-invalid', 'true');
        $('aiChatError').className = 'field-message error';
        $('aiChatError').textContent = 'Message is required.';
        field.focus();
        return;
      }
      appendAiChatMessage('user', prompt);
      $('aiChatInput').value = '';
      try {
        const data = await fetchJson('/api/report-thread/ai-chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            report: selectedThreadReport,
            output_dir: selectedThreadOutputDir,
            prompt
          })
        });
        appendAiChatMessage('assistant', data.reply || 'No response.');
      } catch (error) {
        appendAiChatMessage('assistant', `AI Chat failed: ${error.message}`);
      }
    }

    async function loadResponsibleDownloads() {
      const data = await fetchJson(`/api/responsibles${reportHistoryQuery()}`);
      renderResponsibleDownloads(Array.isArray(data.responsibles) ? data.responsibles : []);
    }

    function renderResponsibleDownloads(responsibles) {
      const container = $('responsibles');
      if (!container) return;
      if (!responsibles.length) {
        container.textContent = 'No responsible folders yet.';
        return;
      }
      container.innerHTML = responsibles.map((item) => `
        <div class="report-row">
          <div class="report-main">
            <strong>${escapeHtml(item.name || '-')}</strong>
            <div class="meta">${item.count || 0} report(s) · ${formatBytes(item.size || 0)} · ${formatTime(item.modified)}</div>
          </div>
          <div class="actions">
            <a class="download-link" href="${escapeHtml(item.download_url || '#')}">Download ZIP</a>
          </div>
        </div>
      `).join('');
    }

    async function loadTrace() {
      if (!$('config') || !$('history')) return;
      const [configData, historyData] = await Promise.all([
        fetchJson('/api/config'),
        fetchJson('/api/history?limit=12')
      ]);
      applyRuntimeConfig(configData);
      const llm = configData.llm || {};
      const gitnexus = configData.gitnexus || {};
      $('config').innerHTML = `
        <div><strong>Runtime</strong> LLM ${escapeHtml(llm.provider || 'auto')} / ${escapeHtml(llm.model || '-')} / speed ${escapeHtml(llm.speed || 'standard')} / min ${escapeHtml(configData.report_min_severity || 'Medium')}</div>
        <div><strong>Reports</strong> ${escapeHtml(configData.report_output_dir || '-')}</div>
        <div><strong>GitNexus</strong> ${escapeHtml(gitnexus.root || '-')}</div>
      `;
      const history = Array.isArray(historyData.history) ? historyData.history : [];
      $('history').innerHTML = history.length ? history.map((item) => `
        <div class="report-row">
          <div class="report-main">
            <strong>${escapeHtml(item.jira_key || item.title || 'Review')}</strong>
            <div class="meta">${escapeHtml(item.report_path || '')}</div>
          </div>
        </div>
      `).join('') : 'No review history yet.';
    }

    async function loadNetwork() {
      if (!$('network')) return;
      $('network').textContent = 'Checking network...';
      const data = await fetchJson('/api/network-check');
      $('network').innerHTML = `
        <div><strong>GitLab</strong> ${data.gitlab_reachable ? 'reachable' : 'unreachable'}</div>
        <div><strong>Codex</strong> ${data.codex_available ? 'available' : 'unavailable'}</div>
        <div class="meta">${escapeHtml(data.message || '')}</div>
      `;
    }

    function applyRuntimeConfig(configData) {
      if (!configData || !$('reportMinSeverity')) return;
      if (configData.report_output_dir) currentOutputDir = configData.report_output_dir;
      const value = configData.report_min_severity || 'Medium';
      if ([...$('reportMinSeverity').options].some((option) => option.value === value)) {
        $('reportMinSeverity').value = value;
      }
    }

    async function loadRuntimeConfig() {
      try {
        applyRuntimeConfig(await fetchJson('/api/config'));
      } catch (error) {
        console.warn('Runtime config unavailable', error);
      }
    }

    function formatBytes(value) {
      const bytes = Number(value || 0);
      if (bytes < 1024) return `${bytes} B`;
      if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
      return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
    }

    function formatTime(value) {
      const timestamp = Number(value || 0);
      if (!timestamp) return '-';
      return new Date(timestamp * 1000).toLocaleString();
    }

    function jobCardId(jobId) {
      return `job-${String(jobId || '').replace(/[^a-zA-Z0-9_-]/g, '')}`;
    }

    function updateProgressSummary() {
      const stateEl = $('progressState');
      const detailEl = $('progressDetail');
      const statusEl = $('status');
      if (!stateEl || !detailEl) return;
      const jobs = Array.from(jobSnapshots.values());
      const active = jobs.filter((job) => activeJobStatuses.has(job.status)).length;
      const running = jobs.filter((job) => ['queued', 'running', 'pausing', 'stopping'].includes(job.status)).length;
      const paused = jobs.filter((job) => job.status === 'paused').length;
      const done = jobs.filter((job) => job.status === 'done').length;
      const failed = jobs.filter((job) => job.status === 'failed').length;
      const canceled = jobs.filter((job) => job.status === 'canceled').length;
      if (!jobs.length) {
        stateEl.textContent = 'Idle';
        stateEl.className = 'progress-state';
        detailEl.textContent = 'Waiting for a Jira issue or Sprint review.';
        return;
      }
      stateEl.textContent = active ? `${active} Active` : 'All Settled';
      stateEl.className = `progress-state ${active ? 'running' : (failed ? 'error' : (canceled ? 'canceled' : 'ok'))}`;
      detailEl.innerHTML = `Running ${running} · Paused ${paused} · Completed ${done} · Failed ${failed} · Stopped ${canceled}`;
      if (!statusEl) return;
      if (active) {
        statusEl.className = 'status running';
        statusEl.textContent = `${active} active review job(s) · ${running} running · ${paused} paused · ${done} completed${failed ? ` · ${failed} failed` : ''}${canceled ? ` · ${canceled} stopped` : ''}`;
      } else if (failed) {
        statusEl.className = 'status error';
        statusEl.textContent = `All review jobs settled · ${done} completed · ${failed} failed${canceled ? ` · ${canceled} stopped` : ''}`;
      } else {
        statusEl.className = 'status ok';
        statusEl.textContent = `All review jobs settled · ${done} completed${canceled ? ` · ${canceled} stopped` : ''}`;
      }
      if (reviewLifecycleActive && active === 0) {
        reviewLifecycleActive = false;
        setRunFormCollapsed(false);
      }
    }

    function ensureJobCard(job) {
      const listEl = $('progressList');
      if (!listEl) return null;
      const id = jobCardId(job.id);
      let card = document.getElementById(id);
      if (!card) {
        card = document.createElement('li');
        card.id = id;
        card.className = 'job-card';
        listEl.prepend(card);
      }
      return card;
    }

    function releaseGateCandidatesForJob(job) {
      const summary = job?.result?.summary || {};
      const excluded = Array.isArray(summary.excluded_branch_type_mrs) ? summary.excluded_branch_type_mrs : [];
      return excluded.filter((item) => String(item.release_gate_role || '').toLowerCase() === 'git_version' || String(item.ignored_branch_type || '').toUpperCase() === 'GIT_VERSION');
    }

    function deferredResourceCountForJob(job) {
      const summary = job?.result?.summary || {};
      const excluded = Array.isArray(summary.excluded_branch_type_mrs) ? summary.excluded_branch_type_mrs : [];
      return excluded.filter((item) => ['company_config', 'scr'].includes(String(item.release_gate_role || '').toLowerCase())).length;
    }

    function renderReleaseGateHandoff(job) {
      if (job?.status !== 'done' || job?.payload?.mode !== 'sprint' || job?.result?.review_mode !== 'final-sprint' || !currentPermissions.run_release_gate) return '';
      const candidates = releaseGateCandidatesForJob(job);
      const deferredCount = deferredResourceCountForJob(job);
      if (candidates.length) {
        const first = candidates[0];
        const details = [candidates.length > 1 ? `${candidates.length} candidates` : '', deferredCount ? `${deferredCount} deferred` : ''].filter(Boolean).join(' · ');
        return `<button class="secondary gate-handoff" type="button" data-job-id="${escapeHtml(job.id || '')}" data-mr-url="${escapeHtml(first.mr_url || '')}" data-sprint="${escapeHtml(job.payload.sprint || '')}">Continue to Release Gate${details ? ` · ${details}` : ''}</button>`;
      }
      if (deferredCount) {
        return `<button class="secondary gate-handoff" type="button" data-job-id="${escapeHtml(job.id || '')}" data-mr-url="" data-sprint="${escapeHtml(job.payload.sprint || '')}">Open Release Gate · ${deferredCount} deferred resource(s)</button>`;
      }
      return '';
    }

    function renderGateResult(gate) {
      if (!gate || !Object.keys(gate).length) return '';
      const status = String(gate.status || 'unknown').toUpperCase();
      const blockers = Number.isFinite(Number(gate.finding_blocker_count))
        ? Number(gate.finding_blocker_count)
        : (Array.isArray(gate.errors) ? gate.errors.length : 0);
      return `<div class="gate-result-strip">
        <div class="gate-result-metric"><span>Gate status</span><strong>${escapeHtml(status)}</strong></div>
        <div class="gate-result-metric"><span>Locked sources</span><strong>${Number(gate.source_repository_count || 0)}</strong></div>
        <div class="gate-result-metric"><span>Build resources / blockers</span><strong>${Number(gate.build_resource_count || 0)} / ${blockers}</strong></div>
      </div>`;
    }

    function prepareReleaseGate(mrUrl, sprint, jobId = '') {
      const panel = $('releaseGatePanel');
      if (!panel) return;
      const candidates = releaseGateCandidatesForJob(jobSnapshots.get(jobId) || {});
      const candidateField = $('releaseGateCandidateField');
      const candidateSelect = $('releaseGateCandidateSelect');
      if (candidateField && candidateSelect) {
        candidateSelect.innerHTML = candidates.map((item) => {
          const project = item.project_path || item.gitlab_project || '';
          const label = [project, item.source_branch || 'GIT_VERSION'].filter(Boolean).join(' · ');
          return `<option value="${escapeHtml(item.mr_url || '')}">${escapeHtml(label)}</option>`;
        }).join('');
        candidateField.hidden = candidates.length < 2;
      }
      if ($('releaseGateMrUrl')) $('releaseGateMrUrl').value = mrUrl || '';
      window.requestAnimationFrame(autoSizeReleaseGateUrl);
      if ($('sprint') && sprint) $('sprint').value = sprint;
      if ($('releaseGateContext')) {
        $('releaseGateContext').textContent = sprint
          ? `Handoff from Sprint ${sprint}. Confirm the GIT_VERSION MR, then run the immutable release gate.`
          : 'Confirm the GIT_VERSION MR, then run the immutable release gate.';
      }
      setRunFormCollapsed(false);
      panel.scrollIntoView({ behavior: 'smooth', block: 'center' });
      window.setTimeout(() => $('releaseGateMrUrl')?.focus(), 180);
    }

    function renderJobProgress(job) {
      if (!job || !job.id) return;
      jobSnapshots.set(job.id, job);
      const card = ensureJobCard(job);
      if (!card) return;
      const payload = job.payload || {};
      const result = job.result || {};
      const summary = result.summary || {};
      const items = Array.isArray(summary.items) ? summary.items : [];
      const errors = Array.isArray(summary.errors) ? summary.errors : [];
      const counts = result.severity_counts || {};
      const severityText = ['Critical', 'High', 'Medium', 'Low', 'Warning'].map((key) => `${key}: ${counts[key] || 0}`).join(' · ');
      const reviewed = summary.reviewed || summary.processed || items.length || 0;
      const skipped = summary.skipped_completed || 0;
      const excluded = Array.isArray(summary.excluded_dev_branch_mrs) ? summary.excluded_dev_branch_mrs.length : 0;
      const stateSkipped = Array.isArray(summary.excluded_state_mrs) ? summary.excluded_state_mrs.length : 0;
      const branchTypeSkipped = Array.isArray(summary.excluded_branch_type_mrs) ? summary.excluded_branch_type_mrs.length : 0;
      const deferredResources = deferredResourceCountForJob(job);
      const gitVersionCandidates = releaseGateCandidatesForJob(job).length;
      const otherBranchTypeSkipped = Math.max(0, branchTypeSkipped - deferredResources - gitVersionCandidates);
      const elapsed = job.started_at ? Math.max(0, Math.round(((job.finished_at || Date.now() / 1000) - job.started_at))) : 0;
      const status = job.status || 'running';
      const statusClass = status === 'done' ? 'ok' : (status === 'failed' ? 'error' : (status === 'canceled' ? 'canceled' : 'running'));
      const title = payload.mode === 'release-gate'
        ? `Release Gate · MR ${String(payload.mr_url || '-').split('/').pop()}`
        : (payload.mode === 'jira-filter' ? `Jira Filter ${payload.jira_filter || '-'}` : (payload.mode === 'sprint' ? `Sprint ${payload.sprint || '-'}` : `Jira ${payload.jira_key || '-'}`));
      const events = Array.isArray(job.events) ? job.events.slice(-80) : [];
      card.dataset.status = status;
      card.classList.toggle('maximized', maximizedJobs.has(job.id));
      card.innerHTML = `
        <div class="job-head">
          <div class="job-leading">
            <span class="job-status-icon ${statusClass}" title="${escapeHtml(status)}" aria-label="${escapeHtml(status)}"></span>
            <div class="job-title-block">
              <strong>${escapeHtml(title)}</strong>
              <div class="meta">Job ${escapeHtml(job.id)} · elapsed ${elapsed}s</div>
            </div>
          </div>
          <div class="job-status-actions">
            ${renderJobControls(job)}
          </div>
        </div>
        <ul class="job-events" tabindex="0" aria-label="Review job event stream">
          ${events.map((event) => renderJobEvent(event)).join('') || '<li class="progress-item active"><span class="progress-dot"></span><span>Waiting for job events...</span></li>'}
        </ul>
        <div class="progress-detail">
          ${result.conclusion ? `<div><strong>${escapeHtml(result.conclusion)}</strong></div>` : ''}
          ${job.status === 'done' ? `<div>Reviewed ${reviewed} item(s) · Skipped completed ${skipped} · Deferred build resources ${deferredResources} · GIT_VERSION candidates ${gitVersionCandidates} · Other branch-type skipped ${otherBranchTypeSkipped} · Dev-branch skipped ${excluded} · State skipped ${stateSkipped}</div>` : ''}
          ${job.status === 'done' ? `<div>Findings ${result.finding_count || 0} · ${escapeHtml(severityText)}</div>` : ''}
          ${result.report ? `<div>Report: ${escapeHtml(result.report)}</div>` : ''}
          ${renderGateResult(result.release_gate || {})}
          ${renderReleaseGateHandoff(job)}
          ${errors.length ? `<div class="status error">Errors: ${escapeHtml(errors.map((item) => item.error || JSON.stringify(item)).join('; '))}</div>` : ''}
          ${job.error ? `<div class="status error">${escapeHtml(job.error)}</div>` : ''}
        </div>
      `;
      if (payload.mode === 'release-gate' && $('releaseGateStatus')) {
        const gateStatus = String((result.release_gate || {}).status || '').toUpperCase();
        if (status === 'done') {
          $('releaseGateStatus').className = `status ${gateStatus === 'READY' ? 'ok' : 'error'}`;
          $('releaseGateStatus').textContent = gateStatus === 'READY'
            ? 'Release Gate READY. Review the report before approval.'
            : `Release Gate ${gateStatus || 'completed'}. Resolve blockers and re-run.`;
        } else if (status === 'failed') {
          $('releaseGateStatus').className = 'status error';
          $('releaseGateStatus').textContent = job.error || 'Release Gate failed.';
        }
      }
      bindJobControlActions(card);
      scrollJobToLatest(card, job);
      updateProgressSummary();
    }

    function renderJobControls(job) {
      const status = job.status || '';
      const id = escapeHtml(job.id || '');
      const maximizeLabel = maximizedJobs.has(job.id) ? 'Restore' : 'Maximize';
      const jumpIcon = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v12m0 0-5-5m5 5 5-5M5 20h14"/></svg>`;
      const maximizeIcon = maximizedJobs.has(job.id)
        ? `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 4v5H4m16 6h-5v5M4 9l5-5m6 16 5-5"/></svg>`
        : `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 4H4v5m16 6v5h-5M4 9l5-5m6 16 5-5"/></svg>`;
      const viewControls = `<button class="icon-action job-view-control" type="button" data-job="${id}" data-action="jump" title="Jump latest" aria-label="Jump latest">${jumpIcon}<span class="visually-hidden">Jump latest</span></button><button class="icon-action job-view-control" type="button" data-job="${id}" data-action="maximize" title="${maximizeLabel}" aria-label="${maximizeLabel}">${maximizeIcon}<span class="visually-hidden">${maximizeLabel}</span></button>`;
      if (['queued', 'running'].includes(status)) {
        return `
          <button class="secondary small-action job-control" type="button" data-job="${id}" data-action="pause">Pause</button>
          <button class="secondary small-action job-control" type="button" data-job="${id}" data-action="stop">Stop</button>
          ${viewControls}
        `;
      }
      if (['pausing', 'paused'].includes(status)) {
        return `
          <button class="secondary small-action job-control" type="button" data-job="${id}" data-action="resume">Resume</button>
          <button class="secondary small-action job-control" type="button" data-job="${id}" data-action="stop">Stop</button>
          ${viewControls}
        `;
      }
      if (status === 'stopping') {
        return `<button class="secondary small-action" type="button" disabled>Stopping...</button>${viewControls}`;
      }
      if (['failed', 'canceled'].includes(status)) {
        return `<button class="secondary small-action job-control" type="button" data-job="${id}" data-action="retry">Retry</button>${viewControls}`;
      }
      return viewControls;
    }

    function bindJobControlActions(card) {
      for (const button of card.querySelectorAll('.job-control')) {
        button.addEventListener('click', () => controlReviewJob(button.dataset.job || '', button.dataset.action || ''));
      }
      for (const button of card.querySelectorAll('.job-view-control')) {
        button.addEventListener('click', () => {
          const jobId = button.dataset.job || '';
          if (button.dataset.action === 'jump') {
            if (jobAutoScrollResumeTimers.has(jobId)) {
              window.clearTimeout(jobAutoScrollResumeTimers.get(jobId));
              jobAutoScrollResumeTimers.delete(jobId);
            }
            jobAutoScroll.set(jobId, true);
            const events = card.querySelector('.job-events');
            if (events) events.scrollTop = events.scrollHeight;
            return;
          }
          if (maximizedJobs.has(jobId)) {
            maximizedJobs.delete(jobId);
          } else {
            document.querySelectorAll('.job-card.maximized').forEach(item => item.classList.remove('maximized'));
            maximizedJobs.clear();
            maximizedJobs.add(jobId);
          }
          renderJobProgress(jobSnapshots.get(jobId));
        });
      }
      const events = card.querySelector('.job-events');
      if (events) {
        const jobId = card.id.replace(/^job-/, '');
        if (!jobAutoScroll.has(jobId)) jobAutoScroll.set(jobId, true);
        const pauseAutoScroll = () => {
          jobAutoScroll.set(jobId, false);
          if (jobAutoScrollResumeTimers.has(jobId)) {
            window.clearTimeout(jobAutoScrollResumeTimers.get(jobId));
          }
          const timer = window.setTimeout(() => {
            jobAutoScrollResumeTimers.delete(jobId);
            jobAutoScroll.set(jobId, true);
            if (document.contains(events)) events.scrollTop = events.scrollHeight;
          }, 60000);
          jobAutoScrollResumeTimers.set(jobId, timer);
        };
        events.addEventListener('wheel', pauseAutoScroll, {passive:true});
        events.addEventListener('pointerdown', pauseAutoScroll, {passive:true});
        events.addEventListener('pointerup', pauseAutoScroll, {passive:true});
        events.addEventListener('touchstart', pauseAutoScroll, {passive:true});
        events.addEventListener('keydown', event => {
          if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key)) pauseAutoScroll();
        });
      }
      for (const button of card.querySelectorAll('.gate-handoff')) {
        button.addEventListener('click', () => prepareReleaseGate(button.dataset.mrUrl || '', button.dataset.sprint || '', button.dataset.jobId || ''));
      }
    }

    async function controlReviewJob(jobId, action) {
      if (!jobId || !['pause', 'resume', 'stop', 'retry'].includes(action)) return;
      if (action === 'retry') {
        await retryReviewJob(jobId);
        return;
      }
      try {
        const data = await fetchJson(`/api/reviews/${encodeURIComponent(jobId)}/${action}`, { method: 'POST' });
        if (data.job) renderJobProgress(data.job);
      } catch (error) {
        $('status').className = 'status error';
        $('status').textContent = error.message;
      }
    }

    async function retryReviewJob(jobId) {
      const previous = jobSnapshots.get(jobId);
      if (!previous || !previous.payload) {
        $('status').className = 'status error';
        $('status').textContent = 'Cannot retry: original job payload is unavailable.';
        return;
      }
      const payload = {
        ...previous.payload,
        retry_of: jobId,
        rerun_confirmed: true
      };
      $('status').className = 'status running';
      $('status').textContent = 'Creating retry job...';
      beginReviewLifecycle(payload);
      try {
        const data = await fetchJson('/api/reviews', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const placeholder = {
          id: data.job_id,
          status: data.status || 'queued',
          payload,
          events: [{ event: 'retry-queued', message: `Retry job queued from ${jobId}.`, data: { retry_of: jobId }, time: Date.now() / 1000 }],
          started_at: 0,
          finished_at: 0,
          result: null,
          error: ''
        };
        renderJobProgress(placeholder);
        pollReviewJob(data.job_id);
      } catch (error) {
        restoreRunFormAfterAbortedStart();
        $('status').className = 'status error';
        $('status').textContent = error.message;
      }
    }

    function scrollJobToLatest(card, job) {
      requestAnimationFrame(() => {
        const eventsEl = card.querySelector('.job-events');
        if (eventsEl && jobAutoScroll.get(job.id) !== false) eventsEl.scrollTop = eventsEl.scrollHeight;
        if (['queued', 'running', 'pausing', 'paused', 'stopping'].includes(job.status || '')) {
          const listEl = $('progressList');
          if (listEl && card.offsetTop < listEl.scrollTop) listEl.scrollTop = card.offsetTop;
        }
      });
    }

    function renderJobEvent(event) {
      const data = event.data || {};
      const eventKind = ['failed'].includes(event.event) ? 'error' : (['done', 'completed', 'skip-done', 'skip-dev-branch', 'skip-branch-type', 'skip-state', 'partial', 'paused', 'resumed', 'canceled'].includes(event.event) ? 'done' : 'active');
      const extra = [
        data.jira_key ? `Jira ${data.jira_key}` : '',
        data.mr_index && data.mr_total ? `MR ${data.mr_index}/${data.mr_total}` : '',
        data.mr_url ? data.mr_url : '',
        data.final_chars || data.original_chars ? `Context ${data.final_chars || '-'} / ${data.max_chars || '-'} chars; original ${data.original_chars || '-'}; trimmed ${data.trimmed_chars || 0}` : '',
        data.report ? `Report: ${data.report}` : '',
      ].filter(Boolean).join(' · ');
      return `
        <li class="progress-item ${eventKind}">
          <span class="progress-dot"></span>
          <span>
            <strong>${escapeHtml(event.event || 'event')}</strong> ${escapeHtml(event.message || '')}
            ${extra ? `<div class="meta">${escapeHtml(extra)}</div>` : ''}
          </span>
        </li>
      `;
    }

    function resetProgress() {
      jobSnapshots.clear();
      for (const timer of jobPollers.values()) clearInterval(timer);
      jobPollers.clear();
      $('progressList').innerHTML = '';
      updateProgressSummary();
    }

    async function loadReviewJobs() {
      const data = await fetchJson('/api/reviews?limit=100');
      const jobs = Array.isArray(data.jobs) ? data.jobs : [];
      if (!jobs.length) {
        updateProgressSummary();
        return;
      }
      for (const job of jobs) jobSnapshots.set(job.id, job);
      const restoredActiveJob = jobs.find((job) => activeJobStatuses.has(job.status || ''));
      if (restoredActiveJob) {
        lastReviewPayload = restoredActiveJob.payload || null;
        reviewLifecycleActive = true;
        setRunFormCollapsed(true, { payload: lastReviewPayload });
      }
      const ordered = jobs.slice().sort((left, right) => Number(left.created_at || 0) - Number(right.created_at || 0));
      for (const job of ordered) {
        renderJobProgress(job);
        if (['queued', 'running', 'pausing', 'paused', 'stopping'].includes(job.status || '')) {
          pollReviewJob(job.id);
        }
      }
      const latestDone = jobs.find((job) => job.status === 'done' && job.result);
      if (latestDone && latestDone.result) {
        setPreviewMarkdown(
          latestDone.result.markdown || JSON.stringify(latestDone.result, null, 2),
          latestDone.result.report_name || latestDone.result.report || 'Latest completed review',
          '',
          ''
        );
      }
      updateProgressSummary();
    }

    async function existingReportsForJira(jiraKey) {
      const query = [
        `jira=${encodeURIComponent(jiraKey)}`,
        currentOutputDir ? `output_dir=${encodeURIComponent(currentOutputDir)}` : ''
      ].filter(Boolean).join('&');
      return await fetchJson(`/api/report-check?${query}`);
    }

    function showReviewPreflight(jiraKey) {
      const list = $('progressList');
      if (!list) return null;
      const existing = $('reviewPreflightCard');
      if (existing) existing.remove();
      const card = document.createElement('li');
      card.id = 'reviewPreflightCard';
      card.className = 'review-preflight-card';
      card.dataset.state = 'running';
      card.innerHTML = `<div class="review-preflight-head">
        <span><span class="review-preflight-spinner" aria-hidden="true"></span><span><strong>Preparing ${escapeHtml(jiraKey)} Review</strong><span id="reviewPreflightDetail" class="meta">Checking existing Code Review reports…</span></span></span>
        <span id="reviewPreflightState" class="status-chip">Checking</span>
      </div><div class="review-preflight-track" role="progressbar" aria-label="Review preflight progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="38"><span></span></div>`;
      list.prepend(card);
      return card;
    }

    function updateReviewPreflight(state, detail, label) {
      const card = $('reviewPreflightCard');
      if (!card) return;
      card.dataset.state = state || 'running';
      const detailNode = $('reviewPreflightDetail');
      const stateNode = $('reviewPreflightState');
      if (detailNode) detailNode.textContent = detail || '';
      if (stateNode) stateNode.textContent = label || (state === 'ok' ? 'Ready' : (state === 'error' ? 'Failed' : 'Checking'));
      const progress = card.querySelector('[role="progressbar"]');
      if (progress) progress.setAttribute('aria-valuenow', state === 'running' ? '68' : '100');
    }

    function clearReviewPreflight(delay = 0) {
      const remove = () => $('reviewPreflightCard')?.remove();
      if (delay > 0) window.setTimeout(remove, delay);
      else remove();
    }

    async function confirmJiraRerunIfNeeded(jiraKey, mode) {
      if (mode !== 'jira' || !jiraKey) return { ok: true, rerun: false };
      const check = await existingReportsForJira(jiraKey);
      const reports = Array.isArray(check.reports) ? check.reports : [];
      if (!reports.length) {
        updateReviewPreflight('ok', 'No existing report was found. A new Review job can be created.', 'Ready');
        return { ok: true, rerun: false };
      }
      updateReviewPreflight('ok', `${reports.length} existing report(s) found. Review freshness and choose how to continue.`, 'Decision needed');
      return await showRerunConfirmDialog(jiraKey, check);
    }

    function showRerunConfirmDialog(jiraKey, check) {
      return new Promise((resolve) => {
        const reports = Array.isArray(check.reports) ? check.reports : [];
        const reuse = check.reuse || {};
        const freshAccessible = reports.find((report) => report.accessible && report.fresh && report.url);
        const modal = $('rerunConfirmModal');
        const stepOne = $('rerunStepOne');
        const stepTwo = $('rerunStepTwo');
        const input = $('rerunConfirmInput');
        const finalButton = $('rerunFinalConfirm');
        const useExistingButton = $('rerunUseExisting');
        const returnFocus = document.activeElement;
        const cleanup = (result) => {
          modal.hidden = true;
          document.removeEventListener('keydown', onKeyDown);
          if (returnFocus && typeof returnFocus.focus === 'function' && document.contains(returnFocus)) returnFocus.focus();
          resolve(result);
        };
        const onKeyDown = (event) => {
          trapDialogFocus(event, modal, () => cleanup(false));
        };
        $('rerunConfirmSummary').textContent = reuseSummaryText(jiraKey, reports, reuse);
        $('rerunReuseHint').textContent = reuseHintText(reuse);
        $('rerunReportList').innerHTML = reports.slice(0, 12).map((report) => `
          <div class="confirm-report">
            ${report.accessible && report.url
              ? `<a href="${escapeHtml(report.url || '#')}" target="_blank" rel="noopener">${breakableText(report.relative_path || report.name || '-')}</a>`
              : `<strong>${breakableText(report.relative_path || report.name || '-')}</strong>`}
            <span class="reuse-badge ${escapeHtml(report.reuse_status || 'unknown')}">${escapeHtml(reuseStatusLabel(report))}</span>
            <div class="meta">${escapeHtml(report.responsible || 'root')} · ${formatBytes(report.size || 0)} · ${formatTime(report.modified)}${report.accessible ? '' : ' · owned by another responsible'}</div>
            <div class="meta">${escapeHtml(report.reuse_reason || '')}</div>
          </div>
        `).join('') + (reports.length > 12 ? `<div class="meta">And ${reports.length - 12} more report(s).</div>` : '');
        stepOne.hidden = false;
        stepTwo.hidden = true;
        input.value = '';
        input.placeholder = jiraKey;
        finalButton.disabled = true;
        useExistingButton.hidden = !freshAccessible;
        $('rerunConfirmClose').onclick = () => cleanup(false);
        $('rerunCancelOne').onclick = () => cleanup(false);
        useExistingButton.onclick = () => cleanup({ ok: true, action: 'reuse', report: freshAccessible });
        $('rerunFirstConfirm').onclick = () => {
          stepOne.hidden = true;
          stepTwo.hidden = false;
          input.focus();
        };
        $('rerunBack').onclick = () => {
          stepTwo.hidden = true;
          stepOne.hidden = false;
        };
        input.oninput = () => {
          finalButton.disabled = input.value.trim().toUpperCase() !== jiraKey.toUpperCase();
        };
        finalButton.onclick = () => {
          if (!finalButton.disabled) cleanup({ ok: true, action: 'rescan' });
        };
        modal.hidden = false;
        document.addEventListener('keydown', onKeyDown);
        requestAnimationFrame(() => (freshAccessible ? useExistingButton : $('rerunFirstConfirm')).focus());
      });
    }

    function reuseSummaryText(jiraKey, reports, reuse) {
      const total = reports.length;
      if (reuse.status === 'fresh') {
        return `${jiraKey} already has a reusable report. No MR commit changes were detected.`;
      }
      if (reuse.status === 'fresh-other-owner') {
        return `${jiraKey} has a fresh report generated by another responsible. Your folder is isolated, so re-scan only if you need your own copy.`;
      }
      if (reuse.status === 'changed') {
        return `${jiraKey} has ${total} existing report(s), but GitLab/Jira metadata changed. Re-scan is recommended.`;
      }
      return `${jiraKey} already has ${total} report(s). Freshness could not be fully verified.`;
    }

    function reuseHintText(reuse) {
      if (reuse.status === 'fresh') {
        return 'Use Existing will open the latest matching report and skip a new LLM review, saving tokens. Re-scan remains available when you intentionally want a fresh report.';
      }
      if (reuse.status === 'fresh-other-owner') {
        return 'Another owner has a fresh report, but it is not automatically reused across login users. Contact the owner/admin or Re-scan to create your own isolated report.';
      }
      if (reuse.status === 'changed') {
        return 'Re-scan will create a new report and keep the current report for Compare. This is recommended because MR metadata no longer matches.';
      }
      return 'Re-scan will create a new report and keep existing reports; use it only when you really need a fresh LLM review.';
    }

    function reuseStatusLabel(report) {
      if (report.reuse_status === 'fresh') return 'Reusable';
      if (report.reuse_status === 'changed') return 'Updated';
      return 'Unknown';
    }

    async function loadSprintSuggestions() {
      const input = $('sprint');
      const list = $('sprintOptions');
      if (!input || !list) return;
      try {
        const data = await fetchJson(`/api/sprints?q=${encodeURIComponent(input.value.trim())}`);
        list.innerHTML = (data.sprints || []).map(item => `<option value="${escapeHtml(item.id || item.name || '')}" label="${escapeHtml(item.label || item.name || item.id || '')}">${escapeHtml(item.label || '')}</option>`).join('');
      } catch (error) {
        const status = $('sprintValidation');
        if (status) { status.className = 'field-message error'; status.textContent = `Sprint suggestions unavailable: ${error.message}`; }
      }
    }

    async function preflightSprint(sprint) {
      const status = $('sprintValidation');
      if (status) { status.className = 'field-message'; status.textContent = 'Validating Sprint and Issue statuses…'; }
      const data = await fetchJson(`/api/sprint-preflight?sprint=${encodeURIComponent(sprint)}`);
      if (!data.valid || !data.accessible || data.empty) {
        const message = data.error || (data.empty
          ? 'Sprint has no accessible Issues. Verify the Sprint ID and Jira permissions.'
          : 'Sprint is invalid or cannot be accessed.');
        if (status) { status.className = 'field-message error'; status.textContent = message; }
        throw new Error(message);
      }
      if (status) {
        status.className = 'field-message';
        status.textContent = data.review_mode === 'final-sprint'
          ? `Final Sprint Review · ${data.issue_count || 0} Issue(s), all Development Done.`
          : `Batch Issue Preview · ${data.not_development_done_count || 0} Issue(s) are not Development Done.`;
      }
      if (data.requires_confirmation) {
        const examples = (data.not_development_done_issues || []).slice(0, 5).map(item => `${item.jira_key} (${item.status})`).join(', ');
        const confirmed = window.confirm(`This Sprint is not ready for the final release gate. Continue as Batch Issue Preview?${examples ? `\n\nNot Development Done: ${examples}` : ''}`);
        if (!confirmed) throw new Error('Sprint preview canceled.');
      }
      return data;
    }

    async function runReview(options = {}) {
      $('status').className = 'status';
      $('status').textContent = 'Creating review job...';
      const jiraKey = $('jira').value.trim();
      const sprintEl = $('sprint');
      const sprint = sprintEl ? sprintEl.value.trim() : '';
      const jiraFilterEl = $('jiraFilter');
      const jiraFilter = jiraFilterEl ? jiraFilterEl.value.trim() : '';
      [$('jira'), sprintEl, jiraFilterEl].filter(Boolean).forEach(field => field.removeAttribute('aria-invalid'));
      if (!jiraKey && !sprint && !jiraFilter) {
        $('jira').setAttribute('aria-invalid', 'true');
        $('status').className = 'status error';
        $('status').textContent = 'Please input a Jira issue, Sprint, or Jira Filter ID.';
        $('jira').focus();
        return;
      }
      if (sprint && !currentUserIsAdmin) {
        $('status').className = 'status error';
        $('status').textContent = 'Sprint review is only available to Manager users.';
        return;
      }
      if (jiraFilter && !currentUserIsAdmin) {
        $('status').className = 'status error';
        $('status').textContent = 'Jira filter review is only available to Manager users.';
        return;
      }
      const mode = jiraFilter ? 'jira-filter' : (sprint ? 'sprint' : 'jira');
      let sprintPreflight = null;
      if (mode === 'sprint') {
        try {
          sprintPreflight = await preflightSprint(sprint);
        } catch (error) {
          $('status').className = 'status error';
          $('status').textContent = error.message;
          return;
        }
      }
      const payload = {
        mode,
        jira_key: jiraKey,
        jira_filter: jiraFilter,
        sprint: sprint,
        output_dir: currentOutputDir,
        report_min_severity: $('reportMinSeverity') ? $('reportMinSeverity').value : 'Medium',
        rerun_confirmed: false,
        review_mode: sprintPreflight ? sprintPreflight.review_mode : undefined,
        batch_preview_confirmed: Boolean(sprintPreflight && sprintPreflight.review_mode === 'batch-preview')
      };
      beginReviewLifecycle(payload, { focusProgress: !options.keepCoverageOpen });
      if (mode === 'jira') showReviewPreflight(jiraKey);
      try {
        var rerunDecision = await confirmJiraRerunIfNeeded(jiraKey, mode);
        if (rerunDecision && rerunDecision.action === 'reuse') {
          clearReviewPreflight();
          restoreRunFormAfterAbortedStart();
          const report = rerunDecision.report || {};
          $('status').className = 'status ok';
          $('status').textContent = 'Using existing fresh report. No new review job was created.';
          if (report.thread_report || report.relative_path) {
            openReportPreview(
              report.thread_report || report.relative_path || '',
              report.url || '',
              report.download_url || '',
              { outputDir: report.output_dir || currentOutputDir || '' }
            );
          }
          return;
        }
        if (!rerunDecision.ok) {
          clearReviewPreflight();
          restoreRunFormAfterAbortedStart();
          $('status').className = 'status';
          $('status').textContent = 'Review canceled.';
          return;
        }
      } catch (error) {
        updateReviewPreflight('error', `Existing report check failed: ${error.message}`, 'Check failed');
        clearReviewPreflight(4500);
        restoreRunFormAfterAbortedStart();
        $('status').className = 'status error';
        $('status').textContent = `Report check failed: ${error.message}`;
        return;
      }
      payload.rerun_confirmed = Boolean(rerunDecision && rerunDecision.action === 'rescan');
      try {
        if (mode === 'jira') {
          updateReviewPreflight('ok', 'Preflight complete. Creating the Review job…', 'Starting');
        }
        const data = await fetchJson('/api/reviews', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const placeholder = {
          id: data.job_id,
          status: data.status || 'queued',
          payload,
          events: [{ event: 'queued', message: 'Review job queued in browser.', data: {}, time: Date.now() / 1000 }],
          started_at: 0,
          finished_at: 0,
          result: null,
          error: ''
        };
        renderJobProgress(placeholder);
        clearReviewPreflight(500);
        if (jiraKey) $('jira').value = '';
        if (jiraFilterEl && jiraFilter) jiraFilterEl.value = '';
        pollReviewJob(data.job_id);
      } catch (error) {
        updateReviewPreflight('error', `Review job could not be created: ${error.message}`, 'Start failed');
        clearReviewPreflight(4500);
        restoreRunFormAfterAbortedStart();
        $('status').className = 'status error';
        $('status').textContent = error.message;
      }
    }

    function pollReviewJob(jobId) {
      if (jobPollers.has(jobId)) return;
      const tick = async () => {
        try {
          const data = await fetchJson(`/api/reviews/${encodeURIComponent(jobId)}`);
          const job = data.job;
          renderJobProgress(job);
          if (['done', 'failed', 'canceled'].includes(job.status)) {
            clearInterval(jobPollers.get(jobId));
            jobPollers.delete(jobId);
            if (job.status === 'done') {
              const result = job.result || {};
              setPreviewMarkdown(
                result.markdown || JSON.stringify(result, null, 2),
                result.report_name || result.report || 'Generated review report',
                '',
                ''
              );
              await loadReports();
              await loadResponsibleDownloads();
              if ($('config')) await loadTrace();
            } else if (job.status === 'canceled') {
              $('status').className = 'status';
              $('status').textContent = job.error || 'Review job stopped.';
            } else {
              $('status').className = 'status error';
              $('status').textContent = job.error || 'Review job failed';
            }
          }
        } catch (error) {
          clearInterval(jobPollers.get(jobId));
          jobPollers.delete(jobId);
          const current = jobSnapshots.get(jobId) || { id: jobId, status: 'failed', payload: {}, events: [] };
          current.status = 'failed';
          current.error = error.message;
          renderJobProgress(current);
          $('status').className = 'status error';
          $('status').textContent = error.message;
        }
      };
      const timer = setInterval(tick, 1500);
      jobPollers.set(jobId, timer);
      tick();
    }
    function escapeHtml(value) {
      return String(value || '').replace(/[&<>"']/g, char => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
      }[char]));
    }

    let issueReviews = [];
    let selectedIssueReview = null;
    let currentJiraDraft = null;
    let atlaskitAdfUnmount = null;
    let issueReviewView = 'overview';
    let issueReviewSprintFilter = '';
    let issueReviewApplicationFilter = '';

    function setIssueReviewView(view) {
      issueReviewView = view === 'issues' ? 'issues' : 'overview';
      $('issueReviewOverviewPanel').hidden = issueReviewView !== 'overview';
      $('issueReviewIssuesView').hidden = issueReviewView !== 'issues';
      [['issueOverviewTab', 'overview'], ['issueListTab', 'issues']].forEach(([id, value]) => {
        const tab = $(id);
        tab.classList.toggle('active', issueReviewView === value);
        tab.setAttribute('aria-selected', issueReviewView === value ? 'true' : 'false');
      });
      if (issueReviewView === 'overview') renderIssueReviewOverview(); else renderIssueReviews();
    }

    function renderIssueReviewOverview() {
      const applicationOrder = ['WVAdmin', 'iTrade Client 7.5.0', 'iTrade Client 7.5.1', 'Services Terminal', 'DPS9', 'DPS11'];
      const groups = new Map();
      issueReviews.forEach(issue => {
        const cycles = (issue.cycles || []).length ? issue.cycles : [issue.current_cycle || {}];
        cycles.forEach(cycle => {
          const sprintId = String(cycle.sprint_id || 'legacy');
          const sprintName = String(cycle.sprint_name || (sprintId === 'legacy' ? 'Legacy / Unknown Sprint' : sprintId));
          const key = `${sprintId}|${sprintName}`;
          if (!groups.has(key)) groups.set(key, {key, sprintId, sprintName, state:String(cycle.sprint_state || 'unknown'), entries:[]});
          groups.get(key).entries.push({issue, cycle});
        });
      });
      const rows = [...groups.values()].sort((a, b) => b.sprintId.localeCompare(a.sprintId, undefined, {numeric:true}));
      $('issueReviewOverviewPanel').innerHTML = rows.length ? `<div class="sprint-overview-grid">${rows.map(group => {
        const entries = group.entries;
        const issueKeys = new Set(entries.map(({issue}) => issue.jira_key));
        const total = issueKeys.size;
        const passed = new Set(entries.filter(({issue, cycle}) => cycle.pass_status === 'passed' || (issue.current_cycle_id === cycle.cycle_id && issue.status === 'passed')).map(({issue}) => issue.jira_key)).size;
        const blockers = entries.reduce((sum, {issue, cycle}) => sum + (issue.current_cycle_id === cycle.cycle_id ? Number(((issue.pass_readiness || {}).pending_blockers || []).length) : 0), 0);
        const pending = entries.reduce((sum, {issue, cycle}) => sum + (issue.current_cycle_id === cycle.cycle_id ? Number((issue.handling_counts || {}).pending || 0) : 0), 0);
        const snapshots = entries.reduce((sum, {cycle}) => sum + Number(cycle.review_snapshot_count || 0), 0);
        const perApplication = new Map(applicationOrder.map(name => [name, []]));
        entries.forEach(({issue, cycle}) => {
          let progress = Array.isArray(cycle.application_progress) ? cycle.application_progress : [];
          if (!progress.length) {
            progress = [{application:'Unmapped', state: issue.latest_run_id ? 'handling' : 'without-report', report_count:issue.latest_run_id ? 1 : 0}];
          }
          progress.forEach(item => {
            const scopeLabel = String(item.scope_label || item.application || 'Unmapped');
            if (!perApplication.has(scopeLabel)) perApplication.set(scopeLabel, []);
            perApplication.get(scopeLabel).push({issue, cycle, progress:item});
          });
        });
        const additionalScopes = [...perApplication.keys()].filter(name => !applicationOrder.includes(name)).sort();
        const applications = [...applicationOrder, ...additionalScopes];
        const appCards = applications.map(application => {
          const scoped = perApplication.get(application) || [];
          const unique = new Map(scoped.map(item => [item.issue.jira_key, item]));
          const values = [...unique.values()];
          const countState = state => values.filter(item => item.progress.state === state).length;
          const appTotal = values.length;
          const reports = values.filter(item => Number(item.progress.report_count || 0) > 0).length;
          const withoutReport = countState('without-report');
          const generating = countState('generating');
          const handling = countState('handling');
          const ready = countState('ready-for-pass');
          const reviewPass = countState('review-pass');
          const failed = countState('failed');
          const remaining = Math.max(0, appTotal - reviewPass);
          const percent = appTotal ? Math.round(reviewPass * 100 / appTotal) : null;
          const readyForGate = appTotal > 0 && remaining === 0 && application !== 'Unmapped' && !application.startsWith('Unmapped ');
          return `<button class="sprint-application-card" data-ready="${readyForGate ? 'true' : 'false'}" data-application="${escapeHtml(application)}" data-open-sprint="${escapeHtml(group.key)}" data-sprint-label="${escapeHtml(`${group.sprintName} (${group.sprintId})`)}" type="button" aria-label="View ${escapeHtml(application)} issues in ${escapeHtml(group.sprintName)}">
            <span class="sprint-application-head"><strong>${escapeHtml(application)}</strong><span class="status-chip">${readyForGate ? 'Ready for Gate' : (appTotal ? `${remaining} remaining` : 'N/A')}</span></span>
            <span class="sprint-application-progress-head"><span class="meta">Review Pass</span><span class="sprint-application-percent">${percent === null ? 'N/A' : `${percent}%`}</span></span>
            <span class="sprint-application-progress" role="progressbar" aria-label="${escapeHtml(application)} review progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent || 0}"><span style="width:${percent || 0}%"></span></span>
            <span class="sprint-application-stats">
              <span class="sprint-application-stat">Reports <strong>${reports}</strong></span><span class="sprint-application-stat">Without report <strong>${withoutReport}</strong></span>
              <span class="sprint-application-stat">Generating <strong>${generating}</strong></span><span class="sprint-application-stat">Handling <strong>${handling}</strong></span>
              <span class="sprint-application-stat">Ready for Pass <strong>${ready}</strong></span><span class="sprint-application-stat">Review Pass <strong>${reviewPass}</strong></span>
              <span class="sprint-application-stat">Failed <strong>${failed}</strong></span><span class="sprint-application-stat">Remaining <strong>${remaining}</strong></span>
            </span>
          </button>`;
        }).join('');
        return `<article class="sprint-overview-card"><div class="sprint-overview-head"><div><strong>${escapeHtml(group.sprintName)}</strong><div class="meta">Sprint ${escapeHtml(group.sprintId)} · ${escapeHtml(statusLabel(group.state))}</div></div><span class="count-pill">${total} issues</span></div><div class="sprint-overview-metrics"><div class="sprint-overview-metric"><span class="meta">Review Pass</span><strong>${passed}/${total}</strong></div><div class="sprint-overview-metric"><span class="meta">Blockers</span><strong>${blockers}</strong></div><div class="sprint-overview-metric"><span class="meta">Pending handling</span><strong>${pending}</strong></div></div><div class="sprint-application-grid">${appCards}</div><div class="sprint-overview-footer"><div class="meta">${snapshots} review snapshot(s) · ${entries.length} cycle membership(s)</div><button class="secondary" type="button" data-open-sprint="${escapeHtml(group.key)}" data-sprint-label="${escapeHtml(`${group.sprintName} (${group.sprintId})`)}">View sprint issues</button></div></article>`;
      }).join('')}</div>` : '<div class="markdown-preview empty">No persisted Sprint review data is available.</div>';
      $('issueReviewOverviewPanel').querySelectorAll('[data-open-sprint]').forEach(button => button.addEventListener('click', () => {
        issueReviewSprintFilter = button.dataset.openSprint || '';
        issueReviewApplicationFilter = button.dataset.application || '';
        $('issueReviewSearch').value = '';
        $('issueReviewScope').hidden = false;
        $('issueReviewScope').textContent = `${button.dataset.sprintLabel || 'Sprint'}${issueReviewApplicationFilter ? ` · ${issueReviewApplicationFilter}` : ''}`;
        setIssueReviewView('issues');
      }));
    }

    async function openIssueReviews() {
      $('issueReviewModal').hidden = false;
      await loadIssueReviews();
    }

    function closeIssueReviews() {
      $('issueReviewModal').hidden = true;
    }

    async function loadIssueReviews() {
      $('issueReviewList').textContent = 'Loading Issue Reviews...';
      try {
        const data = await fetchJson('/api/issue-reviews');
        issueReviews = data.issues || [];
        renderIssueReviewOverview();
        renderIssueReviews();
        if (selectedIssueReview) await loadIssueReviewDetail(selectedIssueReview);
      } catch (error) {
        $('issueReviewList').textContent = error.message;
      }
    }

    async function runReleaseGate() {
      const button = $('runReleaseGateBtn');
      const status = $('releaseGateStatus');
      const mrUrl = ($('releaseGateMrUrl')?.value || '').replace(/\\s+/g, '').trim();
      const sprint = ($('sprint')?.value || '').trim();
      if (!currentPermissions.run_release_gate) {
        if (status) { status.className = 'status error'; status.textContent = 'Release Gate is only available to Manager users.'; }
        return;
      }
      let validMrUrl = false;
      try {
        const parsedMrUrl = new URL(mrUrl);
        validMrUrl = ['http:', 'https:'].includes(parsedMrUrl.protocol)
          && /^\\/.+\\/-\\/merge_requests\\/[0-9]+\\/?$/.test(parsedMrUrl.pathname);
      } catch (_error) {
        validMrUrl = false;
      }
      if (!validMrUrl) {
        $('releaseGateMrUrl')?.setAttribute('aria-invalid', 'true');
        if (status) { status.className = 'status error'; status.textContent = 'Enter a valid GIT_VERSION merge request URL.'; }
        $('releaseGateMrUrl')?.focus();
        return;
      }
      $('releaseGateMrUrl')?.removeAttribute('aria-invalid');
      if ($('releaseGateMrUrl')) {
        $('releaseGateMrUrl').value = mrUrl;
        autoSizeReleaseGateUrl();
      }
      if (button?.disabled) return;
      const payload = {
        mode: 'release-gate',
        mr_url: mrUrl,
        sprint,
        output_dir: currentOutputDir,
        report_min_severity: $('reportMinSeverity') ? $('reportMinSeverity').value : 'Medium',
        rerun_confirmed: true
      };
      if (button) { button.disabled = true; button.setAttribute('aria-busy', 'true'); }
      if (status) { status.className = 'status running'; status.textContent = 'Creating Release Gate job...'; }
      beginReviewLifecycle(payload);
      try {
        const data = await fetchJson('/api/reviews', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const placeholder = {
          id: data.job_id,
          status: data.status || 'queued',
          payload,
          events: [{ event: 'queued', message: 'GIT_VERSION Release Gate queued in Web.', data: { mr_url: mrUrl, sprint }, time: Date.now() / 1000 }],
          started_at: 0,
          finished_at: 0,
          result: null,
          error: ''
        };
        renderJobProgress(placeholder);
        pollReviewJob(data.job_id);
      } catch (error) {
        restoreRunFormAfterAbortedStart();
        if (status) { status.className = 'status error'; status.textContent = error.message; }
      } finally {
        if (button) { button.disabled = false; button.removeAttribute('aria-busy'); }
      }
    }

    function renderIssueReviews() {
      const query = ($('issueReviewSearch').value || '').trim().toLowerCase();
      const rows = issueReviews.filter(item => {
        const cycles = (item.cycles || []).length ? item.cycles : [item.current_cycle || {}];
        const inSprint = !issueReviewSprintFilter || cycles.some(cycle => `${cycle.sprint_id || 'legacy'}|${cycle.sprint_name || (cycle.sprint_id === 'legacy' ? 'Legacy / Unknown Sprint' : cycle.sprint_id || 'legacy')}` === issueReviewSprintFilter);
        const inApplication = !issueReviewApplicationFilter || cycles.some(cycle => {
          const cycleKey = `${cycle.sprint_id || 'legacy'}|${cycle.sprint_name || (cycle.sprint_id === 'legacy' ? 'Legacy / Unknown Sprint' : cycle.sprint_id || 'legacy')}`;
          if (issueReviewSprintFilter && cycleKey !== issueReviewSprintFilter) return false;
          const progress = Array.isArray(cycle.application_progress) ? cycle.application_progress : [];
          if (!progress.length) return issueReviewApplicationFilter === 'Unmapped';
          return progress.some(entry => String(entry.scope_label || entry.application || 'Unmapped') === issueReviewApplicationFilter);
        });
        const haystack = [item.jira_key, item.summary, item.responsible, item.status, ...cycles.flatMap(cycle => [cycle.sprint_id, cycle.sprint_name])].join(' ').toLowerCase();
        return inSprint && inApplication && haystack.includes(query);
      });
      $('issueReviewCount').textContent = String(rows.length);
      if (!rows.length) {
        $('issueReviewList').innerHTML = '<div class="markdown-preview empty">No Issue Review records match this scope.</div>';
        return;
      }
      $('issueReviewList').innerHTML = `<div class="issue-review-cards">${rows.map(item => {
        const counts = item.handling_counts || {};
        const selected = selectedIssueReview === item.jira_key ? ' selected' : '';
        return `<article class="issue-review-card${selected}" data-jira="${escapeHtml(item.jira_key)}" role="button" tabindex="0" aria-label="Open ${escapeHtml(item.jira_key)} Issue Review">
          <div class="issue-review-card-head"><strong class="issue-review-key">${escapeHtml(item.jira_key)}</strong><span class="status-chip" data-status="${escapeHtml(item.status)}">${escapeHtml(statusLabel(item.status))}</span></div>
          <div class="issue-review-summary">${escapeHtml(item.summary || 'No summary')}</div>
          <div class="meta">${escapeHtml(item.responsible || '-')} · Run ${escapeHtml(item.run_number || '-')} · ${escapeHtml(item.finding_count || 0)} findings</div>
          <div class="issue-review-progress"><span class="handling-chip">Fixed ${counts.fixed || 0}</span><span class="handling-chip">Jira ${counts['follow-up'] || 0}</span><span class="handling-chip">Not issue ${counts['not-issue'] || 0}</span><span class="handling-chip">Pending ${counts.pending || 0}</span></div>
          <div class="issue-review-card-foot"><span class="meta">Last updated</span><time class="meta issue-review-updated">${escapeHtml(formatDateTime(item.updated_at))}</time></div>
        </article>`;
      }).join('')}</div>`;
      document.querySelectorAll('.issue-review-card').forEach(card => {
        const open = () => loadIssueReviewDetail(card.dataset.jira || '');
        card.addEventListener('click', open);
        card.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(); } });
      });
    }

    function statusLabel(value) {
      return String(value || 'not-reviewed').split('-').map(part => part ? part[0].toUpperCase() + part.slice(1) : '').join(' ');
    }

    function formatDateTime(value) {
      if (!value) return '-';
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
    }

    async function loadIssueReviewDetail(jiraKey) {
      setIssueReviewView('issues');
      selectedIssueReview = jiraKey;
      document.querySelectorAll('.issue-review-card').forEach(card => {
        card.classList.toggle('selected', card.dataset.jira === jiraKey);
      });
      $('issueReviewDetail').innerHTML = '<div class="markdown-preview empty">Loading Issue Review...</div>';
      try {
        const data = await fetchJson(`/api/issue-reviews/${encodeURIComponent(jiraKey)}`);
        renderIssueReviewDetail(data);
      } catch (error) {
        $('issueReviewDetail').innerHTML = `<div class="status error">${escapeHtml(error.message)}</div>`;
      }
    }

    function renderIssueReviewDetail(data) {
      const issue = data.issue || {};
      const runs = data.runs || [];
      const latest = data.latest_run_group || runs[0] || { findings: [] };
      const findings = latest.findings || [];
      const readiness = data.pass_readiness || {};
      const severity = latest.severity_counts || {};
      const discussions = data.discussions || [];
      const drafts = data.drafts || [];
      const cycles = data.cycles || [];
      const snapshots = data.review_snapshots || [];
      const canPass = Boolean((data.permissions || {}).manual_pass);
      const canReview = Boolean((data.permissions || {}).run_issue_review);
      const severityProgress = level => {
        const scoped = findings.filter(item => String(item.severity || '').toLowerCase() === level.toLowerCase());
        const handled = scoped.filter(item => Boolean(item.handling)).length;
        const total = scoped.length;
        return {handled, unhandled: Math.max(0, total - handled), total, percent: total ? Math.round(handled * 100 / total) : 0};
      };
      const criticalProgress = severityProgress('Critical');
      const highProgress = severityProgress('High');
      const mediumProgress = severityProgress('Medium');
      const cycleHistory = cycles.length ? cycles.map(cycle => {
        const cycleRuns = runs.filter(run => String(run.cycle_id || '') === String(cycle.cycle_id || ''));
        const cycleSnapshots = snapshots.filter(snapshot => String(snapshot.cycle_id || '') === String(cycle.cycle_id || ''));
        const runCards = cycleRuns.length ? cycleRuns.map(run => `<div class="timeline-card"><strong>Run ${escapeHtml(run.run_number)}</strong><div class="meta">Run Group ${escapeHtml(run.run_group_id || '-')} · Cycle ${escapeHtml(cycle.cycle_number || '-')} · ${escapeHtml(formatDateTime(run.created_at))}</div><div class="meta">${escapeHtml(run.conclusion || 'Completed')} · ${escapeHtml(run.report_path || '')}</div><div>${(run.findings || []).filter(item => item.lineage_state === 'new').length} New · ${(run.findings || []).filter(item => item.lineage_state === 'persisting').length} Persisting</div></div>`).join('') : '<div class="meta">No Review Runs in this Cycle.</div>';
        const snapshotCards = cycleSnapshots.length ? cycleSnapshots.map(snapshot => `<div class="timeline-card"><strong>Snapshot r${escapeHtml(snapshot.revision || '-')}</strong> · ${escapeHtml(snapshot.reason || '')}<div class="meta">Cycle ${escapeHtml(cycle.cycle_number || '-')} · ${escapeHtml(snapshot.actor || 'system')} · ${escapeHtml(formatDateTime(snapshot.created_at))}</div></div>`).join('') : '<div class="meta">No review snapshots in this Cycle.</div>';
        return `<article class="timeline-card cycle-history-card"><div class="finding-head"><div><strong>Cycle ${escapeHtml(cycle.cycle_number || '-')} · ${escapeHtml(cycle.sprint_name || 'Legacy / Unknown Sprint')}</strong><div class="meta">Sprint ${escapeHtml(cycle.sprint_id || 'legacy')} · ${escapeHtml(statusLabel(cycle.sprint_state || 'unknown'))} · ${escapeHtml(statusLabel(cycle.review_mode || 'issue'))}</div></div><span class="status-chip">${cycle.cycle_closed_at ? 'Closed' : 'Current'}</span></div><div class="meta">Started ${escapeHtml(formatDateTime(cycle.cycle_started_at))}${cycle.cycle_closed_at ? ` · Closed ${escapeHtml(formatDateTime(cycle.cycle_closed_at))}` : ''}</div><h5>Review Runs</h5>${runCards}<h5>Review Snapshots</h5>${snapshotCards}</article>`;
      }).join('') : '<div class="meta">No Review Cycle history.</div>';
      const pendingBlockerIds = new Set((readiness.pending_blockers || []).map(item => String(
        item && typeof item === 'object' ? (item.finding_id || item.id || '') : item
      )));
      $('issueReviewDetail').innerHTML = `
        <section class="issue-overview" aria-label="Issue review overview">
        <header class="issue-review-header"><div class="issue-review-identity"><div class="issue-hero-title-row"><h2>${escapeHtml(issue.jira_key)} · ${escapeHtml(issue.summary || 'Issue Review')}</h2><span class="info-hint"><button class="information-icon" type="button" aria-label="Issue overview guidance" aria-expanded="false" aria-controls="issueOverviewHintPopover">i</button><span id="issueOverviewHintPopover" class="information-hint-popover" role="tooltip" hidden>Metrics use the latest logical Review Run. Click a severity card to jump to its first Problem.</span></span></div><div class="meta issue-hero-meta"><span>Responsible: ${escapeHtml(issue.responsible || '-')}</span><span>Latest Run ${escapeHtml(latest.run_number || '-')}</span><span>Updated ${escapeHtml(formatDateTime(issue.updated_at))}</span></div></div><div class="issue-review-controls"><div class="issue-review-actions"><span class="status-chip" data-status="${escapeHtml(issue.status)}">${escapeHtml(statusLabel(issue.status))}</span>${canReview ? '<button id="issueRescanBtn" class="secondary" type="button">Re-scan Issue</button>' : ''}${canPass ? `<button id="issuePassBtn" type="button" ${readiness.ready ? '' : 'disabled'}>Manual Pass</button>` : ''}</div><div class="issue-readiness ${readiness.ready ? 'ready' : ''}" role="status"><span class="issue-readiness-dot" aria-hidden="true"></span><span class="meta">${escapeHtml(readiness.message || '')}</span></div></div></header>
        <div class="metric-grid">
          <button class="metric-card" type="button" data-jump-severity="critical" ${(severity.Critical || 0) ? '' : 'disabled'}><span class="metric-card-head"><span class="meta">Critical</span><strong>${severity.Critical || 0}</strong></span><span class="metric-ratio"><span>${criticalProgress.handled} handled / ${criticalProgress.unhandled} unhandled</span><b>${criticalProgress.percent}%</b></span><span class="metric-bar" aria-label="Critical ${criticalProgress.percent}% handled"><span style="--completion:${criticalProgress.percent}%"></span></span></button>
          <button class="metric-card" type="button" data-jump-severity="high" ${(severity.High || 0) ? '' : 'disabled'}><span class="metric-card-head"><span class="meta">High</span><strong>${severity.High || 0}</strong></span><span class="metric-ratio"><span>${highProgress.handled} handled / ${highProgress.unhandled} unhandled</span><b>${highProgress.percent}%</b></span><span class="metric-bar" aria-label="High ${highProgress.percent}% handled"><span style="--completion:${highProgress.percent}%"></span></span></button>
          <button class="metric-card" type="button" data-jump-severity="medium" ${(severity.Medium || 0) ? '' : 'disabled'}><span class="metric-card-head"><span class="meta">Medium</span><strong>${severity.Medium || 0}</strong></span><span class="metric-ratio"><span>${mediumProgress.handled} handled / ${mediumProgress.unhandled} unhandled</span><b>${mediumProgress.percent}%</b></span><span class="metric-bar" aria-label="Medium ${mediumProgress.percent}% handled"><span style="--completion:${mediumProgress.percent}%"></span></span></button>
          <div class="metric-card metric-summary-card"><div class="metric-summary-row"><span class="meta">Manager exceptions</span><strong>${readiness.manager_exceptions || 0}</strong></div><button class="metric-summary-row" type="button" data-jump-blocker="true" ${(readiness.pending_blockers || []).length ? '' : 'disabled'}><span class="meta">Remaining blockers</span><strong>${(readiness.pending_blockers || []).length}</strong></button></div>
        </div>
        </section>
        <div class="workflow-tabs" role="tablist" aria-label="Issue Review details"><button class="workflow-tab active" type="button" role="tab" aria-selected="true" aria-controls="workflowProblemsPanel" data-workflow-tab="problems">Problems</button><button class="workflow-tab" type="button" role="tab" aria-selected="false" aria-controls="workflowDiscussPanel" data-workflow-tab="discuss">Discuss (${discussions.length})</button><button class="workflow-tab" type="button" role="tab" aria-selected="false" aria-controls="workflowHistoryPanel" data-workflow-tab="history">History (${runs.length})</button><button class="workflow-tab" type="button" role="tab" aria-selected="false" aria-controls="workflowPendingPanel" data-workflow-tab="pending">Pending Jira (${drafts.length})</button></div>
        <section id="workflowProblemsPanel" class="workflow-section" role="tabpanel" data-workflow-panel="problems"><h3 class="workflow-section-title">Problem list · Run ${escapeHtml(latest.run_number || '-')}</h3>${findings.length ? findings.map(finding => renderWorkflowFinding(finding, data.role, pendingBlockerIds)).join('') : '<div class="markdown-preview empty">No findings in the latest Run. This Issue is ready for Leader review.</div>'}</section>
        <section id="workflowDiscussPanel" class="workflow-section" role="tabpanel" data-workflow-panel="discuss" hidden><h3 class="workflow-section-title">Discuss</h3><div>${discussions.length ? discussions.map(item => `<div class="discussion-card"><strong>${escapeHtml(item.author)}</strong><span class="meta"> · ${escapeHtml(formatDateTime(item.created_at))}</span><p>${escapeHtml(item.message)}</p></div>`).join('') : '<div class="meta">No discussion yet.</div>'}</div><div class="finding-actions"><label for="issueDiscussionInput">Message <span class="required-mark" aria-hidden="true">*</span></label><textarea id="issueDiscussionInput" placeholder="Discuss this Review Run or ask for clarification." required aria-describedby="issueDiscussionError"></textarea><div id="issueDiscussionError" class="field-message" role="alert"></div><button id="sendIssueDiscussionBtn" class="secondary" type="button">Send</button></div></section>
        <section id="workflowHistoryPanel" class="workflow-section" role="tabpanel" data-workflow-panel="history" hidden><h3 class="workflow-section-title">History &amp; Snapshots</h3><p class="meta">Each Sprint Cycle contains its own Review Runs, Run Groups and immutable Snapshots.</p>${cycleHistory}</section>
        <section id="workflowPendingPanel" class="workflow-section" role="tabpanel" data-workflow-panel="pending" hidden><h3 class="workflow-section-title">Pending Jira</h3>${drafts.length ? drafts.map(renderDraftCard).join('') : '<div class="meta">No Jira follow-up drafts.</div>'}</section>`;
      const activateWorkflowTab = name => {
        $('issueReviewDetail').querySelectorAll('[data-workflow-tab]').forEach(tab => {
          const active = tab.dataset.workflowTab === name;
          tab.classList.toggle('active', active);
          tab.setAttribute('aria-selected', active ? 'true' : 'false');
        });
        $('issueReviewDetail').querySelectorAll('[data-workflow-panel]').forEach(panel => {
          panel.hidden = panel.dataset.workflowPanel !== name;
        });
      };
      $('issueReviewDetail').querySelectorAll('[data-workflow-tab]').forEach(tab => tab.addEventListener('click', () => activateWorkflowTab(tab.dataset.workflowTab || 'problems')));
      $('issueReviewDetail').querySelectorAll('[data-handle-finding]').forEach(button => button.addEventListener('click', () => submitWorkflowHandling(button.dataset.handleFinding || '', button)));
      $('issueReviewDetail').querySelectorAll('[data-expand-finding]').forEach(button => button.addEventListener('click', () => {
        const summary = $(`finding-summary-${button.dataset.expandFinding}`);
        if (!summary) return;
        const expanded = summary.classList.toggle('expanded');
        button.setAttribute('aria-expanded', expanded ? 'true' : 'false');
        button.textContent = expanded ? '收起' : '更多';
        button.setAttribute('aria-label', expanded ? 'Collapse details' : 'View full details');
      }));
      $('issueReviewDetail').querySelectorAll('[data-finding-disposition]').forEach(select => {
        const sync = () => {
          const fields = $(`followup-${select.dataset.findingDisposition}`);
          const form = select.closest('.finding-handling-form');
          const active = select.value === 'follow-up';
          if (fields) fields.hidden = !active;
          if (form) form.classList.toggle('followup-active', active);
          const note = $(`note-${select.dataset.findingDisposition}`);
          if (note) note.placeholder = select.value === 'not-issue'
            ? '说明为什么不是问题，并提供可核验依据（必填）'
            : '说明修改内容、判断依据及测试结果（必填）';
        };
        select.addEventListener('change', sync); sync();
      });
      $('issueReviewDetail').querySelectorAll('[data-compose-adf]').forEach(button => button.addEventListener('click', () => openHandlingAdfComposer(button.dataset.composeAdf || '')));
      $('issueReviewDetail').querySelectorAll('[data-approve-handling]').forEach(button => button.addEventListener('click', () => approveWorkflowHandling(button.dataset.approveHandling || '', true, button)));
      $('issueReviewDetail').querySelectorAll('[data-override-handling]').forEach(button => button.addEventListener('click', () => managerOverrideHandling(button.dataset.overrideHandling || '', button)));
      $('issueReviewDetail').querySelectorAll('[data-edit-draft]').forEach(button => button.addEventListener('click', () => openDraftById(button.dataset.editDraft || '', drafts)));
      $('issueReviewDetail').querySelectorAll('[data-jump-severity], [data-jump-blocker]').forEach(button => button.addEventListener('click', () => {
        activateWorkflowTab('problems');
        const selector = button.dataset.jumpBlocker
          ? '.finding-card[data-finding-blocker="true"]'
          : `.finding-card[data-finding-severity="${button.dataset.jumpSeverity || ''}"]`;
        const target = $('issueReviewDetail').querySelector(selector);
        if (!target) return;
        target.scrollIntoView({behavior: 'smooth', block: 'center'});
        target.classList.add('finding-flash');
        window.setTimeout(() => target.classList.remove('finding-flash'), 1600);
      }));
      if ($('issueRescanBtn')) $('issueRescanBtn').addEventListener('click', () => {
        closeIssueReviews();
        $('jira').value = issue.jira_key;
        runReview();
      });
      if ($('issuePassBtn')) $('issuePassBtn').addEventListener('click', () => manualWorkflowPass(issue.jira_key));
      if ($('sendIssueDiscussionBtn')) $('sendIssueDiscussionBtn').addEventListener('click', () => sendWorkflowDiscussion(issue.jira_key, latest.id || ''));
    }

    function renderWorkflowFinding(finding, role, pendingBlockerIds = new Set()) {
      const handling = finding.handling || null;
      const severityClass = String(finding.severity || '').toLowerCase();
      const needsApproval = handling && handling.approval_status === 'pending';
      const isManager = role === 'manager';
      const isPendingBlocker = pendingBlockerIds.has(String(finding.id || ''));
      const details = finding.details && typeof finding.details === 'object' ? finding.details : {};
      const problemText = String(details.problem || details.detail || details.description || details.impact || finding.description || '').trim();
      const suggestionText = String(details.suggestion || details.recommendation || details.solution || details.workaround || '').trim();
      const hasEvidence = Boolean(problemText || suggestionText);
      const lineage = ({new:'New in this run', persisting:`Still present${finding.first_seen_run ? ` since Run ${finding.first_seen_run}` : ''}`, resolved:'Resolved after re-scan'})[String(finding.lineage_state || '').toLowerCase()] || statusLabel(finding.lineage_state);
      const fileStatus = finding.file_path || 'Architecture / No specific file';
      const scopeLabel = String(finding.scope_label || finding.application || 'Unmapped');
      return `<article class="finding-card" data-finding-severity="${escapeHtml(severityClass)}" data-finding-blocker="${isPendingBlocker ? 'true' : 'false'}"><div class="finding-head"><div class="finding-head-main"><span class="severity-chip ${escapeHtml(severityClass)}">${escapeHtml(finding.severity)}</span> <span class="status-chip">${escapeHtml(scopeLabel)}</span> <strong>#${escapeHtml(finding.report_index)} ${escapeHtml(finding.title)}</strong><div class="meta finding-context">${escapeHtml(fileStatus)} · ${escapeHtml(lineage)}</div>${hasEvidence ? `<div class="finding-evidence-preview" id="finding-summary-${finding.id}">${problemText ? `<div class="finding-evidence-line"><span class="finding-evidence-label">问题详情</span><span class="finding-evidence-text">${escapeHtml(problemText)}</span></div>` : ''}${suggestionText ? `<div class="finding-evidence-line"><span class="finding-evidence-label">处理建议</span><span class="finding-evidence-text">${escapeHtml(suggestionText)}</span></div>` : ''}</div><button class="finding-summary-toggle" type="button" data-expand-finding="${finding.id}" aria-label="View full details" aria-expanded="false" aria-controls="finding-summary-${finding.id}">更多</button>` : ''}</div>${handling ? `<span class="handling-chip">${escapeHtml(handling.disposition)} · ${escapeHtml(handling.approval_status)}</span>` : `<button class="finding-head-action" data-handle-finding="${finding.id}" type="button">Submit</button>`}</div>
        ${handling ? `<p>${escapeHtml(handling.note)}</p>${handling.manager_override ? `<div class="status">Manager Exception: ${escapeHtml(handling.override_reason)}</div>` : ''}<div class="finding-actions">${needsApproval && role !== 'developer' ? `<button class="secondary small-action" data-approve-handling="${handling.id}" type="button">Approve Not an issue</button>` : ''}${isManager && handling.disposition === 'follow-up' && !handling.manager_override ? `<button class="secondary small-action" data-override-handling="${handling.id}" type="button">Manager Exception</button>` : ''}</div>` : `<div class="finding-handling-form"><div class="finding-handling-primary"><label><span>处理结果 <span class="required-mark" aria-hidden="true">*</span></span><select id="disposition-${finding.id}" data-finding-disposition="${finding.id}" required><option value="fixed">已整改，Pass通过</option><option value="follow-up">不是阻碍，另报 Jira</option><option value="not-issue">不是问题，Pass通过</option></select></label><label><span id="note-label-${finding.id}">处理说明 <span class="required-mark" aria-hidden="true">*</span></span><textarea id="note-${finding.id}" aria-labelledby="note-label-${finding.id}" aria-describedby="error-${finding.id}" placeholder="说明修改内容、判断依据及测试结果" required></textarea></label><div id="error-${finding.id}" class="field-message" role="alert"></div></div><div class="finding-handling-secondary"><div id="followup-${finding.id}" class="followup-fields" hidden><div class="followup-fields-head"><label><span>Issue Summary <span class="required-mark" aria-hidden="true">*</span></span><textarea class="summary-input" id="jira-summary-${finding.id}" maxlength="255" rows="2" placeholder="概括待跟进问题，建议 20–50 个字符"></textarea></label></div><textarea id="jira-adf-${finding.id}" hidden>${escapeHtml(JSON.stringify(textToAdf('')))}</textarea><div class="followup-card-head"><div class="followup-adf-state"><strong>Issue Description <span class="required-mark" aria-hidden="true">*</span></strong><div id="jira-adf-preview-${finding.id}" class="followup-adf-preview">Not provided yet.</div><div id="jira-adf-status-${finding.id}" class="meta">Open the editor and add the follow-up details.</div></div><button class="secondary" data-compose-adf="${finding.id}" type="button">Edit issue</button></div></div></div></div>`}
      </article>`;
    }

    function renderDraftCard(draft) {
      return `<article class="draft-card"><div class="draft-head"><div><strong>${escapeHtml(draft.summary)}</strong><div class="meta">${escapeHtml(draft.jira_key)} · ${escapeHtml(statusLabel(draft.status))} · v${escapeHtml(draft.version)}</div></div><button class="secondary small-action" data-edit-draft="${draft.id}" type="button">View / Edit</button></div></article>`;
    }

    function textToAdf(value) {
      return {version: 1, type: 'doc', content: String(value || '').split(/\\r?\\n/).map(line => ({type: 'paragraph', content: line ? [{type: 'text', text: line}] : []}))};
    }

    function adfTextPreview(document, maxLength = 180) {
      const values = [];
      const visit = node => {
        if (!node || typeof node !== 'object') return;
        if (node.type === 'text' && node.text) values.push(String(node.text));
        if (Array.isArray(node.content)) node.content.forEach(visit);
      };
      visit(document);
      const text = values.join(' ').replace(/\\s+/g, ' ').trim() || 'No Issue Description content yet.';
      return text.length > maxLength ? `${text.slice(0, maxLength - 1)}…` : text;
    }

    async function submitWorkflowHandling(findingId, button = null) {
      const disposition = $(`disposition-${findingId}`).value;
      const noteElement = $(`note-${findingId}`);
      const summaryElement = $(`jira-summary-${findingId}`);
      const descriptionElement = $(`jira-adf-${findingId}`);
      const descriptionStatus = $(`jira-adf-status-${findingId}`);
      const errorElement = $(`error-${findingId}`);
      const note = noteElement.value.trim();
      [noteElement, summaryElement].filter(Boolean).forEach(element => element.removeAttribute('aria-invalid'));
      if (errorElement) { errorElement.className = 'field-message'; errorElement.textContent = ''; }
      const errors = [];
      if (!note) errors.push(disposition === 'not-issue' ? '“不是问题”必须填写可核验的理由。' : '处理说明为必填项。');
      if (disposition === 'follow-up' && !summaryElement.value.trim()) errors.push('Issue Summary 为必填项。');
      const descriptionPreview = disposition === 'follow-up' ? adfTextPreview(JSON.parse(descriptionElement.value), 1000) : '';
      const descriptionMissing = disposition === 'follow-up' && (descriptionPreview === 'No Issue Description content yet.' || descriptionPreview === 'Describe the follow-up requirement.');
      if (descriptionMissing) errors.push('Issue Description 为必填项，请点击 Edit issue 补充内容。');
      if (errors.length) {
        if (!note) noteElement.setAttribute('aria-invalid', 'true');
        if (disposition === 'follow-up' && !summaryElement.value.trim()) summaryElement.setAttribute('aria-invalid', 'true');
        if (descriptionMissing && descriptionStatus) { descriptionStatus.className = 'field-message error'; descriptionStatus.textContent = 'Issue Description is required.'; }
        if (errorElement) { errorElement.className = 'field-message error'; errorElement.textContent = errors.join(' '); }
        (note ? (!summaryElement.value.trim() ? summaryElement : $(`followup-${findingId}`).querySelector('[data-compose-adf]')) : noteElement).focus();
        return;
      }
      const payload = {finding_id: findingId, disposition, note};
      if (disposition === 'follow-up') {
        payload.jira_summary = summaryElement.value.trim();
        payload.jira_description_adf = JSON.parse($(`jira-adf-${findingId}`).value);
      }
      const requestId = (window.crypto && window.crypto.randomUUID) ? window.crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
      await singleFlight(`handling:${findingId}`, button, async () => {
        await fetchJson('/api/workflow/handling', {method: 'POST', headers: {'Content-Type':'application/json', 'Idempotency-Key': requestId}, body: JSON.stringify(payload)});
        await loadIssueReviewDetail(selectedIssueReview);
      });
    }

    async function approveWorkflowHandling(handlingId, approved, button = null) {
      const reason = window.prompt(approved ? 'Approval note' : 'Rejection reason', 'Verified by Leader') || '';
      if (!reason.trim()) return;
      await singleFlight(`approval:${handlingId}`, button, async () => {
        const requestId = (window.crypto && window.crypto.randomUUID) ? window.crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
        await fetchJson('/api/workflow/handling/approve', {method:'POST', headers:{'Content-Type':'application/json','Idempotency-Key':requestId}, body:JSON.stringify({handling_id:handlingId, approved, reason})});
        await loadIssueReviewDetail(selectedIssueReview);
      });
    }

    async function managerOverrideHandling(handlingId, button = null) {
      const reason = window.prompt('Manager exception reason (required)') || '';
      if (!reason.trim()) return;
      await singleFlight(`manager-override:${handlingId}`, button, async () => {
        const requestId = (window.crypto && window.crypto.randomUUID) ? window.crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
        await fetchJson('/api/workflow/handling/manager-override', {method:'POST', headers:{'Content-Type':'application/json','Idempotency-Key':requestId}, body:JSON.stringify({handling_id:handlingId, reason})});
        await loadIssueReviewDetail(selectedIssueReview);
      });
    }

    async function manualWorkflowPass(jiraKey) {
      const note = window.prompt('Manual Pass note', 'All configured blocking findings have been reviewed.') || '';
      if (!note.trim()) return;
      const requestId = (window.crypto && window.crypto.randomUUID) ? window.crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
      await singleFlight(`pass:${jiraKey}`, $('issuePassBtn'), async () => {
        await fetchJson('/api/workflow/pass', {method:'POST', headers:{'Content-Type':'application/json','Idempotency-Key':requestId}, body:JSON.stringify({jira_key:jiraKey, note})});
        await loadIssueReviewDetail(jiraKey);
        await loadIssueReviews();
      });
    }

    async function sendWorkflowDiscussion(jiraKey, runId) {
      const field = $('issueDiscussionInput');
      const error = $('issueDiscussionError');
      const message = field.value.trim();
      field.removeAttribute('aria-invalid');
      error.className = 'field-message';
      error.textContent = '';
      if (!message) {
        field.setAttribute('aria-invalid', 'true');
        error.className = 'field-message error';
        error.textContent = 'Message is required.';
        field.focus();
        return;
      }
      const requestId = (window.crypto && window.crypto.randomUUID) ? window.crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
      await singleFlight(`discussion:${jiraKey}`, $('sendIssueDiscussionBtn'), async () => {
        await fetchJson('/api/workflow/discussion', {method:'POST', headers:{'Content-Type':'application/json','Idempotency-Key':requestId}, body:JSON.stringify({jira_key:jiraKey, run_id:runId, message})});
        await loadIssueReviewDetail(jiraKey);
      });
    }

    async function openPendingJira() {
      $('draftEditorModal').hidden = false;
      $('draftEditorForm').hidden = true;
      $('pendingDraftList').innerHTML = '<div class="meta">Loading Pending Jira drafts...</div>';
      const data = await fetchJson('/api/jira-drafts');
      const drafts = data.drafts || [];
      $('pendingDraftList').innerHTML = drafts.length ? `<h3>待创建 · Pending Create</h3>${drafts.map(renderDraftCard).join('')}` : '<div class="markdown-preview empty">No pending Jira drafts.</div>';
      $('pendingDraftList').querySelectorAll('[data-edit-draft]').forEach(button => button.addEventListener('click', () => openDraftById(button.dataset.editDraft || '', drafts)));
    }

    function openDraftById(draftId, drafts) {
      const draft = drafts.find(item => item.id === draftId);
      if (!draft) return;
      currentJiraDraft = draft;
      $('draftEditorModal').hidden = false;
      $('pendingDraftList').innerHTML = '';
      $('draftEditorForm').hidden = false;
      $('draftSummary').value = draft.summary || '';
      $('draftSummary').removeAttribute('aria-invalid');
      $('draftBlockEditor').removeAttribute('aria-invalid');
      $('saveDraftBtn').textContent = 'Save draft';
      $('draftAdfSource').value = JSON.stringify(draft.description_adf || textToAdf(''), null, 2);
      $('draftEditorMeta').textContent = `${draft.jira_key} · Pending Create · version ${draft.version}`;
      showAdfEditMode();
    }

    function openHandlingAdfComposer(findingId) {
      const source = $(`jira-adf-${findingId}`);
      currentJiraDraft = {
        temporary: true,
        findingId,
        jira_key: selectedIssueReview,
        summary: $(`jira-summary-${findingId}`).value || '',
        description_adf: JSON.parse(source.value),
        attachments: [],
        version: 0
      };
      $('draftEditorModal').hidden = false;
      $('pendingDraftList').innerHTML = '';
      $('draftEditorForm').hidden = false;
      $('draftSummary').value = currentJiraDraft.summary;
      $('draftSummary').removeAttribute('aria-invalid');
      $('draftBlockEditor').removeAttribute('aria-invalid');
      $('saveDraftBtn').textContent = 'Apply description';
      $('draftAdfSource').value = JSON.stringify(currentJiraDraft.description_adf, null, 2);
      $('draftEditorMeta').textContent = `${selectedIssueReview} · New Jira follow-up · ADF composer`;
      showAdfEditMode();
    }

    function adfNodeTemplate(type) {
      const paragraph = text => ({type:'paragraph', content:text ? [{type:'text', text}] : []});
      if (type === 'paragraph') return paragraph('New paragraph');
      if (type === 'heading') return {type:'heading', attrs:{level:2}, content:[{type:'text',text:'Heading'}]};
      if (type === 'bulletList' || type === 'orderedList') return {type, content:[{type:'listItem',content:[paragraph('List item')]}]};
      if (type === 'table') return {type:'table',content:[{type:'tableRow',content:[{type:'tableHeader',content:[paragraph('Screenshot')]},{type:'tableHeader',content:[paragraph('Description')]},{type:'tableHeader',content:[paragraph('Additional remarks')]}]},{type:'tableRow',content:[{type:'tableCell',content:[paragraph('Add screenshot')]},{type:'tableCell',content:[paragraph('Describe the issue')]},{type:'tableCell',content:[paragraph('Add remarks')]}]}]};
      if (type === 'expand') return {type:'expand',attrs:{title:'Evidence and details'},content:[paragraph('Expand details'),adfNodeTemplate('table'),adfNodeTemplate('orderedList'),adfNodeTemplate('bulletList')]};
      if (type === 'nestedExpand') return {type:'table',content:[{type:'tableRow',content:[{type:'tableCell',content:[{type:'nestedExpand',attrs:{title:'Nested details'},content:[paragraph('Nested Expand content')]}]}]}]};
      return paragraph('');
    }

    function insertAdfNode(type) {
      try {
        const document = readAdfDocument();
        document.content = Array.isArray(document.content) ? document.content : [];
        document.content.push(adfNodeTemplate(type));
        $('draftAdfSource').value = JSON.stringify(document, null, 2);
        showAdfEditMode();
      } catch (error) {
        $('draftStatus').className = 'status error';
        $('draftStatus').textContent = `ADF JSON error: ${error.message}`;
      }
    }

    let draggedAdfBlockIndex = null;

    function readAdfDocument() {
      const source = $('draftAdfSource');
      const raw = String(source.value || '').trim();
      try {
        const document = JSON.parse(raw || '{"version":1,"type":"doc","content":[]}');
        if (!document || document.type !== 'doc') throw new Error('ADF root must be a doc');
        document.content = Array.isArray(document.content) ? document.content : [];
        return document;
      } catch (_error) {
        const recovered = textToAdf(raw);
        source.value = JSON.stringify(recovered, null, 2);
        $('draftStatus').className = 'status';
        $('draftStatus').textContent = 'Plain text was converted into an editable paragraph block.';
        return recovered;
      }
    }

    function writeAdfDocument(document) {
      $('draftAdfSource').value = JSON.stringify(document, null, 2);
    }

    function adfBlockText(node) {
      if (node && (node.type === 'bulletList' || node.type === 'orderedList')) {
        return (node.content || []).map(item => adfTextPreview(item, 500)).join('\\n');
      }
      return adfTextPreview(node, 1000);
    }

    function updateAdfBlockText(index, value) {
      const document = readAdfDocument();
      const node = document.content[index];
      if (!node) return;
      const paragraph = text => ({type: 'paragraph', content: text ? [{type: 'text', text}] : []});
      if (node.type === 'bulletList' || node.type === 'orderedList') {
        const lines = String(value || '').split(/\\r?\\n/).map(line => line.trim()).filter(Boolean);
        node.content = (lines.length ? lines : ['']).map(line => ({type: 'listItem', content: [paragraph(line)]}));
      } else {
        node.content = value ? [{type: 'text', text: String(value)}] : [];
      }
      writeAdfDocument(document);
      $('draftStatus').className = 'status';
      $('draftStatus').textContent = 'Unsaved changes.';
    }

    function renderAdfBlockEditor() {
      const editor = $('draftBlockEditor');
      const document = readAdfDocument();
      const labels = {paragraph:'Paragraph',heading:'Heading',bulletList:'Bullet list',orderedList:'Ordered list',table:'Table',expand:'Expand',mediaSingle:'Screenshot',panel:'Panel',codeBlock:'Code block',blockquote:'Quote'};
      if (!document.content.length) {
        editor.innerHTML = '<div class="adf-block-empty">Choose a component from the toolbar to start the Issue Description.</div>';
        return;
      }
      editor.innerHTML = document.content.map((node, index) => {
        const editable = ['paragraph', 'heading', 'bulletList', 'orderedList', 'codeBlock', 'blockquote'].includes(node.type);
        const content = editable
          ? `<textarea class="adf-block-input" data-adf-block-input="${index}" rows="${node.type.includes('List') ? 4 : 2}" placeholder="Enter ${escapeHtml(labels[node.type] || node.type)} content">${escapeHtml(adfBlockText(node))}</textarea>`
          : `<div class="adf-block-complex">${escapeHtml(adfTextPreview(node, 220))}</div>`;
        return `<article class="adf-block" draggable="true" data-adf-block-index="${index}">
          <button class="adf-block-grip" type="button" tabindex="-1" aria-label="Drag to reorder" title="Drag to reorder">&#x2637;</button>
          <div class="adf-block-body"><span class="adf-block-type">${escapeHtml(labels[node.type] || node.type)}</span>${content}</div>
          <button class="adf-block-delete" type="button" data-adf-block-delete="${index}" aria-label="Delete ${escapeHtml(labels[node.type] || node.type)}">Delete</button>
        </article>`;
      }).join('');
      editor.querySelectorAll('[data-adf-block-input]').forEach(input => input.addEventListener('input', () => updateAdfBlockText(Number(input.dataset.adfBlockInput), input.value)));
      editor.querySelectorAll('[data-adf-block-delete]').forEach(button => button.addEventListener('click', () => {
        const next = readAdfDocument();
        next.content.splice(Number(button.dataset.adfBlockDelete), 1);
        writeAdfDocument(next);
        renderAdfBlockEditor();
      }));
      editor.querySelectorAll('.adf-block').forEach(block => {
        block.addEventListener('dragstart', event => {
          draggedAdfBlockIndex = Number(block.dataset.adfBlockIndex);
          block.classList.add('dragging');
          if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';
        });
        block.addEventListener('dragend', () => {
          draggedAdfBlockIndex = null;
          editor.querySelectorAll('.adf-block').forEach(item => item.classList.remove('dragging', 'drop-target'));
        });
        block.addEventListener('dragover', event => { event.preventDefault(); block.classList.add('drop-target'); });
        block.addEventListener('dragleave', () => block.classList.remove('drop-target'));
        block.addEventListener('drop', event => {
          event.preventDefault();
          const targetIndex = Number(block.dataset.adfBlockIndex);
          if (draggedAdfBlockIndex === null || draggedAdfBlockIndex === targetIndex) return;
          const next = readAdfDocument();
          const [moved] = next.content.splice(draggedAdfBlockIndex, 1);
          const insertAt = Math.min(targetIndex, next.content.length);
          next.content.splice(insertAt, 0, moved);
          writeAdfDocument(next);
          renderAdfBlockEditor();
        });
      });
    }

    function showAdfEditMode() {
      if (mountAtlaskitAdf('edit')) {
        $('draftBlockEditor').hidden = true;
        $('draftAdfPreview').hidden = true;
        const engine = $('adfEditorEngine');
        if (engine) {
          engine.className = 'adf-editor-engine enhanced';
          engine.textContent = 'Atlaskit enhanced';
        }
        return;
      }
      unmountAtlaskitAdf();
      $('atlaskitAdfEditor').hidden = true;
      $('draftAdfSource').hidden = true;
      $('draftAdfPreview').hidden = true;
      $('draftBlockEditor').hidden = false;
      const engine = $('adfEditorEngine');
      if (engine) {
        engine.className = 'adf-editor-engine';
        engine.textContent = 'Built-in ADF editor';
      }
      renderAdfBlockEditor();
    }

    async function showAdfPreviewMode() {
      unmountAtlaskitAdf();
      $('atlaskitAdfEditor').hidden = true;
      $('draftAdfSource').hidden = true;
      $('draftBlockEditor').hidden = true;
      $('draftAdfPreview').hidden = false;
      const engine = $('adfEditorEngine');
      if (engine) {
        engine.className = 'adf-editor-engine';
        engine.textContent = 'Validated ADF preview';
      }
      await renderCurrentAdf();
    }

    async function renderCurrentAdf() {
      try {
        const document = readAdfDocument();
        const mediaUrls = {};
        for (const attachment of (currentJiraDraft && currentJiraDraft.attachments || [])) mediaUrls[attachment.id] = attachment.url;
        const data = await fetchJson('/api/adf/render', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({document, media_urls:mediaUrls})});
        $('draftAdfPreview').className = 'adf-preview';
        $('draftAdfPreview').innerHTML = data.html;
        $('draftStatus').textContent = 'ADF schema valid.';
      } catch (error) {
        $('draftAdfPreview').textContent = error.message;
        $('draftStatus').className = 'status error';
        $('draftStatus').textContent = error.message;
      }
    }

    function unmountAtlaskitAdf() {
      if (typeof atlaskitAdfUnmount === 'function') {
        try { atlaskitAdfUnmount(); } catch (_error) { /* Fall back without blocking the draft. */ }
      }
      atlaskitAdfUnmount = null;
    }

    function mountAtlaskitAdf(mode) {
      const host = $('atlaskitAdfEditor');
      if (!window.CodeReviewerADF || !host) return false;
      try {
        const document = JSON.parse($('draftAdfSource').value);
        unmountAtlaskitAdf();
        host.hidden = false;
        $('draftAdfSource').hidden = true;
        $('draftBlockEditor').hidden = true;
        $('draftAdfPreview').hidden = true;
        atlaskitAdfUnmount = window.CodeReviewerADF.mount(host, {
          value: document,
          mode,
          onChange: value => {
            $('draftAdfSource').value = JSON.stringify(value, null, 2);
            $('draftStatus').className = 'status';
            $('draftStatus').textContent = 'Unsaved Atlaskit editor changes.';
          }
        });
        return true;
      } catch (error) {
        unmountAtlaskitAdf();
        host.hidden = true;
        console.warn('Atlaskit ADF enhancement unavailable; using the built-in editor.', error);
        return false;
      }
    }

    async function saveCurrentDraft() {
      if (!currentJiraDraft) return;
      const document = readAdfDocument();
      const summaryField = $('draftSummary');
      const descriptionField = $('draftBlockEditor');
      const enhancedDescriptionField = $('atlaskitAdfEditor');
      const summary = summaryField.value.trim();
      summaryField.removeAttribute('aria-invalid');
      descriptionField.removeAttribute('aria-invalid');
      enhancedDescriptionField.removeAttribute('aria-invalid');
      if (!summary) {
        summaryField.setAttribute('aria-invalid', 'true');
        $('draftStatus').className = 'status error';
        $('draftStatus').textContent = 'Issue Summary is required.';
        summaryField.focus();
        return;
      }
      const descriptionPreview = adfTextPreview(document, 1000);
      if (descriptionPreview === 'No Issue Description content yet.' || descriptionPreview === 'Describe the follow-up requirement.') {
        $('draftStatus').className = 'status error';
        $('draftStatus').textContent = 'Issue Description is required. Add at least one text block.';
        showAdfEditMode();
        const activeDescriptionField = $('atlaskitAdfEditor').hidden ? descriptionField : enhancedDescriptionField;
        activeDescriptionField.setAttribute('aria-invalid', 'true');
        activeDescriptionField.focus();
        return;
      }
      if (currentJiraDraft.temporary) {
        const findingId = currentJiraDraft.findingId;
        $(`jira-summary-${findingId}`).value = $('draftSummary').value;
        $(`jira-adf-${findingId}`).value = JSON.stringify(document);
        $(`jira-adf-preview-${findingId}`).textContent = adfTextPreview(document);
        $(`jira-adf-status-${findingId}`).className = 'meta';
        $(`jira-adf-status-${findingId}`).textContent = 'ADF description ready.';
        $('draftEditorModal').hidden = true;
        unmountAtlaskitAdf();
        currentJiraDraft = null;
        return;
      }
      const data = await fetchJson('/api/jira-drafts/update', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({draft_id:currentJiraDraft.id, summary, description_adf:document, version:currentJiraDraft.version})});
      currentJiraDraft = data.draft;
      $('draftEditorMeta').textContent = `${currentJiraDraft.jira_key} · Pending Create · version ${currentJiraDraft.version}`;
      $('draftStatus').className = 'status ok';
      $('draftStatus').textContent = 'Draft saved.';
    }

    async function uploadDraftImage(file) {
      if (!currentJiraDraft || !file) return;
      if (currentJiraDraft.temporary) throw new Error('Save the Jira follow-up first, then attach screenshots from Pending Jira.');
      const contentBase64 = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || '').split(',',2)[1] || '');
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
      const dimensions = await new Promise(resolve => {
        const image = new Image();
        image.onload = () => resolve({width:image.naturalWidth || 1,height:image.naturalHeight || 1});
        image.onerror = () => resolve({width:1,height:1});
        image.src = URL.createObjectURL(file);
      });
      const data = await fetchJson('/api/jira-drafts/attachment', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({draft_id:currentJiraDraft.id,file_name:file.name,media_type:file.type,content_base64:contentBase64})});
      currentJiraDraft.attachments = currentJiraDraft.attachments || [];
      currentJiraDraft.attachments.push(data.attachment);
      const document = readAdfDocument();
      const mediaNode = {type:'mediaSingle',attrs:{layout:'center'},content:[{type:'media',attrs:{id:data.attachment.id,type:'file',alt:file.name,width:dimensions.width,height:dimensions.height}}]};
      const expand = (document.content || []).find(node => node.type === 'expand');
      if (expand) expand.content.push(mediaNode); else document.content.push(mediaNode);
      $('draftAdfSource').value = JSON.stringify(document,null,2);
      showAdfEditMode();
    }

    $('runBtn').addEventListener('click', () => singleFlight('run-review', $('runBtn'), runReview));
    if ($('sprint')) {
      let sprintSearchTimer = 0;
      $('sprint').addEventListener('focus', loadSprintSuggestions);
      $('sprint').addEventListener('input', () => {
        window.clearTimeout(sprintSearchTimer);
        if ($('sprintValidation')) { $('sprintValidation').className = 'field-message'; $('sprintValidation').textContent = ''; }
        sprintSearchTimer = window.setTimeout(loadSprintSuggestions, 250);
      });
    }
    if ($('runReleaseGateBtn')) $('runReleaseGateBtn').addEventListener('click', () => singleFlight('release-gate', $('runReleaseGateBtn'), runReleaseGate));
    function autoSizeReleaseGateUrl() {
      const field = $('releaseGateMrUrl');
      if (!field) return;
      const styles = window.getComputedStyle(field);
      const lineHeight = Number.parseFloat(styles.lineHeight) || 20;
      const verticalChrome = (Number.parseFloat(styles.paddingTop) || 0)
        + (Number.parseFloat(styles.paddingBottom) || 0)
        + (Number.parseFloat(styles.borderTopWidth) || 0)
        + (Number.parseFloat(styles.borderBottomWidth) || 0);
      const oneLineHeight = Math.ceil(lineHeight + verticalChrome);
      const twoLineHeight = Math.ceil((lineHeight * 2) + verticalChrome);
      field.style.height = `${oneLineHeight}px`;
      field.style.height = `${Math.min(Math.max(field.scrollHeight, oneLineHeight), twoLineHeight)}px`;
    }
    if ($('releaseGateMrUrl')) {
      $('releaseGateMrUrl').addEventListener('input', autoSizeReleaseGateUrl);
      window.addEventListener('resize', autoSizeReleaseGateUrl);
      autoSizeReleaseGateUrl();
    }
    if ($('releaseGateCandidateSelect')) $('releaseGateCandidateSelect').addEventListener('change', event => {
      if ($('releaseGateMrUrl')) $('releaseGateMrUrl').value = event.target.value || '';
      autoSizeReleaseGateUrl();
      $('releaseGateMrUrl')?.removeAttribute('aria-invalid');
      if ($('releaseGateStatus')) { $('releaseGateStatus').className = 'status release-gate-status'; $('releaseGateStatus').textContent = ''; }
    });
    $('issueReviewsBtn').addEventListener('click', openIssueReviews);
    $('releaseNotesBtn').addEventListener('click', openReleaseNotes);
    $('appHealthBtn').addEventListener('click', openHealthDetails);
    $('closeHealthDetailBtn').addEventListener('click', closeHealthDetails);
    $('doneHealthDetailBtn').addEventListener('click', closeHealthDetails);
    $('refreshHealthDetailBtn').addEventListener('click', () => singleFlight('refresh-health', $('refreshHealthDetailBtn'), () => loadAppHealth(true)).catch(error => {
      $('healthDetailMeta').textContent = error.message;
    }));
    $('healthDetailModal').addEventListener('click', event => { if (event.target === $('healthDetailModal')) closeHealthDetails(); });
    $('healthDetailModal').addEventListener('keydown', event => trapDialogFocus(event, $('healthDetailModal'), closeHealthDetails));
    $('changePasswordBtn').addEventListener('click', () => openChangePassword(false));
    $('cancelChangePasswordBtn').addEventListener('click', closeChangePassword);
    $('changePasswordForm').addEventListener('submit', event => {
      event.preventDefault();
      singleFlight('change-own-password', $('saveChangedPasswordBtn'), changeOwnPassword).catch(error => {
        $('changePasswordStatus').className = 'status error';
        $('changePasswordStatus').textContent = error.message;
        $('changePasswordValidation').hidden = false;
        $('changePasswordValidation').className = 'validation-summary error';
        $('changePasswordValidation').textContent = error.message;
        $('changePasswordValidation').focus();
      });
    });
    $('changePasswordModal').addEventListener('keydown', event => trapDialogFocus(event, $('changePasswordModal'), closeChangePassword));
    if ($('userManagementBtn')) $('userManagementBtn').addEventListener('click', () => openUserManagement().catch(error => {
      $('userAdminList').innerHTML = `<div class="status error">${escapeHtml(error.message)}</div>`;
    }));
    if ($('configurationBtn')) $('configurationBtn').addEventListener('click', () => openConfiguration().catch(error => {
      $('configurationStatus').className = 'status error';
      $('configurationStatus').textContent = error.message;
    }));
    $('closeConfigurationBtn').addEventListener('click', closeConfiguration);
    $('refreshConfigurationBtn').addEventListener('click', () => singleFlight('refresh-configuration', $('refreshConfigurationBtn'), loadConfiguration).catch(error => {
      $('configurationStatus').className = 'status error';
      $('configurationStatus').textContent = error.message;
    }));
    document.querySelectorAll('[data-configuration-view]').forEach(button => button.addEventListener('click', () => {
      configurationView = button.dataset.configurationView || 'application';
      configurationCreateVisible = false;
      renderConfiguration();
    }));
    $('addConfigurationProjectBtn').addEventListener('click', () => {
      configurationCreateVisible = !configurationCreateVisible;
      renderConfiguration();
      if (configurationCreateVisible) requestAnimationFrame(() => $('newConfigurationProjectModule')?.focus());
    });
    $('configurationSearch').addEventListener('input', () => {
      window.clearTimeout(configurationSearchTimer);
      configurationSearchTimer = window.setTimeout(renderConfiguration, 200);
    });
    $('configurationModal').addEventListener('click', event => { if (event.target === $('configurationModal')) closeConfiguration(); });
    $('configurationModal').addEventListener('keydown', event => trapDialogFocus(event, $('configurationModal'), closeConfiguration));
    $('closeUserAdminBtn').addEventListener('click', closeUserManagement);
    $('refreshUsersBtn').addEventListener('click', () => singleFlight('refresh-users', $('refreshUsersBtn'), loadManagedUsers).catch(error => {
      $('userAdminList').innerHTML = `<div class="status error">${escapeHtml(error.message)}</div>`;
    }));
    $('createUserBtn').addEventListener('click', () => showManagedUserForm(null));
    $('userAdminForm').addEventListener('submit', event => {
      event.preventDefault();
      singleFlight('save-managed-user', $('saveManagedUserBtn'), saveManagedUser).catch(error => {
        $('userAdminSaveStatus').className = 'status error';
        $('userAdminSaveStatus').textContent = error.message;
        $('userAdminValidation').hidden = false;
        $('userAdminValidation').className = 'validation-summary error';
        $('userAdminValidation').textContent = error.message;
        $('userAdminValidation').focus();
      });
    });
    $('userAdminSearch').addEventListener('input', () => {
      window.clearTimeout(userAdminSearchTimer);
      userAdminSearchTimer = window.setTimeout(renderManagedUsers, 250);
    });
    $('userAdminRoleFilter').addEventListener('change', renderManagedUsers);
    $('userAdminStatusFilter').addEventListener('change', renderManagedUsers);
    $('managedRole').addEventListener('change', updateManagedResponsibleMode);
    $('managedResponsibleSearch').addEventListener('input', event => {
      const query = String(event.target.value || '').trim().toLowerCase();
      $('managedResponsibleOptions').querySelectorAll('.user-responsible-option').forEach(option => {
        option.hidden = Boolean(query) && !option.textContent.toLowerCase().includes(query);
      });
    });
    $('addManagedResponsibleBtn').addEventListener('click', addManagedResponsible);
    $('managedResponsibleAdd').addEventListener('keydown', event => {
      if (event.key === 'Enter') {
        event.preventDefault();
        addManagedResponsible();
      }
    });
    $('resetManagedPasswordBtn').addEventListener('click', openManagedPasswordReset);
    $('cancelUserResetBtn').addEventListener('click', closeManagedPasswordReset);
    $('userResetConfirmInput').addEventListener('input', () => {
      $('userResetConfirmError').textContent = '';
      $('confirmUserResetBtn').disabled = $('userResetConfirmInput').value.trim() !== $('managedUsername').value.trim();
    });
    $('confirmUserResetBtn').addEventListener('click', () => singleFlight('reset-managed-password', $('confirmUserResetBtn'), resetManagedPassword).catch(error => {
      $('userResetConfirmError').className = 'field-message error';
      $('userResetConfirmError').textContent = error.message;
    }));
    $('copyTemporaryPasswordBtn').addEventListener('click', () => singleFlight('copy-temporary-password', $('copyTemporaryPasswordBtn'), copyTemporaryPassword).catch(error => {
      $('temporaryPasswordStatus').className = 'status error';
      $('temporaryPasswordStatus').textContent = error.message;
    }));
    $('closeTemporaryPasswordBtn').addEventListener('click', closeTemporaryPassword);
    $('userAdminModal').addEventListener('click', event => { if (event.target === $('userAdminModal')) closeUserManagement(); });
    $('userAdminModal').addEventListener('keydown', event => trapDialogFocus(event, $('userAdminModal'), closeUserManagement));
    $('userResetConfirmModal').addEventListener('keydown', event => trapDialogFocus(event, $('userResetConfirmModal'), closeManagedPasswordReset));
    $('temporaryPasswordModal').addEventListener('keydown', event => {
      if (event.key === 'Escape') event.preventDefault();
      else trapDialogFocus(event, $('temporaryPasswordModal'), closeTemporaryPassword);
    });
    $('closeReleaseNotesBtn').addEventListener('click', closeReleaseNotes);
    $('releaseNotesModal').addEventListener('click', event => { if (event.target === $('releaseNotesModal')) closeReleaseNotes(); });
    $('releaseNotesModal').addEventListener('keydown', handleReleaseNotesKeydown);
    $('pendingJiraBtn').addEventListener('click', () => openPendingJira().catch(error => {
      $('pendingDraftList').innerHTML = `<div class="status error">${escapeHtml(error.message)}</div>`;
    }));
    $('closeIssueReviewsBtn').addEventListener('click', closeIssueReviews);
    $('refreshIssueReviewsBtn').addEventListener('click', loadIssueReviews);
    $('issueOverviewTab').addEventListener('click', () => setIssueReviewView('overview'));
    $('issueListTab').addEventListener('click', () => setIssueReviewView('issues'));
    $('issueReviewSearch').addEventListener('input', () => {
      issueReviewSprintFilter = '';
      issueReviewApplicationFilter = '';
      $('issueReviewScope').hidden = true;
      $('issueReviewScope').textContent = '';
      renderIssueReviews();
    });
    $('issueReviewModal').addEventListener('click', event => { if (event.target === $('issueReviewModal')) closeIssueReviews(); });
    $('closeDraftEditorBtn').addEventListener('click', () => {
      unmountAtlaskitAdf();
      $('draftEditorModal').hidden = true;
      currentJiraDraft = null;
    });
    $('saveDraftBtn').addEventListener('click', () => singleFlight('save-draft', $('saveDraftBtn'), saveCurrentDraft).catch(error => {
      $('draftStatus').className = 'status error'; $('draftStatus').textContent = error.message;
    }));
    $('adfPreviewModeBtn').addEventListener('click', () => showAdfPreviewMode());
    $('adfEditModeBtn').addEventListener('click', showAdfEditMode);
    document.querySelectorAll('[data-adf-insert]').forEach(button => button.addEventListener('click', () => insertAdfNode(button.dataset.adfInsert || 'paragraph')));
    $('draftImageInput').addEventListener('change', event => uploadDraftImage((event.target.files || [])[0]).catch(error => {
      $('draftStatus').className = 'status error'; $('draftStatus').textContent = error.message;
    }));
    $('runFormToggle').addEventListener('click', toggleRunForm);
    $('previewOpenBtn').addEventListener('click', openPreviewModal);
    $('refreshBtn').addEventListener('click', loadReports);
    $('coverageBtn').addEventListener('click', openCoverage);
    $('coverageCloseBtn').addEventListener('click', closeCoverage);
    $('coverageScanBtn').addEventListener('click', () => {
      singleFlight('coverage-scan', $('coverageScanBtn'), scanCoverage).finally(syncCoverageControls);
    });
    document.querySelectorAll('[data-coverage-view]').forEach(button => {
      button.addEventListener('click', () => setCoverageView(button.dataset.coverageView || 'overview'));
    });
    $('coverageModal').addEventListener('click', (event) => {
      if (event.target === $('coverageModal')) closeCoverage();
    });
    $('previewMaximizeBtn').addEventListener('click', openPreviewModal);
    $('previewCompareBtn').addEventListener('click', () => compareReport(currentPreviewReportPath, currentPreviewOutputDir || currentOutputDir || ''));
    $('previewRestoreBtn').addEventListener('click', closePreviewModal);
    $('previewPrevBtn').addEventListener('click', () => navigatePreviewReport(-1));
    $('previewNextBtn').addEventListener('click', () => navigatePreviewReport(1));
    $('threadPrevBtn').addEventListener('click', () => navigateThreadReport(-1));
    $('threadNextBtn').addEventListener('click', () => navigateThreadReport(1));
    $('previewModal').addEventListener('click', (event) => {
      if (event.target === $('previewModal')) closePreviewModal();
    });
    $('threadModal').addEventListener('click', (event) => {
      if (event.target === $('threadModal')) closeThreadModal();
    });
    $('refreshDownloadsBtn').addEventListener('click', async () => {
      await loadResponsibleDownloads();
      await loadReports();
    });
    for (const button of document.querySelectorAll('.history-tab')) {
      button.addEventListener('click', () => setHistoryTab(button.dataset.historyTab || 'responsibles'));
    }
    for (const button of document.querySelectorAll('.thread-tab')) {
      button.addEventListener('click', () => setThreadTab(button.dataset.threadTab || 'discussion'));
    }
    if ($('reportSearch')) $('reportSearch').addEventListener('input', () => {
      clearTimeout(reportSearchTimer);
      reportSearchTimer = setTimeout(async () => {
        await loadResponsibleDownloads();
        await loadReports();
      }, 250);
    });
    if ($('reportDays')) $('reportDays').addEventListener('change', async () => {
      await loadResponsibleDownloads();
      await loadReports();
    });
    $('projectSearch').addEventListener('input', filterProjects);
    $('sendThreadMessageBtn').addEventListener('click', () => {
      singleFlight('thread-message', $('sendThreadMessageBtn'), sendThreadMessage).catch((error) => {
        $('threadMessages').textContent = error.message;
      });
    });
    $('generateFollowupsBtn').addEventListener('click', () => {
      singleFlight('thread-followups', $('generateFollowupsBtn'), generateFollowups).catch((error) => {
        $('followupDraft').textContent = error.message;
      });
    });
    $('copyFollowupDraftBtn').addEventListener('click', () => {
      singleFlight('copy-followup-draft', $('copyFollowupDraftBtn'), copyFollowupDraft).catch((error) => {
        const button = $('copyFollowupDraftBtn');
        button.classList.remove('copied');
        button.classList.add('copy-error');
        button.setAttribute('aria-label', error.message.includes('No follow-up') ? 'No follow-up draft to copy' : 'Copy failed');
        button.title = error.message.includes('No follow-up') ? 'Nothing to copy' : 'Copy failed';
        $('copyFollowupStatus').textContent = button.title;
        window.setTimeout(() => {
          button.classList.remove('copy-error');
          button.setAttribute('aria-label', 'Copy follow-up draft');
          button.title = 'Copy follow-up draft';
        }, 1600);
      });
    });
    $('sendAiChatBtn').addEventListener('click', () => {
      singleFlight('ai-chat', $('sendAiChatBtn'), sendAiChat).catch((error) => appendAiChatMessage('assistant', error.message));
    });
    $('openIssueReviewFromReportBtn').addEventListener('click', openIssueReviewFromReport);
    $('closeThreadBtn').addEventListener('click', () => {
      closeThreadModal();
    });
    if ($('networkBtn')) $('networkBtn').addEventListener('click', loadNetwork);
    async function init() {
      initSidebars();
      initInformationHints();
      const me = await loadMe();
      loadAppHealth(false).catch(() => {
        $('appHealthBtn').dataset.status = 'unhealthy';
        $('appHealthLabel').textContent = 'Unavailable';
      });
      if (me.must_change_password) {
        openChangePassword(true);
        return;
      }
      await loadRuntimeConfig();
      await loadProjects();
      resetProgress();
      await loadReviewJobs();
      if (me.is_admin) await loadTrace();
      await loadResponsibleDownloads();
      await loadReports();
      if (me.is_admin) await loadNetwork();
    }
    init().catch((error) => {
      const message = error && error.message ? error.message : String(error);
      $('status').className = 'status error';
      $('status').textContent = `Web init failed: ${message}`;
      if ($('responsibles')) $('responsibles').textContent = `Failed to load responsible folders: ${message}`;
      if ($('reports')) $('reports').textContent = `Failed to load reports: ${message}`;
      console.error(error);
    });
  </script>
</body>
</html>""".replace("__ADF_ASSET__", adf_asset).replace("__ADMIN_TRACE_SECTION__", admin_trace).replace("__USER_MANAGEMENT_BUTTON__", user_management_button).replace("__CURRENT_USER__", html.escape(user or "-")).replace("__CURRENT_ROLE__", html.escape(_web_user_role(user).title())).replace("__APP_VERSION__", html.escape(app_version())).replace("__INITIAL_PROJECTS__", initial_projects).replace("__SPRINT_FIELD__", admin_fields).replace("__RELEASE_GATE_PANEL__", release_gate_panel).replace("__INPUT_GRID_CLASS__", input_grid_class).replace("__RUN_HINT__", html.escape(run_hint))
