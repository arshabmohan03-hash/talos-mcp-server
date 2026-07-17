"""Extra tools powered by extra pip libraries (pip-only, no API keys, defensive).

Each tool returns a JSON-serializable dict and never raises to the caller.
Libraries: codext, netaddr, phonenumbers, dnstwist, python-whois, ipwhois,
zxcvbn, yara-python, hashid, wafw00f, passlib/bcrypt/argon2, sympy, bs4.
"""
from __future__ import annotations

_ENC = "Crypto & Encoding"
_NET = "Network & Recon"
_WEB = "Web App Testing"
_OSINT = "OSINT & Threat Intel"
_FOR = "Forensics & Analysis"
_DEF = "Defensive / Blue-team"


def _err(msg, **extra):
    d = {"error": msg}
    d.update(extra)
    return d


# --------------------------------------------------------------------------- #
# Crypto & Encoding
# --------------------------------------------------------------------------- #
_CODEXT_SCHEMES = ["base64", "base85", "base91", "base58", "base62", "base32",
                   "morse", "braille", "leetspeak", "atbash", "rot-13", "url",
                   "octal", "binary", "dna-1"]


def codext_convert(text: str = "", scheme: str = "base64", mode: str = "encode") -> dict:
    """Encode/decode text with codext (CyberChef-style, 100s of codecs)."""
    text = text or ""
    if not text:
        return _err("Provide text to convert.")
    scheme = (scheme or "base64").strip()
    try:
        import codext
        out = codext.decode(text, scheme) if mode == "decode" else codext.encode(text, scheme)
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return {"scheme": scheme, "mode": mode, "input_len": len(text), "output": out}
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}",
                    hint="Pick a supported codec; codext has 100s (base*, morse, braille, leetspeak, …).")


def rsa_toolkit(n: str = "", e: str = "65537") -> dict:
    """Factor a small RSA modulus n and (with e) derive the private exponent d."""
    try:
        N = int(str(n).strip())
    except (TypeError, ValueError):
        return _err("Provide the modulus n as an integer.")
    if N < 2:
        return _err("n must be ≥ 2.")
    if N.bit_length() > 160:
        return _err("n is too large to factor in-browser (keep it ≤ 160 bits / ~48 digits).",
                    bits=N.bit_length())
    try:
        from sympy import factorint, mod_inverse
        f = factorint(N)
        factors = []
        for p, k in f.items():
            factors += [str(p)] * k
        out = {"n": str(N), "bits": N.bit_length(), "factors": factors,
               "is_prime": len(factors) == 1}
        if len(f) == 2 and all(k == 1 for k in f.values()) and str(e).strip():
            p, q = list(f.keys())
            phi = (p - 1) * (q - 1)
            try:
                out["e"] = str(e)
                out["d_private_exponent"] = str(mod_inverse(int(e), phi))
            except Exception:  # noqa: BLE001
                out["note"] = "e is not invertible mod φ(n)."
        return out
    except Exception as e2:  # noqa: BLE001
        return _err(f"{type(e2).__name__}: {e2}")


# --------------------------------------------------------------------------- #
# Network & Recon
# --------------------------------------------------------------------------- #
def subnet_calc(cidr: str = "") -> dict:
    """Subnet / CIDR calculator: network, mask, broadcast, host range, size."""
    c = (cidr or "").strip()
    if not c:
        return _err("Provide a CIDR or IP/netmask, e.g. 192.168.1.0/24.")
    try:
        from netaddr import IPNetwork
        net = IPNetwork(c)
        size = net.size
        info = {
            "input": c, "version": net.version, "network": str(net.network),
            "netmask": str(net.netmask), "hostmask": str(net.hostmask),
            "broadcast": str(net.broadcast) if net.broadcast is not None else None,
            "prefix_length": net.prefixlen, "total_addresses": size,
            "usable_hosts": max(0, size - 2) if (net.version == 4 and net.prefixlen < 31) else size,
            "first_host": str(net[1]) if size > 2 else str(net.network),
            "last_host": str(net[-2]) if size > 2 else str(net[-1]),
        }
        try:
            info["is_private"] = bool(net.is_private())
        except Exception:  # noqa: BLE001
            pass
        return info
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


