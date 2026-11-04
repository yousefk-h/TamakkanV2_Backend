# Mariam V2 on the paid LiveKit project - runbook and status

## STATUS (2026-09-29): agent deployed and running; end-to-end blocked by SpatialReal credits

| Item | Value (no secrets) |
|---|---|
| Paid project | `kassel-for-academic-centers` (`p_4w8itjvor7y`, `wss://kassel-for-academic-centers-jb1mccz0.livekit.cloud`) |
| New hosted agent | `CA_8Em9pPqozc5b`, dispatch name `tamakkan-mariam-dev`, region eu-central, Running, worker registered |
| Existing agents (untouched) | `englishteacher` `CA_Xh9JyC6KUd2P`; production V2 `tamakkan-mariam-v2` `CA_LBTsvNPdKaHR` (project `Tamakkan_Agent`) |
| Deployment config | throwaway folder `%TEMP%\tamakkan-agent-paid-dev\livekit.toml`, NOT copied into the repo (`livekit_agent/livekit.toml` still points at production V2 by design) |
| Callbacks | hosted agent -> temporary cloudflared tunnel -> `scripts/local/callback_gateway.py` (127.0.0.1:4674, two paths + dev key only) -> local API. Never the production backend |
| Secrets | agent secrets file `%USERPROFILE%\tamakkan-agent-paid-dev.secrets.env` (outside the repo); local `.env.local-agent` (git-ignored) |
| Provider keys | production V2 OpenAI / Ultravox / SpatialReal keys, reused with the owner's approval on 2026-09-29 |

Verified: dispatch works end to end (session start mints a token, LiveKit dispatches `tamakkan-mariam-dev`, the
hosted worker receives the job). The job then fails while starting the avatar:

    SpatialRealException ... AvatarSDKError: creditsExhausted: WebSocket handshake rejected by server:
    insufficient credits (-17/10) (server code 4001)

The SpatialReal account behind app `app_muli59mj_w70405` is out of credits. The deployed key has the same
fingerprint as production's, so production Mariam sessions are very likely affected as well. Mariam cannot start
without the avatar (no audio-only mode; the agent code was not modified).

To resume: restore credits (or supply a different SpatialReal key/app), update the agent secret with
`lk agent update-secrets --project kassel-for-academic-centers --secrets-file <secrets file>` (run from the throwaway
folder), then run the end-to-end test below.

### 2026-09-29 result of the single cost-capped test (session `79da95a5`)
- Callbacks landed through tunnel + gateway: `mark-connected` 200, `mark-ended` 200 (state `ended`, reason `user_ended`).
- Transcript persisted: 3 turns (Mariam greeting, the student's spoken sentence recognised, Mariam's reply). An
  `insufficient_evidence` report record was written (no Gemini call).
- The SpatialReal avatar participant (`spatialreal-avatar`) joined and published `egress-audio` + `egress-animation`
  tracks (both ACTIVE). The test client's own audio/video measurement had a bug (fixed in `e2e_mariam.py`), so audible
  playback and rendered frames were NOT measured; confirm visually/audibly in the browser.
- Earlier failure cause was the gateway rejecting real (UUID) session ids; fixed and covered by `tests/test_callback_gateway.py`.
- The agent logs `Initial greeting failed; continuing without it` (10 s playout wait times out) in every session; non-fatal.

### Re-running the end-to-end test
```powershell
tamakkan start -Agent ; tamakkan tunnel     # stack + gateway + tunnel (prints the tunnel URL)
# if the tunnel URL changed since the agent was created: re-run scripts\local\agent-setup.ps1 with the new URL,
# then `lk agent update-secrets --project kassel-for-academic-centers --secrets-file <secrets file>`
powershell scripts\local\make-test-speech.ps1          # spoken student answers (Windows TTS)
python -m venv $env:TEMP\e2e\venv ; & $env:TEMP\e2e\venv\Scripts\pip install livekit numpy
& $env:TEMP\e2e\venv\Scripts\python scripts\local\e2e_mariam.py --student student-standard-day4 `
    --wav $env:TEMP\e2e\utt1.wav --wav $env:TEMP\e2e\utt2.wav --wav $env:TEMP\e2e\utt3.wav
