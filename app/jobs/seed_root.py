"""One-shot script to create the first Root account.

Root creates Administrators via `POST /root/admins`, and Administrators
create Students via `POST /users` - so only the very top of that chain needs
a manual seed. This replaces the old `seed_admin.py`: there's no reason to
seed an Administrator directly anymore now that Root's API does it.

Usage:
    python -m app.jobs.seed_root --email root@tamakkan.example --password ... --name "Platform Root"

Idempotent - does nothing if an account with that email already exists.
"""

import argparse
import asyncio

from app.core.database import close_db, init_db
from app.core.security import hash_password
from app.models.user import Role, User
from app.repositories.user_repository import UserRepository


async def _seed(email: str, password: str, full_name: str) -> None:
    await init_db()
    try:
        repository = UserRepository()
        if await repository.get_by_email(email) is not None:
            print(f"User {email} already exists - nothing to do.")
            return
        user = User(
            email=email,
            password_hash=hash_password(password),
            full_name=full_name,
            role=Role.ROOT,
        )
        await repository.save(user)
        print(f"Created root account {email}.")
    finally:
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--name", required=True, dest="full_name")
    args = parser.parse_args()
    asyncio.run(_seed(args.email, args.password, args.full_name))


if __name__ == "__main__":
    main()
