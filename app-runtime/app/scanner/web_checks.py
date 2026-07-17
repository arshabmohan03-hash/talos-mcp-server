"""HTTP-layer checks: security headers, cookies, info disclosure,
fingerprinting, exposed files. All non-destructive GET/HEAD requests."""
from __future__ import annotations

import asyncio
import re

import httpx

from .models import Finding, Severity
from .net import Target

OWASP_HEADERS = "https://owasp.org/www-project-secure-headers/"

# (header, severity_if_missing, title, description, recommendation)
SECURITY_HEADERS = [
    (
        "strict-transport-security", Severity.HIGH,
        "Missing HSTS header",
        "Strict-Transport-Security is not set, so browsers may connect over "
        "plain HTTP and are exposed to SSL-stripping / downgrade attacks.",
        "Add: Strict-Transport-Security: max-age=63072000; includeSubDomains; preload",
    ),
    (
        "content-security-policy", Severity.MEDIUM,
        "Missing Content-Security-Policy",
        "No CSP is defined. CSP is the primary defense against cross-site "
        "scripting (XSS) and data-injection attacks.",
        "Define a Content-Security-Policy starting from default-src 'self'.",
    ),
    (
        "x-content-type-options", Severity.LOW,
        "Missing X-Content-Type-Options",
        "Without 'nosniff', browsers may MIME-sniff responses and execute "
        "content as an unintended type.",
        "Add: X-Content-Type-Options: nosniff",
    ),
    (
        "referrer-policy", Severity.LOW,
        "Missing Referrer-Policy",
        "No Referrer-Policy is set; full URLs (possibly with sensitive query "
        "params) may leak to third-party sites.",
        "Add: Referrer-Policy: strict-origin-when-cross-origin",
    ),
    (
        "permissions-policy", Severity.INFO,
        "Missing Permissions-Policy",
        "No Permissions-Policy; the site does not restrict powerful browser "
        "features (camera, geolocation, etc.).",
        "Add a Permissions-Policy limiting features you don't use.",
    ),
]


def check_security_headers(resp: httpx.Response) -> list[Finding]:
    findings: list[Finding] = []
    h = resp.headers

    for name, sev, title, desc, rec in SECURITY_HEADERS:
        if name not in h:
            # HSTS only meaningful over HTTPS
            if name == "strict-transport-security" and resp.url.scheme != "https":
                continue
            findings.append(Finding(
                id=f"hdr-{name}", title=title, severity=sev,
                category="Security headers", description=desc,
                recommendation=rec, references=[OWASP_HEADERS],
            ))

    # Clickjacking: needs X-Frame-Options OR CSP frame-ancestors
    csp = h.get("content-security-policy", "")
    if "x-frame-options" not in h and "frame-ancestors" not in csp.lower():
        findings.append(Finding(
            id="hdr-clickjacking", title="No clickjacking protection",
            severity=Severity.MEDIUM, category="Security headers",
            description="Neither X-Frame-Options nor CSP 'frame-ancestors' is "
                        "set, so the page can be embedded in a malicious iframe "
                        "(clickjacking).",
            recommendation="Add X-Frame-Options: DENY or CSP frame-ancestors 'none'.",
            references=[OWASP_HEADERS],
        ))
    return findings


def check_cookies(resp: httpx.Response) -> list[Finding]:
    findings: list[Finding] = []
    # httpx exposes multiple Set-Cookie via .headers.get_list
    raw_cookies = resp.headers.get_list("set-cookie")
    for raw in raw_cookies:
        name = raw.split("=", 1)[0].strip()
        low = raw.lower()
        missing = []
        if "secure" not in low and resp.url.scheme == "https":
            missing.append("Secure")
        if "httponly" not in low:
            missing.append("HttpOnly")
        if "samesite" not in low:
            missing.append("SameSite")
        if missing:
            sev = Severity.MEDIUM if "HttpOnly" in missing or "Secure" in missing else Severity.LOW
            findings.append(Finding(
                id=f"cookie-{name}", title=f"Cookie '{name}' missing flags: {', '.join(missing)}",
                severity=sev, category="Cookies",
                description=f"The cookie '{name}' is set without the "
                            f"{', '.join(missing)} attribute(s), increasing risk "
                            "of theft (XSS) or CSRF.",
                evidence=raw[:160],
                recommendation="Set Secure; HttpOnly; SameSite=Lax (or Strict) on session cookies.",
                references=["https://developer.mozilla.org/docs/Web/HTTP/Cookies"],
            ))
    return findings


