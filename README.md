# control

The public CI control plane for the LinuxServer.io v2 playground (`linuxserver-playground` on GitHub). It
acts as a GitHub App that it bootstraps itself through the app manifest flow, receives every webhook from the
org and stores the deliveries, turns them into a state model (repos, runs, jobs, pull requests, issues,
releases, builds), takes the signed `build.started` / `build.summary` events the shared workflow POSTs to
`/events`, posts to Discord, and serves the dashboard: fleet board, live runs, queue and capacity, pull
requests, issues, releases, build pages, a CI badge per image, plus the scheduler and the member actions.
Every page is also JSON under `/api/`, and the website on GitHub Pages renders the fleet pages from that
JSON (see *Driving control from the website*); the HTML pages here stay as the fallback.

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
`python -m control schedule --once [--image x] [--branch y] [--kind upstream|release|packages] [--dry-run]`
runs one check now and prints one line per branch: the upstream check by default (recorded as `manual`, so
the hourly slot is untouched), the release check, or `--kind packages`, which forces a package check and is
recorded as one, so it resets that branch's weekly slot (`--image` is required for it). `--dry-run` resolves
but records and dispatches nothing. All read the same environment as the server.

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
| `SITE_URL`            | `https://linuxserver-playground.github.io/site` | Public URL of the website, the single pane of glass: Discord embeds link to its image pages (`/images/<image>/`), build pages (`/images/<image>/builds/<tag>/`) and scheduler page (`/fleet/scheduler/?image=<image>`). No trailing slash. |
| `HUB_REPO`            | `hub`                       | Repository in the org that gets a `repository_dispatch` `image-released` after every successful live build, so the hub ingests the release right away. |
| `SITE_REPO`           | `site`                      | Repository in the org that gets a `repository_dispatch` `hub-updated` whenever the hub's Deploy workflow succeeds, so the website refreshes from the hub. |
| `HUB_CONSUMERS`       | `site,docs`                 | Every repository that rebuilds from hub data; each gets `hub-updated` after a successful hub Deploy (the site repo is always included). |
| `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET` | unset | A classic OAuth App (callback `${CONTROL_PUBLIC_URL}/login/callback`) used only for sign-in: its consent screen lists `read:user` and `read:org`, and membership is read with the user's token. Without it the GitHub App's OAuth client is used, which needs the App's Members permission. |
| `CONTROL_SETUP_TOKEN` | unset                         | When set, `/setup` requires `?token=<value>`. Set it on any public deployment.       |
| `DISCORD_WEBHOOK_URL` | unset                         | Discord webhook for notifications; without it they are logged and dropped.           |
| `HUB_URL`             | `https://linuxserver-playground.github.io/hub` | The hub's Pages site, read to say where an overdue publication is stuck. |
| `CONTROL_PUBLISH_DEADLINE_MINUTES` | `30`             | A released build's Discord message says "publishing" until its website page answers; past this it is reported (see below). |
| `CONTROL_PUBLISH_EXPIRY_HOURS` | `24`                 | After this control stops watching an overdue publication and posts once more. |
| `CONTROL_PUBLISH_WATCH` | `true`                      | Run the publication watcher (`control/publish.py`). |
| `CONTROL_DISCORD_VERBOSE` | `false`                   | Also post the quiet scheduler embeds (grey): a `rebuild` dispatched for a branch head that was never released, and a skip flag holding a branch back. Build embeds and the error-streak alert post regardless. |
| `CONTROL_EVENTS_SECRET` | unset                       | Shared with the `workflows` repo; signs the events POSTed to `/events`. Unset means `/events` answers 503. |
| `CONTROL_BACKFILL_ON_START` | `false`                 | `true` runs one GitHub API backfill in the background after boot (once the app is configured and installed). |
| `CONTROL_CONCURRENCY_LIMIT` | `20`                    | GitHub's concurrent job limit for the org; the queue page measures in-progress jobs against it. |
| `CONTROL_SCHEDULER`   | `true`                        | Run fleet discovery and the scheduler loop beside the server. `false` keeps the pages and member actions and stops dispatching. |
| `CONTROL_SESSION_SECRET` | unset                      | Signs the sign-in cookie (HMAC-SHA256). **Set it** (`openssl rand -hex 32`): unset means a random secret per process, logged as a warning, and every restart signs everyone out. |
| `CONTROL_ALLOWED_ORIGINS` | `https://linuxserver-playground.github.io` | Comma-separated web origins (`scheme://host[:port]`, never `*`) allowed to drive the JSON API from the browser: CORS with credentials, `next` after sign-in may point there, and the session cookie becomes `SameSite=None; Secure`. Empty turns all three off. |

