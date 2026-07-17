"""OSINT & Threat Intel tools for Talos (defensive, non-destructive).

Every tool is a plain function (``def`` or ``async def``) that takes keyword
arguments and returns a JSON-serializable dict. Functions never raise to the
caller: failures are caught and returned as ``{"error": "..."}``.

External data is fetched over HTTPS with short timeouts:
  * crt.sh           — certificate transparency (subdomains / certs)
  * archive.org      — Wayback Machine availability + CDX history
  * dns.google       — DNS-over-HTTPS (NS / MX / A records)
  * rdap.org         — RDAP (modern WHOIS) for domains

Tools that require a paid API key read it from the environment and, when it is
absent, return ``{"needs_key": True}`` instead of calling out.

Allowed deps only: Python stdlib + httpx (+ cryptography, unused here).
"""
from __future__ import annotations

import datetime as _dt
import ipaddress
import json
import os
import re
import urllib.parse
from typing import Any

import httpx

_UA = "Talos-OSINT/1.0 (+authorized security research; non-destructive)"
_TIMEOUT = 10.0
_MAX_ITEMS = 100  # generic cap to keep outputs small


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _client(timeout: float = _TIMEOUT, **kw: Any) -> httpx.AsyncClient:
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    headers.update(kw.pop("headers", {}) or {})
    return httpx.AsyncClient(timeout=timeout, headers=headers, **kw)


def _clean_domain(value: str) -> str:
    """Reduce a user string to a bare hostname (strip scheme / path / port)."""
    d = (value or "").strip().lower()
    if not d:
        return ""
    if "://" in d:
        d = urllib.parse.urlsplit(d).netloc or d
    d = d.split("/")[0].split("?")[0]
    if "@" in d:                      # strip any userinfo
        d = d.rsplit("@", 1)[-1]
    if d.startswith("[") and "]" in d:   # bracketed IPv6
        d = d[1:d.index("]")]
    else:
        d = d.split(":")[0]          # strip :port
    return d.strip(".")


_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9_-]{1,63}\.)+[a-z]{2,63}$")
# A single hostname label set (subdomain validation for crt.sh results).
_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9_-]{1,63}\.)+[a-z0-9-]{2,63}$")


def _is_domain(value: str) -> bool:
    return bool(_DOMAIN_RE.match((value or "").strip().lower()))


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address((value or "").strip())
        return True
    except ValueError:
        return False


