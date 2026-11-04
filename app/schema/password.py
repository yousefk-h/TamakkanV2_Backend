"""One password type, used by every schema with a password field.

bcrypt's limit is 72 **bytes**, but Pydantic's `max_length` counts
**characters** - so `max_length=72` let a 72-character non-ASCII password (any
Arabic, Cyrillic, accented Latin, or emoji text) through at up to 4x that many
bytes, and bcrypt then raised `ValueError`. That escaped as a 500, which was
worse than it sounds: `AuthService.login` only reaches bcrypt when the account
exists, so a 500-vs-401 difference told an unauthenticated caller exactly which
emails are registered - defeating the generic-error rule the login path is
explicitly built around (FR-003).

Validating on the encoded length is the whole fix. Kept as one shared type
rather than repeated `Field(...)` calls so a field can't be added later that
quietly misses it.
"""

from typing import Annotated

from pydantic import AfterValidator, Field

BCRYPT_MAX_BYTES = 72
MIN_PASSWORD_LENGTH = 8


def _fits_bcrypt(value: str) -> str:
    encoded = len(value.encode("utf-8"))
    if encoded > BCRYPT_MAX_BYTES:
        raise ValueError(
            f"Password is too long: {encoded} bytes, maximum is {BCRYPT_MAX_BYTES}. "
            f"Note that accented, Arabic, and emoji characters use more than one byte each."
        )
    return value


# For setting a password: enforces the minimum length too.
NewPassword = Annotated[str, Field(min_length=MIN_PASSWORD_LENGTH), AfterValidator(_fits_bcrypt)]

# For supplying an existing one (login, current-password checks). No minimum -
# an old password predating a rule change must still be presentable, and a
# rejection here has to be the generic "incorrect", never a validation error
# that reveals anything about the stored value.
ExistingPassword = Annotated[str, AfterValidator(_fits_bcrypt)]