## Routes

Every page is server-rendered and also available as JSON (pydantic models in `control/views.py`).

| Route                            | JSON                              | What                                                                                   |
| -------------------------------- | --------------------------------- | -------------------------------------------------------------------------------------- |
| `GET /`                          | `/api/fleet.json`                 | Fleet board: every image × live branch, last build result and age, released tag, upstream, drift (unknown until the scheduler), next check, skip flags; `helpers` lists the `docker-*` repos that are not images (docker-mods) with their branch build health. Live-refreshing. |
| `GET /runs`                      | `/api/runs.json`                  | Live runs with job-level state, elapsed and stuck detection; the last 50 finished runs. Live-refreshing. |
| `GET /runs/{id}`                 | `/api/runs/{id}.json`             | One run: jobs, steps timeline, linked builds.                                          |
| `GET /queue`                     | `/api/queue.json`                 | Queued and in-progress jobs, concurrency in use against `CONTROL_CONCURRENCY_LIMIT`.   |
| `GET /pulls`                     | `/api/pulls.json`                 | Open PRs across the org with build status, quarantined image tag, review state, age.   |
| `GET /issues`                    | `/api/issues.json`                | Open issues with labels and age.                                                       |
| `GET /releases`                  | `/api/releases.json`              | Releases, newest first, with the build's reason and upstream version.                  |
| `GET /builds/{owner}/{repo}/{run_id}` | `/api/builds/{owner}/{repo}/{run_id}.json` | One build: envelope, bundle (version, per-platform result, tests, assets), jobs. The bundle's `test.report_url` and release notes link here (CONVENTIONS §10). |
| `GET /images/{name}`             | `/api/images/{name}.json`         | One image: live branches, every build, releases, badge snippet.                        |
| `GET /images/{name}/{tag}`       | `/api/images/{name}/{tag}.json`   | One release tag of an image (`latest`, `develop`…); README badges link here.           |
| `GET /scheduler`                 | `/api/scheduler.json`             | The decision log (filters `?image=`, `?kind=` and `?outcome=`) and the next 24 hours of checks. Live-refreshing. |
| `GET /management`                | `/api/management.json`            | The public action log and skip flags; the action forms for signed-in members.          |
|                                  | `/api/skips.json`                 | Active skip flags.                                                                     |
|                                  | `/api/actions.json`               | The public member-action log (last 100), newest first.                                 |
|                                  | `GET /api/me`                     | Who is signed in, for the website: `{authenticated, login, avatar_url, member, checked_at, csrf}`; never 401, anonymous is `authenticated: false`. Re-checks a stale membership. |
| `GET /status`                    | `/api/status.json`                | App configured/installed, Discord, `/events`, scheduler, row counts, last backfill and discovery, last 20 deliveries. |
| `GET /api/stream`                |                                   | Server-sent events: `{"type": "<table>", "key": ...}` whenever a handler changes a row, including `scheduler_decisions`, `actions`, `skips` and `manifests`. |
| `GET /login`, `/login/callback`, `POST /logout` | | Member sign-in (below). `POST /api/logout` with `X-CSRF-Token` is the website's sign-out: `204`, cookie cleared. |
| `POST /manage/{rerun,cancel,dispatch,packages,skip,unskip,reconcile}` | `POST /api/actions/{same}` | Member actions (below): the HTML form, or a JSON body with the same fields plus the `X-CSRF-Token` header, answering `{ok, action_id, detail}`. |
| `GET /edit/authorize`            |                                   | The website editor's own authorisation (below): a signed-in member only, through the GitHub App's OAuth client, back to `next`. |
|                                  | `GET /api/editor/status`          | The visitor as an editor: `{authenticated, member, login, can_edit, authorized, repo, base}`. `can_edit` (push access to the site repo, read with the installation token) decides the site's buttons; never 401. |
|                                  | `GET /api/editor/pending`         | Every open edit in the site repo, by anyone: `{edits: [{path, kind, key, branch, owner, change: new/edit/delete, title, head, updated_at, pull}]}`. Members only; cached 30 s. |
|                                  | `GET /api/editor/entry?path=&branch=` | One docs page or post as the editor opens it, from the named edit branch (anyone's: joining it) or the member's own: content, `head`, images, videos, the PR, other open edits. `428` until the editor is authorised. |
|                                  | `GET /api/editor/removal?path=&branch=` | What deleting an entry would remove, for the confirmation. |
|                                  | `POST /api/editor/{upload,save,publish,delete,discard}` | Upload a file as a blob; commit to the edit branch; open its PR; delete the entry in a commit and PR; or close the PR and delete the branch. `X-CSRF-Token`; writes answer `{ok, action_id, detail, …}`. `POST /api/editor/revoke` forgets the authorisation. |
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
| `skipped`   | A skip flag matches; a run of the branch is in progress; a dispatch for the branch happened in the last 2 hours (cooldown, whatever triggered it); `upstream.version.type` is `os` (those build on the package check); the type is `command` or `deb_repo` (arbitrary shell, or docker, which control will not run: they build on the package check too); or `require_assets` are not published yet; or the branch has no Release and its last build of the head failed or was cancelled less than 24 hours ago (retried daily, not every cooldown). |
| `error`     | The resolver or the dispatch failed. After 6 errors in a row for one branch Discord gets one red embed naming the image, branch and error, linking `${SITE_URL}/fleet/scheduler/?image=<image>`; a success resets the streak. |

**Release check**: in the same hourly pass, after the upstream check, every live branch is checked for a
head that was never released. A package-check run commits `Update package versions` and then dispatches the
release build itself; when that dispatch fails, the branch head stays unreleased for good, because the
upstream check sees no version drift. The check reads the head sha (`GET /repos/{r}/branches/{b}`) and the
commit the branch's newest Release tag points at (`GET /repos/{r}/git/ref/tags/{tag}`; an annotated tag is
followed to its commit) and dispatches `reason: rebuild` when they differ, recording both shas. A head
whose subject ends with `[skip ci]` is compared with the released commit
(`GET /repos/{r}/compare/{released}...{head}`): when every changed file is one the build generates
(`README.md`, `AGENTS.md`, `LICENSE`, `.github/workflows/build.yml`) the outcome is `unchanged` with
"generated files only"; any other file means a release is owed. Nothing is dispatched while a skip flag
matches, a run of the branch is queued or in progress, the branch's last run is younger than 15 minutes
(the Release webhook or the build's follow-up commit may still be on the way), the 2-hour cooldown holds, or
the branch has no Release yet (the upstream check builds the first one). Dev branches are never considered:
only the live branches in `manifests` are walked.

**Package check**: every live branch whose manifest does not set `build.package_check: false`, once a week
at an hour of the week fixed by the same hash, dispatches `reason: packages` unless a skip flag matches or a
run is in progress. Every schedule is "due" when the slot has passed and no decision of that kind was
recorded since, so a restart never double-fires and a missed slot runs at the next tick (every 60 s). The
hourly checks run at the first tick for a branch that was never checked; the weekly one counts from the
moment discovery first stored the branch, so a fresh start (or a new branch) waits for its own slot instead
of firing the whole fleet's package checks at once. `python -m control schedule --once --image X --branch Y
--kind packages` forces one when waiting is not wanted.

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
is https, `SameSite=None` when `CONTROL_ALLOWED_ORIGINS` names an external origin (else `Lax`), and lives 7
days; the member flag is re-checked hourly on `/management`, on `/api/me` and before every action. Sign-in needs no OAuth scopes and no "Request user authorization (OAuth) during installation".

**The GitHub App needs the callback URL**: `${CONTROL_PUBLIC_URL}/login/callback` under *Identifying and
authorizing users → Callback URL* in the app's settings. New apps get it from the manifest
(`callback_urls`); an app created before this increment must have it added by hand, or GitHub answers
`redirect_uri is not associated with this application`.

Actions are POST forms carrying the session's CSRF token; anyone not signed in gets `401`, a non-member or a
bad token `403`, more than 30 actions in an hour per member `429`. Every attempt that passes those checks is
written to `actions` (actor, action, target, result, detail) and shown on `/management` and `/api/actions.json`.
The same seven actions exist as `POST /api/actions/{action}` with a JSON body of the same fields (the website
uses these; see below); both route families run one `prepare_*` function per action in `control/manage.py`.

| Action      | Form fields              | What                                                                        |
| ----------- | ------------------------ | --------------------------------------------------------------------------- |
| `rerun`     | `run_id`                 | `POST …/actions/runs/{id}/rerun-failed-jobs`; on `/runs/{id}` for failed runs. |
| `cancel`    | `run_id`                 | `POST …/actions/runs/{id}/cancel`; on `/runs/{id}` while a run is active.      |
| `dispatch`  | `repo`, `branch`, `reason` (`manual`, `rebuild`, `packages`, `upstream`) | `workflow_dispatch` of `build.yml`; recorded as a `manual` scheduler decision (so the cooldown sees it). |
| `packages`  | `repo`, `branch`         | A package check now; recorded as a `packages` decision, which resets that branch's weekly slot (so does `python -m control schedule --once --kind packages`). |
| `skip` / `unskip` | `scope`, `reason`  | Set or clear a skip flag.                                                   |
| `reconcile` |                          | Run fleet discovery now, in the background; the outcome shows on `/status`. |

## The website editor

People who may push to the site repo edit the documentation and the blog on the website: the pages
themselves carry the buttons (edit and delete on a page or post, new post on the blog, new page on a docs
section), and `/edit/` is the editor they open. Control does the GitHub side in `control/editor.py`, as the
member:

- **Authorisation.** Sign-in keeps nothing of the user's. Editing needs a second, one-off step,
  `GET /edit/authorize?next=<site url>`: the GitHub App's own OAuth client (never the classic OAuth App,
  whose `public_repo` scope would reach every repo the person can push to), back through the App's one
  callback, `/login/callback`, which sees the pending cookie's `purpose: edit` and hands over. The token must
  belong to the member who is signed in. It is stored in `editor_tokens`, Fernet-encrypted with a key derived
  from `CONTROL_SESSION_SECRET` (a new secret makes old tokens unreadable, and they are dropped), refreshed
  before it expires, dropped when GitHub answers 401, and revoked (`DELETE /applications/{client_id}/token`)
  on sign-out or `POST /api/editor/revoke`. It never leaves control and is only used on `SITE_REPO`.
- **Paths.** Only `src/content/docs/<section>/<slug>.md` and `src/content/posts/<slug>/index.md`; png, jpg,
  gif or webp images (5 MiB) beside the Markdown, and mp4 or webm videos (50 MiB, served as uploaded, never
  transcoded) in `public/media/<slug>/` for a post or `public/media/docs/<section>/<slug>/` for a page.
- **Branches and joining.** An edit is a branch, `edit/<login>/docs/<section>/<slug>` or
  `edit/<login>/posts/<slug>`, created from the commit the editor loaded. `/api/editor/pending` lists every
  open one with its PR and check state (two GraphQL calls with the installation token, cached 30 s), so the
  site shows them beside what they change; anyone who opens one joins it (`branch=`), and their saves go onto
  it, keeping one change in one pull request.
- **Commits.** Files go up one at a time as blobs (`upload`); a save is one commit with the git data API (a
  tree on the loaded commit, then a fast-forward of the branch), so a save made elsewhere in between is a
  `409`, never overwritten. Publishing opens the PR into the default branch; the site's `check.yml` builds it;
  merging it publishes. An entry whose branch was merged (and kept) starts again from the default branch the
  next time it opens. Discarding closes the PR and deletes the branch.
- **Deleting.** `delete` removes a post's whole folder, or a docs page and the images beside it that no other
  page in the folder uses, plus either one's media folder, in one commit and its PR (on the open edit when one
  is joined, retitled as a deletion). No redirects: after the merge the address is gone.
- **Limits and the log.** Reads need a member; writes need the CSRF token too. Edits have their own rate
  limit (120 an hour, apart from the 30 member actions) and every attempt is an `actions` row
  (`edit-authorize`, `edit-save`, `edit-publish`, `edit-delete`, `edit-discard`). GitHub's own permissions
  still apply: the site only shows the buttons to people with push access (`can_edit`).

## Driving control from the website

The site (`https://linuxserver-playground.github.io/site/`, static) renders every fleet page from the JSON
above and lets members act from there. Production will serve site and control from one registrable domain,
so **the session cookie is the one credential**; the playground origins differ (github.io vs duckdns.org),
and the team tests with Chromium, which still sends a `SameSite=None; Secure` cookie on credentialed
cross-site fetches. A bearer-token handoff was considered and dropped for now: it only becomes necessary for
browsers that partition or block third-party cookies (Safari), and it would reintroduce a token the page
script can read. The contract:

1. **CORS**: `CONTROL_ALLOWED_ORIGINS` lists the site origin(s). Those, and only those, get
   `Access-Control-Allow-Origin: <origin>` and `Access-Control-Allow-Credentials: true` on every route, with
   `GET`, `POST`, `OPTIONS` and the request headers `Content-Type` and `X-CSRF-Token`. Every fetch from the
   site sends `credentials: "include"`; `EventSource("…/api/stream", {withCredentials: true})` for the live
   feed (the stream is public, the flag just keeps one code path).
2. **Sign-in**: send the browser to `GET /login?next=<absolute url on an allowed origin>` (local paths keep
   working as before). After GitHub, `/login/callback` sets the cookie (`SameSite=None; Secure; HttpOnly`,
   7 days) and redirects straight back to `next`; anything not local and not on an allowed origin becomes `/`.
   Nothing is passed in the URL: no token, no fragment.
3. **Who am I**: `GET /api/me` answers `{authenticated, login, avatar_url, member, checked_at, csrf}` and is
   never a 401. The site renders the action buttons when `member` is true and keeps `csrf` in memory (it is
   per session, so it may be re-read from `/api/me` on every load; never store it in `localStorage`).
4. **Actions**: `POST /api/actions/{rerun|cancel|dispatch|packages|skip|unskip|reconcile}` with
   `Content-Type: application/json`, a body of the form's fields (`run_id`, `repo`, `branch`, `reason`,
   `scope`), and the header `X-CSRF-Token: <csrf>` (double submit: the cookie alone is ambient, the header is
   not). `200 {ok, action_id, detail}` whenever the attempt was logged (`ok: false` when GitHub or the check
   refused; `detail` says why); `400`/`404` for bad fields (`{"detail": …}`), `401` not signed in, `403`
   non-member or bad token, `429` rate limit. The `actions` SSE event follows, so the site refreshes the log.