def _as_bool(value: Any, default: bool = True) -> bool:
    """Coerce UI inputs (incl. the strings 'true'/'false' from a select) to bool."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "y", "on"):
            return True
        if v in ("false", "0", "no", "n", "off", ""):
            return False
        return default
    return bool(value)


def _err(msg: str, **extra: Any) -> dict:
    out = {"error": msg}
    out.update(extra)
    return out


def _need_key(env_name: str, service: str) -> dict:
    return {
        "error": f"Set {env_name} in .env to use the {service} lookup.",
        "needs_key": True,
        "env": env_name,
    }


# --------------------------------------------------------------------------- #
# 1. certificate transparency (crt.sh)
# --------------------------------------------------------------------------- #
async def cert_transparency(domain: str = "", include_expired: bool = True) -> dict:
    """List certificates / discovered subdomains for a domain via crt.sh."""
    d = _clean_domain(domain)
    if not _is_domain(d):
        return _err("Provide a valid domain, e.g. 'example.com'.")
    include_expired = _as_bool(include_expired, default=True)
    url = f"https://crt.sh/?q=%25.{urllib.parse.quote(d)}&output=json"
    try:
        # crt.sh can be slow; give it a longer ceiling than the default.
        async with _client(timeout=20.0) as c:
            r = await c.get(url)
        if r.status_code != 200:
            return _err(f"crt.sh returned HTTP {r.status_code}.", domain=d)
        try:
            rows = r.json()
        except json.JSONDecodeError:
            return _err("crt.sh returned no parseable JSON (it may be rate-limiting).",
                        domain=d)
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}", domain=d)

    names: set[str] = set()
    certs: list[dict] = []
    now = _dt.datetime.now(_dt.timezone.utc)
    for row in rows[: 4000]:
        not_after = (row.get("not_after") or "").strip()
        expired = False
        try:
            if not_after:
                exp = _dt.datetime.fromisoformat(not_after).replace(
                    tzinfo=_dt.timezone.utc)
                expired = exp < now
        except ValueError:
            pass
        if expired and not include_expired:
            continue
        for nm in (row.get("name_value") or "").splitlines():
            nm = nm.strip().lower().lstrip("*.")
            # Keep only true hostnames within the queried zone: exactly the
            # domain or a real sub-label of it (reject look-alikes like
            # 'testexample.com' and CN noise containing spaces).
            if not nm or " " in nm or not _HOSTNAME_RE.match(nm):
                continue
            if nm == d or nm.endswith("." + d):
                names.add(nm)
        if len(certs) < 40:
            certs.append({
                "issuer": (row.get("issuer_name") or "")[:160],
                "common_name": row.get("common_name"),
                "not_before": row.get("not_before"),
                "not_after": not_after,
                "expired": expired,
                "serial": row.get("serial_number"),
            })

    subs = sorted(names)
    return {
        "domain": d,
        "total_cert_entries": len(rows),
        "unique_subdomains": len(subs),
        "subdomains": subs[:_MAX_ITEMS],
        "subdomains_truncated": len(subs) > _MAX_ITEMS,
        "recent_certs": certs,
        "source": "crt.sh",
    }


# --------------------------------------------------------------------------- #
# 2. wayback machine history
# --------------------------------------------------------------------------- #
async def wayback_history(url: str = "", limit: int = 50) -> dict:
    """Closest archived snapshot + recent capture history from the Wayback Machine."""
    target = (url or "").strip()
    if not target:
        return _err("Provide a URL or domain to look up.")
    # archive.org accepts bare domains; keep the user's path if present.
    probe = target
    try:
        limit = max(1, min(200, int(limit or 50)))
    except (TypeError, ValueError):
        limit = 50

    avail: dict = {}
    snapshots: list[dict] = []
    notes: list[str] = []

    # The two archive.org endpoints are queried independently so a slow/failing
    # CDX history call still returns the closest-snapshot availability (and vice
    # versa). The CDX endpoint in particular is often slow, so it gets its own
    # generous timeout.
    try:
        async with _client(timeout=12.0) as c:
            ar = await c.get("https://archive.org/wayback/available",
                             params={"url": probe})
        if ar.status_code == 200:
            closest = ((ar.json() or {}).get("archived_snapshots") or {}).get(
                "closest") or {}
            if closest:
                avail = {
                    "available": bool(closest.get("available")),
                    "url": closest.get("url"),
                    "timestamp": closest.get("timestamp"),
                    "datetime": _fmt_wb_ts(closest.get("timestamp", "")),
                    "status": closest.get("status"),
                }
    except Exception as e:  # noqa: BLE001
        notes.append(f"availability lookup failed: {type(e).__name__}")

    try:
        async with _client(timeout=20.0) as c:
            cr = await c.get(
                "https://web.archive.org/cdx/search/cdx",
                params={
                    "url": probe,
                    "output": "json",
                    "limit": limit,
                    "fl": "timestamp,original,statuscode,mimetype,digest",
                    "collapse": "digest",
                },
            )
        if cr.status_code == 200 and cr.text.strip():
            try:
                rows = cr.json()
            except json.JSONDecodeError:
                rows = []
            for row in rows[1:]:  # first row is the header
                if len(row) < 2:
                    continue
                ts = row[0]
                snapshots.append({
                    "timestamp": ts,
                    "datetime": _fmt_wb_ts(ts),
                    "original": row[1],
                    "status": row[2] if len(row) > 2 else None,
                    "mimetype": row[3] if len(row) > 3 else None,
                    "snapshot_url": f"https://web.archive.org/web/{ts}/{row[1]}",
                })
        elif cr.status_code != 200:
            notes.append(f"history lookup HTTP {cr.status_code}")
    except Exception as e:  # noqa: BLE001
        notes.append(f"history lookup failed: {type(e).__name__}")

    if not avail and not snapshots and notes:
        return _err("; ".join(notes), url=target)

    first = snapshots[0]["datetime"] if snapshots else None
    last = snapshots[-1]["datetime"] if snapshots else None
    return {
        "url": target,
        "archived": bool(avail or snapshots),
        "closest_snapshot": avail or None,
        "total_snapshots_returned": len(snapshots),
        "first_capture": first,
        "last_capture": last,
        "snapshots": snapshots,
        "notes": notes or None,
        "source": "web.archive.org",
    }


def _fmt_wb_ts(ts: str) -> str | None:
    try:
        return _dt.datetime.strptime(ts, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return ts or None


# --------------------------------------------------------------------------- #
# 3. phishing URL heuristic scorer (offline)
# --------------------------------------------------------------------------- #
_SUSPICIOUS_TLDS = {
    "zip", "mov", "xyz", "top", "tk", "ml", "ga", "cf", "gq", "work", "click",
    "link", "country", "kim", "loan", "men", "download", "review", "rest",
    "fit", "racing", "win", "date", "stream", "gdn", "bid", "party", "cam",
}
_BRAND_WORDS = (
    "paypal", "apple", "microsoft", "google", "amazon", "netflix", "facebook",
    "instagram", "whatsapp", "bank", "secure", "account", "login", "signin",
    "verify", "update", "wallet", "coinbase", "binance", "office365", "icloud",
)
# Latin letters that have confusable look-alikes (homoglyph hint set).
_LOOKALIKE = {"0": "o", "1": "l", "rn": "m", "vv": "w"}


def phishing_url_score(url: str = "") -> dict:
    """Score a URL's phishing risk from offline heuristics (no network)."""
    raw = (url or "").strip()
    if not raw:
        return _err("Provide a URL to score.")
    if len(raw) > 4000:
        raw = raw[:4000]

    parsed = urllib.parse.urlsplit(raw if "://" in raw else "http://" + raw)
    host = (parsed.hostname or "").lower()
    if not host:
        return _err("Could not parse a host from that URL.", url=raw)

    reasons: list[str] = []
    score = 0

    # 1) IP literal as host
    host_is_ip = _is_ip(host)
    if host_is_ip:
        score += 30
        reasons.append("Host is a raw IP address instead of a domain name.")

    # 2) '@' in the URL (credential-confusion / real host after @)
    if "@" in (parsed.netloc or "") or "@" in raw.split("?", 1)[0]:
        score += 25
        reasons.append("URL contains '@', which can hide the real destination host.")

    # 3) punycode / IDN homograph
    if "xn--" in host:
        score += 25
        reasons.append("Host uses punycode (xn--), a common homograph-attack technique.")
    if any(ord(ch) > 127 for ch in host):
        score += 20
        reasons.append("Host contains non-ASCII (Unicode) characters.")

    # 4) excessive subdomains
    labels = host.split(".")
    depth = max(0, len(labels) - 2)
    if not host_is_ip and depth >= 3:
        score += 15
        reasons.append(f"Many subdomain levels ({depth}) — often used to look legitimate.")

    # 5) suspicious / abused TLD
    tld = labels[-1] if len(labels) >= 2 and not host_is_ip else ""
    if tld in _SUSPICIOUS_TLDS:
        score += 15
        reasons.append(f"Top-level domain '.{tld}' is frequently abused for phishing.")

    # 6) overall URL length
    if len(raw) > 75:
        score += 10
        reasons.append(f"URL is long ({len(raw)} chars); long URLs often hide intent.")

    # 7) brand keyword in subdomain / path but NOT the registrable domain
    reg_domain = ".".join(labels[-2:]) if len(labels) >= 2 else host
    haystack = (host + parsed.path).lower()
    for brand in _BRAND_WORDS:
        if brand in haystack and brand not in reg_domain:
            score += 15
            reasons.append(
                f"Brand/keyword '{brand}' appears outside the real domain "
                f"('{reg_domain}') — a typical lure.")
            break

    # 8) homoglyph / look-alike substitutions against brand words
    for pat, real in _LOOKALIKE.items():
        if pat in host:
            for brand in _BRAND_WORDS:
                if real in brand and host.replace(pat, real).find(brand) != -1:
                    score += 12
                    reasons.append(
                        f"Host resembles '{brand}' via look-alike characters ('{pat}').")
                    break
            else:
                continue
            break

    # 9) hyphen-stuffed / digit-stuffed host
    if host.count("-") >= 3:
        score += 8
        reasons.append("Host has many hyphens, common in throwaway phishing domains.")

    # 10) non-standard / obfuscated port
    if parsed.port and parsed.port not in (80, 443):
        score += 8
        reasons.append(f"Non-standard port :{parsed.port}.")

    # 11) sensitive path keywords
    if re.search(r"/(login|signin|verify|secure|account|update|confirm|webscr)\b",
                 parsed.path.lower()):
        score += 6
        reasons.append("Path contains credential/verification keywords.")

    score = max(0, min(100, score))
    verdict = ("high" if score >= 60 else "medium" if score >= 30
               else "low" if score >= 12 else "minimal")
    return {
        "url": raw,
        "host": host,
        "registrable_domain": reg_domain if not host_is_ip else None,
        "risk_score": score,
        "risk_level": verdict,
        "reasons": reasons or ["No common phishing indicators detected."],
        "note": "Heuristic, offline scoring only — not a definitive verdict.",
    }


