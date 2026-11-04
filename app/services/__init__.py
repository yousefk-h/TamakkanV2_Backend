"""Business logic layer: sits between the API and the data layer.

A service orchestrates one or more repositories to fulfil one use case (e.g.
"register a student", "submit a recording"). Route handlers call services;
services never import FastAPI/Starlette types, and never call Beanie/Motor
directly - that always goes through `app.repositories`.
"""
