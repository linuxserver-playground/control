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
once (see Backfill); `python -m control discover` reads every live branch's `image.yml` and every mod's
`mod.yml` once (see Scheduler);
`python -m control schedule --once [--image x] [--branch y] [--kind upstream|release|packages|mod] [--dry-run]`
runs one check now and prints one line per branch: the upstream check by default (recorded as `manual`, so
the hourly slot is untouched), the release check, or `--kind packages`, which forces a package check and is
recorded as one, so it resets that branch's weekly slot (`--image` is required for it); `--kind mod` (or
`--image mods`) runs the mod check of every scheduled mod, or of `--branch`, as `manual`. `--dry-run` resolves
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
| `CONTROL_DISCORD_VERBOSE` | `false`                   | Also post the quiet scheduler embeds (grey): a `rebuild` dispatched for a branch head that was never released, and a skip flag holding a branch back. Build embeds, the error-streak alert and the missing-assets alerts post regardless. |
| `CONTROL_EVENTS_SECRET` | unset                       | Shared with the `workflows` repo; signs the events POSTed to `/events`. Unset means `/events` answers 503. |
| `CONTROL_BACKFILL_ON_START` | `false`                 | `true` runs one GitHub API backfill in the background after boot (once the app is configured and installed). |
| `CONTROL_CONCURRENCY_LIMIT` | `20`                    | GitHub's concurrent job limit for the org; the queue page measures in-progress jobs against it. |
| `CONTROL_DELIVERIES_KEEP_DAYS` | `7`                  | Webhook deliveries older than this are deleted daily (see *Self-healing*, housekeeping); `0` keeps them all. |
| `CONTROL_SNAPSHOTS_KEEP` | `2`                       | Daily compacted copies of the database kept in `<data dir>/snapshots/`; `0` turns them off. |
| `CONTROL_RUN_ARCHIVE` | `true`                        | Attach `run.json` and `logs.zip` to every live release once its run completes (see *Run archive*). |
| `CONTROL_RUN_ARCHIVE_PER_PASS`, `CONTROL_RUN_ARCHIVE_ATTEMPTS`, `CONTROL_RUN_ARCHIVE_MAX_LOGS_MB` | `3`, `8`, `500` | Runs archived per minute at most; tries before giving up on one (backing off 5 minutes to 6 hours); the largest log archive attached. |
| `CONTROL_PAGES_CHECK` | `true`                        | Warn once about every image repo without a GitHub Pages site and list them in the discovery record (see *Pages on image repos*). Read-only. |
| `CONTROL_HEAL_PAGES`  | `true`                        | Redeploy an image repo's Pages site when a deploy failed, with backoff (see *Self-healing*). |
| `CONTROL_HEAL_PAGES_BACKOFF_MINUTES`, `CONTROL_HEAL_PAGES_ATTEMPTS` | `5,30,120,1440`, `4` | The wait before each Pages retry (csv; the last step repeats) and the retries before control gives up and reports. |
| `CONTROL_HEAL_RERUNS` | `true`                        | Re-run the failed jobs of a live build once when only an infrastructure step failed (see *Self-healing*). |
| `CONTROL_HEAL_RERUN_DELAY_MINUTES`, `CONTROL_HEAL_RERUN_WINDOW_HOURS` | `5`, `6` | How long after the failure the re-run goes out; how long control may wait for free slots before it gives up and reports. |
| `CONTROL_HEAL_PER_TICK` | `3`                         | Heal actions (Pages dispatches and re-runs together) started per minute at most. |
| `CONTROL_SCHEDULER`   | `true`                        | Run fleet discovery and the scheduler loop beside the server. `false` keeps the pages and member actions and stops dispatching. |
| `CONTROL_BATCH_LIVE`  | `wheelie`                     | Batch build projects control dispatches for real (csv; empty for none). The others are sized and recorded as dry runs while their own cron still fires. |
| `CONTROL_BATCH_RESERVE` | `4`                         | Job slots a batch run never sizes into, kept for image, hub and site builds. |
| `CONTROL_BATCH_MIN_PARALLEL`, `CONTROL_BATCH_MAX_PARALLEL` | `2`, `16` | The narrowest and widest batch run control starts. |
| `CONTROL_SESSION_SECRET` | unset                      | Signs the sign-in cookie (HMAC-SHA256). **Set it** (`openssl rand -hex 32`): unset means a random secret per process, logged as a warning, and every restart signs everyone out. |
| `CONTROL_ALLOWED_ORIGINS` | `https://linuxserver-playground.github.io` | Comma-separated web origins (`scheme://host[:port]`, never `*`) allowed to drive the JSON API from the browser: CORS with credentials, `next` after sign-in may point there, and the session cookie becomes `SameSite=None; Secure`. Empty turns all three off. |

