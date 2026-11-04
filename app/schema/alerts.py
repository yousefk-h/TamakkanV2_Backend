"""What a teacher needs to know without opening every student individually:
who has fallen behind on lessons, and who missed a check-in submission. See
`ProgressService.get_alerts`/`get_alerts_for_students`.
"""

from pydantic import BaseModel


class StudentAlerts(BaseModel):
    pending_rollover_count: int
    # Which past end-of-week check-in days (see `app.core.challenge
    # .END_OF_WEEK_CHECKIN_DAYS`) have no submission yet - empty means none missed.
    missed_submission_days: list[int]
    has_alerts: bool
