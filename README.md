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
once (see Backfill); `python -m control discover` reads every live branch's `image.yml` once (see Scheduler);
`python -m control schedule --once [--image x] [--branch y] [--dry-run]` runs the upstream check now and prints
one line per branch (`--dry-run` resolves but records and dispatches nothing). All read the same environment
as the server.

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
| `HUB_URL`             | unset                       | Public URL of the hub on GitHub Pages; live build embeds and pages link their rendered results there. |
| `HUB_REPO`            | `hub`                       | Repository in the org that gets a `repository_dispatch` `image-released` after every successful live build, so the hub ingests the release right away. |
| `CONTROL_SETUP_TOKEN` | unset                         | When set, `/setup` requires `?token=<value>`. Set it on any public deployment.       |
| `DISCORD_WEBHOOK_URL` | unset                         | Discord webhook for notifications; without it they are logged and dropped.           |
| `CONTROL_EVENTS_SECRET` | unset                       | Shared with the `workflows` repo; signs the events POSTed to `/events`. Unset means `/events` answers 503. |
| `CONTROL_BACKFILL_ON_START` | `false`                 | `true` runs one GitHub API backfill in the background after boot (once the app is configured and installed). |
| `CONTROL_CONCURRENCY_LIMIT` | `20`                    | GitHub's concurrent job limit for the org; the queue page measures in-progress jobs against it. |
| `CONTROL_SCHEDULER`   | `true`                        | Run fleet discovery and the scheduler loop beside the server. `false` keeps the pages and member actions and stops dispatching. |
| `CONTROL_SESSION_SECRET` | unset                      | Signs the sign-in cookie (HMAC-SHA256). **Set it** (`openssl rand -hex 32`): unset means a random secret per process, logged as a warning, and every restart signs everyone out. |

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
| `GET /scheduler`                 | `/api/scheduler.json`             | The decision log (filters `?image=` and `?outcome=`) and the next 24 hours of checks. Live-refreshing. |
| `GET /management`                | `/api/management.json`            | The public action log and skip flags; the action forms for signed-in members.          |
|                                  | `/api/skips.json`                 | Active skip flags.                                                                     |
| `GET /status`                    | `/api/status.json`                | App configured/installed, Discord, `/events`, scheduler, row counts, last backfill and discovery, last 20 deliveries. |
| `GET /api/stream`                |                                   | Server-sent events: `{"type": "<table>", "key": ...}` whenever a handler changes a row, including `scheduler_decisions`, `actions`, `skips` and `manifests`. |
| `GET /login`, `/login/callback`, `POST /logout` | | Member sign-in (below).                                                                 |
| `POST /manage/{rerun,cancel,dispatch,packages,skip,unskip,reconcile}` | | Member actions (below).                                 |
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
`/events`). Upserts are keyed by GitHub ids and guarded so a late or replayed delivery never moves a row
backwards; unknown events are stored and ignored; a handler failure is logged and never fails the webhook
response. The scheduler owns `manifests` (one row per live branch), `scheduler_decisions` and `skips`;
member actions write `actions`.

## Scheduler

**Fleet discovery** (`control/fleet.py`) lists the org's `docker-*` repos with the installation token and
reads `image.yml` from each default branch. `release.tags` there names every tag the image publishes; for
each tag other than the default branch's own, discovery lists the repo's branches, tries the one named after
the tag first, and reads `image.yml` until it finds the branch whose `release.tag` is that tag and whose
`release.branch` is itself (CONVENTIONS §3). The result is the `manifests` table (repo, branch, blob sha,
manifest JSON, `release.tag`, `upstream.version`, `build.package_check`), refreshed every 30 minutes, on
every `push` webhook to a branch it knows, by `python -m control discover`, and by the **Re-read** button on
`/management`. The fleet board lists these branches; before discovery has run for a repo it falls back to the
default branch plus branches with past live builds.

**Upstream check** (`control/scheduler.py`): every live branch once an hour, at a minute of the hour fixed by
`sha256(repo/branch)`, so the fleet spreads across the hour and the slot survives restarts. The check
resolves the upstream version with the resolver the build uses, vendored byte for byte in
`control/vendor/resolve_version.py` from `workflows/scripts/resolve_version.py` (a test fails on drift), run
in a thread with the installation token on `api.github.com` calls. The released version is the `<upstream>`
part of the branch's newest Release tag, `[T-]<upstream>-ls<N>`, matched the way `next_tag.py` does (the
unprefixed branch never claims another branch's `develop-…` tag); with no Release the last `build.summary`
of the branch is the hint. Both sides are compared as sanitised strings (§3). Outcomes, all recorded in
`scheduler_decisions` with a JSON detail (versions, reason, source URL):