## Routes

Every page is server-rendered and also available as JSON (pydantic models in `control/views.py`).

| Route                            | JSON                              | What                                                                                   |
| -------------------------------- | --------------------------------- | -------------------------------------------------------------------------------------- |
| `GET /`                          | `/api/fleet.json`                 | Fleet board: every image × live branch, last build result and age, released tag, upstream with `upstream_url` (its release, commit or package page when the source names one), drift (unknown until the scheduler), next check, skip flags (`skip`: the scopes; `skips`: each flag with its reason, who set it and when); `helpers` lists the `docker-*` repos that are not images (docker-mods) with their branch build health and each failing branch's run (`failures`). Live-refreshing. |
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
|                                  | `GET /api/me`                     | Who is signed in, for the website: `{authenticated, login, avatar_url, member, checked_at, csrf}`; never 401, anonymous is `authenticated: false`. Read from the session cookie; actions confirm membership live. |
| `GET /batch`                     | `/api/batch.json`                 | Batch build projects: runner capacity now (`running`, `waiting`, `held`, the slots each batch run holds, `free`), and per project its schedule, live or dry run, the `parallel` a run started now would get, the active run, the last decision and dispatch (with its run), the last 10 evaluated runs and the units failing now; the last 50 `batch` decisions. Live-refreshing. |
| `GET /status`                    | `/api/status.json`                | Version and the commit the running image was built from (`revision`), app configured/installed, Discord, `/events`, scheduler, row counts, last backfill and discovery, last 20 deliveries; `healing`: the Pages sites control is retrying or gave up on and the failed runs it will re-run or has re-run. |
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
response. The scheduler owns `manifests` (one row per live branch), `mod_manifests` (one row per docker-mods
branch), `scheduler_decisions` and `skips`;
member actions write `actions`; the run archive writes `run_archives` (one row per released run it has tried) and
the Pages check `repo_pages` (one row per image repo reported for having no Pages site, removed once it has one);
self-healing `heal_pages` (one row per image repo whose Pages deploy failed) and `heal_reruns` (one row per failed
live build run it judged).

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
| `skipped`   | A skip flag matches; a run of the branch is in progress; a dispatch for the branch happened in the last 2 hours (cooldown, whatever triggered it); `upstream.version.type` is `os` (those build on the package check); the type is `deb_repo` (it needs docker, which control does not have: it builds on the package check too); the type is `command` and control runs without its sandbox (a checkout rather than the image); or `require_assets` are not published yet (the release and package checks of the branch hold too, and a held package check is tried again next hour, not next week; every such check posts to Discord, amber for the first four and red from the fifth on, with the pattern that failed and the assets that were uploaded: a streak that long usually means upstream renamed the asset and the pattern needs updating); or the branch has no Release and its last build of the head failed or was cancelled less than 24 hours ago (retried daily, not every cooldown). |
| `error`     | The resolver or the dispatch failed. After 6 errors in a row for one branch Discord gets one red embed naming the image, branch and error, linking `${SITE_URL}/fleet/scheduler/?image=<image>`; a success resets the streak. |

