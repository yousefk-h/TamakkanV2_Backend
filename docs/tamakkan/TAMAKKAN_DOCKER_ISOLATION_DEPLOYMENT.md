# ⛔ NOT APPROVED FOR EXECUTION ⛔
# Tamakkan — Docker Image Isolation: Production Deployment Runbook

> **Status: draft for review.** Do not run any command in this document on the production VPS until the project owner gives explicit, written approval for this specific release. The customer-facing platform is live.
>
> **Scope: Tamakkan only**, in the backend / frontend repositories and the VPS directories `$TAMAKKAN_BACKEND_DIR` and `$TAMAKKAN_FRONTEND_DIR`.
> **Sibling LLM and Axis must not be modified, rebuilt, restarted or re-tagged.**
>
> **Server-specific names** are not stored in this repository. Commands use these variables; the
> administrator supplies the current values: `$TAMAKKAN_BACKEND_DIR`, `$TAMAKKAN_FRONTEND_DIR`,
> `$TAMAKKAN_BACKEND_PROJECT`, `$TAMAKKAN_FRONTEND_PROJECT`, `$TAMAKKAN_BACKEND_CONTAINER`,
> `$TAMAKKAN_FRONTEND_CONTAINER`, `$TAMAKKAN_DB_NAME`, `$SHARED_BACKEND_TAG`, `$SHARED_FRONTEND_TAG`
> (the legacy image tags shared with the other product), and for **Sibling LLM** (the separate LLM
> product on the same VPS) `$LLM_BACKEND_DIR`, `$LLM_FRONTEND_DIR`, `$LLM_BACKEND_CONTAINER`,
> `$LLM_FRONTEND_CONTAINER`, `$LLM_IMAGE_PREFIX`. The GitHub repositories still carry their legacy
> names until the rename in plan step 7 (`$BACKEND_GITHUB_REPO`, `$FRONTEND_GITHUB_REPO`).

## 0. What this release does, and does not do
| | Before | After the first isolation release |
|---|---|---|
| Image names | `$SHARED_BACKEND_TAG`, `$SHARED_FRONTEND_TAG`, **shared with Sibling LLM** on the same VPS | `tamakkan-platform-backend:<commit-sha>`, `tamakkan-platform-frontend:<commit-sha>`: Tamakkan only, one immutable tag per commit |
| Deploy trigger | every push to `main`, automatically | **manual only** (Actions → Run workflow, with the exact commit SHA and `DEPLOY`) |
| Deployed commit | whatever `origin/main` was | exactly the SHA entered, which must be on `main` |
| Rollback | none | the previous image is pinned as `tamakkan-platform-*:rollback-<UTC time>`, with automatic rollback when the health check fails |
| **Unchanged** | — | Compose project names (`$TAMAKKAN_BACKEND_PROJECT`, `$TAMAKKAN_FRONTEND_PROJECT`), container names (`$TAMAKKAN_BACKEND_CONTAINER`, `$TAMAKKAN_FRONTEND_CONTAINER`), directories, host ports (4652, 6356), `.env` files, database connection (`$TAMAKKAN_DB_NAME`), LiveKit settings, application code |

**Local branches (not pushed):**

| Repo | Branch | Commits | Base |
|---|---|---|---|
| backend | `infra/tamakkan-platform-image-isolation` | `1c333d5`, `9856b97`, **`0e80f74`** (tip) | `main` @ `d6815f4` (currently deployed) |
| frontend | `infra/tamakkan-platform-image-isolation` | `7160c0a`, `360b4a6`, **`664f78d`** (tip) | `main` @ `255b34b` (currently deployed) |

The H1 security fix is **not** part of this release. It follows later as its own release through the new workflow.

### Integrated local version (2026-09-28): what will eventually reach `main`
| Repo | Branch (local only) | Tip | Contents |
|---|---|---|---|
| backend | `integration/tamakkan-local` | `630645fc86c11100287e1e8b3f52bafcacd8aa70` | `main` + Docker isolation (merge commit) |
| frontend | `integration/tamakkan-local` | `1c183f6bf187a220c5dea0848fe0c1f71f7dc90a` | `main` + Docker isolation (`ebb54ceb`) + H1 (`1c183f6b`) |