# --------------------------------------------------------------------------- #
# 4. google dork generator (template library, offline)
# --------------------------------------------------------------------------- #
_DORK_TEMPLATES: list[tuple[str, str]] = [
    ("Indexed login / admin pages", 'site:{d} inurl:(login OR admin OR signin OR dashboard)'),
    ("Exposed directory listings", 'site:{d} intitle:"index of"'),
    ("Configuration & env files", 'site:{d} ext:env OR ext:ini OR ext:conf OR ext:cfg'),
    ("Backup & archive files", 'site:{d} ext:bak OR ext:old OR ext:backup OR ext:zip OR ext:tar'),
    ("Database dumps", 'site:{d} ext:sql OR ext:db OR ext:dbf OR ext:mdb'),
    ("Log files", 'site:{d} ext:log'),
    ("Documents (PDF/Office)", 'site:{d} ext:pdf OR ext:doc OR ext:xls OR ext:ppt'),
    ("Spreadsheets / CSV data", 'site:{d} ext:csv OR ext:xlsx'),
    ("Exposed API keys / secrets in pages", 'site:{d} intext:(api_key OR apikey OR "secret_key" OR "access_token")'),
    ("Password references", 'site:{d} intext:password filetype:txt OR filetype:log'),
    ("Git / VCS exposure", 'site:{d} inurl:(.git OR .svn OR .hg)'),
    ("phpinfo / debug pages", 'site:{d} ext:php intitle:phpinfo "published by the PHP Group"'),
    ("Open redirects / SSO endpoints", 'site:{d} inurl:(redirect OR url= OR next= OR return=)'),
    ("Error messages / stack traces", 'site:{d} intext:("sql syntax near" OR "fatal error" OR "stack trace")'),
    ("WordPress paths", 'site:{d} inurl:wp- OR inurl:wp-content OR inurl:wp-admin'),
    ("Subdomains via search", 'site:*.{d} -www'),
    ("Cloud storage buckets", '"{d}" site:s3.amazonaws.com OR site:blob.core.windows.net OR site:storage.googleapis.com'),
    ("Pastebin / leak sites", '"{d}" site:pastebin.com OR site:ghostbin.com OR site:throwbin.io'),
    ("Code references (GitHub)", '"{d}" site:github.com'),
    ("FTP servers", 'site:{d} inurl:ftp'),
]


