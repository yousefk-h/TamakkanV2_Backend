"""Data-access layer: one repository per Mongo collection / Beanie Document.

Repositories are the only layer allowed to import from `app.models` or call
Beanie/Motor directly. Services call into repositories - never the other way
around, and routes never import a repository directly.
"""