- **Which commit to deploy:** the workflow deploys any commit that is on GitHub `main`.
- **Recommended frontend sequence:** first the isolation-only commit `ebb54ceb` (tag `tamakkan-platform-frontend:ebb54ceb…`), verify it, then `1c183f6b` (the same plus H1). That keeps "new image names" and "security fix" as two separately verifiable, separately reversible releases.
- **Backend:** a single release at `630645fc…`.
- ⚠ **These SHAs stay valid only if the branches reach GitHub `main` with history preserved** (fast-forward, or a merge commit that keeps these commits). **Do not squash-merge.** A squash creates new SHAs, so the isolation-only frontend step would no longer exist as its own commit. Re-read the SHAs from `main` before any run.

## 1. Files that change
| Repo | File | Change |
|---|---|---|
| both | `docker-compose.yml` | `name:` pinned to the current project name; `image:` becomes `tamakkan-platform-<svc>:${TAMAKKAN_IMAGE_TAG}` (**required**, with no fallback); usage comments. **Services, ports, env_file, healthcheck, extra_hosts and build args are unchanged.** |
| both | `.github/workflows/deploy.yml` | rewritten: `workflow_dispatch` only, with inputs `commit_sha` and `confirm`; `concurrency`; `verify` job (input format, commit on `main`, compose resolves to the Tamakkan image and pinned project); the frontend keeps its `npm ci` + `vinext build` check on that exact commit; the `deploy` job SSHes in, **aborts if tracked files are modified on the server**, then `git reset --hard <sha>` and runs the deploy script. The no-op `npm install --force` and `--remove-orphans` are removed. |
| both | `deploy/tamakkan-deploy.sh` (new) | pins the running image under a rollback tag, builds `<image>:<sha>` (**or reuses it if that commit was already built: commit tags are never rebuilt**), recreates **only** this service (`--no-deps --no-build`), waits for the Docker health check, rolls back automatically, and records `.tamakkan-deploy/history.log` and `current-tag` |
| both | `.gitignore` (frontend also `.dockerignore`) | ignore `.tamakkan-deploy/` |
| backend | `Dockerfile` | usage comment only (it used to suggest building into the shared legacy backend name) |

## 2. Server-side local modifications: review BEFORE any deployment
> The VPS verification reported **uncommitted modifications in the production directories**. That report was not available when this runbook was written, so **the admin must fill in the table below from the server**.
>
> The new workflow **refuses to deploy while tracked files are modified**, precisely so that nothing is silently wiped. Today's workflow would wipe them with `git reset --hard` on the next push.

### 2.1 Collect, read-only (run in each of the two Tamakkan directories)
```bash
cd $TAMAKKAN_BACKEND_DIR            # then repeat in $TAMAKKAN_FRONTEND_DIR
D=~/tamakkan-predeploy-$(date -u +%Y%m%d); mkdir -p "$D"; R=$(basename "$PWD")
git rev-parse HEAD                               > "$D/$R.head"
git status --porcelain --untracked-files=all     > "$D/$R.status"
git diff                                         > "$D/$R.tracked-changes.patch"     # full content of tracked modifications
git ls-files --others --exclude-standard         > "$D/$R.untracked"
git status --porcelain --ignored | grep '^!!'    > "$D/$R.ignored"                   # .env, overrides, state dirs
ls -la docker-compose*.y*ml compose*.y*ml .env* 2>/dev/null > "$D/$R.compose-files"
cp -p docker-compose.override.y*ml "$D/" 2>/dev/null || true                          # if present (untracked overrides)
```
Only `$D` under the admin's home directory is written. Do not `git stash`, `git checkout` or edit anything.

### 2.2 Compare against the release (read-only)
```bash
git fetch --quiet origin      # updates remote refs only; the working tree is untouched
git diff HEAD origin/main -- docker-compose.yml .github/workflows/deploy.yml deploy/ Dockerfile .gitignore .dockerignore
git diff -- docker-compose.yml Dockerfile .gitignore .dockerignore    # the server's own edits to the same files
```

