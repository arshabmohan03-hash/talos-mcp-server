"""FastAPI backend for the Talos AI Security Assistant.

Routes
  GET  /                 -> premium chat UI
  POST /api/chat         -> Server-Sent Events stream of agent events
  POST /api/scan         -> one-shot scan (JSON report)
  GET  /api/bruteforce   -> brute-force log analysis (JSON)
  GET  /api/health       -> health/config probe
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from pathlib import Path

from fastapi import (Depends, FastAPI, File, Form, Header, HTTPException,
                     Request, UploadFile)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse,
                               StreamingResponse)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import __version__, alerts, defense, mitigation, research, resources, sectools, secutils
from app.ai.agent import run_agent
from app.ai.client import chat_completion
from app.bruteforce import analyze_log
from app.config import get_settings
from app.scanner import scan

try:  # Firebase is optional — the app still runs in guest mode without it.
    from app import auth, store
    _FIREBASE_OK = True
except Exception:  # noqa: BLE001
    auth = store = None  # type: ignore
    _FIREBASE_OK = False

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
DS_DIR = Path(__file__).resolve().parent.parent / "design-system"
_FB_CONFIG_PATH = Path(__file__).resolve().parent.parent / "firebase_web_config.json"


def _firebase_web_config() -> dict | None:
    try:
        return json.loads(_FB_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None

app = FastAPI(title="Talos AI Security Assistant", version=__version__)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def defense_middleware(request: Request, call_next):
    """Self-defense: block known-bad IPs and reject attack-shaped requests."""
    ip = _client_ip(request)
    if defense.is_blocked(ip):
        return JSONResponse(
            {"error": "blocked", "reason": "Your IP is temporarily blocked by Talos self-defense."},
            status_code=403)
    hit = defense.check_request(ip, request.url.path, str(request.url.query),
                                request.headers.get("user-agent", ""))
    if hit:
        if hit.get("alert"):
            asyncio.create_task(asyncio.to_thread(
                alerts.send_security_alert, "[Talos] attack blocked", hit["alert"]))
        return JSONResponse(
            {"error": "blocked", "reason": f"Request blocked — {hit['type']} detected."},
            status_code=403)
    return await call_next(request)


@app.middleware("http")
async def ctf_header_middleware(request: Request, call_next):
    """Talos-is-a-CTF: a flag tucked into a response header (flag: header). Harmless."""
    response = await call_next(request)
    try:
        response.headers["X-Talos-Flag"] = "TALOS{h34d3r_h4ck3r}"
    except Exception:  # noqa: BLE001
        pass
    return response


_basic = HTTPBasic(auto_error=False)


def _admin_token() -> str:
    s = get_settings()
    return hashlib.sha256(f"{s.admin_username}:{s.admin_password}:talos".encode()).hexdigest()


def require_admin(request: Request,
                  credentials: HTTPBasicCredentials | None = Depends(_basic)) -> str:
    """Admin gate (separate id/password from user auth). Accepts a session cookie
    set after a Basic login on /admin, or HTTP Basic directly."""
    s = get_settings()
    if request.cookies.get("kadmin") == _admin_token():
        return s.admin_username
    if (credentials
            and secrets.compare_digest(credentials.username, s.admin_username)
            and secrets.compare_digest(credentials.password, s.admin_password)):
        return credentials.username
    raise HTTPException(status_code=401, detail="Admin login required",
                        headers={"WWW-Authenticate": "Basic"})


# ----------------------------- models -----------------------------
class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = Field(default_factory=list)
    chat_id: str | None = None
    mode: str = "assistant"  # "assistant" | "study"


class ScanRequest(BaseModel):
    url: str


class ResearchRequest(BaseModel):
    query: str
    source: str = "openalex"  # openalex | semantic_scholar | core | all
    year_from: int | None = None
    open_access: bool = False
    limit: int = 12


class MitigationRequest(BaseModel):
    ips: list[str] = Field(default_factory=list)
    threshold: int = 5


class ToolRunRequest(BaseModel):
    name: str
    args: dict = Field(default_factory=dict)


class AlertRequest(BaseModel):
    message: str
    subject: str = "Talos security alert"


class ScanSaveRequest(BaseModel):
    report: dict


class LoginAlertRequest(BaseModel):
    email: str = ""
    event: str = "signin"  # signin | signup | attempt


class UnblockRequest(BaseModel):
    ip: str


# ----------------------------- chat (SSE) -----------------------------
@app.post("/api/chat")
async def chat(req: ChatRequest, authorization: str | None = Header(default=None)):
    history = [
        {"role": m.role, "content": m.content}
        for m in req.history
        if m.role in ("user", "assistant") and m.content
    ]

    s = get_settings()
    uid = auth.uid_from(authorization) if _FIREBASE_OK else None
    if s.auth_required and not uid:
        return JSONResponse({"error": "Sign in required."}, status_code=401)

    # Persist the user's message immediately (only when signed in + a chat is open).
    if uid and req.chat_id:
        try:
            await asyncio.to_thread(store.add_message, uid, req.chat_id, "user", req.message)
        except Exception:  # noqa: BLE001
            pass

    async def event_stream():
        final_answer = ""
        try:
            async for event in run_agent(req.message, history, mode=req.mode):
                if event.get("type") == "message_done":
                    final_answer = event.get("data") or final_answer
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'data': str(e)})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        finally:
            if uid and req.chat_id and final_answer:
                try:
                    await asyncio.to_thread(
                        store.add_message, uid, req.chat_id, "assistant", final_answer)
                except Exception:  # noqa: BLE001
                    pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable proxy buffering
        },
    )


# ----------------------------- direct endpoints -----------------------------
@app.post("/api/scan")
async def api_scan(req: ScanRequest):
    report = await scan(req.url)
    return JSONResponse(report.to_compact_dict())


@app.get("/api/bruteforce")
async def api_bruteforce():
    return JSONResponse(await asyncio.to_thread(analyze_log))


@app.post("/api/research/search")
async def api_research(req: ResearchRequest):
    try:
        if req.source == "all":
            papers = await research.multi_search(
                req.query, sources=["openalex", "semantic_scholar", "core"],
                year_from=req.year_from, open_access=req.open_access, limit=req.limit)
        elif req.source == "core":
            papers = await research.search_core(
                req.query, year_from=req.year_from, limit=req.limit)
        else:
            papers = await research.run_search(
                req.source, req.query, year_from=req.year_from,
                open_access=req.open_access, limit=req.limit)
        return {"query": req.query, "source": req.source,
                "count": len(papers), "papers": papers}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)


class PasswordCheckRequest(BaseModel):
    password: str = ""

class HashRequest(BaseModel):
    text: str = ""
    algo: str = "sha256"

class JwtRequest(BaseModel):
    token: str = ""

class IpRequest(BaseModel):
    ip: str = ""


@app.post("/api/security/check-password")
async def api_check_password(req: PasswordCheckRequest):
    return await asyncio.to_thread(secutils.check_password_strength, req.password)


_ROAST_PROMPT = (
    "You are Talos's savage-but-friendly password roaster. Given ONLY metadata about a "
    "password (never the password itself), roast how weak or strong it is in 1-2 punchy, witty "
    "sentences. Be funny and a little brutal, PG-13, no slurs or real personal attacks — roast "
    "the PASSWORD, not the person. If it's genuinely strong, give a grudging sarcastic "
    "compliment. End with one tiny real tip. Plain text, max ~45 words."
)


def _fallback_roast(s: dict) -> str:
    """Deterministic witty roast so the feature still works if the AI is offline."""
    bits = s.get("entropy_bits") or 0
    pwned = s.get("pwned_count") or 0
    if pwned and pwned > 0:
        return (f"Oof — this one's turned up in {pwned:,} breaches. That's not a password, it's a "
                "public greeting. Pick something nobody's ever typed.")
    if bits < 28:
        return ("This password folds faster than a lawn chair. A toddler mashing the keyboard "
                "would take longer to guess. Add length and a few symbols, please.")
    if bits < 50:
        return ("The participation trophy of passwords — survives a lazy guess, not a real "
                "attack. Stretch it past 14 characters and mix in some chaos.")
    return ("Fine, this one actually has a spine — a cracker would get bored before it broke. "
            "Now stash it in a password manager and never type it again.")


@app.post("/api/security/roast-password")
async def api_roast_password(req: PasswordCheckRequest):
    """Comedy 'roast' of a password. Reuses the strength checker, then asks the AI to roast it
    from METADATA ONLY — the raw password is never sent to the model."""
    strength = await asyncio.to_thread(secutils.check_password_strength, req.password)
    meta = (f"length={strength.get('length')}, entropy={strength.get('entropy_bits')} bits, "
            f"strength={strength.get('strength')}, char_classes={strength.get('char_classes')}, "
            f"crack_time={strength.get('crack_time_offline_fast_hardware')}, "
            f"breached_count={strength.get('pwned_count')}, issues={strength.get('issues')}")
    roast = ""
    try:
        msg, _ = await chat_completion(
            [{"role": "system", "content": _ROAST_PROMPT},
             {"role": "user", "content": f"Roast a password with these stats: {meta}"}],
            max_tokens=800)   # gpt-oss is a reasoning model — leave room for tokens after reasoning
        roast = (getattr(msg, "content", "") or "").strip()
    except Exception:  # noqa: BLE001
        roast = ""
    return {**strength, "roast": roast or _fallback_roast(strength)}


@app.post("/api/security/hash")
async def api_hash(req: HashRequest):
    return secutils.hash_text(req.text, req.algo)


@app.post("/api/security/jwt")
async def api_jwt(req: JwtRequest):
    return secutils.decode_jwt(req.token)


@app.post("/api/security/ip")
async def api_ip(req: IpRequest):
    return await secutils.lookup_ip(req.ip)


@app.post("/api/mitigation")
async def api_mitigation(req: MitigationRequest):
    return mitigation.build_mitigation(req.ips, req.threshold)


@app.get("/api/tools")
async def api_tools_catalog():
    return {"tools": sectools.catalog()}


@app.post("/api/tools/run")
async def api_tools_run(req: ToolRunRequest):
    return {"result": await sectools.run(req.name, req.args)}


# ----------------------------- resource library -----------------------------
class ResourceSearchRequest(BaseModel):
    keywords: str = ""
    limit: int = 20


@app.post("/api/resources/upload")
async def api_resource_upload(file: UploadFile = File(...),
                              title: str | None = Form(default=None),
                              ocr: str | None = Form(default=None),
                              authorization: str | None = Header(default=None)):
    """Upload a book (PDF/TXT/MD) -> converted to page-numbered markdown + indexed.
    `ocr` ('true'/'false') overrides the global OCR setting for this upload."""
    s = get_settings()
    if s.auth_required and not _uid(authorization):
        return JSONResponse({"error": "Sign in required."}, status_code=401)
    data = await file.read()
    if len(data) > s.max_upload_mb * 1024 * 1024:
        return JSONResponse({"error": f"File exceeds {s.max_upload_mb} MB."}, status_code=413)
    ocr_flag = None if ocr is None else ocr.strip().lower() in ("1", "true", "on", "yes")
    try:
        meta = await asyncio.to_thread(
            resources.save_book, data, file.filename or "book.pdf", title=title, ocr=ocr_flag)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return JSONResponse(meta, status_code=200 if meta.get("duplicate") else 201)


@app.get("/api/resources")
async def api_resource_list():
    return {"books": await asyncio.to_thread(resources.list_books)}


@app.post("/api/resources/search")
async def api_resource_search(req: ResourceSearchRequest):
    return await asyncio.to_thread(resources.search_resources, req.keywords, req.limit)


@app.get("/api/resources/{book_id}/page/{page}")
async def api_resource_page(book_id: str, page: int):
    res = await asyncio.to_thread(resources.get_resource_page, book_id, page)
    return JSONResponse(res, status_code=404) if res.get("error") else res


@app.get("/api/resources/{book_id}/markdown")
async def api_resource_markdown(book_id: str):
    path = await asyncio.to_thread(resources.book_markdown_path, book_id)
    if not path:
        return JSONResponse({"error": "Book not found."}, status_code=404)
    return FileResponse(str(path), media_type="text/markdown", filename=f"{book_id}.md")


@app.delete("/api/resources/{book_id}")
async def api_resource_delete(book_id: str, authorization: str | None = Header(default=None)):
    s = get_settings()
    if s.auth_required and not _uid(authorization):
        return JSONResponse({"error": "Sign in required."}, status_code=401)
    res = await asyncio.to_thread(resources.delete_book, book_id)
    return JSONResponse(res, status_code=404) if res.get("error") else res


@app.post("/api/alert")
async def api_alert(req: AlertRequest):
    return await asyncio.to_thread(alerts.send_alert, req.message, req.subject)


@app.post("/api/security/login-alert")
async def api_login_alert(req: LoginAlertRequest, request: Request):
    # Normal logins are silent; we only email on a brute-force pattern.
    ip = _client_ip(request)
    success = req.event != "attempt"
    res = await asyncio.to_thread(defense.record_login, ip, req.email, success)
    if res.get("alert"):
        asyncio.create_task(asyncio.to_thread(
            alerts.send_security_alert, "[Talos] brute-force login blocked", res["alert"]))
    return {"recorded": True, "attack_detected": res.get("attack", False)}


@app.get("/admin")
async def admin_page(_: str = Depends(require_admin)):
    p = Path(__file__).resolve().parent.parent / "admin.html"  # outside /static
    if not p.exists():
        return JSONResponse({"error": "admin UI missing"}, status_code=404)
    resp = FileResponse(str(p))
    resp.set_cookie("kadmin", _admin_token(), httponly=True, samesite="strict", max_age=3600)
    return resp


@app.get("/api/admin/defense")
async def api_admin_defense(_: str = Depends(require_admin)):
    return defense.status()


@app.post("/api/admin/defense/unblock")
async def api_admin_unblock(req: UnblockRequest, _: str = Depends(require_admin)):
    defense.unblock(req.ip)
    return {"ok": True}


# ----------------------------- auth + chat history -----------------------------
@app.get("/api/auth/config")
async def auth_config():
    """Public Firebase web config for the client to initialise Auth."""
    s = get_settings()
    cfg = _firebase_web_config()
    return {"enabled": bool(cfg) and _FIREBASE_OK,
            "auth_required": s.auth_required, "config": cfg}


def _uid(authorization: str | None) -> str | None:
    return auth.uid_from(authorization) if _FIREBASE_OK else None


@app.get("/api/chats")
async def api_list_chats(authorization: str | None = Header(default=None)):
    uid = _uid(authorization)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"chats": await asyncio.to_thread(store.list_chats, uid)}


@app.post("/api/chats")
async def api_create_chat(authorization: str | None = Header(default=None)):
    uid = _uid(authorization)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"id": await asyncio.to_thread(store.create_chat, uid)}


@app.get("/api/chats/{chat_id}/messages")
async def api_chat_messages(chat_id: str, authorization: str | None = Header(default=None)):
    uid = _uid(authorization)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"messages": await asyncio.to_thread(store.get_messages, uid, chat_id)}


@app.delete("/api/chats/{chat_id}")
async def api_delete_chat(chat_id: str, authorization: str | None = Header(default=None)):
    uid = _uid(authorization)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    await asyncio.to_thread(store.delete_chat, uid, chat_id)
    return {"ok": True}


@app.post("/api/scans")
async def api_save_scan(req: ScanSaveRequest, authorization: str | None = Header(default=None)):
    uid = _uid(authorization)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"id": await asyncio.to_thread(store.save_scan, uid, req.report)}


@app.get("/api/scans")
async def api_list_scans(authorization: str | None = Header(default=None)):
    uid = _uid(authorization)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"scans": await asyncio.to_thread(store.list_scans, uid)}


@app.delete("/api/scans/{scan_id}")
async def api_delete_scan(scan_id: str, authorization: str | None = Header(default=None)):
    uid = _uid(authorization)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    await asyncio.to_thread(store.delete_scan, uid, scan_id)
    return {"ok": True}


# ----------------------------- academy (learning progress sync) -----------------------------
class AcademyState(BaseModel):
    state: dict = Field(default_factory=dict)


@app.get("/api/academy")
async def api_get_academy(authorization: str | None = Header(default=None)):
    """Signed-in users get their saved progress; guests get null (client uses localStorage)."""
    uid = _uid(authorization)
    if not uid:
        return {"state": None}
    return {"state": await asyncio.to_thread(store.get_academy, uid)}


@app.put("/api/academy")
async def api_put_academy(req: AcademyState, authorization: str | None = Header(default=None)):
    uid = _uid(authorization)
    if not uid:
        return {"ok": False, "guest": True}
    await asyncio.to_thread(store.save_academy, uid, req.state)
    return {"ok": True}


# ----------------------------- gamified: internet weather -----------------------------
@app.get("/api/intel/weather")
async def api_intel_weather():
    """Playful 'internet weather' — a live threat-level read from the CISA KEV feed."""
    from app.scanner import threatfeeds
    try:
        kev = await threatfeeds.kev_catalog()
    except Exception:  # noqa: BLE001
        kev = {}
    items = sorted(kev.items(), key=lambda kv: str(kv[1].get("date_added", "")), reverse=True)
    recent = [{"cve": cid, "vendor": v.get("vendor"), "product": v.get("product"),
               "name": v.get("name"), "date_added": v.get("date_added"),
               "ransomware": v.get("ransomware")}
              for cid, v in items[:8]]
    total = len(kev)
    ransom = sum(1 for v in kev.values()
                 if str(v.get("ransomware", "")).strip().lower() not in ("", "unknown"))
    if not total:
        level = "offline"
    elif ransom > total * 0.15:
        level = "stormy"
    elif ransom > 0:
        level = "cloudy"
    else:
        level = "clear"
    forecast = {
        "clear": "☀️ Calm seas on the wire — patch at your leisure.",
        "cloudy": "⛅ Scattered exploits — keep your shields up.",
        "stormy": "⛈️ Heavy ransomware weather — patch known-exploited bugs NOW.",
        "offline": "📡 Threat feed unreachable — check back shortly.",
    }[level]
    return {"level": level, "forecast": forecast, "kev_total": total,
            "ransomware_linked": ransom, "recent": recent}


@app.get("/api/health")
async def health():
    s = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "ai_enabled": s.ai_enabled,
        "default_model": s.default_model,
        "fallback_model": s.fallback_model,
        "auth_enabled": bool(_firebase_web_config()) and _FIREBASE_OK,
        "auth_required": s.auth_required,
    }


# ----------------------------- static UI -----------------------------
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

# Kallisto Design System — the single source of visual truth. Served live from
# the design-system/ folder so editing a token there re-themes the whole app.
if DS_DIR.exists():
    app.mount("/ds", StaticFiles(directory=str(DS_DIR)), name="ds")


@app.get("/login")
async def login_page():
    p = WEB_DIR / "login.html"
    if p.exists():
        return FileResponse(str(p))
    return JSONResponse({"error": "login page missing"}, status_code=404)


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt():
    # Friendly robots file — with a little something for the curious (CTF flag: robots).
    return ("User-agent: *\nAllow: /\nDisallow: /admin\n"
            "# TALOS{r0b0ts_txt_r3con} — nice recon. Submit it in Arcade -> Flag Console.\n")


@app.get("/")
async def index():
    idx = WEB_DIR / "index.html"
    if idx.exists():
        return FileResponse(str(idx))
    return JSONResponse({"message": "Talos API is running. UI not built yet."})
