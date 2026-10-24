# Working in control

control is the CI control plane for the LinuxServer.io v2 playground: a FastAPI app that is a GitHub App,
stores webhook deliveries in sqlite and posts to Discord. Python 3.13, no ORM, no frontend framework.

## Layout

- `control/config.py` settings from env; `control/db.py` sqlite with numbered migrations in code;
  `control/github_app.py` manifest, conversion, credentials file, JWT and installation tokens;
  `control/webhooks.py` signature check and intake; `control/setup.py` the manifest bootstrap flow;
  `control/notify.py` Discord; `control/app.py` wires it together; `control/templates/` the pages.
- `tests/` pytest, one temp data dir per test; `root/` the s6 services; `deploy/n97/` the operator files.

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
- New schema changes are appended to `MIGRATIONS` in `db.py`; never edit an existing migration.
- Notifications must never raise into request handlers; webhook intake stays minimal and stores first.
- Dockerfile and `root/` follow LinuxServer conventions (single RUN chain, s6-rc.d oneshot/longrun, no
  ENTRYPOINT/CMD/USER/HEALTHCHECK). Keep `Dockerfile.aarch64` identical except for the base tag.
