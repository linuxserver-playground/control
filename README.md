# control

The public CI control plane for the LinuxServer.io v2 playground (`linuxserver-playground` on GitHub). It
acts as a GitHub App that it bootstraps itself through the app manifest flow, receives every webhook from the
org and stores the deliveries, turns them into a state model (repos, runs, jobs, pull requests, issues,
releases, builds), takes the signed `build.started` / `build.summary` events the shared workflow POSTs to
`/events`, posts to Discord, and serves the dashboard: fleet board, live runs, queue and capacity, pull
requests, issues, releases, build pages, a CI badge per image. The scheduler and member actions come in a
later increment; `scheduler_decisions` and the Management page are placeholders for them.

## Running locally

```bash
python3.13 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
CONTROL_DATA_DIR=./config CONTROL_PUBLIC_URL=http://localhost:8080 python -m control   # http://localhost:8080/
ruff check . && ruff format --check . && mypy control tests && pytest
```

`python -m control replay` reprocesses every stored webhook delivery and control event through the handlers
(after a migration, or to rebuild the model); `python -m control backfill` fills the model from the GitHub API
once (see Backfill). Both read the same environment as the server.

The Alpine base image ships Python 3.12, so the package allows 3.12 and up; develop on 3.12 or 3.13. The
`contracts` package (schema validation) is installed from the public
[builder](https://github.com/linuxserver-playground/builder) repo as `lsio-builder`.

As a container (the image is a standard LinuxServer image: `/config` volume, PUID/PGID, port 8080):

```bash
docker build -t control:dev .
docker run --rm -p 8080:8080 -e PUID=1000 -e PGID=1000 -e CONTROL_PUBLIC_URL=http://localhost:8080 -v "$PWD/config:/config" control:dev
```

## Environment

| Variable              | Default                       | What                                                                                 |
| --------------------- | ----------------------------- | ------------------------------------------------------------------------------------ |
| `CONTROL_PUBLIC_URL`  | `http://localhost:8080`       | Where GitHub and browsers reach control; every URL in the app manifest derives from it. No trailing slash. |
| `CONTROL_DATA_DIR`    | `/config`                     | Holds `control.db` and `github_app.json`. **Secrets**: the app's private key and webhook secret live here. |
| `GITHUB_ORG`          | `linuxserver-playground`      | The organisation the GitHub App is created in and installed on.                      |
| `CONTROL_APP_NAME`    | `LinuxServer CI (playground)` | The GitHub App's display name.                                                       |
| `CONTROL_SETUP_TOKEN` | unset                         | When set, `/setup` requires `?token=<value>`. Set it on any public deployment.       |
| `DISCORD_WEBHOOK_URL` | unset                         | Discord webhook for notifications; without it they are logged and dropped.           |
| `CONTROL_EVENTS_SECRET` | unset                       | Shared with the `workflows` repo; signs the events POSTed to `/events`. Unset means `/events` answers 503. |
| `CONTROL_BACKFILL_ON_START` | `false`                 | `true` runs one GitHub API backfill in the background after boot (once the app is configured and installed). |
| `CONTROL_CONCURRENCY_LIMIT` | `20`                    | GitHub's concurrent job limit for the org; the queue page measures in-progress jobs against it. |

## Routes

Every page is server-rendered and also available as JSON (pydantic models in `control/views.py`).

| Route                            | JSON                              | What                                                                                   |
| -------------------------------- | --------------------------------- | -------------------------------------------------------------------------------------- |
| `GET /`                          | `/api/fleet.json`                 | Fleet board: every image × live branch, last build result and age, released tag, upstream, drift (unknown until the scheduler), next check, skip flags. Live-refreshing. |
| `GET /runs`                      | `/api/runs.json`                  | Live runs with job-level state, elapsed and stuck detection; the last 50 finished runs. Live-refreshing. |
| `GET /runs/{id}`                 | `/api/runs/{id}.json`             | One run: jobs, steps timeline, linked builds.                                          |
| `GET /queue`                     | `/api/queue.json`                 | Queued and in-progress jobs, concurrency in use against `CONTROL_CONCURRENCY_LIMIT`.   |
| `GET /pulls`                     | `/api/pulls.json`                 | Open PRs across the org with build status, quarantined image tag, review state, age.   |
| `GET /issues`                    | `/api/issues.json`                | Open issues with labels and age.                                                       |
| `GET /releases`                  | `/api/releases.json`              | Releases, newest first, with the build's reason and upstream version.                  |
| `GET /builds/{owner}/{repo}/{run_id}` | `/api/builds/{owner}/{repo}/{run_id}.json` | One build: envelope, bundle (version, per-platform result, tests, assets), jobs. The bundle's `test.report_url` and release notes link here (CONVENTIONS §10). |
| `GET /images/{name}`             | `/api/images/{name}.json`         | One image: live branches, every build, releases, badge snippet.                        |
| `GET /images/{name}/{tag}`       | `/api/images/{name}/{tag}.json`   | One release tag of an image (`latest`, `develop`…); README badges link here.           |
| `GET /scheduler`                 | `/api/scheduler.json`             | Scheduler log (placeholder reading `scheduler_decisions`).                             |
| `GET /management`                | `/api/management.json`            | Member actions; stubbed, buttons disabled until sign-in lands.                         |
| `GET /status`                    | `/api/status.json`                | App configured/installed, Discord, `/events`, row counts, last backfill, last 20 deliveries. |
| `GET /api/stream`                |                                   | Server-sent events: `{"type": "<table>", "key": ...}` whenever a handler changes a row. |
| `GET /badge/{name}/{tag}.svg`    |                                   | Shields-style `CI | passing / failing / unknown` from the latest live build publishing that release tag (`latest`, `develop`…). Cached 5 minutes. |
| `GET /healthz`                   |                                   | `{"status":"ok","db":true}`                                                            |
| `GET /setup`                     |                                   | Start the GitHub App manifest flow, or show the app's status once it exists.           |
| `GET /setup/callback`            |                                   | GitHub returns here with the one-time code; control converts it into credentials.      |
| `GET /setup/installed`           |                                   | GitHub returns here after the app is installed; control records the installation id.   |
| `POST /webhooks/github`          |                                   | Webhook intake.                                                                        |
| `POST /events`                   |                                   | Control events from the shared workflow (below).                                       |

Stuck detection: a job is stuck when it has been in progress longer than the p95 duration of completed jobs
with the same name in the same repo (at least 10 samples; otherwise a fixed 60 minutes).

## State model

Webhooks are stored whole in `deliveries` first, then `control/handlers.py` upserts the model tables:
`repos` (image repos are those named `docker-*`), `runs` and `jobs` (`workflow_run`, `workflow_job`),
`pull_requests` (+ `pull_request_review` for the review state), `issues`, `releases`, `builds` (from
`/events`), and `scheduler_decisions` (written by the later scheduler). Upserts are keyed by GitHub ids and
guarded so a late or replayed delivery never moves a row backwards; unknown events are stored and ignored;
a handler failure is logged and never fails the webhook response.

## Events contract

The shared workflow POSTs a control-event (`schemas/control-event.schema.json` in
[builder](https://github.com/linuxserver-playground/builder), CONVENTIONS §10) to `/events` with
`X-Control-Signature: sha256=<hex>`, the HMAC-SHA256 of the raw body under `CONTROL_EVENTS_SECRET`.
control answers `503` when the secret is unset, `401` on a bad or missing signature, `400` with the list of
schema violations, and `202` otherwise. The body is stored in `deliveries` as `control:<type>` (keyed by
its hash, so a retried POST is a no-op) and replays with the webhooks.

- `build.started` creates a provisional `builds` row (result `null` = running). For `mode: live` one Discord
  embed is posted and its message id kept on the row.
- `build.summary` fills the row (result, failed job, version, upstream, platforms, test outcome, release or
  artifact URL, the whole bundle as `bundle_json`) and edits the started message into the summary, or posts
  one for dev/pr builds. Green for a live success, red for any failure, grey for a dev/pr success; title
  `<image> <tag>`, fields mode, reason, result, platforms, test, links (run, release, artifact, control).

## Backfill

`python -m control backfill` (or `CONTROL_BACKFILL_ON_START=true`) lists the org's repositories with the
installation token and, per repo, upserts the last 50 workflow runs with their jobs, open pull requests, open
issues and the last 20 releases. It is idempotent, skips the jobs call for runs it already saw finish, and
sleeps until the reset when `X-RateLimit-Remaining` drops below 50. The outcome is on `/status`.

## Bootstrap flow

1. `/setup` renders a form holding the app manifest (name, URLs, permissions, events) and a random `state`
   token kept in the database for 15 minutes. The form POSTs to GitHub's "create app from manifest" page
   for the org; the button is **Create GitHub App**.
2. GitHub creates the app and redirects the browser to `/setup/callback?code=…&state=…`.
3. control checks the `state` token (single use), calls `POST /app-manifests/{code}/conversions` (no auth
   needed; the code is the proof) and writes the result — app id, slug, client id/secret, webhook secret,
   private key — to `${CONTROL_DATA_DIR}/github_app.json` with mode 0600. Discord gets "GitHub App … created".
4. The browser is redirected to `https://github.com/apps/{slug}/installations/new`; after installing, GitHub
   redirects to `/setup/installed?installation_id=…`, which is stored and announced on Discord.

Nothing is pasted by hand, and the credentials never leave the data dir. Authenticating to GitHub later is
an RS256 JWT signed with that key (`iss` = app id, valid 10 minutes) exchanged for an installation token,
which is cached until five minutes before it expires.

## Webhook verification

GitHub signs every delivery with the webhook secret it generated at creation: `X-Hub-Signature-256` is
`sha256=` + HMAC-SHA256 of the raw body. control recomputes it and compares in constant time; a mismatch or
missing header is `401`, and deliveries before the app exists are `503` so GitHub retries them. Each
delivery is stored whole, keyed by `X-GitHub-Delivery` so redeliveries are ignored, and `installation`
events keep the stored installation id current.