### 2.3 Checklist: each item must be answered before approval
| # | Check | Why it matters | Server answer |
|---|---|---|---|
| 1 | Any tracked modification to `docker-compose.yml`? | The release replaces this file. A server edit (port, env, healthcheck, network, volume) must be **merged into the repo first** (a PR included in the approved commit), or it is lost. | |
| 2 | Any tracked modification to `Dockerfile`, `.dockerignore`, `package.json`, `next.config.ts`, app code? | These are rebuilt from the repo. Server-only edits disappear from the new image. | |
| 3 | Any `docker-compose.override.yml` / `compose.override.yml` (untracked)? | Compose loads it automatically. **It must not set `image:` (especially not a shared legacy tag) or `name:`**, and its ports and env must still be correct. | |
| 4 | Does `.env` define `COMPOSE_PROJECT_NAME`, `COMPOSE_FILE` or `TAMAKKAN_IMAGE_TAG`? (Show the key names only: `grep -oE '^(COMPOSE_[A-Z_]+|TAMAKKAN_IMAGE_TAG)=' .env`) | Compose reads `.env` for interpolation. These would override the pinned project name or the image tag. | |
| 5 | Is `.env` untracked or ignored (not modified-tracked)? | It must stay out of git; `reset --hard` never touches ignored files. | |
| 6 | Other untracked files needed by the running service? | They survive `reset --hard`, but record them. | |
| 7 | Decision per modified file: **merge into the repo** / **discard (backup kept in `$D`)**. Signed off by: | The workflow aborts until the tree is clean. | |

**Resolution rule:** the server's tracked tree must be clean at deploy time.
- **Wanted changes:** get them into the approved commit through a PR.
- **Unwanted changes:** restore them only after backing up to `$D`, with the owner's written OK (`git checkout -- <file>`).

That restore is itself a production change, and requires approval.

## 3. Back up and pin all four running images (read-only, plus two new tags)
```bash
T=~/tamakkan-predeploy-$(date -u +%Y%m%d); mkdir -p "$T"
for c in $TAMAKKAN_BACKEND_CONTAINER $TAMAKKAN_FRONTEND_CONTAINER $LLM_BACKEND_CONTAINER $LLM_FRONTEND_CONTAINER; do
  docker inspect -f '{{.Name}} image_id={{.Image}} config_image={{.Config.Image}} started={{.State.StartedAt}} restarts={{.RestartCount}} status={{.State.Status}}' "$c"
done | tee "$T/containers-before.txt"
docker image inspect -f '{{.RepoTags}} {{.Id}}' $SHARED_BACKEND_TAG $SHARED_FRONTEND_TAG | tee "$T/shared-tags-before.txt"
```
- **Pin Tamakkan's two running images.** These are new tags on the existing images: nothing is rebuilt or restarted, and nothing else is affected.
  ```bash
  docker tag "$(docker inspect -f '{{.Image}}' $TAMAKKAN_BACKEND_CONTAINER)"       tamakkan-platform-backend:pre-isolation-$(date -u +%Y%m%d)
  docker tag "$(docker inspect -f '{{.Image}}' $TAMAKKAN_FRONTEND_CONTAINER)" tamakkan-platform-frontend:pre-isolation-$(date -u +%Y%m%d)
  ```
- **Sibling LLM:** record its IDs only (above). **Do not tag, rebuild or restart its images or containers.** Its running images can't be removed while its containers exist.
- **Optional, needs separate approval:** `docker save` Tamakkan's two pinned images to a `.tar` for an off-host copy (check disk space first with `df -h`).
- **Do not run** `docker image prune`, `docker system prune` or `docker rmi` at any point in this release.

## 4. Introduce Tamakkan's independent images (execution outline, once approved)
**Prerequisites:**
- (a) Sections 2 and 3 are complete, and the checklist is signed off.
- (b) The isolation branches have been reviewed and merged into `main` through PRs. **Merging does not deploy**: the merge commit carries the new manual-only workflow, and the Actions tab must show no deploy run afterwards.
- (c) A low-traffic window has been agreed.
- (d) A Tamakkan admin confirms in the admin UI that **no live speaking sessions are in progress**. An API restart during a session loses that session's end-of-session callback.
- (e) Disk space is sufficient for one new image per service.

**Order: backend first, verify fully, then the frontend in a separate run.**
1. In GitHub → Actions → "Deploy Tamakkan backend to VPS (manual)" → Run workflow:
   - `commit_sha` = the full SHA of the approved `main` commit;
   - `confirm` = `DEPLOY`.
2. The `verify` job must pass. The `deploy` job prints `server HEAD before`, then the script's steps: pin, build, recreate, health.
3. Run section 5 (verification) and section 6 (Sibling LLM unchanged).
4. Only then repeat for "Deploy Tamakkan frontend to VPS (manual)".

**Equivalent manual path.** Only if GitHub Actions is unavailable, and with the same approval:
```bash
cd <tamakkan dir>
git status --porcelain --untracked-files=no     # must be empty
git fetch origin main
git merge-base --is-ancestor <sha> origin/main
git reset --hard <sha>
bash deploy/tamakkan-deploy.sh <sha>
```

