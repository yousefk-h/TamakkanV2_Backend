# TAMAKKAN — Migration & Remediation Plan

> **Single source of truth** for moving this project from its legacy naming to **TAMAKKAN**, keeping it isolated from the other products, and fixing the defects found in the code audit.
>
> **Status as of 2026-09-28.** Every step below needs explicit approval before it is executed. Current state and practical details: `TAMAKKAN_HANDOVER.md`.

> **Naming convention.** Server- and account-specific legacy names are not stored in this repository. They appear
> as variables; the administrator holds the current values. Tamakkan: `$BACKEND_GITHUB_REPO`, `$FRONTEND_GITHUB_REPO`
> (GitHub names until the step 7 rename), `$TAMAKKAN_BACKEND_DIR`, `$TAMAKKAN_FRONTEND_DIR`, `$TAMAKKAN_BACKEND_PROJECT`,
> `$TAMAKKAN_FRONTEND_PROJECT`, `$TAMAKKAN_BACKEND_CONTAINER`, `$TAMAKKAN_FRONTEND_CONTAINER`. Shared legacy images:
> `$LEGACY_BACKEND_IMAGE`, `$LEGACY_FRONTEND_IMAGE` (repositories), `$SHARED_BACKEND_TAG`, `$SHARED_FRONTEND_TAG` (their
> `:latest` tags). Databases on the shared Mongo server: `$DB_LEGACY` (named after the legacy brand) and `$DB_LLM`
> (named after Sibling LLM). Legacy Cloudinary root folder: `$LEGACY_CLOUDINARY_FOLDER`. **Sibling LLM** is the
> separate LLM product on the same VPS: `$LLM_BACKEND_REPO`, `$LLM_FRONTEND_REPO`, `$LLM_BACKEND_DIR`, `$LLM_FRONTEND_DIR`,
> `$LLM_BACKEND_CONTAINER`, `$LLM_FRONTEND_CONTAINER`. Its local folder under `Full Projects` is the Sibling LLM folder.

**Status legend:** ✅ done · ⏳ waiting on someone · ⛔ blocked · ☐ not started.
**Evidence labels:** **V** = verified · **I** = inferred · **U** = unknown.

---

## Part A — Project identity and scope

| | **TAMAKKAN (this project)** | Sibling LLM | Axis | `tamakan_*` (older design) |
|---|---|---|---|---|
| GitHub | `$BACKEND_GITHUB_REPO`, `$FRONTEND_GITHUB_REPO` → **to become `tamakkan_backend`, `tamakkan_frontend`** | `$LLM_BACKEND_REPO`, `$LLM_FRONTEND_REPO` | `axis_backend`, `axis_frontend` | `tamakan_backend`, `tamakan_frontend` |
| Relationship | the product being migrated | **separate product: do not touch** | **separate product: do not touch** | out of scope: leave untouched |
| Local copy for this work | `Test Tamakkan\<legacy-named folders>` → **`Test Tamakkan\tamakkan_*`** | `Full Projects\<Sibling LLM folder>\<Sibling LLM repos>` | `Full Projects\Axis\axis_*` | `Full Projects\Tamakan new design\*` |

**Note:** The two checkouts in the Sibling LLM folder under `Full Projects` are a second clone of **Tamakkan's** repos (same commits, no H1), despite the parent folder's name. They are left untouched. Never push H1 from there.

**Decisions already made:**
- **D1:** `tamakan_*` stays untouched.
- **D2:** Sibling LLM stays fully separate.
- **D3:** keep the existing cookie names for now.
- **D4:** keep all existing uploads, paths and references; a `Tamakkan/` folder for *future* uploads comes later, after a compatibility check.
- **D5:** verify the real domains and nginx before proposing any domain change.
- **D6:** temporary ports only for isolated testing; final ports are decided after the VPS check.

---

## Part B — Infrastructure isolation (from the GitHub-side investigation)

### B1. Current infrastructure
| | Tamakkan | Sibling LLM |
|---|---|---|
| Deployed commit | backend `d6815f4`, frontend `255b34b` **V** | `b808297` / `ba97985` **V** |
| Deploy trigger | every push to `main` **V** | every push to `main` **V** |
| VPS directories | `$TAMAKKAN_BACKEND_DIR`, `$TAMAKKAN_FRONTEND_DIR` **V** | `$LLM_BACKEND_DIR`, `$LLM_FRONTEND_DIR` **V** |
| Containers | `$TAMAKKAN_BACKEND_CONTAINER`, `$TAMAKKAN_FRONTEND_CONTAINER` **V** | `$LLM_BACKEND_CONTAINER`, `$LLM_FRONTEND_CONTAINER` **V** |
| Docker images | `$SHARED_BACKEND_TAG`, `$SHARED_FRONTEND_TAG` **V** | **the same tags** **V** |
| Ports (127.0.0.1) | API 4652, web 6356 **V** (config) | API 3524, web 6868 **V** (config) |
| Frontend → API | example `localhost:4652`; production **U** | **example `localhost:4652` = Tamakkan's port**; production **U** |
| Database | default `$DB_LEGACY`; production **U** | **default `$DB_LEGACY`**; production **U** |
| LiveKit agent name | `mariam-agent` (configurable) **V** | **`mariam-agent`, hard-coded** **V** |
| Cookies | legacy-prefixed **V** | **the same names, hard-coded** **V** |
| Same server? | — | **I** (same layout); **U** |

**Axis:** fixed its own workflows on 2026-09-28. They are now manual-only, use their own directory variables, and refuse to run unless the compose name is `axis-…`. It has no secrets or variables set. **Out of scope from here on.**

### B2. Cross-project conflicts
| # | Conflict | Status | Correction (Tamakkan side only) |
|---|---|---|---|
| C1 | Shared legacy image tags (`$SHARED_BACKEND_TAG`, `$SHARED_FRONTEND_TAG`) | confirmed in config; harmful if on the same server | Tamakkan builds its own `tamakkan_backend:<sha>` / `tamakkan_frontend:<sha>` |
| C2 | Shared agent name `mariam-agent` | confirmed in code; harmful if on the same LiveKit project | Tamakkan uses its own agent name (e.g. `tamakkan-mariam`), deployed agent-first |
| C3 | Possibly the same database (`$DB_LEGACY`) | **U: the key question** | If shared: **stop**, and handle it as a separate data-isolation project |
| C4 | Sibling LLM frontend may call Tamakkan's API | **U** | Inform the Sibling LLM owner **before** any Tamakkan backend restart |
| C5 | Same cookie names | **U** (depends on domains) | Only if the hostnames are shared (D3) |
| C7/C8 | Shared SpatialReal app or provider credentials | **U** | Script §Q2/Q3 compares them as same/different; any split is a separate approval |
| C9 | Tamakkan deploys on every push | **V** | Manual-only deploys before any merge |

---

## Part C — Stages and status

| Stage | What | Status |
|---|---|---|
| **0** | H1 backup bundle outside the repo, verified by restoring into a fresh clone | ✅ `h1-bff-refresh-coalescing.bundle`, SHA-256 `847eed5f…34d6d0`, 8/8 tests passed after restore |
| **1a** | GitHub-side verification (repos, workflows, deploy logs, secret names, org-wide workflow scan) | ✅ |
| **1b** | **VPS verification**: the admin runs `tamakkan-vps-readonly-checks.sh` (v2, reviewed, output sanitised) and returns the report and template | ⏳ with the VPS administrator (email text in the handover) |
| **1c** | LiveKit Cloud owner: which agents are registered as `mariam-agent`, in which project | ⏳ |
| **L** | **Local folder rename** legacy names → `tamakkan_*` (local only) | ⏳ blocked by open file handles (VS Code / OneDrive): you run `rename-local-folders.ps1` (see the handover) |

### Gates after 1b/1c
- **G-DB:** if Tamakkan and Sibling LLM share one database → **stop everything below.** This becomes a separate, approved data-isolation project.
- **G-FE:** if Sibling LLM's frontend calls Tamakkan's API → notify its owner before any Tamakkan backend restart.
- **G-LK:** if they share a LiveKit project → the agent-name split (step 5) is required before Tamakkan's next *backend/agent* release. It does **not** block H1.

---

## Part D — Execution sequence (each step needs separate approval)

| Step | Action | Needs | Production effect | Rollback |
|---|---|---|---|---|
| 1 | Evaluate G-DB / G-FE / G-LK from the admin's report | — | none | — |
| 2 | **Manual-only deploys** for both Tamakkan repos: `workflow_dispatch` + `concurrency`; remove the no-op `npm install --force`. Merging this change does not deploy. | PR (push) + reviewer | none | revert the PR |
| 3 | Branch protection on `main` | **GitHub admin** | none | settings |
| 4 | **Image isolation release:** compose `image: tamakkan_*:${IMAGE_TAG}`, **project name pinned to the current `$TAMAKKAN_BACKEND_PROJECT` / `$TAMAKKAN_FRONTEND_PROJECT`** (same containers, directories, ports); SHA tags, health gate, auto-revert. First pin the running images: `docker tag <running-id> tamakkan_*:pre-isolation`. | PR + **manual deploy** + VPS | one normal container recreate per service; identical code | redeploy the pinned image |
| 4v | Verify health, login, admin and student smoke tests, same database name in the startup log; **Sibling LLM containers unchanged** | VPS (read-only) | — | — |
| **5** | **H1:** push `fix/h1-bff-refresh-coalescing` from `Test Tamakkan\tamakkan_frontend` → PR → review → merge → **manual run of the isolated workflow** (frontend only) | push + reviewer + deploy approval | frontend container recreate | redeploy the previous `tamakkan_frontend:<sha>` |
| 6 | Agent-name split, if G-LK: deploy the `tamakkan-mariam` agent first, then set `LIVEKIT_AGENT_NAME` in Tamakkan's backend `.env` | LiveKit Cloud + VPS | new sessions go to Tamakkan's own agent | revert the `.env` value |
| 7 | **Rename GitHub repos** → `tamakkan_backend` / `tamakkan_frontend`; `git remote set-url` locally. The VPS keeps working through GitHub's redirect. **Never recreate the old names.** | **GitHub admin** | none | rename back |
| 8 | **Isolated test stack** under the new directory names. It has its own Mongo container, volume and database, **empty LiveKit / Gemini / Cloudinary keys**, logging-only email, `tamakkan_test` cookies, temporary 127.0.0.1 ports, no nginx entry, and SSH-tunnel access only. | VPS | none on production | `down -v` of the **test** project only |
| 9 | **Production directory cutover**, sequential with no overlap on the production database: pre-check that no live sessions are active → `stop` the old stack (not `down`, no `-v`) → `up` the new stack at the **same commit/image and ports** from `/root/tamakkan_backend` (compose `name: tamakkan_backend`) → verify → frontend likewise → then merge the workflow path change. The old directories are kept for 30 days. | VPS, maintenance window | ~15–60 s interruption per service | stop new, start old |
| 10 | **Branding by configuration:** `NEXT_PUBLIC_APP_BRAND=tamakkan` (frontend rebuild), `PROJECT_NAME`, `EMAIL_BRAND_NAME` | VPS `.env` + manual deploy | visible rebrand | revert the config |
| 11 | Hard-coded strings: reset-email subject (`auth_service.py:99`), Swagger text (`application.py:23,28`), **tutor prompt** (`livekit_agent/prompts.py:43`, LiveKit Cloud deploy); remove the database-name default (fail fast); docs and examples | PRs + deploys | text only | redeploy previous |
| 12 | Deferred: cookie prefix (D3), `Tamakkan/` folder for new uploads (D4), domains (D5), `TAMAKKAN_ENV_FILE` with fallback, retire the legacy brand code | separate approvals | — | — |

**Never, at any step:** rename, copy or migrate a database · move Cloudinary assets or their stored ids · run `docker compose down -v` / `volume prune` / `system prune` on production · touch Sibling LLM, Axis or `tamakan_*` · rewrite Git history.

---

## Part E — Naming inventory (what gets renamed, and what doesn't)

| Category | Examples | Plan |
|---|---|---|
| **User-facing** | The legacy brand is the frontend's default brand (`lib/branding.ts`, `Logo.tsx`, `globals.css`, `auth.login.title`, `Dockerfile` `ARG NEXT_PUBLIC_APP_BRAND` with the legacy default); the reset-email subject; Swagger text; the tutor prompt | Steps 10–11. **The Tamakkan brand already exists in the code** (logo, colours, titles), so switching is configuration. |
| **Identifiers and config** | `MONGODB_DB_NAME` default `$DB_LEGACY`; the legacy cookie prefix; `TAMAKKAN_ENV_FILE`; dead `LEARNING_ANALYTICS_PROFILE`; Cloudinary folders `$LEGACY_CLOUDINARY_FOLDER/…` | Database name: **never renamed** (a data migration, with no benefit). Cookie prefix: D3. Env-var name: optional, with fallback. Dead setting: delete. Cloudinary: existing paths untouched (D4). |
| **Deployment** | workflow names and paths; image tags; implied compose project names; VPS directories; nginx examples | Steps 2, 4, 7–9 |
| **Historical (preserve)** | Git history; branch the legacy integration branch; `Tamakkan-*.md/.pdf/.csv` planning docs; existing Cloudinary paths; provider ids (LiveKit project and agent id, SpatialReal ids, Cloudinary cloud name, GitHub org) | Never rewritten. Old planning docs may move to `docs/history/` later. |

---

## Part F — Code remediation roadmap (from the code audit)

Only **H1** is approved and implemented (local branch). Everything else awaits approval.

**Evidence:**
- Backend suite: 534 passed.
- Frontend: ESLint clean; `tsc` has 1 pre-existing error from dead scaffolding.
- H1 test: 8/8.
- Reproduced defects: F1, B2, B1.

### F1. Top findings
| ID | Finding | Level | Severity |
|---|---|---|---|
| F1 | BFF refresh coalescing gave one user another user's session | **reproduced → fixed (H1)** | Critical |
| B2 | Refresh-token rotation not atomic; no reuse detection | reproduced | High |
| B1 | Avatar prompt readable in the student's LiveKit token | reproduced | High |
| B3 | No way to deactivate or delete a user | confirmed | High |
| F8 | Lesson builder overwrites unsaved edits while polling | confirmed | High |
| D1 | Deploy without tests or rollback; `npm install --force` | confirmed | High |
| A1 | Agent keeps the transcript in memory only and sends it once | confirmed | High |
| U1/U2 | Upload limits (nginx 1 MB example; 30 MB vs 80 MB) | suspected in production | High if true |
| B10–B14 | Lost updates, `force_end`, stuck sessions, template/transcript races | confirmed | Medium |
| F2–F6 | BFF open proxy, buffering, CSRF, timeouts | confirmed/suspected | Medium–High |

### F2. Hotfix track (small, isolated, no schema change)
- **H0:** minimal CI gate; `testpaths = tests`; manual deploys.
- **H1:** ✅ per-token refresh coalescing (local branch; deployment per Part D step 5).
- **H2:** verify, then fix, the upload limits (ops first).
- **H3:** ProgramBuilder stops overwriting edits.
- **H4:** backend validation and error mapping (`day`, English review 500s, background reset email, `compare_digest`).
- **H5:** `force_end` sets state and `ended_at` first; LiveKit best-effort.
- **H6:** Recorder leaks and role-gated hooks.
- **H7:** remove the dead Cloudflare/D1 scaffold (fixes `tsc`).

### F3. Structural track
- **S0:** rollbackable SHA-tagged deploys.
- **S1:** real-Mongo CI tests.
- **S2:** **refresh-token redesign:**
  - atomic rotation;
  - deterministic successor `HMAC(key, old_raw)`, so the stored-hash-only model is kept;
  - 15 s grace window;
  - family revocation on reuse;
  - `token_version`;
  - no mass logout; safe rollback.
- **S3:** account deactivation, reset-token invalidation, rate limits, proxy allowlist and CSRF.
- **S4:** **reliability core:**
  - one atomic `transition()` primitive;
  - revised session state machine;
  - sequence-numbered transcript checkpoints;
  - a durable report queue on the session document with **lease-generation fencing** (every worker write is filtered on its `report_lease_gen`, so an expired worker can never overwrite a newer result);
  - a worker/reaper service;
  - an idempotency matrix.
- **S5:** agent retries and checkpointing.
- **S6:** remove the avatar prompt from the token (backend → agent → backend sequence).
- **S7:** performance (projections, N+1, TTL indexes).
- **S8:** frontend robustness (global 401, dialogs, error states).
- **S9:** infrastructure (nginx, TLS, readiness, GHCR images).
- **S10:** consistency and cleanup (timezone handling after the day-boundary decision, OpenAPI types, i18n/RTL).

### F4. Database rules for every release
- expand/contract;
- new fields optional;
- unique indexes created out of band after a duplicate pre-check (Beanie creates declared indexes at startup, and a failing one stops the app from booting);
- TTL indexes delete data irreversibly;
- backfills are idempotent scripts run after a `mongodump`;
- no multi-document transactions (production may be a standalone server).

### F5. Open product decisions
- topic ownership (shared vs per instructor);
- Asia/Amman vs UTC day boundaries and class times;
- server-side "watch video first";
- refresh grace length and absolute session lifetime;
- Root visibility of internal topic fields;
- whether to score infra-interrupted sessions.


---

# Appendix — Code audit v2 (full text, preserved verbatim)

> Moved here from the earlier planning file so all Tamakkan plans live in this folder. H1 (its step "H1" below) is now implemented on the local branch; see `TAMAKKAN_HANDOVER.md`. Folder and repo names below are the pre-rename ones.


> Still planning only. No project source, dependency, environment file or database was modified while producing this revision. §2 lists exactly what was executed and what the earlier setup session did touch.

## Context
v1 of this audit was reviewed and returned with seven demands:
1. Prove the cross-user refresh bug, and design any grace window properly.
2. Re-justify the priorities.
3. Verify the scope, and explain how the database observations were obtained.
4. Explain concretely how CI blocks deploys, how images are retained, and how database changes affect rollback.
5. Give one coordinated reliability design instead of isolated fixes.
6. Classify every finding by evidence level and report the tests actually run.
7. Deliver a revised roadmap with per-step files, tests, dependencies, database impact, breaking changes, rollback and acceptance criteria.

