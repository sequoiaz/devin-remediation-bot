import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db as db_module  # noqa: E402

WEBHOOK_SECRET = "test-secret"
TARGET_REPO = "fake-org/fake-repo"


@pytest.fixture
def env(tmp_path, monkeypatch):
    database_path = str(tmp_path / "sessions.db")
    monkeypatch.setenv("DATABASE_PATH", database_path)
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("TARGET_REPO", TARGET_REPO)
    monkeypatch.setenv("DEVIN_API_KEY", "devin-key")
    monkeypatch.setenv("DEVIN_ORG_ID", "org-123")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("DRY_RUN", "false")
    db_module.reset_db()
    yield database_path
    db_module.reset_db()


@pytest.fixture
def database(env):
    return db_module.get_db(env)


@pytest.fixture
def client(env, monkeypatch):
    """App client with the background poller disabled; the poller is tested directly."""
    from fastapi.testclient import TestClient

    from app import main

    async def _no_poller(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "poller_loop", _no_poller)
    monkeypatch.setattr(main, "_poller_task", None)
    with TestClient(main.app) as test_client:
        yield test_client
    main._poller_task = None


@pytest.fixture
def sample_issue():
    return {
        "number": 42,
        "title": "Crash on empty config file",
        "body": "Loading an empty config.yaml raises a TypeError.",
        "html_url": f"https://github.com/{TARGET_REPO}/issues/42",
        "labels": [{"name": "devin-remediate"}],
    }
