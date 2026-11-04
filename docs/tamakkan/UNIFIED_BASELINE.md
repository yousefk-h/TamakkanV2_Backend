# Tamakkan unified local baseline (`dev/tamakkan-unified`)

Created 2026-09-29. **This is the single local development baseline for both repos.**
Not pushed. Not merged into `main`. Nothing deployed. No production access was used.

| | Backend | Frontend |
|---|---|---|
| Branch | `dev/tamakkan-unified` | `dev/tamakkan-unified` |
| Worktree | `worktrees/backend-unified` | `worktrees/frontend-unified` |
| Started from | `integration/tamakkan-local` `630645f` | `integration/tamakkan-local` `1c183f6` |
| `main` (read-only reference) | `d6815f4` | `255b34b` |

Older branches, worktrees, the H1 bundle and the `tamakkan-agent-v2` checkout are untouched.
Continue all work from the two unified worktrees.

## 1. What is incorporated (every commit)

Backend (on top of `main` `d6815f4`):
1. `1c333d5` isolate backend image; manual commit-pinned deploys
2. `9856b97` ignore comments when checking shared legacy tags
3. `0e80f74` never rebuild an existing commit-tagged image
4. `630645f` merge of the Docker isolation branch
5. **new** `4db0d2b` `livekit_agent/livekit.toml` -> dedicated `Tamakkan_Agent` project
   (`tamakkan-agent-m4zc52ik`, agent `CA_LBTsvNPdKaHR`), plus guard test
   `livekit_agent/tests/test_deploy_target.py` that fails if the legacy shared agent IDs return.
6. **new** docs under `docs/tamakkan/` (this file, previous handover/plan/runbook, agent V2 handover,
   two VPS read-only scripts). Secret-scanned before adding.

Frontend (on top of `main` `255b34b`):
`010d697`, `3a1f01d` H1 (per-token refresh coalescing) + its 8 regression tests; `7160c0a`, `360b4a6`, `664f78d`
Docker isolation; merges `ebb54ce`, `1c183f6`. No new commits were needed.

Already inside `main` (nothing to transfer): every remote `feature/*` branch and the remote legacy integration branch
in both repos (checked against live `ls-remote` on 2026-09-29): the avatar, interruption,
OpenAI/Ultravox fallback, transcript and mobile fixes.

## 2. Local work deliberately NOT committed (needs your decision)

- `frontend/app/dev/mariam-v2-test/` (2 files): temporary V2 avatar test page. Its own header says
  "not for commit". Left untouched in the original checkout. Does not exist in the unified branch.
- `tamakkan-agent-v2/livekit_agent/livekit.legacy.toml`: old shared-agent IDs. Kept only there so it
  cannot be deployed from the unified tree. The legacy IDs are recorded in the guard test.
- `tamakkan-agent-v2/.venv` lacks the `livekit.agents` package, so the agent's own tests
  (`test_agent.py`, `test_prompts.py`) cannot be collected there. Pre-existing environment gap, not a code
  change; they were not run.

## 3. Production: what is known and what is NOT

**No authorized VPS access exists from this machine (no SSH). Production was NOT inspected.**
The unified branches therefore do **not** yet include, and must not be claimed to include,
production-only changes. Facts below come from the Sep 28/29 handovers and are unverified from here.

Git-tracked (in the unified branches): application code, Docker isolation, V2 agent target.
VPS-only (NOT in Git, NOT captured): the two `.env` files, `/root/tamakkan-compose/backend.override.yml`
and `frontend.override.yml`, the `tamakkan-backend:latest` / `tamakkan-frontend:latest` images
(and their build inputs, e.g. the frontend built with the new SpatialReal App ID), image TAR backups,
the LiveKit hosted-agent deployment, and any tracked-file edits on the VPS.

**Missing information (to collect read-only):** exact HEAD and branch of both server checkouts; whether any
tracked files are modified or any relevant untracked files exist there; the override files
(sanitized); resolved compose images; running image IDs and creation dates (to check whether they were built
from the checkouts); non-secret settings and value fingerprints. Give the administrator
`docs/tamakkan/tamakkan-vps-v2-export.sh` (read-only, writes only its own output folder, never
copies `.env`, sanitizes output). When the output returns, diff its tracked-changes and overrides
against this branch; only then incorporate any production-only source change.

## 4. Docker: local development vs current production runtime

