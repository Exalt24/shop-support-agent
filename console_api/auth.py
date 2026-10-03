"""Passwords, signed session cookies, a login rate limit, and the per-store membership check.

Two deliberate choices:
  * an unknown email and a wrong password take the same time and give the same answer (bcrypt runs against a
    dummy hash when no user exists), so the login form is not an oracle for which emails have accounts;
  * a store you are not a member of answers 404, never 403, for the same reason.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import bcrypt
from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import db
from .secrets_store import session_secret

COOKIE = "console_session"
SESSION_SECONDS = 12 * 3600
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt(rounds=10)).decode()

# Failed logins per (email, client) in a sliding window. In-memory: correct for one process, and the honest
# limit is that it resets on restart and is not shared across workers (a shared store is the production step).
MAX_FAILS, WINDOW = 5, 600
_fails: dict[str, list[float]] = {}


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=10)).decode()


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(session_secret(), salt="console-session")


def make_session(user_id: str) -> str:
    return _serializer().dumps({"uid": user_id})


def read_session(token: str) -> str | None:
    try:
        return _serializer().loads(token, max_age=SESSION_SECONDS)["uid"]
    except (BadSignature, SignatureExpired, KeyError):
        return None


def _rate_key(email: str, client: str) -> str:
    return "%s|%s" % (email.lower(), client)


def check_not_locked(email: str, client: str) -> None:
    now = time.time()
    recent = [t for t in _fails.get(_rate_key(email, client), []) if now - t < WINDOW]
    _fails[_rate_key(email, client)] = recent
    if len(recent) >= MAX_FAILS:
        raise HTTPException(429, "Too many attempts. Try again in a few minutes.")


def record_failure(email: str, client: str) -> None:
    _fails.setdefault(_rate_key(email, client), []).append(time.time())


def clear_failures(email: str, client: str) -> None:
    _fails.pop(_rate_key(email, client), None)


def authenticate(email: str, password: str) -> dict | None:
    with db.tx() as c:
        row = c.execute("select * from login_lookup(%s)", (email,)).fetchone()
    stored = row["password_hash"] if row else _DUMMY_HASH
    ok = bcrypt.checkpw(password.encode(), stored.encode())
    return {"id": str(row["id"]), "name": row["name"]} if (row and ok) else None


def current_user_id(request: Request) -> str:
    token = request.cookies.get(COOKIE)
    uid = read_session(token) if token else None
    if not uid:
        raise HTTPException(401, "Please sign in.")
    return uid


@dataclass(frozen=True)
class Member:
    user_id: str
    tenant_id: str
    slug: str
    store_name: str
    role: str


def member(slug: str, request: Request, uid: str = Depends(current_user_id)) -> Member:
    with db.tx(user_id=uid) as c:
        row = c.execute(
            "select t.id, t.slug, t.name, m.role from tenants t join memberships m on m.tenant_id = t.id "
            "where t.slug = %s and m.user_id = %s", (slug, uid)).fetchone()
    if not row:
        raise HTTPException(404, "Store not found.")
    return Member(uid, str(row["id"]), row["slug"], row["name"], row["role"])


def admin_only(m: Member = Depends(member)) -> Member:
    if m.role != "admin":
        raise HTTPException(403, "Admins only.")
    return m


ALLOWED_ORIGINS = {o.strip() for o in os.environ.get(
    "CONSOLE_ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",") if o.strip()}


def csrf_guard(request: Request) -> None:
    """State-changing requests must come from our own pages: a custom header a cross-site form cannot set, and an
    Origin that is ours when the browser sends one. SameSite=Lax on the cookie is the third layer."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if request.headers.get("x-requested-with") != "console":
            raise HTTPException(403, "Missing request header.")
        origin = request.headers.get("origin")
        if origin and origin not in ALLOWED_ORIGINS:
            raise HTTPException(403, "Origin not allowed.")
