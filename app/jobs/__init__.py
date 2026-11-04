"""Background and scheduled jobs.

Launch-scope jobs run as FastAPI `BackgroundTasks` - nothing in this package
should assume a Celery worker/broker exists. That migration is a deliberate
post-launch step (see the platform plan), not something to build ahead of need.
"""
