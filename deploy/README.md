# Deploying control

Control is one container with one volume. It needs an always-on host, a public HTTPS URL (GitHub sends it
webhooks, the workflows post build events to it, and the website calls its JSON API), and a GitHub organisation
to install its GitHub App on. One control serves one organisation; a second organisation runs a second control
with its own App, data and settings.

`compose.yml` here is an example; `control.subdomain.conf` is an example proxy conf for SWAG. Any reverse proxy
works if it terminates TLS, forwards to port 8080 and sets `X-Forwarded-*`. Below, `<control-url>` is the public
URL you give it.

## Settings

The non-secret settings are code defaults in `control/config.py` (README, *Configuration*), so changing one is a
pull request. The deployment only sets what is specific to it, in `.env` (mode 600, never committed):

```bash
CONTROL_PUBLIC_URL=<control-url>
GITHUB_ORG=<organisation>                       # when it is not the default
CONTROL_SETUP_TOKEN=<openssl rand -hex 24>      # guards /setup
CONTROL_EVENTS_SECRET=<openssl rand -hex 32>    # also the organisation's CONTROL_EVENTS_SECRET Actions secret
CONTROL_SESSION_SECRET=<openssl rand -hex 32>   # signs the sign-in cookie; without it a restart signs everyone out
DISCORD_WEBHOOK_URL=<webhook>                   # optional: build notifications
OAUTH_CLIENT_ID=<id>                            # optional: member sign-in (below)
OAUTH_CLIENT_SECRET=<secret>
CONTROL_BACKFILL_ON_START=true                  # fill the dashboard from the GitHub API on start
```

The organisation needs the Actions secrets `CONTROL_URL` (`<control-url>`) and `CONTROL_EVENTS_SECRET`, and the
variables `CONTROL_URL`, `HUB_URL` and `SITE_URL`, which the shared workflows read.

## First start

1. Start it (`docker compose up -d`) and check `<control-url>/healthz` answers `{"status":"ok","db":true}`.
2. Open `<control-url>/setup?token=<CONTROL_SETUP_TOKEN>` as an owner of the organisation and click
   **Create GitHub App**. GitHub shows the manifest (permissions, events, webhook and callback URLs, all derived
   from `CONTROL_PUBLIC_URL`); confirm. Control stores the credentials in `/config/github_app.json`.
3. Install the App on the organisation, all repositories. `/status` then shows it configured and installed, and
   webhook deliveries start arriving. With `CONTROL_BACKFILL_ON_START=true` the next restart fills the dashboard;
   or run `docker exec control /lsiopy/bin/python -m control backfill` once.

## Member sign-in

Sign-in uses a classic OAuth App, which shows no "act on your behalf" consent: create one in the organisation's
developer settings with the callback URL `<control-url>/login/callback`, and put its id and secret in `.env`.
Active members of the organisation then see the action forms on control's pages and on the website.

## What runs by itself

The scheduler, the run archive, the Pages check, the reconcile of missed webhooks, self-healing, retention and the
database snapshots all run with code defaults and the App's own permissions; the README describes each and the
setting that turns it off. Control drives only what its organisation has: batch projects, the hub refresh and the
`hub-updated` relay skip an organisation without those repositories.

## Updating

Merging to `main` publishes `ghcr.io/linuxserver-playground/control:latest`. With the example's `updater`, the
deployment picks it up within 5 minutes; without it, `docker compose pull control && docker compose up -d control`.
To roll back, pin `image:` to an older `:<sha7>` tag.

## Backups

`/config` is the only state: `control.db`, its daily snapshots in `snapshots/`, and `github_app.json`, the App's
private key. Copy it off the host on a schedule and treat the copy as a secret; restoring it to a fresh container
restores control.
