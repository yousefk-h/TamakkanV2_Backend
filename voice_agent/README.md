# Tamakkan Voice agent (`tamakkan-voice-dev`)

The Voice Practice agent, exported from the LiveKit Builder agent of the same name and now deployed as a code agent (Builder agents cannot take `lk agent deploy`). The dispatch name stays `tamakkan-voice-dev`, so the backend's `LIVEKIT_VOICE_AGENT_NAME` is unchanged. It uses no SpatialReal and never takes an Avatar slot. Its models (Deepgram Nova-3 STT, Gemma 4 31B LLM, Deepgram
Aura-2 "luna" TTS), voice, noise isolation and prompt are the Builder export's own and are pinned by
`tests/test_transcript.py`; only trusted-transcript delivery was added.

## What was added

`src/transcript.py` + the wiring in `src/agent.py`:

- Collects the turns this agent's own `AgentSession` commits (`conversation_item_added`): the student's speech-to-text
  and Mariam's replies, in order, as `student` / `mariam`. Nothing a browser sends is read or forwarded.
- When the call ends (session `close`, or job shutdown if the room dropped first) it POSTs them **once** to the backend's
  `POST /api/v1/sessions/{session_id}/voice-transcript` with `X-Internal-Api-Key`. The backend stores them as the trusted
  `transcript_history` and schedules the one existing report pass. No scoring and no AI calls happen here.
- Retries network errors, 5xx and 429 (1 s, 3 s); any other 4xx is permanent. A failure is logged and never fails the call.
- Only a job whose dispatch metadata says `project: "tamakkan"` and has a `session_id` reports; any other job sends nothing.
- Logs carry the session id, turn counts and delivery status only - never transcript text or the key.

## Secrets (set on this agent in LiveKit Cloud, not in the repo)

| Name | Value |
|---|---|
| `BACKEND_API_URL` | the Tamakkan backend API base, e.g. `https://tamakkanbackend.tamakkan.online/api/v1` |
| `AGENT_INTERNAL_API_KEY` | the same value as the Tamakkan backend's `AGENT_INTERNAL_API_KEY` |

## Tests

From the backend repo root: `pytest voice_agent/tests`.

## Deploy

Not part of the backend deployment. First time: `lk agent create --secrets-file <file with the two secrets above> .` Afterwards: `lk agent deploy .`, from this directory with the LiveKit CLI logged in to the `kassel-for-academic-centers` project. The dependency lock is pinned to the runtime the agent already runs.
