# Tamakkan backend

FastAPI backend for Tamakkan, the AI-driven English-speaking practice platform.

## Structure

```
app/
  core/
    application.py   FastAPI app factory - CORS, lifespan/DB, exception handlers, routers
    config.py         settings (env-driven)
    database.py       Mongo connection + Beanie init
    exceptions.py     custom exceptions -> HTTP response mapping
    security.py       password hashing, JWT, get_current_user/require_role guards
    email.py          EmailSender interface - logging-only impl, no provider chosen yet
    storage.py        AudioStorage interface - Cloudinary impl for recording submissions
    video_processing.py  CaptionsFetcher/AudioExtractor interfaces - YouTube captions + yt-dlp/ffmpeg
    transcription.py  Transcriber interface - Gemini impl for the transcript pipeline's fallback path
  models/
    base.py           BaseDocument: UUID4 `id` (stored as str), created_at/updated_at, is_deleted
    user.py, ...       one file per Mongo collection, each `class X(BaseDocument)`
  schema/
    base.py           BaseSchema: id/created_at/updated_at, mirrors BaseDocument
    user.py, ...       Pydantic request/response schemas - never expose a Document directly
  repositories/
    base_repository.py  BaseRepository[T]: get_by_id, save (Beanie's own upsert)
    user_repository.py, ...  one class per collection, `class XRepository(BaseRepository[X])`
  services/
    auth_service.py, ...  one class per concern, repositories injected via `__init__`;
                          a `get_x_service()` factory at the bottom of each module is
                          the FastAPI `Depends()` wiring - routes never construct these directly
  api/
    v1/          version 1 route modules (one file per resource)
    api.py       registers every version's routers onto one router
  jobs/          background/scheduled jobs (FastAPI BackgroundTasks for now)
  main.py        container/production entry point - see below

livekit_agent/   LiveKit Agents worker (separate process) - not started yet
```

Dependency direction: `api` → `services` → `repositories` → `models`. `core` has
no dependencies on any of the above. A route handler never touches Beanie or a
repository directly, and a service never imports a FastAPI type. A service
holds its repositories as instance attributes (set once in `__init__`, not
re-constructed per method) - route handlers get a service instance via
`Depends(get_x_service)`, never by instantiating one inline.

Every model's `id` is a UUID4, generated with `uuid4()` but stored/typed as
`str` (not Mongo's default ObjectId, and not a native BSON UUID either -
native BSON UUIDs need an explicit `uuid_representation` and are a long-
standing cross-driver footgun; storing the string form sidesteps that
entirely). See `app/models/base.py` for the full reasoning.

The app itself is built in `app.core.application` (the `create_app()` factory
+ the module-level `app` instance), not in `main.py`. `main.py` does exactly
one thing - `uvicorn.run(app)` - and is what the Dockerfile's `CMD` will
invoke. **Never `import app.main` from a test or another module** - it runs
the server as a side effect of import, with no `__main__` guard. Always import
`app.core.application` instead.

## Accounts, roles & password resets

Three roles, strictly hierarchical in *who creates whom*, but **flat in
permission checks** - `require_role(Role.ADMINISTRATOR)` does not also accept
`Role.ROOT`. That's deliberate: Root does not inherit Administrator's
endpoints. If Root needs to do something an Administrator does, that's a
separate ask, not something to build speculatively.

| Role | Creates | Password reset |
|---|---|---|
| **Root** | Administrators (`POST /root/admins`) - seeded once via `python -m app.jobs.seed_root`, no self-signup | Self-service (`/me/change-password`, or the emailed-token flow) |
| **Administrator** (the Instructor) | Students (`POST /users`) | Self-service, or reset by Root (`POST /root/admins/{id}/reset-password`) if locked out |
| **Student** | Nobody | **Cannot self-service at all** - not `/me/change-password` (403), not the emailed-link forgot-password flow either (silently refused, same generic response as an unknown email, so the endpoint can't be used to fingerprint which accounts are students). Must call `POST /me/request-password-reset`, which notifies their instructor; the instructor resolves it via `POST /users/{id}/reset-password`. |

Root has no dashboard concept - it's reachable only via the API/Swagger UI,
by design, not as a stopgap.

**An Administrator may only reset a student they created.** The role guard on
the route is not sufficient on its own - student ids appear in `UserPublic`
responses, so without an ownership check any instructor could take over any
student on the platform. `User.created_by_id` is what that check reads: it
records who created each account (Root for an Administrator, an Administrator
for a Student), and also routes a student's reset request to the right
instructor. A student with no `created_by_id` (seeded outside the normal
create flow) can't be reset by anyone until one is set - deliberately refused
rather than treated as belonging to everybody.

`created_by_id` is a plain indexed string, **not** a `Link[User]`, on purpose:
Beanie Links can't be queried by equality, and `Link.fetch()` returns the Link
itself rather than `None` when the target is gone, so `if x is None` guards on
a fetched Link are dead code. `RefreshToken.user_id` and
`PasswordResetToken.user_id` exist for the same reason.

**Every password change revokes that account's refresh tokens** (change,
admin reset, root reset, emailed-token reset). A reset that leaves existing
sessions alive doesn't lock anyone out, which defeats the point of resetting.