5. **Sign-out**: `POST /api/logout` with `X-CSRF-Token`: `204` and the cookie is cleared.
6. **Data**: `/api/fleet.json`, `/api/runs.json`, `/api/runs/{id}.json`, `/api/queue.json`, `/api/pulls.json`,
   `/api/issues.json`, `/api/releases.json`, `/api/builds/{owner}/{repo}/{run_id}.json`,
   `/api/images/{name}.json`, `/api/images/{name}/{tag}.json`, `/api/scheduler.json?kind=&image=&outcome=`,
   `/api/skips.json`, `/api/actions.json`, `/api/status.json`; `/api/stream` for refreshes. Models are the
   pydantic classes in `control/views.py`.

Local development of the site against a local control: run control with `CONTROL_PUBLIC_URL=http://…`
and the dev server's origin in `CONTROL_ALLOWED_ORIGINS`; the cookie falls back to `SameSite=Lax` on plain
http (browsers refuse `SameSite=None` without `Secure`), which works as long as both run on `localhost`.

## Events contract

The shared workflow POSTs a control-event (`schemas/control-event.schema.json` in
[builder](https://github.com/linuxserver-playground/builder), CONVENTIONS §10) to `/events` with
`X-Control-Signature: sha256=<hex>`, the HMAC-SHA256 of the raw body under `CONTROL_EVENTS_SECRET`.
control answers `503` when the secret is unset, `401` on a bad or missing signature, `400` with the list of
schema violations, and `202` otherwise. The body is stored in `deliveries` as `control:<type>` (keyed by
its hash, so a retried POST is a no-op) and replays with the webhooks.

- `build.started` creates a provisional `builds` row (result `null` = running). For `mode: live` one Discord
  embed is posted and its message id kept on the row (not for a package check).
- `build.summary` fills the row (result, failed job, version, upstream, platforms, test outcome, release or
  artifact URL, the whole bundle as `bundle_json`) and edits the started message into the summary, or posts
  one for dev/pr builds. A package check (`reason: packages` on a live branch, which never releases) is only
  announced when it fails: what it found matters through the release build it dispatches, announced on its own.

### Discord embeds

One embed per build message, the same shape while running and once finished, so the edit reads well. The
website is the single pane of glass, so the embed links there: a released live build's title opens its build
page `${SITE_URL}/images/<image>/builds/<tag>/` (also the "CI report" link) and the image name above the title
opens `${SITE_URL}/images/<image>/`. A finished build without a website page opens the report GitHub shows
(also the "CI report" link): a pr build its pull request's report comment (looked up by the comment's
`<!-- lsio-build-report -->` marker; the first build of a pull request finishes before the comment exists, so
every completed `PR report` run re-links the latest message of each recent pull request build of its repo),
any other build its run page scrolled to the Publish job's summary (`#summary-<job id>`, from the jobs
control holds; the run page until that job's webhook arrived), which opens with the same report. A running
build opens the control run page.

- Title `<image> <tag> · <dot> <result>`, the dot in the embed's colour, the Jenkins-era scheme: green
  (`0x19A719`) `passed` for a live success, dark green (`0x3C6124`) for a dev/pr success, red (`0xFF0000`)
  `failed` for a live failure, dark red (`0xC15253`) for a dev/pr failure, grey `skipped`/`cancelled`, amber
  `running` or held at "publishing".
- Description, one line: `mode · reason`, then `PR #n`, the failed job, and for a released build whose bundle
  names `previous.tag` a build control holds: `packages changed (amd64 +3)` (table hash per arch against the
  previous release; the delta is in package count) or `packages unchanged`.
- Fields in two columns: **Platforms** (per arch: short digest, compressed size in MiB, tests passed/failed,
  or the arch's own failure), **Upstream** (the version linked to `version.source_url`, with the source
  type), **Timing** (build time per arch, `started → finished (elapsed)`), **Links** (CI report, run, release,
  artifact, PR, control). Footer `<repository> · run <id>`. No thumbnail.
- Every part is clipped to Discord's limits (title 256, description 4096, field value 1024, 25 fields, 6000
  in all) with an ellipsis, so a long error can never bounce the message.

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

> Sign-in needs the organisation permission **Members: read** on the App (it is in the manifest for new apps). On an app created before it was added, grant it under the App's *Permissions & events* and then accept the updated permissions on the installation; until then every user is treated as a non-member.

## Publication hold

A released build's website page appears minutes after the release: the hub ingests it and deploys, then the site
refreshes and deploys, longer when a burst of releases queues. So the build's Discord message is edited into the
summary as "passed, publishing", linking control instead of a page that would 404, and `control/publish.py` checks
every held build's page once a minute. When it answers the message becomes the final summary with its CI report
link. Past `CONTROL_PUBLISH_DEADLINE_MINUTES` the message turns into "not on the site" and a separate post (an edit
does not notify) says where it is stuck, read from the hub's own data: the hub has not ingested the release, or the
hub has it and the site has not published it, with the runs to look at. A page that turns up later is announced
with a short follow-up; after `CONTROL_PUBLISH_EXPIRY_HOURS` control stops watching and says so.
