import hashlib
import hmac
import secrets

from fastapi import Depends, HTTPException, Request

from app import clock, db


def hash_password(password: str) -> str:
    salt = secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"{salt}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    salt, _, expected = stored.partition("$")
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return hmac.compare_digest(digest.hex(), expected)


def current_user(request: Request) -> dict:
    token = request.cookies.get("session")
    row = db.one("SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token=?", (token,)) if token else None
    if not row:
        raise HTTPException(401, "Please log in")
    return row


def require(*roles: str):
    def check(user: dict = Depends(current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(403, "Not allowed for your role")
        return user
    return check


def login(username: str, password: str) -> str | None:
    user = db.one("SELECT * FROM users WHERE username=?", (username,))
    if not user or not check_password(password, user["pw_hash"]):
        return None
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO sessions(token, user_id, created_at) VALUES (?,?,?)",
               (token, user["id"], clock.iso(clock.now())))
    return token
