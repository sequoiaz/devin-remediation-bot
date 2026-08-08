import logging
import time
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"


class GitHubAPIError(Exception):
    """Raised when the GitHub API cannot be reached or returns a failing status."""


class GitHubClient:
    def __init__(
        self,
        token: str,
        dry_run: bool = False,
        max_attempts: int = 3,
        backoff_base: float = 1.0,
    ) -> None:
        self.token = token
        self.dry_run = dry_run
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request(
        self, method: str, url: str, json_body: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = requests.request(
                    method, url, headers=self.headers, json=json_body, timeout=30
                )
            except requests.RequestException as exc:
                last_error = f"request error: {exc}"
            else:
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = (
                        f"HTTP {response.status_code} from {url}: {response.text[:300]}"
                    )
                elif response.status_code >= 400:
                    raise GitHubAPIError(
                        f"GitHub API {method} {url} failed with HTTP "
                        f"{response.status_code}: {response.text[:300]}"
                    )
                else:
                    return response.json()

            if attempt < self.max_attempts:
                delay = self.backoff_base * (2 ** (attempt - 1))
                logger.warning(
                    "[github] attempt %s/%s failed (%s); retrying in %.1fs",
                    attempt,
                    self.max_attempts,
                    last_error,
                    delay,
                )
                time.sleep(delay)

        raise GitHubAPIError(
            f"GitHub API {method} {url} failed after {self.max_attempts} attempts: {last_error}"
        )

    def get_issue(self, repo: str, issue_number: int) -> Dict[str, Any]:
        if self.dry_run:
            logger.info("[github][dry-run] returning fake issue #%s for %s", issue_number, repo)
            return {
                "number": issue_number,
                "title": f"[dry-run] Sample issue #{issue_number}",
                "body": "This is a synthetic issue body generated because DRY_RUN is enabled.",
                "html_url": f"https://github.com/{repo}/issues/{issue_number}",
                "labels": [{"name": "devin-remediate"}],
                "state": "open",
            }
        return self._request(
            "GET", f"{GITHUB_API_BASE}/repos/{repo}/issues/{issue_number}"
        )

    def post_comment(self, repo: str, issue_number: int, body: str) -> Dict[str, Any]:
        if self.dry_run:
            logger.info(
                "[github][dry-run] would comment on %s#%s: %s", repo, issue_number, body
            )
            return {"id": 0, "body": body, "dry_run": True}
        return self._request(
            "POST",
            f"{GITHUB_API_BASE}/repos/{repo}/issues/{issue_number}/comments",
            json_body={"body": body},
        )
