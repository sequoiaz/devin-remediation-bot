import logging
import random
import time
import uuid
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

DEVIN_API_BASE = "https://api.devin.ai/v3/organizations"

# Sessions minted by DRY_RUN do not exist in the API; the prefix identifies them
# once the bot is restarted with DRY_RUN off.
DRY_RUN_SESSION_PREFIX = "devin-dryrun-"

STRUCTURED_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "fix_summary": {"type": "string"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "risk_notes": {"type": "string"},
    },
}


class DevinAPIError(Exception):
    """Raised when the Devin API cannot be reached or returns a failing status."""


class DevinClient:
    def __init__(
        self,
        api_key: str,
        org_id: str,
        target_repo: str,
        dry_run: bool = False,
        max_attempts: int = 3,
        backoff_base: float = 1.0,
    ) -> None:
        self.api_key = api_key
        self.org_id = org_id
        self.target_repo = target_repo
        self.dry_run = dry_run
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base

    @property
    def base_url(self) -> str:
        return f"{DEVIN_API_BASE}/{self.org_id}"

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
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
                    raise DevinAPIError(
                        f"Devin API {method} {url} failed with HTTP "
                        f"{response.status_code}: {response.text[:300]}"
                    )
                else:
                    return response.json()

            if attempt < self.max_attempts:
                delay = self.backoff_base * (2 ** (attempt - 1))
                logger.warning(
                    "[devin] attempt %s/%s failed (%s); retrying in %.1fs",
                    attempt,
                    self.max_attempts,
                    last_error,
                    delay,
                )
                time.sleep(delay)

        raise DevinAPIError(
            f"Devin API {method} {url} failed after {self.max_attempts} attempts: {last_error}"
        )

    def build_prompt(self, issue: Dict[str, Any]) -> str:
        number = issue.get("number")
        title = issue.get("title", "")
        body = issue.get("body") or "(no description provided)"
        return (
            f"Fix GitHub issue #{number} in the repository {self.target_repo}.\n\n"
            f"Issue title: {title}\n\n"
            f"Issue body:\n{body}\n\n"
            "Investigate the root cause, implement a minimal and well-tested fix, and "
            f"open a pull request that references issue #{number} in its description. "
            "Run the repository's lint and test commands before opening the PR."
        )

    def build_create_payload(self, issue: Dict[str, Any]) -> Dict[str, Any]:
        number = issue.get("number")
        title = issue.get("title", "")
        return {
            "prompt": self.build_prompt(issue),
            "repos": [self.target_repo],
            "tags": ["devin-remediation-bot", f"issue-{number}"],
            "title": f"Remediate issue #{number}: {title}",
            "structured_output_schema": STRUCTURED_OUTPUT_SCHEMA,
        }

    def create_session(self, issue: Dict[str, Any]) -> Dict[str, Any]:
        payload = self.build_create_payload(issue)
        if self.dry_run:
            session_id = f"{DRY_RUN_SESSION_PREFIX}{uuid.uuid4().hex[:12]}"
            logger.info(
                "[devin][dry-run] would create session for issue #%s -> %s",
                issue.get("number"),
                session_id,
            )
            return {
                "session_id": session_id,
                "url": f"https://app.devin.ai/sessions/{session_id}",
                "status": "new",
                "status_detail": None,
                "pull_requests": [],
                "acus_consumed": 0.0,
            }
        return self._request("POST", f"{self.base_url}/sessions", json_body=payload)

    def get_session(self, session_id: str) -> Dict[str, Any]:
        if self.dry_run:
            pr_number = random.randint(100, 999)
            return {
                "session_id": session_id,
                "url": f"https://app.devin.ai/sessions/{session_id}",
                "status": "running",
                "status_detail": "finished",
                "pull_requests": [
                    {
                        "pr_url": f"https://github.com/{self.target_repo}/pull/{pr_number}",
                        "pr_state": "open",
                    }
                ],
                "acus_consumed": round(random.uniform(1.5, 9.5), 2),
            }
        return self._request("GET", f"{self.base_url}/sessions/{session_id}")
