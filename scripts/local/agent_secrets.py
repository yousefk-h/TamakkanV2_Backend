"""Build the COMPLETE secret set for the hosted DEV Mariam agent, or refuse.

`lk agent update-secrets --overwrite` REPLACES the agent's whole secret set with the file's contents.
Sending a partial file therefore deletes the missing secrets (this took the OpenAI key away from the dev
agent once). This module is the single gate: it writes the file only when all nine required secrets are
present and non-empty, and prints names only - never values.

The agent's own env file (`livekit_agent/.env`) is the only source of the agent's secrets. The backend's env
file (`backend/.env`) is read ONLY to prove that the two components agree on every value they must share
(internal callback key, SpatialReal app, LiveKit project, agent name) - a mismatch refuses the sync.

    python agent_secrets.py --agent livekit_agent/.env --backend .env \
        --tunnel-url https://x.trycloudflare.com --out <tmp file>
    python agent_secrets.py --agent livekit_agent/.env --backend .env --check      # consistency only
Exit 0 = ok / file written; exit 2 = refused, NOTHING written.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Everything the hosted agent needs, all taken from the agent's own env file. Order is the order they are written.
REQUIRED_FROM_AGENT = (
    "OPENAI_API_KEY",
    "ULTRAVOX_API_KEY",
    "SPATIALREAL_API_KEY",
    "SPATIALREAL_APP_ID",
    "SPATIALREAL_AVATAR_ID",
    "MARIAM_IDLE_TIMEOUT_SECONDS",
    "AGENT_INTERNAL_API_KEY",
    "LIVEKIT_AGENT_NAME",
)
REQUIRED_DERIVED = ("BACKEND_API_URL",)  # the temporary tunnel in front of the local callback gateway
REQUIRED_ALL = REQUIRED_FROM_AGENT + REQUIRED_DERIVED  # 9

# Values the backend and the agent must hold identically, or the avatar cannot work end to end.
# (LiveKit is checked only when the agent file carries it: a hosted agent receives it from LiveKit Cloud.)
SHARED_WITH_BACKEND = (
    "AGENT_INTERNAL_API_KEY",
    "SPATIALREAL_API_KEY",
    "SPATIALREAL_APP_ID",
    "SPATIALREAL_AVATAR_ID",
    "LIVEKIT_AGENT_NAME",
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
)

# "tamakkan-mariam-dev" is the PRODUCTION Avatar agent: a local run must never build a secret set for it.
FORBIDDEN_AGENT_NAMES = {"mariam-agent", "tamakkan-mariam-v2", "englishteacher", "tamakkan-mariam-dev"}
TUNNEL_RE = re.compile(r"^https://[a-z0-9-]+\.trycloudflare\.com$")


class SecretSetError(ValueError):
    """Raised with NAMES only (never values)."""


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m:
            values[m.group(1)] = m.group(2).strip()
    return values


def consistency_problems(agent: dict[str, str], backend: dict[str, str]) -> list[str]:
    """Names (never values) the two components disagree on."""
    problems: list[str] = []
    for name in SHARED_WITH_BACKEND:
        if name.startswith("LIVEKIT_") and name != "LIVEKIT_AGENT_NAME" and not agent.get(name):
            continue  # hosted agents get the LiveKit project from LiveKit Cloud
        if agent.get(name, "") != backend.get(name, ""):
            problems.append(f"{name} differs between backend/.env and livekit_agent/.env")
    return problems


def build_secret_set(agent: dict[str, str], backend: dict[str, str], tunnel_url: str) -> dict[str, str]:
    """Return the complete 9-secret dict, or raise SecretSetError naming what is wrong."""
    problems: list[str] = []
    result: dict[str, str] = {}
    for name in REQUIRED_FROM_AGENT:
        if not agent.get(name) or agent.get(name) == "GENERATE_ON_SETUP":
            problems.append(f"{name} is missing or empty in the agent env file")
        else:
            result[name] = agent[name]
    problems.extend(consistency_problems(agent, backend))
    if not TUNNEL_RE.match(tunnel_url or ""):
        problems.append("tunnel URL must be the temporary https://<random>.trycloudflare.com URL (BACKEND_API_URL)")
    else:
        result["BACKEND_API_URL"] = tunnel_url.rstrip("/") + "/api/v1"
    if result.get("LIVEKIT_AGENT_NAME") in FORBIDDEN_AGENT_NAMES:
        problems.append("LIVEKIT_AGENT_NAME is an existing (production/other) agent name")
    if problems:
        raise SecretSetError("; ".join(problems))
    missing = [n for n in REQUIRED_ALL if not result.get(n)]
    if missing:  # defensive: can never happen if the checks above are complete
        raise SecretSetError("incomplete set, missing: " + ", ".join(missing))
    return {name: result[name] for name in REQUIRED_ALL}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True, help="livekit_agent/.env")
    ap.add_argument("--backend", required=True, help="backend/.env (consistency check only)")
    ap.add_argument("--check", action="store_true", help="only verify backend/.env and agent .env agree; write nothing")
    ap.add_argument("--tunnel-url")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    try:
        agent, backend = read_env(Path(a.agent)), read_env(Path(a.backend))
        if a.check:
            problems = consistency_problems(agent, backend)
            if problems:
                raise SecretSetError("; ".join(problems))
            print("backend/.env and livekit_agent/.env agree on every shared value")
            return 0
        if not a.tunnel_url or not a.out:
            raise SecretSetError("--tunnel-url and --out are required (or use --check)")
        secrets = build_secret_set(agent, backend, a.tunnel_url)
    except (SecretSetError, OSError) as exc:
        print(f"agent-sync REFUSED (nothing written, nothing sent): {exc}", file=sys.stderr)
        return 2
    Path(a.out).write_text("".join(f"{k}={v}\n" for k, v in secrets.items()), encoding="utf-8", newline="\n")
    print("complete set ready (" + str(len(secrets)) + " secrets): " + ", ".join(secrets))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