VERSION_RE = re.compile(r"/?(\d+\.[\d.]+)")


def check_information_disclosure(resp: httpx.Response) -> tuple[list[Finding], list[tuple[str, str]]]:
    """Returns (findings, [(product, version), ...]) for CVE follow-up."""
    findings: list[Finding] = []
    detections: list[tuple[str, str]] = []
    h = resp.headers

    for hdr in ("server", "x-powered-by", "x-aspnet-version", "x-generator"):
        val = h.get(hdr)
        if not val:
            continue
        m = VERSION_RE.search(val)
        if m:
            product = re.split(r"[/ ]", val.strip())[0]
            version = m.group(1)
            detections.append((product, version))
            findings.append(Finding(
                id=f"disc-{hdr}", title=f"Software version disclosed via '{hdr}'",
                severity=Severity.LOW, category="Information disclosure",
                description=f"The '{hdr}' header reveals '{val}'. Exposing exact "
                            "versions helps attackers match known CVEs.",
                evidence=f"{hdr}: {val}",
                recommendation=f"Suppress or genericize the {hdr} header.",
            ))
    # Directory listing
    body_start = (resp.text[:2000] if resp.headers.get("content-type", "").startswith("text") else "")
    if "<title>Index of /" in body_start or ">Index of /<" in body_start:
        findings.append(Finding(
            id="disc-dirlist", title="Directory listing enabled",
            severity=Severity.MEDIUM, category="Information disclosure",
            description="The server returns an auto-generated directory index, "
                        "exposing file names that should not be browsable.",
            recommendation="Disable autoindex / directory listing on the web server.",
        ))
    return findings, detections


def fingerprint(resp: httpx.Response, html: str) -> list[Finding]:
    h = resp.headers
    tech: list[str] = []
    if "x-powered-by" in h:
        tech.append(h["x-powered-by"])
    if "server" in h:
        tech.append(h["server"])
    set_cookie = " ".join(resp.headers.get_list("set-cookie")).lower()
    if "phpsessid" in set_cookie:
        tech.append("PHP (PHPSESSID)")
    if "wp-content" in html or "/wp-includes/" in html:
        tech.append("WordPress")
    m = re.search(r'<meta name="generator" content="([^"]+)"', html, re.I)
    if m:
        tech.append(m.group(1))
    if not tech:
        return []
    return [Finding(
        id="fingerprint", title="Detected technologies",
        severity=Severity.INFO, category="Fingerprint",
        description="Technology stack inferred from headers and page content.",
        evidence="; ".join(dict.fromkeys(tech))[:300],
        recommendation="Informational — confirms attack surface to harden.",
    )]


def check_https_redirect(target: Target, resp: httpx.Response) -> list[Finding]:
    if target.scheme == "http" and resp.url.scheme != "https":
        return [Finding(
            id="no-https-redirect", title="HTTP not redirected to HTTPS",
            severity=Severity.MEDIUM, category="Transport",
            description="The site serves content over plain HTTP without "
                        "forcing an HTTPS redirect; traffic can be intercepted.",
            recommendation="Redirect all HTTP traffic to HTTPS (301) and enable HSTS.",
        )]
    return []