def google_dork_generator(domain: str = "") -> dict:
    """Generate useful OSINT Google dork queries for a domain (offline templates)."""
    d = _clean_domain(domain)
    if not _is_domain(d):
        return _err("Provide a valid domain, e.g. 'example.com'.")
    dorks = []
    for label, tmpl in _DORK_TEMPLATES:
        q = tmpl.format(d=d)
        dorks.append({
            "category": label,
            "query": q,
            "search_url": "https://www.google.com/search?q=" + urllib.parse.quote(q),
        })
    return {
        "domain": d,
        "count": len(dorks),
        "dorks": dorks,
        "note": "For authorized reconnaissance / attack-surface review only.",
    }


# --------------------------------------------------------------------------- #
# 5. username enumeration across sites
# --------------------------------------------------------------------------- #
# (label, profile-url template, "exists" status codes)
_USERNAME_SITES: list[tuple[str, str, tuple[int, ...]]] = [
    ("GitHub", "https://github.com/{u}", (200,)),
    ("GitLab", "https://gitlab.com/{u}", (200,)),
    ("Reddit", "https://www.reddit.com/user/{u}/about.json", (200,)),
    ("Instagram", "https://www.instagram.com/{u}/", (200,)),
    ("Twitter/X", "https://x.com/{u}", (200,)),
    ("Keybase", "https://keybase.io/{u}", (200,)),
    ("Telegram", "https://t.me/{u}", (200,)),
    ("Medium", "https://medium.com/@{u}", (200,)),
    ("Pinterest", "https://www.pinterest.com/{u}/", (200,)),
    ("TikTok", "https://www.tiktok.com/@{u}", (200,)),
    ("Steam", "https://steamcommunity.com/id/{u}", (200,)),
    ("HackerNews", "https://news.ycombinator.com/user?id={u}", (200,)),
]
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._\-]{1,40}$")


