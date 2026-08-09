import asyncio
import logging
from typing import Any, Dict, Optional

from app.db import Database, is_done
from app.devin_client import DevinAPIError, DevinClient
from app.github_client import GitHubAPIError, GitHubClient

logger = logging.getLogger(__name__)


def _as_pr(entry: Any) -> Dict[str, Any]:
    if isinstance(entry, str):
        return {"url": entry, "state": None}
    return {"url": entry.get("pr_url"), "state": entry.get("pr_state")}


def extract_pr(
    session: Dict[str, Any], target_repo: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Pull request of a v3 session, whose entries are `{pr_url, pr_state}`.

    A session can open pull requests against several repositories, so the one
    belonging to `target_repo` wins over document order.
    """
    prs = [_as_pr(entry) for entry in session.get("pull_requests") or []]
    prs = [pr for pr in prs if pr["url"]]
    if not prs:
        return None
    if target_repo:
        for pr in prs:
            if f"/{target_repo}/pull/" in pr["url"]:
                return pr
    return prs[0]


def poll_once(
    db: Database,
    devin_client: DevinClient,
    github_client: GitHubClient,
    target_repo: str,
    force: bool = False,
) -> int:
    """Refresh every non-terminal session. Returns the number of rows updated.

    `force` also refreshes rows that already count as done, which recovers a row
    an earlier version of the bot left terminal without ever recording its PR.
    """
    rows = db.list_sessions() if force else db.list_non_terminal_sessions()
    updated = 0
    for row in rows:
        session_id = row["devin_session_id"]
        issue_number = row["issue_number"]
        was_done = is_done(
            row.get("status"), row.get("status_detail"), row.get("pr_url")
        )
        try:
            session = devin_client.get_session(session_id)
        except DevinAPIError as exc:
            logger.error(
                "[poll] session=%s issue=#%s failed to refresh: %s",
                session_id,
                issue_number,
                exc,
            )
            # Nothing polls a done row again, so a stored error would never clear.
            if not was_done:
                db.record_poll(issue_number, session_id, error=str(exc))
            continue

        old_status = row.get("status")
        new_status = session.get("status")
        new_detail = session.get("status_detail")
        pr = extract_pr(session, target_repo)
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
        db.record_poll(issue_number, session_id)
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

        if is_done(new_status, new_detail, pr_url):
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
