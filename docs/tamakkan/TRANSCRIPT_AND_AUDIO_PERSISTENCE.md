# Transcript and audio persistence (Avatar + Voice) - verified 2026-09-30

Read-only audit of the code in this repo plus the installed `livekit-agents==1.7.1` source. Nothing was changed.
"Not verifiable here" marks things decided by a vendor dashboard or vendor policy that no code in this repo controls.

## Avatar Practice (`mode = topic_discussion`, Mariam worker `tamakkan-mariam-dev`)

| Question | Answer | Evidence |
|---|---|---|
| Transcript saved? | **Yes.** Final conversation turns (`student` / `mariam`) | `agent.py` `_on_conversation_item_added` buffers turns in worker memory |
| Where? | MongoDB `live_sessions.transcript_history` (trusted transcript). Written **once**, on `mark-ended`, by the worker over the key-gated internal route. Lost if the worker dies before it reports (the abnormal-exit finalizer still sends what was buffered) | `LiveSessionService.mark_ended`; `LiveSession.transcript_history` |
| Who reads it back? | Scoring, the speaking report, lesson completion, analytics, and the session detail API (`SessionResponse.transcript_history`) | `generate_report`, `core/learning_analytics.py`, `schema/live_session.py` |
| Retention | No TTL or cleanup job for `live_sessions`: kept until deleted manually | no retention job exists for this collection |
| Raw/session audio recorded by Tamakkan? | **No.** No egress, no `MediaRecorder` on the live call, no `record=` in the agent. `recording_enabled_by_default` is pinned `False`; the model docstring says the call is never recorded | `models/live_session.py`, `platform_avatar_config.py`, grep for egress/record |
| LiveKit Cloud observability recording | **Not verifiable here, and not set by our code.** `agent.py` calls `AgentSession.start()` without `record=`. In SDK 1.7.1 that means "defer to the server-side setting": `record = job_ctx.job.enable_recording`. If the Kassel LiveKit Cloud project/agent has session recording/observability enabled, LiveKit Cloud receives audio, transcript, traces and logs (`_upload_session_report`). It is stored by LiveKit, not in our DB. Check the LiveKit Cloud dashboard (agent/project observability or data settings) for state and retention | `livekit/agents/voice/agent_session.py` lines ~892-930, `job.py` `_upload_session_report` |
| Vendors that see the audio live | OpenAI Realtime (primary) or Ultravox (fallback) process the audio; SpatialReal receives Mariam's output audio to render the avatar. Their own retention/logging policies apply | **Not verifiable here** (vendor policy) |
| Browser storage | None. The frontend never writes transcripts to `localStorage` / `sessionStorage` / IndexedDB | grep of `frontend/` |

## Voice Practice (`mode = voice_practice`, hosted Builder agent `tamakkan-voice-dev`)

| Question | Answer | Evidence |
|---|---|---|
| Transcript saved? | **Yes, provisional only.** Final `lk.transcription` segments the student's own browser collected | `voiceRoom.ts`, `transcript.ts` |
| Where? | MongoDB `live_sessions.browser_transcript`, written once when the student ends the session (`POST /sessions/{id}/end`, student-owned, capped at 600 turns). Never copied to `transcript_history`, never scored, no report scheduled. If the tab closes before the end call succeeds, nothing is saved | `end_voice_session`, `MAX_BROWSER_TRANSCRIPT_TURNS` |
| Who reads it back? | The session detail API returns it (`SessionResponse.browser_transcript`), display/provisional archive only | `schema/live_session.py` |
| Retention | Same as Avatar: no TTL/cleanup | |
| Raw/session audio recorded by Tamakkan? | **No.** Same as Avatar: no egress, no recorder, no audio field on `LiveSession` | |
| Hosted Builder agent side | **Not verifiable here.** The agent is configured in LiveKit Builder (not in this repo). Whether Builder/LiveKit Cloud records audio or transcripts, and for how long, depends on that agent's and the project's observability settings | dashboard check needed |
| Browser storage | None | |

## Unrelated feature, often confused with this

`RecordingSubmission` (the student's own "record yourself" homework) IS real audio/video: uploaded to Cloudinary via
`Recorder.tsx`, metadata in `recording_submissions`, media deleted by `app/jobs/cleanup_expired_recordings.py` after a
7-day TTL (`RECORDING_TTL_DAYS`). It is not part of Avatar or Voice calls.

## Open item for the owner (no code change made)

Confirm in the LiveKit Cloud dashboard whether agent session recording/observability is ON for the Kassel project and
for `tamakkan-voice-dev`. If "the live call is never recorded" must hold end to end, the agent would need an explicit
`record=False` (a behaviour change, deliberately not made in this task).