async def username_enum(username: str = "") -> dict:
    """Check whether a username appears to exist across ~12 popular sites."""
    u = (username or "").strip().lstrip("@")
    if not u:
        return _err("Provide a username to check.")
    if not _USERNAME_RE.match(u):
        return _err("Username has unusual characters; use letters, digits, . _ - only.")

    async def _check(c: httpx.AsyncClient, label: str, tmpl: str,
                     ok: tuple[int, ...]) -> dict:
        url = tmpl.format(u=urllib.parse.quote(u))
        try:
            r = await c.get(url)
            code = r.status_code
            if code in (405, 403) or (label == "Instagram" and code == 429):
                # Some sites block HEAD/automation; mark as unknown rather than absent.
                return {"site": label, "url": url, "status": code,
                        "exists": None, "note": "inconclusive (blocked/rate-limited)"}
            return {"site": label, "url": url, "status": code,
                    "exists": code in ok}
        except Exception as e:  # noqa: BLE001
            return {"site": label, "url": url, "status": None,
                    "exists": None, "note": f"{type(e).__name__}"}

    try:
        async with _client(timeout=12.0, follow_redirects=True,
                           headers={"Accept": "text/html,application/json"}) as c:
            import asyncio
            results = await asyncio.gather(*[
                _check(c, label, tmpl, ok) for label, tmpl, ok in _USERNAME_SITES
            ])
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}", username=u)

    found = [r["site"] for r in results if r.get("exists") is True]
    return {
        "username": u,
        "checked": len(results),
        "found_count": len(found),
        "found_on": found,
        "results": results,
        "note": "A 200 is a strong but not definitive signal; verify manually.",
    }


# --------------------------------------------------------------------------- #
# 6. domain profiler (RDAP + DoH + crt.sh)
# --------------------------------------------------------------------------- #
async def _doh(client: httpx.AsyncClient, name: str, rtype: str) -> list[str]:
    try:
        r = await client.get(
            "https://dns.google/resolve",
            params={"name": name, "type": rtype},
            headers={"Accept": "application/dns-json"},
        )
        if r.status_code != 200:
            return []
        data = r.json() or {}
        out = []
        for ans in data.get("Answer", []) or []:
            val = ans.get("data")
            if val:
                out.append(val.strip().rstrip("."))
        return out
    except Exception:  # noqa: BLE001
        return []


