# livekit_agent

The real "Discuss with Mariam" worker (Phase 4) - a **fully separate
deployable** from the FastAPI backend (`app/`), not just a separate process.
It has its own settings (`config.py`) and no database connection of its own
at all. Nothing in this package imports `app.*`: when LiveKit Cloud builds
and deploys an agent, it packages that agent's own directory, so this worker
can only ever rely on what lives under `livekit_agent/`.

It talks to the main backend over plain HTTP, not a shared database (see
"Why no database" below). `app/services/live_session_service.py` mints a
LiveKit access token with this worker's agent name (`mariam-agent`)
explicitly dispatched into the room whenever a student calls
`POST /sessions/start`, embedding everything this worker needs to run the
session (topic instructions, opening line, level) directly into the job's
dispatch metadata. LiveKit then runs `entrypoint()` below for that job, which
reports the session connecting/ending back to the main backend via two small
authenticated HTTP calls (`POST /sessions/{id}/mark-connected` and
`POST /sessions/{id}/mark-ended`).

## Why no database

This worker used to open its own MongoDB connection (via Beanie, sharing the
main backend's database) to read topic content at job start and to persist
every conversation turn and state transition as the call happened. That
connection's own background activity (heartbeats, and especially a full
`LiveSession.save()` - a full-document write of the whole
`transcript_history` array - fired on every single conversation turn) shared
this process's *one* asyncio event loop with the live audio/video session,
and was a real, measured contributor to Mariam's response latency: every
save contended with the loop's ability to service the next audio frame.

The fix was to remove the database entirely, not just optimize the writes:
- **Topic content** (`avatar_prompt`, `opening_line`, `level`) is fetched
  once, backend-side, in `LiveSessionService.start_session`, and handed to
  this worker directly in the job's dispatch metadata - no read of its own,
  at any point.
- **State transitions** (`CONNECTED` on join, `ENDED`/`CONNECTION_FAILED` on
  close) are reported with two short-lived HTTP POSTs
  (`_mark_connected`/`_mark_ended` in `agent.py`), authenticated with a
  shared secret (`AGENT_INTERNAL_API_KEY`, checked by
  `app.core.security.require_internal_agent`) rather than a JWT - this
  worker isn't a `User` and mints none. Both calls are best-effort: a failed
  or slow backend logs a warning and the call continues regardless, since
  the live conversation doesn't depend on either succeeding.
- **Transcript persistence is batched, not per-turn.** `_on_conversation_item_added`
  still only updates the in-memory idle-timeout clock directly, but it also
  appends each turn (student and Mariam both) to a plain in-memory list -
  no I/O, so no event-loop contention. That whole list is sent exactly once,
  as part of the same final `_mark_ended` HTTP call every session already
  makes when it ends - one extra JSON payload on a call that's already
  happening as the session is already tearing down, not a new write path.
  `LiveSessionService.mark_ended` stores it, and (for a normal `ENDED`
  session with a non-empty transcript) kicks off AI scoring
  (`app.core.session_scoring`) in the background.

## Setup

Install this package's own dependencies into **its own virtualenv** (from
`backend/`):

```
python -m venv livekit_agent/.venv
livekit_agent/.venv/Scripts/pip install -r livekit_agent/requirements.txt
```

(For local dev it also runs fine from the main backend's `.venv`, which is a
superset of what's needed - but don't rely on that for anything you intend
to deploy standalone.)

**After bumping a version in `requirements.txt`, verify it actually
installed** - `pip install -r requirements.txt` doesn't always force an
upgrade of a package pulled in as an *extra* (e.g. the `[openai]` in
`livekit-agents[openai]`) if something else already satisfied a looser
constraint at install time. This actually happened here once, with a
different plugin: `livekit-agents` core was correctly at a newer version
while the plugin pulled in via its extra silently stayed on an older one
(visible in the worker's own startup log - `plugin registered {"plugin":
"livekit.plugins....", "version": "..."}`) despite the core package
declaring a `>=` constraint the installed plugin version violated. Confirm
with `pip show` on each plugin package after any dependency bump, not just
the top-level `livekit-agents`.

Copy `livekit_agent/.env.example` to `livekit_agent/.env` and fill in:

- `BACKEND_API_URL` - the main backend's own base URL, including its API
  prefix (e.g. `http://localhost:8000/api/v1`).
- `AGENT_INTERNAL_API_KEY` - a shared secret that must match the main
  backend's own `AGENT_INTERNAL_API_KEY` (`backend/.env.example`).
- `OPENAI_API_KEY` - this worker's primary conversation model (OpenAI's
  Realtime API).
