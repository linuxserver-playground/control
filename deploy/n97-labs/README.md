# Labs control on n97

A second control for the Labs org, `linuxserverlabs-playground` (workspace `docs/labs.md`). It runs the same
image as `deploy/n97/`, as its own compose project with its own `.env`, data and App, so a Labs key or webhook can
only reach Labs. SWAG proxies `labs-ci.*` to it on the `sealskin-demo` network; the `*.thelamer.duckdns.org`
certificate covers the name.

Control drives only what its org has: no batch projects, no hub refresh until a `hub` repository exists, and
`hub-updated` only to consumer repositories in the org (Labs has no site repository; the main org's site renders
Labs builds live from the Labs hub). Nothing here needs a code change for Labs.

## One time

1. `~/control-labs/.env` (mode 600), the main control's keys with Labs values:

   ```bash
   CONTROL_PUBLIC_URL=https://labs-ci.thelamer.duckdns.org:8443
   GITHUB_ORG=linuxserverlabs-playground
   CONTROL_APP_NAME=LinuxServer Labs CI (playground)
   CONTROL_SETUP_TOKEN=<openssl rand -hex 24>
   DISCORD_WEBHOOK_URL=<the Labs builds channel's webhook>
   CONTROL_EVENTS_SECRET=<openssl rand -hex 32>    # also the Labs org secret CONTROL_EVENTS_SECRET
   CONTROL_SESSION_SECRET=<openssl rand -hex 32>
   SITE_URL=https://linuxserver-playground.github.io/site/labs
   HUB_URL=https://linuxserverlabs-playground.github.io/hub
   CONTROL_BATCH_LIVE=
   CONTROL_BACKFILL_ON_START=true
   ```

2. Copy `compose.yml` to `~/control-labs/` and `labs-ci.subdomain.conf` into the sealskin stack's
   `swag/proxy-confs/`, mounted like `ci.subdomain.conf`, then `docker compose up -d swag` in that stack. Keep the
   stack's local source (`deploy.sh` rsyncs it with `--delete`) in step.
3. `cd ~/control-labs && docker compose -p control-labs up -d`, then
   `curl -fsS https://labs-ci.thelamer.duckdns.org:8443/healthz`.
4. Create the App from `https://labs-ci.thelamer.duckdns.org:8443/setup?token=<CONTROL_SETUP_TOKEN>` as an owner
   of the Labs org and install it on all repositories (`linuxserver-labs-ci-playground`, 2026-11-02).
5. Labs org secrets `CONTROL_URL` and `CONTROL_EVENTS_SECRET`, and the variables `CONTROL_URL`, `HUB_URL` and
   `SITE_URL` (the values above).

Member sign-in needs a classic OAuth App of its own in the Labs org (one callback URL per OAuth App:
`https://labs-ci.thelamer.duckdns.org:8443/login/callback`) and `OAUTH_CLIENT_ID`/`OAUTH_CLIENT_SECRET` in `.env`.
Labs starts without it.

## Deploying

The same as the main control: its own updater, `control-labs-updater`, pulls `control:latest` every 5 minutes and
recreates `control-labs` only. Never reuse the main control's container or project names here: the main updater
runs `docker compose -p control`, and a second project under that name would replace it.
