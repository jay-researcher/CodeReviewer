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
from datetime import datetime
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
    jira_spaces,
    llm_config,
    load_projects,
    report_min_severity,
    report_output_dir,
    sprint_prefixes,
)
from .local_workspaces import git_tools_project_entries
from .network import check_network_dict
from .report import handling_result_filename, render_handling_result_template_from_markdown
from .review_service import (
    ReviewCancelled,
    jira_issue_review_fingerprint,
    review_fingerprint_from_merge_requests,
    review_jira_filter_merge_requests,
    review_jira_issue_merge_requests,
    review_sprint_merge_requests,
    run_review_from_payload,
)
from .storage import load_review_history
from .adf import ADFValidationError, empty_adf, render_adf_html, validate_adf
from .workflow_store import blocking_severities, report_fingerprint, workflow_store


WEB_USERS_FILE = Path(os.getenv("WEB_USERS_FILE", str(DATA_DIR / "web_users.json"))).expanduser()
WEB_IP_WHITELIST_FILE = Path(os.getenv("WEB_IP_WHITELIST_FILE", str(DATA_DIR / "web_ip_whitelist.txt"))).expanduser()
WEB_THREADS_DIR = Path(os.getenv("WEB_THREADS_DIR", str(DATA_DIR / "web_threads"))).expanduser()
WEB_STATIC_DIR = ROOT_DIR / "code_reviewer" / "static"
WEB_SESSION_COOKIE = "code_reviewer_session"
WEB_SESSIONS: dict[str, dict[str, object]] = {}
ROBOT_CHALLENGES: dict[str, dict[str, object]] = {}
WEB_REVIEW_JOBS: dict[str, dict[str, object]] = {}
WEB_REVIEW_JOBS_LOCK = threading.Lock()
PASSWORD_SYMBOLS = "!@#$%&*?"
PASSWORD_HASH_ITERATIONS = 310_000
SESSION_TTL_SECONDS = 8 * 60 * 60
CHALLENGE_TTL_SECONDS = 10 * 60
JOB_TTL_SECONDS = 2 * 60 * 60
WEB_ROLES = {"manager", "auditor", "developer"}