| Outcome     | When                                                                                                   |
| ----------- | ------------------------------------------------------------------------------------------------------ |
| `unchanged` | Released equals upstream.                                                                              |
| `dispatch`  | They differ, or nothing was released yet: `workflow_dispatch` of `build.yml` on the branch with `reason: upstream`. |
| `skipped`   | A skip flag matches; a run of the branch is in progress; a dispatch for the branch happened in the last 2 hours (cooldown, whatever triggered it); `upstream.version.type` is `os` (those build on the package check); the type is `command` or `deb_repo` (arbitrary shell, or docker, which control will not run: they build on the package check too); or `require_assets` are not published yet. |
| `error`     | The resolver or the dispatch failed. After 6 errors in a row for one branch Discord gets one alert; a success resets the streak. |

**Package check**: every live branch whose manifest does not set `build.package_check: false`, once a week
at an hour of the week fixed by the same hash, dispatches `reason: packages` unless a skip flag matches or a
run is in progress. Both schedules are "due" when the slot has passed and no decision of that kind was
recorded since, so a restart never double-fires and a missed slot runs at the next tick (every 60 s).

Dispatch is always `POST /repos/{repo}/actions/workflows/build.yml/dispatches` with `ref: <branch>` and
`inputs: {reason}`: `repository_dispatch` only ever runs the default branch, and one path is simpler. The run
itself arrives by webhook; the scheduler page links a `dispatch` decision to the `workflow_dispatch` run that
appeared on the branch within 15 minutes.

**Skip flags** (`skips` table, `/api/skips.json`) replace the old `SKIP_EXTERNAL_TRIGGER` org variables. A
scope is `image` (every branch), `image/branch`, or `image/branch/version` (only while upstream resolves to
that sanitised version; the next version builds again). Members set and clear them on `/management`, the
fleet board and image pages; each flag keeps who set it and why. The fleet board shows `upstream`,
`released`, `drift` (`ahead`, `same`, `unknown`), `next_check` and the matching flags per branch.

## Member sign-in and actions

`/login` sends the browser to GitHub's OAuth authorisation with the App's `client_id`; `/login/callback`
exchanges the code (with `client_secret`) for a user token, uses it for exactly one call (`GET /user`) and
drops it. Membership is then read with **our** installation token: `GET /orgs/{org}/memberships/{login}`
must answer `state: active`. The session cookie (`control_session`: login, avatar, member flag, when it was
checked, a CSRF token) is HMAC-signed with `CONTROL_SESSION_SECRET`, `HttpOnly`, `Secure` when the public URL
is https, `SameSite=Lax`, and lives 7 days; the member flag is re-checked hourly on `/management` and before
every action. Sign-in needs no OAuth scopes and no "Request user authorization (OAuth) during installation".

**The GitHub App needs the callback URL**: `${CONTROL_PUBLIC_URL}/login/callback` under *Identifying and
authorizing users → Callback URL* in the app's settings. New apps get it from the manifest
(`callback_urls`); an app created before this increment must have it added by hand, or GitHub answers
`redirect_uri is not associated with this application`.

Actions are POST forms carrying the session's CSRF token; anyone not signed in or not an active member gets
`403`, a bad token `403`, more than 30 actions in an hour per member `429`. Every attempt that passes those
checks is written to `actions` (actor, action, target, result, detail) and shown on `/management`:

| Action      | Form fields              | What                                                                        |
| ----------- | ------------------------ | --------------------------------------------------------------------------- |
| `rerun`     | `run_id`                 | `POST …/actions/runs/{id}/rerun-failed-jobs`; on `/runs/{id}` for failed runs. |
| `cancel`    | `run_id`                 | `POST …/actions/runs/{id}/cancel`; on `/runs/{id}` while a run is active.      |
| `dispatch`  | `repo`, `branch`, `reason` (`manual`, `rebuild`, `packages`, `upstream`) | `workflow_dispatch` of `build.yml`; recorded as a `manual` scheduler decision (so the cooldown sees it). |
| `packages`  | `repo`, `branch`         | A package check now; recorded as a `packages` decision, which resets that branch's weekly slot. |
| `skip` / `unskip` | `scope`, `reason`  | Set or clear a skip flag.                                                   |
| `reconcile` |                          | Run fleet discovery now, in the background; the outcome shows on `/status`. |

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
