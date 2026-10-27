# Working in control

control is the CI control plane for the LinuxServer.io v2 playground: a FastAPI app that is a GitHub App,
stores webhook deliveries in sqlite, builds a state model from them, takes signed build events from the
shared workflow, posts to Discord, serves the dashboard, schedules every upstream and package build of the
fleet, and lets org members act (re-run, cancel, dispatch, skip). Python 3.12+, no ORM, no frontend framework.

## Layout

- `control/config.py` settings from env; `control/db.py` sqlite with numbered migrations in code and the
  `deliveries` table; `control/model.py` typed rows, GitHub-object converters, upserts and queries for the
  state tables; `control/handlers.py` delivery → model (and `replay`); `control/backfill.py` the GitHub API
  cold start; `control/events.py` the signed `/events` endpoint and Discord embeds; `control/views.py` the
  pages, their JSON twins, the SSE stream and the badge route; `control/badge.py` the SVG;
  `control/stream.py` the in-process broadcaster; `control/github_app.py` manifest, conversion, credentials
  file, JWT and installation tokens; `control/github.py` the authenticated REST client the scheduler, fleet
  and actions share; `control/fleet.py` live-branch discovery from `image.yml` into `manifests`;
  `control/scheduler.py` the hourly upstream and release checks, the weekly package schedule, decisions and dispatch;
  `control/batch.py` the batch build projects (selkies-layers, proot-apps, wheelie): sizing `parallel` from the
  jobs running, their slots, and the per-unit failure alerts; `control/vendor/wheelie_gaps.py` wheelie's gap
  check, copied verbatim like the resolver;
  `control/vendor/resolve_version.py` the workflows repo's resolver, copied verbatim (header names the
  commit; `tests/test_vendor.py` fails on drift); `control/auth.py` GitHub OAuth sign-in, the signed session
  cookie and the membership check; `control/manage.py` the member POST actions; `control/webhooks.py`
  signature check and intake; `control/setup.py` the manifest bootstrap flow; `control/notify.py` Discord;
  `control/editor.py` the website editor (the member's own GitHub App token, commits and PRs on the site
repo's content); `control/templating.py` Jinja filters; `control/app.py` wires it together; `control/__main__.py` serve /
  replay / backfill / discover / schedule.
- `control/templates/` one template per page extending `base.html` (nav with sign-in state, CSS,
  live-refresh script); `_jobs.html` holds the shared job and step macros, `_actions.html` the member forms.
- `tests/` pytest, one temp data dir per test; `tests/fixtures/` minimal realistic webhook, control-event
  and REST payloads (`seed()` in `conftest.py` runs them through the handlers); `root/` the s6 services;
  `deploy/n97/` the operator files.

## Checks

```bash
pip install -e '.[dev]'
ruff check . && ruff format --check . && mypy control tests
pytest
```

All three must pass before a commit. Match the existing style: short modules, docstrings that say why.

## Deploying

A push to `main` is a deploy: Publish (`.github/workflows/publish.yml`) runs Check, publishes
`ghcr.io/linuxserver-playground/control:latest`, and the `updater` service on n97 recreates the container
within 5 minutes. A red `main` ships nothing. Settings and secrets live only in `~/control/.env` on n97; a new
setting needs a default that works without it, plus a line in `deploy/n97/README.md` for the operator.

## Rules

- Never commit secrets. Everything under the data dir (`control.db`, `github_app.json`) is a secret: it holds
  the GitHub App's private key, webhook secret and every webhook payload. `.env` files too.
- New schema changes are appended to `MIGRATIONS` in `db.py`; never edit an existing migration. After a
  migration that adds derived columns, `python -m control replay` rebuilds them from `deliveries`.
- Notifications must never raise into request handlers; webhook intake stays minimal and stores first.
  Handlers never raise either: log and continue, and keep every upsert idempotent and guarded against
  out-of-order deliveries (compare `updated_at` or a status rank).
- Each page is one function returning a pydantic model, rendered by a template and served as JSON under
  `/api/`; add both routes and a row in the README table. Handlers publish `stream.publish(table, key)`
  when they change a row so the live pages refresh.
- Incoming control events are validated with the `contracts` package from the builder repo, never with a
  hand-copied schema. Nothing may reference namespaces other than `linuxserver-playground` (use
  `settings.github_org`).
- The upstream version is resolved only through `control/vendor/resolve_version.py`, never reimplemented;
  to change it, change `workflows/scripts/resolve_version.py`, copy the file back and update the commit in
  the header. The scheduler never runs `command` or `deb_repo` sources (shell from a repo, docker).
- Every scheduler outcome is a `scheduler_decisions` row with a JSON detail; dispatch is always
  `workflow_dispatch` on `build.yml` with the branch as `ref` (a batch project: the workflow and ref its
  `PROJECTS` entry names). Member dispatches go through the same
  `Scheduler.dispatch`, so the cooldown and the log see them.
- Mutating routes live in `control/manage.py` only, apart from the website editor's in `control/editor.py`: POST, CSRF token from the session, `require_member`
  (401 anonymous, 403 for anyone else), rate limited, and an `actions` row whatever the outcome. Each action
  is one `prepare_*` function in `ACTIONS`; `POST /manage/{action}` (form, `csrf` field, redirect) and
  `POST /api/actions/{action}` (JSON body, `X-CSRF-Token` header, JSON result) both run it, so never add
  logic to one route family only. The sign-in token is used once for `GET /user` and never stored;
  membership is read with the installation token. The one token of a user control keeps is the website
  editor's (`control/editor.py`): asked for separately, encrypted at rest, used only on `SITE_REPO`'s content
  paths, never sent to a browser, revoked on sign-out. Editor writes go through `manage.require_member`
  (`editing=True`) and log an `edit-*` row like any action.
- The website on `CONTROL_ALLOWED_ORIGINS` drives the JSON API with the session cookie (CORS with
  credentials, `SameSite=None`); every new page needs its `/api/*.json` twin and a README row, and anything
  the site must know about the visitor goes through `/api/me`, never a token in a URL.
- Dockerfile: `git` is a build dependency (the `lsio-builder` git dependency) and pip runs with
  `--ignore-requires-python` because Alpine ships Python 3.12 while `lsio-builder` declares 3.13.
- Dockerfile and `root/` follow LinuxServer conventions (single RUN chain, s6-rc.d oneshot/longrun, no
  ENTRYPOINT/CMD/USER/HEALTHCHECK). Keep `Dockerfile.aarch64` identical except for the base tag.
