"""Shared "30-Day Challenge" calendar constants - the total length, and
which days are reserved for the end-of-week check-in rather than a lesson
topic. Used by `TopicContentService` (validating an admin's day-pin on
`TopicContent.challenge_day`) and `ProgressService` (building the student's
actual day-by-day schedule) alike, so the two can't drift out of sync.

These day numbers were never actually anchored to the real calendar
weekday - it's a fixed position in each 7-day cycle, not `.weekday() == 4`
against `User.program_start_date`. Originally labeled "Friday" back when day
1 was assumed to always be a Monday; renamed to the more accurate
"end of week" once teacher-set start dates (Phase F) made that assumption
untrue in general.

Mirrored by hand on the frontend as `END_OF_WEEK_DAYS` in
`app/student/challenge/page.tsx` - nothing shares config between the two
apps today.
"""

CHALLENGE_TOTAL_DAYS = 30
END_OF_WEEK_CHECKIN_DAYS = {5, 12, 19, 26}
