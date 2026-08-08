import os


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    """Runtime configuration, read from the environment."""

    def __init__(self) -> None:
        self.devin_api_key = os.getenv("DEVIN_API_KEY", "")
        self.devin_org_id = os.getenv("DEVIN_ORG_ID", "")
        self.github_token = os.getenv("GITHUB_TOKEN", "")
        self.github_webhook_secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")
        self.target_repo = os.getenv("TARGET_REPO", "")
        self.dry_run = _bool(os.getenv("DRY_RUN", "false"))
        self.database_path = os.getenv("DATABASE_PATH", "data/sessions.db")
        self.poll_interval_seconds = int(os.getenv("POLL_INTERVAL_SECONDS", "20"))
        self.remediation_label = os.getenv("REMEDIATION_LABEL", "devin-remediate")


def get_settings() -> Settings:
    """Settings are re-read on each call so tests can patch the environment."""
    return Settings()
