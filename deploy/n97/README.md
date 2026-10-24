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

   For the very first deploy, before `ghcr.io/linuxserver-playground/control` exists, comment out `image:`
   and uncomment `build: ../..` so it builds from the checkout, then `docker compose -f compose.yml up -d --build`.

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

## Upgrading

```bash
docker compose -f compose.yml pull && docker compose -f compose.yml up -d
```

`/config` is the only state. Back it up like a secret; restoring it to a fresh container restores the app.