def domain_whois_full(domain: str = "") -> dict:
    """Full registrar WHOIS for a domain via python-whois (complements RDAP)."""
    d = (domain or "").strip().lower()
    if not d or "." not in d:
        return _err("Provide a domain, e.g. example.com.")

    def _s(v):
        if v is None:
            return None
        if isinstance(v, (list, tuple, set)):
            return [str(x) for x in v][:10]
        return str(v)

    try:
        import whois
        w = whois.whois(d)
        return {
            "domain": d, "registrar": _s(w.registrar), "org": _s(w.org),
            "creation_date": _s(w.creation_date), "expiration_date": _s(w.expiration_date),
            "updated_date": _s(w.updated_date), "name_servers": _s(w.name_servers),
            "status": _s(w.status), "emails": _s(w.emails), "country": _s(w.country),
            "source": "python-whois",
        }
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}",
                    hint="Some TLDs block WHOIS — try the RDAP-based Domain Profiler instead.")


def ip_rdap_asn(ip: str = "") -> dict:
    """IP → ASN, org, and network via RDAP (ipwhois). Reliable ASN lookup."""
    t = (ip or "").strip()
    if not t:
        return _err("Provide an IP address.")
    try:
        import ipaddress
        ipaddress.ip_address(t)
    except ValueError:
        return _err("Provide a valid IPv4/IPv6 address.")
    try:
        from ipwhois import IPWhois
        r = IPWhois(t).lookup_rdap(depth=1)
        net = r.get("network") or {}
        return {
            "ip": t, "asn": r.get("asn"), "asn_description": r.get("asn_description"),
            "asn_country": r.get("asn_country_code"), "asn_cidr": r.get("asn_cidr"),
            "asn_registry": r.get("asn_registry"), "network_name": net.get("name"),
            "network_cidr": net.get("cidr"), "network_country": net.get("country"),
            "source": "RDAP (ipwhois)",
        }
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# Web App Testing
# --------------------------------------------------------------------------- #
def waf_detect_pro(url: str = "") -> dict:
    """Fingerprint the WAF/CDN in front of a site with wafw00f."""
    u = (url or "").strip()
    if not u:
        return _err("Provide a URL or domain.")
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    try:
        from wafw00f.main import WAFW00F
        w = WAFW00F(u)
        wafs = w.identwaf(findall=True) or []
        out = {"url": u, "waf_detected": bool(wafs), "wafs": list(wafs)}
        if not wafs:
            try:
                out["generic_protection"] = bool(w.genericdetect())
            except Exception:  # noqa: BLE001
                pass
            out["note"] = "No specific WAF fingerprinted (it may still be protected)."
        return out
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


async def link_harvester(url: str = "") -> dict:
    """Parse a page (bs4): links, external domains, scripts, and forms."""
    u = (url or "").strip()
    if not u:
        return _err("Provide a URL.")
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    try:
        import urllib.parse as up
        import httpx
        from bs4 import BeautifulSoup
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True,
                                     headers={"User-Agent": "Talos/1.0 (+authorized review)"}) as c:
            r = await c.get(u)
        soup = BeautifulSoup(r.text, "lxml")
        host = up.urlsplit(str(r.url)).netloc
        links, ext = set(), set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith(("javascript:", "#", "mailto:", "tel:")):
                continue
            full = up.urljoin(str(r.url), href)
            links.add(full)
            nl = up.urlsplit(full).netloc
            if nl and nl != host:
                ext.add(nl)
        scripts = [up.urljoin(str(r.url), s["src"]) for s in soup.find_all("script", src=True)]
        forms = [{
            "action": up.urljoin(str(r.url), f.get("action") or str(r.url)),
            "method": (f.get("method") or "get").upper(),
            "inputs": [i.get("name") for i in f.find_all(["input", "textarea", "select"]) if i.get("name")][:20],
        } for f in soup.find_all("form")]
        title = soup.title.string.strip() if soup.title and soup.title.string else None
        return {
            "url": str(r.url), "status": r.status_code, "title": title,
            "link_count": len(links), "links": sorted(links)[:60],
            "external_domains": sorted(ext)[:40], "scripts": scripts[:30],
            "form_count": len(forms), "forms": forms[:15],
        }
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# OSINT & Threat Intel
# --------------------------------------------------------------------------- #
_LINE_TYPE = {0: "fixed line", 1: "mobile", 2: "fixed line or mobile", 3: "toll free",
              4: "premium rate", 5: "shared cost", 6: "VoIP", 7: "personal number",
              8: "pager", 9: "UAN", 10: "voicemail", 27: "emergency", 28: "short code",
              99: "unknown"}


