import hashlib
import hmac
import secrets
from typing import Optional

from fastapi import Cookie, HTTPException

from . import db


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=2**14, r=8, p=1)
    return f"{salt}${digest.hex()}"


def check_password(password, stored):
    salt = stored.split("$")[0]
    return hmac.compare_digest(hash_password(password, salt), stored)


def new_session(user_id):
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO sessions(token, user_id) VALUES(?, ?)", (token, user_id))
    return token


def current_user(session: Optional[str] = Cookie(default=None)):
    rows = []
    if session:
        rows = db.query(
            "SELECT u.id, u.email, u.phone FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token = ?",
            (session,),
        )
    if not rows:
        raise HTTPException(status_code=401, detail="not logged in")
    return rows[0]
