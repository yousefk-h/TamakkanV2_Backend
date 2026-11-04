"""Shared response-schema fields, mirroring `app.models.base.BaseDocument` -
every schema that represents a persisted Document exposes at least these.
"""

from datetime import datetime

from pydantic import BaseModel


class BaseSchema(BaseModel):
    id: str
    created_at: datetime
    updated_at: datetime
