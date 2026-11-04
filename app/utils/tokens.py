"""Random opaque token generation/hashing for refresh and password-reset
tokens - deliberately *not* bcrypt (that's for low-entropy secrets like
passwords, where deliberate slowness matters). These tokens are 256 bits of
`secrets`-grade randomness, so a fast, well-known hash is the right tool:
brute-forcing the hash is infeasible either way, and there's no reason to pay
bcrypt's cost for it.
"""

import hashlib
import secrets


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
