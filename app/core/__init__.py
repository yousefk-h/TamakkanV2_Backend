"""Cross-cutting infrastructure: settings, database connection, security primitives,
and custom exceptions. Nothing here depends on `app.api`, `app.services`, or
`app.repositories` - core is the bottom of the dependency graph.
"""
