"""Talos — Web App Testing tools.

Self-contained, passive/observational HTTP checks built on httpx. Every tool
sends ordinary browser-like requests (follow_redirects=True, a normal User-Agent)
and only *reads* the responses. Nothing here performs active exploitation
(no SQLi/XSS payload injection, no brute forcing, no destructive methods).

Each tool is a plain function whose name matches its SPEC "name". Functions take
keyword arguments and ALWAYS return a JSON-serializable dict. Failures are caught
and returned as {"error": "..."} rather than raised to the caller.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse, urlunparse

import httpx

# A normal, current desktop browser UA so targets behave as they would for a user.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 10.0
MAX_BODY = 200_000  # cap how much body text we ever scan / return


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _normalize_url(raw: str) -> str:
    """Normalize a user-supplied target into an absolute http(s) URL.

    Adds https:// when no scheme is given. Raises ValueError on bad input.
    """
    raw = (raw or "").strip().strip("\"'<> ")
    if not raw:
        raise ValueError("Empty target URL.")
    if "://" not in raw:
        raw = "https://" + raw
    p = urlparse(raw)
    if p.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported scheme {p.scheme!r}; use http or https.")
    if not p.hostname:
        raise ValueError("Could not parse a hostname from the target.")
    path = p.path or "/"
    return urlunparse((p.scheme, p.netloc, path, "", p.query, ""))


def _origin(url: str) -> str:
    """Return scheme://host[:port] for a URL (no path)."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _client(**kwargs) -> httpx.AsyncClient:
    opts = dict(
        headers=DEFAULT_HEADERS,
        timeout=TIMEOUT,
        follow_redirects=True,
        verify=True,
    )
    opts.update(kwargs)
    return httpx.AsyncClient(**opts)


def _safe_text(resp: httpx.Response) -> str:
    """Decoded response text, capped at MAX_BODY chars."""
    try:
        return resp.text[:MAX_BODY]
    except Exception:
        try:
            return resp.content[:MAX_BODY].decode("utf-8", "replace")
        except Exception:
            return ""