async def domain_profiler(domain: str = "") -> dict:
    """One-shot domain report: RDAP whois + NS/MX (DoH) + crt.sh subdomain count."""
    d = _clean_domain(domain)
    if not _is_domain(d):
        return _err("Provide a valid domain, e.g. 'example.com'.")

    report: dict = {"domain": d, "source": "rdap.org + dns.google + crt.sh"}

    # --- RDAP (modern WHOIS) ---
    whois: dict = {}
    try:
        async with _client(timeout=12.0, follow_redirects=True) as c:
            rr = await c.get(f"https://rdap.org/domain/{urllib.parse.quote(d)}")
        if rr.status_code == 200:
            rd = rr.json() or {}
            events = {e.get("eventAction"): e.get("eventDate")
                      for e in rd.get("events", []) or []}
            registrar = None
            for ent in rd.get("entities", []) or []:
                roles = ent.get("roles") or []
                if "registrar" in roles:
                    registrar = _vcard_name(ent) or ent.get("handle")
                    break
            whois = {
                "handle": rd.get("handle"),
                "ldh_name": rd.get("ldhName"),
                "status": rd.get("status", []),
                "registrar": registrar,
                "registered": events.get("registration"),
                "expires": events.get("expiration"),
                "last_changed": events.get("last changed") or events.get("last update"),
                "nameservers_rdap": [ns.get("ldhName") for ns in rd.get("nameservers", []) or []],
            }
        elif rr.status_code == 404:
            whois = {"error": "Domain not found in RDAP (may be unregistered)."}
        else:
            whois = {"error": f"RDAP HTTP {rr.status_code}."}
    except Exception as e:  # noqa: BLE001
        whois = {"error": f"{type(e).__name__}: {e}"}
    report["whois"] = whois

    # --- DNS over HTTPS: NS, MX, A, AAAA, TXT ---
    try:
        async with _client(timeout=10.0) as c:
            import asyncio
            ns, mx, a, aaaa, txt = await asyncio.gather(
                _doh(c, d, "NS"), _doh(c, d, "MX"), _doh(c, d, "A"),
                _doh(c, d, "AAAA"), _doh(c, d, "TXT"),
            )
        report["dns"] = {
            "ns": ns,
            "mx": sorted(mx),
            "a": a,
            "aaaa": aaaa,
            "has_spf": any("v=spf1" in t.lower() for t in txt),
            "has_dmarc": False,  # filled below
            "txt_sample": txt[:8],
        }
        # DMARC lives at _dmarc.<domain>
        async with _client(timeout=10.0) as c:
            dmarc = await _doh(c, "_dmarc." + d, "TXT")
        report["dns"]["has_dmarc"] = any("v=dmarc1" in t.lower() for t in dmarc)
    except Exception as e:  # noqa: BLE001
        report["dns"] = {"error": f"{type(e).__name__}: {e}"}

    # --- crt.sh subdomain count (reuse the tool, keep output small) ---
    ct = await cert_transparency(d, include_expired=True)
    if "error" in ct:
        report["subdomains"] = {"error": ct["error"]}
    else:
        report["subdomains"] = {
            "unique_count": ct.get("unique_subdomains", 0),
            "sample": (ct.get("subdomains") or [])[:25],
        }
    return report


def _vcard_name(entity: dict) -> str | None:
    """Pull the formatted name (fn) out of an RDAP jCard, if present."""
    try:
        vcard = (entity.get("vcardArray") or [None, []])[1]
        for item in vcard:
            if item and item[0] == "fn":
                return item[3]
    except Exception:  # noqa: BLE001
        pass
    return None


# --------------------------------------------------------------------------- #
# 7-11. key-gated threat-intel lookups (stubs that return needs_key)
# --------------------------------------------------------------------------- #
async def virustotal_lookup(resource: str = "") -> dict:
    """VirusTotal reputation for a domain/IP/URL/hash (needs VIRUSTOTAL_API_KEY)."""
    res = (resource or "").strip()
    if not res:
        return _err("Provide a domain, IP, URL, or file hash to look up.")
    key = os.environ.get("VIRUSTOTAL_API_KEY", "")
    if not key:
        return _need_key("VIRUSTOTAL_API_KEY", "VirusTotal")
    try:
        if _is_ip(res):
            path = f"ip_addresses/{res}"
        elif re.fullmatch(r"[A-Fa-f0-9]{32,64}", res):
            path = f"files/{res}"
        elif _is_domain(_clean_domain(res)):
            path = f"domains/{_clean_domain(res)}"
        else:
            import base64
            enc = base64.urlsafe_b64encode(res.encode()).decode().rstrip("=")
            path = f"urls/{enc}"
        async with _client(timeout=12.0, headers={"x-apikey": key}) as c:
            r = await c.get(f"https://www.virustotal.com/api/v3/{path}")
        if r.status_code == 401:
            return _err("VirusTotal rejected the API key (401).", needs_key=True)
        if r.status_code == 404:
            return _err("Resource not found on VirusTotal.", resource=res)
        if r.status_code != 200:
            return _err(f"VirusTotal HTTP {r.status_code}.", resource=res)
        attrs = ((r.json() or {}).get("data") or {}).get("attributes") or {}
        stats = attrs.get("last_analysis_stats") or {}
        return {
            "resource": res,
            "malicious": stats.get("malicious", 0),
            "suspicious": stats.get("suspicious", 0),
            "harmless": stats.get("harmless", 0),
            "undetected": stats.get("undetected", 0),
            "reputation": attrs.get("reputation"),
            "source": "VirusTotal v3",
        }
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}", resource=res)


