import asyncio
import hashlib
import hmac
import html
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import get_settings
from app.db import STALLED_DETAILS, Database, get_db, is_done
from app.devin_client import DevinAPIError, DevinClient
from app.github_client import GitHubAPIError, GitHubClient
from app.poller import extract_pr, poll_once, poller_loop

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("app.main")

_poller_task: Optional[asyncio.Task] = None


def get_devin_client() -> DevinClient:
    settings = get_settings()
    return DevinClient(
        api_key=settings.devin_api_key,
        org_id=settings.devin_org_id,
        target_repo=settings.target_repo,
        dry_run=settings.dry_run,
    )


def get_github_client() -> GitHubClient:
    settings = get_settings()
    return GitHubClient(token=settings.github_token, dry_run=settings.dry_run)


def get_database() -> Database:
    return get_db(get_settings().database_path)


def verify_signature(secret: str, body: bytes, signature_header: Optional[str]) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def create_remediation_session(issue: Dict[str, Any]) -> Dict[str, Any]:
    """Create a Devin session for an issue and record it in the database."""
    settings = get_settings()
    db = get_database()
    devin_client = get_devin_client()
    issue_number = issue.get("number")

    active = db.get_active_session_for_issue(issue_number)
    if active:
        logger.info(
            "[remediate] issue=#%s already has in-flight session=%s; skipping duplicate",
            issue_number,
            active["devin_session_id"],
        )
        return active

    try:
        session = devin_client.create_session(issue)
    except DevinAPIError as exc:
        logger.error("[remediate] issue=#%s failed to create session: %s", issue_number, exc)
        raise

    session_id = session.get("session_id")
    pr = extract_pr(session, settings.target_repo)
    row = db.upsert_session(
        issue_number=issue_number,
        devin_session_id=session_id,
        issue_title=issue.get("title"),
        devin_session_url=session.get("url"),
        status=session.get("status", "new"),
        status_detail=session.get("status_detail"),
        pr_url=pr["url"] if pr else None,
        pr_state=pr["state"] if pr else None,
        acus_consumed=session.get("acus_consumed", 0.0),
    )
    logger.info(
        "[remediate] issue=#%s session=%s created (%s)",
        issue_number,
        session_id,
        session.get("url"),
    )

    try:
        get_github_client().post_comment(
            settings.target_repo,
            issue_number,
            f"Devin is working on this issue: {session.get('url')}",
        )
    except GitHubAPIError as exc:
        logger.error("[remediate] issue=#%s failed to comment: %s", issue_number, exc)

    return row


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global _poller_task
    settings = get_settings()
    get_database()
    _poller_task = asyncio.create_task(
        poller_loop(
            get_database(),
            get_devin_client(),
            get_github_client(),
            settings.target_repo,
            settings.poll_interval_seconds,
        )
    )
    try:
        yield
    finally:
        _poller_task.cancel()
        _poller_task = None


app = FastAPI(title="Devin Remediation Bot", lifespan=lifespan)


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook/github")
async def github_webhook(
    request: Request, background_tasks: BackgroundTasks
) -> Response:
    settings = get_settings()
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_signature(settings.github_webhook_secret, body, signature):
        return JSONResponse({"detail": "invalid signature"}, status_code=401)

    try:
        payload = json.loads(body.decode() or "{}")
    except json.JSONDecodeError:
        return JSONResponse({"status": "ignored", "reason": "invalid json"})

    action = payload.get("action")
    label_name = (payload.get("label") or {}).get("name")
    if action != "labeled" or label_name != settings.remediation_label:
        logger.info(
            "[webhook] ignoring event action=%s label=%s", action, label_name
        )
        return JSONResponse({"status": "ignored", "action": action, "label": label_name})

    issue = payload.get("issue") or {}
    background_tasks.add_task(create_remediation_session, issue)
    return JSONResponse({"status": "accepted", "issue_number": issue.get("number")})


@app.post("/refresh")
async def refresh(force: bool = False) -> Dict[str, Any]:
    """Poll every in-flight session immediately instead of waiting for the loop.

    `?force=true` also re-polls rows that already count as done, for a row an
    earlier version of the bot completed without ever recording its PR.
    """
    settings = get_settings()
    report = await asyncio.to_thread(
        poll_once,
        get_database(),
        get_devin_client(),
        get_github_client(),
        settings.target_repo,
        force,
    )
    return {
        "status": "ok",
        "sessions_refreshed": report.updated,
        "forced": force,
        "errors": report.errors,
    }


@app.post("/simulate")
async def simulate(request: Request) -> Response:
    settings = get_settings()
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"detail": "body must be valid JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"detail": "body must be a JSON object"}, status_code=400)

    issue_number = payload.get("issue_number")
    if issue_number is None:
        return JSONResponse({"detail": "issue_number is required"}, status_code=400)
    try:
        issue_number = int(issue_number)
    except (TypeError, ValueError):
        return JSONResponse(
            {"detail": "issue_number must be an integer"}, status_code=400
        )

    try:
        issue = get_github_client().get_issue(settings.target_repo, issue_number)
    except GitHubAPIError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=502)

    try:
        row = create_remediation_session(issue)
    except DevinAPIError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=502)

    return JSONResponse(
        {
            "status": "accepted",
            "issue_number": issue.get("number"),
            "devin_session_id": row.get("devin_session_id"),
            "devin_session_url": row.get("devin_session_url"),
        }
    )


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def time_to_pr_seconds(row: Dict[str, Any]) -> Optional[float]:
    if not row.get("pr_url"):
        return None
    start = _parse_ts(row.get("created_at"))
    end = _parse_ts(row.get("completed_at")) or _parse_ts(row.get("updated_at"))
    if not start or not end:
        return None
    return max((end - start).total_seconds(), 0.0)


