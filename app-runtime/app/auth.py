"""Firebase Admin initialisation + ID-token verification.

The Admin SDK uses the service-account key (server-side, full trust). Every
request that mutates user data must present a Firebase **ID token**; we verify it
here and derive the trusted `uid` — the client can never spoof another user.
"""
from __future__ import annotations

from functools import lru_cache

import firebase_admin
from firebase_admin import auth as fb_auth
from firebase_admin import credentials, firestore

from app.config import get_settings


@lru_cache
def _app():
    s = get_settings()
    cred = credentials.Certificate(s.firebase_credentials)
    return firebase_admin.initialize_app(cred)


@lru_cache
def db():
    _app()
    return firestore.client()


def verify_token(authorization: str | None) -> dict | None:
    """Verify an ``Authorization: Bearer <idToken>`` header.

    Returns the decoded token (contains ``uid``, ``email``) or None if missing
    / invalid / expired.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return None
    try:
        _app()
        return fb_auth.verify_id_token(token)
    except Exception:  # noqa: BLE001
        return None


def uid_from(authorization: str | None) -> str | None:
    decoded = verify_token(authorization)
    return decoded.get("uid") if decoded else None
