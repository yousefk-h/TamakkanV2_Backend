"""Safeguards for LOCAL development mode (`LOCAL_DEV_MODE=true`).

Local mode must never be able to reach a production database or a production
LiveKit project, even through a copy-pasted `.env`. `Settings` calls
`local_mode_violations` at startup and refuses to boot on any violation; the
same function backs the pre-start check in `scripts/local/tamakkan-local.ps1`
(`python -m app.core.local_guard <env-file>`).

Add a new production identifier to the constants below whenever one is created.
"""

from __future__ import annotations

import sys
from typing import Iterable
from urllib.parse import urlsplit

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
# Docker Desktop containers cannot reach the host's loopback address. This
# exact service name is reserved for Tamakkan V2's local Mongo container.
LOCAL_DOCKER_MONGO_HOST = "tamakkan-v2-mongo"
# Databases that hold (or are planned to hold) real data. Compared case-insensitively:
# MongoDB treats names differing only by case as the same database on most servers.
# Every other production database is already rejected by LOCAL_DB_PREFIX below.
PRODUCTION_DB_NAMES = {"tamakkan"}
LOCAL_DB_PREFIX = "tamakkan_"
# Fragments of production LiveKit project hosts and agent names.
PRODUCTION_LIVEKIT_MARKERS = ("ielets-dajbgqlm", "tamakkan-agent-m4zc52ik")
PRODUCTION_AGENT_NAMES = {"mariam-agent", "tamakkan-mariam-v2"}


def _mongo_hosts(uri: str) -> list[str]:
    """Hosts in a mongodb:// URI (comma-separated seed list supported)."""
    rest = uri.split("://", 1)[1] if "://" in uri else uri
    authority = rest.split("/", 1)[0].split("?", 1)[0]
    authority = authority.rsplit("@", 1)[-1]
    hosts = []
    for part in authority.split(","):
        part = part.strip()
        if part.startswith("["):  # [::1]:27017
            hosts.append(part.split("]")[0] + "]")
        else:
            hosts.append(part.split(":")[0])
    return [h for h in hosts if h]


def local_mode_violations(
    *,
    mongodb_uri: str,
    mongodb_db_name: str,
    environment: str,
    livekit_url: str = "",
    livekit_agent_name: str = "",
    email_delivery_mode: str = "",
    backend_api_url: str = "",
    allow_docker_local_mongo: bool = False,
) -> list[str]:
    problems: list[str] = []
    if mongodb_uri.startswith("mongodb+srv://"):
        problems.append("MONGODB_URI uses mongodb+srv:// (a hosted cluster); local mode allows only a loopback MongoDB")
    else:
        allowed_hosts = set(LOOPBACK_HOSTS)
        if allow_docker_local_mongo:
            allowed_hosts.add(LOCAL_DOCKER_MONGO_HOST)
        bad = [h for h in _mongo_hosts(mongodb_uri) if h.lower() not in allowed_hosts]
        if bad or not _mongo_hosts(mongodb_uri):
            problems.append(f"MONGODB_URI host(s) {bad or ['<none>']} are not loopback (127.0.0.1/localhost)")
    name = mongodb_db_name.strip()
    if name.lower() in PRODUCTION_DB_NAMES:
        problems.append(f"MONGODB_DB_NAME '{name}' is a production database name")
    if not name.startswith(LOCAL_DB_PREFIX):
        problems.append(f"MONGODB_DB_NAME '{name}' must start with '{LOCAL_DB_PREFIX}' in local mode")
    if environment != "development":
        problems.append(f"ENVIRONMENT must be 'development' in local mode (got '{environment}')")
    lk = livekit_url.lower()
    for marker in PRODUCTION_LIVEKIT_MARKERS:
        if marker in lk:
            problems.append("LIVEKIT_URL points at a production LiveKit project; use a separate test project or leave it unset")
            break
    if livekit_agent_name and livekit_agent_name in PRODUCTION_AGENT_NAMES:
        problems.append(f"LIVEKIT_AGENT_NAME '{livekit_agent_name}' is a production agent name")
    if email_delivery_mode == "smtp":
        problems.append("EMAIL_DELIVERY_MODE=smtp could send real email; use 'file' or 'logging' in local mode")
    if backend_api_url:
        host = (urlsplit(backend_api_url).hostname or "").lower()
        if host not in {h.strip("[]") for h in LOOPBACK_HOSTS}:
            problems.append(f"BACKEND_API_URL host '{host}' is not loopback")
    return problems


def frontend_api_url_violations(api_base_url: str) -> list[str]:
    host = (urlsplit(api_base_url).hostname or "").lower()
    if host not in {h.strip("[]") for h in LOOPBACK_HOSTS}:
        return [f"API_BASE_URL host '{host}' is not loopback; the local frontend may only call the local backend"]
    return []


def main(argv: Iterable[str]) -> int:
    """`python -m app.core.local_guard <env-file>`: exit 0 if the file is safe for local mode."""
    from dotenv import dotenv_values

    args = list(argv)
    if len(args) != 1:
        print("usage: python -m app.core.local_guard <env-file>", file=sys.stderr)
        return 2
    v = dotenv_values(args[0])
    problems = local_mode_violations(
        mongodb_uri=v.get("MONGODB_URI") or "",
        mongodb_db_name=v.get("MONGODB_DB_NAME") or "",
        environment=v.get("ENVIRONMENT") or "development",
        livekit_url=v.get("LIVEKIT_URL") or "",
        livekit_agent_name=v.get("LIVEKIT_AGENT_NAME") or "",
        email_delivery_mode=v.get("EMAIL_DELIVERY_MODE") or "",
        backend_api_url=v.get("BACKEND_API_URL") or "",
        allow_docker_local_mongo=(v.get("ALLOW_DOCKER_LOCAL_MONGO") or "").lower() in {"1", "true", "yes"},
    )
    if (v.get("LOCAL_DEV_MODE") or "").lower() not in {"1", "true", "yes"}:
        problems.append("LOCAL_DEV_MODE=true is not set in this env file")
    for p in problems:
        print(f"LOCAL GUARD: {p}", file=sys.stderr)
    if not problems:
        print("local guard: OK (loopback MongoDB, tamakkan_* database, no production LiveKit)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