def _err(exc: Exception) -> dict:
    return {"error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------- #
# 1. security_header_grader
# --------------------------------------------------------------------------- #
# (header, weight, friendly-name, fix)
_HEADER_RULES = [
    (
        "strict-transport-security", 25, "HTTP Strict Transport Security (HSTS)",
        "Add: Strict-Transport-Security: max-age=63072000; includeSubDomains; preload",
    ),
    (
        "content-security-policy", 25, "Content Security Policy (CSP)",
        "Define a Content-Security-Policy, starting from: default-src 'self'; "
        "object-src 'none'; base-uri 'self'",
    ),
    (
        "x-frame-options", 15, "Clickjacking protection (X-Frame-Options)",
        "Add: X-Frame-Options: DENY  (or a CSP frame-ancestors 'none' directive)",
    ),
    (
        "x-content-type-options", 15, "MIME-sniffing protection",
        "Add: X-Content-Type-Options: nosniff",
    ),
    (
        "referrer-policy", 10, "Referrer-Policy",
        "Add: Referrer-Policy: strict-origin-when-cross-origin",
    ),
    (
        "permissions-policy", 10, "Permissions-Policy",
        "Add a Permissions-Policy that disables unused features, e.g. "
        "geolocation=(), camera=(), microphone=()",
    ),
]


def _grade_letter(pct: float) -> str:
    if pct >= 90:
        return "A"
    if pct >= 80:
        return "B"
    if pct >= 70:
        return "C"
    if pct >= 60:
        return "D"
    if pct >= 40:
        return "E"
    return "F"


async def security_header_grader(url: str = "") -> dict:
    """Grade a site's HTTP security response headers (A–F) with exact fixes."""
    try:
        target = _normalize_url(url)
    except ValueError as e:
        return {"error": str(e)}
    try:
        async with _client() as client:
            resp = await client.get(target)
    except Exception as e:  # noqa: BLE001
        return _err(e)

    h = resp.headers
    is_https = resp.url.scheme == "https"
    earned = 0
    total = 0
    present: list[dict] = []
    missing: list[dict] = []

    for name, weight, friendly, fix in _HEADER_RULES:
        # HSTS is only meaningful over HTTPS — don't penalize plain-HTTP origins.
        if name == "strict-transport-security" and not is_https:
            continue
        total += weight
        val = h.get(name)
        if val:
            earned += weight
            present.append({"header": name, "value": val[:300]})
        else:
            # X-Frame-Options can be satisfied by CSP frame-ancestors.
            if name == "x-frame-options":
                csp = h.get("content-security-policy", "").lower()
                if "frame-ancestors" in csp:
                    earned += weight
                    present.append(
                        {"header": name,
                         "value": "(covered by CSP frame-ancestors)"}
                    )
                    continue
            missing.append({"header": name, "name": friendly, "fix": fix})

    pct = round((earned / total) * 100, 1) if total else 0.0
    return {
        "url": str(resp.url),
        "final_status": resp.status_code,
        "https": is_https,
        "grade": _grade_letter(pct),
        "score_percent": pct,
        "points": f"{earned}/{total}",
        "present": present,
        "missing": missing,
        "summary": (
            f"Graded {len(present)} present / {len(missing)} missing "
            f"security headers — grade {_grade_letter(pct)} ({pct}%)."
        ),
    }


# --------------------------------------------------------------------------- #
# 2. cookie_analyzer
# --------------------------------------------------------------------------- #
def _parse_samesite(raw_low: str) -> str | None:
    m = re.search(r"samesite=([a-z]+)", raw_low)
    return m.group(1) if m else None


async def cookie_analyzer(url: str = "") -> dict:
    """Flag missing Secure / HttpOnly / SameSite attributes on Set-Cookie headers."""
    try:
        target = _normalize_url(url)
    except ValueError as e:
        return {"error": str(e)}
    try:
        async with _client() as client:
            resp = await client.get(target)
    except Exception as e:  # noqa: BLE001
        return _err(e)

    is_https = resp.url.scheme == "https"
    raw_cookies = resp.headers.get_list("set-cookie")
    cookies: list[dict] = []

    for raw in raw_cookies:
        name = raw.split("=", 1)[0].strip()
        low = raw.lower()
        samesite = _parse_samesite(low)
        issues: list[str] = []
        if "secure" not in low:
            issues.append(
                "Missing Secure — cookie can be sent over plain HTTP."
                if is_https else
                "Missing Secure (site is HTTP; cannot set Secure until HTTPS)."
            )
        if "httponly" not in low:
            issues.append("Missing HttpOnly — readable by JavaScript (XSS theft risk).")
        if not samesite:
            issues.append("Missing SameSite — cross-site requests send the cookie (CSRF risk).")
        elif samesite == "none" and "secure" not in low:
            issues.append("SameSite=None requires Secure, but Secure is absent (browsers reject it).")
        cookies.append({
            "name": name,
            "secure": "secure" in low,
            "httponly": "httponly" in low,
            "samesite": samesite or "(unset)",
            "raw": raw[:200],
            "issues": issues,
        })

    flagged = sum(1 for c in cookies if c["issues"])
    return {
        "url": str(resp.url),
        "https": is_https,
        "cookie_count": len(cookies),
        "flagged_count": flagged,
        "cookies": cookies,
        "recommendation": (
            "Set every session cookie as: Secure; HttpOnly; SameSite=Lax "
            "(use Strict for the most sensitive cookies)."
        ),
        "summary": (
            f"Found {len(cookies)} cookie(s); {flagged} have at least one missing/weak flag."
            if cookies else "No Set-Cookie headers were returned on this response."
        ),
    }


# --------------------------------------------------------------------------- #
# 3. cors_check
# --------------------------------------------------------------------------- #
async def cors_check(url: str = "") -> dict:
    """Probe CORS by sending Origin: https://evil.example and inspecting ACAO/ACAC."""
    try:
        target = _normalize_url(url)
    except ValueError as e:
        return {"error": str(e)}
    evil = "https://evil.example"
    try:
        async with _client() as client:
            resp = await client.get(target, headers={"Origin": evil})
    except Exception as e:  # noqa: BLE001
        return _err(e)

    h = resp.headers
    acao = h.get("access-control-allow-origin")
    acac = h.get("access-control-allow-credentials", "")
    acam = h.get("access-control-allow-methods")
    acah = h.get("access-control-allow-headers")
    creds = acac.strip().lower() == "true"

    risk = "info"
    findings: list[str] = []
    if acao == "*":
        if creds:
            risk = "high"
            findings.append(
                "ACAO '*' together with Allow-Credentials: true is invalid and, "
                "where honored, exposes credentialed responses to any origin."
            )
        else:
            risk = "low"
            findings.append(
                "ACAO '*' allows any origin to read responses (acceptable only for public data)."
            )
    elif acao and evil in acao:
        # Server reflected our attacker origin.
        risk = "high" if creds else "medium"
        findings.append(
            f"Server reflected the arbitrary Origin '{evil}' in "
            f"Access-Control-Allow-Origin"
            + (" WITH credentials — a cross-origin data-theft risk." if creds
               else " (reflection without credentials is lower risk).")
        )
    elif acao:
        findings.append(f"ACAO is set to a fixed origin: {acao}")
    else:
        findings.append("No Access-Control-Allow-Origin header — CORS not enabled for this Origin.")

    return {
        "url": str(resp.url),
        "tested_origin": evil,
        "access_control_allow_origin": acao,
        "access_control_allow_credentials": acac or None,
        "access_control_allow_methods": acam,
        "access_control_allow_headers": acah,
        "reflects_arbitrary_origin": bool(acao and evil in (acao or "")),
        "allows_credentials": creds,
        "risk": risk,
        "findings": findings,
        "recommendation": (
            "Never reflect arbitrary Origins. Allow-list specific trusted origins "
            "and only combine credentials with an exact origin (never '*')."
        ),
    }


# --------------------------------------------------------------------------- #
# 4. http_methods_check
# --------------------------------------------------------------------------- #
_RISKY_METHODS = {
    "PUT": "Allows uploading/replacing resources — verify it is authenticated and intended.",
    "DELETE": "Allows deleting resources — verify it is authenticated and intended.",
    "TRACE": "Enables Cross-Site Tracing (XST); should be disabled.",
    "TRACK": "IIS equivalent of TRACE; should be disabled.",
    "CONNECT": "Proxy method; should not be exposed by an application server.",
    "PATCH": "Allows partial modification — verify it is authenticated and intended.",
}


async def http_methods_check(url: str = "") -> dict:
    """Send an OPTIONS request and report the Allow header, flagging risky methods."""
    try:
        target = _normalize_url(url)
    except ValueError as e:
        return {"error": str(e)}
    try:
        async with _client() as client:
            resp = await client.options(target)
    except Exception as e:  # noqa: BLE001
        return _err(e)

    allow = resp.headers.get("allow", "")
    methods = [m.strip().upper() for m in re.split(r"[,\s]+", allow) if m.strip()]
    risky = [
        {"method": m, "note": _RISKY_METHODS[m]}
        for m in methods if m in _RISKY_METHODS
    ]
    return {
        "url": str(resp.url),
        "status": resp.status_code,
        "allow_header": allow or "(not returned)",
        "methods": methods,
        "risky_methods": risky,
        "note": (
            "An OPTIONS Allow header is advisory — the server may still accept methods "
            "it does not advertise. This check is observational only."
        ),
        "recommendation": (
            "Disable TRACE/TRACK. Ensure PUT/DELETE/PATCH are only reachable on "
            "authenticated, intended endpoints."
            if risky else "No high-risk methods advertised."
        ),
        "summary": (
            f"Advertised methods: {', '.join(methods) or 'none'}"
            + (f"; risky: {', '.join(r['method'] for r in risky)}." if risky else ".")
        ),
    }


# --------------------------------------------------------------------------- #
# 5. waf_detector
# --------------------------------------------------------------------------- #
# name -> {"headers": {hdr: regex}, "values": [regex over any header value], "body": [regex]}
_WAF_SIGS = [
    ("Cloudflare", {
        "headers": ["cf-ray", "cf-cache-status", "cf-mitigated"],
        "server": r"cloudflare",
        "cookie": r"__cfduid|__cf_bm|cf_clearance",
        "body": [r"attention required.*cloudflare", r"cf-error-details", r"/cdn-cgi/"],
    }),
    ("Akamai", {
        "headers": ["x-akamai-transformed", "akamai-grn"],
        "server": r"akamaighost|akamai",
        "cookie": r"ak_bmsc|akaalb_|bm_sz",
        "body": [r"akamai", r"reference\s*#\d+\.\w+"],
    }),
    ("Sucuri CloudProxy", {
        "headers": ["x-sucuri-id", "x-sucuri-cache"],
        "server": r"sucuri",
        "cookie": r"",
        "body": [r"sucuri website firewall", r"cloudproxy"],
    }),
    ("Imperva Incapsula", {
        "headers": ["x-iinfo", "x-cdn"],
        "server": r"incapsula|imperva",
        "cookie": r"incap_ses|visid_incap|nlbi_",
        "body": [r"incapsula incident id", r"_incapsula_resource", r"powered by imperva"],
    }),
    ("AWS WAF / CloudFront", {
        "headers": ["x-amz-cf-id", "x-amzn-requestid", "x-amzn-waf-action"],
        "server": r"awselb|cloudfront|aws",
        "cookie": r"aws-waf-token|awsalb",
        "body": [r"request blocked.*aws", r"<!-- aws waf"],
    }),
]


async def waf_detector(url: str = "") -> dict:
    """Detect a WAF/CDN by matching signatures in response headers, cookies and body."""
    try:
        target = _normalize_url(url)
    except ValueError as e:
        return {"error": str(e)}
    try:
        async with _client() as client:
            resp = await client.get(target)
    except Exception as e:  # noqa: BLE001
        return _err(e)

    h = resp.headers
    server = h.get("server", "").lower()
    powered = h.get("x-powered-by", "").lower()
    cookies = " ".join(resp.headers.get_list("set-cookie")).lower()
    body = _safe_text(resp).lower()
    header_keys = {k.lower() for k in h.keys()}

    detected: list[dict] = []
    for name, sig in _WAF_SIGS:
        evidence: list[str] = []
        for hk in sig.get("headers", []):
            if hk in header_keys:
                evidence.append(f"header '{hk}' present")
        srv_re = sig.get("server")
        if srv_re and (re.search(srv_re, server) or re.search(srv_re, powered)):
            evidence.append(f"Server/X-Powered-By matches /{srv_re}/")
        ck_re = sig.get("cookie")
        if ck_re and re.search(ck_re, cookies):
            evidence.append("identifying cookie present")
        for brx in sig.get("body", []):
            if re.search(brx, body):
                evidence.append(f"body matches /{brx}/")
                break
        if evidence:
            detected.append({"waf": name, "evidence": evidence})

    return {
        "url": str(resp.url),
        "status": resp.status_code,
        "detected": detected,
        "waf_present": bool(detected),
        "server_header": h.get("server"),
        "summary": (
            "Detected: " + ", ".join(d["waf"] for d in detected)
            if detected else
            "No known WAF/CDN signature matched (a WAF may still be present but silent)."
        ),
    }


# --------------------------------------------------------------------------- #
# 6. tech_detector
# --------------------------------------------------------------------------- #
_BODY_TECH = [
    ("WordPress", [r"/wp-content/", r"/wp-includes/", r'name="generator" content="WordPress']),
    ("Drupal", [r"drupal\.js", r'name="generator" content="Drupal', r"/sites/default/files/"]),
    ("Joomla", [r"/media/jui/", r'name="generator" content="Joomla']),
    ("React", [r"data-reactroot", r"react-dom", r"__REACT_DEVTOOLS_GLOBAL_HOOK__"]),
    ("Vue.js", [r"data-v-[0-9a-f]{8}", r"vue(?:\.min)?\.js", r"__VUE__"]),
    ("Angular", [r"ng-version=", r"ng-app", r"angular(?:\.min)?\.js"]),
    ("Next.js", [r"/_next/static/", r'id="__next"', r"__NEXT_DATA__"]),
    ("Nuxt.js", [r"/_nuxt/", r"window\.__NUXT__"]),
    ("jQuery", [r"jquery(?:[-.]\d|\.min)?\.js"]),
    ("Bootstrap", [r"bootstrap(?:\.min)?\.(?:css|js)"]),
    ("Shopify", [r"cdn\.shopify\.com", r"Shopify\.theme"]),
    ("Cloudflare", [r"/cdn-cgi/"]),
]
_SERVER_TECH = [
    ("nginx", r"nginx"),
    ("Apache", r"apache"),
    ("Microsoft IIS", r"microsoft-iis|iis"),
    ("LiteSpeed", r"litespeed"),
    ("Caddy", r"caddy"),
    ("Express", r"express"),
    ("PHP", r"php"),
    ("ASP.NET", r"asp\.net"),
    ("OpenResty", r"openresty"),
    ("Gunicorn", r"gunicorn"),
    ("Werkzeug/Flask", r"werkzeug"),
    ("Kestrel", r"kestrel"),
]


async def tech_detector(url: str = "") -> dict:
    """Fingerprint server software and front-end frameworks from headers + body."""
    try:
        target = _normalize_url(url)
    except ValueError as e:
        return {"error": str(e)}
    try:
        async with _client() as client:
            resp = await client.get(target)
    except Exception as e:  # noqa: BLE001
        return _err(e)

    h = resp.headers
    server = h.get("server", "")
    powered = h.get("x-powered-by", "")
    hay = f"{server} {powered}".lower()
    body = _safe_text(resp)

    tech: list[dict] = []

    for label, rx in _SERVER_TECH:
        if re.search(rx, hay):
            tech.append({"name": label, "source": "header"})

    cookies = " ".join(resp.headers.get_list("set-cookie")).lower()
    if "phpsessid" in cookies:
        tech.append({"name": "PHP (PHPSESSID cookie)", "source": "cookie"})
    if "asp.net_sessionid" in cookies or "aspnet" in cookies:
        tech.append({"name": "ASP.NET (session cookie)", "source": "cookie"})
    if "jsessionid" in cookies:
        tech.append({"name": "Java (JSESSIONID cookie)", "source": "cookie"})
    if "laravel_session" in cookies:
        tech.append({"name": "Laravel (PHP)", "source": "cookie"})

    for label, patterns in _BODY_TECH:
        for pat in patterns:
            if re.search(pat, body, re.I):
                tech.append({"name": label, "source": "body"})
                break

    gen = re.search(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)', body, re.I)
    if gen:
        tech.append({"name": gen.group(1).strip()[:80], "source": "meta generator"})

    # De-duplicate by name, keep first source seen.
    seen: dict[str, dict] = {}
    for t in tech:
        seen.setdefault(t["name"], t)
    tech_list = list(seen.values())

    return {
        "url": str(resp.url),
        "status": resp.status_code,
        "server_header": server or None,
        "x_powered_by": powered or None,
        "technologies": tech_list,
        "summary": (
            "Detected: " + ", ".join(t["name"] for t in tech_list)
            if tech_list else "No specific technologies fingerprinted."
        ),
    }


# --------------------------------------------------------------------------- #
# 7. robots_sitemap
# --------------------------------------------------------------------------- #
async def robots_sitemap(url: str = "") -> dict:
    """Fetch /robots.txt and /sitemap.xml; list Disallow rules and sitemap URLs."""
    try:
        target = _normalize_url(url)
    except ValueError as e:
        return {"error": str(e)}
    origin = _origin(target)
    out: dict = {"origin": origin}

    try:
        async with _client() as client:
            # robots.txt
            disallowed: list[str] = []
            sitemaps_from_robots: list[str] = []
            try:
                r = await client.get(urljoin(origin + "/", "robots.txt"))
                out["robots_status"] = r.status_code
                if r.status_code == 200 and "text" in r.headers.get("content-type", "text"):
                    text = _safe_text(r)
                    out["robots_present"] = True
                    for line in text.splitlines():
                        line = line.strip()
                        if line.lower().startswith("disallow:"):
                            val = line.split(":", 1)[1].strip()
                            if val:
                                disallowed.append(val)
                        elif line.lower().startswith("sitemap:"):
                            sitemaps_from_robots.append(line.split(":", 1)[1].strip())
                    out["robots_excerpt"] = text[:1500]
                else:
                    out["robots_present"] = False
            except httpx.HTTPError as e:
                out["robots_error"] = str(e)
                out["robots_present"] = False

            out["disallowed"] = disallowed[:200]
            out["disallow_count"] = len(disallowed)

            # sitemap.xml (prefer one referenced by robots, else the default path)
            sitemap_url = sitemaps_from_robots[0] if sitemaps_from_robots else urljoin(
                origin + "/", "sitemap.xml")
            urls: list[str] = []
            try:
                s = await client.get(sitemap_url)
                out["sitemap_url"] = sitemap_url
                out["sitemap_status"] = s.status_code
                if s.status_code == 200:
                    body = _safe_text(s)
                    urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body, re.I)
                    out["sitemap_present"] = bool(urls) or "<urlset" in body or "<sitemapindex" in body
                else:
                    out["sitemap_present"] = False
            except httpx.HTTPError as e:
                out["sitemap_error"] = str(e)
                out["sitemap_present"] = False

            out["sitemap_urls"] = urls[:200]
            out["sitemap_url_count"] = len(urls)
            if sitemaps_from_robots:
                out["sitemaps_declared_in_robots"] = sitemaps_from_robots[:20]
    except Exception as e:  # noqa: BLE001
        return _err(e)

    out["summary"] = (
        f"{out.get('disallow_count', 0)} Disallow rule(s); "
        f"{out.get('sitemap_url_count', 0)} sitemap URL(s) found."
    )
    return out