## 5. Verify the exact deployed commit
```bash
cd $TAMAKKAN_BACKEND_DIR                                   # frontend: $TAMAKKAN_FRONTEND_DIR
git rev-parse HEAD                                         # = approved <sha>
cat .tamakkan-deploy/current-tag                           # = approved <sha>
docker inspect -f '{{.Config.Image}} {{.Image}} {{.State.Health.Status}}' $TAMAKKAN_BACKEND_CONTAINER
#   -> tamakkan-platform-backend:<sha>  <id>  healthy
docker image inspect -f '{{.Id}}' tamakkan-platform-backend:<sha>   # must equal <id> above
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4652/api/v1/health   # 200 (frontend: http://127.0.0.1:6356/ -> 200)
tail -n 3 .tamakkan-deploy/history.log
```
**Functional smoke tests**, by a Tamakkan admin: log in; open the admin dashboard and the student list; log in as a test student and open a lesson. Frontend release: also check that the page title and branding are unchanged from before.

## 6. Verify that Sibling LLM is unchanged
```bash
T=~/tamakkan-predeploy-<date>
for c in $LLM_BACKEND_CONTAINER $LLM_FRONTEND_CONTAINER; do
  docker inspect -f '{{.Name}} image_id={{.Image}} config_image={{.Config.Image}} started={{.State.StartedAt}} restarts={{.RestartCount}} status={{.State.Status}}' "$c"
done > "$T/llm-after.txt"
grep -F -e "$LLM_BACKEND_CONTAINER" -e "$LLM_FRONTEND_CONTAINER" "$T/containers-before.txt" | diff - "$T/llm-after.txt" && echo "Sibling LLM containers UNCHANGED"
docker image inspect -f '{{.RepoTags}} {{.Id}}' $SHARED_BACKEND_TAG $SHARED_FRONTEND_TAG | diff "$T/shared-tags-before.txt" - && echo "shared legacy tags UNCHANGED"
```
Both diffs must be empty. Tamakkan's release never builds, tags, pulls or restarts anything under the shared legacy tags or `$LLM_IMAGE_PREFIX*`.

## 7. Rollback
- **Automatic:** if the new container isn't `healthy` within 180 s, the script recreates the service from `tamakkan-platform-<svc>:rollback-<UTC>` (the image that was running a moment before) and exits with an error. `history.log` and `current-tag` record it.
- **Manual,** if needed after a "successful" deploy (for example a functional problem found in the smoke tests), using the recorded tag:
  ```bash
  cd <tamakkan dir>
  grep rollback= .tamakkan-deploy/history.log | tail -1                                           # find the rollback tag
  TAMAKKAN_IMAGE_TAG=rollback-<UTC> docker compose up -d --no-build --no-deps api                 # frontend: service "frontend"
  #   or, back to the state before the very first isolation release:
  TAMAKKAN_IMAGE_TAG=pre-isolation-<date> docker compose up -d --no-build --no-deps api
  echo rollback-<UTC> > .tamakkan-deploy/current-tag
  ```
  Then run the checks in sections 5 and 6.
- ⚠ **Never roll back by checking out the old commit and running `docker compose up`.** The old `docker-compose.yml` uses `image: $SHARED_BACKEND_TAG` / `$SHARED_FRONTEND_TAG`. On this VPS those tags may point to **Sibling LLM's** images (per the GitHub deploy logs, Sibling LLM was the last to build `$SHARED_BACKEND_TAG`; section 3 records where both tags point now), so the Tamakkan container would start **the other product's code**. Always roll back with a `tamakkan-platform-*` tag, as above.
- **Limitation:** rollback restores the **image**, not the compose file. For this release the compose change is minimal (the image name and the project-name pin), so a compose-caused failure is unlikely. Future releases that change the compose file need their own rollback note.

## 8. Expected interruption and risks
**Interruption:**
- **Backend:** one container recreate, so the API is unavailable for roughly 5–20 s (stop, then start with the Mongo connection). nginx returns 502 during that time.
- **Frontend:** similar, roughly 5–15 s.
- **Health gate:** the Docker health check runs every 30 s, so a run takes roughly 1 minute after the build. The build itself happens **before** the running container is touched, and adds no downtime.

**Failure scenarios:**