This document replaces v1. v1's detailed findings catalogue is kept in condensed form in Appendix A.

---

## 1. Answers at a glance
| Demand | Answer | Section |
|---|---|---|
| Prove the refresh bug | **Reproduced** against the unmodified `route.ts`: Bob's request returned Alice's data and Bob's cookies now hold Alice's access token and 7-day refresh token. The candidate per-token fix also verified. The backend rotation race is **reproduced** too (under realistic read latency, one token forks into two live sessions, and there is no reuse detection). | §3 |
| Grace window | Designed around deterministic successor derivation (HMAC of the old raw token), so the hashed-only storage model is kept. Plus atomic rotation, family reuse detection, and a per-user `token_version`. Analysed for replay, concurrency and multiple instances. | §4 |
| Priorities | v1 grouped work by theme. v2 uses **two tracks**: a hotfix track (small, isolated, no schema change; includes lesson-edit loss and the upload-limit check) that starts immediately, and a structural track. | §6, §9 |
| Scope | Both repos on `main` with clean working trees (no tracked or untracked changes). Exact SHAs are below. The database observations came from a read-only listing I ran during the earlier setup session using the `.env` built from your pasted values. That session's startup also ran Beanie index creation against the shared database, which I disclose in full. | §2 |
| CI and rollback | `needs:` gating plus branch protection; SHA-tagged images with the previous tag recorded; health-gated deploy with automatic revert; expand/contract database rules, including the Beanie-specific hazard that an index which fails to build crashes startup. | §7 |
| Reliability | A single transition primitive, a revised session state machine, sequence-numbered transcript checkpoints, a durable report queue on the session document, a worker service (reaper and report retries), and an idempotency matrix. | §8 |
| Evidence levels | Each finding is tagged REPRODUCED, CONFIRMED-VERIFIED, CONFIRMED-AGENT, SUSPECTED or DECISION. | §5 |

---

## 2. Scope verification and disclosure

### 2.1 Repositories audited
| Repo | Branch | HEAD | Commit | the remote legacy integration branch | Working tree |
|---|---|---|---|---|---|
| backend | `main` | `d6815f476fbf32dc0b16915f1cccd3c7a25db4de` | 2026-09-23 13:10 +0300, "Merge pull request #9 … <legacy integration branch>" | `5c1fbc8e…`, an ancestor of HEAD (0 commits ahead) | **clean** (`git status --porcelain` is empty) |
| frontend | `main` | `255b34b8daea54509f6583341f802d601bc7b94f` | 2026-09-23 13:11 +0300, "Merge pull request #12 … <legacy integration branch>" | `70e307f5…`, an ancestor of HEAD (0 commits ahead) | **clean** |

The ignored files present on disk are **all from my earlier setup session**, and none of them are tracked:
- **Backend:** `.env` (your pasted values), `.env.tamakkan` (generated by the Tamakkan launcher), `.tamakkan/` (local demo credentials), `.venv/`, `__pycache__/`.
- **Frontend:** `.env.local` (points to local port 4654), `.env.tamakkan`, `node_modules/`, `.vinext/`, `.sites-runtime/`.

### 2.2 What the earlier setup session did (before the audit request)
1. **Cloned** both repos, created the backend `.venv` and ran `npm ci` in the frontend.
2. **Tamakkan local stack:** ran a Docker Mongo container `tamakkan-mongo` (127.0.0.1:27019, volume `tamakkan-mongo-data`), the launchers on 4653/5176, and seeded 3 demo accounts into the **local** `tamakkan_dev` database only.
3. **Legacy-named stack from your pasted values:**
   - I wrote them into `backend/.env` (removing a leading space from `MONGODB_URI`) and started a backend on **4654** against the shared server `10.8.0.1:29525`, database `$DB_LEGACY`, plus a frontend on 5177.
   - **Writes to the shared database: Beanie's `init_beanie()` ran `createIndexes` for all 13 models** (`skip_indexes=False` by default, `.venv/Lib/site-packages/beanie/odm/utils/init.py:541,595`). Creating an index that already exists with the same spec is a no-op, and Beanie never drops indexes (`allow_index_dropping=False`). If any declared index was missing on `$DB_LEGACY`, it was created. I have not checked which case applied, because that needs another connection to the shared database.
   - No documents were written. The only requests were `GET /health` (no database) and one `POST /auth/login` that failed validation with a 422 before any database access.
   - ⚠ **That backend process (port 4654) is still running and connected to the shared database.** I recommend stopping it; I have not done so, because this task forbids environment changes without your approval.
4. **How the "database observations" were obtained:**
   - While answering your question about which database the app was using, I ran a one-off script, written to the OS temp directory (outside both repos), that read `MONGODB_URI` from that `.env`.
   - It called only `list_database_names`, `list_collection_names` and `estimated_document_count` on `users`. These are read-only metadata operations.
   - Result: database `$DB_LEGACY` has 28 user documents and `$DB_LLM` has 24 (plus other unrelated databases on the same server).
   - The *audit agents* never opened any real env file. The observation comes from that earlier session, not from the audit.
   - **Why it matters:** `database.py:63-66` uses `MONGODB_DB_NAME` (`$DB_LEGACY`) and ignores the `/$DB_LLM` path in the URI. Which database is the intended production one is an open question (Q1).