| | Unified branch (local dev / future) | Production today (VPS-only) |
|---|---|---|
| Image | `tamakkan-platform-{backend,frontend}:<commit-sha>` (tag required, no fallback) | `tamakkan-{backend,frontend}:latest` |
| Selected by | `docker-compose.yml` in Git | `COMPOSE_FILE` in the server `.env` loading `/root/tamakkan-compose/*.override.yml` |
| Deploy | manual workflow + `deploy/tamakkan-deploy.sh`, health check, automatic rollback | not in Git; done by hand / the Sep 28 cutover script |
| Project name | pinned `$TAMAKKAN_BACKEND_PROJECT` / `$TAMAKKAN_FRONTEND_PROJECT` (matches prod) | same |

Consequences (unchanged, deliberately not "fixed" without the export):
- With the production overrides loaded, `tamakkan-deploy.sh` refuses to run (it requires the resolved image
  to be exactly `tamakkan-platform-*:<sha>`). Safe, but it means **the local runbook cannot be executed on the
  VPS unchanged**.
- Moving production to the commit-tagged scheme is a separate approved cutover: retire the overrides, keep
  the current `.env`, run the first deploy so the script pins the running image as the rollback tag.
- Never build the shared legacy tags. The workflow's verify step and compose both enforce it.
- The old auto-deploy workflow on remote `main` is still live; do not push `main` until it is replaced
  or disabled.

## 5. Validation of the unified baseline (2026-09-29, local only)

| Check | Result |
|---|---|
| Backend `pytest tests` | **534 passed** (6m10s) |
| Guard test `test_deploy_target.py` | 1 passed |
| Frontend H1 `npm run test:bff` | **8/8 passed** |
| Frontend `npm run lint` | exit 0 |
| Frontend `npm run build` | bundle and `dist/standalone` produced. Script exits 66 ("Missing packaged Sites manifest"), **identically on the previous integration branch**, so not a regression |
| Compose resolution (both repos) | `tamakkan-platform-*:<sha>`, project pinned; missing tag is rejected |
| Combined run, isolated DB `tamakkan_integration` (`127.0.0.1` ports 4683/5196) | backend health 200, pages `/`, `/admin`, `/student`, `/verify-email` 200; root login via backend and BFF 200, auth cookies set with the `tamakkanint` prefix, `/me` and `/topics` 200 through the BFF, role guards return 403 as designed; 0 errors in backend log |

Not re-run: the previous 54/55 full API sweep and the local rollback drill (files unchanged since they
passed; only `livekit.toml` and a test were added). No Docker image was built in this session.

Needs real providers (not testable locally): LiveKit/SpatialReal/OpenAI/Ultravox sessions, the full
student lifecycle (`connected`, `ended`, transcript, report), Gemini, Cloudinary, YouTube, SMTP.
The avatar-prompt-in-token issue (audit B1) needs LiveKit.

## 6. Open items and conflicts

1. Production comparison not done (section 3).
2. Docker scheme reconciliation needs the override files (section 4).
3. Remote auto-deploy on `main` still active.
4. `tamakkan-agent-v2` clone's `origin` is `tamakkan_backend.git` while the others use the legacy backend repository URL;
   heads are identical (redirect/rename?). Confirm before changing any remote.
5. Handover says the VPS branch is the legacy integration branch at `d6815f4`/`255b34b`, which are `main` merge
   commits, not the branch tips (`5c1fbc8`/`70e307f`). Content is equivalent; verify on the VPS.
6. MongoDB: still the legacy database name (`$TAMAKKAN_DB_NAME`) in production; the `Tamakkan` database migration is unconfirmed. No change made.
7. Audit hotfixes H0, H2-H7 and structural findings (B1, B2, B3, A1) are not implemented anywhere.
   Not started, per instructions.
8. Documents `TAMAKKAN_HANDOVER.md`, `TAMAKKAN_MIGRATION_PLAN.md` and `TAMAKKAN_DOCKER_ISOLATION_DEPLOYMENT.md`
   in this folder predate V2 and the production image names; treat them as history. This file supersedes their status sections.

## 7. Local test environment

Isolated MongoDB container `tamakkan-integration-mongo` (`127.0.0.1:27039`, DB `tamakkan_integration`).
Backend env file `.env.integration` is git-ignored and copied into the worktree (contains no real credentials).
Start: `TAMAKKAN_ENV_FILE=.env.integration python -m uvicorn app.main:app --port 4683` (backend venv:
`backend/.venv`); frontend: build then `API_BASE_URL=http://127.0.0.1:4683/api/v1 AUTH_COOKIE_PREFIX=tamakkanint PORT=5196 node dist/standalone/server.js`.
