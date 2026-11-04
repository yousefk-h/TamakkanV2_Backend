"""Beanie Document models - one file per Mongo collection.

Every `Document` subclass defined here must be registered in the
`document_models` list passed to `init_beanie()` in `app.core.database`, or it
will silently fail to persist.
"""
