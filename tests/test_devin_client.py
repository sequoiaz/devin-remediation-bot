import pytest
from unittest.mock import MagicMock, patch

from app.devin_client import DevinAPIError, DevinClient


def make_client(**kwargs):
    return DevinClient(
        api_key="key", org_id="org-123", target_repo="fake-org/fake-repo",
        backoff_base=0, **kwargs
    )


def response(status_code, payload=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload or {}
    resp.text = "error body"
    return resp


def test_create_payload_shape(sample_issue):
    payload = make_client().build_create_payload(sample_issue)
    assert payload["repos"] == ["fake-org/fake-repo"]
    assert payload["title"] == "Remediate issue #42: Crash on empty config file"
    assert "issue-42" in payload["tags"]
    assert sample_issue["title"] in payload["prompt"]
    assert sample_issue["body"] in payload["prompt"]
    assert "#42" in payload["prompt"]
    assert payload["structured_output_schema"]["properties"].keys() == {
        "fix_summary", "files_changed", "risk_notes"
    }
    assert payload["structured_output_schema"]["properties"]["files_changed"] == {
        "type": "array", "items": {"type": "string"}
    }


def test_create_session_posts_expected_request(sample_issue):
    client = make_client()
    with patch("app.devin_client.requests.request", return_value=response(200, {"session_id": "s1"})) as req:
        result = client.create_session(sample_issue)
    assert result == {"session_id": "s1"}
    args, kwargs = req.call_args
    assert args[0] == "POST"
    assert args[1] == "https://api.devin.ai/v3/organizations/org-123/sessions"
    assert kwargs["headers"]["Authorization"] == "Bearer key"
    assert kwargs["json"] == client.build_create_payload(sample_issue)


def test_get_session_url():
    client = make_client()
    with patch("app.devin_client.requests.request", return_value=response(200, {"status": "finished"})) as req:
        client.get_session("s1")
    assert req.call_args[0] == ("GET", "https://api.devin.ai/v3/organizations/org-123/sessions/s1")


def test_retries_on_429_then_succeeds(sample_issue):
    client = make_client()
    with patch(
        "app.devin_client.requests.request",
        side_effect=[response(429), response(500), response(200, {"session_id": "s2"})],
    ) as req:
        assert client.create_session(sample_issue)["session_id"] == "s2"
    assert req.call_count == 3


def test_raises_custom_error_after_max_attempts(sample_issue):
    client = make_client()
    with patch("app.devin_client.requests.request", return_value=response(503)):
        with pytest.raises(DevinAPIError) as exc:
            client.create_session(sample_issue)
    assert "after 3 attempts" in str(exc.value)


def test_client_error_raises_immediately(sample_issue):
    client = make_client()
    with patch("app.devin_client.requests.request", return_value=response(404)) as req:
        with pytest.raises(DevinAPIError):
            client.create_session(sample_issue)
    assert req.call_count == 1


def test_dry_run_makes_no_network_call(sample_issue):
    client = make_client(dry_run=True)
    with patch("app.devin_client.requests.request") as req:
        created = client.create_session(sample_issue)
        fetched = client.get_session(created["session_id"])
    req.assert_not_called()
    assert created["session_id"].startswith("devin-dryrun-")
    assert fetched["status"] == "running"
    assert fetched["status_detail"] == "finished"
    assert fetched["pull_requests"][0]["pr_url"].startswith(
        "https://github.com/fake-org/fake-repo/pull/"
    )
    assert fetched["acus_consumed"] > 0


def test_get_acus_reads_the_consumption_api():
    client = make_client()
    with patch(
        "app.devin_client.requests.request",
        return_value=response(200, {"total_acus": 7.5}),
    ) as req:
        assert client.get_acus("799dfa97") == 7.5
    url = req.call_args[0][1]
    # The organization scope is tried first, and addresses sessions by prefixed id.
    assert url == (
        "https://api.devin.ai/v3/organizations/org-123"
        "/consumption/daily/sessions/devin-799dfa97"
    )


def test_get_acus_falls_back_to_the_enterprise_scope():
    """An org-scoped key may be refused there but allowed at enterprise level."""
    client = make_client()
    with patch(
        "app.devin_client.requests.request",
        side_effect=[response(403), response(200, {"total_acus": 7.5})],
    ) as req:
        assert client.get_acus("s1") == 7.5
    assert req.call_args[0][1].startswith("https://api.devin.ai/v3/enterprise/")
    assert not client.consumption_denied


def test_get_acus_stops_asking_once_the_key_is_not_allowed():
    client = make_client()
    with patch(
        "app.devin_client.requests.request", return_value=response(403)
    ) as req:
        assert client.get_acus("s1") is None
        assert client.get_acus("s2") is None
    # Both scopes tried once, then never again.
    assert req.call_count == 2
    assert client.consumption_denied


def test_a_missing_billing_record_keeps_other_sessions_billable():
    """404 is per-session; only 401/403 mean the key cannot read consumption."""
    client = make_client()
    with patch("app.devin_client.requests.request", return_value=response(404)):
        assert client.get_acus("s1") is None
    assert not client.consumption_denied


def test_one_scope_refusing_is_enough_to_stop_asking():
    """The other answering 404 does not make the key any more allowed."""
    client = make_client()
    with patch(
        "app.devin_client.requests.request",
        side_effect=[response(404), response(403)],
    ):
        assert client.get_acus("s1") is None
    assert client.consumption_denied