async def shodan_host(ip: str = "") -> dict:
    """Shodan host profile: open ports, services, tags (needs SHODAN_API_KEY)."""
    target = (ip or "").strip()
    if not target:
        return _err("Provide an IP address to look up.")
    if not _is_ip(target):
        return _err("Provide a valid IP address.")
    key = os.environ.get("SHODAN_API_KEY", "")
    if not key:
        return _need_key("SHODAN_API_KEY", "Shodan")
    try:
        async with _client(timeout=12.0) as c:
            r = await c.get(f"https://api.shodan.io/shodan/host/{target}",
                            params={"key": key, "minify": "true"})
        if r.status_code == 401:
            return _err("Shodan rejected the API key (401).", needs_key=True)
        if r.status_code == 404:
            return _err("No Shodan information for that IP.", ip=target)
        if r.status_code != 200:
            return _err(f"Shodan HTTP {r.status_code}.", ip=target)
        d = r.json() or {}
        return {
            "ip": d.get("ip_str", target),
            "ports": sorted(d.get("ports", []) or []),
            "hostnames": d.get("hostnames", []),
            "org": d.get("org"),
            "isp": d.get("isp"),
            "country": d.get("country_name"),
            "os": d.get("os"),
            "tags": d.get("tags", []),
            "vulns": (d.get("vulns") or [])[:50],
            "source": "Shodan",
        }
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}", ip=target)


async def abuseipdb_check(ip: str = "", max_age_days: int = 90) -> dict:
    """AbuseIPDB reputation/abuse confidence for an IP (needs ABUSEIPDB_API_KEY)."""
    target = (ip or "").strip()
    if not target:
        return _err("Provide an IP address to check.")
    if not _is_ip(target):
        return _err("Provide a valid IP address.")
    key = os.environ.get("ABUSEIPDB_API_KEY", "")
    if not key:
        return _need_key("ABUSEIPDB_API_KEY", "AbuseIPDB")
    try:
        days = max(1, min(365, int(max_age_days or 90)))
    except (TypeError, ValueError):
        days = 90
    try:
        async with _client(timeout=12.0, headers={"Key": key,
                                                  "Accept": "application/json"}) as c:
            r = await c.get("https://api.abuseipdb.com/api/v2/check",
                            params={"ipAddress": target, "maxAgeInDays": days})
        if r.status_code in (401, 403):
            return _err("AbuseIPDB rejected the API key.", needs_key=True)
        if r.status_code != 200:
            return _err(f"AbuseIPDB HTTP {r.status_code}.", ip=target)
        d = (r.json() or {}).get("data") or {}
        return {
            "ip": d.get("ipAddress", target),
            "abuse_confidence_score": d.get("abuseConfidenceScore"),
            "total_reports": d.get("totalReports"),
            "country": d.get("countryCode"),
            "isp": d.get("isp"),
            "domain": d.get("domain"),
            "is_whitelisted": d.get("isWhitelisted"),
            "usage_type": d.get("usageType"),
            "last_reported": d.get("lastReportedAt"),
            "source": "AbuseIPDB",
        }
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}", ip=target)


async def email_breach(email: str = "") -> dict:
    """List breaches an email appears in via HaveIBeenPwned (needs HIBP_API_KEY)."""
    addr = (email or "").strip().lower()
    if not addr or "@" not in addr:
        return _err("Provide a valid email address.")
    key = os.environ.get("HIBP_API_KEY", "")
    if not key:
        return _need_key("HIBP_API_KEY", "HaveIBeenPwned")
    try:
        async with _client(
            timeout=12.0,
            headers={"hibp-api-key": key, "User-Agent": _UA},
        ) as c:
            r = await c.get(
                "https://haveibeenpwned.com/api/v3/breachedaccount/"
                + urllib.parse.quote(addr),
                params={"truncateResponse": "false"},
            )
        if r.status_code == 404:
            return {"email": addr, "breached": False, "breach_count": 0,
                    "breaches": [], "source": "HaveIBeenPwned"}
        if r.status_code in (401, 403):
            return _err("HIBP rejected the API key.", needs_key=True)
        if r.status_code == 429:
            return _err("HIBP rate limit hit — slow down and retry.", email=addr)
        if r.status_code != 200:
            return _err(f"HIBP HTTP {r.status_code}.", email=addr)
        rows = r.json() or []
        breaches = [{
            "name": b.get("Name") or b.get("Title"),
            "domain": b.get("Domain"),
            "breach_date": b.get("BreachDate"),
            "pwn_count": b.get("PwnCount"),
            "data_classes": (b.get("DataClasses") or [])[:12],
            "is_verified": b.get("IsVerified"),
        } for b in rows[:_MAX_ITEMS]]
        return {
            "email": addr,
            "breached": bool(breaches),
            "breach_count": len(breaches),
            "breaches": breaches,
            "source": "HaveIBeenPwned",
        }
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}", email=addr)