- `ULTRAVOX_API_KEY` - the fallback conversation model, used if OpenAI's
  session dies mid-call (see "Realtime model: OpenAI primary, Ultravox
  fallback" below).
- `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` - from a LiveKit
  Cloud project (the same one the main backend mints tokens against).
- `SPATIALREAL_API_KEY`, `SPATIALREAL_APP_ID` - from a SpatialReal account.
  `SPATIALREAL_AVATAR_ID` already defaults to Mariam's fixed avatar id.

## Running

From `backend/` (an unrelated convention to `python -m app.main`,
just a similarly-shaped command):

```
python -m livekit_agent.agent dev      # local development, connects to LiveKit Cloud, auto-reload
python -m livekit_agent.agent start    # production
```

`dev`/`start` are LiveKit Agents' own server startup modes - see
[Server startup modes](https://docs.livekit.io/agents/server/startup-modes/).

## What it does

1. Reads `session_id`/`max_duration_minutes`/`topic_instructions`/
   `opening_line`/`level` straight from the job's dispatch metadata (set by
   `LiveSessionService.start_session` - see "Why no database" above for why
   this worker never fetches them itself).
2. Builds Mariam's instructions from three layers, in order: the fixed
   baseline guardrail prompt (scope, speech-safe output, injection
   resistance - never shown to or editable by anyone through the UI), a
   speaking-style block chosen by the topic's `Level` (beginner: slow,
   neutral-accent, simple vocabulary; intermediate: normal pace; advanced:
   fast, native-paced British English, richer vocabulary), and finally that
   topic's own `avatar_prompt` (passed in as `topic_instructions`).
3. Starts an `AgentSession` on OpenAI's Realtime API (with Ultravox wired in
   as an automatic fallback - see "Realtime model: OpenAI primary, Ultravox
   fallback" below) and a SpatialReal `AvatarSession`, reporting `CONNECTED`
   back to the main backend over HTTP once both are up (`_mark_connected`).
4. Tracks conversation activity (for the idle timeout below) and buffers each
   turn in memory, for the batched transcript sent at the end (see "Why no
   database").
5. Enforces the student's own session-duration cap (passed in via job
   metadata) and an idle timeout (`MARIAM_IDLE_TIMEOUT_SECONDS`), and
   reports the finished session (`end_reason`, plus the buffered transcript)
   back to the main backend over HTTP on close, however it ends
   (`_mark_ended`). `entrypoint()` blocks until that report has been
   attempted - it must not return while the
   session is still live, since returning ends this job's own resources.

`MariamAgent` (the conversational persona/instructions) is defined
separately from `entrypoint()` specifically so it can be constructed and
exercised in `tests/test_agent.py` via LiveKit's own test framework, without
a real room, LiveKit Cloud, or SpatialReal account.

## SpatialReal: the new platform (migrated from AvatarKit)

