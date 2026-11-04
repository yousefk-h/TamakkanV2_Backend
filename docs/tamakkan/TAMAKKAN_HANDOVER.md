# TAMAKKAN — Handover

> This is the current state of the local work on **TAMAKKAN**, and how to continue it. The plan itself is in `TAMAKKAN_MIGRATION_PLAN.md`.
>
> **As of 2026-09-28.** Nothing has been pushed, merged, deployed, renamed on GitHub, or changed on any server.

## 1. This folder
```
Test Tamakkan/
├── backend/                         (local backend checkout; see §4 for the folder rename)
├── frontend/                        (local frontend checkout; holds the H1 branch)
├── h1-bff-refresh-coalescing.bundle   verified backup of H1
├── TAMAKKAN_MIGRATION_PLAN.md         the plan (single source of truth)
├── TAMAKKAN_HANDOVER.md               this file
├── tamakkan-vps-readonly-checks.sh    read-only VPS check for the server administrator (§6)
└── rename-local-folders.ps1           performs the local folder rename safely (§4)
```

## 2. Repositories and branches
| Local folder | GitHub (still the old names) | Checked-out branch | Key commits |
|---|---|---|---|
| backend | `kassel-for-information-technology/$BACKEND_GITHUB_REPO` | `main` @ `d6815f4` (= deployed) | — |
| frontend | `kassel-for-information-technology/$FRONTEND_GITHUB_REPO` | **`fix/h1-bff-refresh-coalescing` @ `3a1f01d`** | `main` @ `255b34b` (= deployed); H1: `010d697` (tests), `3a1f01d` (fix) |

- **Docker image isolation (local only, not pushed, not deployed):**
  - branch `infra/tamakkan-platform-image-isolation` in both repos:
    - backend `1c333d5`, `9856b97`, `0e80f74` on `main@d6815f4`;
    - frontend `7160c0a`, `360b4a6`, `664f78d` on `main@255b34b`;
  - no upstream set; H1 is not included;
  - runbook: `TAMAKKAN_DOCKER_ISOLATION_DEPLOYMENT.md` (**NOT APPROVED FOR EXECUTION**).
- **Checked-out branches:** the backend working copy is on `main`, and the frontend on the H1 branch, exactly as before.
- **Integrated local version (not pushed, not deployed):** branch `integration/tamakkan-local` in both repos, each in its own worktree:

  | Worktree | Tip | Contains |
  |---|---|---|
  | `Test Tamakkan\worktreesackend-integration` | `630645fc86c11100287e1e8b3f52bafcacd8aa70` | `main` + Docker isolation |
  | `Test Tamakkan\worktreesrontend-integration` | `1c183f6bf187a220c5dea0848fe0c1f71f7dc90a` | `main` + Docker isolation (`ebb54ceb`) + H1 |

  - Merged with `--no-ff`. There were **no conflicts**: the isolation and H1 changes touch disjoint files.
  - Every integrated file is byte-identical to its approved source branch, and nothing else is included.
  - The original branches, checked-out working copies and the H1 bundle are untouched.
  - After the pending local folder rename, `rename-local-folders.ps1` repairs the worktree links automatically.
- Both working trees are clean, with no stashes and no extra worktrees.
- `origin` still points to the legacy repository URLs, **on purpose**: the `tamakkan_*` repos don't exist until the GitHub rename is approved (plan step 7). After that rename:
  ```bash
  git remote set-url origin https://github.com/kassel-for-information-technology/tamakkan_backend.git    # in tamakkan_backend
  git remote set-url origin https://github.com/kassel-for-information-technology/tamakkan_frontend.git   # in tamakkan_frontend
  ```

## 3. H1: the critical security fix
- **What it fixes:** the frontend's refresh handling was shared across all users, so under load one user could receive another user's session cookies. This was reproduced, and fixed by keying the coalescing per refresh token.
- **Files changed:**
  - `app/api/backend/[...path]/route.ts`
  - new `tests/bff-refresh.test.mjs` and `tests/helpers/*`
  - `package.json` (script `test:bff`)
- **Tests:**
  - 4 of 8 fail on the original code, and 8 of 8 pass with the fix;
  - ESLint clean; no new `tsc` errors;
  - live check on the local stack: correct.
