import hashlib
import hmac
import json
from unittest.mock import patch

from tests.conftest import TARGET_REPO, WEBHOOK_SECRET


def sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def labeled_payload(issue, label="devin-remediate", action="labeled"):
    return {"action": action, "label": {"name": label}, "issue": issue}


def test_valid_signature_is_accepted(client, sample_issue):
    body = json.dumps(labeled_payload(sample_issue)).encode()
    with patch("app.main.create_remediation_session") as create:
        response = client.post(
            "/webhook/github",
            content=body,
            headers={"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    create.assert_called_once()
    assert create.call_args[0][0]["number"] == sample_issue["number"]


def test_invalid_signature_is_rejected(client, sample_issue):
    body = json.dumps(labeled_payload(sample_issue)).encode()
    with patch("app.main.create_remediation_session") as create:
        response = client.post(
            "/webhook/github",
            content=body,
            headers={"X-Hub-Signature-256": sign(body, "wrong-secret")},
        )
    assert response.status_code == 401
    create.assert_not_called()


def test_missing_signature_is_rejected(client, sample_issue):
    body = json.dumps(labeled_payload(sample_issue)).encode()
    response = client.post("/webhook/github", content=body)
    assert response.status_code == 401


def test_non_labeled_action_is_ignored(client, sample_issue):
    body = json.dumps(labeled_payload(sample_issue, action="opened")).encode()
    with patch("app.main.create_remediation_session") as create:
        response = client.post(
            "/webhook/github", content=body, headers={"X-Hub-Signature-256": sign(body)}
        )
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    create.assert_not_called()


def test_other_label_is_ignored(client, sample_issue):
    body = json.dumps(labeled_payload(sample_issue, label="bug")).encode()
    with patch("app.main.create_remediation_session") as create:
        response = client.post(
            "/webhook/github", content=body, headers={"X-Hub-Signature-256": sign(body)}
        )
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    create.assert_not_called()


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
