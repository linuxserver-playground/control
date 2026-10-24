# control

The public CI control plane for the LinuxServer.io v2 playground (`linuxserver-playground` on GitHub). It
acts as a GitHub App that it bootstraps itself through the app manifest flow, receives every webhook from the
org and stores the deliveries, posts to Discord, and shows a status page. This first increment is only that
skeleton; scheduling, dashboards and the handlers that act on events come in later increments and build on
the `deliveries` table and the Discord client here.

## Running locally

```bash
python3.13 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
CONTROL_DATA_DIR=./config CONTROL_PUBLIC_URL=http://localhost:8080 python -m control   # http://localhost:8080/
ruff check . && ruff format --check . && mypy control tests && pytest
```

The Alpine base image ships Python 3.12, so the package allows 3.12 and up; develop on 3.12 or 3.13.

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

## Routes

| Route                      | What                                                                                      |
| -------------------------- | ----------------------------------------------------------------------------------------- |
| `GET /`                    | Status: app configured/installed, Discord configured, delivery count, last 20 deliveries. |
| `GET /healthz`             | `{"status":"ok","db":true}`                                                               |
| `GET /setup`               | Start the GitHub App manifest flow, or show the app's status once it exists.              |
| `GET /setup/callback`      | GitHub returns here with the one-time code; control converts it into credentials.         |
| `GET /setup/installed`     | GitHub returns here after the app is installed; control records the installation id.      |
| `POST /webhooks/github`    | Webhook intake.                                                                           |

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