def phone_intel(number: str = "", region: str = "") -> dict:
    """Phone number intel: validity, country, carrier, line type, timezones."""
    num = (number or "").strip()
    if not num:
        return _err("Provide a phone number, ideally with country code (e.g. +14155552671).")
    try:
        import phonenumbers
        from phonenumbers import carrier, geocoder, timezone
        p = phonenumbers.parse(num, (region or "").strip().upper() or None)
        return {
            "input": num, "valid": phonenumbers.is_valid_number(p),
            "possible": phonenumbers.is_possible_number(p),
            "e164": phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.E164),
            "international": phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.INTERNATIONAL),
            "country_code": p.country_code,
            "region": geocoder.description_for_number(p, "en") or None,
            "carrier": carrier.name_for_number(p, "en") or None,
            "line_type": _LINE_TYPE.get(phonenumbers.number_type(p), "unknown"),
            "timezones": list(timezone.time_zones_for_number(p)),
        }
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}", hint="Include the country code, e.g. +1 415 555 2671.")


def typosquat_finder(domain: str = "", limit: int = 60) -> dict:
    """Generate typosquat / look-alike domains (dnstwist) — phishing candidates."""
    d = (domain or "").strip().lower()
    if not d or "." not in d:
        return _err("Provide a domain, e.g. example.com.")
    try:
        lim = max(10, min(200, int(limit or 60)))
    except (TypeError, ValueError):
        lim = 60
    try:
        import dnstwist
        fuzz = dnstwist.Fuzzer(d)
        fuzz.generate()
        perms = fuzz.permutations()
        rows = [{"type": p.get("fuzzer"), "domain": p.get("domain")}
                for p in perms if p.get("domain") and p.get("domain") != d]
        return {
            "domain": d, "total_permutations": len(rows), "shown": min(len(rows), lim),
            "permutations": rows[:lim],
            "note": "Offline permutations (not checked for registration). Many are squatting/phishing candidates.",
        }
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# Forensics & Analysis
# --------------------------------------------------------------------------- #
def yara_match(rule: str = "", sample: str = "") -> dict:
    """Match a sample against a YARA rule — pure-Python (no yara binary needed)."""
    if not (rule or "").strip():
        return _err("Provide a YARA rule.")
    try:
        import yara
    except Exception as e:  # noqa: BLE001
        return _err(f"yara-python unavailable: {e}")
    try:
        rules = yara.compile(source=rule)
    except yara.Error as e:
        return _err(f"Rule error: {e}")
    try:
        data = (sample or "")
        matches = rules.match(data=data.encode("utf-8", "replace") if isinstance(data, str) else data)
        out = []
        for m in matches:
            strs = []
            for s in getattr(m, "strings", []) or []:
                ident = getattr(s, "identifier", None)
                if ident is None and isinstance(s, (list, tuple)) and len(s) > 1:
                    ident = s[1]
                if ident:
                    strs.append(str(ident))
            out.append({"rule": m.rule, "tags": list(getattr(m, "tags", []) or []),
                        "matched_strings": strs[:20]})
        return {"matched": bool(out), "match_count": len(out), "matches": out,
                "engine": "yara-python (no external binary)"}
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