| Scenario | Effect | Handling |
|---|---|---|
| Server has modified tracked files | none: aborted before any change | Section 2 |
| Commit not on `main`, wrong SHA, missing `DEPLOY` | none: `verify` job fails | fix the inputs |
| `git fetch` / SSH fails | none | retry later |
| Image build fails (network, disk, npm registry) | **none**: the running container is untouched | investigate; retry |
| New container unhealthy | ~1–2 min of degraded service: two recreates | automatic rollback; logs are printed in the Actions run |
| Rollback also unhealthy | **service down** | manual `pre-isolation` rollback (section 7); escalate |
| `.env` / override sets `COMPOSE_PROJECT_NAME` or `image:` | a new, differently named container and a **port conflict** | caught by checklist items 3–4 |
| Live session during the backend swap | that session's transcript/report may be lost | pre-check (d) |
| In-flight background jobs (transcript generation, report scoring) | lost, as on any restart | low-traffic window; regenerate transcripts afterwards if needed |
| Older Docker Compose without `config --images` | the script falls back to parsing `image:` lines | — |

**Not covered here, still open:**
- Tamakkan and Sibling LLM both use the LiveKit agent name `mariam-agent`. This doesn't affect this release, but must be resolved before any Tamakkan backend or agent change that touches sessions.
- Sibling LLM's own use of the shared legacy tags is its owner's decision.

## 9a. Integrated version, validated locally (2026-09-28)
- **Backend:** full suite **534 passed** (same as the baseline).
- **Frontend:** H1 tests **8/8**; ESLint clean; `tsc` shows only the pre-existing `db/index.ts` leftover error; **production build succeeds**.
- **Docker, isolated test projects, dummy credentials:**
  - `tamakkan-platform-backend:630645fc…` healthy; guard rails refused; broken release **auto-rolled back** to the same image ID;
  - `tamakkan-platform-frontend:1c183f6b…` healthy (`GET /` 200); re-deploy reused the image.
- **Frontend and backend run together:** the production frontend build with the integrated backend and an isolated database passed **54 of 55** automated end-to-end checks. The 55th (the API-contract scan) flagged only my test's parser, which couldn't read the one conditional URL `${scope === "student" ? "/me" : ""}/analytics`; both real paths (`/analytics`, `/me/analytics`) returned 200 in the same run. Details are in `TAMAKKAN_HANDOVER.md`.

## 9. Local validation already done (2026-09-28, developer PC only)
**Setup:** throwaway clones, a dummy `.env` from `.env.example`, a new Docker network and new Mongo container, test project names, and host ports 14652 / 16356. No production system, database or provider was contacted. All test containers, networks, volumes and images were removed afterwards, and the PC's Docker state was compared against a snapshot taken before the tests.

| Check | Result |
|---|---|
| Compose with no `TAMAKKAN_IMAGE_TAG` | **refused** (both repos) |
| Compose image and project | `tamakkan-platform-{backend,frontend}:<exact commit>`; project `$TAMAKKAN_BACKEND_PROJECT` / `$TAMAKKAN_FRONTEND_PROJECT`; container `$TAMAKKAN_BACKEND_CONTAINER` / `$TAMAKKAN_FRONTEND_CONTAINER`; ports `127.0.0.1:4652` / `6356`; `restart: unless-stopped` unchanged |
| Shared legacy tags in the compose file, workflow, script or Dockerfile (non-comment) | none |
| Workflow triggers | `workflow_dispatch` only; `concurrency` (no cancelling); `permissions: contents: read` |
| Workflow verify steps run locally | pass; short SHA, uppercase SHA and wrong confirmation all **rejected** |
| `actionlint` 1.7.12 (includes shellcheck of the `run:` blocks) | 0 problems |
| `shellcheck -S style` on both deploy scripts | 0 findings |
| **Backend end to end** | first deploy healthy (15–56 s after recreate); refuses a short SHA, a SHA ≠ HEAD and a modified tracked file; **a deliberately broken release was detected as unhealthy after ~85 s and automatically rolled back within ~15 s to the previous image ID**; `history.log` and `current-tag` correct |
| **Frontend end to end** | first deploy healthy ~10 s after recreate; `GET /` → 200; a re-deploy of the same commit **reuses** the existing image (same ID) and pins the rollback tag |
| Bug found and fixed during testing | (1) the verify step's shared-tag grep matched comments, so it would have failed every real run → fixed (`9856b97` / `360b4a6`); (2) rebuilding the same commit produced a different image ID, which would re-point the "immutable" tag → reuse instead (`0e80f74` / `664f78d`) |
