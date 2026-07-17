"""In-app intrusion detection + automatic counter-measures (self-defense).

Wired as FastAPI middleware. It watches traffic to Talos itself and detects:
  * brute force        — too many FAILED logins from one IP
  * credential stuffing— many DIFFERENT usernames tried from one IP
  * DDoS / flood        — too many requests from one IP in a short window
  * exploitation        — path-traversal, sensitive-file probes, SQLi/XSS, LFI/RFI,
                          command injection, Log4Shell, SSRF, or attack-tool UAs

On detection it BLOCKS the offending IP in-app for a cooldown (the automatic
counter-measure) and emails the security address. Normal traffic and *successful*
logins are never alerted. Loopback / trusted IPs are never blocked, so the app
can't lock itself out. Thresholds come from settings (env-overridable).
"""
from __future__ import annotations

import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from urllib.parse import unquote_plus

from app.config import get_settings

RATE_WINDOW = 10          # seconds (request-rate window)
LOGIN_WINDOW = 300        # seconds (failed-login window)
ALERT_COOLDOWN = 300      # min seconds between alerts for the same IP

TRUSTED = {"127.0.0.1", "::1", "localhost", "testclient", "unknown", ""}

EXPLOIT_PATTERNS = [
    (re.compile(r"(?:\.\./|\.\.\\|%2e%2e)", re.I), "path traversal"),
    (re.compile(r"/\.(?:env|git|aws|ssh|htpasswd|svn)\b", re.I), "sensitive-file probe"),
    (re.compile(r"(?:union\s+select|\bor\s+1=1\b|';--|\bsleep\(|waitfor\s+delay|\bdrop\s+table\b)", re.I), "SQL injection"),
    (re.compile(r"(?:<script|onerror\s*=|javascript:|<img[^>]+onerror|<svg[^>]+onload)", re.I), "XSS attempt"),
    (re.compile(r"(?:php|file|data|expect|gopher)://|/proc/self/environ", re.I), "LFI/RFI"),
    (re.compile(r"\$\{jndi:", re.I), "Log4Shell (CVE-2021-44228)"),
    (re.compile(r"(?:[;|`]\s*(?:cat|wget|curl|bash|sh|nc|ncat|powershell|whoami)\b|\$\([^)]+\))", re.I), "command injection"),
    (re.compile(r"169\.254\.169\.254|/latest/meta-data", re.I), "SSRF / cloud-metadata"),
    (re.compile(r"/(?:wp-admin|wp-login|phpmyadmin|xmlrpc\.php|etc/passwd|cgi-bin|boaform)", re.I), "vuln scan"),
    (re.compile(r"\b(?:sqlmap|nikto|nmap|masscan|hydra|dirbuster|wpscan|zgrab|nuclei|acunetix|nessus|gobuster|ffuf|feroxbuster)\b", re.I), "attack tool"),
]

_lock = threading.Lock()
_reqs: dict[str, deque] = defaultdict(deque)
_fails: dict[str, deque] = defaultdict(deque)   # ip -> deque[(ts, username)]
_blocked: dict[str, float] = {}
_last_alert: dict[str, float] = {}
_events: deque = deque(maxlen=150)
_counts: dict[str, int] = defaultdict(int)


def is_trusted(ip: str) -> bool:
    return ip in TRUSTED


def is_blocked(ip: str) -> bool:
    exp = _blocked.get(ip)
    if exp is None:
        return False
    if exp > time.time():
        return True
    _blocked.pop(ip, None)
    return False


def _alert_text(ip: str, attack_type: str, detail: str, block_secs: int) -> str | None:
    now = time.time()
    if now - _last_alert.get(ip, 0) < ALERT_COOLDOWN:
        return None
    _last_alert[ip] = now
    return (f"Talos self-defense blocked an attack.\n\n"
            f"Type:      {attack_type}\n"
            f"Source IP: {ip}\n"
            f"Detail:    {detail}\n"
            f"When:      {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Action:    IP auto-blocked for {block_secs // 60} minutes.")


def _block(ip: str, attack_type: str, detail: str) -> str | None:
    block_secs = get_settings().defense_block_minutes * 60
    with _lock:
        _blocked[ip] = time.time() + block_secs
        _events.appendleft({"ip": ip, "type": attack_type, "detail": detail,
                            "ts": datetime.now().strftime("%H:%M:%S")})
        _counts[attack_type] += 1
        return _alert_text(ip, attack_type, detail, block_secs)


def check_request(ip: str, path: str, query: str, user_agent: str) -> dict | None:
    """Inspect a request; block + return {type, detail, alert} on an attack, else None."""
    s = get_settings()
    if not s.defense_enabled or is_trusted(ip):
        return None
    haystack = unquote_plus(f"{path}?{query} {user_agent}")  # decode %-encoded AND '+'-spaced payloads
    for rx, label in EXPLOIT_PATTERNS:
        if rx.search(haystack):
            detail = f"{path}?{query}"[:200]
            return {"type": label, "detail": detail, "alert": _block(ip, label, detail)}
    now = time.time()
    with _lock:
        dq = _reqs[ip]
        dq.append(now)
        while dq and dq[0] < now - RATE_WINDOW:
            dq.popleft()
        count = len(dq)
    if count > s.defense_rate_max:
        detail = f"{count} requests in {RATE_WINDOW}s"
        return {"type": "DDoS / flooding", "detail": detail, "alert": _block(ip, "DDoS / flooding", detail)}
    return None


def record_login(ip: str, email: str, success: bool) -> dict:
    """Record a login. Alert ONLY on a brute-force or credential-stuffing pattern;
    successful and normal logins are never alerted."""
    s = get_settings()
    if success:
        _fails.pop(ip, None)
        return {"attack": False, "alert": None}
    if not s.defense_enabled or is_trusted(ip):
        return {"attack": False, "alert": None}
    now = time.time()
    with _lock:
        dq = _fails[ip]
        dq.append((now, (email or "").lower()))
        while dq and dq[0][0] < now - LOGIN_WINDOW:
            dq.popleft()
        fails = len(dq)
        distinct = len({u for _, u in dq if u})
    if distinct >= s.defense_distinct_users:
        detail = f"{distinct} different usernames from one IP"
        return {"attack": True, "fails": fails, "alert": _block(ip, "credential stuffing", detail)}
    if fails >= s.defense_login_max_fails:
        detail = f"{fails} failed logins for {email or 'unknown'}"
        return {"attack": True, "fails": fails, "alert": _block(ip, "brute force (login)", detail)}
    return {"attack": False, "alert": None, "fails": fails}


def unblock(ip: str) -> None:
    with _lock:
        _blocked.pop(ip, None)
        _fails.pop(ip, None)
        _reqs.pop(ip, None)


def status() -> dict:
    s = get_settings()
    now = time.time()
    blocked = [{"ip": ip, "expires_in_seconds": max(0, int(exp - now))}
               for ip, exp in list(_blocked.items()) if exp > now]
    return {
        "enabled": s.defense_enabled,
        "blocked_ips": blocked,
        "blocked_count": len(blocked),
        "events_total": sum(_counts.values()),
        "attacks_by_type": dict(_counts),
        "recent_events": list(_events)[:25],
        "thresholds": {
            "max_requests_per_10s": s.defense_rate_max,
            "max_failed_logins": s.defense_login_max_fails,
            "max_distinct_usernames": s.defense_distinct_users,
            "block_minutes": s.defense_block_minutes,
        },
    }