```
Checks: room join, agent joined, avatar video frames, Mariam's greeting, a spoken reply per turn, session ended via
callback, transcript persisted, report generated. A fully SCORED report also needs a Gemini key (`GEMINI_API_KEY`,
blank locally): without one, a short conversation yields an `insufficient_evidence` report and a long one yields none.

---

# (Original preparation notes - superseded by the status above)

Status 2026-09-29: **blocked on credentials** - nothing was created or changed in LiveKit Cloud.
No secrets are stored in this file or anywhere in Git.

## What is blocking (exactly)

1. **Paid project not available on this PC.** `lk project list` shows only `tamakkan`
   (`wss://tamakkan-p4wm4qxh.livekit.cloud`) and `Tamakkan_Agent` (production V2). There is no
   "Kassel for Academic Centers" entry, and it cannot be verified which (if either) is the paid one.
   Needed from you: add it once, interactively -
   `lk cloud auth` (browser login), then `lk project list`, or
   `lk project add "Kassel for Academic Centers" --url <wss url> --api-key <key> --api-secret <secret>`
   using a **development API key pair created for this test** in that project's dashboard.
2. **Agent provider secrets.** The hosted agent needs `OPENAI_API_KEY`, `ULTRAVOX_API_KEY`,
   `SPATIALREAL_API_KEY`, `SPATIALREAL_APP_ID` (and avatar id). They exist only in
   `tamakkan-agent-v2/livekit_agent/.env.tamakkan-v2.cloud` (production V2 values) and on the VPS.
   I will not copy production credentials without your explicit instruction; tell me either
   "reuse the V2 provider keys for the dev agent" or provide separate dev keys. (LiveKit and callback
   secrets are always separate dev values.)
3. **Public callback path.** A cloud-hosted agent cannot reach `127.0.0.1`. `cloudflared` is installed
   here. Approval needed to open a temporary public tunnel to the **callback gateway only**
   (see below), for the duration of a test.

## Safety design (already built and tested)

- `scripts/local/callback_gateway.py` listens on 127.0.0.1:4674 and forwards ONLY
  `POST /api/v1/sessions/<id>/mark-connected|mark-ended`, and only with the dev
  `X-Internal-Api-Key`. Everything else is a 404/403 that never reaches the backend. The tunnel points
  here, never at the API itself.
- The dev `AGENT_INTERNAL_API_KEY` is generated locally (in `.env.local-dev`), differs from production,
  and is unknown to production.
- `app/core/local_guard.py` refuses to start the local backend with `Tamakkan_Agent`
  (`tamakkan-agent-m4zc52ik`), the historical shared project, or the agent names `mariam-agent` /
  `tamakkan-mariam-v2`.
- The new agent uses its own dispatch name: **`tamakkan-mariam-dev`** (proposed; change freely, but never reuse an
  existing name). Backend and agent must use the identical value - explicit dispatch means a mismatch
  means no agent ever joins.

**Warning:** this PC's default `lk` project is `Tamakkan_Agent` (production V2). Any `lk agent ...` command
without `--project` acts on production. Do NOT pass `--default` when adding the paid project, and always
pass `--project` explicitly (every command below does).

## Commands (run after the blockers above are cleared)

Never run these from `livekit_agent/` with its existing `livekit.toml` (that file points at production V2).
Work in a throwaway copy so the tracked toml cannot be reused or overwritten:

