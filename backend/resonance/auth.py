from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import secrets

from fastapi import Cookie, Depends, HTTPException
from sqlalchemy.orm import Session

from . import models as m
from .db import get_db

COOKIE = "resonance_session"
SESSION_DAYS = 14


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
        cand = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(cand.hex(), dk)
    except Exception:
        return False


def create_session(db: Session, user: m.User) -> str:
    token = secrets.token_urlsafe(32)
    db.add(m.AuthSession(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=user.id,
                         expires_at=m.now() + dt.timedelta(days=SESSION_DAYS)))
    return token


def current_user(db: Session = Depends(get_db), resonance_session: str | None = Cookie(default=None)) -> m.User:
    if not resonance_session:
        raise HTTPException(401, "Not signed in")
    s = db.get(m.AuthSession, hashlib.sha256(resonance_session.encode()).hexdigest())
    if not s:
        raise HTTPException(401, "Session expired")
    exp = s.expires_at if s.expires_at.tzinfo else s.expires_at.replace(tzinfo=dt.timezone.utc)
    if exp < m.now():
        db.delete(s)
        db.commit()
        raise HTTPException(401, "Session expired")
    user = db.get(m.User, s.user_id)
    if not user:
        raise HTTPException(401, "Not signed in")
    return user


def owned_project(project_id: str, user: m.User = Depends(current_user), db: Session = Depends(get_db)) -> m.Project:
    p = db.get(m.Project, project_id)
    if not p or p.owner_id != user.id:  # 404, not 403: do not reveal that someone else's project exists
        raise HTTPException(404, "Project not found")
    return p
