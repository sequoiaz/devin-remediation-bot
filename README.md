# devin-remediation-bot

Event-driven automation service: when a GitHub issue is labeled `devin-remediate`, it
creates a [Devin](https://devin.ai) session (Devin v3 API) that fixes the issue and opens a
PR, then polls the session, comments the PR link back on the issue, and reports results on a
dashboard.

## Architecture

| File | Responsibility |
| --- | --- |
| `app/main.py` | FastAPI routes: `/webhook/github`, `/simulate`, `/refresh`, `/dashboard`, `/metrics`, `/health` |
| `app/devin_client.py` | Devin v3 API wrapper (create/get session) with retries and `dry_run` |
| `app/github_client.py` | GitHub REST wrapper (get issue, post comment) with retries and `dry_run` |
| `app/db.py` | SQLite `sessions` table, upsert keyed on `(issue_number, devin_session_id)` |
| `app/poller.py` | Background poller (every 20s) refreshing non-terminal sessions |

A session stays in-flight until its lifecycle ends (`status` is `exit`/`error`) or it
reports `status_detail == "finished"` *and* its pull request has been captured, so a PR
published after the agent declares itself finished still reaches the dashboard. Failed
polls are recorded per row and shown in the dashboard's **Last poll** column and the
`poll_errors` metric instead of only reaching the log.

## Setup

```bash
cp .env.example .env      # fill in the values
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Environment variables:

| Name | Description |
| --- | --- |
| `DEVIN_API_KEY` | Devin API key (`Authorization: Bearer ...`) |
| `DEVIN_ORG_ID` | Devin organization id used in the v3 base URL |
| `GITHUB_TOKEN` | GitHub token with issue read/comment access to `TARGET_REPO` |
| `GITHUB_WEBHOOK_SECRET` | Shared secret for `X-Hub-Signature-256` verification |
| `TARGET_REPO` | `owner/repo` the bot remediates |
| `DRY_RUN` | `true` disables all outbound Devin/GitHub calls and returns fake data |
| `POLL_INTERVAL_SECONDS` | Poller interval (default `20`) |
| `DATABASE_PATH` | SQLite path (default `data/sessions.db`) |

## Run with Docker

```bash
docker compose up --build -d
curl http://localhost:8000/health
docker compose down
```

The SQLite file lives in the `./data` volume so rows survive restarts.

## Register the GitHub webhook

1. Expose the service: `ngrok http 8000` → copy the `https://<id>.ngrok-free.app` URL.
2. In the target repo: **Settings → Webhooks → Add webhook**
   - Payload URL: `https://<id>.ngrok-free.app/webhook/github`
   - Content type: `application/json`
   - Secret: the same value as `GITHUB_WEBHOOK_SECRET`
   - Events: *Let me select individual events* → **Issues**
3. Label an issue `devin-remediate` to trigger a remediation session.

The webhook verifies `X-Hub-Signature-256` (HMAC-SHA256 over the raw body), returns `401` on
mismatch, ignores anything that isn't `action == "labeled"` with the `devin-remediate` label,
and otherwise schedules session creation in a background task so GitHub gets an immediate
`200`.

## Trigger manually

```bash
curl -X POST http://localhost:8000/simulate \
  -H "Content-Type: application/json" \
  -d '{"issue_number": 1}'
```

Other endpoints:

```bash
curl -X POST http://localhost:8000/refresh   # poll in-flight sessions now
curl -X POST 'http://localhost:8000/refresh?force=true'  # also re-poll completed rows
# both return {"sessions_refreshed": N, "errors": [...]} so a failed poll is never silent
curl http://localhost:8000/health
curl http://localhost:8000/metrics
open http://localhost:8000/dashboard
```

## Dry run

Set `DRY_RUN=true` to exercise the whole pipeline (webhook → session → poller → dashboard)
without spending ACUs or touching a real repository; the clients return realistic fake
payloads instead of making network calls.

Dry-run rows keep their `devin-dryrun-…` session id in the database. Those sessions do not
exist in the Devin API, so the poller skips them once the bot runs with `DRY_RUN=false`
rather than failing against them on every cycle.

## Tests

```bash
pytest
```

All external HTTP is mocked; the suite makes no real network calls.
