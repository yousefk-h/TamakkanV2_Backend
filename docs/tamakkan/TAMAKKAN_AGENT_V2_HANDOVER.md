# Tamakkan Mariam V2 agent: handover

Date: 2026-09-28. Branch: the legacy integration branch (not merged, committed or pushed).

Nothing has been deployed to LiveKit Cloud, and production (VPS backend and frontend, the historical LiveKit project `ielets-dajbgqlm`, agent `CA_L7qaqyCwtoMe`) has not been touched.

## 1. Status at a glance

| Item | Status |
|---|---|
| Agent code | Unchanged. V2 is configured entirely by environment. |
| Target LiveKit project | **Tamakkan_Agent**, `wss://tamakkan-agent-m4zc52ik.livekit.cloud` (the earlier `tamakkan` / `p_3mzq8by0p0k` project is no longer used) |
| Dispatch name | `tamakkan-mariam-v2` |
| Real voice session (local worker, LiveKit Meet) | **PASS** (see section 4) |
| SpatialReal server-side integration | **PASS**; avatar *rendering* still needs the Tamakkan frontend |
| Backend session lifecycle through V2 | Not yet tested (the test dispatch had no `session_id`) |
| Hosted LiveKit Cloud agent | **Created 2026-09-28**: `CA_LBTsvNPdKaHR`, region `eu-central`, Running 1/1, registered as `tamakkan-mariam-v2`; cloud voice test **PASS** (section 4b) |
| Production backend switch | Prepared below; not executed |

## 2. Verified architecture (unchanged by V2)

- **Dispatch.** `POST /api/v1/sessions/start` (`app/services/live_session_service.py`) mints a student token (`identity=student-<id>`, room `mariam-<session_id>`) with a `RoomAgentDispatch(agent_name=settings.LIVEKIT_AGENT_NAME, metadata=...)`.
  - The metadata is `session_id`, `topic_id`, `max_duration_minutes`, `topic_instructions`, `opening_line` and `level`.
  - The response returns `server_url = settings.LIVEKIT_URL` to the browser.
- **Registration.** `livekit_agent/agent.py` registers `@server.rtc_session(agent_name=get_settings().LIVEKIT_AGENT_NAME)` (default `mariam-agent`, left unchanged). V2 sets `LIVEKIT_AGENT_NAME=tamakkan-mariam-v2` through its environment.
- **Conversation.** OpenAI Realtime (`gpt-realtime-2.1-mini`) is the primary model, with Ultravox (voice "Jessica") as the fallback via `RealtimeModelFallbackAdapter`. The system prompt is the base prompt, then level-specific instructions, then the topic instructions (`prompts.py`).
- **Avatar.** `spatialreal.AvatarSession(api_key, app_id, avatar_id)`. It also reads `LIVEKIT_URL/API_KEY/API_SECRET` from the environment to mint the `spatialreal-avatar` participant's token.
- **Backend callbacks.** The agent sends `POST {BACKEND_API_URL}/sessions/{id}/mark-connected` and `.../mark-ended` (with `end_reason` and a batched transcript), authenticated with the `X-Internal-Api-Key` header, which must equal the backend's `AGENT_INTERNAL_API_KEY`.
  - These calls are best-effort: a failure only logs a warning.
  - `mark-ended` triggers scoring and report generation on the backend.
- **Session end.** The agent deletes the room (`_close_room`, using the LiveKit credentials) on max duration or idle timeout.
- **Frontend.** The frontend gets the LiveKit URL from the backend response, so moving to a new LiveKit project needs **no frontend LiveKit change**. It renders the avatar with the SpatialReal AvatarKit SDK using `NEXT_PUBLIC_SPATIALREAL_APP_ID` / `NEXT_PUBLIC_SPATIALREAL_AVATAR_ID`, which are baked in at build time.

## 3. Files and local state

| Path | Change |
|---|---|
| `livekit_agent/.env.tamakkan-v2` | **New, private.** Ignored by git (`.gitignore` `.env.*`) and by the Docker build (`.dockerignore`). Holds the V2 settings. |
| `TAMAKKAN_AGENT_V2_HANDOVER.md` | New (this file). |
| `livekit_agent/livekit.toml` → `livekit.legacy.toml` | Your earlier rename (historical project and agent id). Not modified. |
| `.venv/`, `livekit_agent/.venv/` | Local virtualenvs (git-ignored). |
| `~/.livekit/cli-config.yaml` | Project `Tamakkan_Agent` added and set as **default**; the old `tamakkan` entry was left unused. |
| git | Read-only `origin/main` ref fetched for comparison: the legacy integration branch is fully contained in `main`, with no diff in any agent or session file. |