```powershell
$P   = "Kassel for Academic Centers"             # verify the exact name in `lk project list`
$SRC = "C:\Users\youss\OneDrive\Desktop\Test Tamakkan\worktrees\backend-unified\livekit_agent"
$DST = "$env:TEMP\tamakkan-agent-paid-dev"
Remove-Item -Recurse -Force $DST -ErrorAction SilentlyContinue
Copy-Item $SRC $DST -Recurse -Exclude .venv,.env*,livekit.toml,__pycache__
Set-Location $DST

# 0. Read-only checks: right project, and list existing agents (englishteacher, Axis, ... stay untouched)
lk project list
lk agent list --project $P

# 1. Secrets file OUTSIDE the repo (never committed). LIVEKIT_URL/KEY/SECRET are injected by LiveKit Cloud.
#    LIVEKIT_AGENT_NAME=tamakkan-mariam-dev
#    OPENAI_API_KEY=...  ULTRAVOX_API_KEY=...  SPATIALREAL_API_KEY=...  SPATIALREAL_APP_ID=...  SPATIALREAL_AVATAR_ID=...
#    BACKEND_API_URL=https://<tunnel-host>/api/v1     AGENT_INTERNAL_API_KEY=<dev key from .env.local-dev>
#    MARIAM_IDLE_TIMEOUT_SECONDS=...
$SECRETS = "C:\Users\youss\tamakkan-agent-paid-dev.secrets.env"

# 2. Create a NEW agent (no livekit.toml in $DST, so it cannot target an existing agent id)
lk agent create --project $P --secrets-file $SECRETS --region eu-central .
#    -> writes a new livekit.toml in $DST with the NEW agent id. Record the id (not secret) in
#       docs/tamakkan/ (this file) and NEVER copy that toml over livekit_agent/livekit.toml.

# 3. Verify
lk agent status --project $P     # from $DST
lk agent logs   --project $P     # from $DST
lk agent list   --project $P     # existing agents unchanged: same ids / versions / deployed-at
```

Local side (separate dev LiveKit credentials from the paid project's **development** key pair):

```powershell
# worktrees\backend-unified\.env.local-agent   (git-ignored)
#   LIVEKIT_URL=wss://<paid project>.livekit.cloud
#   LIVEKIT_API_KEY=<dev key>   LIVEKIT_API_SECRET=<dev secret>   LIVEKIT_AGENT_NAME=tamakkan-mariam-dev
python scripts\local\callback_gateway.py --env .tamakkan\local\backend.env      # terminal A
cloudflared tunnel --url http://127.0.0.1:4674                                   # terminal B -> https://<random>.trycloudflare.com
tamakkan start -Agent                                                            # terminal C
```
The web app needs the paid project's SpatialReal App ID at build/run time
(`NEXT_PUBLIC_SPATIALREAL_APP_ID`); set it in the environment before `start -Agent`.

## End-to-end test checklist
Student session start -> see and hear Mariam -> speak -> end -> transcript saved -> report generated.
Check in the local DB (`tamakkan_integration`) that the session reached `connected` then `ended`,
that a transcript exists, and that the report is generated; check `lk agent logs`.

## Production cutover and rollback (DRAFT - do not run without explicit confirmation)

Preconditions: the local end-to-end test above passed; the VPS export shows the `.env` names below; a fresh
backup of `$TAMAKKAN_BACKEND_DIR/.env` and `$TAMAKKAN_FRONTEND_DIR/.env` exists.

Cutover (VPS, backend `.env` then frontend rebuild):
1. `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` -> the paid project's PRODUCTION key pair;
   `LIVEKIT_AGENT_NAME` -> the new production dispatch name (create a production agent in the paid project
   with its own name and secrets, `BACKEND_API_URL=https://tamakkanbackend.tamakkan.online/api/v1`).
2. Frontend: `NEXT_PUBLIC_SPATIALREAL_APP_ID` if it changes (requires an image rebuild).
3. Recreate only the two Tamakkan containers via the existing Compose overrides; health-check both.
4. Smoke: teacher session, then one student session; watch `lk agent logs` and the callbacks.

Rollback (keeps the existing V2 agent running throughout, so this is a config revert):
1. Restore the backed-up `.env` files (`LIVEKIT_*`, `LIVEKIT_AGENT_NAME=tamakkan-mariam-v2` in the `Tamakkan_Agent` project).
2. Recreate the two containers; re-run the health checks. If the frontend image was rebuilt, re-tag the
   preserved `tamakkan-frontend:v2-202609282049` image (see /root/image-backups).
3. Leave the new agent deployed but idle (no dispatch reaches it) or delete it with `lk agent delete`.

Every production step above needs your confirmation first.