5. I also noticed, without touching them, that a different legacy-named backend (4652) and frontend (5173) were already running from `…\Full Projects\<Sibling LLM folder>\`. I killed only my own frontend process (PID 14484), after confirming its command line.

### 2.3 What was executed for this revision (all read-only)
| # | Command / method | Writes? | Result |
|---|---|---|---|
| E1 | `git rev-parse`, `git status --porcelain [--ignored]` | none | §2.1 |
| E2 | F1 reproduction: real `route.ts` bundled **in memory** with the project's esbuild 0.28 (`write:false`); only `fetch` and `next/headers` mocked; per-request cookie jars via AsyncLocalStorage | none | **Reproduced** (§3.1) |
| E3 | B2 reproduction: real `AuthService.refresh` and repositories against in-memory `mongomock-motor` (the same setup as `tests/conftest.py`), settings from `.env.example`, `PYTHONDONTWRITEBYTECODE=1` | none | **Reproduced under latency** (§3.2) |
| E4 | `pytest tests -p no:cacheprovider` with `TAMAKKAN_ENV_FILE=.env.example`, `PYTHONDONTWRITEBYTECODE=1`; git status diffed before and after | none (diff empty) | **534 passed, 0 failed**, 28 warnings (PyJWT `InsecureKeyLengthWarning`: a 29-byte HMAC key in the test settings), 398 s |
| E5 | Bare `pytest --collect-only` (as in the README) | none | **Interrupted: 2 collection errors** (`livekit_agent/tests/*`: `cannot import name 'agents' from 'livekit'`) |
| E6 | `tsc --noEmit --incremental false` (frontend) | none | **1 error**: `db/index.ts(1,21) TS2307 'cloudflare:workers'`. The only error is in the unused template scaffold; app, components and lib typecheck clean. |
| E7 | `eslint .` (frontend, ignoring build directories); tree diffed | none | **0 problems** |
| E8 | B1 reproduction: the real `livekit.api.AccessToken…with_room_config(...)` built offline with dummy keys, then the JWT payload base64-decoded | none | **Reproduced**: `roomConfig` is in the student JWT and the metadata text is readable without the key |
| E9 | Manual code verification: `force_end`, session transitions, refresh repository, ProgramBuilder seeding and polling, `soft_delete` callers, the `day` param, deploy workflow, nginx example, `pytest.ini`, the frontend test string, Beanie index init | none | see §5 |

**Not executed:**
- Any test against a real MongoDB.
- Any browser or E2E run.
- Anything against production (nginx, the VPS `.env`, TLS).
- Worker (`livekit_agent`) tests, because their dependencies are not installed and installing them was out of scope.
- The frontend `npm test` / `npm run build`: they are wired to "OpenAI Sites" artefacts and are known to be unrunnable (CONFIRMED-AGENT). Running them would also create `.sites-runtime` and `dist` output.

---

## 3. Reproductions

### 3.1 F1: cross-user session swap in the BFF refresh (REPRODUCED)

**Mechanism:**
- `refreshInFlight` is a module-level promise shared by every request in the Node process (`app/api/backend/[...path]/route.ts:39-46`).
- A request that needs a refresh while *another user's* refresh is in flight awaits that other user's promise.
- It then writes the resulting `TokenPair` into its **own** cookie jar (`:94-96`).

**Test harness (reproducible, no files):**
- esbuild bundles the real `route.ts` together with `lib/api/server/backend.ts` into memory.
- `import {cookies} from "next/headers"` is replaced by a per-request jar lookup through AsyncLocalStorage, and `fetch` is replaced by a fake FastAPI.
- The fake `/auth/refresh` takes 50 ms and returns user-specific tokens. `/me` returns 401 for expired tokens and the identity for valid ones.
- Alice and Bob, each with an expired access cookie and their own refresh cookie, call `GET /api/backend/me` 5 ms apart.
- The script is kept verbatim in the session log; Phase 1 turns it into a committed regression test (step H1).

**Output against the unmodified route:**
```
refresh tokens sent to backend: ["refresh-alice"]
alice -> HTTP 200 body={"me":"alice"} cookies={access:"access-alice", refresh:"refresh2-alice"}
bob   -> HTTP 200 body={"me":"alice"} cookies={access:"access-alice", refresh:"refresh2-alice"}
RESULT: VULNERABLE - bob's browser now holds alice's session
```

**Output with the candidate fix (a `Map` keyed by refresh token, applied to the in-memory copy only):**
```
refresh tokens sent to backend: ["refresh-alice","refresh-bob"]
alice -> {"me":"alice"} …alice cookies ; bob -> {"me":"bob"} …bob cookies
RESULT: OK
```

**Security implications:**
- Bob receives Alice's **7-day refresh token**, so it is a full, persistent account takeover, not a one-off data leak.
- If Alice is an administrator, Bob gets her admin rights.
- Bob's own session silently disappears, because his refresh token is never used.

**When it triggers:** any two users whose access tokens expired (every 30 minutes of activity) and whose refreshes overlap within one backend round trip on the same frontend process. That is likely at class start times when many students are active together.

**What is not proven:** the rate in production. The reproduction shows the mechanism, not how often it happens.

### 3.2 B2: non-atomic refresh rotation (REPRODUCED, with a condition)
Real `AuthService.refresh` code, with two concurrent refreshes presenting the **same** token:

| Condition | Succeeded | Live refresh tokens after | Old token replayed afterwards |
|---|---|---|---|
| mongomock as-is (does not yield between read and write) | 1/2 | 1 | rejected; nothing revoked |
| 10 ms latency added after the repository read (models a real network read) | **2/2** | **2 (the session forked)** | rejected; **the 2 live tokens are not revoked**, so there is no reuse detection |

**Implications:**
- A stolen refresh token that is used at the same moment as the legitimate client yields an independent session that survives the legitimate client's rotation.
- A replay is never treated as a theft signal.
- **The existing suite cannot catch this**, because mongomock serialises operations. That is why Phase 0 adds real-Mongo tests.

### 3.3 B1: avatar prompt readable in the student token (REPRODUCED at SDK level)
`AccessToken.with_room_config(RoomConfiguration(agents=[RoomAgentDispatch(metadata=…)]))` places `roomConfig` in the JWT claims, and the metadata can be read with a plain base64 decode (E8). The production code builds exactly this token with `topic.avatar_prompt` inside the metadata (`live_session_service.py:108-141`, verified).

---

## 4. Refresh-token design (the v1 grace-window proposal revisited)

### 4.1 Current model (verified)
- The raw token is 32 random bytes (`utils/tokens.py`). Only `sha256(raw)` is stored (`RefreshToken.token_hash`, unique index).
- Rotation = read by hash → whole-document save `revoked=True` → insert a new token with a new 7-day expiry (sliding expiry).
- Logout and password changes set `revoked=True` document by document (`revoke_all_for_user` loop).

### 4.2 Why a naive grace window is wrong here
"Within N seconds, a retried rotation returns the same successor" needs the successor's **raw** token, which the server does not keep: only hashes are stored.
- Storing raw successors, even briefly, breaks the hashed-only model.
- Issuing a *new* sibling pair during grace forks the family, which is exactly the B2 defect.

### 4.3 Proposed design
1. **Deterministic successor.** `successor_raw = b64url(HMAC-SHA256(REFRESH_ROTATION_KEY, old_raw))`, and only `sha256(successor_raw)` is stored. Any backend instance can recompute the same successor from the presented old token, so nothing secret is stored. `REFRESH_ROTATION_KEY` is a new secret, distinct from `JWT_SECRET_KEY`.
2. **Atomic rotation.** `find_one_and_update({token_hash: h, revoked: false, expires_at: {$gt: now}}, {$set: {revoked: true, rotated_at: now, revoke_reason: "rotated"}})`. Exactly one concurrent caller wins.
3. **Idempotent successor insert.** Upsert on `token_hash` (the unique index already exists) with `$setOnInsert`; it carries `family_id` and `token_version`.
4. **Grace path.** If the atomic update matched nothing, read by hash:
   - `revoke_reason == "rotated"`, `rotated_at >= now − GRACE`, and `token_version == user.token_version`: recompute the successor, ensure it exists (upsert), and return it with a fresh access JWT. This is idempotent.
   - `revoke_reason == "rotated"` but outside the grace window: **reuse detected**. Revoke the whole family (`update_many({family_id}, revoked)`), log a security event, return 401.
   - `revoke_reason in {"logout", "password_change", "deactivated"}`: 401. There is **no grace for security revocations**.
5. **`token_version` on User.**
   - Every password change or reset, deactivation, and "log out everywhere" does `$inc: {token_version: 1}` in the same atomic update as the change itself.
   - Refresh validates `token.token_version == user.token_version`.
   - This closes the race where an in-flight rotation inserts a successor *after* `revoke_all` ran. The successor inherits the old version and is dead on arrival.
   - `revoke_all_for_user` becomes a single `update_many` for tidiness; correctness comes from the version check.
6. **Legacy compatibility.** Existing tokens have no `family_id` or `token_version`. Missing values are read as `family_id = own id` and `token_version = 0`, and `User.token_version` defaults to 0. **No mass logout** on deploy.
7. **Frontend.** Coalescing keyed per refresh token (the F1 fix). With the backend grace window this becomes an optimisation rather than a correctness requirement.

### 4.4 Security analysis
| Concern | Behaviour |
|---|---|
| **Token replay (theft)** | A stolen old token is useful only (a) before the legitimate rotation, as today, or (b) within GRACE after it, and in (b) it yields the *same* successor the legitimate client holds, not a new session. After GRACE, any replay revokes the family and both parties must re-login. That turns silent theft into a detectable event (the OAuth 2.1 BCP pattern for refresh-token reuse detection). **Net cost:** a stolen token stays useful up to GRACE seconds longer. Recommended GRACE is 15 s (DECISION D5). |
| **Concurrent requests** | The atomic update gives one winner; losers take the grace path and get the identical successor. In the §3.2 scenario the family stays at 1 live token. |
| **Multiple instances** | All state lives in Mongo and derivation is deterministic, so any mix of frontend or backend instances resolves identically. The frontend per-token map only reduces backend calls within one process. Cross-instance coalescing is not needed. |
| **Hashed storage** | Preserved: no raw token is ever persisted, successors included. |
| **Key compromise** | `REFRESH_ROTATION_KEY` alone yields nothing, because hashes are one-way. Key plus a stolen raw token yields that token's successors, which is no worse than holding the raw token. Rotating the key only breaks in-flight grace retries (seconds). |
| **Security revocations** | Never covered by grace (`revoke_reason`), and backed by `token_version`. |
| **Access tokens** | Unchanged: 30 minutes, no `jti`, still valid after revocation. This is an existing documented trade-off. Adding `token_version` to the JWT and checking it in `get_current_user` would give instant access revocation at no extra database cost, because the user is already loaded. Recommended, as step S3. |

**Alternative (not recommended):** no grace, strict single-use plus the frontend per-token map. That is correct only while the frontend runs as a single process and nothing else refreshes concurrently. Any second frontend instance, or a mobile client later, reintroduces forced logouts.

---

## 5. Findings by evidence level
**Legend:**
- **REPRODUCED:** demonstrated by execution in this revision.
- **CONFIRMED-VERIFIED:** code path read and checked by me personally.
- **CONFIRMED-AGENT:** traced with `file:line` by an audit agent and consistent with everything I spot-checked, but not independently re-read by me.
- **SUSPECTED:** plausible but depends on runtime or production facts not observed.
- **DECISION:** the correct behaviour is a product or ops choice.

| ID | Finding | Level | Severity |
|---|---|---|---|
| F1 | BFF refresh coalescing hands one user's session to another | **REPRODUCED** (E2) | Critical |
| B2 | Refresh rotation not atomic; no reuse detection | **REPRODUCED** (E3, under latency) | High |
| B1 | Avatar prompt in the student-readable LiveKit JWT | **REPRODUCED** (E8, SDK) + CONFIRMED-VERIFIED | High |
| T1 | README `pytest` fails at collection | **REPRODUCED** (E5) | Medium |
| T2 | Frontend typecheck fails, only because of the dead `db/index.ts` scaffold | **REPRODUCED** (E6) | Low |
| T3 | The backend suite passes (534) but is never run in CI | **REPRODUCED** (E4) + CONFIRMED-VERIFIED (workflow) | High |
| D1 | Backend deploy: `npm install --force`, no tests, no rollback, `:latest` overwritten | CONFIRMED-VERIFIED | High |
| F8 | ProgramBuilder resets every field (title, prompt, vocabulary, transcript) whenever `loadedTopic` changes, and the draft poll refetches every 4 s, so unsaved edits are lost | CONFIRMED-VERIFIED (`ProgramBuilder.tsx:78-91,139-147`); not reproduced in a browser | High |
| B3 | No way to deactivate or delete a user (`soft_delete` is only used on templates, classes and topics) | CONFIRMED-VERIFIED | High |
| B11 | `force_end` calls LiveKit before changing state, never sets `ended_at`, and bypasses the transition table | CONFIRMED-VERIFIED | Medium |
| B12 | CONNECTING→ENDED not allowed; no reaper; racy mark-ended | CONFIRMED-VERIFIED (table) + CONFIRMED-AGENT (race) | Medium |
| B15 | End-of-week `day` is an unvalidated `int` | CONFIRMED-VERIFIED (`submissions.py:53`) | Medium |
| U1 | The frontend nginx **example** sets no `client_max_body_size` (default 1 MB) | CONFIRMED-VERIFIED for the repo file; **SUSPECTED for production** (VPS config not seen) | High if true in production |
| U2 | `.env.example` `MAX_REQUEST_BODY_BYTES` of 30 MB, while videos are allowed up to 80 MB | CONFIRMED-AGENT; production value unknown (SUSPECTED) | Medium |
| F2, F4, F6, F7, F9, F10, F13, F14, F16–F22 | Proxy allowlist, buffering, fragility, no global 401, nested dialog, Recorder leaks, ungated hooks, duplicated rules, UX, i18n/RTL, types, dead code, a11y | CONFIRMED-AGENT | Medium–Low |
| F18 | `ClassModal` stores the admin's local input as UTC | CONFIRMED-AGENT; also DECISION (what existing data means) | Medium |
| B4–B7, B9, B10, B13, B14, B16–B25, B27 | Reset tokens, inline email, rate limits, verification lockout, topic deletion, lost updates, templates, transcript run token, validation, errors, report loss, performance, config, layering, duplication, dead code | CONFIRMED-AGENT | Medium–Low |
| A1–A3, A5 | Agent: in-memory transcript with a single send, no secret validation, shutdown recorded as user-ended, defaults | CONFIRMED-AGENT | High (A1) … Low |
| F3 | Path traversal inside the backend host via decoded segments | SUSPECTED (depends on vinext decoding) | Medium |
| F5 | Login CSRF via a `text/plain` form | SUSPECTED | Medium |
| F11 | Avatar or room not torn down after a runtime error; iOS autoplay | SUSPECTED | Medium |
| A4 | Agent timer cancellation skips room deletion; unreferenced task | SUSPECTED | Medium |
| I1 | Frontend container cannot reach `host.docker.internal:4652` on Linux (backend bound to 127.0.0.1) | SUSPECTED | High if true |
| I2 | Secure cookies over HTTP-only nginx break login unless TLS terminates upstream | SUSPECTED | High if true |
| I3 | IP rate limits would apply to everyone at once (no X-Forwarded-For) | SUSPECTED (no limiter exists yet) | Low |
| B8 | Topics have no owner | DECISION (D2) | Medium |
| B26/F18 | UTC vs Asia/Amman day boundaries and class times | DECISION (D3) | Medium |
| F15 | "Watch video first" enforced only on the client | DECISION (D4) | Low |
| — | Root sees internal topic fields | DECISION (D6) | Low |
| — | Which database is production (`$DB_LEGACY` vs `$DB_LLM`) | DECISION (Q1) | — |

---

## 6. Priorities reconsidered
v1 ordered work by theme (security → integrity → performance → frontend → infrastructure). That was wrong for three items:
- **F8 (lesson edits lost)** is a frontend-only change of about 20 lines with no API or schema impact, and it causes daily data loss for instructors. There was no reason to wait for Phase 4.
- **Video uploads (U1/U2)** may be failing *today*. The first step is **verification, not code**: ops reads the production nginx and `.env` values. If they are wrong, the fix is a config change that needs no application deploy. It belongs at the very start.
- **F1** is a 10-line change that was proven in §3.1. It should not wait for all of Phase 0. The only prerequisite is running its reproduction test (manually, if CI does not exist yet).

**v2 rule.** An item goes on the **Hotfix track** if it meets all of the following:
- It touches at most about 3 files in one repo.
- It needs no database schema, index or migration change.
- It needs no coordinated cross-repo deploy.
- It has a local reproduction or test.

Everything else goes on the **Structural track**, which follows Phase 0. The hotfix track still gets tests. For the first few hotfixes, the tests are run manually and recorded until CI exists (step H0 creates CI first thing, in parallel).

Deliberately **not** hotfixed:
- **F18 timezone:** changes how existing stored class times are interpreted; needs D3.
- **B1 avatar prompt:** needs a coordinated backend → agent → backend sequence.
- **B2 rotation:** changes token storage fields, even though compatible.

---

## 7. CI, deployment and rollback (concrete)

### 7.1 How failed tests block deployment
1. **Same-workflow gating.** Each repo's `deploy.yml` gets a `test` job, and the `deploy` job declares `needs: [test]`. If `test` fails or is cancelled, GitHub Actions skips `deploy`.
2. **Merge gating.** Branch protection on `main`: require PRs, require the `test` status check, and disallow direct pushes (the history shows direct "auto deploy" commits). **Prerequisite:** someone with repo admin rights on `kassel-for-information-technology` must enable it; to be confirmed (Q8).
3. **Serialised deploys.** `concurrency: {group: deploy-<repo>, cancel-in-progress: false}`.
4. **Backend `test` job:** Python 3.12, to match the Dockerfile; `pip install -r requirements-dev.txt`; `pytest tests` with `TAMAKKAN_ENV_FILE=.env.example`; plus a **real-Mongo service container** job for the concurrency and index tests added in later phases.
5. **Frontend `test` job:** `npm ci`, `eslint`, `tsc --noEmit` (after H-series step H7 removes the dead `db/`), then `npx vinext build`. This replaces the unrunnable "Sites" `build`/`test` scripts.

### 7.2 How previous images are retained
Today, `docker compose up -d --build` rebuilds `$SHARED_BACKEND_TAG` on the VPS and the previous image becomes a dangling layer, so there is no rollback target.

**Phase 0 (keeps the SSH model):**
- The deploy script builds `$LEGACY_BACKEND_IMAGE:${GITHUB_SHA}`. Compose uses `image: $LEGACY_BACKEND_IMAGE:${IMAGE_TAG}`, read from `$TAMAKKAN_BACKEND_DIR/.deploy.env`.
- Before switching, the script writes the current tag to `.deploy.previous`.
- It then runs `docker compose up -d` and a health gate that polls `/api/v1/health` (later `/health/ready`, which pings the database) for up to 60 s.
- **On failure it automatically resets `IMAGE_TAG` to the previous tag and brings that back up.** The job then fails loudly.
- It keeps the last 5 SHA-tagged images and prunes older ones.
- The frontend is identical, with `$LEGACY_FRONTEND_IMAGE:${SHA}`.

**Manual rollback:** `IMAGE_TAG=$(cat .deploy.previous) docker compose up -d`, or any retained SHA. It takes seconds and needs no rebuild.

**Phase 5 (better):** CI builds and pushes SHA-tagged images to GHCR; the VPS only pulls. That makes images immutable and reproducible, keeps builds off the production host, and allows rollback to any past SHA.

**Code vs config:** `git reset --hard` still updates the compose file; compose changes are rolled back with `git checkout <sha> -- docker-compose.yml`.

### 7.3 How database changes affect rollback
MongoDB has no schema, but **Beanie creates declared indexes at every startup** (§2.2), and that creates hazards specific to this project:
| Change type | Rule | Rollback effect |
|---|---|---|
| New optional field | Always give it a default; new code must tolerate it being missing, and old code ignores it (Pydantic ignores unknown fields when loading from the database, CONFIRMED-VERIFIED via `BaseDocument` usage) | Safe: roll back the code, the field remains and is harmless |
| Rename or remove a field | **Expand/contract:** release N writes both (or reads the fallback), release N+1 stops reading the old one, release N+2 removes it | Safe if the steps are separate releases |
| New non-unique index | Declared in the model and created at startup (brief build) | Old code ignores it. Beanie never drops indexes, so removing one requires a manual `dropIndex` |
| **New unique or partial-unique index** | **Pre-check the data for duplicates with a read-only script. Create the index out of band (ops runbook) *before* deploying code that declares it.** If the build fails at startup, `init_beanie` raises and **the app will not boot** | Rolling back the code leaves the constraint enforced; old code hitting it gets a 409 (safer than duplicates). Documented in the runbook |
| TTL index (expired tokens) | Deletes expired documents continuously | **Irreversible data deletion**, limited to expired tokens. Acceptable, but called out |
| Data backfill | Idempotent script under `app/jobs/`, run manually after a `mongodump` of the affected collections, and never auto-run at startup | Roll back by restoring the dumped collection, or leave the extra fields |
| Token model (§4) | Legacy defaults (§4.3.6) | Rolling back to old code: new token docs are plain `RefreshToken`s with extra fields, which old code accepts, so **no forced logout either way** |

**Transactions:** the URI uses `directConnection=true`. If production is a standalone server (not a replica set), **multi-document transactions are unavailable**. Every design here therefore uses single-document atomic operations plus unique indexes and never needs transactions (Q9: confirm the topology).

---

## 8. Coordinated reliability design
**Goal:** every live-session outcome is decided by exactly one atomic write. Every side effect (report generation) is recorded durably by that same write. Every actor (agent, admin, reaper, worker) can retry safely.

### 8.1 Building blocks
1. **Repository primitives** (`base_repository.py`), used everywhere instead of whole-document `save()` for existing documents:
   - `transition(id, from_states, to_state, set=…, push=…) → doc | None`, implemented as `find_one_and_update({_id, state ∈ from_states}, …, return AFTER)`. `None` means "someone else already moved it"; the caller re-reads and answers idempotently.
   - `update_fields(id, **fields)` (`$set`) and conditional variants. These generalise the existing `topic_content_repository.update_fields` and the verification service's conditional updates, which already work.
2. **Session state machine (revised):**
```
CONNECTING ──mark-connected──► CONNECTED ──mark-ended / force-end / reaper(timeout)──► ENDED
    │  └────────── mark-ended(early) / force-end ─────────────────────────────────────► ENDED
    └─ reaper(no connect in 2 min) / agent failure ──► CONNECTION_FAILED ◄── agent failure ─┘
Terminal: ENDED, CONNECTION_FAILED. (PAUSED/DISCONNECTED remain valid enum values for old
documents but have no transitions; unused edges ENDED/CONNECTION_FAILED→NOT_STARTED removed.)
```
   Every transition into a terminal state also sets `ended_at` in the **same** update. That fixes the `force_end` gap in analytics and progress.
3. **New `live_sessions` fields (all optional with defaults):**
   - `connected_at`
   - `transcript_count` (int, default 0)
   - `report_status` (`none`, `pending`, `running`, `done`, `failed`, `skipped`; a missing value is read as `none`)
   - `report_attempts`, `report_eligible_at`, `report_lease_until`, `report_lease_gen` (int, default 0: the fencing token), `report_lease_owner`, `report_generated_by_gen`, `report_error`
   - `end_reason` gains `infra_shutdown`, `never_connected` and `timeout_reaped`.

### 8.2 Transcript checkpointing (agent → backend)
- **New internal endpoint `POST /sessions/{id}/transcript`**, body `{from_seq, turns[]}`, protected by `require_internal_agent`. The agent sends every 5 turns or 10 s, and once more at the end.
- **Backend update:** `update_one({_id, transcript_count: from_seq, $or: [{state: {$in: [CONNECTING, CONNECTED]}}, {ended_at: {$gte: now − 90s}}]}, {$push: {transcript_history: {$each: turns}}, $inc: {transcript_count: len(turns)}})`.
- **If nothing matched**, the backend re-reads and answers:
  - `transcript_count >= from_seq + len` → **200 duplicate**; the agent advances (idempotent retry).
  - `transcript_count > from_seq` (partial overlap) → **409 {transcript_count}**; the agent resends from that point.
  - `transcript_count < from_seq` (gap) → **409 {transcript_count}**; the agent resends the missing turns.
  - The session closed more than 90 s ago → **410**; the agent stops.
- **The 90 s post-end window** lets final turns land even when an admin force-ends or the reaper fires first.
- **`mark-ended {final_seq, reason}`** keeps accepting the legacy full `transcript_history` for backward compatibility. It is used only if `transcript_count == 0`, which lets the backend deploy before the agent.

### 8.3 Report queue (replaces BackgroundTasks for correctness)
- **Enqueue happens atomically with the terminal transition.** The winning `transition(...→ENDED)` also sets `report_status: pending` and `report_eligible_at`:
  - `now` if `transcript_count >= final_seq`;
  - `ended_at + 90s` for force-ended or reaped sessions, which waits for late checkpoints.
  - Only the winner's update matches the state filter, so a session is **enqueued exactly once**.
- **The worker claims a job atomically and receives a fencing token:** `find_one_and_update({$or: [{report_status: {$in: [pending, failed]}, report_attempts: {$lt: 3}, report_eligible_at: {$lte: now}}, {report_status: running, report_lease_until: {$lt: now}}]}, {$set: {report_status: running, report_lease_until: now + 5min, report_lease_owner: <worker_id>}, $inc: {report_attempts: 1, report_lease_gen: 1}}, return AFTER)`. The worker keeps the returned `report_lease_gen = G` as its fencing token.
- **Lease-generation fencing (revised per review).** Every write the worker makes carries `G` in its filter:
  - **Heartbeat** (every 60 s during long Gemini calls): `update_one({_id, report_lease_gen: G, report_status: running}, {$set: {report_lease_until: now + 5min}})`. If it matches 0, the worker has been fenced out: it abandons the job and never writes.
  - **Success:** `update_one({_id, report_lease_gen: G, report_status: running}, {$set: {report: …, report_status: done, report_generated_by_gen: G}})`.
  - **Failure:** the same filter with `$set: {report_status: failed, report_error, report_eligible_at: now + backoff}` (1, 5, 30 min).
  - **Evidence gate failed:** the same filter with `report_status: skipped` (a short session is not a failure).
  - **If any fenced write matches 0 documents**, a newer claim (a higher `G`) or an admin action has superseded this worker. It logs `fenced_out(session, G)` and discards its result. **An expired worker therefore cannot overwrite a newer worker's result or state.** Checking status alone was not enough: after a lease expires and a new worker claims the job, the status is still `running`, and the old worker's write would have matched.
- **Admin regenerate** (`POST /sessions/{id}/regenerate-report`): `update_one({_id, report_status: {$ne: running}}, {$set: {report_status: pending, report_attempts: 0, report_eligible_at: now}, $inc: {report_lease_gen: 1}})`. If a lease is `running`, the request gets 409. An optional `force=true` (admin only) also accepts `running` and bumps the generation, which fences out the running worker immediately.
- **Idempotency:** writes are single-writer by construction (fencing). The only duplication possible is a second Gemini *call* after a lease expires, which costs money but can never produce a second or out-of-order write. `report_generated_by_gen` records which claim produced the stored report, for auditability.
- **The same fencing pattern applies to the transcript pipeline:** the `run_id` from B14 is its fencing token, and a claim sets a new `run_id`. It also applies to any future job that runs on this queue.
- **Historical sessions are not touched.** A missing `report_status` is read as `none`, so the worker never regenerates the whole history.
- The API may still "kick" the worker immediately, but **correctness never depends on in-process `BackgroundTasks`**. The same queue pattern is applied later to transcript generation and drafting (with a `run_id` for the pipeline; see B14).

### 8.4 Worker and reaper
- **New compose service `worker`:** same image, `python -m app.jobs.worker`, one replica. Its loop, every 30–60 s:
  - (a) claims report jobs;
  - (b) runs the reaper;
  - (c) runs `cleanup_expired_recordings` hourly, which also fixes its missing scheduler.
- **Reaper rules**, using the same `transition` primitive:
  - `CONNECTING` and `started_at < now − 2 min` → `CONNECTION_FAILED` (`never_connected`).
  - `CONNECTED` and `connected_at + max_duration + 5 min < now` → `ENDED` (`timeout_reaped`), with a report enqueued if `transcript_count > 0`.
- **Worker multiplicity:** a single worker is required unless jobs are claimed atomically (they are), so a second worker is safe as well.

### 8.5 force_end (admin)
1. `transition(active → ENDED, end_reason=ADMIN_TERMINATED, ended_at=now, report enqueue)`. If it returns `None`, respond 422 "already ended", as today.
2. Write the audit log entry.
3. Delete the LiveKit room on a best-effort basis: errors are logged, not raised. LiveKit also closes empty rooms by itself.

It no longer blocks on LiveKit and no longer overwrites the agent's writes.

### 8.6 Agent changes
- One HTTP helper with exponential backoff and jitter (0.5 → 8 s):
  - total cap of about 20 s for `mark-connected` and about 60 s for checkpoints and `mark-ended`;
  - retries only on connection errors, 5xx and 429; handles 409 and 410 as in §8.2.
- Validates required secrets at startup (fail fast).
- `JOB_SHUTDOWN` → `infra_shutdown`. Whether those sessions are scored normally is decision D7.
- Keeps task references; guards metadata parsing.
- **Crash-loss bound:** at most about 10 s of turns (the last checkpoint interval), down from the whole session today.

### 8.7 Interaction and idempotency matrix
| Race | Resolution |
|---|---|
| Agent `mark-ended` ×2 (finalizer and fallback) | The first `transition` wins; the second sees `None` and returns 200 `{transitioned:false}`. One report is enqueued. |
| `force_end` vs agent `mark-ended` | Whichever transition lands first sets the reason. The loser is a no-op. Late checkpoints within 90 s are still appended, and the report waits until `ended_at + 90s`. |
| Reaper vs a late `mark-connected` | The reaper wins (CONNECTION_FAILED); a late `mark-connected` gets 409 and the agent closes the room. Or `mark-connected` wins and the reaper's filter no longer matches. |
| Checkpoint retry after a lost response | The `from_seq` precondition turns a duplicate into a 200 no-op. |
| Worker crash mid-report | The lease expires and another claim retries (attempt counted, `report_lease_gen` incremented). |
| Worker A's lease expires but A is still running; worker B claims | B holds generation G+1, so A's heartbeat and final write (filter `report_lease_gen: G`) match nothing. A is fenced out and discards its result; only B can write. |
| Admin regenerate during `running` | Refused (409); or with `force=true`, the generation is bumped and the running worker is fenced out at its next write. |
| Password reset vs concurrent `PATCH /me` | Both are field-level `$set`s, so neither restores the other's stale fields (fixes B10). |
| Password reset vs in-flight refresh | `token_version` is incremented, so the successor is dead on arrival (§4.3.5). |
| Two template activations | Partial unique index `{owner_admin_id, level, kind: "standard", is_active: true, is_deleted: false}`; the loser gets `DuplicateKeyError` → 409 "retry". |
| Stale transcript run vs reclaimed run | The completion filter includes `run_id`, so the stale run's write matches nothing. |

**Deployment order for this design:**
1. Backend: new fields, primitives, the checkpoint endpoint, tolerant `mark-ended`, report status.
2. The worker service.
3. The agent: checkpoints and retries.
4. Optional removal of the report BackgroundTask kick.

Each step is backward compatible with its predecessor.

---

## 9. Revised roadmap
**Format per step:** Files · Tests · Deploy dependencies · DB impact · Breaking changes · Rollback · Acceptance.
**Evidence rule:** each fix ships with a regression test that fails before the fix, named after its finding ID.

### Track H: Hotfixes (start immediately after approval; H0 runs in parallel)

**H0. Minimal CI gate (both repos)**
- **Files:** new `.github/workflows/ci.yml` (per repo); `deploy.yml` (`needs: test`, `concurrency`, **remove `npm install --force`**); backend `pytest.ini` (`testpaths = tests`).
- **Tests:** backend `pytest tests`, currently 534 passing (E4); frontend `eslint` (0 problems today, E7). `tsc` is added after H7.
- **Deploy dependencies:** repo admin for branch protection (Q8).
- **DB impact:** none.
- **Breaking changes:** none; the deploy only becomes conditional.
- **Rollback:** revert the workflow files.
- **Acceptance:** a PR with a failing test cannot merge, and a push with a failing test does not deploy.

**H1. F1: per-token refresh coalescing (frontend)**
- **Files:** `app/api/backend/[...path]/route.ts` (≈10 lines); new `tests/bff-refresh.test.mjs`, the §3.1 harness on `node:test` using the esbuild already installed, so there is **no new dependency**.
- **Tests:** the two-user scenario (must fail on the old code, pass on the new), the same-user multi-request scenario (1 backend refresh), and the refresh-failure scenario (cookies cleared).
- **Deploy dependencies:** none.
- **DB impact:** none.
- **Breaking changes:** none.
- **Rollback:** revert one file, redeploy the previous SHA image.
- **Acceptance:** the harness shows each user keeps their own cookies, and the backend receives one refresh per distinct token.

**H2. Upload path: verify first, then fix**
- **Step 1 (ops, read-only):** on the VPS, run `nginx -T | grep -n client_max_body_size` for both sites, and read `MAX_REQUEST_BODY_BYTES` from the production `.env`. Then upload a 10 MB and a 60 MB test video as a *test* student on production or staging.
- **Step 2, only if confirmed:**
  - **Files:** frontend `nginx/system-site.conf.example` (`client_max_body_size 100m`, `proxy_read_timeout 300s`), the live nginx site (ops), and `.env.example` (`MAX_REQUEST_BODY_BYTES=94371840`, with a corrected comment).
  - **Tests:** a manual upload of 60 MB, plus the existing `test_body_size_limit`.
- **DB impact:** none.
- **Breaking changes:** none.
- **Rollback:** restore the previous nginx file and reload (`nginx -t && systemctl reload nginx`).
- **Acceptance:** an 80 MB video uploads end to end; a 100 MB+ body gets a clean 413.

**H3. F8: ProgramBuilder stops overwriting edits (frontend)**
- **Files:** `components/admin/topics/builder/ProgramBuilder.tsx`; `lib/hooks/useTopics.ts` (`refetch` returns a promise).
- **Change:**
  - Seed the form once per topic id.
  - When a poll returns, merge only **server-owned** fields (`transcript_state`, `transcript_source`, `raw_transcript` before approval, draft outputs into fields that are *still empty and untouched*).
  - Track a per-field "dirty" flag.
- **Tests:** introduce Vitest + Testing Library, a first frontend unit test dependency (**dependency addition: needs approval**). Alternatively keep node:test for pure logic, extract the merge rule into `lib/topics/mergeServerTopic.ts`, and unit-test it without new dependencies. **Recommended: the pure-function extraction plus node:test**, with Vitest deferred to S7.
- **DB impact:** none.
- **Breaking changes:** none.
- **Rollback:** revert.
- **Acceptance:** typing in Step 3 while a draft poll or thumbnail upload completes keeps the typed text; server drafts still fill empty fields.

**H4. Small backend correctness fixes (no schema change)**
- **Files:** `app/api/v1/submissions.py` + `recording_submission_service.py` (`day ∈ CHECK_IN_DAYS` and not in the future, B15); `transcript_review_service.py` + `topic_content_service.py` (map errors to `AppException`/502 with fixed messages, B17); `auth.py` (reset email sent via BackgroundTasks, B5); `security.py` (encode the header to bytes before `compare_digest`, B27).
- **Tests:** new API tests per item.
- **DB impact:** none.
- **Breaking changes:** `day` values outside the check-in days now get a 422. The frontend only sends valid days (`challenge/page.tsx:25-27`), so there is no client impact.
- **Rollback:** revert.
- **Acceptance:** tests green; no 500 for review or LiveKit failures.

**H5. force_end minimal fix (backend)**
- **Files:** `live_session_service.py`.
- **Change:** set state, `ended_at` and the reason first (one conditional update); make `delete_room` best-effort.
- **Tests:** `force_end` with LiveKit raising → 200 and the session ENDED with `ended_at`; the session appears in analytics.
- **DB impact:** none; `ended_at` is an existing field.
- **Breaking changes:** none.
- **Rollback:** revert.
- **Acceptance:** tests green. (Superseded later by S4's primitive, and consistent with it.)

**H6. Frontend leaks and gates**
- **Files:** `Recorder.tsx`, `EndOfWeekTaskModal.tsx` (stop the stream in catch, revoke blob URLs, no backdrop close while recording, F10); `student/challenge/page.tsx`, `student/progress/page.tsx` (`enabled` role gates, F13).
- **Tests:** node:test on extracted helpers; manual browser check.
- **DB impact:** none.
- **Breaking changes:** none.
- **Rollback:** revert.
- **Acceptance:** the camera light turns off on failure or unmount; admins in the student view see no 403 panels.

**H7. Remove the dead Cloudflare/D1 scaffold that breaks typecheck (frontend)**
- **Files:** delete `db/`, `drizzle/`, `drizzle.config.ts`, `examples/`; remove `drizzle-orm`, `drizzle-kit`, `wrangler` and `@cloudflare/vite-plugin` from `package.json` (**dependency removal: needs approval**); drop the `examples/**` include from `tsconfig`.
- **Tests:** `tsc --noEmit` goes from 1 error to 0; `vinext build` succeeds; the app smoke-tests locally.
- **DB impact:** none; D1 was never used (CONFIRMED-AGENT).
- **Breaking changes:** none at runtime.
- **Rollback:** revert.
- **Acceptance:** `tsc` is clean, and the CI frontend job adds `tsc`.

### Track S: Structural (in order)

**S0. Delivery safety: rollbackable deploys (both repos)**
- **Files:** `deploy.yml` (SHA tags, `.deploy.previous`, health gate, auto-revert, prune to 5); `docker-compose.yml` (`image: …:${IMAGE_TAG}`); `docs/RUNBOOK.md` (rollback, index pre-creation, mongodump).
- **Tests:** a dry-run deploy to Tamakkan or staging, and a deliberate failed-health deploy that proves auto-revert.
- **Deploy dependencies:** H0.
- **DB impact:** none.
- **Breaking changes:** none.
- **Rollback:** revert the workflow and compose.
- **Acceptance:** a documented one-command rollback, verified once.

**S1. Real-Mongo test job**
- **Files:** `ci.yml` (a `mongo:7` service, marker `@pytest.mark.realdb`); `tests/realdb/conftest.py`.
- **Tests:** port the §3.2 reproduction (it must fail before S2); add concurrency tests for email verification (which currently prove nothing on mongomock).
- **DB impact:** CI only.
- **Rollback:** remove the job.
- **Acceptance:** the realdb job runs on every PR.

**S2. Refresh-token redesign (§4) plus the frontend pairing**
- **Files:** `models/refresh_token.py` (+`family_id`, `rotated_at`, `revoke_reason`, `token_version`); `models/user.py` (+`token_version`); `refresh_token_repository.py` (atomic rotate, grace path, family revoke, `update_many`); `auth_service.py`; `user_service.py`, `password_reset_request_service.py` and `auth_service` (`$inc token_version` together with password changes); `core/config.py` (+`REFRESH_ROTATION_KEY`, `REFRESH_GRACE_SECONDS`); `.env.example`.
- **Tests (realdb):** 2 concurrent refreshes → 1 live token and identical successors; replay within grace returns the same successor; replay after grace → family revoked plus a 401; a password reset during an in-flight refresh → the successor is rejected; logout is never graced; legacy tokens refresh normally.
- **Deploy dependencies:** S1; `REFRESH_ROTATION_KEY` set in every environment **before** deploy (startup validation refuses to boot without it outside development).
- **DB impact:** new optional fields; a new index on `family_id`; **no backfill** (legacy defaults).
- **Breaking changes:** none for clients.
- **Rollback:** redeploy the previous image; new-format tokens remain valid for the old code (§7.3); no forced logout.
- **Acceptance:** the realdb tests are green and the §3.2 reproduction no longer forks.

**S3. Account lifecycle and auth hardening**
- **Files:** `users.py`, `root.py`, `user_service.py` (deactivate and reactivate, owner-checked, `$inc token_version`, B3); `security.py` (`token_version` in the JWT, checked in `get_current_user`); `password_reset_repository.py` (invalidate sibling reset tokens, B4); `email_verification_service.py` (lockout fix, B7); a rate limiter on login, reset and verification (B6), with the frontend forwarding `X-Forwarded-For` in `route.ts` and the auth routes; frontend proxy hardening (F2 allowlist derived from the `client.ts` paths, segment validation, body cap, Origin check, timeouts; F4–F6).
- **Tests:** API tests per item; frontend node:test for proxy validation (the allowlist rejects `/auth/login`, `..` → 400, a large body → 413, cross-origin POST → 403).
- **Deploy dependencies:** S2 (`token_version`); the backend before the frontend (the frontend starts forwarding XFF, the backend trusts it only from the frontend's address).
- **DB impact:** none new beyond S2.
- **Breaking changes:** deactivated users are logged out within one request; the proxy now rejects unknown paths (mitigated because the allowlist is generated from `client.ts`); rate limits could affect classrooms behind one NAT (limits are per account and IP, with tuned thresholds and config flags).
- **Rollback:** revert each PR; limiter thresholds are configurable without a deploy.
- **Acceptance:** all tests green; a manual check that the admin UI and student flows work through the allowlisted proxy.

**S4. Reliability core (§8)**
- **Files:** `base_repository.py` (primitives); `models/live_session.py` (state table, new fields, end reasons); `live_session_repository.py`; `live_session_service.py` (all transitions, checkpoint, tolerant `mark-ended`, regenerate); `api/v1/sessions.py` (+`/transcript`, +`/regenerate-report`); new `app/jobs/worker.py`; `docker-compose.yml` (+`worker`); `user_repository.py` (field-level updates, B10); `calendar_template_*` (activation, partial unique index, delete guard, B13); transcript pipeline `run_id` (B14); topic-delete guard (B9).
- **Tests (realdb):** every row of §8.7; reaper; lease expiry; historical sessions are not enqueued.
- **Deploy dependencies:** S1; **read-only pre-check script** for duplicate active templates, then ops creates the partial unique index before deploy (§7.3); then the worker service.
- **DB impact:** new optional fields on `live_sessions`, `topic_contents` (`run_id`), `users`; new indexes `{report_status, report_eligible_at}`, `{state, started_at}`, the partial unique template index; no backfill.
- **Breaking changes:** `mark-connected` after a reap returns 409 (agent handles it in S5); a template delete while referenced returns 409 (UI already shows errors). Everything else is additive.
- **Rollback:** stop the worker; redeploy the previous image (it ignores the new fields; `BackgroundTasks` scoring still exists in the old code); the partial unique index stays (safe).
- **Acceptance:** §8.7 tests green; a staging session force-ended mid-call has `ended_at` and a report containing the final turns.

**S5. Agent reliability (§8.6)**
- **Files:** `livekit_agent/agent.py`, `config.py`; new `livekit_agent/requirements-dev.txt`; tests.
- **Tests:** unit tests with a fake backend (retry/backoff, 409/410 handling, checkpoint cadence, close-reason mapping, secret validation); the existing prompt tests.
- **Deploy dependencies:** the new agent calls `/transcript`, which only exists after S4, so **S4 must be live in the same environment first**. (The reverse holds: the S4 backend still accepts the old agent's full-transcript `mark-ended`.)
- **DB impact:** none directly.
- **Breaking changes:** none for students.
- **Rollback:** redeploy the previous agent version on LiveKit Cloud (the backend still accepts full-transcript `mark-ended`).
- **Acceptance:** killing the backend for 30 s mid-session loses no turns; killing the worker mid-session loses at most 10 s of turns.

**S6. B1: remove the avatar prompt from the token (cross-component)**
- **Files:** backend `sessions.py` (+`GET /sessions/{id}/agent-context`, internal key), `live_session_service.py`; agent `agent.py` (fetches the context, falling back to metadata).
- **Sequence:** backend deploy 1 (endpoint) → agent deploy (reads the endpoint, falls back to metadata) → backend deploy 2 (drops `topic_instructions` from the metadata).
- **Tests:** a unit test that decodes the minted token and asserts no `topic_instructions` or `opening_line`; an agent test with a fake context endpoint.
- **DB impact:** none.
- **Breaking changes:** none if the sequence is followed.
- **Rollback:** each deploy is revertible independently, in reverse order.
- **Acceptance:** a decoded student token contains only `session_id`, `topic_id`, `max_duration_minutes` and `level`.

**S7. Performance (B19, B20)**
- **Files:** `live_session_repository.py` (projection excluding `transcript_history`), `progress_service.py` (memoise template resolution per request), `topic_content_service.py`, list routes (optional `limit/skip` with defaults that keep today's results), TTL indexes on token collections.
- **Tests:** existing suite; realdb query-count assertions.
- **DB impact:** TTL indexes (**irreversible deletion of expired tokens only**).
- **Breaking changes:** none (defaults unchanged).
- **Rollback:** revert; drop the TTL index manually if needed.
- **Acceptance:** entitlement and topic-list database reads stay constant as session history grows.

**S8. Frontend robustness and data layer**
- **Files:** a shared request helper with AbortController and a global 401 → logout event (`client.ts`, `AuthContext.tsx`), error boundaries (`app/**/error.tsx`), `Dialog.tsx` portal (F9), SpatialReal teardown on error and `startAudio` (F11), error states (F16/F17); introduce Vitest + Playwright (**dependency addition**).
- **Tests:** Vitest for hooks; Playwright smoke (login → admin → student → lesson; session start with LiveKit mocked; upload).
- **DB impact:** none.
- **Breaking changes:** none visible.
- **Rollback:** revert.
- **Acceptance:** the smoke suite is green in CI; an expired session redirects to login.

**S9. Infrastructure**
- **Files:** compose networking (a shared network instead of `host-gateway`, I1), TLS (I2), `/health/ready`, log rotation, Swagger off in production, image build moved to GHCR (§7.2).
- **Deploy dependencies:** verify I1/I2 in production first (ops).
- **DB impact:** none.
- **Rollback:** previous compose and nginx files.
- **Acceptance:** the frontend reaches the backend via the service name; HTTPS login works; readiness fails when Mongo is down.

**S10. Consistency and cleanup** (after D2/D3 decisions)
- Timezone handling (F18/B26, needs D3 and a data interpretation decision); OpenAPI → TypeScript types plus a CI diff check; backend-computed challenge and trend fields replacing the frontend duplicates (F14); i18n/RTL/dates (F19); dead code and models (B25, F21); duplicated AI-skill folders and binary docs moved out; stale docs and `.env.example`.
- **DB impact:** any timezone data correction is an explicit, dumped, idempotent script, approved separately.

**Dependency summary:**
- H0 comes first. H1–H7 run in parallel after H0 (H1 may ship before H0 with a manually recorded test run if you prefer).
- The structural chain is S0 → S1 → S2 → S3 and S1 → S4 → S5 → S6. S7–S9 follow S4. S10 follows the decisions.

---

## 10. Decisions and open questions
**Product and ops decisions (block only the steps named):**
- **D2 (S4/B9):** topic ownership. Shared across instructors, or scoped to the author?
- **D3 (S10):** Asia/Amman vs UTC day boundaries. Were stored class times entered as local time?
- **D4:** must "watch the video first" be enforced on the server?
- **D5 (S2):** grace window length (recommended 15 s); absolute family lifetime (today it slides indefinitely at 7 days per rotation)?
- **D6:** should Root see internal topic fields?
- **D7 (S5):** should infrastructure-interrupted sessions be scored?

**Open questions:**
- **Q1:** is production database `$DB_LEGACY` or `$DB_LLM`?
- **Q8:** who has admin rights to enable branch protection?
- **Q9:** is production Mongo a standalone server or a replica set?
- **I1/I2/U1/U2:** the production nginx, `.env` and TLS facts listed in §5.
- **Q10:** does production run more than one frontend instance? (Not required by the §4 design, but it informs H1's urgency assessment.)

**Environment clean-up awaiting your instruction:** stop my legacy-named backend on 4654 (connected to the shared database) and frontend on 5177, and decide whether to keep or remove the local Tamakkan Docker Mongo and the `.env` files I created. I will not change them without your go-ahead.

---

## Appendix A: v1 findings catalogue (condensed; evidence levels in §5)
- **Frontend:**
  - F1 refresh swap
  - F2 open proxy
  - F3 traversal
  - F4 unbounded buffering
  - F5 no CSRF or Origin check
  - F6 no timeouts, half-login
  - F7 no global 401
  - F8 builder overwrites edits
  - F9 nested dialog closes its parent
  - F10 Recorder leaks
  - F11 avatar teardown and autoplay
  - F12 SDK internals
  - F13 ungated student hooks
  - F14 business rules duplicated from the backend
  - F15 client-only watch gate
  - F16 fetch and UX gaps
  - F17 no error UI or boundaries
  - F18 dates in en-US/UTC, class input stored as UTC
  - F19 i18n/RTL/brand leaks
  - F20 unchecked casts, `ApiErrorBody.code` missing
  - F21 dead code and template leftovers
  - F22 accessibility
- **Backend:**
  - B1 prompt in token
  - B2 rotation race
  - B3 no deactivation
  - B4 reset-token reuse
  - B5 inline reset email
  - B6 no rate limits
  - B7 verification lockout
  - B8 topics without owner
  - B9 topic delete breaks calendars
  - B10 whole-document lost updates
  - B11 force_end
  - B12 stuck sessions
  - B13 template activation race
  - B14 stale transcript run
  - B15 `day` validation
  - B16 uncapped nested input
  - B17 unmapped 500s and leaked errors
  - B18 lost reports
  - B19 N+1 and full-transcript loads
  - B20 unbounded lists, no TTL
  - B21 stale `assigned_topic_ids`
  - B22 weak config validation
  - B23 layering drift
  - B24 duplication
  - B25 dead models and states
  - B26 UTC day boundary
  - B27 `compare_digest` TypeError, CLI password
- **Agent:**
  - A1 transcript in memory only
  - A2 no secret validation
  - A3 shutdown recorded as user-ended
  - A4 timer and room race
  - A5 defaults and hard-coding
- **Delivery and infrastructure:**
  - D1 deploy without gates or rollback
  - T1 bare pytest broken
  - T2 frontend typecheck broken by scaffold
  - T3 tests not in CI
  - Frontend `build`/`test` wired to "OpenAI Sites"
  - U1/U2 upload limits
  - I1 host-gateway
  - I2 TLS vs Secure cookies
  - No database readiness check, no log rotation, no metrics
  - Swagger in production
  - Recording cleanup has no scheduler
  - Repo bloat (AI-skill copies, binaries); the frontend `.claude/settings.json` runs a react-doctor hook in Claude Code sessions
- **What is well done and must be preserved** (unchanged from v1): backend layering and DI; password and verification handling; compare-and-set pipeline transitions; upload hardening; the BFF cookie design; typed en/ar i18n with parity; responsive layout; the broad API test suite (534 passing); non-root Docker images; Tamakkan isolation guards.


---

# Appendix B — Earlier infrastructure drafts (superseded by Parts A–E; kept for reference)

> Where these differ from Parts A–E above, **Parts A–E win**. In particular: Axis has since fixed its own workflows, and the VPS script has been replaced by the reviewed v2 (`tamakkan-vps-readonly-checks.sh` in this folder).


---

## Draft: tamakkan-rename-migration-plan.md

### Legacy name → Tamakkan: Inventory & Staged Migration Plan

> **Status:** plan only. Nothing has been renamed, pushed, merged, deployed, restarted or modified.
>
> **Evidence sources, all read-only:**
> - Both local repos: `git grep`, plus reads of the files themselves.
> - GitHub, via `gh api` as `yousefk-h` (repo metadata, workflow files, secret *names*, branch lists).
> - This PC's Docker metadata.
> - The git remotes of other local project folders.
>
> **Not seen:** the VPS itself (no access from here). Every VPS statement is inferred from checked-in deploy files and marked **[VPS: verify]**.

### 0. Findings that change the plan

1. **There are three separate product lines in the org, and only one of them is being renamed.**

   | GitHub repos | What it is | Deployed? | Local checkouts on this PC |
   |---|---|---|---|
   | `$BACKEND_GITHUB_REPO`, `$FRONTEND_GITHUB_REPO` | **This project**, the one to be renamed to Tamakkan. It already contains the "Tamakkan" profile and brand (merged from the legacy integration branch). | Yes. Actions → VPS `$TAMAKKAN_BACKEND_DIR`, `$TAMAKKAN_FRONTEND_DIR`; ports 4652/6356 | `Test Tamakkan\<legacy-named folders>` (with the H1 branch), and `Full Projects\<Sibling LLM folder>\<second Tamakkan clone>` (same commits, running on 4652/5173) |
   | `$LLM_BACKEND_REPO`, `$LLM_FRONTEND_REPO` | **"Sibling LLM", a genuinely separate production installation** with diverged code ("Deploy Sibling LLM backend to VPS") | Yes. VPS `$LLM_BACKEND_DIR` (port 3524), `$LLM_FRONTEND_DIR` (port 6868) | `Full Projects\<Sibling LLM folder>\<Sibling LLM repos>` |
   | `tamakan_backend`, `tamakan_frontend` (single "k") | **"Tamakan new design"**, a different codebase (React/Vite, multi-tenant, lesson-block player) | No workflows found | `Full Projects\Tamakan new design\tamakkan-*` (compose project `tamakkan-backend`, running locally) |
   | `axis_*` | Axis, out of scope | — | — |

   **Out of scope and not to be touched:** Sibling LLM, Tamakan-new-design and Axis, including their repos, directories, images, containers and env files.

2. **Latent production hazard: shared image tags.** Both this project and Sibling LLM build `image: $SHARED_BACKEND_TAG` and `$SHARED_FRONTEND_TAG` (compare the two `docker-compose.yml`s).
   - If both run on the same VPS **[VPS: verify, same `VPS_HOST` value?]**, each deploy re-points the tag the other project uses.
   - Running containers keep their image ID, so nothing breaks today.
   - But any `docker compose up -d` *without* `--build` (for example a manual restart after a host reboot or a config change) recreates the container from whatever `:latest` currently points to. That can be **the other product's code**.
   - Giving this project distinct image names is the one naming change with a real, non-cosmetic benefit.

3. **The production database is not yet established, and cannot be established from here.**
   - The app uses `MONGODB_DB_NAME`, not the database in the URI path (`app/core/database.py:63-66`). Its default is `$DB_LEGACY` (`app/core/config.py:25`).
   - **Sibling LLM's `.env.example` also defaults to `MONGODB_DB_NAME=$DB_LEGACY`.**
   - The earlier read-only listing of the shared Mongo server showed four relevant databases: `$DB_LEGACY` (28 users), `$DB_LLM` (24 users), `tamakan` (20 collections, 0 users), and the local test database `tamakkan_dev` (on the PC only).
   - **If both production apps kept the default, two different codebases are writing to the same database.** That would be a serious, pre-existing problem, separate from the rename. It must be checked first (Stage 1) and must **not** be "fixed" as part of a cosmetic rename.

4. **The Tamakkan brand is already implemented in the frontend.**
   - `lib/branding.ts`, `components/ui/Logo.tsx`, `app/globals.css` (`data-brand`), `LoginScreen.tsx:67` and `public/brand/tamakkan-logo.jpg` cover name, Arabic name, logo, wordmark, colours, title, description and login heading.
   - It is selected at **build time** by `NEXT_PUBLIC_APP_BRAND=tamakkan`, which is a Docker build arg (`Dockerfile:39`, legacy default).
   - **So the user-facing frontend rename is a configuration switch plus clean-up, not a rewrite.**

5. **Permissions:**
   - You have **push but not admin** on every repo.
   - Renaming repos, listing webhooks and deploy keys, and enabling branch protection need an **org owner or repo admin**.
   - There is **no branch protection**, and **every push to `main` deploys to production**. Any rename-related merge is therefore a production deploy unless deploys are gated first (Stage 2).

6. **GitHub secrets:** only `VPS_HOST`, `VPS_SSH_KEY` and `VPS_USER` exist, per repo, and none is named after the legacy brand. Secrets belong to the repo object and **survive a rename**.

---

### 1. Inventory of legacy-name references

### 1.1 GitHub
| Item | Current | Evidence |
|---|---|---|
| Repo names | `$BACKEND_GITHUB_REPO`, `$FRONTEND_GITHUB_REPO` (private, default branch `main`) | `gh api repos/...` |
| Workflow names | "Deploy <legacy brand> backend to VPS", "Deploy <legacy brand> Frontend to VPS" | `.github/workflows/deploy.yml:1` (both) |
| Workflow VPS paths | `cd $TAMAKKAN_BACKEND_DIR`; `cd $TAMAKKAN_FRONTEND_DIR` | `deploy.yml:18` / `:36` |
| Branch | the legacy integration branch (already merged into `main` in both) | `git branch -a` |
| Other branches | backend: 5 `feature/*`; frontend: 3 `feature/*` (one named `…-and-rebrand`) | — |
| Secrets | `VPS_HOST`, `VPS_SSH_KEY`, `VPS_USER` (no legacy naming) | `gh api …/actions/secrets` |
| Environments, branch protection | none | 404 |
| Webhooks, deploy keys | **unknown**: needs admin (the listing call returned 404 / missing scope) | **[admin: verify]** |
| Hard-coded repo URL in code | `app/core/application.py:28` (README link in the Swagger description) | grep |
| Name availability | `tamakkan_backend` / `tamakkan_frontend` return 404 to your account. This means "free", **or** "private and not visible to you". **[admin: verify]** | `gh api` |
| Near-collision | `tamakan_backend` / `tamakan_frontend` exist; they differ from the proposed names by one letter | org repo list |

### 1.2 Local repositories (tracked files; AI-tooling copies counted separately)

**Backend:** 40 files. Code and config hits are listed below; ~13 more files are AI-tooling copies.

**Frontend:** 28 files, plus 7 AI-tooling files. False positives (the word "confidence") are excluded from the classification.

#### Category 1: user-facing names and branding
| Where | What | Plan |
|---|---|---|
| FE `lib/branding.ts:13-19`, `components/ui/Logo.tsx:1,22-34`, `app/globals.css:4,91`, `lib/i18n/messages/en.ts:160`, `ar.ts:157` (`auth.login.title`: legacy brand name), `Dockerfile:39` (`ARG NEXT_PUBLIC_APP_BRAND` with the legacy default), `.env.example:17` | The legacy brand is the **default** | Stage 5a: flip production to `tamakkan`. Stage 6: make Tamakkan the only or default brand and remove the legacy-brand branch of `branding.ts`/`Logo.tsx` |
| BE `app/services/auth_service.py:99` | Email subject naming the legacy brand is **hard-coded** | Use `settings.EMAIL_BRAND_NAME` |
| BE `app/core/config.py:20,39`; `.env.example:1,17` | `PROJECT_NAME`, `EMAIL_BRAND_NAME` with legacy-brand values (defaults, overridable by env) | Set production env to Tamakkan (Stage 5a); change the defaults (Stage 5b) |
| BE `app/core/application.py:23,28` | Swagger description naming the legacy brand, and the repo link | Text change; link updated after the repo rename |
| BE `livekit_agent/prompts.py:43` | **The tutor's system prompt names the legacy brand**: Mariam may say it to students | Change to Tamakkan. **This is an agent deploy on LiveKit Cloud** (Stage 5b) |
| BE `app/services/recording_submission_service.py:45,65`, `topic_content_service.py:30` | Cloudinary folders `$LEGACY_CLOUDINARY_FOLDER/…` and `$LEGACY_CLOUDINARY_FOLDER/topic-thumbnails`, visible in media URLs | **Decision D4.** New uploads can go to `Tamakkan/…`; **existing assets keep their stored `cloudinary_public_id` and must not be moved**. Low benefit; two folders coexist |
| Frontend page titles, metadata, email templates | Driven by `brand.*` / `EMAIL_BRAND_NAME` (verification email) | Covered by the rows above |

#### Category 2: source-code identifiers and configuration
| Identifier | Where | Rename? | Reasoning |
|---|---|---|---|
| `MONGODB_DB_NAME` **value** `$DB_LEGACY` (and whatever production actually uses) | `config.py:25`, `.env.example:6` | **No, never as part of a rename** | Renaming a database is a data migration: every document is copied and connections are switched, for **zero user-visible benefit**. **Changing the default is dangerous:** if a production `.env` ever lacked the variable, the app would silently connect to a fresh, empty `tamakkan` database (Beanie creates it) and appear "healthy" with no users. **Recommendation:** remove the default so the app refuses to start without an explicit value (fail fast). A `tamakan` database also already exists on the shared server, which is another reason not to reuse that name. |
| `AUTH_COOKIE_PREFIX` legacy default → cookies `<legacy prefix>_access_token` / `_refresh_token` | FE `lib/api/server/backend.ts:16` | **Decision D3** | Changing it in production **signs every user out once**, because the old cookies are no longer read. The name is not visible to users. Benefit: consistency only. If changed, do it in the same release as a planned re-login notice, or support reading the old name for one refresh cycle. |
| `TAMAKKAN_ENV_FILE` | `config.py:173`, `livekit_agent/agent.py:54`, `livekit_agent/config.py:84`, `scripts/tamakkan_*.py`, tests | Optional | Internal only. If renamed: read `TAMAKKAN_ENV_FILE` first and fall back to `TAMAKKAN_ENV_FILE` for one release, so existing shells, scripts and the agent keep working. |
| Legacy brand id | `lib/branding.ts:14` | Removed in Stage 6 with the legacy brand | — |
| `LEARNING_ANALYTICS_PROFILE` (legacy value) | `.env.example:39-40`, `scripts/tamakkan_local.py` | **Delete** | A dead setting: no code reads it (removed in commit `9b081dd`) |
| Test DB (legacy name), test fixtures | `tests/conftest.py:38`, `test_environment_profiles.py`, `test_recording_submission_naming.py`, `test_submissions.py:164` | Only together with the code they assert (Cloudinary folder, profiles) | Tests follow behaviour; a blind replace would break them |
| Docstrings and comments naming `$BACKEND_GITHUB_REPO/…` paths | `app/__init__.py:1`, `live_session_service.py:5`, `submissions.py:9`, FE `lib/api/client.ts:2,13`, `types.ts:3`, `format.ts:4`, `next.config.ts:19`, `backend.ts:15`, … | Yes, after the repo rename | Cosmetic, no runtime effect |
| Example root email `root@tamakkan.example` | `app/jobs/seed_root.py:9` | Yes (docs example) | — |
| Package name `zero-to-hero-english` | FE `package.json:2` | Optional → `tamakkan-frontend` | Leftover template name; no runtime effect |

#### Category 3: deployment and infrastructure
| Item | Current | Target | Risk |
|---|---|---|---|
| Workflow display names | "Deploy <legacy brand> …" | "Deploy Tamakkan …" | None |
| Workflow paths | `$TAMAKKAN_BACKEND_DIR`, `$TAMAKKAN_FRONTEND_DIR` | `/root/tamakkan_backend`, `/root/frontendwebsite/tamakkan_frontend` | **The deploy breaks if the path and the directory change out of order** (Stage 4) |
| Image tags | `$SHARED_BACKEND_TAG`, `$SHARED_FRONTEND_TAG` (**shared with Sibling LLM**) | `tamakkan_backend:<git-sha>` / `tamakkan_frontend:<git-sha>` (later `ghcr.io/kassel-for-information-technology/tamakkan_*:<sha>`) | Avoid the unqualified `tamakkan-backend` tag: the Tamakan-new-design project already uses `tamakkan-backend:latest` (seen on this PC) |
| Compose project, container names | Implied from the directory: `$TAMAKKAN_BACKEND_PROJECT` → container `$TAMAKKAN_BACKEND_CONTAINER`; `$TAMAKKAN_FRONTEND_CONTAINER` **[VPS: verify]** | Pinned with `name: tamakkan_backend` / `tamakkan_frontend` in compose | **Changing the project name makes compose create new containers** (they don't adopt the old ones), which then fight over the port. Handled by blue/green (Stage 4) |
| Compose volumes and networks | **none declared** in either compose file (the database is external) | — | **No Docker volume holds this project's data** **[VPS: verify no extra override files]** |
| Host ports | 4652 (API), 6356 (web), bound to 127.0.0.1 | New stack on temporary 4662 / 6366 (**[VPS: verify free]**) | nginx upstream switch |
| nginx | Repo examples use placeholders (`api.tamakkan.example`, `tamakkan.example`) | Real site file names, `server_name`s and upstream ports **[VPS: verify]** | **If the real domain contains the legacy brand name**, see decision D5 |
| Environment files on the VPS | `$TAMAKKAN_BACKEND_DIR/.env`, `$TAMAKKAN_FRONTEND_DIR/.env` | Copied (`cp -p`, mode 600) into the new dirs **unchanged** except the brand variables in Stage 5a | Never re-typed; never edit DB values |
| Git remotes on the VPS | Clones of `…/$BACKEND_GITHUB_REPO.git` / `$FRONTEND_GITHUB_REPO.git` (HTTPS or SSH, **[VPS: verify]**) | New clones of `tamakkan_*`, or `git remote set-url` | GitHub redirects old URLs after a rename, **until someone creates a new repo with the old name** |
| Logs, backups, cron | **Unknown**: none are defined in the repos (no log volumes, no backup scripts, no cron) | Inventory in Stage 1 | **[VPS: verify]** `crontab -l`, `/etc/cron.d`, `/etc/logrotate.d`, any `mongodump` scripts |
| Dockerfile and compose comments; `nginx/*.example`; `.env.example` comments | the legacy folder and image names | Updated with Tamakkan names | None |
| Local launcher | `scripts/start-tamakkan.ps1:17` searches `tmp/tamakkan-mongodb` | Keep, and also search `tmp/tamakkan-mongodb` | Local only |
| LiveKit agent deploy | `livekit_agent/livekit.toml` (LiveKit Cloud project subdomain and agent id); agent name `mariam-agent` | **Unchanged** | Provider identifiers, not legacy-brand names |

#### Category 4: historical, preserve rather than rewrite
- **Git history:** commit messages, authors, SHAs. Never rewritten.
- **Branch the legacy integration branch:** already merged. Keep it or delete it later; don't rename it.
- **Planning documents:** `Tamakkan-Kanban-Board.md/.csv`, `Tamakkan-Platform-Plan.pdf`, `Tamakkan-Consolidated-Platform-Plan.md`, `Tamakkan-Task-Breakdown.md`, `LiveKit-Avatar-Platform-Requirements.md` and others. Move them to `docs/history/` with a one-line note ("written under the project's legacy name"). Don't edit their contents.
- **`TAMAKKAN_ENVIRONMENT.md`, `PROJECT_ACCESS.md` and `BRANDING.md`** describe the legacy/Tamakkan *profile separation*. They need a careful rewrite once the separation itself is retired (Stage 6), not a word swap.
- **Existing Cloudinary asset paths** under `$LEGACY_CLOUDINARY_FOLDER/…` are stored in the database as `cloudinary_public_id`. Never rewrite them.
- **External provider identifiers:** the GitHub org, the LiveKit Cloud project URL and subdomain, the agent name, the Cloudinary cloud name, SpatialReal app and avatar ids, the Mongo host and user names, and database names. **Unchanged.**

---

### 2. Name mapping
| Existing | Proposed | Changed in |
|---|---|---|
| GitHub `$BACKEND_GITHUB_REPO` | `tamakkan_backend` | Stage 3 (admin) |
| GitHub `$FRONTEND_GITHUB_REPO` | `tamakkan_frontend` | Stage 3 (admin) |
| VPS `$TAMAKKAN_BACKEND_DIR` | `/root/tamakkan_backend` (the old directory is archived, not deleted) | Stage 4 |
| VPS `$TAMAKKAN_FRONTEND_DIR` | `/root/frontendwebsite/tamakkan_frontend` | Stage 4 |
| Image `$SHARED_BACKEND_TAG` | `tamakkan_backend:<sha>` | Stage 4 |
| Image `$SHARED_FRONTEND_TAG` | `tamakkan_frontend:<sha>` | Stage 4 |
| Compose project (implied) `$TAMAKKAN_BACKEND_PROJECT` / `$TAMAKKAN_FRONTEND_PROJECT` | `tamakkan_backend` / `tamakkan_frontend` (pinned via `name:`) | Stage 4 |
| Workflow "Deploy <legacy brand> backend/Frontend to VPS" | "Deploy Tamakkan backend/frontend to VPS" | Stage 4 |
| `NEXT_PUBLIC_APP_BRAND` in production: legacy brand (default) | `tamakkan` | Stage 5a |
| `PROJECT_NAME`, `EMAIL_BRAND_NAME` in production | `Tamakkan Backend`, `Tamakkan` | Stage 5a |
| Hard-coded legacy-brand strings (email subject, Swagger text, tutor prompt) | "Tamakkan" | Stage 5b |
| `TAMAKKAN_ENV_FILE` | `TAMAKKAN_ENV_FILE`, with fallback (optional) | Stage 6 |
| `AUTH_COOKIE_PREFIX` (legacy prefix) | **D3** (keep, or `tamakkan` with a one-time re-login) | Stage 6 |
| Cloudinary folder for **new** uploads `$LEGACY_CLOUDINARY_FOLDER/` | **D4** (keep, or `Tamakkan/`) | Stage 6 |
| Database name | **unchanged** | never |
| Local folders `Test Tamakkan\<legacy-named folders>` | `tamakkan_*` (optional, local) | after Stage 3 |

---

### 3. Order of changes (staged; each stage needs separate approval)

**Guiding rules:**
1. Discover before changing.
2. Gate deploys before merging anything.
3. Rename the GitHub repos before VPS paths (GitHub redirects make the order safe).
4. VPS cutover is blue/green, with the old stack kept intact for rollback.
5. User-facing branding goes last, as configuration, so it can be flipped back instantly.
6. The database is never touched.

### Stage 0: local safeguards (local only; no access needed)
- Create an H1 backup bundle outside the repo: `git -C frontend bundle create ../h1-bff-refresh.bundle main..fix/h1-bff-refresh-coalescing`, then `git bundle verify ../h1-bff-refresh.bundle`. That's one new file; no repo change.
- Record the local state (branch SHAs `010d697`, `3a1f01d` on base `255b34b`).

### Stage 1: read-only discovery on GitHub and the VPS (**needs a GitHub admin and VPS SSH access**)

**GitHub admin:**
- Confirm `tamakkan_backend`/`tamakkan_frontend` are free.
- List webhooks, deploy keys, GitHub Apps and integrations on both repos.
- Confirm no other repo's workflow or `uses:` references them.

**VPS (read-only commands; values are redacted before they are shared):**
1. `ls -la /root /root/frontendwebsite`, then `git -C <each dir> remote -v`, `rev-parse HEAD`, `status --porcelain`.
2. `docker ps -a --format '{{.Names}} {{.Image}} {{.Label "com.docker.compose.project"}} {{.Label "com.docker.compose.project.working_dir"}}'`, plus `docker images`, `docker volume ls`, `docker network ls`, all filtered by the legacy and Tamakkan name prefixes.
3. **Which database production uses**, without printing the URI: `docker inspect <api container> --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E '^MONGODB_DB_NAME='` for **both** `$TAMAKKAN_BACKEND_CONTAINER` and Sibling LLM's container. Also compare the host part of each `MONGODB_URI` (`… | sed -E 's#^MONGODB_URI=mongodb(\+srv)?://[^@]*@([^/?]*).*#\2#'`).
4. `nginx -T 2>/dev/null | grep -nE 'server_name|proxy_pass|listen|client_max_body_size|ssl_certificate '`.
5. `crontab -l; ls /etc/cron.d /etc/logrotate.d`; any backup scripts or `mongodump` usage; `ss -ltnp | grep -E '4652|6356|4662|6366|3524|6868'`.
6. Whether `VPS_HOST` for both products is the same machine (compare `hostname` with what the admin sees in the secret, via the admin UI).

**Decision gate:** from the database result, choose one outcome.

| Outcome | Action |
|---|---|
| A. Tamakkan uses `$DB_LEGACY`, Sibling LLM uses `$DB_LLM` (or another host) | Proceed; the database names stay as they are |
| B. **Both use the same database** | **Stop the rename.** Escalate as a separate data-isolation incident; the rename waits |
| C. Tamakkan uses `$DB_LLM` | Proceed; the name stays as it is (the name is not user-visible) |

### Stage 2: gate deploys (**needs a GitHub admin for branch protection**; the workflow change is a normal PR)
- Merge a PR per repo that changes the deploy trigger from `on: push: main` to **`workflow_dispatch`** (manual), and adds a `concurrency:` group.
  - The commit containing the trigger change is evaluated with the new trigger, so **merging it does not deploy**.
  - From this point, merges (H1, the rename PRs) do not deploy until someone presses "Run workflow".
  - This is H0-lite. The full CI gate from Audit v2 can follow.
- Enable branch protection: require a PR, no direct pushes.

### Stage 3: rename the GitHub repositories (**needs a GitHub admin**)
- Settings → Rename: `$BACKEND_GITHUB_REPO` → `tamakkan_backend`, `$FRONTEND_GITHUB_REPO` → `tamakkan_frontend`.
- **What GitHub preserves:** issues, PRs, stars, Actions secrets, workflow history, branches, and redirects for web, API, `git clone/fetch/push` over HTTPS and SSH.
- **What breaks:**
  - The redirect breaks if a new repo is later created with the old name. **Never create `$BACKEND_GITHUB_REPO`/`$FRONTEND_GITHUB_REPO` again.**
  - Hard-coded links (`application.py:28`) keep working through the redirect until they are updated.
- **Local:** `git remote set-url origin https://github.com/kassel-for-information-technology/tamakkan_{backend,frontend}.git` in each clone. That's **both** local clone pairs: `Test Tamakkan\…` and `Full Projects\<Sibling LLM folder>\<second Tamakkan clone>`.
- **The VPS keeps working unchanged**, through the redirect.
- Verify: `git ls-remote origin main` returns the same SHAs (`d6815f4`, `255b34b`).

### Stage 4: VPS cutover to Tamakkan names (**needs VPS access**; blue/green; zero data)

**Backend first, then frontend.**
1. **Pin the rollback image:**
   - `docker tag $(docker inspect -f '{{.Image}}' $TAMAKKAN_BACKEND_CONTAINER) $LEGACY_BACKEND_IMAGE:pre-tamakkan-<date>`.
   - Otherwise a later Sibling LLM deploy would overwrite `:latest`, and the old stack could no longer be restarted as it was.
   - Do the same for the frontend.
2. **Code PR** (merged, but not auto-deployed thanks to Stage 2):
   - compose `name: tamakkan_backend`, `image: tamakkan_backend:${IMAGE_TAG:-latest}`;
   - workflow `cd /root/tamakkan_backend`, SHA tags, health gate;
   - updated comments.
3. **On the VPS:**
   - `git clone <tamakkan_backend url> /root/tamakkan_backend`.
   - `cp -p $TAMAKKAN_BACKEND_DIR/.env /root/tamakkan_backend/.env` (unchanged; verify `chmod 600`).
   - Create a VPS-only `docker-compose.override.yml` mapping `127.0.0.1:4662:4652`.
   - `docker compose up -d --build`.
4. **Verify the new stack alongside the old one:**
   - `/api/v1/health` on 4662.
   - A read-only authenticated smoke test (login as an internal test admin, `GET /me`, `GET /topics`).
   - Container logs show the same `MONGODB_DB_NAME` as the old container.
   - Two API instances against one database is safe here, because the app keeps no in-process state that matters across instances; only `BackgroundTasks` work that was in flight at the moment of the switch is lost, as on any restart.
5. **Switch nginx** to 4662: edit the upstream, `nginx -t && systemctl reload nginx` (a reload, zero downtime). Watch the logs and error rate for 15 minutes.
6. **Frontend:**
   - Same steps with `/root/frontendwebsite/tamakkan_frontend` on 6366.
   - Its `.env` `API_BASE_URL` points to the backend's **new** port.
   - Switch the nginx web upstream.
7. **Stop the old stacks, keeping them:** `docker compose -p "$TAMAKKAN_BACKEND_PROJECT" stop` (**not `down -v`**; there are no volumes anyway). Keep the old directories for 30 days (`mv` to `/root/_archive/…` only after that).
8. Remove the temporary override file only if you decide to return to the original ports. Otherwise document 4662/6366 as the final ports.
9. Update the LiveKit agent's `BACKEND_API_URL` **only if** it points at a VPS port rather than the public API domain **[VPS/LiveKit: verify]**.

### Stage 5: branding
- **5a, configuration only:** in the new VPS `.env` files set `NEXT_PUBLIC_APP_BRAND=tamakkan` (a **build arg**, so the frontend is rebuilt), `PROJECT_NAME=Tamakkan Backend` and `EMAIL_BRAND_NAME=Tamakkan`. Then do a manual deploy.
  - Verify: login page, admin UI, student UI, page titles, favicon, verification email and reset email (to an internal test inbox).
  - **Rollback: set the variable back and redeploy.**
- **5b, code PRs:** the hard-coded strings (email subject, Swagger text, the tutor prompt in `livekit_agent/prompts.py:43`, which is **a separate LiveKit Cloud agent deploy**), backend defaults, `.env.example`, docs, the repo link.

### Stage 6: clean-up (optional, lower priority)
- Remove the legacy brand code path.
- `TAMAKKAN_ENV_FILE`, with fallback.
- D3 cookie prefix; D4 Cloudinary folder for new uploads.
- Move historical documents into `docs/history/`.
- Rename the local folders.
- Retire the legacy/Tamakkan profile separation docs.
- Remove the dead `LEARNING_ANALYTICS_PROFILE`.

---

### 4. H1: preservation and integration
- **Where it is:** `Test Tamakkan\frontend`, local branch `fix/h1-bff-refresh-coalescing` (`010d697` test, `3a1f01d` fix, base `main@255b34b`). No upstream, never pushed.
- **Preserved by:**
  - (1) The Stage 0 `git bundle` outside the repo.
  - (2) A repo rename, which does not touch local branches.
  - (3) Never running `git reset`, `git clean` or a re-clone in this folder during the migration.
  - (4) Not pushing from the other clone (`Full Projects\<Sibling LLM folder>\<second Tamakkan frontend clone>`), which lacks H1.
- **Integration (each step on your approval):**
  1. After Stage 2 (deploys are manual), push the branch and open a PR against `main`. This works before or after the rename, because the remote URL redirects.
  2. If `main` has moved (for example a rename PR merged first), rebase H1 onto the new `main` and re-run `npm run test:bff`, ESLint and `tsc`. **Expected conflicts: none.** H1 touches only `route.ts` (no legacy-brand strings), the new test files and one `package.json` line. The test pins `AUTH_COOKIE_PREFIX` itself, so a later change to the default cookie prefix doesn't affect it.
  3. Review, merge, then deploy **deliberately**, through a manual workflow run, as its own release.
- **Recommended order:** ship H1 as the **first** release after Stage 2, before any rename release. It is a proven critical security fix, it is independent of naming, and keeping it separate means a rename rollback never reverts the security fix, and vice versa.
- **Duplication risk:** there is none if the H1 work is only ever pushed from `Test Tamakkan\frontend` and the bundle is kept purely as a backup, never imported into a second clone.

---

### 5. Zero-data-loss rules
1. **No database is renamed, copied, migrated or re-pointed.** The `MONGODB_*` values are copied byte-for-byte into the new directories. Before and after each cutover, compare the database name reported in the startup log of the old and new containers (`Connected to MongoDB database '<name>'`).
2. **No Docker volumes are involved:** this project's compose files declare none **[VPS: verify]**. Never run `docker compose down -v`, `docker volume prune` or `docker system prune` during the migration. **Sibling LLM, Tamakan-new-design and Axis resources are not touched, even when their names contain the legacy brand name or "tamak".**
3. Optional extra safety, not required because nothing touches data: a `mongodump` of the production database to a dated backup location before Stage 4. **This needs database host access** **[ops]**.
4. **Uploads (Cloudinary) and their stored `cloudinary_public_id`s are never moved.**
5. **Secrets:** the GitHub secrets survive the rename; the VPS `.env` files are copied with `cp -p`, never retyped, never committed, never printed.
6. **Local:** do not remove the legacy local MongoDB container on this PC. It keeps its data in an **anonymous** volume, so removing or recreating the container would orphan that data. It isn't part of this migration.

---

### 6. Verification and rollback per stage
| Stage | Verify | Rollback |
|---|---|---|
| 2 | Merging the trigger PR does not start a deploy run; "Run workflow" is available | Revert the workflow PR |
| 3 | `git ls-remote` on the old and new URLs returns the same SHAs; the VPS `git fetch` still works; Actions secrets are still listed | Rename back in Settings (the redirect is released and re-established) |
| 4 | New stack healthy on the temporary ports; smoke tests pass; same database name in the logs; nginx switched; 15-minute watch of error logs; old stack stopped but present | `nginx` upstream back to 4652/6356 plus a reload; `docker compose -p "$TAMAKKAN_BACKEND_PROJECT" start` (the image is pinned as `$LEGACY_BACKEND_IMAGE:pre-tamakkan-<date>`). Takes seconds |
| 5a | Tamakkan branding on every page, and in both emails | Set `NEXT_PUBLIC_APP_BRAND` back to the legacy brand (or remove it), then rebuild the frontend; reset the email/project name env |
| 5b / 6 | Tests plus manual checks per PR | Redeploy the previous SHA image |

---

### 7. Who can do what
| Action | Local (me) | Needs a GitHub admin | Needs VPS access | Needs DB or provider access |
|---|---|---|---|---|
| Stage 0 bundle, local inventory, drafting PRs on local branches | ✅ | | | |
| Pushing branches and opening PRs | ✅ with your approval (push permission) | | | |
| Checking name availability, webhooks, deploy keys, integrations | | ✅ | | |
| Branch protection | | ✅ | | |
| Changing the workflow trigger (PR) | ✅ PR | review and merge | | |
| Renaming repos | | ✅ | | |
| Stage 1 discovery commands | | | ✅ | |
| Stage 4 cutover | | | ✅ | |
| nginx changes | | | ✅ (root) | |
| Deploying the agent prompt change | | | | ✅ LiveKit Cloud |
| Optional `mongodump` | | | | ✅ |

---

### 8. Exact change list

**Code PRs (tamakkan_backend):**
- `.github/workflows/deploy.yml` (trigger, name, path, SHA tag)
- `docker-compose.yml` (`name:`, image)
- `Dockerfile` (comments)
- `app/core/config.py` (defaults; remove the DB default)
- `app/core/application.py` (description, link)
- `app/services/auth_service.py:99`
- `livekit_agent/prompts.py:43`
- `.env.example`, `.env.tamakkan.example`, `livekit_agent/.env.example`, `livekit_agent/Dockerfile`/`requirements.txt`/`agent.py` comments
- `nginx/system-site.conf.example`
- `app/__init__.py`, docstrings
- `app/jobs/seed_root.py` example
- `scripts/start-tamakkan.ps1` (search path)
- Optional: `TAMAKKAN_ENV_FILE` fallback (`config.py`, `livekit_agent/config.py`, `agent.py`, `scripts/tamakkan_*.py`, tests); Cloudinary folder constants and their tests (D4)
- Docs: `README.md`, `TAMAKKAN_ENVIRONMENT.md`, `PROJECT_ACCESS.md`, `EMAIL_VERIFICATION.md`, `LEARNING_ANALYTICS.md`, `BACKEND_VALIDATION.md`, `livekit_agent/README.md`; historical files moved to `docs/history/`

**Code PRs (tamakkan_frontend):**
- `.github/workflows/deploy.yml`
- `docker-compose.yml`
- `Dockerfile` (`ARG NEXT_PUBLIC_APP_BRAND` default)
- `.env.example`
- `nginx/system-site.conf.example`
- `lib/branding.ts`, `components/ui/Logo.tsx`, `app/globals.css`, `lib/i18n/messages/en.ts`/`ar.ts` (`auth.login.title`), `components/auth/LoginScreen.tsx:67` (Stage 6)
- Comments in `lib/api/*`, `lib/format.ts`, `next.config.ts`
- `package.json` name
- Docs: `README.md`, `BRANDING.md`, `TAMAKKAN_ENVIRONMENT.md`, `RESPONSIVE_DESIGN.md`, `DESIGN_SNAPSHOT.md`; historical files moved to `docs/history/`
- Optional: D3 cookie prefix (`lib/api/server/backend.ts:16`)

**VPS:**
- New `/root/tamakkan_backend` and `/root/frontendwebsite/tamakkan_frontend` (clones, copied `.env`, temporary `docker-compose.override.yml`)
- nginx site upstreams
- Rollback image tags
- Old directories archived after 30 days

**GitHub settings:**
- Repo renames
- Branch protection
- (Secrets unchanged)

**Not changed:**
- Database names, contents and connections
- Cloudinary assets
- Provider identifiers
- Git history
- Sibling LLM, Tamakan-new-design and Axis

---

### 9. Decisions needed before execution
- **D1 (naming collision):** `tamakkan_*` (the new names) versus the existing `tamakan_*` repos (Tamakan new design). Is the new design the future of this product (to be merged later), or separate? Should the `tamakan_*` repos be renamed or archived to avoid a one-letter confusion (for example `tamakan_newdesign_*`)? **That is outside this task and needs your call.**
- **D2:** is Sibling LLM staying a separate product with the legacy name? If so, is it expected to keep its shared-looking image tags? (Its own fix would be a separate, separately approved task.)
- **D3:** cookie prefix change (a one-time sign-out for all users), or keep the legacy prefix internally?
- **D4:** Cloudinary folder for new uploads: keep `$LEGACY_CLOUDINARY_FOLDER/`, or start `Tamakkan/`?
- **D5:** does the production **domain** contain the legacy brand name? If yes, a domain change is a bigger, separate project: DNS, TLS, allowed origins at SpatialReal and LiveKit if they are configured, `EMAIL_VERIFICATION_BASE_URL`, and a permanent 301 from the old domain so outstanding verification links keep working.
- **D6:** after cutover, keep the temporary ports 4662/6366, or move back to 4652/6356 in a later maintenance window?


---

## Draft: tamakkan-rename-stage1-report.md

### Tamakkan Migration — Stage 0 Result, Stage 1 Findings, Revised Sequence

> Approved scope executed: Stage 0, plus the read-only Stage 1 checks available without server access.
> Nothing was renamed, pushed, deployed or restarted; no database was touched.
> This document supersedes §3 (order of changes) of `tamakkan-rename-migration-plan.md`. The inventory there still stands.

### 1. Stage 0: H1 backup (done, verified)
| Item | Value |
|---|---|
| Backup file | `C:\Users\youss\OneDrive\Desktop\Test Tamakkan\h1-bff-refresh-coalescing.bundle` (outside both repos; 6,522 bytes) |
| SHA-256 | `847eed5fd6d4b64bf3c90cc9a1fdd5762719a2a2a9a210753d85b6f18e34d6d0` |
| Contents | `refs/heads/fix/h1-bff-refresh-coalescing` → `3a1f01d966b0f39ccf370ad85b79d6337b19b164`; requires base `255b34b8daea54509f6583341f802d601bc7b94f` (= current GitHub `main`) |
| `git bundle verify` | "is okay" |
| Restore test | Fresh clone of GitHub `main` in the scratchpad → fetched H1 **only from the bundle** → commits `010d697` and `3a1f01d` reproduced exactly, with the identical 6-file diff (+309/−14). **`test:bff`: 8/8 passed in the restored copy.** The scratch clone was then deleted, so no duplicate copy is left. |
| Original | Still on local branch `fix/h1-bff-refresh-coalescing` in `Test Tamakkan\frontend`; clean tree; no upstream; never pushed |

**Note:** the bundle sits in a OneDrive-synced folder, which gives an automatic off-machine copy. Keep it until H1 is merged.

### 2. Stage 1 findings (verified from GitHub and deploy logs; read-only)

### 2.1 Verified
| # | Fact | Evidence |
|---|---|---|
| V1 | The VPS pulls from GitHub over **SSH** (`github.com:kassel-for-information-technology/…`) | "From github.com:…" in the deploy logs |
| V2 | VPS container names: **Tamakkan** `$TAMAKKAN_BACKEND_CONTAINER`, `$TAMAKKAN_FRONTEND_CONTAINER`; **Sibling LLM** `$LLM_BACKEND_CONTAINER`, `$LLM_FRONTEND_CONTAINER`. The names don't collide (the compose projects differ). | deploy logs of all 4 repos |
| V3 | **All four deploys build the same unqualified tags:** `$SHARED_BACKEND_TAG` (Tamakkan 2026-09-23 10:10; **Sibling LLM 12:32, later**) and `$SHARED_FRONTEND_TAG` (Sibling LLM 06:44; **Tamakkan 10:11, later**) | "naming to docker.io/library/<shared legacy image>:latest" in each log |
| V4 | The deployed code matches the audited code: backend `d6815f4`, frontend `255b34b` | "HEAD is now at …" |
| V5 | Both backend deploys run `npm install --force`, which **fails on every deploy** (no `package.json`). Harmless, but noisy. | npm ENOENT in the logs |
| V6 | **`axis_backend` and `axis_frontend` contain deploy workflows copied from this project that `cd $TAMAKKAN_BACKEND_DIR` and `$TAMAKKAN_FRONTEND_DIR`, Tamakkan's production directories.** They trigger on every push to Axis `main`. | `gh api` workflow contents |
| V7 | The Axis repos have **no secrets**. Today's runs (2026-09-28 07:14) failed with `Error: missing server host` and **never reached a server**. | run logs |
| V8 | No other org workflow references the Tamakkan repos (all 19 visible repos' workflow files scanned). GitHub code search does not index these private repos (a sanity query returned 0), so it was not relied on. | direct file scan |
| V9 | Your account has push, not admin, on all repos. No branch protection, no environments. Webhooks and deploy keys can't be listed without admin. | `gh api` |

### 2.2 What V3 and V6 mean
- **Image tag collision (V3):** *if* both products are on the same host (not provable from GitHub, because `VPS_HOST` is masked; the VPS script §5 settles it), then right now:
  - `$SHARED_BACKEND_TAG` points to **Sibling LLM's** backend image;
  - `$SHARED_FRONTEND_TAG` points to **Tamakkan's** frontend image.
  - Running containers are unaffected. But a `docker compose up -d` without `--build`, a `docker compose up` after `docker compose down`, or re-creation after a host rebuild would start **the wrong product's code**: Tamakkan's backend would come up as Sibling LLM's, and Sibling LLM's frontend as Tamakkan's.
  - The existing deploy workflow is exactly this kind of shared-tag workflow, so **H1 must not ship through it** (your requirement).
- **Axis workflows (V6):** harmless today (no secrets). But if anyone adds the VPS secrets to Axis:
  - every Axis push would run `git reset --hard origin/main` and a rebuild inside Tamakkan's production directories, which is an unrequested Tamakkan production redeploy;
  - that would defeat any manual-deploy gate we add to the Tamakkan repos;
  - after the Tamakkan directories are renamed, those Axis runs would fail in unpredictable ways instead.
  - **Per your isolation rule I have not touched Axis.** Its owner needs to change or disable those workflows (decision/approval item E1).

### 2.3 Still needs the server (run the script)
Which host(s); which database each product uses; running image IDs versus tags; volumes and overrides; Mongo location; ports; nginx domains, TLS and body limits; cron, backup and logrotate; how the server authenticates to GitHub; whether the LiveKit agent runs on the VPS; disk space.

### 3. VPS read-only verification commands
**Script:** `C:\Users\youss\.claude\plans\tamakkan-vps-readonly-checks.sh`
- Its syntax has been checked.
- It has been scanned and contains no mutating command: no stop, start, restart, build, pull, tag, rm, git fetch/reset, nginx reload or database client.

**How to run it**, as the deploy user:
```bash
scp tamakkan-vps-readonly-checks.sh <user>@<vps>:~
ssh <user>@<vps> 'bash ~/tamakkan-vps-readonly-checks.sh 2>&1 | tee ~/tamakkan-stage1-$(date +%F).txt'
```
- It prints only allowlisted, non-secret settings (for example `MONGODB_DB_NAME`, `API_BASE_URL`, `AUTH_COOKIE_PREFIX`, `LIVEKIT_AGENT_NAME`).
- The Mongo URI is reduced to `host` and `uri_path`, and credentials in git remote URLs are masked.
- Review the report before sharing it.

| Section | Answers |
|---|---|
| 1, 5 | Same host for both products? (hostname plus image IDs versus tags) |
| 2, 3 | Directories, server-side uncommitted changes, override files, compose projects |
| 4, 6 | Containers, volumes, networks, mounts (confirms no data volumes) |
| **7** | **Which database each product uses** (`MONGODB_DB_NAME`, Mongo host, URI path, and the startup log line "Connected to MongoDB database '…'") |
| 8 | Frontend `API_BASE_URL`, cookie prefix, brand; whether the frontend container can actually reach the backend (the suspected I1 issue from Audit v2) |
| 9 | Is Mongo on this VPS (for example on the VPN address)? |
| 10 | Free ports for temporary stacks; final-port conflicts with Sibling LLM (3524/6868) |
| 11 | Real domains, upstreams, `client_max_body_size` (U1), TLS certificates (I2) → D5 |
| 12 | Cron, timers, backups, logrotate |
| 13 | SSH identity used for GitHub (deploy key or user key), which a repo rename preserves either way |
| 14 | LiveKit agent location |
| 15 | Disk space for keeping rollback images |

**Optional, needs separate approval (reads from the production database):** a read-only `mongosh` query listing each database's collection counts and the newest `live_sessions.started_at`, to confirm which database is *active*. It is not part of the script.

### 4. Revised, dependency-safe execution sequence
Each step needs separate approval. **G** = gate; do not proceed until it is satisfied.

| Step | What | Access | Depends on | Production effect |
|---|---|---|---|---|
| **S1-VPS** | Run the read-only script and review the report | VPS | — | none |
| **G1** | **Database decision gate.** A: separate databases → continue. B: both products on one database → **stop** and handle it as a separate data-isolation incident. C: other → decide. | — | S1-VPS | — |
| **E1** | Axis owner disables or repoints the Axis deploy workflows (`workflow_dispatch` only, or their own directories) | Axis owner | — (can happen now) | none |
| **2a** | **Manual-only deploys** for `$BACKEND_GITHUB_REPO`/`$FRONTEND_GITHUB_REPO`: trigger `workflow_dispatch`, plus `concurrency`; remove `npm install --force`. Merging this PR does not deploy (the pushed commit carries the new trigger). | PR (push) plus reviewer | E1 (otherwise Axis can bypass the gate) | none |
| **2b** | Branch protection on `main` (PR required, no direct pushes) | **GitHub admin** | 2a | none |
| **3** | **Image isolation release, code unchanged.** Compose files: `image: tamakkan_backend:${IMAGE_TAG}` / `tamakkan_frontend:${IMAGE_TAG}`, and **`name: $TAMAKKAN_BACKEND_PROJECT` / `$TAMAKKAN_FRONTEND_PROJECT` pinned to the *current* project names**, so container identity, directory and ports stay the same. The workflow builds `:${GITHUB_SHA}`, records the previous tag, runs a health gate, and reverts automatically. Nothing else changes: no H1, no branding. | PR, then **manual deploy** (approval) | G1, 2a, S1-VPS §5/§6 (no other override files; disk space) | One normal container recreate per service (the same restart as any deploy today). **Backend image content is identical to what's running** (same commit `d6815f4`, same models), so Beanie's startup index creation is a no-op. **Tamakkan never re-points the shared legacy tags again.** Before deploying: `docker tag <running image id> tamakkan_*:pre-isolation` for rollback. |
| **3v** | Verify: health; login; admin and student smoke tests; database name in the startup log unchanged; Sibling LLM containers untouched (same image IDs and start times as before) | VPS (read-only) | 3 | — |
| **H1** | Push `fix/h1-bff-refresh-coalescing` → PR → review → merge → **manual deploy through the isolated workflow**, a frontend-only release | push, reviewer, deploy approval | 3v | Frontend container recreate |
| **4** | **Rename the GitHub repos** → `tamakkan_backend`/`tamakkan_frontend`; `git remote set-url` in both local clone pairs; the server keeps working through the redirect (SSH) | **GitHub admin** | 2b; best after H1 (any order works technically) | none |
| **5a** | **Isolated test stack** on the VPS (see §5) under the new directory names, to rehearse the cutover | VPS | 4, S1-VPS §10 (free ports) | none on production |
| **5b** | **Directory cutover, never two stacks on the production database** (see §5) | VPS, maintenance window | 5a passed | ~15–60 s API/web interruption |
| **6a** | Branding by configuration: `NEXT_PUBLIC_APP_BRAND=tamakkan` (frontend rebuild), `PROJECT_NAME`, `EMAIL_BRAND_NAME` | VPS `.env`, manual deploy | 5b (or 3v, if the directory rename is postponed) | Visible rebrand; instant rollback by config |
| **6b** | Hard-coded strings (reset-email subject, Swagger text, **tutor prompt: LiveKit Cloud agent deploy**), docs, `.env.example`; the database default removed (fail fast) | PRs, deploys, LiveKit Cloud | 6a | — |
| **7** | Deferred decisions: cookie prefix (D3), `Tamakkan/` folder for new uploads (D4), domains (D5), `TAMAKKAN_ENV_FILE`, removal of the legacy brand code | separate approvals | S1-VPS §8/§11 | — |

**Why this order:**
- Image isolation (3) goes before H1, as you required.
- Deploy gating (2a) goes before any merge, and E1 goes before 2a, because otherwise Axis could redeploy Tamakkan behind the gate.
- The repo rename (4) is independent of the server thanks to redirects, so it isn't on the critical path.
- The directory cutover (5) comes last among infrastructure steps and is rehearsed first.
- The database is never modified at any step.

### 5. Isolated test environment and cutover (revised per your requirement)
**Test stack (5a).** Nothing in it can reach production data or providers:
- **Its own MongoDB container:** compose service `mongo` in project `tamakkan_backend_test`, with its own named volume `tamakkan_backend_test_mongo`, on the project's internal network. **No host port** for Mongo.
- **Its own `.env.test`**, built from `.env.example`, **not** copied from production:
  - `MONGODB_URI=mongodb://mongo:27017`, `MONGODB_DB_NAME=tamakkan_test`;
  - `EMAIL_DELIVERY_MODE=logging`;
  - **empty `LIVEKIT_*`** (session start returns 503, so no rooms and no agent dispatch);
  - empty `GEMINI_API_KEY` (no billed jobs);
  - empty `CLOUDINARY_*` (uploads fail closed);
  - a new JWT secret and `AGENT_INTERNAL_API_KEY`;
  - `LIVEKIT_AGENT_NAME=tamakkan-test-noop`.
- **Ports:** API and web bound to `127.0.0.1` on temporary ports chosen from S1-VPS §10. The compose project names `tamakkan_backend_test` / `tamakkan_frontend_test` don't touch production containers.
- **Not added to nginx.** Testers reach it through an SSH tunnel (`ssh -L 16366:127.0.0.1:<port> …`).
- **Frontend `.env.test`:** `API_BASE_URL` points to the test API, and `AUTH_COOKIE_PREFIX=tamakkan_test`, so test logins never share cookies with production even on the same browser.
- **Checks:** clean build, a seeded test root/admin/student, login, refresh, admin and student UI, and the H1 behaviour.
- **Teardown after the test:** `docker compose -p tamakkan_*_test down -v`. This deletes the **test** volume only, and needs approval at that time.

**Production cutover (5b).** It is sequential, so there is never an overlap on the production database:
1. **Pre-checks:**
   - no active live sessions (admin API `GET /sessions`: nothing in `connecting`/`connected`);
   - no transcript generation in flight;
   - a low-traffic window.
2. **Pin rollback images.** They are already SHA-tagged after step 3.
3. **Prepare** `/root/tamakkan_backend`:
   - a fresh clone of the renamed repo, at the **same commit as running**;
   - `cp -p` the production `.env` unchanged;
   - compose `name: tamakkan_backend`, **same host port 4652** (final port per D6, assuming no conflict);
   - **no** `up` yet.
4. **Switch:**
   - `docker compose -p "$TAMAKKAN_BACKEND_PROJECT" stop` (old; not `down`, no `-v`);
   - then `docker compose up -d` in the new directory using the already-built SHA image (no rebuild);
   - then a health gate.
   - The nginx upstream port is unchanged, so **no nginx change is needed**.
5. **Verify:**
   - the startup log shows the same database name;
   - login;
   - admin and student smoke tests;
   - Sibling LLM untouched.
6. **Repeat for the frontend**, with `/root/frontendwebsite/tamakkan_frontend` on port 6356.
7. **Workflow PR:** `cd /root/tamakkan_…`, `name: tamakkan_…`, merged **after** the directories exist (deploys are manual, so there is no race).
8. **Rollback, in seconds:** `docker compose -p tamakkan_backend stop`, then `docker compose -p "$TAMAKKAN_BACKEND_PROJECT" start`. The old container, image and directory are intact, and the old directory is kept 30 days.

**Why it's safe on the database:**
- Only one API ever runs against production.
- The new container runs the same commit as the old one, so index definitions are identical and startup index creation is a no-op.
- There are no schedulers, so no duplicate background processing.
- The pre-check ensures no live sessions lose their end-of-session callback during the ~15–60 s swap. The worker's single-try callback (audit finding A1) is the reason for this pre-check.

### 6. Decisions and escalations
- **E1 (Axis owner):** disable or repoint the Axis deploy workflows. Blocks step 2a.
- **E2 (Sibling LLM owner, information only; I won't act on it, per D2):** if both products share the host, `$SHARED_FRONTEND_TAG` currently points to **Tamakkan's** frontend image, so a Sibling LLM frontend restart-without-build would serve Tamakkan's frontend. After step 3, Tamakkan stops touching these tags, but the current pointer stays wrong until Sibling LLM's next build, or until their owner re-tags it to their running image. Their call.
- **G1:** database outcome from S1-VPS §7.
- **D5/D6** are resolved from S1-VPS §10–11. The default recommendation is to keep the final ports 4652/6356 (no nginx change), because Sibling LLM uses 3524/6868 per its repo; to be confirmed.
- **Admin needed for:** 2b branch protection, 4 repo rename, listing webhooks and deploy keys.
- **VPS needed for:** S1-VPS, 3 (rollback tag and manual deploy), 3v, 5a, 5b.


---

## Draft: three-project-isolation-report.md

### Three-Project Infrastructure Isolation Report

> Verification and planning only. Nothing was modified, pushed, renamed, restarted or deployed. No secrets were read or printed.
>
> **Status markers:**
> - **V** = verified (GitHub API, deploy logs, repo files at the current `main`, or this PC)
> - **I** = inferred
> - **U** = unknown; needs the VPS or a provider console
>
> **Checked on:** 2026-09-28. Axis changed its workflows at 10:43 today, so the Axis Stage 1 findings are updated below.

### 1. Infrastructure comparison
| | **Tamakkan** (current project) | **Sibling LLM** | **Axis** |
|---|---|---|---|
| GitHub repos | `$BACKEND_GITHUB_REPO`, `$FRONTEND_GITHUB_REPO` **V** | `$LLM_BACKEND_REPO`, `$LLM_FRONTEND_REPO` **V** | `axis_backend`, `axis_frontend` **V** |
| `main` HEAD | `d6815f4` / `255b34b` **V** (the deployed commits, **V** from logs) | `b808297` / `ba97985` **V** (deployed, **V**) | `238ecbb` / `c46e5a7` **V**, never deployed **V** |
| Other branches | backend: the legacy integration branch (merged) + 5 `feature/*`; frontend: the legacy integration branch + 3 `feature/*`; **local only:** `fix/h1-bff-refresh-coalescing` **V** | `main` only **V** | `main` only **V** |
| Local folders | `Test Tamakkan\<legacy-named folders>` (**has H1**) and `Full Projects\<Sibling LLM folder>\<second Tamakkan clone>` (same `main`, no H1; running on 4652/5173) **V** | `Full Projects\<Sibling LLM folder>\<Sibling LLM repos>` **V** | `Full Projects\Axis\axis_*` **V** |
| Deploy trigger | **every push to `main`** **V** | **every push to `main`** **V** | backend: `workflow_dispatch` only; frontend: manual **and** `vars.AXIS_DEPLOY_ENABLED=='true'` (unset) **V** |
| VPS directory | `$TAMAKKAN_BACKEND_DIR`, `$TAMAKKAN_FRONTEND_DIR` **V** (workflow + log) | `$LLM_BACKEND_DIR`, `$LLM_FRONTEND_DIR` **V** | from `AXIS_*_DIR`, **not set**; the workflow refuses to run unless the compose file says `name: axis-…` **V**. Whether it exists on the VPS: **U** |
| Deploy secrets | `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY` **V** | `VPS_*` (same names) **V**; same *values*: **U** | `AXIS_VPS_*` (distinct names), **none set** **V** |
| Same server as Tamakkan? | — | **I** (identical `/root/frontendwebsite/` layout and user); **U** | **U** |
| Docker image | `$SHARED_BACKEND_TAG`, `$SHARED_FRONTEND_TAG` **V** | **the same tags** **V** | `axis-backend:latest`, `axis-frontend:latest` **V** |
| Compose project / containers | `$TAMAKKAN_BACKEND_PROJECT` → `$TAMAKKAN_BACKEND_CONTAINER`; `$TAMAKKAN_FRONTEND_CONTAINER` **V** (logs) | `$LLM_BACKEND_CONTAINER`, `$LLM_FRONTEND_CONTAINER` **V** | pinned `name: axis-backend` / `axis-frontend`; network `axis` **V** (files) |
| Host ports (127.0.0.1) | API 4652, web 6356 **V** (files) | API 3524, web 6868 **V** (files) | API 4660, web 6360 (overridable) **V** (files) |
| Frontend → backend URL | example `localhost:4652` **V**; production value **U** | **example `localhost:4652`, which is Tamakkan's API port** **V**; production value **U** | `127.0.0.1:4660` **V** |
| Auth cookie names | legacy-prefixed (default prefix) **V** | **`<legacy prefix>_access_token` hard-coded, the same names as Tamakkan** **V** | `axis_*` **V** |
| Domains, TLS | placeholders only (`tamakkan.example`) **V**; real values **U** | **U** | **U** |
| Database | code default `$DB_LEGACY` **V**; production value **U** | **example default also `$DB_LEGACY`** **V**; production value **U** | `axis_dev` on its own Mongo (`127.0.0.1:27030`, container `axis-mongo` on this PC) **V** for dev; production **U** |
| Mongo server | shared server reached over a private address (from the `.env` values you provided) **I**; it hosts `$DB_LEGACY`, `$DB_LLM`, `tamakan` … **V** (earlier read-only listing) | **U** | local container for dev **V** |
| Env files | `.env` in each VPS directory (compose `env_file`) **V** | same pattern **V** | same pattern **V** |
| LiveKit agent name | `mariam-agent` (default; env-overridable) **V** | **`mariam-agent`, hard-coded** (`livekit_agent/agent.py:57`, and in the backend dispatch) **V** | `AXIS-AGENT` **V** |
| LiveKit agent deploy | LiveKit Cloud, from `livekit_agent/livekit.toml` (project and agent id committed) **V** | no `livekit.toml`; how the worker is deployed is **U** | `livekit.toml` has empty ids (not deployed yet) **V** |
| LiveKit project (`LIVEKIT_URL`) | the project in your `.env` matches `livekit.toml`'s subdomain **I** | **U** | **U** |
| SpatialReal | avatar id default `ca9c5c22…` **V**; app id from env **U** | **the same avatar id default** **V**; app id **U** | **the same avatar id default** **V**; app id **U** |

### 2. Cross-project conflicts
| # | Conflict | Products | Evidence | Status | Correction |
|---|---|---|---|---|---|
| **C1** | **Shared image tags** `$SHARED_BACKEND_TAG` / `$SHARED_FRONTEND_TAG`. Each deploy re-points the tag the other product uses. | Tamakkan ↔ Sibling LLM | All four deploy logs name the same tags. The last builds were: backend by Sibling LLM (12:32), frontend by Tamakkan (10:11). | **Confirmed in config. Harmful only if they share a server (U).** | Tamakkan builds its own images (`tamakkan_backend:<sha>`, `tamakkan_frontend:<sha>`). Sibling LLM's own fix is theirs (D2). |
| **C2** | **Shared LiveKit agent name `mariam-agent`.** If both products use the same LiveKit Cloud project, LiveKit sends each product's sessions to whichever `mariam-agent` worker picks them up. The wrong worker uses the wrong prompts and calls the wrong backend with an unknown session id, so the session gets stuck and its transcript and report are lost. **Redeploying one product's agent can take over the other's sessions.** | Tamakkan ↔ Sibling LLM | Tamakkan's config default and Sibling LLM's hard-coded `AGENT_NAME` | **Confirmed in code. Harmful only if `LIVEKIT_URL` is the same (U).** | Tamakkan sets its own `LIVEKIT_AGENT_NAME` (e.g. `tamakkan-mariam`) in the backend `.env` **and** in the LiveKit Cloud agent, deployed backend-first with a fallback (see §5). Better still, separate LiveKit projects. |
| **C3** | **Possible shared database.** Both products default to `MONGODB_DB_NAME=$DB_LEGACY`, and the app ignores the database in the URI path. | Tamakkan ↔ Sibling LLM | both `.env.example` files, `database.py:63-66` | **U: the key question** | VPS script §7/§17. If they share one, stop and plan a separate, approved data-isolation migration. **Never as part of a rename.** |
| **C4** | **The Sibling LLM frontend may be calling Tamakkan's backend.** Its example `API_BASE_URL` uses port 4652, but its own API listens on 3524. | Sibling LLM → Tamakkan | `$LLM_FRONTEND_REPO/.env.example`, `$LLM_BACKEND_REPO/app/main.py` | **U** (depends on the production `.env`) | VPS script §8/§17. If confirmed, the Sibling LLM owner corrects their `.env`. |
| **C5** | **Same cookie names** (`<legacy prefix>_access_token` / `_refresh_token`). If both frontends are served on the **same hostname** (for example different ports or paths), each overwrites the other's login. | Tamakkan ↔ Sibling LLM | `backend.ts:16` (Tamakkan), hard-coded in Sibling LLM | **U** (domains unknown) | Only matters if the hostnames match. If they do, Tamakkan moves to a `tamakkan` prefix; that is decision D3, a one-time re-login. |
| **C6** | Axis workflows targeted Tamakkan's directories | Axis → Tamakkan | earlier Stage 1 | **Resolved by Axis at 10:43 today (V):** manual only, directory from a variable, compose-name guard; no secrets or variables set | None needed. Re-check before Tamakkan's directory cutover. |
| **C7** | Shared SpatialReal avatar id | all three | `.env.example` / config defaults | **Low:** an avatar id is a shared asset, not an account. Sharing the **app id / API key** would share quota and billing. **U** | Script §17 compares app ids and keys (same/different only). |
| **C8** | Possibly shared provider credentials (JWT secret, agent internal key, OpenAI, Ultravox, Gemini, Cloudinary, SMTP). A shared `JWT_SECRET_KEY` means **tokens from one product would be accepted by the other** if user ids collided (they are random UUIDs, so unlikely). A shared Cloudinary account mixes uploads (both use the `$LEGACY_CLOUDINARY_FOLDER/` folder). | Tamakkan ↔ Sibling LLM | Sibling LLM uses the same folder constants as Tamakkan's code **I** | **U** | Script §17 (fingerprint comparison, no values). Rotate or split any shared secret as a separate approved change. |
| **C9** | A Tamakkan deploy triggers automatically on every push to `main` | Tamakkan (self) | workflow | **V** | Manual-only deploys before any merge (§5). |

**Not conflicts:**
- Compose project and container names are distinct (V).
- Host ports are distinct, per the files (V; confirm on the server).
- None of the three compose files declares data volumes (V).
- On this PC, Axis has its own Mongo container and data volume (V).

### 3. Remaining VPS checks (for the server administrator)
**Script:** `C:\Users\youss\.claude\plans\tamakkan-vps-readonly-checks.sh`
- Read-only; syntax checked; scanned for mutating commands (none).
- It prints only allowlisted non-secret settings. Secrets are compared as **same/different**, never printed. URIs are reduced to host and path.

**Run it as the deploy user:**
```bash
bash ~/tamakkan-vps-readonly-checks.sh 2>&1 | tee ~/tamakkan-stage1-$(date +%F).txt
```

| Question | Script section |
|---|---|
| Are the products on one server? Which image does each container really run, versus what the tags point to? | 1, 5 |
| Directories, server-side changes, override files, compose projects, volumes, mounts | 2, 3, 4, 6 |
| **Which database each product uses** (C3) | 7, 17 |
| Which backend each frontend calls (C4); frontend→backend reachability | 8, 17 |
| Is Mongo on this server? | 9 |
| Ports in use and free (for temporary test stacks) | 10 |
| Domains, TLS, upload limits (C5, D5) | 11 |
| Cron, backups, log rotation | 12 |
| GitHub SSH identity | 13 |
| Is any agent running on the VPS? | 14 |
| Disk space for rollback images | 15 |
| Axis directories or containers on the server | 16 |
| **Shared secrets, database, LiveKit project, SpatialReal app, Cloudinary, SMTP** (C2, C7, C8) | 17 |

**Also needed outside the VPS (LiveKit Cloud account owner, read-only):**
- `lk project list`, then for each product's project `lk agent list` / `lk agent status`. Or check the LiveKit Cloud dashboard → Agents.
- Which LiveKit project(s) exist? Which agents are registered with name `mariam-agent`? Where is Sibling LLM's worker deployed (it has no `livekit.toml`)?
- **SpatialReal console:** is there one app per product, or one shared app (quota and billing)?

### 4. Separation plan (protects production and data)
**Principle:** fix only what can collide at runtime; never move data; one product at a time; Tamakkan changes only Tamakkan.

1. **Verify** (§3). Decision gates:
   - **G-DB (C3):** if Tamakkan and Sibling LLM share a database → **stop.** Isolating the data is its own approved project, not part of this.
   - **G-LK (C2):** if they share a LiveKit project and agent name → the agent-name split (step 4) becomes a prerequisite for H1 as well.
   - **G-FE (C4):** if Sibling LLM's frontend calls Tamakkan's backend → notify the Sibling LLM owner **before** any Tamakkan backend restart or cutover, because it would go down with ours.
2. **Gate Tamakkan deploys:** `workflow_dispatch` only, plus `concurrency`; remove `npm install --force`. Merging this change does not deploy.
3. **Isolate Tamakkan's images** (C1). Compose `image: tamakkan_backend:${IMAGE_TAG}` / `tamakkan_frontend:${IMAGE_TAG}`, and **pin `name: $TAMAKKAN_BACKEND_PROJECT` / `$TAMAKKAN_FRONTEND_PROJECT`**, so the containers, directories and ports stay exactly as they are. The workflow builds `:${GITHUB_SHA}`, records the previous tag, runs a health gate and reverts on failure.
   - **First deploy:** run manually, with the same code that's running now. The only effect is one normal container recreate per service.
   - **Before deploying:** tag the currently running image IDs as `tamakkan_*:pre-isolation` for rollback.
   - **Verify:** Sibling LLM's container image IDs and start times are unchanged.
4. **Split the LiveKit agent name**, only if G-LK finds sharing:
   - (a) Deploy the Tamakkan LiveKit Cloud agent registered as `tamakkan-mariam`, alongside the existing one. LiveKit routes by name, so nothing changes yet.
   - (b) Set `LIVEKIT_AGENT_NAME=tamakkan-mariam` in Tamakkan's backend `.env` and redeploy manually.
   - (c) Verify a session reaches the Tamakkan worker.
   - **Rollback:** revert the `.env` value.
   - Sibling LLM's `mariam-agent` is never touched.
5. **Shared secrets** (C8), only if step 1 shows sharing: rotate Tamakkan's copy of each shared secret, one at a time, as separate approved changes. Rotating the JWT secret logs users out once, so schedule it.
6. **Cookies** (C5): only if the domains show a shared hostname (D3).

Steps 2–3 change only Tamakkan files and Tamakkan containers. Steps 4–6 each depend on a verified finding.

### 5. Minimum changes before H1 can be deployed safely
1. §3 checks done, and **G-DB** and **G-LK** evaluated. H1 is a frontend-only change, but its deploy recreates Tamakkan's frontend container, so we need to know what that container is connected to.
2. **Deploy gating** (step 2) merged.
3. **Image isolation** (step 3) deployed and verified: Tamakkan's frontend now builds and runs `tamakkan_frontend:<sha>` and **never re-points `$SHARED_FRONTEND_TAG`**. Sibling LLM verified unaffected.
4. The agent-name split (step 4) is **not** required for H1: H1 doesn't touch sessions or the agent. It is required before Tamakkan's next *backend* or agent release **if** G-LK shows sharing.
5. **Then H1:**
   - push `fix/h1-bff-refresh-coalescing` from `Test Tamakkan\frontend` (**not** from the `Full Projects\<Sibling LLM folder>\<second Tamakkan frontend clone>` clone);
   - PR, review, merge;
   - **manual run of Tamakkan's isolated workflow**;
   - verify: login, refresh, two concurrent users.
   - **Rollback:** redeploy the previous `tamakkan_frontend:<sha>`.
   - The bundle stays in place until the merge is confirmed.

### 6. Afterwards: repository and directory rename (Tamakkan only)
1. **GitHub rename** (admin): `$BACKEND_GITHUB_REPO` → `tamakkan_backend`, `$FRONTEND_GITHUB_REPO` → `tamakkan_frontend`.
   - Secrets, PRs, history and deploy keys are kept.
   - Git over SSH on the VPS follows the redirect.
   - Update `origin` in both local Tamakkan clones.
   - **Never recreate the old names.**
2. **Rehearsal on an isolated test stack:**
   - its own Mongo container, volume and database;
   - empty LiveKit, Gemini and Cloudinary keys;
   - logging-only email;
   - `tamakkan_test` cookies;
   - temporary ports, no nginx entry, SSH tunnel only.
   - It never touches production data or providers.
3. **Production directory cutover**, sequential and never two stacks on the production database:
   - pre-check no active sessions;
   - `stop` the old stack (not `down`, no `-v`);
   - `up` the new stack at the same commit and SHA image, same ports, from `/root/tamakkan_backend` (compose `name: tamakkan_backend`);
   - verify; repeat for the frontend;
   - then merge the workflow path change.
   - **Rollback:** stop new, start old (kept 30 days).
   - **Re-check Axis's `AXIS_*_DIR` values first**, so nothing points at the old path.
4. **Branding by configuration** (`NEXT_PUBLIC_APP_BRAND=tamakkan`, `PROJECT_NAME`, `EMAIL_BRAND_NAME`), then the hard-coded strings, including the tutor prompt (LiveKit Cloud agent deploy).
5. **Deferred:** cookie prefix (D3), a `Tamakkan/` folder for new uploads (D4), domains (D5). The database name is never renamed.

### 7. Owners and notifications
- **Sibling LLM owner (information only; no action by us, per D2):**
  - C1: their `$SHARED_FRONTEND_TAG` may currently point to Tamakkan's image;
  - C2: they hard-code `mariam-agent`;
  - C4: their example `API_BASE_URL` points at Tamakkan's port;
  - C5: cookie names.
- **Axis owner:** C6 is resolved. When Axis goes live: its own server directories, secrets, database, LiveKit project and agent name are already in place in its config. It should verify its SpatialReal app id isn't shared (C7).
- **Access needed:**
  - VPS administrator: §3;
  - LiveKit Cloud and SpatialReal account owners: agent and app checks;
  - GitHub admin: branch protection and the rename.
