"""Seed realistic LOCAL test data through the real API (so seeding also exercises it).

Only runs against a LOCAL_DEV_MODE backend (the guard refuses anything else).
Writes credentials to .tamakkan/local/accounts.json (git-ignored). Never prints passwords.

    TAMAKKAN_ENV_FILE=<absolute path to backend/.env> python scripts/local/seed_local.py --api http://127.0.0.1:4675/api/v1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
ACCOUNTS = ROOT / ".tamakkan" / "local" / "accounts.json"
DOMAIN = "tamakkan.example.com"
# Every 5th day is an end-of-week check-in and cannot hold a lesson.
LESSON_DAYS = [1, 2, 3, 4, 6]

# label -> (role, full name). Emails are <label>@tamakkan.example.com.
PEOPLE = {
    "admin": ("root", "Local Administrator (Root)"),
    "centeradmin": ("administrator", "Local Center Admin"),
    "teacher1": ("teacher", "Teacher One"),
    "teacher2": ("teacher", "Teacher Two"),
    "student-standard-day1": ("student", "Standard Student - Day 1 (starts today)"),
    "student-standard-day4": ("student", "Standard Student - Day 4 (started 3 days ago)"),
    "student-standard-future": ("student", "Standard Student - Not started yet"),
    "student-pro": ("student", "Pro Student - custom calendar"),
    "student-noplan": ("student", "Student - no plan assigned"),
    "student-teacher2": ("student", "Teacher Two's Student"),
}


def call(api: str, method: str, path: str, body=None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(api + path, data=data, method=method, headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw.decode(errors="replace")


def must(result, what: str):
    status, payload = result
    if status >= 300:
        raise SystemExit(f"seed failed at {what}: HTTP {status} {str(payload)[:300]}")
    return payload


async def ensure_root(password: str) -> bool:
    """Create the Root account directly (the API has no way to bootstrap one)."""
    from app.core.database import close_db, init_db
    from app.core.security import hash_password
    from app.models.user import Role, User

    await init_db()
    try:
        email = f"admin@{DOMAIN}"
        if await User.find_one(User.email == email):
            return False
        await User(email=email, password_hash=hash_password(password), full_name=PEOPLE["admin"][1], role=Role.ROOT).insert()
        return True
    finally:
        await close_db()


def topic(title: str, level: str) -> dict:
    return {
        "title": title,
        "level": level,
        "youtube_urls": [],
        "warm_up_questions": [f"What do you already know about {title.lower()}?", "Why does this topic interest you?"],
        "useful_phrases": [],
        "avatar_prompt": f"You are Mariam, a friendly English speaking coach. Today's topic is: {title}.",
        "opening_line": f"Hi! Today we are talking about {title.lower()}. Are you ready?",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:4673/api/v1")
    ap.add_argument("--force", action="store_true", help="re-run even if accounts.json exists")
    args = ap.parse_args()
    api = args.api.rstrip("/")
    if not os.environ.get("TAMAKKAN_ENV_FILE"):
        raise SystemExit("TAMAKKAN_ENV_FILE must point at the V2 backend/.env")
    if ACCOUNTS.exists() and not args.force:
        print("seed: accounts.json exists - already seeded (use `reset` to start over)")
        return 0
    ACCOUNTS.parent.mkdir(parents=True, exist_ok=True)

    pw = {label: secrets.token_urlsafe(12) for label in PEOPLE}
    creds = {label: {"email": f"{label}@{DOMAIN}", "password": pw[label], "role": PEOPLE[label][0], "name": PEOPLE[label][1]} for label in PEOPLE}
    if not asyncio.run(ensure_root(pw["admin"])):
        raise SystemExit("seed: root account already exists in the database but accounts.json is missing; run `reset`")

    def login(label):
        p = must(call(api, "POST", "/auth/login", {"email": creds[label]["email"], "password": creds[label]["password"]}), f"login {label}")
        return p["access_token"]

    root_tok = login("admin")
    must(call(api, "POST", "/root/admins", {"email": creds["centeradmin"]["email"], "password": pw["centeradmin"], "full_name": creds["centeradmin"]["name"]}, root_tok), "create centeradmin")
    center_admin_tok = login("centeradmin")
    for label in ("teacher1", "teacher2"):
        must(call(api, "POST", "/teachers", {"email": creds[label]["email"], "password": pw[label], "full_name": creds[label]["name"]}, center_admin_tok), f"create {label}")
    t1 = login("teacher1")

    t2 = login("teacher2")
    sid = {}
    for label, (role, name) in PEOPLE.items():
        if role != "student":
            continue
        owner_tok = t2 if label == "student-teacher2" else t1
        u = must(call(api, "POST", "/users", {"email": creds[label]["email"], "password": pw[label], "full_name": name}, owner_tok), f"create {label}")
        sid[label] = u["id"]

    beginner = [must(call(api, "POST", "/topics", topic(t, "beginner"), t1), f"topic {t}")["id"] for t in
                ["Introducing Yourself", "Daily Routine", "Food and Restaurants", "Talking about Music", "My Family"]]
    inter = [must(call(api, "POST", "/topics", topic(t, "intermediate"), t1), f"topic {t}")["id"] for t in
             ["Travel and Cultures", "Technology in Daily Life"]]

    std = must(call(api, "POST", "/calendar-templates", {"name": "Beginner - 5 day starter", "kind": "standard", "level": "beginner",
               "days": [{"day": d, "topic_ids": [tid]} for d, tid in zip(LESSON_DAYS, beginner)]}, t1), "standard template")
    must(call(api, "POST", f"/calendar-templates/{std['id']}/activate", None, t1), "activate standard template")
    pro = must(call(api, "POST", "/calendar-templates", {"name": "Pro - mixed levels", "kind": "custom",
               "days": [{"day": 1, "topic_ids": [beginner[0]]}, {"day": 2, "topic_ids": [inter[0]]}, {"day": 3, "topic_ids": [inter[1]]}]}, t1), "pro template")

    today = datetime.now(timezone.utc).date()
    plans = {
        "student-standard-day1": {"plan": "standard", "assigned_level": "beginner", "start_date": str(today)},
        "student-standard-day4": {"plan": "standard", "assigned_level": "beginner", "start_date": str(today - timedelta(days=3))},
        "student-standard-future": {"plan": "standard", "assigned_level": "beginner", "start_date": str(today + timedelta(days=14))},
        "student-pro": {"plan": "pro", "assigned_plan_template_id": pro["id"], "start_date": str(today - timedelta(days=1))},
    }
    for label, body in plans.items():
        must(call(api, "POST", f"/users/{sid[label]}/assign-plan", body, t1), f"assign plan {label}")

    when = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0).isoformat()
    must(call(api, "POST", "/classes", {"kind": "group", "title": "Local group speaking class", "scheduled_at": when, "duration_minutes": 45,
         "meet_link": "https://meet.example.com/local-test", "participant_ids": [sid["student-standard-day1"], sid["student-standard-day4"]]}, t1), "live class")

    ACCOUNTS.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    print(f"seed: done - {len(PEOPLE)} accounts, 7 lessons, 2 calendar templates, 1 group class.")
    print(f"seed: credentials saved to {ACCOUNTS} (git-ignored)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