async def greynoise_check(ip: str = "") -> dict:
    """GreyNoise community context for an IP (needs GREYNOISE_API_KEY)."""
    target = (ip or "").strip()
    if not target:
        return _err("Provide an IP address to check.")
    if not _is_ip(target):
        return _err("Provide a valid IP address.")
    key = os.environ.get("GREYNOISE_API_KEY", "")
    if not key:
        return _need_key("GREYNOISE_API_KEY", "GreyNoise")
    try:
        async with _client(timeout=12.0, headers={"key": key,
                                                  "Accept": "application/json"}) as c:
            r = await c.get(
                f"https://api.greynoise.io/v3/community/{urllib.parse.quote(target)}")
        if r.status_code in (401, 403):
            return _err("GreyNoise rejected the API key.", needs_key=True)
        if r.status_code == 404:
            return {"ip": target, "noise": False, "seen": False,
                    "classification": "unknown", "source": "GreyNoise Community"}
        if r.status_code != 200:
            return _err(f"GreyNoise HTTP {r.status_code}.", ip=target)
        d = r.json() or {}
        return {
            "ip": d.get("ip", target),
            "noise": d.get("noise"),
            "riot": d.get("riot"),
            "classification": d.get("classification"),
            "name": d.get("name"),
            "last_seen": d.get("last_seen"),
            "link": d.get("link"),
            "source": "GreyNoise Community",
        }
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}", ip=target)


# --------------------------------------------------------------------------- #
# tool specs (consumed by the UI / AI bridge)
# --------------------------------------------------------------------------- #
_CAT = "OSINT & Threat Intel"

SPECS: list[dict] = [
    {
        "name": "cert_transparency",
        "label": "Cert Transparency",
        "description": "Enumerate subdomains and certificates for a domain via crt.sh.",
        "category": _CAT,
        "tier": "green",
        "inputs": [
            {"key": "domain", "label": "Domain", "type": "text",
             "placeholder": "example.com"},
            {"key": "include_expired", "label": "Include expired certs",
             "type": "select", "options": ["true", "false"]},
        ],
    },
    {
        "name": "wayback_history",
        "label": "Wayback History",
        "description": "Closest archived snapshot plus recent capture history (archive.org).",
        "category": _CAT,
        "tier": "green",
        "inputs": [
            {"key": "url", "label": "URL or domain", "type": "text",
             "placeholder": "example.com/page"},
            {"key": "limit", "label": "Max snapshots", "type": "number",
             "placeholder": "50"},
        ],
    },
    {
        "name": "phishing_url_score",
        "label": "Phishing URL Score",
        "description": "Offline heuristic phishing-risk score for a URL with reasons.",
        "category": _CAT,
        "tier": "green",
        "inputs": [
            {"key": "url", "label": "URL", "type": "text",
             "placeholder": "http://paypal.secure-login.example.tk/verify"},
        ],
    },
    {
        "name": "google_dork_generator",
        "label": "Google Dork Generator",
        "description": "Generate useful OSINT Google dork queries for a domain.",
        "category": _CAT,
        "tier": "green",
        "inputs": [
            {"key": "domain", "label": "Domain", "type": "text",
             "placeholder": "example.com"},
        ],
    },
    {
        "name": "username_enum",
        "label": "Username Enum",
        "description": "Check whether a username exists across ~12 popular sites.",
        "category": _CAT,
        "tier": "yellow",
        "inputs": [
            {"key": "username", "label": "Username", "type": "text",
             "placeholder": "octocat"},
        ],
    },
    {
        "name": "domain_profiler",
        "label": "Domain Profiler",
        "description": "Combined RDAP whois + DNS (NS/MX/SPF/DMARC) + subdomain count report.",
        "category": _CAT,
        "tier": "green",
        "inputs": [
            {"key": "domain", "label": "Domain", "type": "text",
             "placeholder": "example.com"},
        ],
    },
]