**Release check**: in the same hourly pass, after the upstream check, every live branch is checked for a
head that was never released. A package-check run commits `Update package versions` and then dispatches the
release build itself; when that dispatch fails, the branch head stays unreleased for good, because the
upstream check sees no version drift. The check reads the head sha (`GET /repos/{r}/branches/{b}`) and the
commit the branch's newest Release tag points at (`GET /repos/{r}/git/ref/tags/{tag}`; an annotated tag is
followed to its commit) and dispatches `reason: rebuild` when they differ, recording both shas. A head
whose subject ends with `[skip ci]` is compared with the released commit
(`GET /repos/{r}/compare/{released}...{head}`): when every changed file is one the build commits itself
(`README.md`, `AGENTS.md`, `LICENSE`, `.github/FUNDING.yml`, `root/donate.txt`, the two workflows, and
`package_versions.txt`, which every release commits afterwards) the outcome is `unchanged` with
"generated files only"; any other file means a release is owed. Nothing is dispatched while a skip flag
matches, a run of the branch is queued or in progress, the branch's last run is younger than 15 minutes
(the Release webhook or the build's follow-up commit may still be on the way), the 2-hour cooldown holds, or
the branch has no Release yet (the upstream check builds the first one). Dev branches are never considered:
only the live branches in `manifests` are walked.

**Package check**: every live branch whose manifest does not set `build.package_check: false`, once a week
at an hour of the week fixed by the same hash, dispatches `reason: packages` unless a skip flag matches or a
run is in progress. That one run compares the fresh package table with the committed one and releases only when
it changed; every live release commits the table afterwards as `Update package versions [skip ci]`, which the
release check (above) treats like the generated files: no rebuild for it. Every schedule is "due" when the slot has passed and no decision of that kind was
recorded since, so a restart never double-fires and a missed slot runs at the next tick (every 60 s). The
hourly checks run at the first tick for a branch that was never checked; the weekly one counts from the
moment discovery first stored the branch, so a fresh start (or a new branch) waits for its own slot instead
of firing the whole fleet's package checks at once. `python -m control schedule --once --image X --branch Y
--kind packages` forces one when waiting is not wanted.

Dispatch is always `POST /repos/{repo}/actions/workflows/build.yml/dispatches` with `ref: <branch>` and
`inputs: {reason}`: `repository_dispatch` only ever runs the default branch, and one path is simpler. The run
itself arrives by webhook; the scheduler page links a `dispatch` decision to the `workflow_dispatch` run that
appeared on the branch within 15 minutes.

**Pages deploys are not builds.** `reason: pages` (a member dispatch, or `repository_dispatch` with
`client_payload.reason`) runs only the image workflow's `pages` job, `build / Pages`, in its own concurrency group
(`build-<ref>-pages`): it redeploys the repo's Pages site from its Releases, with Prepare, the arch builds and
Publish skipped, no `/events` and no release. GitHub tells control neither a run's inputs nor its concurrency
group, and its `display_title` is just `Build`, so control tells such a run by its jobs: a `Pages` job that is not
skipped and no Prepare job that ran (a build is the other way round; a run whose jobs have not arrived counts as a
build). The scheduler's in-flight, settle and failed-initial-build checks, the helper repos' branch health and the
decision-to-run link leave Pages deploys out, and a pages dispatch neither starts nor resets a branch's cooldown.
With no build event there is no builds row, so no fleet result, Discord build message, publication hold or run
archive. A failed `Pages` job is a warning in the log (`pages deploy failed on <repo> run <id>: <job url>`) and
a red run on the runs page, and control redeploys the site with backoff (see *Self-healing*); its stuck threshold is
the default hour, not its own short history.

**Mod check** (`control/mods.py`): the Jenkins External-Triggers jobs, which looked up a mod's tool version every
hour and started its build when Docker Hub had no `mods:<branch>-<version>` tag. Discovery also reads every
`docker-mods` branch's `mod.yml` in one GraphQL query into `mod_manifests` (a head that has not moved keeps its
row; an invalid file is skipped with a warning). A `push` webhook to a docker-mods branch that creates or deletes
it, or changes its `mod.yml`, re-reads that one branch at once (a GraphQL query for the ref), as a push does for an
image's `image.yml`: a new branch appears, a deleted one or one whose `mod.yml` went away disappears. A mod is scheduled when its `mod.yml` has an `upstream` block,
it is not deprecated, and the branch is the one that publishes it (`<target>-<name>`). Once an hour, at the
minute `sha256(<org>/docker-mods/<branch>)` gives it, the check resolves `upstream.version` with the same
vendored resolver (mod.yml reshaped as `{upstream, build.platforms}`, as the build's plan job does) and asks
GHCR, with an anonymous pull token, whether `ghcr.io/<org>/mods:<branch>-<version>` exists. It is recorded as
image `mods`, branch `<branch>`, kind `mod`: `unchanged` when the tag exists; `dispatch` (`build.yml` on the
branch with `inputs: {mod_version}`, the resolved version) when it does not; `skipped` for a skip flag
(`mods`, `mods/<branch>`, `mods/<branch>/<version>`), a run in progress, the cooldown, a source control will
not check for a mod (`os`, `command`, `deb_repo`, also inside `combined`), or a dispatch of the same version less than a day
ago whose build failed (retried daily); `error` for the resolver or the registry. The build itself passes the
version as `MOD_VERSION` and tags `<branch>-<version>` (CONVENTIONS §3, "Mods"), so a push that already built
the version leaves the next check `unchanged`.

A mod with `upstream.lines` (Go's previous release and its release candidate beside the newest) has a version per
line. The check resolves every line, the default one first, and dispatches the first whose tag is missing with
`inputs: {mod_version, line}` (`line` empty for the default line; only such a branch's `build.yml` has that
input). A version two lines share is the earlier line's. A run in progress holds the whole branch, so one build of a
mod runs at a time (the branch's `build.yml` concurrency group would cancel a second pending run); when that run
completes, the `workflow_run` webhook checks a mod with lines again at once, so the next missing line goes out
within a minute rather than at the next hourly slot. The cooldown and the daily retry are per version, so per
line. Each check is still one decision: the dispatched line's (`line`), else the first error, skip, or the
default line's, with every line's `{line, upstream, state}` under `lines`.

**Skip flags** (`skips` table, `/api/skips.json`) replace the old `SKIP_EXTERNAL_TRIGGER` org variables. A
scope is `image` (every branch), `image/branch`, or `image/branch/version` (only while upstream resolves to
that sanitised version; the next version builds again). Members set and clear them on `/management`, the
fleet board and image pages; each flag keeps who set it and why. The fleet board shows `upstream`,
`released`, `drift` (`ahead`, `same`, `unknown`), `next_check` and the matching flags per branch.

## Batch build projects

`control/batch.py` starts the org's batch build projects from the same loop: repositories that are not images
but run wide matrices on the same 20 job slots. The list is code (`PROJECTS`), not discovered:

| Project | Workflow | Slot | `parallel` cap |
| --- | --- | --- | --- |
| `proot-apps` | `package-check.yml` on `master` (it publishes the apps whose packages changed itself; a pass in `build-on-commit.yml` clears an app's failure) | daily 00:00 UTC | 16 |
| `selkies-layers` | `build.yml` on `main` | daily 04:30 UTC | 10 |
| `wheelie` | `build.yml` on `main`, only when the gap check finds a missing wheel | hourly at :20 | 12 |

**Sizing.** `parallel = clamp(CONCURRENCY_LIMIT - BATCH_RESERVE - busy, MIN_PARALLEL, min(MAX_PARALLEL, cap))`.
`busy` is the jobs in progress outside the batch projects, the jobs queued outside them (an image build waiting
for a runner), and for each batch project with a run going the width it was started with or its jobs in
progress, whichever is more (a fresh dispatch holds its width for 15 minutes before its run appears). Queued
jobs of the batch projects are never counted: a run holds most of its matrix back with `max-parallel`, and
GitHub shows those as queued. Jobs come from the `workflow_job` webhooks; open jobs older than GitHub's limits
(6 hours running, 24 hours queued) or in finished runs are lost webhooks and are ignored.

**Slots.** At its slot a project is skipped while a run of its own is still going, and deferred while fewer than
`MIN_PARALLEL` slots are free; both look again every 10 minutes for up to 3 hours (30 minutes for wheelie). When
that window closes a busy fleet still gets a run at the minimum, so a batch always progresses; a run of its own
still going skips the slot. A slot missed for longer than that window (control down or just deployed) waits for the next one.
wheelie's gap check is `control/vendor/wheelie_gaps.py`, wheelie's `scripts/gaps.py` copied verbatim (the header
names the commit; `tests/test_vendor.py` fails on drift), fed `distros.json`, `packages.txt` and the index pages
from the repo at `main` and PyPI over HTTP. No gap records `unchanged`; the same gaps whose build failed wait a
day. Every outcome is a `batch` decision (`image` is the repo, `branch` the ref), with `parallel`, the capacity
snapshot, the gaps and `retry_at` in its detail. Only projects in `CONTROL_BATCH_LIVE` are dispatched; the
others record `dispatch` with `dry_run: true`. `python -m control batch --once [--project X] [--dry-run]` checks
now, without waiting for a busy fleet.

**Discord.** Every finished batch run (push, cron or dispatch) is evaluated once into `batch_runs`; each job is
a unit (a selkies `<distro>-<layer>-<arch>`, a proot-apps app, a wheelie `<distro> <arch>` with the packages its
gap check found) with its last state in `batch_units`. A red embed names the units that went from passing (or
unknown) to failing, with links to their jobs and how many were already red; a green one names the units that
pass again. A unit that stays red is silent, and successful runs post nothing. Runs that finished before control
first watched are learnt silently. Also reported, once each: a dispatch GitHub refused (red, at once) or gap
checks failing 6 times in a row (red), a dispatched run that never appeared within 15 minutes (red), a run still
queued after an hour or running past twice its usual length (p95 of recent successes, at least 3 hours; 12 hours
without history) (amber). Deferrals for a busy fleet post nothing.

## Member sign-in and actions

`/login` sends the browser to GitHub's OAuth authorisation; `/login/callback` exchanges the code for a user token
and drops it after reading who signed in and whether they are a member, which must answer `state: active`. With
the classic OAuth App (`OAUTH_CLIENT_ID`/`OAUTH_CLIENT_SECRET`, scopes `read:user read:org`; the playground's
setup) the user's token makes both calls, `GET /user` and `GET /user/memberships/orgs/{org}`, so the GitHub App
needs no Members permission. Without an OAuth App the GitHub App's own OAuth client signs in and the user's token
is used only for `GET /user`; membership is then read with **our** installation token,
`GET /orgs/{org}/memberships/{login}`, which needs the App's organisation permission Members: read. The session
cookie (`control_session`: login, avatar, member flag, when it was
checked, a CSRF token) is HMAC-signed with `CONTROL_SESSION_SECRET`, `HttpOnly`, `Secure` when the public URL
is https, `SameSite=None` when `CONTROL_ALLOWED_ORIGINS` names an external origin (else `Lax`), and lives 7
days. Pages (`/management`, `/api/me`, the editor's reads) show what the cookie says and never call GitHub.
Every action and every editor save confirms membership first, live, with the installation token (one call to
`GET /orgs/{org}/memberships/{login}`, which needs the App's Members: read), so someone who leaves the org can
do nothing from that moment, whatever their cookie says. When GitHub cannot answer, the action is refused with
503 rather than run on the old verdict. Sign-in never needs "Request user authorization (OAuth) during installation".

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
| `dispatch`  | `repo`, `branch`, `reason` (`manual`, `rebuild`, `packages`, `upstream`, `pages`: redeploy the Pages site only) | `workflow_dispatch` of `build.yml`; recorded as a `manual` scheduler decision (so the cooldown sees it). |
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
   `/api/batch.json`, `/api/skips.json`, `/api/actions.json`, `/api/status.json`; `/api/stream` for refreshes. Models are the
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
  one for dev/pr builds. A package check that released nothing (`reason: packages` on a live branch, the
  installed packages unchanged) is only announced when it fails; one that found a change releases from the same
  run and is announced like any release.

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

> The run archive uploads Release assets with **Contents: write**, which the App has. The Pages check needs no
> more than the metadata every App has: control never enables Pages or changes environments.

> The manifest asks for one organisation permission, **Members: read**, for the membership check before every
> action (see *Member sign-in*): without it every action is refused with 503.
> Sign-in itself reads membership with the user's own token through the classic OAuth App. On an app created
> before the manifest asked for it, grant it under the App's *Permissions & events* and accept it on the
> installation. The playground's installation has it.

## Publication hold

A released build's website page appears minutes after the release: the hub ingests it and deploys, then the site
refreshes and deploys, longer when a burst of releases queues. So the build's Discord message is edited into the
summary as "passed, publishing", linking control instead of a page that would 404, and `control/publish.py` checks
every held build's page once a minute. When it answers the message becomes the final summary with its CI report
link. Past `CONTROL_PUBLISH_DEADLINE_MINUTES` the message turns into "not on the site" and a separate post (an edit
does not notify) says where it is stuck, read from the hub's own data: the hub has not ingested the release, or the
hub has it and the site has not published it, with the runs to look at. A page that turns up later is announced
with a short follow-up; after `CONTROL_PUBLISH_EXPIRY_HOURS` control stops watching and says so.

## Run archive

The website renders build pages only for the newest builds of a branch; an older build is its GitHub Release,
and GitHub deletes run logs after 90 days. So once a released live run has completed, `control/archive.py`
attaches two assets to its Release with the installation token:

- `run.json`: the run's structured record from the REST API, never log text. `schema` (1), `archived_at`,
  `repository`, `release` (`tag`, `url`), `build` (`image`, `branch`, `mode`, `version_tag`, `release_tag`),
  `run` (`id`, `name`, `path`, `run_number`, `run_attempt`, `html_url`, `display_title`, `event`, `status`,
  `conclusion`, `created_at`, `run_started_at`, `updated_at`), `trigger` (`event`, the build's `reason`,
  `actor`, `triggering_actor`), `head` (`branch`, `sha`, `message`, `timestamp`, `author` name) and `jobs`, each
  with `id`, `name`, `html_url`, `run_attempt`, `status`, `conclusion`, `created_at`, `started_at`,
  `completed_at`, `runner_name`, `runner_group_name`, `labels` and `steps` (`number`, `name`, `status`,
  `conclusion`, `started_at`, `completed_at`). Field names are GitHub's.
- `logs.zip`: `GET /repos/{repo}/actions/runs/{id}/logs` as GitHub serves it (the redirect to signed storage is
  followed without the token). The logs of these public repos are already public and masked by GitHub when
  written; before attaching them control still checks every line for GitHub token formats (`ghp_`, `gho_`,
  `ghs_`, `ghu_`, `ghr_`, `github_pat_`), JWTs, `Authorization:` headers carrying a credential (not `***` or an
  unexpanded `$VAR`), Discord webhook URLs and the exact events secret. On any match the logs are not attached
  (`run.json` still is), and the log line names the kind of match and the file, never the match.

A run is matched to its Release through its `build.summary` (`/events`): a live build with `result: success`, a
version tag and a `release_url`, whose tag (the end of that URL) names the Release. It is archived once its
`workflow_run` has completed, or an hour after the summary should that webhook never arrive (control reads the
run's status itself). The completion webhook wakes the archiver (30 seconds later, so the logs are ready);
otherwise it sweeps every minute, newest first and `CONTROL_RUN_ARCHIVE_PER_PASS` runs at a time, which is also
how releases from before the archive existed get theirs. Assets already on the Release are left alone. Logs not
ready, a GitHub error or a network failure are retried with backoff; after `CONTROL_RUN_ARCHIVE_ATTEMPTS` control
gives up. Each give-up and each withheld `logs.zip` has its own log line, and Discord hears of them grouped: one
summary at most an hour (the first at once, later ones at the hour), with the count, the first five repos and tags
linking their runs, and the most common reason (control's own note: an HTTP status, an error class, the kind of
credential matched; never log text). A systematic failure, such as every upload answering 403, is one message an
hour rather than one per run. `run_archives.reported_at` records which runs a summary has named.
Logs GitHub has deleted (`410`, or `404` past the 90 days) leave `run.json` alone on the
Release. The outcome per run is in `run_archives` (`done`, `retry`, `failed`, `skipped` when the Release is gone;
`logs` says `uploaded`, `present`, `withheld`, `expired`, `too-large` or `unreadable`).

## Pages on image repos

Each image's build deploys its heavy files (file trees, package lists, test files) to its own repository's GitHub
Pages, so an image repo without a Pages site builds and releases but those files never show on the website.
Enabling Pages (source: GitHub Actions) and opening the `github-pages` environment to every branch is a
maintainer's step of the new-image recipe; control holds no permission for it and changes nothing. Fleet discovery
reads `has_pages` from the org's repository list it already fetches, and `control/pages.py` reports every image repo
(not archived, `image.yml` on its default branch) without a site: one log line and one Discord post, "<repo> has no
GitHub Pages site: its build files will not show on the website. Enable it as the new-image recipe says.",
recorded in `repo_pages` so it is not repeated every 30 minutes. The row goes when `has_pages` turns true, so a
repo that loses its site again is reported again. Every discovery lists the repos still without one in its record
(`last_discovery.pages_missing` in `/api/status.json`). `CONTROL_PAGES_CHECK=false` turns it off.

## Self-healing

GitHub loses webhooks, answers 500s, drops runners and flakes on clones and registry calls; nobody should babysit
that. Control repairs what it can and reports only what stays broken.

**Lost job webhooks** (`control/heal.py`): whenever a run completes, every five minutes from the scheduler's loop,
and once at start, control reads the jobs of every completed run that still holds an unfinished job from the API, so
no job shows as running forever, and every run left unfinished for five minutes with no job running. The loop's heal
is for quiet hours, when the one lost completion has no other run's completion to trigger it.

**Reconcile** (`control/reconcile.py`): the backstop for every lost webhook, every 30 minutes and after every start,
since a restart is when deliveries are dropped. It asks GitHub to redeliver each delivery since the previous pass
that never reached control (from the App's delivery log, with the App's JWT; three tries at most per delivery), reads
every run control holds as unfinished again with its jobs (dropping one GitHub no longer has, skipping one it cannot
answer for), reads every open pull request and issue again, and adds the org's open ones it lacks (two searches). The
pass is `last_reconcile` on `/status`. A pass that redelivered anything or hit an error is followed by another after
five minutes: GitHub is dropping or refusing things right now, and that is when the backstop earns its keep. Control
never depends on a restart's backfill to be right.

**Degraded GitHub** (`control/github.py`): every read is asked again after a 502, 503, 504 or a dropped connection
(twice, 1 s then 4 s), the backfill's too. Writes never are: a dispatch that answered 502 may still have happened.
During GitHub's 2026-10-30 incident one 502 in a few hundred requests ended whole reconcile and backfill passes.

**Housekeeping** (`control/housekeeping.py`), daily: deliveries older than `CONTROL_DELIVERIES_KEEP_DAYS` are deleted
(the reconcile and backfill cover anything older than a replay would rebuild), and a compacted copy of the database
goes to `<data dir>/snapshots/` (`CONTROL_SNAPSHOTS_KEEP` newest), unless the disk has less free than twice the
database. Snapshots guard against a bad migration or a corrupted file, not against losing the disk. Intake stores only
the events a handler reads (`handlers.STORED_EVENTS`); others, such as the `check_run` and `check_suite` an older App
still subscribes to, are answered and dropped. `/status` shows the database's size, the free disk and the last pass.

**Failing batch units that no longer exist** (`control/batch.py`): a unit's failure is dropped once its workflow
has run for three days without building it (an app disabled or removed, a layer dropped, a wheel PyPI now ships).

**Pages deploys** (`control/recover.py`): a deploy that failed leaves the previous site up, stale, and repeating one
is always safe. Control sees a failure from a `reason: pages` run's failed `Pages` job, a live Publish whose
`Pages (live)` step failed, or a `build.summary` whose `pages` is `failed` (`deployed` clears). It then dispatches
`reason: pages` on the repo's default branch after `CONTROL_HEAL_PAGES_BACKOFF_MINUTES` (5 minutes, then 30 after
that dispatch, then 2 hours, then daily), one row per repo so at most one retry is pending, and stops at the first
deploy that succeeds, whoever started it. A retry waits while a non-PR run of the repo is going (a build's Publish
deploys anyway) and while the fleet has no free slot. After `CONTROL_HEAL_PAGES_ATTEMPTS` retries (about a day) it
gives up; a repo with no Pages site (*Pages on image repos*) is never retried. Each dispatch is a scheduler
decision of kind `heal` (a Pages deploy never starts a branch's build cooldown) and an action by `control`.

**Infrastructure failures of live builds**: a failed first attempt of a push or dispatch run on a live branch (never
a pull request run, so never a fork) is judged once from its jobs, `CONTROL_HEAL_RERUN_DELAY_MINUTES` after it
failed. Each failed job's failing step (the first failed, else the first cancelled) must be one of these top-level
steps of `image-build.yml`, whose inputs earlier steps made and checked and which change nothing when repeated:

| Step | Why it is infrastructure |
| --- | --- |
| `Set up job`, `Complete job`, a job with no failed step | GitHub assigning the runner and fetching actions; the runner lost or never started |
| `Check out repository`, `Check out shared workflows`, `Check out repository (live)` | a git clone from GitHub |
| `Free disk space`, `Set up buildx` | the runner's preinstalled toolchains (best effort), downloading BuildKit |
| `Log in to GHCR` | the registry login |
| `Upload manifest`, `Download manifest`, `Upload bundle files`, `Upload image (not pushed)`, `Download bundle files`, `Upload bundle` | the artifact service |
| `Next build number` | reading the repo's Releases and tags from the API |
| `Publish tags` | tagging images already built and tested from their staging references; idempotent |
| `Release (live)` | the GitHub Release; a re-run finds it and re-uploads the assets with `--clobber` |
| `Notify control plane` | the POST to `/events`, which control de-duplicates |
| `Post <any of these>` | the action's cleanup |

Publish's `Fail if any job failed` defers to the job that failed. Anything else failing means no re-run: `Build`,
`Test`, `Fail on test failure`, `Resolve upstream version`, the generated-file and shellcheck checks, and the
composites that mix a pip install with validation (`Read manifest`, `Emit build.json`; the jobs API names only the
composite). Otherwise control calls `rerun-failed-jobs` once, while the run is still its branch's newest build, no
skip flag holds the branch and the fleet has as many slots free as the failed jobs; a run waiting longer than
`CONTROL_HEAL_RERUN_WINDOW_HOURS` for that is given up. A run is never re-run twice by control, and one a member
already re-ran is left alone. The re-run is an action by `control`.

**Capacity**: both wait for free slots as batch runs are sized (`/api/batch.json` `free`: the limit less
`CONTROL_BATCH_RESERVE`, the jobs running and queued and the batch runs' widths), so a retry never takes a slot
an image, hub or site build is waiting for, and at most `CONTROL_HEAL_PER_TICK` heal actions start a minute.

**Reporting**: every step is a log line (`heal:`), every dispatch and re-run a row on `/management` by `control`, and
`/api/status.json` `healing` lists what is pending. Discord hears only of give-ups (a Pages site still failing
after its retries, a build that failed again after its re-run, a re-run GitHub refused or the fleet had no room for),
grouped as the run archive's are: one summary at most an hour with the count, the first five repos or runs linking
their runs, and the most common reason or step. A build's own failure message still posts when it fails; a re-run
that passes edits it green.