def format_status(row: Dict[str, Any]) -> str:
    """`status`, qualified by `status_detail` when it adds information."""
    status = row.get("status") or "-"
    detail = row.get("status_detail")
    return f"{status} ({detail})" if detail else status


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def build_metrics() -> Dict[str, Any]:
    rows = get_database().list_sessions()
    sessions: List[Dict[str, Any]] = []
    ttp_values: List[float] = []
    completed_with_pr = 0
    failed_or_blocked = 0
    poll_errors = 0
    total_acus = 0.0

    for row in rows:
        ttp = time_to_pr_seconds(row)
        if ttp is not None:
            ttp_values.append(ttp)
        status = (row.get("status") or "").lower()
        detail = (row.get("status_detail") or "").lower()
        if row.get("pr_url"):
            completed_with_pr += 1
        elif status in ("error", "suspended") or detail in STALLED_DETAILS:
            failed_or_blocked += 1
        if row.get("last_poll_error"):
            poll_errors += 1
        total_acus += float(row.get("acus_consumed") or 0)
        sessions.append(
            {
                **row,
                "time_to_pr_seconds": ttp,
                "is_done": is_done(status, detail, row.get("pr_url")),
            }
        )

    total = len(rows)
    return {
        "summary": {
            "total_triggered": total,
            "completed_with_pr": completed_with_pr,
            "failed_or_blocked": failed_or_blocked,
            "poll_errors": poll_errors,
            "success_rate_pct": round(completed_with_pr / total * 100, 1) if total else 0.0,
            "average_time_to_pr_seconds": (
                round(sum(ttp_values) / len(ttp_values), 1) if ttp_values else None
            ),
            "total_acus_consumed": round(total_acus, 2),
        },
        "sessions": sessions,
    }


@app.get("/metrics")
async def metrics() -> Dict[str, Any]:
    return build_metrics()


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    data = build_metrics()
    summary = data["summary"]

    cards = "".join(
        f'<div class="card"><div class="card-label">{html.escape(label)}</div>'
        f'<div class="card-value">{html.escape(str(value))}</div></div>'
        for label, value in [
            ("Total triggered", summary["total_triggered"]),
            ("Completed with PR", summary["completed_with_pr"]),
            ("Failed / blocked", summary["failed_or_blocked"]),
            ("Success rate", f"{summary['success_rate_pct']}%"),
            ("Avg time to PR", format_duration(summary["average_time_to_pr_seconds"])),
            ("Total ACUs", summary["total_acus_consumed"]),
        ]
    )

    rows_html = []
    for session in data["sessions"]:
        pr_url = session.get("pr_url")
        pr_cell = (
            f'<a href="{html.escape(pr_url)}">{html.escape(pr_url)}</a>' if pr_url else "-"
        )
        session_url = session.get("devin_session_url")
        status = format_status(session)
        session_cell = (
            f'<a href="{html.escape(session_url)}">session</a>' if session_url else "-"
        )
        rows_html.append(
            "<tr>"
            f"<td>#{html.escape(str(session.get('issue_number')))}</td>"
            f"<td>{html.escape(str(session.get('issue_title') or '-'))}</td>"
            f"<td>{html.escape(str(status))}</td>"
            f"<td>{pr_cell}</td>"
            f"<td>{float(session.get('acus_consumed') or 0):.2f}</td>"
            f"<td>{html.escape(str(session.get('created_at') or '-'))}</td>"
            f"<td>{html.escape(str(session.get('updated_at') or '-'))}</td>"
            f"<td>{html.escape(format_duration(session.get('time_to_pr_seconds')))}</td>"
            f"<td>{session_cell}</td>"
            "</tr>"
        )

    body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Devin Remediation Dashboard</title>
<style>
body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; color: #1b1b1f; }}
h1 {{ font-size: 1.5rem; }}
.cards {{ display: flex; flex-wrap: wrap; gap: 1rem; margin-bottom: 2rem; }}
.card {{ border: 1px solid #ddd; border-radius: 8px; padding: 1rem 1.25rem; min-width: 150px; }}
.card-label {{ font-size: 0.8rem; color: #666; text-transform: uppercase; }}
.card-value {{ font-size: 1.6rem; font-weight: 600; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border-bottom: 1px solid #eee; padding: 0.5rem 0.75rem; text-align: left; font-size: 0.9rem; }}
th {{ background: #fafafa; }}
</style>
</head>
<body>
<h1>Devin Remediation Dashboard</h1>
<div class="cards">{cards}</div>
<table>
<thead><tr>
<th>Issue</th><th>Title</th><th>Status</th><th>PR</th><th>ACUs</th>
<th>Created</th><th>Updated</th><th>Time to PR</th><th>Devin</th>
</tr></thead>
<tbody>{''.join(rows_html) or '<tr><td colspan="9">No sessions yet.</td></tr>'}</tbody>
</table>
</body>
</html>"""
    return HTMLResponse(content=body)
