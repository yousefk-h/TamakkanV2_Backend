"""Pydantic schemas for API request/response bodies.

A route handler should never accept or return an `app.models` Document
directly - it accepts/returns a schema from here, and a service or repository
translates between the two.
"""