def hash_id_pro(hash_value: str = "") -> dict:
    """Identify a hash's algorithm(s) with hashid, incl. hashcat / john modes."""
    h = (hash_value or "").strip()
    if not h:
        return _err("Provide a hash value.")
    try:
        from hashid import HashID
        cands = []
        for p in HashID().identifyHash(h):
            cands.append({"name": p.name, "hashcat": p.hashcat, "john": p.john})
        if not cands:
            return {"hash": h, "length": len(h), "candidates": [],
                    "note": "No known algorithm matched this format."}
        return {"hash": h, "length": len(h), "count": len(cands), "candidates": cands[:15]}
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# Defensive / Blue-team
# --------------------------------------------------------------------------- #
def password_strength_pro(password: str = "") -> dict:
    """Realistic password strength with zxcvbn (score, crack time, feedback)."""
    pw = password or ""
    if not pw:
        return _err("Provide a password to evaluate.")
    try:
        from zxcvbn import zxcvbn
        r = zxcvbn(pw[:200])
        ct = r.get("crack_times_display") or {}
        fb = r.get("feedback") or {}
        labels = ["very weak", "weak", "fair", "strong", "very strong"]
        return {
            "score": r.get("score"), "rating": labels[int(r.get("score", 0))],
            "guesses": str(r.get("guesses")),
            "crack_time_offline_fast": ct.get("offline_fast_hashing_1e10_per_second"),
            "crack_time_online_throttled": ct.get("online_throttling_100_per_hour"),
            "warning": fb.get("warning") or None, "suggestions": fb.get("suggestions") or [],
        }
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