# --------------------------------------------------------------------------- #
# 8. open_redirect_check
# --------------------------------------------------------------------------- #
_REDIRECT_PARAMS = ["next", "url", "redirect", "return", "returnUrl", "dest", "destination"]


async def open_redirect_check(url: str = "", param: str = "next") -> dict:
    """Append a redirect param pointing off-site and observe if a 3xx Location leaves the origin."""
    try:
        target = _normalize_url(url)
    except ValueError as e:
        return {"error": str(e)}
    param = (param or "next").strip() or "next"
    canary_host = "example.org"
    canary = f"https://{canary_host}/talos-redir-probe"

    # Build test URL preserving any existing query string.
    p = urlparse(target)
    sep = "&" if p.query else "?"
    test_url = f"{target}{sep}{param}={canary}"

    try:
        # Do NOT follow redirects here — we want to inspect the raw Location.
        async with _client(follow_redirects=False) as client:
            resp = await client.get(test_url)
    except Exception as e:  # noqa: BLE001
        return _err(e)

    location = resp.headers.get("location", "")
    off_site = False
    where = None
    if location:
        abs_loc = urljoin(test_url, location)
        loc_host = (urlparse(abs_loc).hostname or "").lower()
        where = abs_loc
        if loc_host and loc_host == canary_host:
            off_site = True

    is_3xx = 300 <= resp.status_code < 400
    vulnerable = bool(is_3xx and off_site)

    return {
        "tested_url": test_url,
        "parameter": param,
        "status": resp.status_code,
        "location_header": location or None,
        "redirects_off_site": off_site,
        "redirect_target": where,
        "likely_open_redirect": vulnerable,
        "note": (
            "Observational only: a 3xx Location pointing to the injected external "
            "host indicates a probable open redirect. Confirm manually before reporting."
        ),
        "recommendation": (
            "Validate redirect targets against an allow-list of paths/hosts; never "
            "redirect to an unvalidated user-supplied URL."
            if vulnerable else
            "No off-site redirect observed for this parameter."
        ),
        "other_params_to_try": [x for x in _REDIRECT_PARAMS if x != param],
    }