def check_mixed_content(resp: httpx.Response, html: str) -> list[Finding]:
    if resp.url.scheme != "https":
        return []
    insecure = re.findall(r'(?:src|href)=["\'](http://[^"\']+)', html, re.I)
    if insecure:
        return [Finding(
            id="mixed-content", title="Mixed content (HTTP resources on HTTPS page)",
            severity=Severity.LOW, category="Transport",
            description=f"The HTTPS page references {len(insecure)} resource(s) "
                        "over insecure HTTP, which browsers may block or which "
                        "weaken the page's security.",
            evidence=insecure[0][:160],
            recommendation="Load all sub-resources over HTTPS.",
        )]
    return []


# --- Exposed sensitive files (safe GETs against well-known paths) ---
EXPOSED_PATHS = [
    ("/.env", "DB_|API_|SECRET|PASSWORD|=", Severity.CRITICAL, "Environment file (.env) exposed"),
    ("/.git/HEAD", "ref:", Severity.HIGH, "Git repository exposed (.git/HEAD)"),
    ("/.git/config", "[core]", Severity.HIGH, "Git config exposed (.git/config)"),
    ("/.svn/entries", "", Severity.MEDIUM, "SVN metadata exposed"),
    ("/.DS_Store", "", Severity.LOW, "macOS .DS_Store exposed"),
    ("/phpinfo.php", "phpinfo()", Severity.HIGH, "phpinfo() page exposed"),
    ("/server-status", "Apache Status", Severity.MEDIUM, "Apache server-status exposed"),
    ("/.htaccess", "", Severity.LOW, ".htaccess readable"),
    ("/backup.zip", "", Severity.MEDIUM, "Backup archive exposed (backup.zip)"),
    ("/wp-config.php.bak", "DB_PASSWORD", Severity.CRITICAL, "WordPress config backup exposed"),
]


async def _baseline_soft404(client: httpx.AsyncClient, base: str) -> int | None:
    """Length of the response for a random non-existent path that returns 200,
    used to suppress false positives on sites that 200 everything."""
    try:
        r = await client.get(base.rstrip("/") + "/zzq-nonexistent-9f3a1c.html", timeout=6.0)
        if r.status_code == 200:
            return len(r.content)
    except httpx.HTTPError:
        pass
    return None


async def check_exposed_files(client: httpx.AsyncClient, base: str) -> list[Finding]:
    findings: list[Finding] = []
    base = base.rstrip("/")
    soft404_len = await _baseline_soft404(client, base)

    async def probe(path: str, sig: str, sev: Severity, title: str) -> Finding | None:
        try:
            r = await client.get(base + path, timeout=6.0)
        except httpx.HTTPError:
            return None
        if r.status_code != 200 or not r.content:
            return None
        # suppress soft-404 false positives
        if soft404_len is not None and abs(len(r.content) - soft404_len) < 32:
            return None
        body = r.text[:4000]
        if sig and not re.search(sig, body, re.I):
            return None
        return Finding(
            id=f"exposed-{path.strip('/').replace('/', '-')}",
            title=title, severity=sev, category="Exposed files",
            description=f"A sensitive resource is publicly reachable at {path}. "
                        "This can leak source code, credentials, or internals.",
            evidence=f"GET {path} -> 200 ({len(r.content)} bytes)",
            recommendation=f"Block public access to {path} at the web server / deny-list.",
        )

    results = await asyncio.gather(*(probe(*p) for p in EXPOSED_PATHS))
    findings.extend(f for f in results if f)
    return findings


async def check_security_txt(client: httpx.AsyncClient, base: str) -> list[Finding]:
    base = base.rstrip("/")
    for path in ("/.well-known/security.txt", "/security.txt"):
        try:
            r = await client.get(base + path)
            if r.status_code == 200 and "contact" in r.text.lower():
                return []  # present — good
        except httpx.HTTPError:
            continue
    return [Finding(
        id="no-security-txt", title="No security.txt",
        severity=Severity.INFO, category="Best practice",
        description="No /.well-known/security.txt was found; researchers have "
                    "no documented way to report vulnerabilities.",
        recommendation="Publish a security.txt with a contact address.",
        references=["https://securitytxt.org/"],
    )]
