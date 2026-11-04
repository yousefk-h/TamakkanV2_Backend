# Tamakkan backend validation and task status

Validation date: 2026-09-18. Branch: the legacy integration branch in both repositories.

The complete existing API test suite and the offline voice-worker suite passed.
The running local API also passed checks against real local MongoDB. This is
local validation, not confirmation that every coworker requirement is finished
or that hosted integrations are ready.

## Results from this review

| Check | Passed | Failed/errors | Skipped |
| --- | ---: | ---: | ---: |
| API/backend suite (`tests/`) | 519 | 0 | 0 |
| Separate voice-worker suite (`livekit_agent/tests/`) | 7 | 0 | 4 |
| Running local HTTP/MongoDB acceptance assertions | 62 | 0 | 0 |

The 519 backend tests comprise 293 API tests, 107 core tests, 99 service tests,
12 model tests, 4 repository tests, 3 cleanup-job tests and 1 health test.
They cover authentication and refresh/revocation, role and ownership checks,
account management, email verification, lessons/topics, calendars, classes,
entitlements, session state, transcript/report handling, analytics, progress,
submissions, input/body limits, soft deletion and recording cleanup.

The four skipped tests call a real OpenAI model to evaluate greeting, topic
boundaries, instruction integrity and spoken-output formatting. No paid model,
LiveKit room, avatar, SMTP provider or media-upload service was called.
Passing prompt-composition tests does not establish the quality of live AI
coaching or pronunciation feedback.

The API suite uses a fresh in-memory MongoDB substitute for each test and mocks
external services. The run selected `.env.example`, disabled automatic dotenv
loading and cleared provider credentials in its child process. Tamakkan-specific
tests explicitly enable the relevant profile/settings. Actual environment files
were not edited.

The initial repository-wide collection could not import the separately deployed
voice worker because its dependencies were absent from the API virtualenv. Its
pinned dependencies and test tools were installed into `livekit_agent/.venv`,
then its own suite ran successfully. Run the two suites in their respective
environments; a bare `pytest` in the API environment still collects worker tests
whose dependencies belong to the worker environment.

The API run emitted one dependency deprecation warning and 28 warnings about
short HMAC keys in session-test fixtures. They did not cause failures. This run
does not claim 100% line coverage, load testing or a security audit.

## Running API and real database checks

The additional acceptance run used `http://127.0.0.1:4653` and the existing local
MongoDB instance on port 27019, database `tamakkan_dev`. It checked:

- Teacher/student login, current-user roles, anonymous rejection and role guards.
- Actual 7/30-day analytics, response shape, nonnegative counts, roster agreement,
  invalid-range rejection and `no-store` caching.
- Teacher reads for topics, calendar templates, sessions and submissions; student
  reads for topics, own sessions and progress.
- Creation of one temporary student, pending-verification login rejection and a
  real locally rendered verification email preview.
- Hashed-token storage, generic resend responses and cooldown enforcement.
- Two concurrent confirmation requests against MongoDB: exactly one succeeded.
- Persisted verified state, successful sign-in, refresh rotation, rejection of
  the old token, logout and rejection of the revoked token.

The temporary account, its preview email and the refresh-token records created
by this check were removed. No lessons, learning activity or scores were seeded.
The local acceptance script is `../tmp/tamakkan-backend-smoke.py`; it checks the
local database address/name and file-only mail mode before running. Its 62
assertions are additional acceptance evidence, separate from the pytest count.

Evidence files in the workspace:

- `../tmp/tamakkan-backend-api-tests.xml`
- `../tmp/tamakkan-worker-tests.xml`
- `../tmp/tamakkan-backend-smoke-results.json`

## Coworker requirement checklist

| Task | Current status | Remaining work or acceptance limit |
| --- | --- | --- |
| 1. Separate Tamakkan project | Complete for the agreed local, branch-based setup | Both repositories use the legacy integration branch, with separate local runtime/data. Separate hosted deployment is not provisioned. |
| 2. Branding | Implemented; previously checked locally | White/blue Tamakkan interface and branding are present. This backend review did not repeat the frontend visual audit. |
| 3. AI prompt and detailed feedback | Implemented for live-tutor instructions and transcript-based reports; final acceptance pending | Real provider conversation/report generation and teacher review are pending. Saved pronunciation analysis is absent: stored session evidence is text only. Supporting recorded pronunciation measurements would require an audio-evidence pipeline. |
| 4. Activate platform metrics | Implemented and locally tested | Metrics aggregate persisted sessions/submissions; empty activity correctly gives zero counts. Live conversation/upload-to-metric flows await configured providers. |
| 5. Project access | Complete for the agreed repository/branch workflow | The user confirmed receipt of the repositories and branch access. Hosting/service access is a separate deployment dependency. |
| 6. First two lessons | Not complete | The course owner must identify/provide the first two lessons, their order and level. No substitute lessons were imported. |
| 7. Email verification | Implemented and locally tested; delivery not complete | Secure links, resend, pending/verified states and access blocking work. Configure SMTP/sender/domain and prove real inbox delivery. This follows teacher-created students and Root-created teachers; public self-registration is not implemented. |
| 8. Phone/tablet responsiveness | Complete per subsequent user confirmation | The user confirmed the iPhone/iPad checks and video-resize item are done. Prior automated evidence is retained in the frontend responsiveness document. |
| 9. Framework migration | Omitted by user decision | No migration is required for the agreed remaining scope. |

## What prevents full completion

1. **Course material:** obtain and configure the two approved lessons.
2. **Provider setup and live acceptance:** the current local Tamakkan profile has
   no Gemini, LiveKit, Cloudinary or SMTP credentials. Configure Tamakkan services
   and the separate voice worker, then test a real conversation, generated report,
   recording upload, resulting metrics and delivered verification email.
3. **Pronunciation reporting:** the live tutor prompt addresses audible
   pronunciation, but saved reports explicitly mark it unassessed. Decide whether
   live coaching satisfies the requirement or recorded analysis must be added.
4. **Release environment:** the emailed company MongoDB address has not replaced
   the isolated local profile. Confirm the intended development/staging/production
   target and network access, then verify the configured hosted database and app.
5. **Senior review and release:** submit the implementation on the legacy integration branch
   for senior review before merging into `main` and deploying. The testing run
   itself did not merge or deploy changes.

Implementation details and prior evidence:
[environment](docs/LOCAL_DEVELOPMENT.md), [AI feedback](LEARNING_ANALYTICS.md),
[metrics](PLATFORM_ANALYTICS.md), [access](PROJECT_ACCESS.md),
[lessons](TAMAKKAN_LESSON_CONTENT.md), [verification](EMAIL_VERIFICATION.md), and
[responsiveness](../frontend/RESPONSIVE_DESIGN.md).