### V2 configuration (variable names only)

| Variable | Local run | Hosted agent | Value / source |
|---|---|---|---|
| `LIVEKIT_AGENT_NAME` | required | required | `tamakkan-mariam-v2` |
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | required | **do not upload** (LiveKit Cloud injects them) | Tamakkan_Agent project |
| `OPENAI_API_KEY` | required | required | set |
| `ULTRAVOX_API_KEY` | required | required | set |
| `SPATIALREAL_API_KEY` | required | required | set |
| `SPATIALREAL_APP_ID` | required | required | `app_muli59mj_w70405` |
| `SPATIALREAL_AVATAR_ID` | required | required | `ca9c5c22-6dba-4b59-ae3b-d26066f8c017` |
| `BACKEND_API_URL` | only with a real backend | **required** | `https://tamakkanbackend.tamakkan.online/api/v1` (set; verified, see below) |
| `AGENT_INTERNAL_API_KEY` | only with a real backend | **required** | set; 64 chars, SHA-256 fingerprint `85694f8859fb` matches production (verified 2026-09-28) |
| `MARIAM_IDLE_TIMEOUT_SECONDS` | optional | optional | 120 |

A blank value is not caught when the worker starts, so both must be supplied:

- A blank `OPENAI_API_KEY` degrades silently to the fallback model.
- A blank `ULTRAVOX_API_KEY` fails each job before the agent's cleanup code runs, leaving a backend-started session stuck in `connecting`.
- A blank or wrong `BACKEND_API_URL` / `AGENT_INTERNAL_API_KEY` only logs `Failed to report session state to backend`, and the backend session never reaches `connected`/`ended` and gets no report.

## 4. Real session inspection (local worker, 2026-09-28)

Job `AJ_cVh38hiVgFHH`, room `mariam-v2-test` (`RM_hhTN6cinic3e`), dispatched through a `lk token create --agent tamakkan-mariam-v2 --job-metadata ...` token. Joined in LiveKit Meet as `student-test`. There was no `session_id`, so no backend calls were made by design.

| # | Check | Result | Evidence in the log |
|---|---|---|---|
| 1 | Registered as `tamakkan-mariam-v2` on Tamakkan_Agent | **PASS** | `registered worker {"agent_name": "tamakkan-mariam-v2", "id": "AW_2JU2xhj4uEwN", "url": "wss://tamakkan-agent-m4zc52ik.livekit.cloud"}`; `received job request {... "agent_name": "tamakkan-mariam-v2"}` |
| 2 | OpenAI Realtime connected; Ultravox fallback used? | **PASS**, fallback **not** used | `livekit.plugins.openai` response events for the whole call; no fallback/switch or Ultravox activity logged. The Ultravox key was never exercised. |
| 3 | Student speech received, spoken replies generated | **PASS** | 5 user and 4 assistant `conversation_item_added` entries logged (2 more turns were received but not printed; see note A); 4 avatar playback segments; `start reading stream {"participant": "student-test", "source": "SOURCE_MICROPHONE"}` |
| 4 | SpatialReal initialized, joined, published media | **PASS** (server side) | `starting SpatialReal avatar session` → no start error (the session-token call with the API key and the ingress session with the App ID and Avatar ID both succeeded, otherwise `start()` raises) → `SpatialReal avatar LiveKit audio track subscribed {"participant_identity": "spatialreal-avatar"}` about 8 s later → `playback started` ×4, `completion derived from observed LiveKit playback` |
| 5 | No credential/auth/credit/connection/runtime errors during the conversation | **PASS**, with non-blocking notes | No 401/403, quota, credit or connection errors. Clean close: `session closed {"reason": "participant_disconnected", "error": null}`, `SpatialReal avatar session closed`. |

Notes:

- **A. `UnicodeEncodeError` ×2.** These come from the Windows console's cp1252 encoding while printing a DEBUG transcript line; logging recovers and the conversation was unaffected. They are specific to this local run: the hosted image runs Linux with UTF-8. For local runs, set `PYTHONIOENCODING=utf-8`.
- **B. `AssertionError` in `livekit/agents/voice/avatar/_types.py` `_on_connection_state_changed`.** It fired about 24 s *after* the session had closed, during room teardown. It is internal to the SDK (livekit-agents 1.7.1), and the job still uploaded its report and exited normally. Watch for it in hosted logs; no action is needed now.
- **C. Benign warnings.** `no warmed process available` (first job in dev mode), `recorder dropped audio` and `dropping pass-through signal` (teardown).
- **D. Interruptions.** 3 of Mariam's 4 replies were cut off by detected student speech (`reason=turn_detected`). If the test used open speakers, her own voice may have been picked up as speech. Use headphones for the frontend test before judging interruption behaviour.
- **E. Session recording is enabled on this project.** The job request shows `"enable_recording": true`, and the job logged `uploading session report to LiveKit Cloud`. Student audio and transcripts are therefore stored in LiveKit Cloud's agent observability. Decide in the Tamakkan_Agent project settings whether this is acceptable for student data before production.

**What the logs do not prove, and how to test it through the real Tamakkan frontend:**

- that the avatar renders in the browser: the black tile in Meet is expected, because the video track carries animation data for AvatarKit;
- that the frontend's `NEXT_PUBLIC_SPATIALREAL_APP_ID` matches `app_muli59mj_w70405`;
- `mark-connected` / `mark-ended`, transcript storage and report generation with a real `session_id`;
- idle timeout and max-duration handling, including `_close_room`;
- the Ultravox fallback;
- the hosted worker reaching the production backend over HTTPS.

Automated tests (run earlier on this checkout): backend `pytest tests` gave **534 passed**. Agent `livekit_agent/tests` gave **7 passed, 4 skipped**; the 4 LLM tests were skipped because no OpenAI key was set at the time.

### Production backend reachability (verified 2026-09-28, from this PC)

- `GET https://tamakkanbackend.tamakkan.online/api/v1/health` returned **200** `{"status":"ok"}`, with a valid TLS certificate, in about 1 s.
- `POST .../api/v1/sessions/<dummy id>/mark-connected` **without** a key returned **401** `Invalid or missing internal API key`. The agent callback route exists at this prefix and is gated.
- A 401 would also appear if production's key were blank, so the key match itself is confirmed only by the fingerprint check (section 5, step 1).
- LiveKit Cloud's egress to this domain is proven only once the hosted agent completes a real `mark-connected`.

## 4b. Hosted agent deployment and cloud test (2026-09-28)

- **Created with:**
  ```bash
  lk agent create --project Tamakkan_Agent --region eu-central --secrets-file .env.tamakkan-v2.cloud .
  ```
  The CLI requires `--region` when run non-interactively. `eu-central` is the nearest of `us-east`, `eu-central` and `ap-south`.
- **Result:** `livekit_agent/livekit.toml` has `subdomain = "tamakkan-agent-m4zc52ik"` and `id = "CA_LBTsvNPdKaHR"`. It is not committed.
- **Health:** status Running, replicas 1/1/1. The worker registered as `tamakkan-mariam-v2` (`CAW_oYFPSFgkFZz7`). Secrets are the 9 variables, with no `LIVEKIT_*`. The local worker was stopped afterwards.
- **Cloud test:** job `AJ_UwpkQiKSs9E2`, room `mariam-v2-cloud-test`, synthetic metadata, no `session_id`. The job was received at 20:11:54 UTC and ran until the student disconnected at 20:13:56 (`CLIENT_INITIATED`).
  - No credential, authentication, quota or SpatialReal start errors.
  - An avatar start or join failure would have ended the job right away; it didn't.
- **Hosted logs are INFO-level only,** so individual turns and SpatialReal playback events aren't logged there. Mariam's responses are confirmed by the tester; the full transcript is in the LiveKit Cloud dashboard's session view, because recording is on.
- **Known, non-blocking ERROR: `Initial greeting failed; continuing without it`.** This is `TimeoutError` from `agent.py:427`, where the code waits at most 10 s for the first greeting to finish playing. The greeting itself is shielded and keeps playing, and the session continued normally. This is existing behaviour in the current code (production `mariam-agent` runs the same code), not a V2 regression. If the ERROR level is a problem, raising or removing that 10 s wait is a later, separately approved code change.
- **Same teardown `AssertionError`** in the SDK avatar code after session close as in the local run; benign.