- **Backup:** `h1-bff-refresh-coalescing.bundle`, SHA-256 `847eed5fd6d4b64bf3c90cc9a1fdd5762719a2a2a9a210753d85b6f18e34d6d0`. Verified by restoring it into a fresh clone, where the tests pass 8/8.
- **Restore if ever needed:**
  ```bash
  git fetch <path-to>/h1-bff-refresh-coalescing.bundle fix/h1-bff-refresh-coalescing:fix/h1-bff-refresh-coalescing
  ```
- **Deploy rule:** only after image isolation (plan step 4), through Tamakkan's own manual workflow. **Never** through a workflow that shares image tags with Sibling LLM. Push only from this folder's frontend, not from the second clone under `Full Projects` (the Sibling LLM folder).
- **Run its tests:** `npm run test:bff` in the frontend folder.

## 4. Local folder rename: pending
**Checked, and safe:**
- Git has no absolute paths and no worktrees.
- Nothing in the code depends on the folder names (only two comments mention them).
- `node_modules` is relocatable.
- The H1 branch and the bundle are unaffected.

**Needs care:** the backend `.venv` records absolute paths in its launchers and activate scripts. The rename script rebuilds it with the same pinned `requirements-dev.txt`, and **restores the old one if the reinstall fails**.

**Why it's pending:**
- Windows refused both renames ("Access denied") because a program still has files open inside the folders.
- A rename of a test folder next to them worked, and none of my processes use the folders any more.
- The most likely holders are **VS Code**, which has this workspace open (its git integration watches both `.git` folders, and `backend\.env` is open in an editor), and **OneDrive** syncing `node_modules`.

**To finish:**
1. Close VS Code, and any terminal or editor opened inside these folders.
2. OneDrive icon → **Pause syncing** → 2 hours.
3. In a normal PowerShell window:
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\youss\OneDrive\Desktop\Test Tamakkan\rename-local-folders.ps1"
   ```
   It checks the trees are clean and nothing is running from them, then renames both folders and verifies HEADs, `git fsck`, the H1 branch tip and the bundle. It then rebuilds the venv and runs the H1 tests and the backend core tests. It is safe to re-run.
4. Resume OneDrive, then reopen VS Code on `Test Tamakkan`.

**Not changed by the rename:**
- the GitHub repo names and remote URLs;
- deploy workflows and VPS paths (`$TAMAKKAN_BACKEND_DIR` etc.; those change only in plan step 9);
- in-repo docs that mention `backend/` as a folder name. Those are updated by the naming PRs in plan steps 7/11, because editing them now would mean commits nobody has approved.

## 5. Local development environment
| Service | How | Port | State now |
|---|---|---|---|
| Local Tamakkan MongoDB | Docker container `tamakkan-mongo`, volume `tamakkan-mongo-data`, database `tamakkan_dev` | 27019 | running (`docker start tamakkan-mongo` after a reboot) |
| Tamakkan backend (local profile) | `.\tamakkan_backend\scripts\start-tamakkan.ps1` | 4653 | stopped (for the rename) |
| Tamakkan frontend (local profile) | `.\tamakkan_frontend\scripts\start-tamakkan.ps1` | 5176 | stopped (for the rename) |
| Demo accounts | `tamakkan_backend\.tamakkan\local-accounts.json` (teacher, student, root; random passwords) | — | seeded in `tamakkan_dev` only |

- The local profiles are `.env.tamakkan` in both folders. They were generated by the launchers and are git-ignored.
- **Not part of this work, and untouched:** the legacy-named checkout running from the Sibling LLM folder under `Full Projects` (ports 4652/5173), the legacy local MongoDB container, `axis-*` and `tamakkan-backend-*` containers.

**⚠ Sensitive local file.** `tamakkan_backend\.env` (created 2026-09-27 from values you pasted) contains **real production-style credentials**: the shared MongoDB server, OpenAI, LiveKit, Cloudinary, Gemini, SpatialReal and SMTP. It is git-ignored and not in use (nothing is running with it).
- **Recommendation:** delete it once you confirm it isn't needed, and consider rotating those keys, because they were shared in chat. I haven't deleted it, because you asked to keep env files.
- The same applies to `tamakkan_frontend\.env.local`, which points to a local port that is no longer used.

## 5b. Local integration test environment (2026-09-28)
- **Isolated database:** Docker container `tamakkan-integration-mongo` (label `purpose=tamakkan-local-integration-test`), new volume `tamakkan-integration-mongo-data`, `127.0.0.1:27039`, database `tamakkan_integration`. It holds test users and sample data only. Remove it when no longer needed:
  ```bash
  docker rm -f tamakkan-integration-mongo && docker volume rm tamakkan-integration-mongo-data
  ```
- **Backend:** the integrated worktree on `127.0.0.1:4673` with `worktreesackend-integration\.env.integration`.
  - git-ignored; empty LiveKit / Gemini / Cloudinary / YouTube keys; logging-only email; verification off.
  - Root test account: `root@integration-test.example.com`; its random password is in this session's scratchpad only.
- **Frontend:** the **production build** (`dist/standalone/server.js`) on `127.0.0.1:5186`, with `API_BASE_URL=http://127.0.0.1:4673/api/v1` and cookie prefix `tamakkanint`.
- These two app processes ran inside this working session and stop when it ends. To restart them, run the same commands from the worktrees.

