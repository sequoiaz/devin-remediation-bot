import asyncio
import logging
from typing import Any, Dict, List, NamedTuple, Optional

from app.db import Database, is_done
from app.devin_client import DRY_RUN_SESSION_PREFIX, DevinAPIError, DevinClient
from app.github_client import GitHubAPIError, GitHubClient

logger = logging.getLogger(__name__)

PR_STATES = {"open", "closed", "merged"}
SETTLED_PR_STATES = {"merged", "closed"}


class PollReport(NamedTuple):
    """Outcome of one poll cycle: rows refreshed, plus per-row failures."""

    updated: int
    errors: List[Dict[str, Any]]


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


def resolve_pr(
    devin_client: DevinClient,
    session: Dict[str, Any],
    target_repo: Optional[str] = None,
    max_depth: int = 2,
) -> Optional[Dict[str, Any]]:
    """Pull request of a session, or of a descendant it delegated the work to.

    A session that spawns children keeps an empty `pull_requests` of its own, so
    the PR has to be looked up on `child_session_ids`.
    """
    pr = extract_pr(session, target_repo)
    if pr or max_depth <= 0:
        return pr
    for child_id in session.get("child_session_ids") or []:
        try:
            child = devin_client.get_session(child_id)
        except DevinAPIError as exc:
            logger.warning("[poll] child session=%s unreadable: %s", child_id, exc)
            continue
        pr = resolve_pr(devin_client, child, target_repo, max_depth - 1)
        if pr:
            return pr
    return None


def poll_once(
    db: Database,
    devin_client: DevinClient,
    github_client: GitHubClient,
    target_repo: str,
    force: bool = False,
) -> PollReport:
    """Refresh every non-terminal session.

    `force` also refreshes rows that already count as done, which recovers a row
    an earlier version of the bot left terminal without ever recording its PR.
    """
    rows = db.list_sessions() if force else db.list_non_terminal_sessions()
    updated = 0
    errors: List[Dict[str, Any]] = []
    for row in rows:
        session_id = row["devin_session_id"]
        issue_number = row["issue_number"]
        was_done = is_done(
            row.get("status"), row.get("status_detail"), row.get("pr_url")
        )
        if session_id.startswith(DRY_RUN_SESSION_PREFIX) and not devin_client.dry_run:
            # Simulated session: the API never knew it, so polling only yields 403s.
            logger.debug(
                "[poll] session=%s issue=#%s is simulated, skipping",
                session_id,
                issue_number,
            )
            db.record_poll(issue_number, session_id)
            continue
        try:
            session = devin_client.get_session(session_id)
        except DevinAPIError as exc:
            logger.error(
                "[poll] session=%s issue=#%s failed to refresh: %s",
                session_id,
                issue_number,
                exc,
            )
            errors.append(
                {
                    "issue_number": issue_number,
                    "devin_session_id": session_id,
                    "error": str(exc),
                }
            )
            # Nothing polls a done row again, so a stored error would never clear;
            # the caller sees it in the report instead.
            if not was_done:
                db.record_poll(issue_number, session_id, error=str(exc))
            continue

        old_status = row.get("status")
        new_status = session.get("status")
        new_detail = session.get("status_detail")
        pr = resolve_pr(devin_client, session, target_repo)
        pr_url = pr["url"] if pr else None
        had_pr = bool(row.get("pr_url"))
        # A session keeps reporting the pull request as open after it is merged, so
        # GitHub's verdict wins once it is in.
        settled = row.get("pr_state") in SETTLED_PR_STATES
        pr_state = None if settled or not pr else pr["state"]

        db.upsert_session(
            issue_number=issue_number,
            devin_session_id=session_id,
            devin_session_url=session.get("url"),
            status=new_status,
            status_detail=new_detail,
            pr_url=pr_url,
            pr_state=pr_state,
            acus_consumed=session.get("acus_consumed"),
        )
        db.record_poll(issue_number, session_id)
        updated += 1
        # Every cycle re-reports the same state, so only transitions are worth a line.
        changed = (new_status, new_detail) != (old_status, row.get("status_detail"))
        logger.log(
            logging.INFO if changed else logging.DEBUG,
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

        if not was_done and is_done(new_status, new_detail, pr_url):
            logger.info(
                "[poll] session=%s issue=#%s is done (status=%s detail=%s)",
                session_id,
                issue_number,
                new_status,
                new_detail or "-",
            )
    errors.extend(refresh_pr_states(db, github_client))
    return PollReport(updated, errors)


def refresh_pr_states(
    db: Database, github_client: GitHubClient
) -> List[Dict[str, Any]]:
    """Keep what GitHub knows about each pull request current.

    A row whose session is done is never polled again, so its `pr_state` would stay
    `open` forever; GitHub is also the only source of when the pull request was
    actually opened, which is what time-to-PR should measure.
    """
    errors: List[Dict[str, Any]] = []
    for row in db.list_sessions():
        pr_url = row.get("pr_url")
        settled = row.get("pr_state") in SETTLED_PR_STATES
        if not pr_url or (settled and row.get("pr_created_at")):
            continue
        try:
            pr = github_client.get_pr(pr_url)
        except GitHubAPIError as exc:
            logger.error("[poll] pr=%s unavailable: %s", pr_url, exc)
            errors.append(
                {
                    "issue_number": row["issue_number"],
                    "devin_session_id": row["devin_session_id"],
                    "error": str(exc),
                }
            )
            continue
        if not pr:
            continue
        state = pr.get("state") if pr.get("state") in PR_STATES else None
        state = state if state != row.get("pr_state") else None
        created_at = pr.get("created_at") if not row.get("pr_created_at") else None
        if not isinstance(created_at, str):
            created_at = None
        if not state and not created_at:
            continue
        db.record_pr(row["issue_number"], row["devin_session_id"], state, created_at)
        if state:
            logger.info("[poll] pr=%s is now %s", pr_url, state)
    return errors


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