# --------------------------------------------------------------------------- #
# 9. graphql_introspection
# --------------------------------------------------------------------------- #
_INTROSPECTION_QUERY = (
    "{__schema{queryType{name} types{name kind}}}"
)


async def graphql_introspection(url: str = "", path: str = "/graphql") -> dict:
    """POST a tiny introspection query and report whether the GraphQL schema is exposed."""
    try:
        base = _normalize_url(url)
    except ValueError as e:
        return {"error": str(e)}
    # If the user already passed a full /graphql URL, respect its path.
    if urlparse(base).path not in ("", "/"):
        endpoint = base
    else:
        endpoint = urljoin(_origin(base) + "/", (path or "/graphql").lstrip("/"))

    payload = {"query": _INTROSPECTION_QUERY}
    try:
        async with _client() as client:
            resp = await client.post(
                endpoint,
                json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
    except Exception as e:  # noqa: BLE001
        return _err(e)

    body = _safe_text(resp)
    exposed = False
    type_count = None
    parse_note = None
    try:
        data = json.loads(body)
        schema = (data.get("data") or {}).get("__schema")
        if schema and isinstance(schema.get("types"), list):
            exposed = True
            type_count = len(schema["types"])
        elif data.get("errors"):
            parse_note = "Endpoint responded with GraphQL errors (introspection may be disabled)."
    except (ValueError, AttributeError):
        # Fall back to a loose signature match.
        if '"__schema"' in body and '"types"' in body:
            exposed = True
        parse_note = "Response was not valid JSON; matched on signature instead."

    return {
        "endpoint": endpoint,
        "status": resp.status_code,
        "introspection_enabled": exposed,
        "type_count": type_count,
        "content_type": resp.headers.get("content-type"),
        "response_excerpt": body[:600],
        "note": parse_note,
        "recommendation": (
            "Disable GraphQL introspection in production to avoid exposing your full "
            "schema (queries, mutations, types) to attackers."
            if exposed else
            "Introspection does not appear to be enabled at this endpoint."
        ),
    }


# --------------------------------------------------------------------------- #
# 10. sri_check
# --------------------------------------------------------------------------- #
def _host_of(u: str) -> str:
    return (urlparse(u).hostname or "").lower()


async def sri_check(url: str = "") -> dict:
    """Parse HTML and flag external <script>/<link> resources lacking Subresource Integrity."""
    try:
        target = _normalize_url(url)
    except ValueError as e:
        return {"error": str(e)}
    try:
        async with _client() as client:
            resp = await client.get(target)
    except Exception as e:  # noqa: BLE001
        return _err(e)

    base_url = str(resp.url)
    page_host = _host_of(base_url)
    html = _safe_text(resp)

    # Grab each <script ...> and <link ...> opening tag (full tag, not just the
    # element name — note the non-capturing group so findall returns whole tags).
    tags = re.findall(r"<(?:script|link)\b[^>]*>", html, re.I)
    external_missing: list[dict] = []
    external_ok = 0
    examined = 0

    for tag in tags:
        low = tag.lower()
        if low.startswith("<script"):
            m = re.search(r'src\s*=\s*["\']([^"\']+)["\']', tag, re.I)
            kind = "script"
        else:  # <link>
            rel = re.search(r'rel\s*=\s*["\']([^"\']+)["\']', tag, re.I)
            # Only stylesheets / preloaded scripts are SRI-relevant.
            if not rel or not re.search(r"stylesheet|preload|modulepreload", rel.group(1), re.I):
                continue
            m = re.search(r'href\s*=\s*["\']([^"\']+)["\']', tag, re.I)
            kind = "link"
        if not m:
            continue
        src = m.group(1).strip()
        if src.startswith(("data:", "blob:", "javascript:", "#")):
            continue
        abs_src = urljoin(base_url, src)
        src_host = _host_of(abs_src)
        # External = different host than the page (protocol-relative + absolute).
        is_external = bool(src_host) and src_host != page_host
        if not is_external:
            continue
        examined += 1
        has_integrity = bool(re.search(r"\bintegrity\s*=", tag, re.I))
        if has_integrity:
            external_ok += 1
        else:
            external_missing.append({
                "type": kind,
                "resource": abs_src[:300],
                "host": src_host,
            })

    return {
        "url": base_url,
        "status": resp.status_code,
        "external_resources_examined": examined,
        "with_integrity": external_ok,
        "missing_integrity_count": len(external_missing),
        "missing_integrity": external_missing[:100],
        "recommendation": (
            "Add an integrity=\"sha384-...\" attribute (plus crossorigin=\"anonymous\") "
            "to every externally hosted <script>/<link>, so a compromised CDN cannot "
            "serve tampered code."
            if external_missing else
            "All examined external resources include an integrity attribute (or none were found)."
        ),
        "summary": (
            f"{len(external_missing)} of {examined} external resource(s) lack SRI."
            if examined else "No cross-origin script/style resources found in the HTML."
        ),
    }


# --------------------------------------------------------------------------- #
# 11. admin_finder
# --------------------------------------------------------------------------- #
_ADMIN_PATHS = [
    "/admin", "/administrator", "/admin/login", "/wp-admin/", "/wp-login.php",
    "/login", "/user/login", "/admin.php", "/cpanel", "/phpmyadmin/",
    "/manager/html", "/.git/config", "/server-status", "/admin/dashboard",
    "/api/admin",
]


async def admin_finder(url: str = "") -> dict:
    """HEAD a small built-in list of common admin/login paths; report 200/401/403 hits."""
    try:
        target = _normalize_url(url)
    except ValueError as e:
        return {"error": str(e)}
    origin = _origin(target)

    interesting = {200, 401, 403}
    hits: list[dict] = []
    checked = 0
    try:
        async with _client() as client:
            for path in _ADMIN_PATHS:
                full = urljoin(origin + "/", path.lstrip("/"))
                checked += 1
                try:
                    r = await client.head(full)
                    code = r.status_code
                    # Some servers reject HEAD (405) — retry once with GET to confirm.
                    if code == 405:
                        r = await client.get(full)
                        code = r.status_code
                except httpx.HTTPError:
                    continue
                if code in interesting:
                    label = {
                        200: "reachable (200 OK)",
                        401: "auth required (401)",
                        403: "forbidden but present (403)",
                    }[code]
                    hits.append({
                        "path": path,
                        "url": str(r.url),
                        "status": code,
                        "note": label,
                    })
    except Exception as e:  # noqa: BLE001
        return _err(e)

    return {
        "origin": origin,
        "paths_checked": checked,
        "hits": hits,
        "hit_count": len(hits),
        "note": (
            "Observational discovery only — a 200/401/403 means the path exists and may "
            "host an admin/login surface. This does not attempt any authentication."
        ),
        "recommendation": (
            "Restrict admin panels to trusted networks/VPN, enforce strong auth + MFA, "
            "and remove anything unintentionally exposed."
            if hits else "None of the common admin paths returned 200/401/403."
        ),
    }


# --------------------------------------------------------------------------- #
# SPECS
# --------------------------------------------------------------------------- #
_URL_INPUT = {
    "key": "url",
    "label": "Target URL",
    "type": "text",
    "placeholder": "https://example.com",
}

SPECS = [
    {
        "name": "security_header_grader",
        "label": "Security Header Grader",
        "description": "Grade HSTS/CSP/X-Frame/X-Content-Type/Referrer/Permissions headers A–F with exact fixes.",
        "category": "Web App Testing",
        "tier": "green",
        "inputs": [_URL_INPUT],
    },
    {
        "name": "cookie_analyzer",
        "label": "Cookie Analyzer",
        "description": "Flag missing Secure / HttpOnly / SameSite attributes on Set-Cookie headers.",
        "category": "Web App Testing",
        "tier": "green",
        "inputs": [_URL_INPUT],
    },
    {
        "name": "cors_check",
        "label": "CORS Misconfiguration Check",
        "description": "Send Origin: https://evil.example and inspect Access-Control-Allow-* for reflection.",
        "category": "Web App Testing",
        "tier": "green",
        "inputs": [_URL_INPUT],
    },
    {
        "name": "http_methods_check",
        "label": "HTTP Methods Check",
        "description": "Send OPTIONS and report the Allow header, flagging PUT/DELETE/TRACE and friends.",
        "category": "Web App Testing",
        "tier": "green",
        "inputs": [_URL_INPUT],
    },
    {
        "name": "waf_detector",
        "label": "WAF / CDN Detector",
        "description": "Signature-match headers/cookies/body for Cloudflare, Akamai, Sucuri, Imperva, AWS WAF.",
        "category": "Web App Testing",
        "tier": "green",
        "inputs": [_URL_INPUT],
    },
    {
        "name": "tech_detector",
        "label": "Tech Stack Detector",
        "description": "Fingerprint Server/X-Powered-By and body signatures (WordPress, React, nginx, etc.).",
        "category": "Web App Testing",
        "tier": "green",
        "inputs": [_URL_INPUT],
    },
    {
        "name": "robots_sitemap",
        "label": "robots.txt & Sitemap",
        "description": "Fetch /robots.txt and /sitemap.xml; list Disallow rules and sitemap URLs.",
        "category": "Web App Testing",
        "tier": "green",
        "inputs": [_URL_INPUT],
    },
    {
        "name": "open_redirect_check",
        "label": "Open Redirect Check",
        "description": "Append an off-site redirect param and observe whether a 3xx Location leaves the origin.",
        "category": "Web App Testing",
        "tier": "yellow",
        "inputs": [
            _URL_INPUT,
            {
                "key": "param",
                "label": "Redirect parameter",
                "type": "select",
                "placeholder": "next",
                "options": ["next", "url", "redirect", "return", "returnUrl", "dest", "destination"],
            },
        ],
    },
    {
        "name": "graphql_introspection",
        "label": "GraphQL Introspection Check",
        "description": "POST a tiny introspection query to /graphql and report if the schema is exposed.",
        "category": "Web App Testing",
        "tier": "yellow",
        "inputs": [
            _URL_INPUT,
            {
                "key": "path",
                "label": "GraphQL path",
                "type": "text",
                "placeholder": "/graphql",
            },
        ],
    },
    {
        "name": "sri_check",
        "label": "Subresource Integrity (SRI) Check",
        "description": "Parse HTML and flag external <script>/<link> resources missing an integrity attribute.",
        "category": "Web App Testing",
        "tier": "green",
        "inputs": [_URL_INPUT],
    },
    {
        "name": "admin_finder",
        "label": "Admin Panel Finder",
        "description": "HEAD ~15 common admin/login paths and report which return 200/401/403.",
        "category": "Web App Testing",
        "tier": "yellow",
        "inputs": [_URL_INPUT],
    },
]