def password_hasher(password: str = "", algorithm: str = "bcrypt") -> dict:
    """Produce a salted, slow password hash (bcrypt / argon2 / pbkdf2 / sha256_crypt)."""
    pw = password or ""
    if not pw:
        return _err("Provide a password to hash.")
    algo = (algorithm or "bcrypt").strip().lower()
    try:
        if algo == "bcrypt":
            import bcrypt as _bcrypt  # use directly (passlib 1.7.4 ≠ bcrypt 5.x)
            h = _bcrypt.hashpw(pw.encode("utf-8")[:72], _bcrypt.gensalt(rounds=12)).decode("ascii")
        elif algo == "argon2":
            from passlib.hash import argon2
            h = argon2.hash(pw)
        elif algo in ("pbkdf2", "pbkdf2_sha256"):
            from passlib.hash import pbkdf2_sha256
            h = pbkdf2_sha256.hash(pw)
        elif algo in ("sha256", "sha256_crypt"):
            from passlib.hash import sha256_crypt
            h = sha256_crypt.hash(pw)
        else:
            return _err("Choose bcrypt, argon2, pbkdf2_sha256, or sha256_crypt.")
        return {"algorithm": algo, "hash": h,
                "note": "Salted one-way hash suitable for storing passwords. Never store plaintext."}
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
SPECS = [
    {"name": "codext_convert", "label": "Codext Converter", "tier": "green", "category": _ENC,
     "description": "Encode/decode with codext — CyberChef-style (base*, morse, braille, leetspeak, …).",
     "inputs": [{"key": "text", "label": "Text", "type": "textarea", "placeholder": "Hello"},
                {"key": "scheme", "label": "Codec", "type": "select", "options": _CODEXT_SCHEMES},
                {"key": "mode", "label": "Mode", "type": "select", "options": ["encode", "decode"]}]},
    {"name": "rsa_toolkit", "label": "RSA Toolkit", "tier": "edu", "category": _ENC,
     "description": "Factor a small RSA modulus n and derive the private exponent d (CTF).",
     "inputs": [{"key": "n", "label": "Modulus n", "type": "text", "placeholder": "3233"},
                {"key": "e", "label": "Public exponent e", "type": "text", "placeholder": "65537"}]},
    {"name": "subnet_calc", "label": "Subnet Calculator", "tier": "green", "category": _NET,
     "description": "CIDR math: network, mask, broadcast, host range and address count.",
     "inputs": [{"key": "cidr", "label": "CIDR / IP", "type": "text", "placeholder": "192.168.1.0/24"}]},
    {"name": "domain_whois_full", "label": "WHOIS (Full)", "tier": "green", "category": _NET,
     "description": "Full registrar WHOIS for a domain (registrar, dates, NS, status).",
     "inputs": [{"key": "domain", "label": "Domain", "type": "text", "placeholder": "example.com"}]},
    {"name": "ip_rdap_asn", "label": "IP RDAP / ASN", "tier": "green", "category": _NET,
     "description": "IP → ASN, org and network via RDAP (reliable ASN lookup).",
     "inputs": [{"key": "ip", "label": "IP address", "type": "text", "placeholder": "8.8.8.8"}]},
    {"name": "waf_detect_pro", "label": "WAF Detector (wafw00f)", "tier": "yellow", "category": _WEB,
     "description": "Fingerprint the WAF / CDN protecting a site (wafw00f, 150+ signatures).",
     "inputs": [{"key": "url", "label": "URL", "type": "text", "placeholder": "example.com"}]},
    {"name": "link_harvester", "label": "Link Harvester", "tier": "yellow", "category": _WEB,
     "description": "Parse a page: links, external domains, scripts and forms (bs4 + lxml).",
     "inputs": [{"key": "url", "label": "URL", "type": "text", "placeholder": "https://example.com"}]},
    {"name": "phone_intel", "label": "Phone Number Intel", "tier": "green", "category": _OSINT,
     "description": "Validate a phone number → country, carrier, line type, timezones.",
     "inputs": [{"key": "number", "label": "Phone number", "type": "text", "placeholder": "+1 415 555 2671"},
                {"key": "region", "label": "Region (optional)", "type": "text", "placeholder": "US"}]},
    {"name": "typosquat_finder", "label": "Typosquat Finder", "tier": "green", "category": _OSINT,
     "description": "Generate look-alike / typosquat domains (dnstwist) — phishing candidates.",
     "inputs": [{"key": "domain", "label": "Domain", "type": "text", "placeholder": "example.com"},
                {"key": "limit", "label": "Max results", "type": "number", "placeholder": "60"}]},
    {"name": "yara_match", "label": "YARA Match (Python)", "tier": "green", "category": _FOR,
     "description": "Match a sample against a YARA rule — pure-Python, no yara binary required.",
     "inputs": [{"key": "rule", "label": "YARA rule", "type": "textarea",
                 "placeholder": 'rule demo { strings: $a = "evil" condition: $a }'},
                {"key": "sample", "label": "Sample text", "type": "textarea", "placeholder": "the evil string"}]},
    {"name": "hash_id_pro", "label": "Hash Identifier (hashID)", "tier": "green", "category": _FOR,
     "description": "Identify a hash's algorithm(s) with hashID, incl. hashcat / john modes.",
     "inputs": [{"key": "hash_value", "label": "Hash", "type": "text",
                 "placeholder": "5f4dcc3b5aa765d61d8327deb882cf99"}]},
    {"name": "password_strength_pro", "label": "Password Strength (zxcvbn)", "tier": "green", "category": _DEF,
     "description": "Realistic password strength: score, crack time and improvement tips.",
     "inputs": [{"key": "password", "label": "Password", "type": "text", "placeholder": "correct horse battery"}]},
    {"name": "password_hasher", "label": "Password Hasher", "tier": "green", "category": _DEF,
     "description": "Produce a salted, slow one-way hash (bcrypt / argon2 / pbkdf2 / sha256_crypt).",
     "inputs": [{"key": "password", "label": "Password", "type": "text", "placeholder": "s3cr3t"},
                {"key": "algorithm", "label": "Algorithm", "type": "select",
                 "options": ["bcrypt", "argon2", "pbkdf2_sha256", "sha256_crypt"]}]},
]