No real email provider is wired up yet (`app/core/email.py`'s
`LoggingEmailSender` just logs what would be sent) - the platform plan's own
open questions flag notification channels as unresolved. Swapping in a real
provider is a new class behind the existing `EmailSender` interface, not a
redesign of anything that calls it. Note the reset **token** is only ever
available at creation time (only its hash is stored), so `AuthService` hands
it to the `EmailSender` immediately - there is no way to recover it later.

## Topic content (Kanban card #9)

CRUD behind `/topics` - `POST`/`PATCH`/`DELETE` are Administrator-only (not
Root: content authoring is Instructor territory, and Root's scope is
deliberately narrow); Administrator/Root reads are unfiltered. Both
`GET /topics` (the list) and `GET /topics/{id}` are entitlement-gated for
Students - see cards #14/#15, below.

The create/update schema (`app/schema/topic.py`) deliberately excludes
`transcript_state`, `raw_transcript`, and `approved_transcript` - those are
owned by the transcript pipeline (card #11), not admin-authored content, and
exposing them here would let a plain content edit corrupt that state machine
by hand.

This is what makes `POST /submissions` (below) actually usable - before this,
the only way to get a `topic_id` to submit against was inserting a
`TopicContent` directly in a test.

**Three response shapes**, because "what a topic looks like" depends on who is
asking and whether it's a list:

| Shape | Used by | Carries |
| --- | --- | --- |
| `TopicListItem` | `GET /topics` | id/timestamps, title, level, `youtube_urls`, `transcript_state`. **No transcript, for anyone.** |
| `TopicPublic` (`include_internal=False`) | `GET /topics/{id}` as a Student | the lesson content. No `avatar_prompt`, no transcript, no `transcript_state`. |
| `TopicPublic` (`include_internal=True`) | every Administrator/Root route | everything. |

`GET /topics` is **paged** (`limit`, default 50, max 200; `skip`) and sorted
newest-first. Both the paging and the slim list shape are load-bearing rather
than cosmetic: a list row used to serialize `raw_transcript` (~400 segments,
~40 KB per topic for an 18-minute video) *and* `approved_transcript`, so a
few hundred topics made this endpoint a multi-megabyte response that also built
tens of thousands of `TranscriptSegment` objects per request. A Student's
entitlement is applied as a Mongo filter (`TopicContentRepository
.list_for_student`), not as a post-filter over the whole catalogue, so a page
counts entitled topics rather than thinning unpredictably.

## Plan & entitlement enforcement (Kanban cards #14/#15)

Two Administrator-only, ownership-checked endpoints on `/users`, plus a
server-side gate on both `GET /topics/{topic_id}` (card #14) and `GET
/topics` (card #15's backend slice - the *list*):

- **`POST /users/{student_id}/assign-plan`** - Standard sets one
  `assigned_level` (the student's group gets that whole level's catalogue);
  Pro sets a hand-picked `assigned_topic_ids` list spanning any level. One
  schema (`AssignPlan`), not two endpoints - the two shapes are mutually
  exclusive by design (a `model_validator` rejects mixing them), and every
  Pro topic id is checked to actually exist.
- **`POST /users/{student_id}/raise-session-limit`** - Pro only (Standard's
  15-minute cap is fixed platform-wide, not per-student); sets
  `duration_override_minutes`, capped at
  `MAX_SESSION_DURATION_OVERRIDE_MINUTES` (180, so a fat-fingered value can't
  become a de facto unlimited session) and **always writes an `AuditLog`
  entry** - the card is explicit this is "an explicit admin action," and it's
  the first thing in this codebase that actually writes to `AuditLog` (the
  model existed since card #1 with nothing calling it - see its own docstring).
- **`GET /topics/{topic_id}`** rejects a Student outside their entitlement:
  Standard needs `topic.level == assigned_level`; Pro needs `topic.id in
  assigned_topic_ids`; no plan assigned yet means no access at all. This is
  card #14's literal done-when: an Intermediate/Standard student can no
  longer open an Advanced lesson by id.
- **`GET /topics`** (the list) applies the identical rule
  (`TopicContentService.list_for_requester`, reusing the same
  `_student_is_entitled` check rather than re-deriving it) - a Standard
  student's list contains only their assigned level; a Pro student's list
  contains only their hand-picked topics; a student with no plan sees an
  empty list. This is card #15's backend slice - the card's own UI half
  (removing the level-picker so there's no path to even *attempt* viewing
  other content) is frontend work in the original UI source repository, not here.

Administrators and Root are never gated on either endpoint - entitlement is
a Student concept, not a content-management restriction.

**`User.assigned_topics` (a `list[Link[TopicContent]]`) became
`assigned_topic_ids` (`list[str]`)** while building card #14 - same reasoning
as `created_by_id`: a Link can't be checked for containment reliably, and the
old field had never been read or written by any code path, so there was no
existing behavior to preserve by keeping it.

**What's still missing, on purpose**: card #14 also asks to reject a
session-start call outside entitlement "before any LiveKit room is
created" - there is no session-start endpoint yet (that's Lane F, not
started), so nothing to gate. That check lands with the LiveKit work, not
here. Likewise, the prompt-assembly service (card #13) is deferred to Lane F
for the same reason - it depends on card #11 *and* the LiveKit session
context that doesn't exist yet.

## Transcript pipeline (Kanban card #11)

`POST /topics/{id}/generate-transcript` (Administrator-only, 202) fetches
YouTube captions for `topic.youtube_urls[0]` via `youtube-transcript-api`
first; if the video has none, it falls back to downloading audio with
`yt-dlp` and transcribing it with Gemini (`gemini-3.5-flash`, via the
Interactions API - `client.aio.interactions.create`, not the older
`generateContent` call, and newer than this codebase's own training-data
cutoff; verified against the live docs and the installed SDK's real method
signatures, not written from memory).

- **Only the first URL is processed.** `TopicContent.youtube_urls` allows
  more than one, but stitching several videos' transcripts into one document
  for a single topic wasn't asked for, and it's not obviously even
  meaningful - a scope decision, not an oversight.
- **Two-phase, on purpose**: `start_generation` is synchronous - it validates
  and moves `transcript_state` to `extracting` immediately, so a caller in
  the wrong state (already extracting, or regenerating an approved
  transcript) gets a real 409 in the response. `run_generation` is the slow
  part (minutes of network calls), run via FastAPI `BackgroundTasks` - it has
  no HTTP caller left to report a failure to, so it catches its own errors,
  logs them, and reverts `transcript_state` to `queued` instead of raising.
- **Long audio is chunked** above `TRANSCRIPT_CHUNK_THRESHOLD_SECONDS`
  (default 600s), into overlapping pieces (`TRANSCRIPT_CHUNK_OVERLAP_SECONDS`,
  default 10s) cut via `ffmpeg -c copy` (stream copy, no re-encode). The
  overlap exists specifically because `-ss` before `-i` seeks fast but can
  only cut cleanly at a keyframe for some codecs - a few imprecise seconds at
  each edge are expected, not a bug. Each chunk is transcribed and retried
  independently (3 attempts); if a chunk never recovers, the **whole**
  transcript fails and `transcript_state` reverts to `queued` - a silent
  half-transcript would poison Maya's context, per the card's own reasoning.
  Lesson videos run 15-20 minutes, so the default threshold deliberately
  chunks a typical video, not just a hypothetical long one - the chunking
  path is meant to actually run in normal use, not sit untested.
- **The review queue is `GET /topics?transcript_state=pending_review`** -
  not a separate endpoint. `POST /topics/{id}/approve-transcript`
  (Administrator-only) moves `pending_review` -> `approved_for_ai` and joins
  `raw_transcript`'s segment texts into `approved_transcript`.
  `approved_for_ai` is terminal (from card #1's own state machine, not a
  change made here) - there is no API path back down, so a later
  prompt-assembly step (card #13) can trust that once approved, a transcript
  won't change under it.
- **Needs `ffmpeg` and a Gemini API key to actually run.** `ffmpeg` is
  already planned as a Docker dependency (card #36); locally, install it and
  put it on `PATH`. Real YouTube caption-fetching and a real `yt-dlp`
  download were both verified against an actual public video during
  development (see `app/core/video_processing.py`'s docstring) - only the
  Gemini transcription call and the `ffmpeg` chunk-cut itself are unverified
  end-to-end in this environment specifically (no ffmpeg, no Gemini
  credentials here); the request *shape* to Gemini was confirmed by sending
  one with a deliberately invalid key and getting a real structured error
  back from Google's servers, not a client-side failure.
- **`yt-dlp` format selection matters for reliability**: `bestaudio[ext=m4a]`
  is used, not the generic `bestaudio/best` - the generic form picked a
  stream that came back `403 Forbidden` against a real video during
  development (likely needing the JS-runtime-based signature deciphering
  yt-dlp warns about without one installed); the explicit `m4a` request
  avoided it. Worth knowing if extraction starts failing against future
  videos - yt-dlp's needs here can shift with YouTube's own anti-bot changes.

## Recording submissions (Kanban card #6, scoped to audio only)

`POST /submissions` (Student-only, `multipart/form-data`: `topic_id` + `file`)
lets a student submit an audio recording for a lesson topic. Scope, as agreed
- this is **not** the full card #6: no Friday-video check-in submission, no
`upload_large()` chunked upload (see below), no submissions-viewing endpoints
(that's card #8).

- **Hard 2-minute cap**, enforced server-side against Cloudinary's own
  reported `duration` once the file is uploaded - not before. This endpoint
  only ever receives a completed file (an HTTP upload, not a live stream), so
  the server can reject an over-length recording but can't stop a client
  mid-recording; that has to be a client-side timer on the recorder itself.
  An over-limit upload is deleted from Cloudinary immediately (no orphaned
  asset) and no `RecordingSubmission` is written.
- **No `upload_large()`.** Chunked upload only matters above 100MB; two
  minutes of audio is a few MB even uncompressed. Using it here would be
  complexity with nothing behind it.
- **Cloudinary `public_id` is `Tamakkan/{student}/{topic}-{timestamp}-{short
  id}`** (`_build_public_id` in `recording_submission_service.py`), not a raw
  UUID - findable by student, then by topic, directly in the Cloudinary
  console. Both name components are slugified (`_slugify`): NFKD-normalized
  so accented Latin folds to its plain letter ("Écoute" -> "ecoute") instead
  of losing the character entirely, falling back to `"untitled"` if nothing
  ASCII survives (a name in a non-Latin script, e.g. Arabic, has no ASCII
  transliteration - a real, accepted limitation, not a bug, since
  localization is deferred platform-wide). The trailing random suffix is not
  cosmetic: Cloudinary overwrites silently on a duplicate `public_id`, so
  readability never gets to compromise uniqueness.
- **Upload retries 3 times**, no backoff between attempts (kept simple; add
  one if real-world failures turn out to cluster). Exhausting retries is a
  502, not a 500 - the request was fine, Cloudinary wasn't.
- **Resubmitting for the same `(student, topic)` pair** creates a new
  `RecordingSubmission` linked via `replaces` to the previous one, rather than
  overwriting it - review history isn't lost. `RecordingSubmissionRepository
  .get_latest_for_student_and_topic()` is what any UI should call to find the
  current one; it's just "latest by `submitted_at`," since a resubmission is
  always later than what it replaces.
- **7-day cleanup is a standalone script**
  (`python -m app.jobs.cleanup_expired_recordings`), not an in-process
  scheduler - this project defers Celery/APScheduler until actually needed.
  It needs an external cron (or a platform's scheduled-task feature) to
  actually run on a schedule. It deletes the Cloudinary asset and sets
  `media_deleted=True`; the document itself, and its `reviewed` status, stay
  queryable forever.
- **Not independently verified against a real Cloudinary account** - there
  are no test credentials for one in this environment. `AudioStorage` is a
  `Protocol` specifically so the whole pipeline (validation, retry, replace
  semantics, cleanup) is tested behind a fake; only the real
  `CloudinaryAudioStorage.upload()`/`.delete()` calls themselves are unverified
  until `CLOUDINARY_*` credentials are set and a real submission is tried.

### Submissions views (Kanban card #8)

Three read endpoints, all in `app/api/v1/submissions.py`:

- `GET /submissions/me` (Student) - the caller's own full submission history
  (not just the current one per topic - a resubmission's `replaces` link
  means history is recoverable from a full list but not the other way
  around; the frontend groups by `topic_id` for display).
- `GET /submissions/{id}` - ownership-checked single fetch. **Scoped to the
  owning Administrator, not any Administrator** - the card text says "an
  Administrator" can play a submission, but that's the exact cross-tenant
  shape of the IDOR already fixed for password resets (any admin acting on
  any student via an id they can see), so this deliberately narrows it.
- `GET /submissions` (Administrator) - scoped to the caller's own students
  only (never platform-wide), with `student_id` / `topic_id` / `reviewed`
  query filters behind the frontend's per-student view and filter chips.
  Passing a `student_id` that isn't the caller's own is a 403, not an empty
  list.

**No dedicated unreviewed-count endpoint.** The done-when only requires the
count to match an independent count of `reviewed=false` records - the
frontend gets that by counting the same list `GET /submissions` already
returns, so a second endpoint returning just a number would be one more thing
that could drift from the list instead of provably matching it.

**No pagination on any of these**, consistent with every other list endpoint
in this codebase (`GET /users` has none either) - add it if a real roster
size makes it necessary, not pre-emptively.

## Security invariants

Things that look like details but are load-bearing. Each has a regression test
in `tests/api/test_security_regressions.py`.

- **`JWT_SECRET_KEY` must be set outside development.** `Settings` refuses to
  construct when `ENVIRONMENT != "development"` and the key is still the
  placeholder from `.env.example` - otherwise a deploy that forgets the env var
  boots looking perfectly healthy while signing tokens with a key published in
  this repo.
- **Access tokens must carry `exp` and `sub`.** python-jose validates `exp`
  when present but doesn't require it, so a signed token that simply omits it
  would never expire. (The `options={"require": [...]}` list form is silently
  ignored by python-jose 3.5.0 - only the per-claim `require_exp` /
  `require_sub` flags work.)
- **Passwords are validated on UTF-8 byte length, not character length**
  (`app/schema/password.py`). bcrypt's limit is 72 bytes; Pydantic's
  `max_length` counts characters, so a 72-character Arabic or emoji password
  passed validation and then made bcrypt raise. That surfaced as a 500 - and
  because login only reaches bcrypt for accounts that exist, the 500-vs-401
  difference was a perfect account-enumeration oracle.
- **Login spends the same time on a miss as on a hit.** The miss path runs a
  dummy bcrypt verify; skipping it returned in ~4ms versus ~240ms for a real
  account, which leaks account existence just as plainly as a distinct error
  message would.
- **A `DuplicateKeyError` is a 409, not a 500.** Services pre-check where they
  can, but that check can't see soft-deleted rows and can't be atomic against a
  concurrent request, so the unique index is the real guarantee.

### From the post-implementation review

A full security/correctness/performance pass over everything above turned up
the following. Each has a regression test; several were reproduced with a
failing test first.

- **Request bodies are capped before FastAPI parses them**
  (`app/core/middleware.py`). FastAPI reads the body (`await request.form()` /
  `await request.body()`) *before* `solve_dependencies()` runs, and Starlette
  caps non-file form fields but not file parts - so an unauthenticated caller's
  multi-gigabyte upload was spooled to disk before the 401. Two guards:
  `Content-Length` up front, and a byte counter on the receive channel because
  `Content-Length` is client-supplied and absent on a chunked request. The
  overflow is signalled as `HTTPException(413)` specifically because FastAPI
  wraps body-parsing errors into a 400 but re-raises `HTTPException` untouched.
- **bcrypt runs off the event loop** (`hash_password_async` and friends).
  bcrypt is ~240ms of blocking CPU, and it was called straight from `async def`
  services - so every login stalled the whole worker, and the deliberate
  constant-time dummy verify on the miss path made that an *unauthenticated*
  amplifier rather than a defence. The sync forms remain for jobs and tests.
- **Entitlement is enforced on the write path too**, from one definition
  (`app/core/entitlement.py`). Cards #14/#15 gated reading a topic;
  `POST /submissions` checked only that the topic existed, so a Student with no
  plan at all could submit against any topic and get its title back in `label`.
  Two copies of one rule is how that happens, so there is now one.
- **Recording audio is stored privately.** Uploads use Cloudinary's
  `authenticated` delivery type, not the default `upload`, and responses carry a
  freshly signed URL. Previously the ownership checks on `GET /submissions/{id}`
  protected the *pointer* while the asset itself was publicly fetchable by
  anyone with the URL - and the URL embedded the student's real name.
  `media_url` is now empty once the cleanup job has removed the media, instead
  of a link to a deleted asset. (True time-limited expiry needs Cloudinary's
  auth-token add-on; the signature is what closes the public-access hole.)
- **The transcript pipeline never writes a whole stale document.** All writes
  are scoped `$set`s, and state changes are compare-and-set
  (`TopicContentRepository.transition_state`). A whole-document save from a
  snapshot held across minutes of network calls could revert a concurrent edit
  and - because the transition was validated against the *snapshot's* state -
  move an `approved_for_ai` topic back to `pending_review` and null the
  `approved_transcript`, defeating a state the transition table calls terminal.
  A run whose topic has moved on now matches nothing and is discarded.
- **`run_generation` catches `Exception`, and `extracting` is reclaimable.**
  Catching only `TranscriptGenerationFailed` left the topic in `extracting`
  forever on anything else - a missing `GEMINI_API_KEY` (`ValueError` from the
  SDK) bricked every audio-fallback topic on a fresh deploy - and
  `TRANSCRIPT_TRANSITIONS` has no `extracting -> extracting` edge, so there was
  no API path back. `transcript_state_started_at` plus
  `TRANSCRIPT_EXTRACTION_TIMEOUT_MINUTES` makes an abandoned run recognisable,
  which also covers process death and redeploys.
- **Approving an empty transcript is refused.** `approved_for_ai` is terminal,
  so it would have permanently handed the AI an empty transcript.
- **Chunk settings are validated at startup**, and before the single-chunk
  early return. A bad pair used to transcribe short videos happily and fail only
  on long ones, surfacing as a topic stuck in `extracting`; a negative overlap
  silently *skipped* audio between chunks rather than overlapping it.
- **ffmpeg has a timeout and no stdin.** It runs in the threadpool, so a hang
  was uncancellable and held one of ~40 shared tokens for the process lifetime -
  the same pool every Cloudinary upload and yt-dlp download draws from. Chunk
  files are also written inside the run's `TemporaryDirectory` now, so a SIGKILL
  leaks nothing.
- **An upload that is never recorded is deleted.** Cloudinary assets are found
  for cleanup by iterating database rows, so an asset whose row was never
  written could never be reached. Every failure path after the upload now
  removes it, and the compensating delete swallows its own error so a Cloudinary
  blip can't turn a clear 422 into an opaque 500.
- **Upstream error text stays server-side.** The raw Cloudinary exception
  string - naming the cloud, the endpoint, and connection-pool internals - was
  returned verbatim in a 502 body.
- **User-supplied strings and lists are bounded** (`app/schema/limits.py`).
  `full_name` is the one that mattered: Student-writable via `PATCH /me`, and it
  leaves the database into an email body and a Cloudinary storage key.
- **`youtube_urls` must actually be YouTube URLs.** The stored string is handed
  verbatim to yt-dlp. SSRF was blocked only *accidentally*, by a hostname check
  living in the captions fetcher that happens to run first; the allowlist is now
  the field's own contract, and it checks the scheme too (`urlparse` fills in
  `hostname` for `file://` as well).
- **A student with no owning Administrator notifies nobody.** The reset-request
  path used to fan out to every Administrator on the platform, emailing them the
  student's name and address, while `resolve` already refused an unowned student
  outright. Two halves of one ownership rule disagreeing is what produced it.

**Kanban card #4 (instructor creates students):** resolved as instructor-
creates-outright, not invite-then-signup - `POST /users` already covers
creation; `GET /users` lists an Administrator's own students (never another
admin's), which is what the frontend's "Awaiting class assignment" queue
should filter client-side by `plan is None` rather than a separate endpoint.

**Soft delete:** every model has `is_deleted` (`app/models/base.py`).
`BaseRepository.soft_delete()` sets it; `get_by_id()`/`UserRepository.get_by_email()`
exclude it, and a soft-deleted user's existing token is rejected on its next
use. Nothing physically removes a document. One known gap, flagged rather
than silently handled: `User.email`'s unique index still counts a
soft-deleted account, so that email can't be reused by a new one - fixing
that needs a partial/sparse index decision that wasn't part of this ask.

## Run locally

```bash
python -m venv .venv
source .venv/Scripts/activate   # .venv\Scripts\activate on cmd.exe
pip install -r requirements-dev.txt

cp .env.example .env            # then fill in real values
uvicorn app.core.application:app --reload
```

Requires a reachable MongoDB (`MONGODB_URI` in `.env`) - the app connects to it
on startup and will fail to boot without one.

`GET /api/v1/health` is a plain liveness check with no dependencies.

`uvicorn app.core.application:app --reload` is for local dev (autoreload needs
an import string). In a container, use `python -m app.main` instead - that's
the fixed, no-reload entry point the Dockerfile will call.

`requirements.txt`/`requirements-dev.txt` are pinned to exact versions (`==`),
not `>=` - update them deliberately (`pip install -U <pkg>`, re-freeze,
re-test), not by accident because pip resolved something newer. This already
caught one real incompatibility: `passlib` (unmaintained) is broken against
current `bcrypt` releases, so password hashing calls `bcrypt` directly instead
- see `app/core/security.py`.

## Tests

```bash
pytest
```

No real MongoDB needed - `tests/conftest.py` wires Beanie up against
`mongomock-motor` (in-memory), fresh per test, so the suite runs anywhere with
no external services. One narrow, test-only compatibility shim lives there
too (mongomock 4.3.0 vs. a newer kwarg Beanie 2.2.0 passes) - it's commented,
and doesn't touch any production code path.

If you need to test against a *real* MongoDB (e.g. before a release, or if a
mongomock behavior difference is suspected), point `MONGODB_URI` at a real
instance and adapt the `mock_db` fixture - the app code itself doesn't know or
care which one it's talking to.

## Skills

Project-scoped Claude Code skills live under `.claude/skills/` (real files,
copied - not symlinked - so they work immediately after `git clone`, no extra
step needed). Installed via [skills.sh](https://skills.sh/):

- `fastapi` (official) / `fastapi-expert` - FastAPI patterns
- `cloudinary-docs` (official) - for the recording-submission pipeline
- `livekit-agents` (official) / `gemini-live-api-dev` (official) - for `livekit_agent/`
- `karpathy-guidelines` - behavioral guardrails against overcomplicating changes

`skills-lock.json` tracks exact versions. To add another skill:
`npx skills add <owner/repo>@<skill> --copy -y` (the `--copy` matters - without
it, the CLI defaults to a symlink with an absolute path, which breaks the
moment someone clones this repo somewhere else).

## MCP server (FastAPI docs)

[`.mcp.json`](.mcp.json) at the repo root wires up
[fastapi-docs-mcp](https://github.com/jaredthivener/fastapi-docs-mcp) as a
**project-scoped** MCP server - real-time FastAPI documentation lookup from
inside Claude Code (or any MCP client). The wiring is committed and shared;
the server's source is **not** - clone it yourself, once, wherever you keep
this repo:

```bash
git clone https://github.com/jaredthivener/fastapi-docs-mcp.git .mcp/fastapi-docs-mcp
cd .mcp/fastapi-docs-mcp && uv sync
```

`.mcp/` is gitignored - this is a local, per-developer clone, not a submodule.
`.mcp.json` expects the server at exactly `.mcp/fastapi-docs-mcp` (relative to
the repo root, via `${CLAUDE_PROJECT_DIR}`) - clone it there and it just works;
clone it elsewhere and update the path in `.mcp.json` to match.

Claude Code will prompt to approve the project's MCP servers the first time
you open this folder - that's expected, since it's defined in a committed
file, not your personal config.

Requires [`uv`](https://docs.astral.sh/uv/) on PATH (the server's own
dependency manager - not related to this backend's `requirements.txt`).

## Full platform context

The architecture decisions, data model, and Kanban board this backend is being
built against live in the original UI source repository repo:
`Tamakkan-Consolidated-Platform-Plan.md` and `Tamakkan-Kanban-Board.md`.