Avatar Practice runs on SpatialReal's **new** SDK platform
([migration guide](https://docs.spatialreal.ai/resources/migration-guide)); the legacy AvatarKit / `spatialwalk.cloud`
integration is gone, and new apps and API keys (from the SpatialReal Studio) only work on the new platform.

- **This worker** - `livekit-plugins-spatialreal==1.8.4` (runs on the `spatialreal` Python SDK, talks to
  `api.spatialreal.com`). The agent code is unchanged (`spatialreal.AvatarSession()` + `avatar.start(session, room=...)`);
  the plugin connects on its own, so **no endpoint override** (`SPATIALREAL_CONSOLE_ENDPOINT`,
  `SPATIALREAL_INGRESS_ENDPOINT`) may be set. The plugin only requires `livekit-agents>=1.6.6`, so the rest of the stack
  (LiveKit Agents, OpenAI Realtime, Ultravox, Krisp) is untouched.
- **The browser** (`frontend/lib/spatialreal/`, `MariamSession.tsx`) - `@spatialreal/web-sdk` with `livekit-client >= 2.19`: one
  `createSession({ avatarId, credential, container, livekit })` call, `start()` from the student's click. The SDK plays
  Mariam's voice itself, so the room's audio tracks are never attached anywhere else.
- **Tokens** - the browser never sees the SpatialReal API key. The API (`POST /sessions/{id}/avatar-token`, the student's own
  open Avatar session only) exchanges the key for a short-lived session token and returns it with the public App ID /
  Avatar ID, so the browser and this worker are always configured from the same app.
- **Credentials** - `SPATIALREAL_API_KEY`, `SPATIALREAL_APP_ID` and `SPATIALREAL_AVATAR_ID` are ONE matched set (an App ID only
  works with its own app's key and avatars). Check a key without starting a session:
  `POST https://api.spatialreal.com/v1/auth/session-token` with `X-API-KEY` and `{"expire_at": <now + 3600>}`.

## Response latency

If Mariam feels slow to respond: **deployment geography is the main lever,
and it's not a code fix, it's a deployment decision.** In production,
LiveKit recommends deploying agents onto LiveKit Cloud's own agent hosting,
colocated with the same region as the *LiveKit Cloud project itself* (check
your project's actual configured region - it's what every hop to LiveKit's
media/SFU infrastructure has to reach, regardless of where the main backend
happens to be deployed). Every hop this worker makes to LiveKit Cloud,
OpenAI/Ultravox, and SpatialReal's driving service happens over the public
internet, on top of the model's own response time - proximity to the
*backend* doesn't help this at all, since this worker's only calls to it
(`_mark_connected`/`_mark_ended`) are best-effort and off the conversational
critical path (see "Why no database" above).

## Scripted announcements were simplified (time-warning and farewell removed; opening kept minimal)

`agent.py` used to force Mariam to speak at three specific moments: an
explicit `generate_reply()` call to make her greet first with a suggested
opening line (wrapped in mic-hold signaling and server-side audio-muting so
nothing could interrupt it), a mid-session "we're almost out of time"
warning, and a farewell line before closing.

The time-warning and farewell were removed entirely, in favor of keeping
this worker simple - the session now just ends (`agent_session.aclose()` +
deleting the room) with no spoken announcement when time runs out or the
student goes idle.

**The opening greeting could not be removed the same way - a realtime model
still needs one explicit `generate_reply()` nudge to ever start talking at
all.** This was tried (removing every explicit trigger, relying purely on
the model's own "server-side turn detection auto-responds once it detects
end-of-turn" behavior) and confirmed live: Mariam never spoke a single word,
in either direction, for the whole session - not just no opening greeting,
no response to the student either. Confirmed against LiveKit's own
quickstart template (`/agents/start/voice-ai`), which calls
`session.generate_reply(instructions="Greet the user and offer your
assistance.")` unconditionally right after `session.start()`, for *every*
agent template they ship, including realtime models - this isn't optional
scaffolding, it's the standard required kickoff. `agent.py` now makes this
one plain call after the student joins - no mic-hold, no audio-muting, no
forced exact wording, just the same bare nudge LiveKit's own docs use. The
opening line itself still lives *only* as guidance in the system prompt
(`MARIAM_BASE_SYSTEM_PROMPT`'s "Starting the conversation" section,
`prompts.py`) - the `generate_reply()` call doesn't reference it directly,
Mariam just uses it naturally because it's already in her instructions.

If a spoken heads-up before time runs out turns out to matter in practice,
reintroducing it needs real care: `generate_reply(instructions="Say
exactly...")` is documented by LiveKit as not guaranteed to follow a script
on a realtime model, and a fair amount was learned the hard way (on Gemini
Live, since removed - see git history around this file for the full
writeup) about how little control `allow_interruptions` actually gives you
over a realtime model's own turn detection. Re-litigating that isn't
necessary while this worker has no scripted lines left to protect, but
don't assume `allow_interruptions=False` will reliably stop a realtime
model from talking over a scripted line if this comes back - verify it live
against whichever provider is active at the time.

## Silent ~10s gap right after connecting

Separate issue, unrelated to the above and still fixed: `agent_session.start()`
is what kicks off the realtime model's WebSocket connect, but that connect
runs in a background task the call itself doesn't await, so `.start()`
returns almost immediately while the actual handshake finishes a few seconds
later. The code used to call `agent_session.start()` only *after*
`avatar.wait_for_join()` (SpatialReal's own join wait, which can take up to
~30s), so that ~10s handshake landed as extra dead air on top of the avatar
wait instead of inside it.

Fix: `avatar.wait_for_join()` and `agent_session.start()` now run
concurrently (`asyncio.gather`) instead of sequentially, so the connect time
overlaps the avatar's own join wait instead of adding to it in serial.
`avatar.start()` itself still runs first and is still awaited on its own -
it wires the session's audio output to the avatar and needs to be in place
before the session can start producing audio, so it can't be folded into
the same `gather`.

## Realtime model: OpenAI primary, Ultravox fallback

Gemini Live was this worker's first realtime model, and was fully removed
after a long run of provider-specific interruption/silence bugs (see git
history for the full saga, mostly in the now-superseded sections above).
OpenAI's Realtime API is primary now, with Ultravox wired in as an automatic
fallback via LiveKit's own `livekit.agents.llm.RealtimeModelFallbackAdapter`
- if OpenAI's session dies with an unrecoverable error, the adapter swaps to
Ultravox transparently (preserving chat context, re-issuing an interrupted
reply) instead of ending the call.

Two things worth knowing if this is touched again:

- **Order matters.** `RealtimeModelFallbackAdapter(models=[...])` - the
  first entry is primary, the rest are fallbacks tried in order.
- **The two models don't have the same instruction-mutability capability.**
  Confirmed via `RealtimeModel(...).capabilities` on the installed 1.7.1
  plugins: OpenAI's `RealtimeSession` has `mutable_instructions=True` (it
  accepts instructions pushed dynamically by `AgentSession.start()`, same as
  Gemini did), but Ultravox's has `mutable_instructions=False` - it needs
  its full system prompt baked into `system_prompt=` at construction time,
  before any session starts. `agent.py` builds `mariam_agent` up front and
  passes `mariam_agent.instructions` into the Ultravox model's constructor
  specifically for this reason - if a fallback swap ever lands on Ultravox,
  the persona is already correct rather than falling back to its default
  "You are a helpful assistant."
- The adapter also requires every wrapped model to agree on `audio_output`
  and `turn_detection` (it raises `ValueError` at construction otherwise) -
  both OpenAI's and Ultravox's realtime models report the same values for
  both, so this hasn't been an issue, but it's worth checking again if a
  third provider ever gets added to the `models=[...]` list.

## Deploying to LiveKit Cloud

Resolves the "deployment geography" factor above. Run every command from
**this directory** (`livekit_agent/`), not `backend/` - the build
context must stay scoped to this package alone, same reasoning as everything
else in this README about it being a separate deployable. `Dockerfile`/
`.dockerignore` here are already set up for this (the Dockerfile copies
itself into `/app/livekit_agent/` rather than flattening to `/app/`,
specifically so this package's existing `from livekit_agent.config import
...`-style absolute imports keep working unchanged inside the container -
no source changes needed for deployment).

Prerequisites: the [LiveKit CLI](https://docs.livekit.io/reference/developer-tools/livekit-cli/)
(`lk`) installed, and the main backend already deployed and reachable at a
real URL (`BACKEND_API_URL` below has to point at it).

```bash
cd backend/livekit_agent

lk cloud auth                      # links this machine to your LiveKit Cloud account
lk project set-default "<project-name>"

lk agent create                    # first deploy - registers the agent, writes livekit.toml
```

`lk agent create` prompts to load secrets from an env file (`.env`,
`.env.local`, or `.env.production` if present) - it automatically strips out
`LIVEKIT_URL`/`LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` even if they're in that
file, since **LiveKit Cloud injects those three itself at runtime; they must
never be set as secrets here.** Everything else in `.env.example` should be
a real secret for the deploy:

- `BACKEND_API_URL` - the main backend's real public URL once it's deployed
  (e.g. `https://api.yourdomain.com/api/v1`), not `localhost`.
- `AGENT_INTERNAL_API_KEY` - must match that backend's own value.
- `OPENAI_API_KEY`, `ULTRAVOX_API_KEY`, `SPATIALREAL_API_KEY`,
  `SPATIALREAL_APP_ID`.

To set secrets explicitly instead of (or in addition to) the env-file
prompt:

```bash
lk agent create --secrets-file=.env.production
# or, one at a time:
lk agent update-secrets --secrets "BACKEND_API_URL=https://api.yourdomain.com/api/v1"
```

After the first deploy, ship every subsequent change with:

```bash
lk agent deploy
```

(a rolling deploy - old instances keep serving active calls for up to an
hour while new ones take over new sessions). Useful follow-ups:

```bash
lk agent status     # replica count, health
lk agent logs       # live tail
lk agent rollback    # instant revert to the previous version (paid plans)
```

`AGENT_NAME = "mariam-agent"` in `agent.py` is what the main backend's
`LiveSessionService.start_session` dispatches against - deploying preserves
it automatically, no config to update on the backend side when this worker
moves from a local dev process to LiveKit Cloud.

## Out of scope for this phase

An admin-facing "force end" button (the backend route exists -
`POST /sessions/{id}/force-end` - but no admin screen calls it yet) and
picking/paying for a different avatar vendor are deliberately not part of
this build - see the plan doc's "Out of scope" section for the full list.
(AI scoring of a finished session's transcript *is* now built - see
`app.core.session_scoring` and `LiveSessionService.generate_report`.)
