import asyncio
import logging
from typing import Any, Dict, Optional

from app.db import Database, is_done
from app.devin_client import DevinAPIError, DevinClient
from app.github_client import GitHubAPIError, GitHubClient

logger = logging.getLogger(__name__)


def extract_pr(session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """First pull request of a v3 session, whose entries are `{pr_url, pr_state}`."""
    pull_requests = session.get("pull_requests") or []
    if not pull_requests:
        return None
    first = pull_requests[0]
    if isinstance(first, str):
        return {"url": first, "state": None}
    return {"url": first.get("pr_url"), "state": first.get("pr_state")}


def poll_once(
    db: Database,
    devin_client: DevinClient,
    github_client: GitHubClient,
    target_repo: str,
) -> int:
    """Refresh every non-terminal session. Returns the number of rows updated."""
    updated = 0
    for row in db.list_non_terminal_sessions():
        session_id = row["devin_session_id"]
        issue_number = row["issue_number"]
        try:
            session = devin_client.get_session(session_id)
        except DevinAPIError as exc:
            logger.error(
                "[poll] session=%s issue=#%s failed to refresh: %s",
                session_id,
                issue_number,
                exc,
            )
            continue

        old_status = row.get("status")
        new_status = session.get("status")
        new_detail = session.get("status_detail")
        pr = extract_pr(session)
        pr_url = pr["url"] if pr else None
        had_pr = bool(row.get("pr_url"))

        db.upsert_session(
            issue_number=issue_number,
            devin_session_id=session_id,
            devin_session_url=session.get("url"),
            status=new_status,
            status_detail=new_detail,
            pr_url=pr_url,
            pr_state=pr["state"] if pr else None,
            acus_consumed=session.get("acus_consumed"),
        )
        updated += 1
        logger.info(
            "[poll] session=%s issue=#%s status=%s -> %s (%s)",
            session_id,
            issue_number,
            old_status,
            new_status,
            new_detail or "-",
        )

        if pr_url and not had_pr:
            body = (
                f"Devin opened a pull request for this issue: {pr_url}\n\n"
                f"Devin session: {session.get('url')}"
            )
            try:
                github_client.post_comment(target_repo, issue_number, body)
                logger.info(
                    "[poll] session=%s issue=#%s commented PR link %s",
                    session_id,
                    issue_number,
                    pr_url,
                )
            except GitHubAPIError as exc:
                logger.error(
                    "[poll] session=%s issue=#%s failed to comment: %s",
                    session_id,
                    issue_number,
                    exc,
                )

        if is_done(new_status, new_detail):
            logger.info(
                "[poll] session=%s issue=#%s is done (status=%s detail=%s)",
                session_id,
                issue_number,
                new_status,
                new_detail or "-",
            )
    return updated


async def poller_loop(
    db: Database,
    devin_client: DevinClient,
    github_client: GitHubClient,
    target_repo: str,
    interval_seconds: int = 20,
) -> None:
    logger.info("[poll] background poller started (interval=%ss)", interval_seconds)
    while True:
        try:
            await asyncio.to_thread(
                poll_once, db, devin_client, github_client, target_repo
            )
        except asyncio.CancelledError:
            logger.info("[poll] background poller stopped")
            raise
        except Exception:  # noqa: BLE001 - poller must never die
            logger.exception("[poll] unexpected error during poll cycle")
        await asyncio.sleep(interval_seconds)