class CodeReviewerHandler(BaseHTTPRequestHandler):
    server_version = "CodeReviewer/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if not self._client_ip_allowed():
            self._send_ip_forbidden(parsed.path)
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
            self._send_json(
                {
                    "username": user,
                    "role": role,
                    "permissions": _web_user_permissions(user),
                    "is_admin": role == "manager",
                    "version": app_version(),
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
        if parsed.path == "/api/login":
            self._handle_login()
            return
        if not self._current_user():
            self._send_json({"ok": False, "error": "Authentication required"}, status=HTTPStatus.UNAUTHORIZED)
            return
        if parsed.path == "/api/reviews":
            self._handle_review()
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
        if parsed.path == "/api/report-thread/handling":
            self._handle_thread_handling()
            return
        if parsed.path == "/api/report-thread/ai-chat":
            self._handle_thread_ai_chat()
            return
        if parsed.path == "/api/report-thread/teams-send":
            self._handle_thread_teams_send()
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
            )
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
            self._send_json({"ok": True})
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
            self._send_json({"ok": True})
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
            result = workflow_store().manual_pass(jira_key, user, _web_user_role(user), _text(payload.get("note")))
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
            current = _text(payload.get("current_password")).strip()
            new_password = _text(payload.get("new_password")).strip()
            if len(new_password) < 12 or not _strong_password(new_password):
                raise ValueError("New password must contain at least 12 characters with upper/lower case, number, and symbol.")
            users = _load_web_users()
            canonical, record = _find_web_user(users, user)
            if not record:
                raise ValueError("User was not found.")
            encoded = str(record.get("password_hash") or "")
            legacy = str(record.get("password") or "")
            if not (verify_web_password(current, encoded) if encoded else hmac.compare_digest(current, legacy)):
                raise ValueError("Current password is incorrect.")
            record["password_hash"] = hash_web_password(new_password)
            record.pop("password", None)
            record["must_change_password"] = False
            record["password_changed_at"] = datetime.now().isoformat(timespec="seconds")
            WEB_USERS_FILE.write_text(json.dumps({"users": users}, ensure_ascii=False, indent=2), encoding="utf-8")
            self._send_json({"ok": True, "username": canonical})
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
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            user = self._current_user()
            permissions = _web_user_permissions(user)
            if not permissions["run_issue_review"]:
                self._send_json({"ok": False, "error": "Your role cannot start code review jobs."}, status=HTTPStatus.FORBIDDEN)
                return
            payload["web_report_owner"] = user
            if _text(payload.get("sprint")).strip() and not permissions["run_sprint_review"]:
                self._send_json({"ok": False, "error": "Sprint review is only available to Manager users."}, status=HTTPStatus.FORBIDDEN)
                return
            if _text(payload.get("jira_filter")).strip() and not permissions["run_sprint_review"]:
                self._send_json({"ok": False, "error": "Jira filter review is only available to Manager users."}, status=HTTPStatus.FORBIDDEN)
                return
            jira_key = _text(payload.get("jira_key")).strip().upper()
            is_single_jira_review = jira_key and not _text(payload.get("sprint")).strip() and not _text(payload.get("jira_filter")).strip()
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
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            username = _text(payload.get("username")).strip()
            password = _text(payload.get("password")).strip()
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
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _current_user(self) -> str:
        token = _cookie_value(self.headers.get("Cookie", ""), WEB_SESSION_COOKIE)
        session = WEB_SESSIONS.get(token)
        if not session:
            return ""
        if int(time.time()) > int(session.get("expires_at", 0)):
            WEB_SESSIONS.pop(token, None)
            return ""
        return str(session.get("username") or "")

    def _logout(self) -> None:
        token = _cookie_value(self.headers.get("Cookie", ""), WEB_SESSION_COOKIE)
        if token:
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
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            user = self._current_user()
            base, target = _resolve_report_for_user(_text(payload.get("report")), _text(payload.get("output_dir")), user)
            message = _text(payload.get("message")).strip()
            kind = _text(payload.get("kind")).strip() or "comment"
            if not message:
                self._send_json({"ok": False, "error": "Message is required."}, status=HTTPStatus.BAD_REQUEST)
                return
            if kind == "manual-pass":
                if not _web_user_permissions(user)["manual_pass"]:
                    self._send_json({"ok": False, "error": "Only Auditor or Manager users can record Review Pass."}, status=HTTPStatus.FORBIDDEN)
                    return
                readiness = _manual_pass_readiness(target, _load_report_thread(base, target))
                if not readiness["ready"]:
                    self._send_json(
                        {"ok": False, "error": readiness["message"], "pass_readiness": readiness},
                        status=HTTPStatus.CONFLICT,
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

    def _handle_thread_handling(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            user = self._current_user()
            if not _web_user_permissions(user)["submit_handling"]:
                self._send_json({"ok": False, "error": "Your role cannot submit handling results."}, status=HTTPStatus.FORBIDDEN)
                return
            base, target = _resolve_report_for_user(_text(payload.get("report")), _text(payload.get("output_dir")), user)
            finding_index = _text(payload.get("finding_index")).strip()
            disposition = _text(payload.get("disposition")).strip().lower()
            note = _text(payload.get("note")).strip()
            _record_finding_handling(base, target, user, finding_index, disposition, note)
            self._send_json({"ok": True, **_report_thread_payload(base, target, user)})
        except PermissionError:
            self._send_json({"ok": False, "error": "Forbidden"}, status=HTTPStatus.FORBIDDEN)
        except FileNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc) or "Report not found"}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _handle_thread_followups(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
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
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
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

    def _handle_thread_teams_send(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            user = self._current_user()
            if not _web_user_permissions(user)["teams_delivery"]:
                self._send_json({"ok": False, "error": "Teams delivery is available to Auditor and Manager users."}, status=HTTPStatus.FORBIDDEN)
                return
            base, target = _resolve_report_for_user(_text(payload.get("report")), _text(payload.get("output_dir")), user)
            link = _text(payload.get("link")).strip()
            channel = _text(payload.get("channel")).strip()
            mention = _text(payload.get("mention")).strip()
            mode = _text(payload.get("mode")).strip() or "markdown"
            thread = _load_report_thread(base, target)
            message = _build_teams_message(target.name, link, channel, mention, mode)
            result = _send_teams_message_if_configured(message)
            if result.get("sent"):
                _append_report_thread_message(base, target, user, "teams", f"Teams message sent. Channel: {channel or '-'}; Mention: {mention or '-'}")
            else:
                _append_report_thread_message(base, target, user, "teams-draft", f"Teams message prepared but not sent. {result.get('message') or ''}".strip())
            self._send_json({"ok": True, "teams_message": message, **result, "thread": _report_thread_payload(base, target, user)})
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
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


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
    _update_job(job_id, status="running", started_at=time.time())
    _append_job_event(job_id, "started", "Review job started.")
    try:
        result = run_review_from_payload(payload, progress=lambda event: _job_progress(job_id, event))
        if _job_cancel_requested(job_id):
            raise ReviewCancelled("Review job stopped by user.")
        _wait_while_job_paused(job_id)
        if _job_cancel_requested(job_id):
            raise ReviewCancelled("Review job stopped by user.")
        _update_job(job_id, status="done", result=result, finished_at=time.time())
        _append_job_event(job_id, "completed", str(result.get("conclusion") or "Review completed."), {"result": _compact_job_result(result)})
    except ReviewCancelled as exc:
        _update_job(job_id, status="canceled", error=str(exc), finished_at=time.time())
        _append_job_event(job_id, "canceled", str(exc) or "Review job stopped by user.")
    except Exception as exc:
        _update_job(job_id, status="failed", error=str(exc), finished_at=time.time())
        _append_job_event(job_id, "failed", str(exc), {"traceback": traceback.format_exc(limit=8)})


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
            job["status"] = "running"
        job["updated_at"] = time.time()
    _append_job_event(job_id, "resume-requested", "Resume requested.")
    return {"ok": True, "status": "running", "job": review_job_snapshot(job_id, user)}


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
    allowed = {"mode", "jira_key", "jira_filter", "sprint", "output_dir", "report_language", "report_min_severity", "speed", "state", "limit", "rerun_confirmed", "retry_of", "web_report_owner"}
    return {key: payload.get(key) for key in sorted(allowed) if key in payload}


def _compact_job_result(result: dict[str, object]) -> dict[str, object]:
    return {
        "conclusion": result.get("conclusion"),
        "finding_count": result.get("finding_count"),
        "severity_counts": result.get("severity_counts"),
        "report": result.get("report"),
        "report_name": result.get("report_name"),
        "mode": result.get("mode"),
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
            if not _can_access_report(directory, path, user):
                continue
            stat = path.stat()
            if cutoff and stat.st_mtime < cutoff:
                continue
            relative = path.relative_to(directory).as_posix()
            responsible = path.relative_to(directory).parts[0] if len(path.relative_to(directory).parts) > 1 else ""
            if not _report_matches_search(relative, path.name, responsible, search):
                continue
            reports.append(
                {
                    "name": path.name,
                    "relative_path": relative,
                    "responsible": responsible,
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


def build_review_coverage(user: str, jira_keys: str = "", sprint: str = "", jira_filter: str = "") -> dict[str, object]:
    permissions = _web_user_permissions(user)
    requested_keys = _jira_keys_from_text(jira_keys)
    if (sprint or jira_filter) and not permissions["scan_coverage"]:
        raise PermissionError("Your role cannot scan Sprint or Jira Filter coverage.")

    discovery: dict[str, Any] = {}
    direct_issue_details: list[dict[str, str]] = []
    if jira_filter:
        discovery = review_jira_filter_merge_requests(filter_id=jira_filter, list_only=True, report_owner=user)
    elif sprint:
        discovery = review_sprint_merge_requests(sprint=sprint, list_only=True, report_owner=user)
    elif requested_keys:
        discovery = {
            "items": [],
            "issues_without_mrs": [],
            "discovery_errors": [],
        }
        for key in requested_keys:
            try:
                result = review_jira_issue_merge_requests(key, list_only=True, report_owner=user)
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

    issue_rows: dict[str, dict[str, object]] = {
        key: {"jira_key": key, "summary": "", "jira_status": "", "responsibles": set(), "mr_count": 0}
        for key in requested_keys
    }
    for detail in direct_issue_details:
        key = detail["jira_key"]
        row = issue_rows.setdefault(
            key,
            {"jira_key": key, "summary": "", "jira_status": "", "responsibles": set(), "mr_count": 0},
        )
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
        row = issue_rows.setdefault(
            key,
            {"jira_key": key, "summary": "", "jira_status": "", "responsibles": set(), "mr_count": 0},
        )
        row["summary"] = str(item.get("jira_summary") or row.get("summary") or "")
        row["jira_status"] = str(item.get("jira_status") or row.get("jira_status") or "")
        if responsible:
            row["responsibles"].update(_split_people(responsible))
        row["mr_count"] = int(row.get("mr_count") or 0) + 1

    if _web_user_role(user) == "manager":
        for item in discovery.get("issues_without_mrs") or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("jira_key") or "").upper()
            if key:
                issue_rows.setdefault(
                    key,
                    {
                        "jira_key": key,
                        "summary": str(item.get("summary") or ""),
                        "jira_status": "",
                        "responsibles": set(),
                        "mr_count": 0,
                    },
                )

    scoped_keys = set(issue_rows)
    reports = list_reports(user=user, days=0 if scoped_keys else _default_report_history_days())
    reports_by_issue: dict[str, list[dict[str, object]]] = {}
    for report in reports:
        key = _jira_key_from_report_name(str(report.get("relative_path") or report.get("name") or ""))
        if not key or (scoped_keys and key not in scoped_keys):
            continue
        reports_by_issue.setdefault(key, []).append(report)
        issue_rows.setdefault(key, {"jira_key": key, "summary": "", "jira_status": "", "responsibles": set(), "mr_count": 0})

    jobs_by_issue: dict[str, list[dict[str, object]]] = {}
    for job in list_review_job_snapshots(user, limit=500):
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        key = str(payload.get("jira_key") or "").upper()
        if not key or (scoped_keys and key not in scoped_keys):
            continue
        jobs_by_issue.setdefault(key, []).append(job)
        issue_rows.setdefault(key, {"jira_key": key, "summary": "", "jira_status": "", "responsibles": set(), "mr_count": 0})

    rows: list[dict[str, object]] = []
    for key, row in issue_rows.items():
        report_states = [_coverage_report_state(item) for item in reports_by_issue.get(key, [])]
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
        rows.append(
            {
                **{name: value for name, value in row.items() if name != "responsibles"},
                "responsible": "+".join(sorted(row.get("responsibles") or [], key=str.lower)) or "-",
                "workflow_status": workflow_status,
                "report_count": len(report_states),
                "running_jobs": len(active_jobs),
                "finding_count": sum(int(item.get("finding_count") or 0) for item in report_states),
                "handled_count": sum(int(item.get("handled_count") or 0) for item in report_states),
                "blocking_pending": sum(int(item.get("blocking_pending") or 0) for item in report_states),
                "latest_report": str((reports_by_issue.get(key) or [{}])[0].get("relative_path") or ""),
                "latest_output_dir": str((reports_by_issue.get(key) or [{}])[0].get("output_dir") or ""),
            }
        )

    order = {"running": 0, "failed": 1, "missing": 2, "pending": 3, "ready": 4, "passed": 5}
    rows.sort(key=lambda item: (order.get(str(item.get("workflow_status") or ""), 9), str(item.get("jira_key") or "")))
    counts: dict[str, int] = {name: 0 for name in order}
    for row in rows:
        status = str(row.get("workflow_status") or "")
        counts[status] = counts.get(status, 0) + 1
    return {
        "role": _web_user_role(user),
        "scope": {"jira": requested_keys, "sprint": sprint, "jira_filter": jira_filter},
        "counts": counts,
        "issues": rows,
        "discovery_errors": discovery.get("discovery_errors") or [],
    }


def _jira_keys_from_text(value: str) -> list[str]:
    return list(dict.fromkeys(match.upper() for match in re.findall(r"\b[A-Z][A-Z0-9]+-\d+\b", (value or "").upper())))


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
    }


def _aggregate_coverage_report_status(states: list[dict[str, object]]) -> str:
    values = [str(item.get("status") or "") for item in states]
    if "pending" in values:
        return "pending"
    if values and all(value == "passed" for value in values):
        return "passed"
    return "ready"


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
    return {
        **data,
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
    for match in pattern.finditer(report_text or ""):
        findings.append({"index": match.group(1), "severity": match.group(2).strip(), "title": match.group(3).strip()})
    return findings


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


def _build_teams_message(report_name: str, link: str, channel: str, mention: str, mode: str) -> str:
    mention_text = mention.strip()
    if mention_text and not mention_text.startswith("@"):
        mention_text = f"@{mention_text}"
    target = f" for {channel}" if channel else ""
    delivery = "responsible ZIP" if mode == "zip" else "Markdown report"
    lines = [
        f"CodeReviewer {delivery} ready{target}: {report_name}",
    ]
    if mention_text:
        lines.append(f"Responsible: {mention_text}")
    if link:
        lines.append(f"Link: {link}")
    lines.append("Please reply with handling result: fixed/pass, follow-up Jira, or clarified-not-issue.")
    return "\n".join(lines)


def _send_teams_message_if_configured(message: str) -> dict[str, object]:
    webhook = os.getenv("TEAMS_BOT_WEBHOOK_URL", "").strip()
    if not webhook:
        return {
            "sent": False,
            "message": "Teams Bot is not configured. Set TEAMS_BOT_WEBHOOK_URL on the Web server to enable sending.",
        }
    payload = json.dumps({"text": message}, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(webhook, data=payload, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    cafile = os.getenv("TEAMS_BOT_CA_BUNDLE", "").strip()
    context = ssl.create_default_context(cafile=cafile) if cafile else None
    try:
        with urlrequest.urlopen(req, timeout=15, context=context) as response:
            body = response.read(512).decode("utf-8", errors="ignore")
            return {"sent": True, "status": response.status, "message": body or "Teams webhook accepted the message."}
    except Exception as exc:
        return {"sent": False, "message": f"Teams webhook send failed: {exc}"}


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
            if not _can_access_responsible(responsible, user):
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


def _can_access_report(base: Path, report_path: Path, user: str) -> bool:
    if not user:
        return False
    try:
        parts = report_path.resolve().relative_to(base.resolve()).parts
    except ValueError:
        return False
    responsible = parts[0] if len(parts) > 1 else "__root__"
    return _can_access_responsible(responsible, user)


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
        **(stored if isinstance(stored, dict) else {}),
        **(configured if isinstance(configured, dict) else {}),
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
        "scan_coverage": role in {"manager", "auditor"},
        "submit_handling": role in {"manager", "auditor", "developer"},
        "ai_chat": role in {"manager", "auditor"},
        "manual_pass": role in {"manager", "auditor"},
        "teams_delivery": role in {"manager", "auditor"},
        "view_all": role == "manager",
    }


def _issue_access_allowed(user: str, jira_key: str) -> bool:
    if _web_user_permissions(user)["view_all"]:
        return True
    detail = workflow_store().issue_detail((jira_key or "").strip().upper())
    if not detail:
        return False
    issue = detail.get("issue") if isinstance(detail.get("issue"), dict) else {}
    owners = {item.lower() for item in _split_people(str(issue.get("responsible") or ""))}
    allowed = {item.lower() for item in _web_user_responsibles(user)}
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
        responsible = str(metadata.get("responsible") or metadata.get("web_report_owner") or report_path.parent.name)
        findings = _extract_report_findings(report_path.read_text(encoding="utf-8", errors="ignore"))
        store.register_run(
            jira_key=jira_key,
            report_path=str(report_path),
            findings=findings,
            summary=summary,
            responsible=responsible,
            conclusion=str(entry.get("conclusion") or ""),
            created_at=str(entry.get("reviewed_at") or ""),
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
    }


def _history_report_belongs_to_user(report_path: Path, user: str) -> bool:
    if not user or not report_path:
        return False
    try:
        base = report_output_dir().expanduser().resolve()
        target = report_path.expanduser().resolve()
        if base == target or base in target.parents:
            return _can_access_report(base, target, user)
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


def ensure_web_users() -> dict[str, dict[str, str]]:
    ensure_directories()
    WEB_USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    users = _load_web_users()
    changed = False
    expected = _responsible_usernames()
    if os.getenv("WEB_AUTH_PRUNE_USERS", "1").strip().lower() not in {"0", "false", "no", "off"}:
        for username in list(users):
            explicit_role = str(users[username].get("role") or "").strip().lower()
            if username not in expected and explicit_role not in WEB_ROLES:
                users.pop(username, None)
                changed = True
    for username in sorted(expected):
        configured_role = str((_configured_web_user_profiles().get(username) or {}).get("role") or "").strip().lower()
        assigned_role = configured_role if configured_role in WEB_ROLES else _default_web_user_role(username)
        if username not in users:
            initial_password = generate_strong_password(12)
            users[username] = {
                "username": username,
                "password_hash": hash_web_password(initial_password),
                "role": assigned_role,
                "must_change_password": True,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            _append_initial_credential(username, initial_password)
            changed = True
        elif str(users[username].get("role") or "").strip().lower() != assigned_role:
            users[username]["role"] = assigned_role
            changed = True
    for record in users.values():
        legacy_password = str(record.get("password") or "")
        if legacy_password and not record.get("password_hash"):
            record["password_hash"] = hash_web_password(legacy_password)
            record.pop("password", None)
            changed = True
    if changed or not WEB_USERS_FILE.exists():
        WEB_USERS_FILE.write_text(json.dumps({"users": users}, ensure_ascii=False, indent=2), encoding="utf-8")
    return users


def _load_web_users() -> dict[str, dict[str, str]]:
    if not WEB_USERS_FILE.exists():
        return {}
    try:
        payload = json.loads(WEB_USERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    users = payload.get("users") if isinstance(payload, dict) else {}
    return users if isinstance(users, dict) else {}


def _responsible_usernames() -> set[str]:
    names: set[str] = set()
    names.add("admin")
    names.add("root")
    names.update(_configured_web_user_profiles())
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
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(digest.hex(), expected)
    except (TypeError, ValueError):
        return False


def _append_initial_credential(username: str, password: str) -> None:
    path = WEB_USERS_FILE.parent / "initial_credentials_20260714.txt"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if any(line.startswith(f"{username}=") for line in existing.splitlines()):
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{username}={password}\n")


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
    canonical_username, user = _find_web_user(users, username)
    if not user:
        return False, "Invalid username or password.", ""
    encoded = str(user.get("password_hash") or "")
    legacy = str(user.get("password") or "")
    password_matches = verify_web_password(password.strip(), encoded) if encoded else hmac.compare_digest(legacy, password.strip())
    if not password_matches:
        return False, "Invalid username or password.", ""
    ROBOT_CHALLENGES.pop(challenge_id, None)
    return True, "", canonical_username


def _find_web_user(users: dict[str, dict[str, str]], username: str) -> tuple[str, dict[str, str] | None]:
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
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1d232b;
      --muted: #68717d;
      --line: #d9dee7;
      --accent: #0b6bcb;
      --danger: #b42318;
      --ok: #137333;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #111418;
        --panel: #171b21;
        --text: #edf1f7;
        --muted: #a8b1bd;
        --line: #2c333d;
        --accent: #6aa9ff;
        --danger: #ff8a7a;
        --ok: #73d18a;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: var(--bg);
      color: var(--text);
      font-family: Arial, "Microsoft YaHei", sans-serif;
    }}
    main {{
      width: min(420px, calc(100vw - 32px));
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 24px;
    }}
    h1 {{ margin: 0 0 18px; font-size: 22px; }}
    label {{ display: grid; gap: 6px; margin-bottom: 14px; color: var(--muted); font-size: 13px; }}
    input, button {{ width: 100%; min-height: 42px; border-radius: 6px; font: inherit; }}
    input {{ border: 1px solid var(--line); background: transparent; color: var(--text); padding: 10px; }}
    button {{ border: 1px solid var(--accent); background: var(--accent); color: white; cursor: pointer; }}
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
      background: transparent;
      color: var(--accent);
    }}
    .challenge-line {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }}
    .challenge-line strong {{
      color: var(--text);
    }}
    .challenge-reset {{
      flex: 0 0 auto;
      border-color: var(--line);
      background: transparent;
      color: var(--accent);
    }}
    .status {{ min-height: 24px; margin-top: 12px; color: var(--muted); }}
    .status.error {{ color: var(--danger); }}
    .version-line {{
      margin-top: 16px;
      padding-top: 14px;
      border-top: 1px solid var(--line);
      color: var(--muted);
      font-size: 12px;
      text-align: center;
    }}
  </style>
</head>
<body>
  <main>
    <h1>CodeReviewer</h1>
    <label>Username
      <input id="username" autocomplete="username" placeholder="responsible">
    </label>
    <label>Password
      <span class="password-field">
        <input id="password" type="password" autocomplete="current-password" placeholder="Password">
        <button id="togglePassword" class="inline-button password-toggle" type="button" aria-label="Show password" aria-pressed="false">Show</button>
      </span>
    </label>
    <label>
      <span class="challenge-line">
        <span>Robot Check: <strong id="robotQuestion">{html.escape(challenge["question"])}</strong></span>
        <button id="refreshChallengeBtn" class="inline-button challenge-reset" type="button">Reset</button>
      </span>
      <input id="robotAnswer" inputmode="numeric" autocomplete="off">
    </label>
    <input id="challengeId" type="hidden" value="{html.escape(challenge["id"])}">
    <button id="loginBtn">Login</button>
    <div id="status" class="status"></div>
    <div class="version-line">
      Version <strong>{html.escape(version)}</strong>
    </div>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    let challengeRefreshTimer = 0;
    const CHALLENGE_REFRESH_MS = 60000;

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
    resetChallengeTimer();
  </script>
</body>
</html>"""


def render_index(user: str = "") -> str:
    role = _web_user_role(user)
    is_admin = role == "manager"
    initial_projects = render_projects_markup(list_gitlab_projects_for_user(user))
    admin_fields = """          <label>Sprint
            <input id="sprint" placeholder="10068">
          </label>
          <label>Jira Filter ID
            <input id="jiraFilter" placeholder="12345">
          </label>""" if is_admin else ""
    input_grid_class = "grid" if is_admin else "grid jira-only"
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
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1d232b;
      --muted: #68717d;
      --line: #d9dee7;
      --accent: #0b6bcb;
      --accent-strong: #094f96;
      --danger: #b42318;
      --ok: #137333;
      --code: #111827;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #111418;
        --panel: #171b21;
        --text: #edf1f7;
        --muted: #a8b1bd;
        --line: #2c333d;
        --accent: #6aa9ff;
        --accent-strong: #98c4ff;
        --danger: #ff8a7a;
        --ok: #73d18a;
        --code: #0f141b;
      }
    }
    * { box-sizing: border-box; }
    html {
      height: 100%;
    }
    body {
      margin: 0;
      height: 100%;
      overflow: hidden;
      font-family: Arial, "Microsoft YaHei", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    header {
      border-bottom: 1px solid var(--line);
      background: var(--panel);
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
      border-radius: 8px;
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
    input, select, textarea, button {
      font: inherit;
      border-radius: 6px;
    }
    input, select, textarea {
      width: 100%;
      border: 1px solid var(--line);
      background: transparent;
      color: var(--text);
      padding: 10px;
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
    }
    button:disabled {
      cursor: wait;
      opacity: 0.64;
    }
    button.secondary {
      background: transparent;
      color: var(--accent-strong);
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
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
    .report-filter {
      --history-filter-width: min(100%, 252px);
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 8px;
      margin-bottom: 10px;
      justify-items: start;
    }
    .report-filter > .search-input,
    .report-filter-row {
      width: var(--history-filter-width);
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
    }
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
      margin: 12px 0 14px;
      gap: 4px;
      padding: 3px;
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
    .coverage-dialog {
      width: min(1180px, calc(100vw - 40px));
      max-height: calc(100vh - 48px);
      display: flex;
      flex-direction: column;
      gap: 14px;
      padding: 18px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: 0 18px 48px rgba(0, 0, 0, 0.22);
    }
    .coverage-filters {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) minmax(150px, 0.45fr) minmax(150px, 0.45fr) auto;
      gap: 10px;
      align-items: end;
    }
    .coverage-filters label {
      margin: 0;
    }
    .coverage-summary {
      display: grid;
      grid-template-columns: repeat(6, minmax(90px, 1fr));
      gap: 8px;
    }
    .coverage-stat {
      padding: 9px 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: color-mix(in srgb, var(--bg) 62%, var(--panel));
    }
    .coverage-stat strong {
      display: block;
      margin-top: 2px;
      font-size: 18px;
    }
    .coverage-table-wrap {
      min-height: 240px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 6px;
    }
    .coverage-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    .coverage-table th,
    .coverage-table td {
      padding: 9px 10px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }
    .coverage-table th {
      position: sticky;
      top: 0;
      z-index: 1;
      background: var(--panel);
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
      width: min(680px, calc(100vw - 32px));
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
      padding: 24px;
      background: rgba(17, 24, 39, 0.48);
    }
    .thread-modal-backdrop[hidden] {
      display: none;
    }
    .thread-modal-dialog {
      width: min(1280px, calc(100vw - 48px));
      height: min(840px, calc(100vh - 48px));
      max-height: calc(100vh - 48px);
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
    .thread-layout {
      display: grid;
      grid-template-columns: minmax(280px, 0.9fr) minmax(300px, 1fr) minmax(300px, 1fr);
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
      grid-template-columns: minmax(320px, 0.9fr) minmax(360px, 1fr);
      gap: 14px;
      height: 100%;
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
      border-radius: 6px;
      padding: 10px;
      background: var(--panel);
    }
    .chat-message {
      display: grid;
      gap: 4px;
      padding: 8px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: color-mix(in srgb, var(--bg) 38%, var(--panel));
      white-space: pre-wrap;
    }
    .chat-message.assistant {
      border-color: color-mix(in srgb, var(--accent) 45%, var(--line));
    }
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
    .job-card {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      padding: 12px;
    }
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
    .markdown-preview {
      flex: 1 1 auto;
      min-height: 0;
      overflow: auto;
      padding: 18px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--text);
      line-height: 1.62;
      font-size: 14px;
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
    .markdown-preview h3 { font-size: 15px; }
    .markdown-preview p {
      margin: 0 0 10px;
    }
    .markdown-preview ul,
    .markdown-preview ol {
      margin: 0 0 12px 22px;
      padding: 0;
    }
    .markdown-preview li {
      margin: 4px 0;
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
      padding-right: 2px;
    }
    .report-tab-panel[hidden] {
      display: none;
    }
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
      width: min(1160px, calc(100vw - 48px));
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
        overflow: auto;
      }
      .thread-messages, .followup-draft {
        min-height: 220px;
      }
      pre { min-height: 320px; }
      .grid { grid-template-columns: 1fr; }
    }
    .workflow-launch { min-height: 34px; padding: 6px 10px; }
    .workflow-modal {
      position: fixed; inset: 0; z-index: 1200; padding: 20px;
      background: color-mix(in srgb, #07111f 68%, transparent);
    }
    .workflow-dialog {
      width: min(1680px, 100%); height: calc(100vh - 40px); margin: 0 auto;
      display: grid; grid-template-rows: auto 1fr; overflow: hidden;
      border: 1px solid var(--line); border-radius: 12px; background: var(--panel);
      box-shadow: 0 24px 80px rgba(0,0,0,.28);
    }
    .workflow-head {
      display: flex; align-items: center; justify-content: space-between; gap: 16px;
      padding: 16px 20px; border-bottom: 1px solid var(--line);
    }
    .workflow-head h2, .workflow-head p { margin: 0; }
    .workflow-body { min-height: 0; display: grid; grid-template-columns: minmax(340px, 38%) 1fr; }
    .workflow-list-pane { min-height: 0; overflow: auto; padding: 16px; border-right: 1px solid var(--line); }
    .workflow-detail-pane { min-height: 0; overflow: auto; padding: 20px; background: color-mix(in srgb, var(--bg) 55%, var(--panel)); }
    .workflow-toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 14px; }
    .workflow-toolbar input { flex: 1; }
    .issue-review-table { width: 100%; border-collapse: collapse; font-size: 13px; }
    .issue-review-table th { position: sticky; top: 0; z-index: 2; background: var(--panel); color: var(--muted); text-align: left; }
    .issue-review-table th, .issue-review-table td { padding: 10px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
    .issue-review-row { cursor: pointer; }
    .issue-review-row:hover { background: color-mix(in srgb, var(--accent) 8%, transparent); }
    .status-chip, .severity-chip, .handling-chip {
      display: inline-flex; align-items: center; min-height: 24px; padding: 2px 8px;
      border-radius: 999px; border: 1px solid var(--line); font-size: 12px; white-space: nowrap;
    }
    .status-chip[data-status="passed"] { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 45%, var(--line)); }
    .status-chip[data-status="handling"], .status-chip[data-status="rescan-required"] { color: #b54708; }
    .severity-chip.critical { color: #b42318; font-weight: 700; }
    .severity-chip.high { color: #c2410c; font-weight: 700; }
    .issue-hero { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
    .issue-hero h2 { margin: 0 0 5px; font-size: 21px; }
    .metric-grid { display: grid; grid-template-columns: repeat(4,minmax(120px,1fr)); gap: 10px; margin: 14px 0; }
    .metric-card { padding: 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); }
    .metric-card strong { display: block; margin-top: 4px; font-size: 20px; }
    .workflow-tabs { display: flex; gap: 6px; margin: 16px 0 10px; border-bottom: 1px solid var(--line); }
    .workflow-tab { border: 0; border-radius: 6px 6px 0 0; background: transparent; color: var(--muted); }
    .workflow-tab.active { color: var(--accent-strong); border-bottom: 2px solid var(--accent); }
    .finding-card, .timeline-card, .draft-card, .discussion-card {
      margin-bottom: 10px; padding: 14px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel);
    }
    .finding-head, .draft-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
    .finding-actions { display: flex; gap: 7px; flex-wrap: wrap; margin-top: 10px; }
    .finding-actions textarea { min-height: 74px; }
    .followup-fields { display: grid; gap: 8px; margin-top: 9px; }
    .adf-editor-shell { border: 1px solid var(--line); border-radius: 8px; overflow: hidden; background: var(--panel); }
    .adf-toolbar { display: flex; gap: 5px; flex-wrap: wrap; padding: 8px; border-bottom: 1px solid var(--line); }
    .adf-toolbar button { min-height: 30px; padding: 4px 8px; font-size: 12px; }
    .adf-source { min-height: 260px; border: 0; border-radius: 0; font-family: Consolas,monospace; }
    .adf-preview { min-height: 260px; padding: 16px; overflow: auto; }
    .adf-preview table { width: 100%; border-collapse: collapse; }
    .adf-preview th, .adf-preview td { border: 1px solid var(--line); padding: 8px; }
    .adf-expand { margin: 10px 0; border: 1px solid var(--line); border-radius: 7px; padding: 8px 10px; }
    .adf-media { display: block; max-width: 100%; height: auto; border-radius: 6px; }
    .draft-editor-grid { display: grid; grid-template-columns: minmax(360px,1fr) minmax(360px,1fr); gap: 14px; }
    @media (max-width: 900px) {
      .workflow-modal { padding: 8px; }
      .workflow-dialog { height: calc(100vh - 16px); }
      .workflow-body { grid-template-columns: 1fr; }
      .workflow-list-pane { max-height: 42vh; border-right: 0; border-bottom: 1px solid var(--line); }
      .metric-grid { grid-template-columns: repeat(2,1fr); }
      .draft-editor-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <h1>CodeReviewer</h1>
      <div class="meta topbar-meta">
        <button id="issueReviewsBtn" class="secondary workflow-launch" type="button">Issue Reviews</button>
        <button id="pendingJiraBtn" class="secondary workflow-launch" type="button">Pending Jira</button>
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
          <div class="actions">
            <button id="runBtn">Run Review</button>
            <button class="secondary" id="refreshBtn" type="button">Refresh Reports</button>
            <button class="secondary" id="coverageBtn" type="button">Review Coverage</button>
            <div id="status" class="status"></div>
          </div>
          <div id="runHint" class="meta" style="margin-top: 10px;">__RUN_HINT__</div>
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
          <div class="report-filter">
            <input id="reportSearch" class="search-input" placeholder="Search reports, Jira, responsible">
            <div class="report-filter-row">
              <select id="reportDays" class="search-input" title="Report history range">
                <option value="14" selected>Last 2 weeks</option>
                <option value="30">Last 30 days</option>
                <option value="60">Last 60 days</option>
                <option value="0">All records</option>
              </select>
              <button class="secondary" id="refreshDownloadsBtn" type="button">Refresh</button>
            </div>
          </div>
          <div class="history-tabs" role="tablist" aria-label="Report history type">
            <button class="history-tab active" type="button" data-history-tab="reports" role="tab" aria-selected="true">Markdown Reports</button>
            <button class="history-tab" type="button" data-history-tab="responsibles" role="tab" aria-selected="false">Responsible Downloads</button>
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
    <div class="workflow-dialog" role="dialog" aria-modal="true" aria-labelledby="issueReviewTitle">
      <div class="workflow-head">
        <div><h2 id="issueReviewTitle">Issues Review History</h2><p class="meta">ECHNL Issue lifecycle, handling, re-scan and Pass readiness.</p></div>
        <div class="actions"><button id="refreshIssueReviewsBtn" class="secondary small-action" type="button">Refresh</button><button id="closeIssueReviewsBtn" class="icon-action" type="button" aria-label="Close">&#x2715;</button></div>
      </div>
      <div class="workflow-body">
        <section class="workflow-list-pane">
          <div class="workflow-toolbar"><input id="issueReviewSearch" placeholder="Search ECHNL, summary, responsible"><span id="issueReviewCount" class="count-pill">0</span></div>
          <div id="issueReviewList" class="meta">Loading Issue Reviews...</div>
        </section>
        <section id="issueReviewDetail" class="workflow-detail-pane"><div class="markdown-preview empty">Select an ECHNL Issue to inspect its Review history.</div></section>
      </div>
    </div>
  </div>

  <div id="draftEditorModal" class="workflow-modal" hidden>
    <div class="workflow-dialog" role="dialog" aria-modal="true" aria-labelledby="draftEditorTitle">
      <div class="workflow-head">
        <div><h2 id="draftEditorTitle">Pending Jira</h2><p id="draftEditorMeta" class="meta">ADF-native Issue Description</p></div>
        <div class="actions"><button id="saveDraftBtn" type="button">Save Draft</button><button id="closeDraftEditorBtn" class="icon-action" type="button" aria-label="Close">&#x2715;</button></div>
      </div>
      <div class="workflow-detail-pane">
        <div id="pendingDraftList"></div>
        <div id="draftEditorForm" hidden>
          <label>Issue Summary<input id="draftSummary" maxlength="255"></label>
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
              <textarea id="draftAdfSource" class="adf-source" spellcheck="false"></textarea>
              <div id="atlaskitAdfEditor" class="adf-preview" hidden></div>
              <div id="draftAdfPreview" class="adf-preview meta">Choose Preview to render the ADF document.</div>
            </div>
          </div>
          <div id="draftStatus" class="status"></div>
        </div>
      </div>
    </div>
  </div>

  <div id="previewModal" class="preview-modal-backdrop" hidden>
    <div class="preview-modal-dialog" role="dialog" aria-modal="true" aria-labelledby="previewModalTitle">
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
    <div class="coverage-dialog" role="dialog" aria-modal="true" aria-labelledby="coverageTitle">
      <div class="thread-head">
        <div>
          <h2 id="coverageTitle">Review Coverage</h2>
          <div id="coverageRoleHint" class="meta">Scan report generation and handling status.</div>
        </div>
        <button id="coverageCloseBtn" class="icon-action" type="button" title="Close coverage" aria-label="Close coverage">&#x2715;</button>
      </div>
      <div class="coverage-filters">
        <label>Jira issues
          <input id="coverageJira" placeholder="ECHNL-1001, ECHNL-1002">
        </label>
        <label>Sprint
          <input id="coverageSprint" placeholder="10068">
        </label>
        <label>Jira Filter ID
          <input id="coverageFilter" placeholder="12345">
        </label>
        <button id="coverageScanBtn" type="button">Scan</button>
      </div>
      <div id="coverageStatus" class="status"></div>
      <div id="coverageSummary" class="coverage-summary"></div>
      <div class="coverage-table-wrap">
        <table class="coverage-table">
          <thead><tr><th>Jira</th><th>Summary</th><th>Responsible</th><th>MRs</th><th>Reports</th><th>Handling</th><th>Status</th></tr></thead>
          <tbody id="coverageRows"><tr><td colspan="7" class="meta">Select a scope and scan.</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>

  <div id="threadModal" class="thread-modal-backdrop" hidden>
    <div class="thread-modal-dialog" role="dialog" aria-modal="true" aria-labelledby="threadModalTitle">
      <div class="thread-head">
        <div class="preview-title">
          <h2 id="threadModalTitle">Review Communication</h2>
          <div id="threadReportName" class="meta"></div>
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
        <button class="thread-tab" type="button" data-thread-tab="handling" role="tab" aria-selected="false">Handling</button>
        <button class="thread-tab" type="button" data-thread-tab="chat" role="tab" aria-selected="false">AI Chat</button>
        <button class="thread-tab" type="button" data-thread-tab="teams" role="tab" aria-selected="false">Teams</button>
        <button class="thread-tab" type="button" data-thread-tab="status" role="tab" aria-selected="false">Status</button>
      </div>
      <div id="discussionPane" class="thread-pane">
        <div class="thread-layout">
        <section class="thread-column">
          <h3>History</h3>
          <div id="threadMessages" class="thread-messages meta"></div>
        </section>
        <section class="thread-column">
          <h3>Reply</h3>
          <div class="thread-form">
            <label>Message
              <textarea id="threadMessage" placeholder="记录组长处理说明，例如：1 已整改，Pass通过；2 不是阻碍，另报issue跟进。"></textarea>
            </label>
            <div class="actions">
              <button class="secondary small-action" id="sendThreadMessageBtn" type="button">Send</button>
            </div>
            <label>Follow-up整理说明
              <textarea id="followupInstruction" placeholder="告诉 LLM/整理器需要怎样汇总后续跟进清单，例如：只整理不是阻碍、另报 issue 跟进的改善项。"></textarea>
            </label>
            <div class="actions">
              <button class="secondary small-action" id="generateFollowupsBtn" type="button">Generate Follow-ups</button>
            </div>
          </div>
        </section>
        <section class="thread-column thread-followup-column">
          <h3>Follow-up Draft</h3>
          <pre id="followupDraft" class="followup-draft">No follow-up draft yet.</pre>
        </section>
        </div>
      </div>
      <div id="handlingPane" class="thread-pane" hidden>
        <section class="thread-column">
          <h3>Finding Handling</h3>
          <div id="handlingSummary" class="handling-summary meta">Loading handling status...</div>
          <div id="handlingList" class="handling-list meta">No findings loaded.</div>
        </section>
      </div>
      <div id="chatPane" class="thread-pane tools" hidden>
        <section class="thread-column">
          <h3>AI Chat</h3>
          <div id="aiChatMessages" class="chat-messages meta">Ask about this report, follow-up items, Teams wording, or pass readiness.</div>
        </section>
        <section class="thread-column">
          <h3>Ask</h3>
          <div class="thread-form">
            <label>Message
              <textarea id="aiChatInput" placeholder="例如：请总结 High/Critical 是否具备手动 Pass 条件，或帮我整理 Teams 群消息。"></textarea>
            </label>
            <div class="actions">
              <button class="secondary small-action" id="sendAiChatBtn" type="button">Ask AI</button>
            </div>
          </div>
        </section>
      </div>
      <div id="teamsPane" class="thread-pane tools" hidden>
        <section class="thread-column">
          <h3>Teams Delivery</h3>
          <div class="thread-form">
            <label>HTTPS Link
              <input id="teamsLink" placeholder="https://code-review.example.com/report/...">
            </label>
            <label>Teams Group / Channel
              <input id="teamsChannel" placeholder="e-Channel release review">
            </label>
            <label>@ Responsible
              <input id="teamsMention" placeholder="@wen.yi">
            </label>
            <label>Content
              <select id="teamsMode">
                <option value="markdown">Markdown report</option>
                <option value="zip">Responsible ZIP</option>
              </select>
            </label>
            <div class="actions">
              <button class="secondary small-action" id="sendTeamsBtn" type="button">Send / Prepare</button>
            </div>
          </div>
        </section>
        <section class="thread-column">
          <h3>Delivery Status</h3>
          <div id="teamsStatus" class="teams-status meta">Configure TEAMS_BOT_WEBHOOK_URL on the Web server to enable direct sending. Without it, this panel prepares the message only.</div>
        </section>
      </div>
      <div id="statusPane" class="thread-pane tools" hidden>
        <section class="thread-column">
          <h3>Issue Handling</h3>
          <div id="reviewPassStatus" class="review-status-box meta">High/Critical items should be fixed or explicitly clarified before manual Review Pass.</div>
        </section>
        <section class="thread-column">
          <h3>Actions</h3>
          <div class="thread-form">
            <button class="secondary" id="rescanReportBtn" type="button">Re-scan Report</button>
            <button class="secondary" id="manualPassBtn" type="button">Manual Review Pass</button>
            <div class="meta">Re-scan creates a fresh review job for the Jira issue. Manual Review Pass records a team lead decision in the report communication history.</div>
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

  __ADF_ASSET__
  <script>
    const $ = (id) => document.getElementById(id);
    let currentOutputDir = '';
    let currentUserIsAdmin = false;
    let currentUserRole = 'auditor';
    let currentPermissions = {};
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
      const priority = source
        ? String(source.report_min_severity || 'Medium')
        : ($('reportMinSeverity')?.selectedOptions?.[0]?.textContent || 'Medium and above');
      const subject = jira ? `Jira ${jira}` : (jiraFilter ? `Jira Filter ${jiraFilter}` : (sprint ? `Sprint ${sprint}` : 'Review inputs'));
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

    function beginReviewLifecycle(payload) {
      lastReviewPayload = payload;
      reviewLifecycleActive = true;
      const state = $('progressState');
      const detail = $('progressDetail');
      if (state) {
        state.textContent = 'Starting';
        state.className = 'progress-state running';
      }
      if (detail) detail.textContent = `Creating ${reviewInputSummary(payload)} review job...`;
      setRunFormCollapsed(true, { payload, focusProgress: true });
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
      for (const button of document.querySelectorAll('[data-thread-tab="teams"]')) button.hidden = !currentPermissions.teams_delivery;
      if ($('manualPassBtn')) $('manualPassBtn').hidden = !currentPermissions.manual_pass;
      if ($('rescanReportBtn')) $('rescanReportBtn').hidden = !currentPermissions.run_issue_review;
      if ($('generateFollowupsBtn')) $('generateFollowupsBtn').hidden = !currentPermissions.ai_chat;
      return data;
    }

    async function loadProjects() {
      const data = await fetchJson('/api/projects');
      renderProjects(Array.isArray(data.projects) ? data.projects : []);
    }

    function openCoverage() {
      $('coverageModal').hidden = false;
      $('coverageJira').value = ($('jira')?.value || '').trim();
      $('coverageSprint').value = ($('sprint')?.value || '').trim();
      $('coverageFilter').value = ($('jiraFilter')?.value || '').trim();
      $('coverageRoleHint').textContent = currentUserRole === 'manager'
        ? 'Manager view: all configured responsible teams and Sprint issues.'
        : 'Auditor view: only GitLab projects and reports matching your responsible scope.';
      requestAnimationFrame(() => $('coverageJira').focus());
    }

    function closeCoverage() {
      $('coverageModal').hidden = true;
    }

    async function scanCoverage() {
      const params = new URLSearchParams();
      const jira = $('coverageJira').value.trim();
      const sprint = $('coverageSprint').value.trim();
      const jiraFilter = $('coverageFilter').value.trim();
      if (jira) params.set('jira', jira);
      if (sprint) params.set('sprint', sprint);
      if (jiraFilter) params.set('jira_filter', jiraFilter);
      $('coverageStatus').className = 'status running';
      $('coverageStatus').textContent = 'Scanning Jira, active jobs, reports, and handling results...';
      $('coverageScanBtn').disabled = true;
      try {
        const data = await fetchJson(`/api/review-coverage?${params.toString()}`);
        renderCoverage(data);
        $('coverageStatus').className = 'status ok';
        $('coverageStatus').textContent = `Coverage scan completed · ${Array.isArray(data.issues) ? data.issues.length : 0} issue(s)`;
      } catch (error) {
        $('coverageStatus').className = 'status error';
        $('coverageStatus').textContent = error.message;
      } finally {
        $('coverageScanBtn').disabled = false;
      }
    }

    function renderCoverage(data = {}) {
      const counts = data.counts || {};
      const labels = [
        ['missing', 'No report'],
        ['running', 'Generating'],
        ['pending', 'Handling'],
        ['ready', 'Ready for Pass'],
        ['passed', 'Review Pass'],
        ['failed', 'Failed']
      ];
      $('coverageSummary').innerHTML = labels.map(([key, label]) => `
        <div class="coverage-stat"><span class="meta">${label}</span><strong>${counts[key] || 0}</strong></div>
      `).join('');
      const rows = Array.isArray(data.issues) ? data.issues : [];
      $('coverageRows').innerHTML = rows.length ? rows.map((item) => `
        <tr>
          <td><strong>${escapeHtml(item.jira_key || '-')}</strong><div class="meta">${escapeHtml(item.jira_status || '')}</div></td>
          <td>${escapeHtml(item.summary || '-')}</td>
          <td>${escapeHtml(item.responsible || '-')}</td>
          <td>${item.mr_count || 0}</td>
          <td>${item.report_count || 0}${item.latest_report ? ('<br><button class="secondary small-action coverage-preview" type="button" data-report="' + escapeHtml(item.latest_report) + '" data-output-dir="' + escapeHtml(item.latest_output_dir || '') + '">Preview</button>') : ''}</td>
          <td>${item.handled_count || 0}/${item.finding_count || 0}<div class="meta">${item.blocking_pending || 0} blocker(s) pending</div></td>
          <td><span class="workflow-badge ${escapeHtml(item.workflow_status || '')}">${escapeHtml(coverageStatusLabel(item.workflow_status))}</span></td>
        </tr>
      `).join('') : '<tr><td colspan="7" class="meta">No issues found for this scope.</td></tr>';
      for (const button of document.querySelectorAll('.coverage-preview')) {
        button.addEventListener('click', () => {
          closeCoverage();
          openReportPreview(button.dataset.report || '', '', '', { outputDir: button.dataset.outputDir || '' });
        });
      }
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
            <a href="#" class="report-preview-open" data-report="${escapeHtml(report.thread_report || report.relative_path || '')}" data-output-dir="${escapeHtml(report.output_dir || currentOutputDir || '')}" data-raw="${escapeHtml(report.url || '#')}" data-download="${escapeHtml(report.download_url || '#')}">${breakableText(report.relative_path || report.name || '-')}</a>
            <div class="meta">${escapeHtml(report.responsible || 'root')} · ${escapeHtml(report.output_dir_name || '')} · ${formatBytes(report.size || 0)} · ${formatTime(report.modified)}</div>
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
      const active = ['discussion', 'handling', 'chat', 'teams', 'status'].includes(tab) ? tab : 'discussion';
      for (const button of document.querySelectorAll('.thread-tab')) {
        const selected = button.dataset.threadTab === active;
        button.classList.toggle('active', selected);
        button.setAttribute('aria-selected', selected ? 'true' : 'false');
      }
      for (const pane of ['discussion', 'handling', 'chat', 'teams', 'status']) {
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
      if (box.classList.contains('meta')) {
        box.classList.remove('meta');
        box.textContent = '';
      }
      const message = document.createElement('div');
      message.className = `chat-message ${role === 'assistant' ? 'assistant' : ''}`;
      message.innerHTML = `<strong>${role === 'assistant' ? 'AI' : 'You'}</strong><div>${escapeHtml(text || '').replace(/\\n/g, '<br>')}</div>`;
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
      if ($('aiChatMessages')) {
        $('aiChatMessages').classList.add('meta');
        $('aiChatMessages').textContent = 'Ask about this report, follow-up items, Teams wording, or pass readiness.';
      }
      if ($('teamsLink')) $('teamsLink').value = selectedReportDownloadLink();
      if ($('teamsMention')) $('teamsMention').value = reportResponsibleFromPath(reportPath) ? `@${reportResponsibleFromPath(reportPath)}` : '';
      if ($('teamsStatus')) $('teamsStatus').textContent = 'Configure TEAMS_BOT_WEBHOOK_URL on the Web server to enable direct sending. Without it, this panel prepares the message only.';
      setThreadTab('discussion');
      $('threadMessage').focus();
      try {
        const data = await fetchJson(`/api/report-thread?report=${encodeURIComponent(reportPath)}${selectedThreadOutputDir ? `&output_dir=${encodeURIComponent(selectedThreadOutputDir)}` : ''}`);
        renderThread(data);
        updateThreadNavigation();
      } catch (error) {
        $('threadMessages').textContent = error.message;
        $('followupDraft').textContent = 'No follow-up draft yet.';
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

    function renderThread(data) {
      const messages = Array.isArray(data.messages) ? data.messages : [];
      $('threadMessages').innerHTML = messages.length ? messages.map((message) => `
        <div class="thread-message ${message.kind === 'followup-draft' ? 'system' : ''}">
          <strong>${escapeHtml(message.user || '-')}</strong>
          <div class="meta">${escapeHtml(message.kind || 'comment')} · ${escapeHtml(message.time || '')}</div>
          <div>${escapeHtml(message.message || '').replace(/\\n/g, '<br>')}</div>
        </div>
      `).join('') : 'No communication yet.';
      $('followupInstruction').value = data.followup_instruction || '';
      $('followupDraft').textContent = data.followup_draft || 'No follow-up draft yet.';
      renderFindingHandling(data);
      updateReviewPassStatus(data);
      requestAnimationFrame(() => {
        const box = $('threadMessages');
        if (box) box.scrollTop = box.scrollHeight;
      });
    }

    function renderFindingHandling(data = {}) {
      const findings = Array.isArray(data.findings) ? data.findings : [];
      const results = data.handling_results || {};
      const summary = data.handling_summary || {};
      const canSubmit = Boolean((data.permissions || currentPermissions).submit_handling);
      if ($('handlingSummary')) {
        $('handlingSummary').textContent = `${summary.completed || 0}/${summary.total || findings.length} handled · ${summary.pending || 0} pending · ${summary.blocking_pending || 0} Critical/High pending`;
      }
      const list = $('handlingList');
      if (!list) return;
      if (!findings.length) {
        list.className = 'handling-list meta';
        list.textContent = 'No findings require handling.';
        return;
      }
      list.className = 'handling-list';
      list.innerHTML = findings.map((finding) => {
        const saved = results[finding.index] || {};
        return `
          <article class="handling-item">
            <div>
              <div class="handling-item-head">
                <span class="handling-severity">#${escapeHtml(finding.index)} · ${escapeHtml(finding.severity)}</span>
                <strong>${escapeHtml(finding.title)}</strong>
              </div>
              <div class="meta">${saved.user ? ('Last handled by ' + escapeHtml(saved.user) + ' · ' + escapeHtml(saved.time || '')) : 'Awaiting handling result'}</div>
            </div>
            <div class="handling-controls">
              <label>Result
                <select data-handling-disposition="${escapeHtml(finding.index)}" ${canSubmit ? '' : 'disabled'}>
                  <option value="">Select...</option>
                  <option value="fixed" ${saved.disposition === 'fixed' ? 'selected' : ''}>已整改，Pass通过</option>
                  <option value="follow-up" ${saved.disposition === 'follow-up' ? 'selected' : ''}>不是阻碍，另报 Jira</option>
                  <option value="not-issue" ${saved.disposition === 'not-issue' ? 'selected' : ''}>不是问题，Pass通过</option>
                </select>
              </label>
              <label>Explanation
                <textarea data-handling-note="${escapeHtml(finding.index)}" ${canSubmit ? '' : 'disabled'} placeholder="说明修改、另报 issue 或澄清依据">${escapeHtml(saved.note || '')}</textarea>
              </label>
              <button class="secondary small-action handling-save" type="button" data-finding-index="${escapeHtml(finding.index)}" ${canSubmit ? '' : 'disabled'}>Save</button>
            </div>
          </article>`;
      }).join('');
      for (const button of list.querySelectorAll('.handling-save')) {
        button.addEventListener('click', () => saveFindingHandling(button.dataset.findingIndex || ''));
      }
    }

    async function saveFindingHandling(findingIndex) {
      if (!selectedThreadReport || !findingIndex) return;
      const disposition = document.querySelector(`[data-handling-disposition="${findingIndex}"]`)?.value || '';
      const note = document.querySelector(`[data-handling-note="${findingIndex}"]`)?.value.trim() || '';
      const data = await fetchJson('/api/report-thread/handling', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          report: selectedThreadReport,
          output_dir: selectedThreadOutputDir,
          finding_index: findingIndex,
          disposition,
          note
        })
      });
      renderThread(data);
      setThreadTab('handling');
      await loadReports();
    }

    async function sendThreadMessage() {
      const message = $('threadMessage').value.trim();
      if (!selectedThreadReport || !message) return;
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

    async function sendAiChat() {
      const prompt = $('aiChatInput').value.trim();
      if (!selectedThreadReport || !prompt) return;
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

    async function sendTeamsMessage() {
      if (!selectedThreadReport) return;
      $('teamsStatus').textContent = 'Preparing Teams message...';
      const data = await fetchJson('/api/report-thread/teams-send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          report: selectedThreadReport,
          output_dir: selectedThreadOutputDir,
          link: $('teamsLink').value.trim(),
          channel: $('teamsChannel').value.trim(),
          mention: $('teamsMention').value.trim(),
          mode: $('teamsMode').value
        })
      });
      $('teamsStatus').textContent = [
        data.sent ? 'Teams message sent.' : 'Teams message prepared only.',
        data.message || '',
        '',
        'Payload:',
        data.teams_message || ''
      ].join('\\n').trim();
      if (data.thread) renderThread(data.thread);
      await loadReports();
    }

    async function manualReviewPass() {
      if (!selectedThreadReport) return;
      const confirmed = window.confirm('Record manual Review Pass for this report?');
      if (!confirmed) return;
      const data = await fetchJson('/api/report-thread/message', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          report: selectedThreadReport,
          output_dir: selectedThreadOutputDir,
          kind: 'manual-pass',
          message: 'Manual Review Pass recorded in CodeReviewer. High/Critical handling has been confirmed by team lead.'
        })
      });
      renderThread(data);
      setThreadTab('status');
      await loadReports();
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
      const elapsed = job.started_at ? Math.max(0, Math.round(((job.finished_at || Date.now() / 1000) - job.started_at))) : 0;
      const status = job.status || 'running';
      const statusClass = status === 'done' ? 'ok' : (status === 'failed' ? 'error' : (status === 'canceled' ? 'canceled' : 'running'));
      const title = payload.mode === 'jira-filter' ? `Jira Filter ${payload.jira_filter || '-'}` : (payload.mode === 'sprint' ? `Sprint ${payload.sprint || '-'}` : `Jira ${payload.jira_key || '-'}`);
      const events = Array.isArray(job.events) ? job.events.slice(-80) : [];
      card.dataset.status = status;
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
        <ul class="job-events">
          ${events.map((event) => renderJobEvent(event)).join('') || '<li class="progress-item active"><span class="progress-dot"></span><span>Waiting for job events...</span></li>'}
        </ul>
        <div class="progress-detail">
          ${result.conclusion ? `<div><strong>${escapeHtml(result.conclusion)}</strong></div>` : ''}
          ${job.status === 'done' ? `<div>Reviewed ${reviewed} item(s) · Skipped completed ${skipped} · Dev-branch skipped ${excluded} · Branch-type skipped ${branchTypeSkipped} · State skipped ${stateSkipped}</div>` : ''}
          ${job.status === 'done' ? `<div>Findings ${result.finding_count || 0} · ${escapeHtml(severityText)}</div>` : ''}
          ${result.report ? `<div>Report: ${escapeHtml(result.report)}</div>` : ''}
          ${errors.length ? `<div class="status error">Errors: ${escapeHtml(errors.map((item) => item.error || JSON.stringify(item)).join('; '))}</div>` : ''}
          ${job.error ? `<div class="status error">${escapeHtml(job.error)}</div>` : ''}
        </div>
      `;
      bindJobControlActions(card);
      scrollJobToLatest(card, job);
      updateProgressSummary();
    }

    function renderJobControls(job) {
      const status = job.status || '';
      const id = escapeHtml(job.id || '');
      if (['queued', 'running'].includes(status)) {
        return `
          <button class="secondary small-action job-control" type="button" data-job="${id}" data-action="pause">Pause</button>
          <button class="secondary small-action job-control" type="button" data-job="${id}" data-action="stop">Stop</button>
        `;
      }
      if (['pausing', 'paused'].includes(status)) {
        return `
          <button class="secondary small-action job-control" type="button" data-job="${id}" data-action="resume">Resume</button>
          <button class="secondary small-action job-control" type="button" data-job="${id}" data-action="stop">Stop</button>
        `;
      }
      if (status === 'stopping') {
        return '<button class="secondary small-action" type="button" disabled>Stopping...</button>';
      }
      if (['failed', 'canceled'].includes(status)) {
        return `<button class="secondary small-action job-control" type="button" data-job="${id}" data-action="retry">Retry</button>`;
      }
      return '';
    }

    function bindJobControlActions(card) {
      for (const button of card.querySelectorAll('.job-control')) {
        button.addEventListener('click', () => controlReviewJob(button.dataset.job || '', button.dataset.action || ''));
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
        if (eventsEl) eventsEl.scrollTop = eventsEl.scrollHeight;
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

    async function confirmJiraRerunIfNeeded(jiraKey, mode) {
      if (mode !== 'jira' || !jiraKey) return { ok: true, rerun: false };
      const check = await existingReportsForJira(jiraKey);
      const reports = Array.isArray(check.reports) ? check.reports : [];
      if (!reports.length) return { ok: true, rerun: false };
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
        const cleanup = (result) => {
          modal.hidden = true;
          document.removeEventListener('keydown', onKeyDown);
          resolve(result);
        };
        const onKeyDown = (event) => {
          if (event.key === 'Escape') cleanup(false);
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

    async function runReview() {
      $('status').className = 'status';
      $('status').textContent = 'Creating review job...';
      const jiraKey = $('jira').value.trim();
      const sprintEl = $('sprint');
      const sprint = sprintEl ? sprintEl.value.trim() : '';
      const jiraFilterEl = $('jiraFilter');
      const jiraFilter = jiraFilterEl ? jiraFilterEl.value.trim() : '';
      if (!jiraKey && !sprint && !jiraFilter) {
        $('status').className = 'status error';
        $('status').textContent = 'Please input a Jira issue, Sprint, or Jira Filter ID.';
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
      const payload = {
        mode,
        jira_key: jiraKey,
        jira_filter: jiraFilter,
        sprint: sprint,
        output_dir: currentOutputDir,
        report_min_severity: $('reportMinSeverity') ? $('reportMinSeverity').value : 'Medium',
        rerun_confirmed: false
      };
      beginReviewLifecycle(payload);
      try {
        var rerunDecision = await confirmJiraRerunIfNeeded(jiraKey, mode);
        if (rerunDecision && rerunDecision.action === 'reuse') {
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
          restoreRunFormAfterAbortedStart();
          $('status').className = 'status';
          $('status').textContent = 'Review canceled.';
          return;
        }
      } catch (error) {
        restoreRunFormAfterAbortedStart();
        $('status').className = 'status error';
        $('status').textContent = `Report check failed: ${error.message}`;
        return;
      }
      payload.rerun_confirmed = Boolean(rerunDecision && rerunDecision.action === 'rescan');
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
          events: [{ event: 'queued', message: 'Review job queued in browser.', data: {}, time: Date.now() / 1000 }],
          started_at: 0,
          finished_at: 0,
          result: null,
          error: ''
        };
        renderJobProgress(placeholder);
        if (jiraKey) $('jira').value = '';
        if (jiraFilterEl && jiraFilter) jiraFilterEl.value = '';
        pollReviewJob(data.job_id);
      } catch (error) {
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

    function openIssueReviews() {
      $('issueReviewModal').hidden = false;
      loadIssueReviews();
    }

    function closeIssueReviews() {
      $('issueReviewModal').hidden = true;
    }

    async function loadIssueReviews() {
      $('issueReviewList').textContent = 'Loading Issue Reviews...';
      try {
        const data = await fetchJson('/api/issue-reviews');
        issueReviews = data.issues || [];
        renderIssueReviews();
        if (selectedIssueReview) await loadIssueReviewDetail(selectedIssueReview);
      } catch (error) {
        $('issueReviewList').textContent = error.message;
      }
    }

    function renderIssueReviews() {
      const query = ($('issueReviewSearch').value || '').trim().toLowerCase();
      const rows = issueReviews.filter(item => [item.jira_key, item.summary, item.responsible, item.status].join(' ').toLowerCase().includes(query));
      $('issueReviewCount').textContent = String(rows.length);
      if (!rows.length) {
        $('issueReviewList').innerHTML = '<div class="markdown-preview empty">No Issue Review records match this scope.</div>';
        return;
      }
      $('issueReviewList').innerHTML = `<table class="issue-review-table"><thead><tr><th>Issue</th><th>Status</th><th>Handling</th><th>Updated</th></tr></thead><tbody>${rows.map(item => {
        const counts = item.handling_counts || {};
        return `<tr class="issue-review-row" data-jira="${escapeHtml(item.jira_key)}">
          <td><strong>${escapeHtml(item.jira_key)}</strong><div>${escapeHtml(item.summary || 'No summary')}</div><div class="meta">${escapeHtml(item.responsible || '-')}</div></td>
          <td><span class="status-chip" data-status="${escapeHtml(item.status)}">${escapeHtml(statusLabel(item.status))}</span><div class="meta">Run ${escapeHtml(item.run_number || '-')} · ${escapeHtml(item.finding_count || 0)} findings</div></td>
          <td><span class="handling-chip">Fixed ${counts.fixed || 0}</span> <span class="handling-chip">Jira ${counts['follow-up'] || 0}</span> <span class="handling-chip">Not issue ${counts['not-issue'] || 0}</span><div class="meta">Pending ${counts.pending || 0}</div></td>
          <td>${escapeHtml(formatDateTime(item.updated_at))}</td>
        </tr>`;
      }).join('')}</tbody></table>`;
      document.querySelectorAll('.issue-review-row').forEach(row => row.addEventListener('click', () => loadIssueReviewDetail(row.dataset.jira || '')));
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
      selectedIssueReview = jiraKey;
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
      const latest = runs[0] || { findings: [] };
      const findings = latest.findings || [];
      const readiness = data.pass_readiness || {};
      const severity = latest.severity_counts || {};
      const discussions = data.discussions || [];
      const drafts = data.drafts || [];
      const canPass = Boolean((data.permissions || {}).manual_pass);
      const canReview = Boolean((data.permissions || {}).run_issue_review);
      $('issueReviewDetail').innerHTML = `
        <div class="issue-hero"><div><h2>${escapeHtml(issue.jira_key)} · ${escapeHtml(issue.summary || 'Issue Review')}</h2><div class="meta">Responsible: ${escapeHtml(issue.responsible || '-')} · Latest Run ${escapeHtml(latest.run_number || '-')} · Updated ${escapeHtml(formatDateTime(issue.updated_at))}</div></div><span class="status-chip" data-status="${escapeHtml(issue.status)}">${escapeHtml(statusLabel(issue.status))}</span></div>
        <div class="metric-grid">
          <div class="metric-card"><span class="meta">Critical</span><strong>${severity.Critical || 0}</strong></div>
          <div class="metric-card"><span class="meta">High</span><strong>${severity.High || 0}</strong></div>
          <div class="metric-card"><span class="meta">Remaining blockers</span><strong>${(readiness.pending_blockers || []).length}</strong></div>
          <div class="metric-card"><span class="meta">Manager exceptions</span><strong>${readiness.manager_exceptions || 0}</strong></div>
        </div>
        <div class="actions">
          ${canReview ? '<button id="issueRescanBtn" class="secondary" type="button">Re-scan Issue</button>' : ''}
          ${canPass ? `<button id="issuePassBtn" type="button" ${readiness.ready ? '' : 'disabled'}>Manual Pass</button>` : ''}
          <span class="meta">${escapeHtml(readiness.message || '')}</span>
        </div>
        <div class="workflow-tabs"><button class="workflow-tab active" type="button">Problems</button><button class="workflow-tab" type="button">Discuss (${discussions.length})</button><button class="workflow-tab" type="button">History (${runs.length})</button><button class="workflow-tab" type="button">Pending Jira (${drafts.length})</button></div>
        <section><h3>Problem list · Run ${escapeHtml(latest.run_number || '-')}</h3>${findings.length ? findings.map(finding => renderWorkflowFinding(finding, data.role)).join('') : '<div class="markdown-preview empty">No findings in the latest Run. This Issue is ready for Leader review.</div>'}</section>
        <section><h3>Discuss</h3><div>${discussions.length ? discussions.map(item => `<div class="discussion-card"><strong>${escapeHtml(item.author)}</strong><span class="meta"> · ${escapeHtml(formatDateTime(item.created_at))}</span><p>${escapeHtml(item.message)}</p></div>`).join('') : '<div class="meta">No discussion yet.</div>'}</div><div class="finding-actions"><textarea id="issueDiscussionInput" placeholder="Discuss this Review Run or ask for clarification."></textarea><button id="sendIssueDiscussionBtn" class="secondary" type="button">Send</button></div></section>
        <section><h3>Review Run History</h3>${runs.map(run => `<div class="timeline-card"><strong>Run ${escapeHtml(run.run_number)}</strong> · ${escapeHtml(formatDateTime(run.created_at))}<div class="meta">${escapeHtml(run.conclusion || 'Completed')} · ${escapeHtml(run.report_path || '')}</div><div>${(run.findings || []).filter(item => item.lineage_state === 'new').length} New · ${(run.findings || []).filter(item => item.lineage_state === 'persisting').length} Persisting</div></div>`).join('')}</section>
        <section><h3>Pending Jira</h3>${drafts.length ? drafts.map(renderDraftCard).join('') : '<div class="meta">No Jira follow-up drafts.</div>'}</section>`;
      $('issueReviewDetail').querySelectorAll('[data-handle-finding]').forEach(button => button.addEventListener('click', () => submitWorkflowHandling(button.dataset.handleFinding || '')));
      $('issueReviewDetail').querySelectorAll('[data-finding-disposition]').forEach(select => {
        const sync = () => { const fields = $(`followup-${select.dataset.findingDisposition}`); if (fields) fields.hidden = select.value !== 'follow-up'; };
        select.addEventListener('change', sync); sync();
      });
      $('issueReviewDetail').querySelectorAll('[data-compose-adf]').forEach(button => button.addEventListener('click', () => openHandlingAdfComposer(button.dataset.composeAdf || '')));
      $('issueReviewDetail').querySelectorAll('[data-approve-handling]').forEach(button => button.addEventListener('click', () => approveWorkflowHandling(button.dataset.approveHandling || '', true)));
      $('issueReviewDetail').querySelectorAll('[data-override-handling]').forEach(button => button.addEventListener('click', () => managerOverrideHandling(button.dataset.overrideHandling || '')));
      $('issueReviewDetail').querySelectorAll('[data-edit-draft]').forEach(button => button.addEventListener('click', () => openDraftById(button.dataset.editDraft || '', drafts)));
      if ($('issueRescanBtn')) $('issueRescanBtn').addEventListener('click', () => {
        closeIssueReviews();
        $('jira').value = issue.jira_key;
        runReview();
      });
      if ($('issuePassBtn')) $('issuePassBtn').addEventListener('click', () => manualWorkflowPass(issue.jira_key));
      if ($('sendIssueDiscussionBtn')) $('sendIssueDiscussionBtn').addEventListener('click', () => sendWorkflowDiscussion(issue.jira_key, latest.id || ''));
    }

    function renderWorkflowFinding(finding, role) {
      const handling = finding.handling || null;
      const severityClass = String(finding.severity || '').toLowerCase();
      const needsApproval = handling && handling.approval_status === 'pending';
      const isManager = role === 'manager';
      return `<article class="finding-card"><div class="finding-head"><div><span class="severity-chip ${escapeHtml(severityClass)}">${escapeHtml(finding.severity)}</span> <strong>#${escapeHtml(finding.report_index)} ${escapeHtml(finding.title)}</strong><div class="meta">${escapeHtml(finding.file_path || 'No file')} · ${escapeHtml(statusLabel(finding.lineage_state))}</div></div>${handling ? `<span class="handling-chip">${escapeHtml(handling.disposition)} · ${escapeHtml(handling.approval_status)}</span>` : ''}</div>
        ${handling ? `<p>${escapeHtml(handling.note)}</p>${handling.manager_override ? `<div class="status">Manager Exception: ${escapeHtml(handling.override_reason)}</div>` : ''}<div class="finding-actions">${needsApproval && role !== 'developer' ? `<button class="secondary small-action" data-approve-handling="${handling.id}" type="button">Approve Not an issue</button>` : ''}${isManager && handling.disposition === 'follow-up' && !handling.manager_override ? `<button class="secondary small-action" data-override-handling="${handling.id}" type="button">Manager Exception</button>` : ''}</div>` : `<div class="finding-actions"><select id="disposition-${finding.id}" data-finding-disposition="${finding.id}"><option value="fixed">已整改，Pass通过</option><option value="follow-up">不是阻碍，另报 Jira</option><option value="not-issue">不是问题，Pass通过</option></select><textarea id="note-${finding.id}" placeholder="Required handling explanation"></textarea><div id="followup-${finding.id}" class="followup-fields" hidden><input id="jira-summary-${finding.id}" placeholder="Issue Summary (required for follow-up)"><textarea id="jira-adf-${finding.id}" hidden>${escapeHtml(JSON.stringify(textToAdf('Describe the follow-up requirement.')))}</textarea><button class="secondary" data-compose-adf="${finding.id}" type="button">Edit Issue Description (ADF)</button><span id="jira-adf-status-${finding.id}" class="meta">ADF description not reviewed.</span></div><button data-handle-finding="${finding.id}" type="button">Submit handling</button></div>`}
      </article>`;
    }

    function renderDraftCard(draft) {
      return `<article class="draft-card"><div class="draft-head"><div><strong>${escapeHtml(draft.summary)}</strong><div class="meta">${escapeHtml(draft.jira_key)} · ${escapeHtml(statusLabel(draft.status))} · v${escapeHtml(draft.version)}</div></div><button class="secondary small-action" data-edit-draft="${draft.id}" type="button">View / Edit</button></div></article>`;
    }

    function textToAdf(value) {
      return {version: 1, type: 'doc', content: String(value || '').split(/\\r?\\n/).map(line => ({type: 'paragraph', content: line ? [{type: 'text', text: line}] : []}))};
    }

    async function submitWorkflowHandling(findingId) {
      const disposition = $(`disposition-${findingId}`).value;
      const payload = {finding_id: findingId, disposition, note: $(`note-${findingId}`).value};
      if (disposition === 'follow-up') {
        payload.jira_summary = $(`jira-summary-${findingId}`).value;
        payload.jira_description_adf = JSON.parse($(`jira-adf-${findingId}`).value);
      }
      await fetchJson('/api/workflow/handling', {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload)});
      await loadIssueReviewDetail(selectedIssueReview);
    }

    async function approveWorkflowHandling(handlingId, approved) {
      const reason = window.prompt(approved ? 'Approval note' : 'Rejection reason', 'Verified by Leader') || '';
      await fetchJson('/api/workflow/handling/approve', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({handling_id:handlingId, approved, reason})});
      await loadIssueReviewDetail(selectedIssueReview);
    }

    async function managerOverrideHandling(handlingId) {
      const reason = window.prompt('Manager exception reason (required)') || '';
      if (!reason.trim()) return;
      await fetchJson('/api/workflow/handling/manager-override', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({handling_id:handlingId, reason})});
      await loadIssueReviewDetail(selectedIssueReview);
    }

    async function manualWorkflowPass(jiraKey) {
      const note = window.prompt('Manual Pass note', 'All configured blocking findings have been reviewed.') || '';
      await fetchJson('/api/workflow/pass', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({jira_key:jiraKey, note})});
      await loadIssueReviewDetail(jiraKey);
      await loadIssueReviews();
    }

    async function sendWorkflowDiscussion(jiraKey, runId) {
      const message = $('issueDiscussionInput').value.trim();
      if (!message) return;
      await fetchJson('/api/workflow/discussion', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({jira_key:jiraKey, run_id:runId, message})});
      await loadIssueReviewDetail(jiraKey);
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
      $('draftAdfSource').value = JSON.stringify(draft.description_adf || textToAdf(''), null, 2);
      $('draftEditorMeta').textContent = `${draft.jira_key} · Pending Create · version ${draft.version}`;
      renderCurrentAdf();
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
      $('draftAdfSource').value = JSON.stringify(currentJiraDraft.description_adf, null, 2);
      $('draftEditorMeta').textContent = `${selectedIssueReview} · New Jira follow-up · ADF composer`;
      renderCurrentAdf();
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
        const document = JSON.parse($('draftAdfSource').value);
        document.content = Array.isArray(document.content) ? document.content : [];
        document.content.push(adfNodeTemplate(type));
        $('draftAdfSource').value = JSON.stringify(document, null, 2);
        renderCurrentAdf();
      } catch (error) {
        $('draftStatus').className = 'status error';
        $('draftStatus').textContent = `ADF JSON error: ${error.message}`;
      }
    }

    async function renderCurrentAdf() {
      try {
        const document = JSON.parse($('draftAdfSource').value);
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

    function mountAtlaskitAdf(mode) {
      const host = $('atlaskitAdfEditor');
      if (!window.CodeReviewerADF || !host) return false;
      const document = JSON.parse($('draftAdfSource').value);
      host.hidden = false;
      $('draftAdfSource').hidden = true;
      $('draftAdfPreview').hidden = true;
      window.CodeReviewerADF.mount(host, {
        value: document,
        mode,
        onChange: value => { $('draftAdfSource').value = JSON.stringify(value, null, 2); }
      });
      return true;
    }

    async function saveCurrentDraft() {
      if (!currentJiraDraft) return;
      const document = JSON.parse($('draftAdfSource').value);
      if (currentJiraDraft.temporary) {
        const findingId = currentJiraDraft.findingId;
        $(`jira-summary-${findingId}`).value = $('draftSummary').value;
        $(`jira-adf-${findingId}`).value = JSON.stringify(document);
        $(`jira-adf-status-${findingId}`).textContent = 'ADF description ready.';
        $('draftEditorModal').hidden = true;
        currentJiraDraft = null;
        return;
      }
      const data = await fetchJson('/api/jira-drafts/update', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({draft_id:currentJiraDraft.id, summary:$('draftSummary').value, description_adf:document, version:currentJiraDraft.version})});
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
      const document = JSON.parse($('draftAdfSource').value);
      const mediaNode = {type:'mediaSingle',attrs:{layout:'center'},content:[{type:'media',attrs:{id:data.attachment.id,type:'file',alt:file.name,width:dimensions.width,height:dimensions.height}}]};
      const expand = (document.content || []).find(node => node.type === 'expand');
      if (expand) expand.content.push(mediaNode); else document.content.push(mediaNode);
      $('draftAdfSource').value = JSON.stringify(document,null,2);
      await renderCurrentAdf();
    }

    $('runBtn').addEventListener('click', runReview);
    $('issueReviewsBtn').addEventListener('click', openIssueReviews);
    $('pendingJiraBtn').addEventListener('click', () => openPendingJira().catch(error => {
      $('pendingDraftList').innerHTML = `<div class="status error">${escapeHtml(error.message)}</div>`;
    }));
    $('closeIssueReviewsBtn').addEventListener('click', closeIssueReviews);
    $('refreshIssueReviewsBtn').addEventListener('click', loadIssueReviews);
    $('issueReviewSearch').addEventListener('input', renderIssueReviews);
    $('issueReviewModal').addEventListener('click', event => { if (event.target === $('issueReviewModal')) closeIssueReviews(); });
    $('closeDraftEditorBtn').addEventListener('click', () => { $('draftEditorModal').hidden = true; currentJiraDraft = null; });
    $('saveDraftBtn').addEventListener('click', () => saveCurrentDraft().catch(error => {
      $('draftStatus').className = 'status error'; $('draftStatus').textContent = error.message;
    }));
    $('adfPreviewModeBtn').addEventListener('click', () => {
      if (!mountAtlaskitAdf('preview')) {
        $('draftAdfPreview').hidden = false;
        $('draftAdfSource').hidden = true;
        renderCurrentAdf();
      }
    });
    $('adfEditModeBtn').addEventListener('click', () => {
      if (!mountAtlaskitAdf('edit')) {
        $('atlaskitAdfEditor').hidden = true;
        $('draftAdfPreview').hidden = true;
        $('draftAdfSource').hidden = false;
        $('draftAdfSource').focus();
      }
    });
    document.querySelectorAll('[data-adf-insert]').forEach(button => button.addEventListener('click', () => insertAdfNode(button.dataset.adfInsert || 'paragraph')));
    $('draftImageInput').addEventListener('change', event => uploadDraftImage((event.target.files || [])[0]).catch(error => {
      $('draftStatus').className = 'status error'; $('draftStatus').textContent = error.message;
    }));
    $('runFormToggle').addEventListener('click', toggleRunForm);
    $('previewOpenBtn').addEventListener('click', openPreviewModal);
    $('refreshBtn').addEventListener('click', loadReports);
    $('coverageBtn').addEventListener('click', openCoverage);
    $('coverageCloseBtn').addEventListener('click', closeCoverage);
    $('coverageScanBtn').addEventListener('click', scanCoverage);
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
      sendThreadMessage().catch((error) => {
        $('threadMessages').textContent = error.message;
      });
    });
    $('generateFollowupsBtn').addEventListener('click', () => {
      generateFollowups().catch((error) => {
        $('followupDraft').textContent = error.message;
      });
    });
    $('sendAiChatBtn').addEventListener('click', () => {
      sendAiChat().catch((error) => appendAiChatMessage('assistant', error.message));
    });
    $('sendTeamsBtn').addEventListener('click', () => {
      sendTeamsMessage().catch((error) => {
        $('teamsStatus').textContent = `Teams delivery failed: ${error.message}`;
      });
    });
    $('rescanReportBtn').addEventListener('click', () => {
      regenerateReport(selectedThreadReport, selectedThreadOutputDir);
    });
    $('manualPassBtn').addEventListener('click', () => {
      manualReviewPass().catch((error) => {
        $('reviewPassStatus').textContent = `Manual pass failed: ${error.message}`;
      });
    });
    $('closeThreadBtn').addEventListener('click', () => {
      closeThreadModal();
    });
    if ($('networkBtn')) $('networkBtn').addEventListener('click', loadNetwork);
    async function init() {
      initSidebars();
      const me = await loadMe();
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
</html>""".replace("__ADF_ASSET__", adf_asset).replace("__ADMIN_TRACE_SECTION__", admin_trace).replace("__CURRENT_USER__", html.escape(user or "-")).replace("__CURRENT_ROLE__", html.escape(_web_user_role(user).title())).replace("__APP_VERSION__", html.escape(app_version())).replace("__INITIAL_PROJECTS__", initial_projects).replace("__SPRINT_FIELD__", admin_fields).replace("__INPUT_GRID_CLASS__", input_grid_class).replace("__RUN_HINT__", html.escape(run_hint))