## 4c. Visual avatar test in the real Tamakkan frontend (local, 2026-09-28): PASS

- **Page.** A temporary dev-only page in the local frontend checkout, `frontend/app/dev/mariam-v2-test/` (`page.tsx`, `MariamV2TestClient.tsx`).
  - It is untracked and never committed, and returns 404 in production builds.
  - It uses the unchanged `useSpatialRealAvatar` + `useRoomAudio` hooks and the same avatar box as `MariamSession`. It connects with a test token passed in the URL fragment instead of `sessionsApi.start`.
- **Dev server.** Run with `TAMAKKAN_LOCAL=1` (so `.env.local`, which holds the old App ID and a production API URL, is not loaded) and process env `NEXT_PUBLIC_SPATIALREAL_APP_ID=app_muli59mj_w70405` / `..._AVATAR_ID=ca9c5c22-…`. `API_BASE_URL` pointed at an unused local port.
  - No env files were edited.
  - The dev server was stopped after the test.
- **Result.** The tester confirmed the avatar face appeared, animation and voice worked, and there was a normal conversation. The page rendered with the V2 App ID.
- **Hosted log** (job `AJ_9Zdm9dqBN6Pr`, room `mariam-v2-avatar-test`, 20:24:47–20:26:04 UTC, ended by the student's disconnect). Only the known non-blocking entries appeared: the `Initial greeting failed` 10 s playout timeout and the teardown `AssertionError` / `recorder dropped audio` / `dropping pass-through signal`. There were no credential, SpatialReal or connection errors.
- **To repeat.** Start Vite the same way on port 5177, then open `http://127.0.0.1:5177/dev/mariam-v2-test#url=<wss url>&token=<lk token>`.
- **Cleanup.** Delete `app/dev/mariam-v2-test/` when it's no longer needed.

## 5. Remaining steps to deploy to LiveKit Cloud (needs your approval)

1. **Supply `AGENT_INTERNAL_API_KEY` in `livekit_agent/.env.tamakkan-v2`**: the same value the production backend uses. (`BACKEND_API_URL` is already set.)

   Check that the key matches without revealing it, using the fingerprint command in section 7 (step 3) together with this local one:
   ```bash
   python -c "import hashlib;v=[l.split('=',1)[1].strip() for l in open('livekit_agent/.env.tamakkan-v2') if l.startswith('AGENT_INTERNAL_API_KEY=')][0];print(hashlib.sha256(v.encode()).hexdigest()[:12])"
   ```
2. **Done 2026-09-28:** `livekit_agent/.env.tamakkan-v2.cloud` was generated (9 variables, no `LIVEKIT_URL/API_KEY/API_SECRET`, git- and docker-ignored). To regenerate it after editing the source file, create a secrets file for upload without the `LIVEKIT_*` lines (LiveKit Cloud injects those three itself):
   ```bash
   cd livekit_agent
   grep -vE '^(LIVEKIT_URL|LIVEKIT_API_KEY|LIVEKIT_API_SECRET)=' .env.tamakkan-v2 > .env.tamakkan-v2.cloud   # also git/docker-ignored
   ```
3. **Stop the local worker** before or right after the hosted one is up. Otherwise LiveKit splits `tamakkan-mariam-v2` jobs between your PC and the cloud.
4. **Create the hosted agent** (run inside `livekit_agent/`; it builds this directory's `Dockerfile` and writes a new `livekit.toml`):
   ```bash
   lk project list                     # confirm "* Tamakkan_Agent  wss://tamakkan-agent-m4zc52ik.livekit.cloud"
   lk agent list --project Tamakkan_Agent
   lk agent create --project Tamakkan_Agent --secrets-file .env.tamakkan-v2.cloud .
   ```
   - `--region` can be added. None was chosen: SpatialReal's default endpoints are `us-west`, and the VPS location still needs confirming.
   - The CLI project was added with an API key (not `lk cloud auth`). If `create` asks for cloud login, run `lk cloud auth` and pick Tamakkan_Agent.
   - Afterwards, check that the new `livekit.toml` shows `subdomain = "tamakkan-agent-m4zc52ik"` and a **new** agent id, not `CA_L7qaqyCwtoMe`.
5. **Verify the hosted agent:**
   ```bash
   lk agent status
   lk agent logs          # expect: registered worker {"agent_name": "tamakkan-mariam-v2", ...}
   lk agent secrets       # names only; LIVEKIT_* must not be listed
   ```
   Then repeat the Meet test with the local worker stopped:
   ```bash
   lk token create --join --room mariam-v2-cloud --identity student-test --agent tamakkan-mariam-v2 --job-metadata '{"level":"intermediate","max_duration_minutes":5}' --valid-for 30m --open meet
   ```
6. **Recommended before production: one frontend-backed test** against a non-production backend (the local Tamakkan profile):
   - **Backend:** `LIVEKIT_URL/API_KEY/API_SECRET` = Tamakkan_Agent, `LIVEKIT_AGENT_NAME=tamakkan-mariam-v2`.
   - **Frontend:** `NEXT_PUBLIC_SPATIALREAL_APP_ID=app_muli59mj_w70405`.
   - **Worker:** reachable `BACKEND_API_URL` plus the matching internal key. Note that `scripts/start-tamakkan-agent.ps1` enforces the name `tamakkan-mariam-local`, so for this test run the worker with `TAMAKKAN_ENV_FILE` as in section 8.
   - This covers everything in the "not proven" list above except production reachability.

## 6. Production backend migration (prepared, not executed)

Only the backend `.env` on the VPS changes. No code change, rebuild, merge or branch switch is needed.

| Variable in `$TAMAKKAN_BACKEND_DIR/.env` | From | To |
|---|---|---|
| `LIVEKIT_URL` | `wss://ielets-dajbgqlm.livekit.cloud` | `wss://tamakkan-agent-m4zc52ik.livekit.cloud` |
| `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | old project key | a Tamakkan_Agent key (preferably one created just for the backend) |
| `LIVEKIT_AGENT_NAME` | `mariam-agent` (or unset) | `tamakkan-mariam-v2` |
| `AGENT_INTERNAL_API_KEY` | unchanged | unchanged (the V2 agent uses the same value) |

**Frontend:** the LiveKit URL needs no change. For SpatialReal, if production's `NEXT_PUBLIC_SPATIALREAL_APP_ID` is not `app_muli59mj_w70405`, the frontend must be **rebuilt** with it (build-time variable), or the V2 avatar's animation data will not match the app it renders under. Check this in section 7 first.

**Steps** (in a quiet period, after the section 7 checks):

1. Confirm there are no active live sessions: admin sessions view, or a read-only DB check, with your approval.
2. Back up the environment file:
   ```bash
   cd $TAMAKKAN_BACKEND_DIR
   cp -p .env .env.pre-livekit-v2.$(date +%Y%m%d%H%M)
   ```
3. Edit the four `LIVEKIT_*` lines in `.env`.
4. Recreate only the API container with the **existing** image. `docker restart` does not reload `env_file`.
   ```bash
   docker compose up -d --no-build --no-deps --force-recreate api
   ```
   Expected API interruption: a few seconds.
5. Verify:
   ```bash
   docker exec $TAMAKKAN_BACKEND_CONTAINER python -c "from app.core.config import get_settings as g;s=g();print(s.LIVEKIT_URL,s.LIVEKIT_AGENT_NAME)"
   curl -fsS https://<api-domain>/api/v1/health
   ```

**Effect on sessions in progress:** calls already running on the old project keep their old agent and still report back, because the internal key is unchanged. An admin force-end of such a session after the switch would fail, since the backend now targets the new project.

## 6b. Frontend SpatialReal App ID (image names superseded by section 6c) (findings 2026-09-28, from the frontend repo's `origin/main`)

These findings come from the frontend repo's `origin/main`. Its `Dockerfile`, `docker-compose.yml`, `.dockerignore` and `deploy.yml` are identical on the remote legacy integration branch.

- **Source of the value.** `components/student/player/MariamSession.tsx` reads `process.env.NEXT_PUBLIC_SPATIALREAL_APP_ID` and `..._AVATAR_ID` and passes them to `AvatarSDK.initialize(appId, { environment: Environment.intl })`.
  - Next.js/vinext inlines both into the browser bundle **at build time**.
  - No session token is passed, and no server-side SpatialReal key is used.
- **Build path.** `docker-compose.yml` passes `args: NEXT_PUBLIC_SPATIALREAL_APP_ID: ${NEXT_PUBLIC_SPATIALREAL_APP_ID}` (and `..._AVATAR_ID`).
  - Compose resolves `${...}` from the deploying shell's environment first, then from **`$TAMAKKAN_FRONTEND_DIR/.env`** (the project `.env`).
  - `.dockerignore` excludes `.env` and `.env.*`, so no other env file can reach the build. `.env.local`, `.env.production` and similar files on the VPS **do not affect** the bundle.
- **Deploy.** `deploy.yml` runs on every push to `main`: `git reset --hard origin/main` then `docker compose up -d --build`. The image is `$SHARED_FRONTEND_TAG`, published on `127.0.0.1:6356`.
- **What changes for V2.** Only `NEXT_PUBLIC_SPATIALREAL_APP_ID`: `app_mq7tru7i_65rao7` → `app_muli59mj_w70405`.
  - The Avatar ID (`ca9c5c22-…c017`) is unchanged.
  - The environment is `intl` in the frontend. The agent uses SpatialReal's `us-west` intl endpoints, which the V2 cloud test proved work with the V2 App ID.
  - `SPATIALREAL_APP_ID` in compose `args` is not declared as an `ARG` in the Dockerfile, so it's unused by the build. Keep it consistent anyway.
- **Coupling.** The frontend App ID and the agent's App ID belong together. Whether the old agent works with the V2 frontend App ID (or the reverse) is untested, so switch the frontend and backend **in the same window** and roll them back together.

**Frontend rebuild and deployment (after approval; pre-build, swap only in the window):**

1. Record the rollback points:
   ```bash
   cd $TAMAKKAN_FRONTEND_DIR
   TS=$(date +%Y%m%d%H%M)
   docker tag "$SHARED_FRONTEND_TAG" "${SHARED_FRONTEND_TAG%:*}:pre-v2-$TS"
   cp -p .env .env.pre-v2-$TS
   ```
2. In `.env`, change only `NEXT_PUBLIC_SPATIALREAL_APP_ID` (and `SPATIALREAL_APP_ID` if present) to `app_muli59mj_w70405`.
3. Build from the **current checkout** (no `git pull`) while the old container keeps serving:
   ```bash
   docker compose build frontend
   ```
4. Verify before swapping. Expect only `app_muli59mj_w70405`:
   ```bash
   docker run --rm --entrypoint sh $SHARED_FRONTEND_TAG -c "grep -rhoE 'app_[a-z0-9]{8}_[a-z0-9]{6}' /app --include=*.js | sort | uniq -c"
   ```
5. Swap (in the same window as the backend switch; a few seconds of downtime):
   ```bash
   docker compose up -d --no-build --no-deps --force-recreate frontend
   ```
6. **Rollback:**
   ```bash
   docker tag "${SHARED_FRONTEND_TAG%:*}:pre-v2-$TS" "$SHARED_FRONTEND_TAG"
   cp -p .env.pre-v2-$TS .env
   docker compose up -d --no-build --no-deps --force-recreate frontend
   ```
   Roll back the backend at the same time (section 8).

**Test the V2 avatar before production (local, synthetic data, existing launchers, no code change):**

- **Backend:** the `.env.tamakkan` profile (port 4653, local Mongo `tamakkan_dev`) with `LIVEKIT_URL/API_KEY/API_SECRET` = Tamakkan_Agent, `LIVEKIT_AGENT_NAME=tamakkan-mariam-local`, and the OpenAI, Ultravox and SpatialReal keys plus `SPATIALREAL_APP_ID=app_muli59mj_w70405`.
- **Worker:** `scripts/start-tamakkan-agent.ps1` (it enforces `tamakkan-mariam-local` and the local backend URL). This is the same agent code as the hosted one; the distinct name keeps the hosted agent, which reports to production, out of the test.
- **Frontend:** `.env.tamakkan` with `NEXT_PUBLIC_SPATIALREAL_APP_ID=app_muli59mj_w70405` and `NEXT_PUBLIC_SPATIALREAL_AVATAR_ID=ca9c5c22-6dba-4b59-ae3b-d26066f8c017`, started with `scripts/start-tamakkan.ps1`.
- **Data:** seed demo accounts (`scripts/tamakkan_local.py --seed-demo`). The demo student needs one assigned, unlocked topic to start a session.
- **Checks:** the avatar renders and speaks; the local session goes connecting → connected → ended; a transcript and report are stored.

## 6c. Image isolation (2026-09-28): supersedes every `$SHARED_FRONTEND_TAG` / `$SHARED_BACKEND_TAG` step in sections 6, 6b and 8

**Why.** Tamakkan and Sibling LLM Compose files both use `$SHARED_FRONTEND_TAG` and `$SHARED_BACKEND_TAG`. Any `docker compose build` / `up --build` in either project moves the shared tag, and a later `up --no-build` in the other project would start the wrong image.

Sibling LLM's running containers already reference image IDs that `docker image inspect` no longer finds. Its containers can therefore keep running and restart in place, but **cannot be recreated** (`compose up --force-recreate`, `docker rm` + `up`) until its images are rebuilt or preserved.

**Tamakkan uses its own image names from now on:** `tamakkan-frontend:*` and `tamakkan-backend:*`. This goes through override files kept outside the repos (so `git reset --hard` in `deploy.yml` never removes them), activated by `COMPOSE_FILE` in each Tamakkan `.env`:

| File | Content |
|---|---|
| `/root/tamakkan-compose/frontend.override.yml` | `services: { frontend: { image: tamakkan-frontend:latest } }` |
| `/root/tamakkan-compose/backend.override.yml` | `services: { api: { image: tamakkan-backend:latest } }` |
| Frontend `.env` | `COMPOSE_FILE=docker-compose.yml:/root/tamakkan-compose/frontend.override.yml` |
| Backend `.env` | `COMPOSE_FILE=docker-compose.yml:/root/tamakkan-compose/backend.override.yml` |

- The project directory and name are unchanged, so container names stay `$TAMAKKAN_FRONTEND_CONTAINER` / `$TAMAKKAN_BACKEND_CONTAINER`.
- The GitHub `deploy.yml` (`docker compose up -d --build`) then builds `tamakkan-*:latest` instead of the shared tags.
- Adding `COMPOSE_FILE` changes no running container. Verify with `docker compose config --images`.

**Preserved rollback images** (tagged from the verified running IDs, plus `docker save` tarballs so a prune can't remove them):

| Tag | Image ID |
|---|---|
| `tamakkan-frontend:pre-v2-<TS>` | `sha256:73c6304db4285d7c8e989a69ccfa4ff263c451e8531c92f3eb27f80add23eb51` |
| `tamakkan-backend:pre-v2-<TS>` | `sha256:68341d644303e0b1c367616c6251d624af2cb58530e4a99213cc773b44a49cd6` |

`tamakkan-frontend:latest` and `tamakkan-backend:latest` start at those same IDs. Take the V2 `.env` backups **after** `COMPOSE_FILE` is added, so a rollback keeps the isolation.

**V2 frontend build (replaces 6b steps 1-5):**

1. Build under an immutable tag through a temporary second override (`image: tamakkan-frontend:v2-<TS>`), with `NEXT_PUBLIC_SPATIALREAL_APP_ID=app_muli59mj_w70405` set in the shell.
2. Verify that tag's bundle contains only the V2 App ID.
3. At cutover:
   ```bash
   docker tag tamakkan-frontend:v2-<TS> tamakkan-frontend:latest
   ```
   Then change the App ID line in `.env` and run:
   ```bash
   docker compose up -d --no-build --no-deps --force-recreate frontend
   ```
4. Backend cutover as in section 6: `docker compose up -d --no-build --no-deps --force-recreate api`, which now resolves to `tamakkan-backend:latest`.

**Rollback (replaces section 8 image steps):**

- `docker tag tamakkan-frontend:pre-v2-<TS> tamakkan-frontend:latest` (backend likewise).
- Restore both V2 `.env` backups, which still contain `COMPOSE_FILE`.
- Recreate both services with `--no-build --no-deps --force-recreate`.

Tamakkan never runs `docker tag` into the shared legacy tags or builds into those names.

**Sibling LLM (separate owner authorisation; nothing in the Tamakkan procedure touches it):**

- Keep its containers running. Do not `compose up`, `down`, `rm` or recreate them, and do not prune.
- Preserve: save `docker inspect` output (chmod 600; it contains env values), then `docker commit --pause=false` of each running container to `$LLM_IMAGE_PREFIX-*-preserve:<TS>`, then `docker save` those.
  - A commit is a runnable image made from the container's current filesystem and config, **with the container's env, including secrets, baked in**. It is not the original build.
  - `docker export` is only a flat filesystem backup: no CMD/ENV/USER/WORKDIR, restorable only with `docker import --change ...` using the saved inspect JSON.
  - Neither includes volumes or bind mounts.
- The proper fix is rebuilding Sibling LLM from its own repo commit and `.env`, under its own `$LLM_IMAGE_PREFIX-*` tags, with its own Compose change.
- Note: the shared legacy tags will keep pointing at Tamakkan's pre-V2 IDs. A Sibling LLM `compose up` **without** `--build` would start Tamakkan images until that fix is made.

## 7. Read-only VPS checks (to run before migrating; nothing is modified)

```bash
# 1. What is actually deployed
cd $TAMAKKAN_BACKEND_DIR
git rev-parse --abbrev-ref HEAD; git log -1 --format='%H %cd %s'; git status --porcelain | head
docker inspect $TAMAKKAN_BACKEND_CONTAINER --format '{{.Config.Image}} created={{.Created}} started={{.State.StartedAt}}'
# Note: .github/workflows/deploy.yml still runs `git reset --hard origin/main` on every push to main.

# 2. Public API domain (for BACKEND_API_URL)
grep -Rn "server_name\|proxy_pass http://127.0.0.1:4652" /etc/nginx/sites-enabled/

# 3. Current LiveKit settings + internal-key fingerprint (no secret printed)
docker exec $TAMAKKAN_BACKEND_CONTAINER python -c "import hashlib;from app.core.config import get_settings as g;s=g();print(s.LIVEKIT_URL,s.LIVEKIT_AGENT_NAME,s.API_PREFIX,'key_fp=',hashlib.sha256(s.AGENT_INTERNAL_API_KEY.encode()).hexdigest()[:12])"

# 4. Frontend SpatialReal IDs (public, non-secret values)
grep -h "NEXT_PUBLIC_SPATIALREAL" $TAMAKKAN_FRONTEND_DIR/.env* 2>/dev/null
```

Also confirm the **old agent is still healthy** (recent sessions reaching `ended` with reports). Rollback depends on it, and `CA_L7qaqyCwtoMe` has not been verified from this machine: the historical project is not configured in the local CLI.

## 8. Rollback

- **Backend** (restores the old project and dispatch name):
  ```bash
  cd $TAMAKKAN_BACKEND_DIR
  cp -p .env.pre-livekit-v2.<timestamp> .env
  docker compose up -d --no-build --no-deps --force-recreate api
  ```
  Then verify with the `get_settings()` command above; it should show `wss://ielets-dajbgqlm.livekit.cloud` and `mariam-agent`.
- **Hosted V2 agent:** it can stay deployed (it's idle without dispatches). `lk agent rollback` / `lk agent delete` must only ever be run with the **Tamakkan_Agent** `livekit.toml`.
- **Frontend** (only if it was rebuilt for the new App ID): redeploy the previous image or build.

Running the local worker (for reference):

```powershell
$env:TAMAKKAN_ENV_FILE = "livekit_agent\.env.tamakkan-v2"; $env:PYTHONIOENCODING = "utf-8"
livekit_agent\.venv\Scripts\python.exe -m livekit_agent.agent dev
```

## 9. Outstanding blockers and decisions

1. *(resolved)* `AGENT_INTERNAL_API_KEY` is set and its fingerprint matches production.
2. *(resolved)* `BACKEND_API_URL` is set and verified.
3. VPS facts are unverified: deployed branch and commit, frontend SpatialReal App ID, old agent health (section 7). There is no SSH access from this machine.
4. Decision: LiveKit Cloud session recording/observability for student sessions (note E).
5. Decision: hosted agent region.
6. Approval to run `lk agent create` (it creates a cloud resource) and, separately, to change production.
