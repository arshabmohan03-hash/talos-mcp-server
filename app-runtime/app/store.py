"""Firestore persistence for per-user, PRIVATE chat history.

Data model — everything lives under the user's own UID, so one user can never
read another's data (the server only ever queries within `users/{uid}`):

    users/{uid}/chats/{chatId}                   {title, created_at, updated_at}
    users/{uid}/chats/{chatId}/messages/{msgId}  {role, content, ts}

All functions are synchronous (the Firestore SDK is blocking) — call them via
`asyncio.to_thread(...)` from async endpoints.
"""
from __future__ import annotations

from firebase_admin import firestore

from app.auth import db


def _chats(uid: str):
    return db().collection("users").document(uid).collection("chats")


def _ts(v) -> str | None:
    try:
        return v.isoformat() if v else None
    except Exception:  # noqa: BLE001
        return None


def list_chats(uid: str, limit: int = 50) -> list[dict]:
    q = (_chats(uid)
         .order_by("updated_at", direction=firestore.Query.DESCENDING)
         .limit(limit))
    out = []
    for d in q.stream():
        c = d.to_dict() or {}
        out.append({
            "id": d.id,
            "title": c.get("title") or "New chat",
            "updated_at": _ts(c.get("updated_at")),
        })
    return out


def create_chat(uid: str, title: str = "New chat") -> str:
    ref = _chats(uid).document()
    ref.set({
        "title": (title or "New chat")[:80],
        "created_at": firestore.SERVER_TIMESTAMP,
        "updated_at": firestore.SERVER_TIMESTAMP,
    })
    return ref.id


def get_messages(uid: str, chat_id: str, limit: int = 300) -> list[dict]:
    q = (_chats(uid).document(chat_id).collection("messages")
         .order_by("ts").limit(limit))
    msgs = []
    for m in q.stream():
        d = m.to_dict() or {}
        msgs.append({"role": d.get("role"), "content": d.get("content")})
    return msgs


def add_message(uid: str, chat_id: str, role: str, content: str) -> None:
    chat = _chats(uid).document(chat_id)
    chat.collection("messages").add({
        "role": role, "content": content, "ts": firestore.SERVER_TIMESTAMP,
    })
    update = {"updated_at": firestore.SERVER_TIMESTAMP}
    # title the chat from its first user message
    if role == "user":
        data = chat.get().to_dict() or {}
        if not data.get("title") or data.get("title") == "New chat":
            update["title"] = content[:60]
    chat.set(update, merge=True)


def delete_chat(uid: str, chat_id: str) -> None:
    chat = _chats(uid).document(chat_id)
    for m in chat.collection("messages").stream():
        m.reference.delete()
    chat.delete()


# --------------------------- scan history ---------------------------
def _scans(uid: str):
    return db().collection("users").document(uid).collection("scans")


def save_scan(uid: str, report: dict) -> str:
    ref = _scans(uid).document()
    ref.set({
        "target": report.get("target"),
        "grade": report.get("grade"),
        "score": report.get("score"),
        "report": report,
        "created_at": firestore.SERVER_TIMESTAMP,
    })
    return ref.id


def list_scans(uid: str, limit: int = 40) -> list[dict]:
    q = (_scans(uid)
         .order_by("created_at", direction=firestore.Query.DESCENDING)
         .limit(limit))
    out = []
    for d in q.stream():
        c = d.to_dict() or {}
        out.append({
            "id": d.id,
            "target": c.get("target"),
            "grade": c.get("grade"),
            "score": c.get("score"),
            "report": c.get("report"),
        })
    return out


def delete_scan(uid: str, scan_id: str) -> None:
    _scans(uid).document(scan_id).delete()


# --------------------------- academy (learning progress) ---------------------------
# One document per user holds the whole Academy state blob (xp, skills, badges, …).
def get_academy(uid: str) -> dict:
    doc = (db().collection("users").document(uid)
           .collection("academy").document("state").get())
    return (doc.to_dict() or {}).get("state", {}) if doc.exists else {}


def save_academy(uid: str, state: dict) -> None:
    (db().collection("users").document(uid)
     .collection("academy").document("state")
     .set({"state": state, "updated_at": firestore.SERVER_TIMESTAMP}))
