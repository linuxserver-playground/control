# Deploying control on n97

control runs as one container on the `sealskin-demo` network next to SWAG, which already terminates TLS
for `*.thelamer.duckdns.org` and proxies by container name. Nothing here publishes a port.

## One time

1. Make the directories and the env file (the data dir will hold the GitHub App's private key):

   ```bash
   mkdir -p /home/thelamer/control/config
   cat > /home/thelamer/control/.env <<'EOF'
   CONTROL_PUBLIC_URL=https://ci.thelamer.duckdns.org:8443
   CONTROL_SETUP_TOKEN=<openssl rand -hex 24>
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/<id>/<token>
   CONTROL_EVENTS_SECRET=<openssl rand -hex 32>   # the same value goes into the workflows repo as CONTROL_EVENTS_SECRET
   CONTROL_SESSION_SECRET=<openssl rand -hex 32>  # signs the member sign-in cookie; without it a restart signs everyone out
   CONTROL_ALLOWED_ORIGINS=https://linuxserver-playground.github.io  # the default; the website origin(s) that may drive the JSON API
   CONTROL_BACKFILL_ON_START=true
   CONTROL_SCHEDULER=true                         # the default; false stops dispatching builds
   CONTROL_BATCH_LIVE=wheelie                     # the default; batch projects control dispatches (csv), the rest are dry runs
   EOF
   chmod 600 /home/thelamer/control/.env
   ```

2. Copy `ci.subdomain.conf` next to the other site confs SWAG loads, and mount it in the sealskin stack's
   compose file the same way the existing confs are mounted, e.g.

   ```yaml
       volumes:
         - ./ci.subdomain.conf:/config/nginx/proxy-confs/ci.subdomain.conf:ro
   ```

   then `docker compose up -d swag` in that stack so nginx reloads with the new server block. `ci.` must
   be covered by the certificate SWAG already requests (a wildcard, or add it to `EXTRA_DOMAINS`/`SUBDOMAINS`).

3. Start control from this directory:

   ```bash
   docker compose -f compose.yml up -d
   ```

   For the very first deploy, before `ghcr.io/linuxserver-playground/control` exists, replace `image:` with
   `build: ../..` so it builds from the checkout, then `docker compose -f compose.yml up -d --build`.

4. Check it answers: `curl -fsS https://ci.thelamer.duckdns.org:8443/healthz` → `{"status":"ok","db":true}`.

## Bootstrapping the GitHub App

1. Open `https://ci.thelamer.duckdns.org:8443/setup?token=<CONTROL_SETUP_TOKEN>` as an owner of the
   `linuxserver-playground` org and click **Create GitHub App**. GitHub shows the manifest; confirm.
2. GitHub redirects back to `/setup/callback`, control stores the credentials in `/config/github_app.json`
   (0600, owned by `abc`) and sends you on to the install page.
3. Install the app on `linuxserver-playground` (all repositories). GitHub redirects to `/setup/installed`
   and control records the installation id. Discord gets a message for each step if the webhook is set.
4. `/status` now shows configured + installed, and webhook deliveries start appearing as repos see activity.
   With `CONTROL_BACKFILL_ON_START=true` the next restart fills the dashboard from the GitHub API; or run
   `docker exec control /lsiopy/bin/python -m control backfill` once by hand.

## Member sign-in

The GitHub App must know where to send members after they authorise: open the app's settings
(`https://github.com/organizations/linuxserver-playground/settings/apps/<slug>`), and under **Identifying and
authorizing users** add the callback URL `https://ci.thelamer.duckdns.org:8443/login/callback`. Leave
"Request user authorization (OAuth) during installation" off; it is not needed. Apps created from the manifest
after this increment get the URL automatically. Then `/login` works for anyone, and members of the org (active
membership) see the action forms on `/management`, `/runs/{id}`, `/images/{name}` and the fleet board.

## Granting new App permissions

The manifest only applies to an app created from it; an existing app needs new permissions granted by hand.
Pages setup on new image repos (`control/pages.py`) needs **Pages: write** and **Administration: write**: add them
under the App's *Permissions & events* (`https://github.com/organizations/linuxserver-playground/settings/apps/<slug>/permissions`),
then accept the request on the organisation's installation (*Settings → GitHub Apps → Configure*). Nothing on
n97 changes; control notices on the `installation` webhook. The run archive (`control/archive.py`) needs only
**Contents: write**, which the App already has. Both run with code defaults, no `.env` line
(`CONTROL_RUN_ARCHIVE=false` or `CONTROL_REPO_PAGES=false` turn them off).

## Deploying changes

Merge to `main`. The Publish workflow runs the checks, builds both arches and moves
`ghcr.io/linuxserver-playground/control:latest`; within 5 minutes the `updater` service pulls it and recreates
`control` (and only `control`). Watch it with `docker logs -f control-updater`, and confirm the running
revision with `docker inspect control --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'`.
To deploy right away instead of waiting:

```bash
docker compose -f compose.yml pull control && docker compose -f compose.yml up -d control
```

To roll back, pin `image:` to an older `:<sha7>` tag in `~/control/compose.yml` and `up -d`; the updater then
keeps that pin until you put `:latest` back.

`/config` is the only state. Back it up like a secret; restoring it to a fresh container restores the app.

## What only n97 holds

- `~/control/.env` (mode 600): the secrets listed above plus `OAUTH_CLIENT_ID`/`OAUTH_CLIENT_SECRET` for member
  sign-in, and the one deployment-specific value, `CONTROL_PUBLIC_URL`. Everything else (site, hub and consumer
  repos, allowed origins) uses the defaults in `control/config.py`, so changing it is a pull request, not an
  edit here. Changing `.env` needs ssh and `docker compose -f compose.yml up -d control`.
- `~/control/config/`: `control.db` and `github_app.json` (the App's private key).
- The SWAG side: `~/sealskin-demo/stack/compose.yml` mounts `ci.subdomain.conf`; the `*.thelamer.duckdns.org`
  certificate in `~/sealskin-demo/swag/keys/` is copied there by hand.

Adding an image to the fleet needs none of this: discovery reads every `docker-*` repo with an `image.yml`.