**End-to-end result: 54 of 55 automated checks passed.**
- **Pages:** `/`, `/admin`, `/student` and `/verify-email` render.
- **Accounts:** root → teacher → students.
- **Teacher:** create and edit lessons; a standard calendar; activation; Standard-plan assignment; a group class; every roster, alert, session, submission, analytics and calendar list.
- **Student:** day-1 lesson opens; day-2 locked (403); no-plan student sees nothing (403); progress, calendar, analytics and own lists all 200; **the avatar prompt is `null` in the student view**.
- **Role guards**, including flat roles (root can't use teacher endpoints).
- **H1 through the real BFF:** two users refreshing at once keep their own identity; the same user ×3 shares one successor; the rotated token is rejected.
- **Instructor password reset:** the old password fails, the new one works, and existing refresh tokens are revoked.
- **Logout:** clears cookies, and the refresh token can't be reused.
- **API contract:** all 40 frontend path templates exist in the backend. The single "failed" check was my parser tripping over one conditional URL whose two real paths returned 200 in the same run.

**Not validated (need real provider credentials):**
- **LiveKit / SpatialReal / OpenAI / Ultravox live sessions.** Session start returns a clean 503 "not configured" when LiveKit is absent. The avatar-prompt-in-token issue (audit B1) can only be exercised with LiveKit.
- **Gemini** (transcripts, drafting, English review, scoring).
- **Cloudinary uploads** (video check-ins, thumbnails).
- **YouTube duration lookup**, and **SMTP email**.

**Database selection (for the VPS team's planned new database):**
- The backend picks its database **only** from `MONGODB_DB_NAME` (`app/core/database.py:65`). This was confirmed live: it connected to `tamakkan_integration` purely from the env file.
- The cutover is therefore a one-line change to the server `.env`, with **no code change**.
- **MongoDB names are case-sensitive:** `Tamakkan` ≠ `tamakkan`.
- **The database name in the `MONGODB_URI` path is ignored**, so changing only the URI does not switch databases.
- The code default was left unchanged at the time; it has since been removed, so `MONGODB_DB_NAME` must be set explicitly.

## 6. Waiting on other people
| Who | What | Material |
|---|---|---|
| **VPS administrator** | Run the read-only check and return the sanitised report plus the template. Answers: same server? which database? same LiveKit project? which backend each frontend calls? | `tamakkan-vps-readonly-checks.sh` (SHA-256 `32aaca17e4e96acdcae58f29afc90c7b9472dc865715924edfc95b164d2a94cf`) and the email below |
| **LiveKit Cloud owner** | Which agents are registered as `mariam-agent`, in which project; where Sibling LLM's agent runs | — |
| **GitHub org admin** | Later: branch protection; repo rename | plan steps 3, 7 |
| **Sibling LLM owner (information only)** | Their image tags, agent name and cookie names overlap with ours; their example config points at Tamakkan's API port | plan Part B |

### Email to the VPS administrator (ready to send; attach `tamakkan-vps-readonly-checks.sh`)
**Subject:** Read-only verification of Tamakkan and Sibling LLM on our VPS (no changes)

> Hello,
>
> We are preparing a security update for **Tamakkan**. Its code currently lives in the GitHub repositories `$BACKEND_GITHUB_REPO` and `$FRONTEND_GITHUB_REPO`, deployed to `$TAMAKKAN_BACKEND_DIR` and `$TAMAKKAN_FRONTEND_DIR`. A separate product, **Sibling LLM** (`$LLM_BACKEND_DIR`, `$LLM_FRONTEND_DIR`), was built from almost the same code. Our GitHub review found settings that may overlap between the two: the same Docker image names, the same default database name, the same LiveKit agent name, and a frontend that may point to the other product's backend. Before we deploy anything, we need you to check how the server is actually set up. **This is verification only: please don't change, restart or fix anything.**
>
> 1. Copy the attached script over SSH: `mkdir -p ~/tamakkan-check`, then `scp tamakkan-vps-readonly-checks.sh <user>@<vps>:~/tamakkan-check/`
> 2. Check it arrived unchanged: `cd ~/tamakkan-check && sha256sum tamakkan-vps-readonly-checks.sh`. It must be `32aaca17e4e96acdcae58f29afc90c7b9472dc865715924edfc95b164d2a94cf`.
> 3. Read the script. It only reads container settings, logs, ports, nginx config and certificates, and sends two `GET /api/v1/health` requests to `127.0.0.1:4652` and `127.0.0.1:3524`. It doesn't restart, build, pull or deploy anything, and it doesn't connect to MongoDB.
> 4. Run it as the account that manages these deployments (normally `root`): `bash tamakkan-vps-readonly-checks.sh > tamakkan-stage1-report.txt 2>&1`. Anything your permissions don't allow will be reported as unavailable; please don't use workarounds. Leave the optional `ALLOW_EXEC=1` check off unless you're comfortable with it.
> 5. Review the report, replace anything else sensitive with `[redacted]`, then run this check, which should print `leak check: clean`:
>    `grep -nEi 'mongodb(\+srv)?://|://[^/[:space:]]+:[^@[:space:]]+@|sk-[A-Za-z0-9]{6}|gh[pousr]_[A-Za-z0-9]{6}|BEGIN [A-Z ]*PRIVATE KEY|password|secret=|token=' tamakkan-stage1-report.txt || echo "leak check: clean"`
> 6. Send us `tamakkan-stage1-report.txt` and the filled-in template below. You may then delete `~/tamakkan-check`.
>
> **Please do not:** restart, stop or reload any service or nginx · run builds, pulls, `docker compose up/down` or deploys · rename, move or delete directories, containers, images or volumes · edit `.env` files or secrets · read or change database records · change GitHub or LiveKit/SpatialReal settings.
>
> **Response template:**
> ```
> Script checksum matched: yes/no      Ran as: ____
> Q1 Same VPS for both products: yes/no
>    $SHARED_BACKEND_TAG used by: Tamakkan / Sibling LLM / neither
>    $SHARED_FRONTEND_TAG used by: Tamakkan / Sibling LLM / neither
> Q2 Tamakkan DB name/server type: ____ / ____   Sibling LLM: ____ / ____
>    Same server AND same database: yes/no
> Q3 Same LiveKit project: yes/no/unknown   Agent names: Tamakkan ____ Sibling LLM ____
>    Agents on this VPS: yes(which)/no      Needs LiveKit Cloud owner: yes/no
> Q4 Tamakkan frontend calls port ____ (expect 4652); Sibling LLM frontend ____ (expect 3524)
> Domains / TLS expiry: ____          Rollback images: ____
> Script SUMMARY items: ____          Checks not completed: ____     Redacted: ____
> ```
> Thank you. Nothing needs to be fixed at this stage.
> Best regards, Yousef

## 7. Approvals still required (nothing below has been done)
1. Run `rename-local-folders.ps1` (local): needs you to close VS Code and pause OneDrive.
2. Send the VPS email; review the report; evaluate the gates G-DB, G-FE and G-LK.
3. Plan steps 2 → 12, each separately: manual deploys, branch protection, image isolation, **H1 deploy**, agent-name split, GitHub rename, test stack, directory cutover, branding.
4. Deleting the sensitive local `tamakkan_backend\.env`, and rotating the keys that were shared in chat.
