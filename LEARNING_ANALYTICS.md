# Tamakkan learning feedback (Task 3)

## Scope and activation

The Tamakkan conversation instructions and report format are always on. The former `LEARNING_ANALYTICS_PROFILE` setting is no longer read by the backend or the worker; do not set it. Use the backend and frontend from the legacy integration branch together: report scores can now be null.

The backend passes `learning_profile` in its signed LiveKit dispatch configuration. Deploy the updated `livekit_agent` worker alongside the backend: it chooses the Tamakkan instructions for those jobs and keeps the original instructions for jobs without that profile. Both the existing OpenAI Realtime and Ultravox paths use the selected instructions. Model selections and service credentials are unchanged.

The live tutor now uses concrete lesson goals, focused follow-up questions, a correction/model/retry cycle, and specific observed strengths. Pronunciation coaching focuses on intelligibility, stress and rhythm heard in live audio. It must request a repeat when audio is unclear and must not invent numerical acoustic measurements.

## Saved reports

Reports are still generated in the background after a completed conversation supplies its transcript. Existing reports are retained, with no migration or automatic re-scoring. No new endpoint or permission is needed: students see their own reports and teachers see reports for their own roster.

The additive `report.analysis` object includes:

- Rubric `tamakkan-text-v1`, assigned lesson-level snapshot, and assessed/insufficient-evidence status.
- Counts of student responses, English words, distinct words and average words per response. Tutor text is excluded. Session duration is wall-clock time, not speaking time or a words-per-minute denominator.
- Supported strengths and improvement areas; exact quoted corrections, natural alternatives and explanations; recurring patterns backed by distinct turns.
- One to three practice actions, suggested minutes and observable success criteria.
- Score differences from a prior completed assessment for the same student, lesson, assigned level and rubric. Legacy, unassessed, deleted, other-lesson and future sessions are excluded.

Grammar, vocabulary, sentence complexity and **text-based fluency/coherence** use 0–100 teaching estimates. Overall is their rounded arithmetic mean, computed in code. These are not standardized proficiency measurements. Historical comparisons are score changes, not proof of lasting improvement.

**Pronunciation is not assessed in saved reports.** The current session pipeline retains text only, by design. It cannot support acoustic pronunciation, speech-rate, pause or rhythm scores. The UI states this explicitly; live pronunciation coaching is separate. Persistent pronunciation analysis would need a separately designed audio-evidence pipeline.

## Evidence and failure handling

- At least three nonblank student turns, 30 English word tokens and ten distinct English words are required before calling the assessment model. This is a conservative product guard, not a validated proficiency threshold. The model can still mark a longer but unusable transcript insufficient.
- Insufficient evidence produces null scores, not zeros, and does not qualify for score-based badges or trends.
- Feedback quotes must occur verbatim in the referenced student turn. Tutor quotes, nonexistent turns and invented evidence are rejected. A recurring pattern requires at least two distinct student turns. These checks establish provenance, not the correctness of every model judgment.
- Turns are serialized as structured JSON. Student and lesson content are described as untrusted data in the prompts.
- Model failures, invalid feedback or inputs above 100,000 serialized characters leave the report absent; they do not substitute a fabricated result. Empty transcripts also receive no report. The existing flow has no automatic retry or regenerate action.
- Logs for Tamakkan assessment failures contain the session ID and exception class, not transcript text or model output. Existing stored reports are not overwritten.

## Dashboard locations

- Teacher: **AI Reports → AI Summary**; also the student's detail page.
- Student: **My Progress → Latest analysis**. Recent history comes from completed sessions. The trend uses comparable scored sessions and handles a single score.
- English and Arabic interface labels are available. Generated teaching feedback remains in English.

## Validation and remaining live check

Local automated validation: 94 relevant backend tests passed across the assessment, session API/dispatch, progress and environment-isolation checks, including the subsequent comparable-level badge regression check. Frontend report fixtures cover rich/legacy/insufficient reports, empty lists, fetch errors and retry, and teacher review actions. Headless Edge checks cover English widths 320–1440 and Arabic widths 320–1440 on both teacher and student views. Fixtures are intercepted in the browser and are not saved to the database. Targeted frontend ESLint and the Tamakkan production build pass; the standalone TypeScript check still has the pre-existing `cloudflare:workers` type-resolution error in `db/index.ts`.

Live provider/audio validation remains pending dedicated Tamakkan credentials. Before considering production activation complete:

1. Configure Tamakkan's Gemini and LiveKit settings, and a separate worker profile with the matching agent name, internal API key and Tamakkan backend URL. Configure its voice/avatar providers as described in the worker's environment example. Do not reuse a worker running for another deployment.
2. Run the updated worker and backend. Assign a real lesson to a test student and complete a substantive conversation. Check that the tutor follows the lesson goals and offers useful corrections/retries without interrupting every sentence.
3. End normally, then refresh AI Reports/My Progress after background scoring. Verify quoted evidence against the transcript, the practice goals, four skill scores and computed overall. Have a teacher review the quality of the feedback.
4. Repeat the same lesson and level to check the comparison. Try a short session and unclear audio to check appropriate uncertainty. Verify another student's reports remain inaccessible.

No live provider call, acoustic accuracy evaluation or hosted deployment is claimed by the local checks.
