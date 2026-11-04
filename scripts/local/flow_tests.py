"""Application-flow checks against the RUNNING local stack (web BFF + API + local MongoDB).

    python scripts/local/flow_tests.py --web http://127.0.0.1:5188 --api http://127.0.0.1:4675/api/v1

Uses the seeded accounts in .tamakkan/local/accounts.json. Creates a few throwaway
records (prefixed "flowtest-") on each run; `reset` clears them. Exit code 1 on any FAIL.
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ACCOUNTS = ROOT / ".tamakkan" / "local" / "accounts.json"
MAIL_DIR = ROOT / ".tamakkan" / "local" / "mail-preview"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail and not ok else ""))
    return bool(ok)


def http(method, url, body=None, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            raw = r.read()
            try:
                return r.status, (json.loads(raw) if raw else None), r.headers
            except ValueError:  # HTML pages etc.
                return r.status, raw.decode(errors="replace"), r.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw), e.headers
        except Exception:
            return e.code, raw.decode(errors="replace"), e.headers


class Api:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def login(self, email, password):
        s, p, _ = http("POST", self.base + "/auth/login", {"email": email, "password": password})
        return s, p

    def req(self, method, path, token=None, body=None):
        h = {"Authorization": f"Bearer {token}"} if token else {}
        s, p, _ = http(method, self.base + path, body, h)
        return s, p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--web", default="http://127.0.0.1:5186")
    ap.add_argument("--api", default="http://127.0.0.1:4673/api/v1")
    a = ap.parse_args()
    if not ACCOUNTS.exists():
        print("no accounts.json - run `tamakkan-local.ps1 start` first")
        return 2
    acct = json.loads(ACCOUNTS.read_text(encoding="utf-8"))
    api = Api(a.api)
    web = a.web.rstrip("/")
    tok: dict[str, str] = {}

    print("\n== 1. Pages render (web)")
    for path in ("/", "/admin", "/student", "/verify-email"):
        s, _, _ = http("GET", web + path)
        check(f"GET {path} -> 200", s == 200, f"got {s}")

    print("\n== 2. Authentication")
    for label, v in acct.items():
        s, p = api.login(v["email"], v["password"])
        if check(f"login {label}", s == 200 and p and "access_token" in p, f"HTTP {s}"):
            tok[label] = p["access_token"]
    s, _ = api.login(acct["teacher1"]["email"], "wrong-password-123")
    check("wrong password rejected (401)", s == 401, f"got {s}")
    s, p, hd = http("POST", web + "/api/auth/login", {"email": acct["teacher1"]["email"], "password": acct["teacher1"]["password"]})
    cookies = [v.split(";")[0] for k, v in hd.items() if k.lower() == "set-cookie"]
    check("web login sets access + refresh cookies", s == 200 and len(cookies) >= 2, f"HTTP {s}, {len(cookies)} cookies")
    ck = {"Cookie": "; ".join(cookies), "Origin": web}
    s, p, _ = http("GET", web + "/api/backend/me", None, ck)
    check("web BFF proxies authenticated /me", s == 200 and p and p.get("email") == acct["teacher1"]["email"], f"HTTP {s}")

    print("\n== 3. Account creation")
    t1, admin = tok["teacher1"], tok["admin"]
    email = f"flowtest-{secrets.token_hex(3)}@tamakkan.example.com"
    s, p = api.req("POST", "/users", t1, {"email": email, "password": "Flowtest-Pass-1", "full_name": "Flow Test Student"})
    made = check("teacher creates a student", s == 200, f"HTTP {s} {p}")
    sid = p["id"] if made else None
    s, _ = api.login(email, "Flowtest-Pass-1")
    check("new student can sign in", s == 200, f"HTTP {s}")
    s, _ = api.req("POST", "/users", t1, {"email": email, "password": "Flowtest-Pass-1", "full_name": "Dup"})
    check("duplicate email rejected", s in (400, 409, 422), f"got {s}")
    s, _ = api.req("POST", "/root/admins", admin, {"email": f"flowtest-t-{secrets.token_hex(3)}@tamakkan.example.com", "password": "Flowtest-Pass-1", "full_name": "Flow Teacher"})
    check("admin (root) creates a teacher", s == 200, f"got {s}")

    print("\n== 4. Lessons: create, edit, delete")
    s, p = api.req("POST", "/topics", t1, {"title": "Flowtest lesson", "level": "beginner", "warm_up_questions": ["Q1?"], "avatar_prompt": "You are Mariam.", "opening_line": "Hello!"})
    made = check("teacher creates a lesson", s in (200, 201), f"HTTP {s} {p}")
    tid = p["id"] if made else None
    if tid:
        s, p = api.req("PATCH", f"/topics/{tid}", t1, {"title": "Flowtest lesson (edited)"})
        check("teacher edits the lesson", s == 200 and p["title"].endswith("(edited)"), f"HTTP {s}")
        s, p = api.req("GET", f"/topics/{tid}", t1)
        check("edit persisted", s == 200 and p["title"].endswith("(edited)"), f"HTTP {s}")
        s, _ = api.req("DELETE", f"/topics/{tid}", t1)
        check("teacher deletes the lesson", s == 204, f"got {s}")
    s, p = api.req("GET", "/topics", t1)
    check("teacher lists seeded lessons (>=7)", s == 200 and len(p) >= 7, f"HTTP {s}")

    print("\n== 5. Calendar and classes")
    s, p = api.req("GET", "/calendar-templates", t1)
    check("teacher lists calendar templates (standard + custom)", s == 200 and {"standard", "custom"} <= {x["kind"] for x in p}, f"HTTP {s}")
    when = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0).isoformat()
    s, p = api.req("GET", "/users", t1)
    stu = {u["email"]: u["id"] for u in p} if s == 200 else {}
    s, p = api.req("POST", "/classes", t1, {"kind": "one_to_one", "title": "Flowtest 1:1", "scheduled_at": when, "duration_minutes": 30,
                   "meet_link": "https://meet.example.com/flowtest", "participant_ids": [stu[acct["student-pro"]["email"]]]})
    made = check("teacher schedules a live class", s in (200, 201), f"HTTP {s} {p}")
    cid = p["id"] if made else None
    s, p = api.req("GET", "/classes/me", tok["student-pro"])
    check("student sees their scheduled class", s == 200 and any(c["title"] == "Flowtest 1:1" for c in p), f"HTTP {s}")
    if cid:
        s, _ = api.req("PATCH", f"/classes/{cid}", t1, {"title": "Flowtest 1:1 (moved)"})
        check("teacher reschedules/edits the class", s == 200, f"got {s}")
        s, _ = api.req("DELETE", f"/classes/{cid}", t1)
        check("teacher cancels the class", s == 204, f"got {s}")

    print("\n== 6. Student access (entitlement scenarios)")
    s, p = api.req("GET", "/topics", tok["student-standard-day1"])
    open1 = [t for t in p if t.get("lesson_status") not in (None, "locked")] if s == 200 else []
    check("Standard day-1 student: lessons listed, day 1 open", s == 200 and len(open1) >= 1, f"HTTP {s}")
    check("Standard day-1 student: later days locked", s == 200 and len([t for t in p if t.get("lesson_status") == "locked"]) >= 1, f"statuses={[t.get('lesson_status') for t in p] if s == 200 else s}")
    s, p = api.req("GET", "/topics", tok["student-standard-day4"])
    check("Standard day-4 student: more lessons open than day-1", s == 200 and len([t for t in p if t.get("lesson_status") not in (None, "locked")]) > len(open1), f"HTTP {s}")
    s, p = api.req("GET", "/topics", tok["student-standard-future"])
    check("Not-started student: everything locked", s == 200 and all(t.get("lesson_status") in (None, "locked") for t in p), f"HTTP {s}")
    s, p = api.req("GET", "/topics", tok["student-pro"])
    check("Pro student: custom-calendar lessons visible", s == 200 and len(p) >= 1, f"HTTP {s}")
    s, p = api.req("GET", "/topics", tok["student-noplan"])
    check("No-plan student: no lessons / access denied", (s == 200 and len(p) == 0) or s == 403, f"HTTP {s}")

    print("\n== 7. Dashboards")
    for label in ("student-standard-day1", "student-pro"):
        s, _ = api.req("GET", "/me/progress", tok[label])
        s2, _ = api.req("GET", "/me/challenge-calendar", tok[label])
        s3, _ = api.req("GET", "/me/analytics", tok[label])
        check(f"{label}: progress / calendar / analytics", (s, s2, s3) == (200, 200, 200), f"{s},{s2},{s3}")
    codes = [api.req("GET", p_, t1)[0] for p_ in ("/users", "/users/alerts", "/sessions", "/classes", "/calendar-templates")]
    check("teacher dashboards (roster, alerts, sessions, classes, calendars)", codes == [200] * 5, str(codes))
    s, p = api.req("GET", "/me", tok["student-noplan"])
    check("student /me profile", s == 200 and p["role"] == "student", f"HTTP {s}")

    print("\n== 8. Password reset and change")
    if sid:
        s, _ = api.req("POST", f"/users/{sid}/reset-password", t1, {"new_password": "Flowtest-Reset-2"})
        check("teacher resets a student's password", s in (200, 204), f"got {s}")
        old, _ = api.login(email, "Flowtest-Pass-1")
        new, _ = api.login(email, "Flowtest-Reset-2")
        check("old password fails, new works", old == 401 and new == 200, f"old={old} new={new}")
    s, _ = api.req("POST", "/me/change-password", tok["student-noplan"], {"current_password": acct["student-noplan"]["password"], "new_password": "Whatever-123"})
    check("students cannot self-change password (by design -> 403)", s == 403, f"got {s}")
    s, _ = api.req("POST", "/me/request-password-reset", tok["student-noplan"])
    check("student requests a password reset from their teacher", s == 200, f"got {s}")
    s, p = api.req("GET", "/users/alerts", t1)
    check("teacher sees the reset request in alerts", s == 200 and len(p) >= 1, f"HTTP {s}")
    tp = acct["teacher1"]["password"]
    s, _ = api.req("POST", "/me/change-password", t1, {"current_password": tp, "new_password": tp + "x"})
    ok1, _ = api.login(acct["teacher1"]["email"], tp + "x")
    check("teacher changes own password", s == 200 and ok1 == 200, f"change={s} login={ok1}")
    s, p = api.login(acct["teacher1"]["email"], tp + "x")
    if s == 200:
        api.req("POST", "/me/change-password", p["access_token"], {"current_password": tp + "x", "new_password": tp})
    ok2, _ = api.login(acct["teacher1"]["email"], tp)
    check("teacher password restored for later runs", ok2 == 200, f"got {ok2}")
    before = set(MAIL_DIR.glob("*.eml")) if MAIL_DIR.exists() else set()
    s, _, _ = http("POST", a.api.rstrip("/") + "/auth/password-reset/request", {"email": acct["teacher2"]["email"]})
    time.sleep(1.5)
    new_mail = sorted(set(MAIL_DIR.glob("*.eml")) - before) if MAIL_DIR.exists() else []
    token = None
    if new_mail:
        import quopri

        body = quopri.decodestring(new_mail[-1].read_bytes()).decode(errors="replace")
        m = re.search(r"reset your password:\s*(\S+)", body)
        token = m.group(1) if m else None
    check("teacher password-reset email captured locally (.eml)", s == 200 and token is not None, f"HTTP {s}, mail files={len(new_mail)}")
    if token:
        newpw = "Teacher2-New-Pass-9"
        s, _, _ = http("POST", a.api.rstrip("/") + "/auth/password-reset/confirm", {"token": token, "new_password": newpw})
        ok_new, _ = api.login(acct["teacher2"]["email"], newpw)
        ok_old, _ = api.login(acct["teacher2"]["email"], acct["teacher2"]["password"])
        check("reset link sets new password; old rejected", s == 200 and ok_new == 200 and ok_old == 401, f"confirm={s} new={ok_new} old={ok_old}")
        s, _, _ = http("POST", a.api.rstrip("/") + "/auth/password-reset/confirm", {"token": token, "new_password": newpw + "z"})
        check("reset token is single-use", s >= 400, f"got {s}")
        # restore the seeded password so credentials in accounts.json stay valid
        s, p = api.login(acct["teacher2"]["email"], newpw)
        if s == 200:
            api.req("POST", "/me/change-password", p["access_token"], {"current_password": newpw, "new_password": acct["teacher2"]["password"]})

    print("\n== 9. Logout and refresh tokens")
    s, p = api.login(acct["student-pro"]["email"], acct["student-pro"]["password"])
    rt = p["refresh_token"]
    s, p2, _ = http("POST", a.api.rstrip("/") + "/auth/refresh", {"refresh_token": rt})
    check("refresh issues new tokens", s == 200 and "access_token" in p2, f"HTTP {s}")
    rt2 = p2["refresh_token"] if s == 200 else rt
    s, _, _ = http("POST", a.api.rstrip("/") + "/auth/logout", {"refresh_token": rt2})
    check("logout succeeds", s in (200, 204), f"got {s}")
    s, _, _ = http("POST", a.api.rstrip("/") + "/auth/refresh", {"refresh_token": rt2})
    check("refresh token unusable after logout", s in (401, 403), f"got {s}")
    s, _, _ = http("POST", a.api.rstrip("/") + "/auth/refresh", {"refresh_token": rt})
    check("rotated (old) refresh token rejected", s in (401, 403), f"got {s}")
    s, _, hd = http("POST", web + "/api/auth/logout", None, ck)
    cleared = [v for k, v in hd.items() if k.lower() == "set-cookie"]
    check("web logout clears cookies", s in (200, 204) and cleared, f"HTTP {s}")

    print("\n== 10. Role permissions")
    check("student cannot list users (403)", api.req("GET", "/users", tok["student-pro"])[0] == 403)
    check("student cannot create topics (403)", api.req("POST", "/topics", tok["student-pro"], {"title": "x", "level": "beginner"})[0] == 403)
    check("teacher cannot create teachers (403)", api.req("POST", "/root/admins", t1, {"email": "x@tamakkan.example.com", "password": "Passw0rd-xx", "full_name": "x"})[0] == 403)
    check("root cannot use teacher endpoints (flat roles)", api.req("GET", "/users", admin)[0] == 403)
    check("unauthenticated request rejected", api.req("GET", "/me")[0] in (401, 403))
    check("garbage token rejected", api.req("GET", "/me", "not-a-token")[0] in (401, 403))
    s, p = api.req("GET", "/users", tok["teacher2"])
    teacher1_emails = {info["email"] for label, info in acct.items() if info["role"] == "student" and label != "student-teacher2"}
    seen_emails = {u["email"] for u in p} if s == 200 and isinstance(p, list) else set()
    check("teacher2 sees only their own student", s == 200 and not (seen_emails & teacher1_emails)
          and acct["student-teacher2"]["email"] in seen_emails, f"HTTP {s}")
    if stu:
        s, _ = api.req("POST", f"/users/{stu[acct['student-noplan']['email']]}/assign-plan", tok["teacher2"], {"plan": "standard", "assigned_level": "beginner"})
        check("teacher2 cannot assign plans to teacher1's student", s in (403, 404), f"got {s}")

    print("\n== 11. Mariam disabled by default (no LiveKit needed)")
    _, tl = api.req("GET", "/topics", tok["student-standard-day4"])
    open_t = next((t for t in tl if t.get("lesson_status") not in (None, "locked")), tl[0])
    s, p = api.req("POST", "/sessions/start", tok["student-standard-day4"], {"topic_id": open_t["id"]})
    check("start-session fails cleanly (503, no crash)", s == 503, f"HTTP {s} {str(p)[:120]}")
    s, _ = api.req("GET", "/sessions/me", tok["student-standard-day4"])
    check("student session history endpoint works", s == 200, f"got {s}")

    print("\n== 12. H1 through the real BFF (two users refreshing at once)")
    import concurrent.futures as cf

    def web_session(label):
        s, p, hd = http("POST", web + "/api/auth/login", {"email": acct[label]["email"], "password": acct[label]["password"]})
        c = {k.split("=")[0]: k.split("=", 1)[1] for k in [v.split(";")[0] for kk, v in hd.items() if kk.lower() == "set-cookie"]}
        return c

    ca, cb = web_session("student-pro"), web_session("student-standard-day1")
    pref = next((k[: -len("_access_token")] for k in ca if k.endswith("_access_token")), "tamakkanlocal")

    def whoami(cs):  # drop the access token so the BFF must refresh with the refresh token
        h = {"Cookie": f"{pref}_refresh_token={cs[pref + '_refresh_token']}", "Origin": web}
        s, p, _ = http("GET", web + "/api/backend/me", None, h)
        return s, (p or {}).get("email") if isinstance(p, dict) else None

    with cf.ThreadPoolExecutor(2) as ex:
        ra, rb = ex.map(whoami, [ca, cb])
    check("each user keeps their own identity on concurrent refresh",
          ra == (200, acct["student-pro"]["email"]) and rb == (200, acct["student-standard-day1"]["email"]), f"{ra} {rb}")

    ok = sum(1 for _, o, _ in RESULTS if o)
    print(f"\n==== {ok}/{len(RESULTS)} checks passed ====")
    for n, o, d in RESULTS:
        if not o:
            print(f"FAIL: {n}  ({d})")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
