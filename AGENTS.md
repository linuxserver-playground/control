# Working in control

control is the CI control plane for the LinuxServer.io v2 playground: a FastAPI app that is a GitHub App,
stores webhook deliveries in sqlite, builds a state model from them, takes signed build events from the
shared workflow, posts to Discord and serves the dashboard. Python 3.12+, no ORM, no frontend framework.

## Layout

- `control/config.py` settings from env; `control/db.py` sqlite with numbered migrations in code and the
  `deliveries` table; `control/model.py` typed rows, GitHub-object converters, upserts and queries for the
  state tables; `control/handlers.py` delivery → model (and `replay`); `control/backfill.py` the GitHub API
  cold start; `control/events.py` the signed `/events` endpoint and Discord embeds; `control/views.py` the
  pages, their JSON twins, the SSE stream and the badge route; `control/badge.py` the SVG;
  `control/stream.py` the in-process broadcaster; `control/github_app.py` manifest, conversion, credentials
  file, JWT and installation tokens; `control/webhooks.py` signature check and intake; `control/setup.py`
  the manifest bootstrap flow; `control/notify.py` Discord; `control/templating.py` Jinja filters;
  `control/app.py` wires it together; `control/__main__.py` serve / replay / backfill.
- `control/templates/` one template per page extending `base.html` (nav, CSS, live-refresh script);
  `_jobs.html` holds the shared job and step macros.
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
- Dockerfile: `git` is a build dependency (the `lsio-builder` git dependency) and pip runs with
  `--ignore-requires-python` because Alpine ships Python 3.12 while `lsio-builder` declares 3.13.
- Dockerfile and `root/` follow LinuxServer conventions (single RUN chain, s6-rc.d oneshot/longrun, no
  ENTRYPOINT/CMD/USER/HEALTHCHECK). Keep `Dockerfile.aarch64` identical except for the base tag.
