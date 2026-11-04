# Platform activity and metrics (Task 4)

The teacher Analytics page and overview, plus the student's last-seven-days panel, now use persisted activity. They do not need AI credentials to display existing records. A new isolated database shows zero counts and unavailable assessment averages until activity exists.

## API and ownership

- `GET /api/v1/analytics?days=7|30`: administrator only, scoped to non-deleted students created by that administrator. Disabled students remain in the roster and historical activity.
- `GET /api/v1/me/analytics?days=7|30`: student only, scoped to the authenticated account. No user selector is accepted or used.
- Other periods return 422. Existing authentication and exact role checks remain in force. Responses use `Cache-Control: no-store`, which the frontend proxy preserves.
- The queries load only metric fields from at most two periods (60 days). Transcripts, report feedback, user contact details and recording URLs are not loaded for aggregation. No collection or migration is added.

## Metric definitions

| Metric | Meaning |
| --- | --- |
| Students who practised | Distinct students with an ended conversation or submitted recording in the selected period. Login and enabled-account status do not count as practice. |
| Completed sessions | Sessions with state `ended`, a non-missing end time, and `start <= end <= now`. Failed, active, deleted and future sessions are excluded. |
| Session minutes | Sum of valid session wall-clock durations, rounded to one decimal minute. Includes the tutor and pauses; not measured student speech time. |
| Recordings submitted | Number of submitted recording records, including separate resubmission attempts. Expired media still represents an activity event; soft-deleted records are excluded. |
| Topics practised | Distinct non-null topic IDs among completed sessions. Repeated practice of one topic counts once, including across a teacher's students. This is not vocabulary mastery or course completion. |
| Daily active students | Distinct practising students on each UTC day. Empty days are returned with zero counts. |
| Returning students | Students active in both the current and immediately preceding period, divided by those active in the preceding period. The denominator is restricted to the current roster; no prior cohort produces null, not 0%. |
| Assessment averages | Mean of numeric scores per rubric and recorded lesson level, with report and student sample counts. Each report has equal weight. Legacy scores have their own group with unknown level; null/unassessed scores are excluded. An actual score of zero is included. |
| Awaiting review | Reports and recording attempts from the selected activity period whose review flag is false. These are not all-time backlog counts. |

Periods include today's partial UTC day and the previous 6 or 29 days. A session's full duration and activity are attributed to its start day. Recordings use their submission day. The preceding period consists of the previous 7 or 30 full calendar days, so return rates are provisional while the current day is in progress. The response includes its generation timestamp; refresh or refocusing the browser fetches current data. Switching ranges hides the previous range while loading and ignores late responses.

## Student progress corrections

- The home-page ring counts `done` lessons in the assigned catalogue; this follows the existing learning-calendar rule of a completed conversation for that topic. The ring is unavailable when the plan has not loaded or there are no assigned lessons.
- The challenge page's time-based ring is explicitly labelled **Program days elapsed**, not completion. Unassigned accounts no longer default to day one.
- The featured lesson prefers current/missed lessons, then a completed lesson for review. Locked lessons are not promoted as available. The fixed 40% bar and sample weekly values/date were removed.
- Weekly activity and the progress-page dots use actual UTC dates. `GET /me/progress` now includes additive `last_7_days_dates` alongside `last_7_days_active`.
- The vocabulary badge description now reflects its existing calculation: practising topics containing vocabulary entries, not proving those words were learned.
- Loading, unavailable-data and retry states prevent failed reads from becoming invented zero counts or unearned badges.

## Verification

- 88 relevant backend tests passed across analytics, session API and progress. New cases cover ownership/roles, invalid ranges, exact UTC boundaries, repeated activity, disabled/deleted accounts, missing/failed/future sessions, genuine zero scores, separate rubrics and review-state updates.
- Real local teacher and student logins reached the analytics endpoints through the authenticated frontend proxy. The local database remained unchanged apart from normal login/logout tokens; sample activity was not seeded.
- Browser-only fixtures exercised nonzero data, zero/empty state, errors/retry, range changes including late responses, lesson status and responsive English/Arabic layouts from 320 to 1440 pixels. Fixtures never became stored student activity.
- Targeted frontend ESLint and the Tamakkan production build passed. The repository's existing `cloudflare:workers` type-resolution error in `db/index.ts` is unrelated and remains; the production build also retains its existing large-chunk warning.

AI report generation, live conversation and uploads still depend on their separately configured service credentials. This task activates metrics over records; it does not provision those providers or deploy a hosted environment.
