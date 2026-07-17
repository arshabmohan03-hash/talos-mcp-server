"""Self-contained security utilities, exposed to the AI as tools.

All defensive / non-destructive: password analysis (with breach check via the
HaveIBeenPwned k-anonymity API — only a SHA-1 prefix leaves the machine), a strong
password generator, hashing, JWT decoding, and IP reputation/geolocation.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import math
import re
import secrets
import string
from urllib.parse import quote, urlsplit

import httpx

COMMON = {
    "password", "123456", "12345678", "qwerty", "admin", "letmein", "welcome",
    "iloveyou", "111111", "123123", "abc123", "000000", "root", "toor", "passw0rd",
}


def _humantime(seconds: float) -> str:
    if seconds < 1:
        return "instantly"
    for unit, sec in (("years", 31536000), ("days", 86400),
                      ("hours", 3600), ("minutes", 60)):
        if seconds >= sec:
            return f"~{round(seconds / sec):,} {unit}"
    return f"~{round(seconds)} seconds"


def _pwned_count(pw: str) -> int:
    """How many breaches this password appears in (-1 = couldn't check)."""
    if not pw:
        return 0
    sha1 = hashlib.sha1(pw.encode()).hexdigest().upper()
    prefix, suffix = sha1[:5], sha1[5:]
    try:
        r = httpx.get(f"https://api.pwnedpasswords.com/range/{prefix}",
                      timeout=8, headers={"User-Agent": "Talos-Security"})
        for line in r.text.splitlines():
            h, _, count = line.partition(":")
            if h.strip() == suffix:
                return int(count)
        return 0
    except Exception:  # noqa: BLE001
        return -1


def check_password_strength(password: str) -> dict:
    pw = password or ""
    n = len(pw)
    pool = ((26 if re.search(r"[a-z]", pw) else 0) +
            (26 if re.search(r"[A-Z]", pw) else 0) +
            (10 if re.search(r"\d", pw) else 0) +
            (33 if re.search(r"[^\w]", pw) else 0))
    classes = sum(bool(re.search(p, pw)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^\w]"))
    entropy = round(n * math.log2(pool), 1) if pool else 0.0

    try:
        guesses = pool ** min(n, 80) if pool else 0
        crack = _humantime(guesses / 1e10) if pool else "instantly"
    except OverflowError:
        crack = "longer than the age of the universe"

    issues = []
    if n < 12:
        issues.append("Too short — use at least 12 characters.")
    if classes < 3:
        issues.append("Mix uppercase, lowercase, digits and symbols.")
    if pw.lower() in COMMON:
        issues.append("This is one of the most common passwords.")
    if re.search(r"(.)\1\1", pw):
        issues.append("Avoid 3+ repeated characters in a row.")
    pwned = _pwned_count(pw)
    if pwned > 0:
        issues.append(f"Found in {pwned:,} known data breaches — do not use it.")

    label = ("very weak" if entropy < 28 else "weak" if entropy < 40
             else "fair" if entropy < 60 else "strong" if entropy < 100 else "very strong")
    return {
        "length": n,
        "entropy_bits": entropy,
        "score": min(100, int(entropy / 1.28)),
        "strength": label,
        "char_classes": classes,
        "crack_time_offline_fast_hardware": crack,
        "pwned_count": pwned,
        "issues": issues or ["Looks solid — strong and not seen in breaches."],
    }


def generate_password(length: int = 20, symbols: bool = True) -> dict:
    length = max(8, min(128, int(length or 20)))
    alphabet = string.ascii_letters + string.digits + ("!@#$%^&*()-_=+[]{}?" if symbols else "")
    pw = "".join(secrets.choice(alphabet) for _ in range(length))
    s = check_password_strength(pw)
    return {"password": pw, "length": length, "entropy_bits": s["entropy_bits"],
            "strength": s["strength"]}


def hash_text(text: str, algo: str = "sha256") -> dict:
    data = (text or "").encode()
    out = {a: hashlib.new(a, data).hexdigest() for a in ("md5", "sha1", "sha256", "sha512")}
    algo = (algo or "sha256").lower()
    return {"input_length": len(text or ""), "algorithm": algo,
            "hash": out.get(algo, out["sha256"]), "all": out}


def decode_jwt(token: str) -> dict:
    parts = (token or "").strip().split(".")
    if len(parts) < 2:
        return {"error": "Not a JWT — expected header.payload.signature."}

    def _dec(seg: str):
        seg += "=" * (-len(seg) % 4)
        try:
            return json.loads(base64.urlsafe_b64decode(seg))
        except Exception:  # noqa: BLE001
            return None

    return {"header": _dec(parts[0]), "payload": _dec(parts[1]),
            "signature_present": len(parts) >= 3 and bool(parts[2]),
            "note": "Signature is NOT verified — this is decode-only."}


def _clean_target(raw: str) -> str:
    """Pull a bare host/IP out of whatever the user typed — a full URL, host:port,
    user@host, [IPv6], trailing path/query, etc."""
    s = (raw or "").strip()
    if not s:
        return ""
    if "://" in s:                       # full URL -> take the host
        u = urlsplit(s)
        s = u.netloc or u.path
    s = s.split("@")[-1]                  # drop user:pass@
    s = s.split("/")[0].split("?")[0].split("#")[0].strip()
    if s.startswith("["):                # [IPv6] or [IPv6]:port
        return s[1:].split("]")[0].strip()
    if s.count(":") == 1 and s.rsplit(":", 1)[1].isdigit():   # host:port (not IPv6)
        s = s.rsplit(":", 1)[0]
    return s.strip().rstrip(".")


async def _resolve(host: str) -> str | None:
    """Resolve a hostname to an IP without blocking the event loop."""
    try:
        infos = await asyncio.get_event_loop().getaddrinfo(host, None)
        return infos[0][4][0]
    except Exception:  # noqa: BLE001
        return None


async def lookup_ip(ip: str) -> dict:
    target = _clean_target(ip)
    if not target:
        return {"error": "Enter an IP address or domain — e.g. 8.8.8.8 or example.com."}
    if " " in target or len(target) > 253 or not re.match(r"^[A-Za-z0-9.:_-]+$", target):
        return {"error": f"'{(ip or '').strip()}' isn't a valid IP or domain.", "ip": target}
    # Private / loopback / reserved addresses have no public geolocation — answer locally.
    is_ip = True
    try:
        addr = ipaddress.ip_address(target)
        if (addr.is_private or addr.is_loopback or addr.is_reserved
                or addr.is_link_local or addr.is_multicast or addr.is_unspecified):
            return {"ip": target,
                    "note": "Private / loopback / reserved address — public geolocation "
                            "isn't available (it only works for routable public IPs)."}
    except ValueError:
        is_ip = False  # a hostname

    errors = []
    async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "Talos-Security/1.0"}) as c:
        ipx = target if is_ip else (await _resolve(target) or "")   # the IP-only providers need an IP

        async def via_ipapi_com():   # richest: proxy/VPN + datacenter + reverse DNS; resolves domains itself
            d = (await c.get("http://ip-api.com/json/" + quote(target, safe="") +
                 "?fields=status,message,country,regionName,city,isp,org,as,reverse,proxy,hosting,query")).json()
            if d.get("status") != "success":
                return None
            return {"ip": d.get("query"), "country": d.get("country"), "region": d.get("regionName"),
                    "city": d.get("city"), "isp": d.get("isp"), "org": d.get("org"), "asn": d.get("as"),
                    "reverse_dns": d.get("reverse"), "is_proxy_or_vpn": d.get("proxy"),
                    "is_hosting_datacenter": d.get("hosting"), "source": "ip-api.com"}

        async def via_ipwhois():     # HTTPS, rich geo + isp/org/asn
            d = (await c.get("https://ipwhois.app/json/" + quote(ipx, safe=""))).json()
            if not d.get("success"):
                return None
            return {"ip": d.get("ip"), "country": d.get("country"), "region": d.get("region"),
                    "city": d.get("city"), "isp": d.get("isp") or d.get("org"), "org": d.get("org"),
                    "asn": d.get("asn"), "reverse_dns": None, "is_proxy_or_vpn": None,
                    "is_hosting_datacenter": None, "source": "ipwhois.app"}

        async def via_ipapi_co():    # HTTPS, geo + org + ASN
            d = (await c.get("https://ipapi.co/" + quote(ipx, safe="") + "/json/")).json()
            if d.get("error"):
                return None
            return {"ip": d.get("ip") or ipx, "country": d.get("country_name"), "region": d.get("region"),
                    "city": d.get("city"), "isp": d.get("org"), "org": d.get("org"), "asn": d.get("asn"),
                    "reverse_dns": None, "is_proxy_or_vpn": None, "is_hosting_datacenter": None,
                    "source": "ipapi.co"}

        async def via_ipinfo():      # HTTPS, geo + reverse DNS (hostname); org = "AS#### Name"
            d = (await c.get("https://ipinfo.io/" + quote(ipx, safe="") + "/json")).json()
            if d.get("error") or not d.get("ip"):
                return None
            m = re.match(r"^(AS\d+)\s+(.*)$", d.get("org") or "")
            return {"ip": d.get("ip"), "country": d.get("country"), "region": d.get("region"),
                    "city": d.get("city"), "isp": (m.group(2) if m else (d.get("org") or None)),
                    "org": (m.group(2) if m else d.get("org")), "asn": (m.group(1) if m else None),
                    "reverse_dns": d.get("hostname"), "is_proxy_or_vpn": None,
                    "is_hosting_datacenter": None, "source": "ipinfo.io"}

        providers = [("ip-api.com", via_ipapi_com)]
        if ipx:
            providers += [("ipwhois.app", via_ipwhois), ("ipapi.co", via_ipapi_co), ("ipinfo.io", via_ipinfo)]
        for name, fn in providers:
            try:
                res = await fn()
                if res:
                    return res
                errors.append(name + ": no data")
            except Exception as e:  # noqa: BLE001
                errors.append(name + ": " + type(e).__name__)
    return {"error": f"Couldn't geolocate '{target}' — all lookup providers failed ({'; '.join(errors)}).",
            "ip": target}
