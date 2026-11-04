"""Length caps for user-supplied strings and lists.

Nothing here is a business rule - these are the ceilings that stop a request
body from becoming a denial-of-service or a poisoned storage key. They live in
one module so every schema uses the same numbers, and so it's obvious at a
glance which fields are bounded and which were missed.

Why this matters beyond "big request slow": `full_name` is Student-writable via
`PATCH /me` and flows into the reset-request email body *and* the Cloudinary
`public_id` for every recording that student submits. Unbounded, a 10 MB name
produced a 10 MB storage key that no upload could ever succeed with. The
`BodySizeLimitMiddleware` caps the whole request, but a body well under 30 MB
can still carry one absurd field, and a field cap is what keeps stored values
sane rather than merely survivable.

Generous on purpose - real content should never come near these.
"""

MAX_EMAIL_LENGTH = 254  # RFC 5321's practical limit on an address
MAX_NAME_LENGTH = 120
MAX_TITLE_LENGTH = 200
MAX_URL_LENGTH = 2048
# One line of authored content: a warm-up question, a phrase, a sentence starter.
MAX_LINE_LENGTH = 1000
# The avatar/system prompt and other multi-paragraph authored text.
MAX_PROMPT_LENGTH = 10_000
# A full video transcript, admin-edited before approval. A ~3 hour video at
# normal speech rate is roughly 150-200k characters - generous headroom above that.
MAX_TRANSCRIPT_LENGTH = 300_000
# Opaque credentials arriving on unauthenticated routes. A refresh token is a
# 43-character urlsafe string and a JWT is a few hundred bytes; this is slack,
# not a target.
MAX_TOKEN_LENGTH = 4096
# Any authored list on a topic (questions, vocabulary, phrase groups, ...).
MAX_LIST_ITEMS = 200
