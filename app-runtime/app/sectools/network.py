"""Network & Recon tools for Talos (defensive security assistant).

Each public function is a self-contained tool that returns a JSON-serializable
dict and never raises to the caller (failures come back as {"error": ...}).

Only stdlib + httpx + cryptography are used. External lookups go through public
HTTPS services (DNS-over-HTTPS, RDAP, crt.sh, bgpview, macvendors) so no extra
libraries (dnspython, python-whois, ...) are required.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
from datetime import datetime, timezone

import httpx

# ---------------------------------------------------------------------------
# Shared constants / helpers
# ---------------------------------------------------------------------------

_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=8.0)
_USER_AGENT = "Talos-SecTools/1.0 (+defensive-recon)"

_ETHICAL_NOTE = (
    "Only scan systems you own or are explicitly authorized to test. "
    "Unauthorized scanning may be illegal."
)

# A small, sensible "common ports" set (~25 entries).
_COMMON_PORTS = [
    21, 22, 23, 25, 53, 80, 110, 111, 135, 139, 143, 443, 445, 465,
    587, 993, 995, 1723, 3306, 3389, 5432, 5900, 6379, 8080, 8443,
]

_MAX_PORTS = 1024
_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")


def _clean_host(host: str) -> str:
    """Strip scheme/path/port noise a user might paste, return a bare host."""
    host = (host or "").strip().strip("\"'<>")
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]
    # strip a trailing :port but keep bracketed IPv6 literals intact
    if host.startswith("[") and "]" in host:
        host = host[1:host.index("]")]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.strip().rstrip(".")


def _valid_hostname(host: str) -> bool:
    if not host or len(host) > 253:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(_HOST_RE.match(host))


def _new_client(follow_redirects: bool = False) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT,
        follow_redirects=follow_redirects,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json,*/*"},
    )


def _parse_ports(ports, *, default_common: bool = True) -> list[int]:
    """Turn a ports input (str/list/None/'common') into a sorted unique list.

    Accepts: "common", "22,80,443", "1-1024", "20-25,80", a list of ints, etc.
    """
    if ports is None or ports == "":
        return list(_COMMON_PORTS) if default_common else []

    if isinstance(ports, (list, tuple)):
        tokens: list[str] = [str(p) for p in ports]
    else:
        text = str(ports).strip().lower()
        if text in ("common", "default", "top", "top25"):
            return list(_COMMON_PORTS)
        if text in ("all",):  # guard against absurd ranges
            return list(range(1, _MAX_PORTS + 1))
        tokens = re.split(r"[\s,]+", text)

    out: set[int] = set()
    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        if "-" in tok:
            lo_s, hi_s = tok.split("-", 1)
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError:
                continue
            if lo > hi:
                lo, hi = hi, lo
            for p in range(lo, hi + 1):
                if 1 <= p <= 65535:
                    out.add(p)
        else:
            try:
                p = int(tok)
            except ValueError:
                continue
            if 1 <= p <= 65535:
                out.add(p)
    return sorted(out)


# ---------------------------------------------------------------------------
# Port scanner (async TCP connect)
# ---------------------------------------------------------------------------

async def _check_port(host: str, port: int, timeout: float, sem: asyncio.Semaphore):
    async with sem:
        try:
            fut = asyncio.open_connection(host, port)
            reader, writer = await asyncio.wait_for(fut, timeout=timeout)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            return port, True
        except (asyncio.TimeoutError, OSError):
            return port, False
        except Exception:  # noqa: BLE001
            return port, False


async def port_scanner(host: str = "", ports="common", timeout: float = 1.0) -> dict:
    """Async TCP-connect scan of a host over a port list (or the common set)."""
    host = _clean_host(host)
    if not _valid_hostname(host):
        return {"error": "Provide a valid host or IP address."}
    try:
        timeout = float(timeout) if timeout else 1.0
    except (TypeError, ValueError):
        timeout = 1.0
    timeout = max(0.2, min(timeout, 5.0))

    port_list = _parse_ports(ports, default_common=True)
    if not port_list:
        return {"error": "No valid ports to scan."}
    truncated = len(port_list) > _MAX_PORTS
    port_list = port_list[:_MAX_PORTS]

    sem = asyncio.Semaphore(200)
    try:
        results = await asyncio.gather(
            *(_check_port(host, p, timeout, sem) for p in port_list)
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Scan failed: {exc}", "ethical_note": _ETHICAL_NOTE}

    open_ports = sorted(p for p, is_open in results if is_open)
    named = [{"port": p, "service": _service_name(p)} for p in open_ports]
    return {
        "host": host,
        "scanned": len(port_list),
        "open_count": len(open_ports),
        "open_ports": open_ports,
        "open_ports_detail": named,
        "timeout_s": timeout,
        "truncated": truncated,
        "ethical_note": _ETHICAL_NOTE,
    }


def _service_name(port: int) -> str:
    try:
        return socket.getservbyport(port, "tcp")
    except OSError:
        return "unknown"


# ---------------------------------------------------------------------------
# Banner grabber
# ---------------------------------------------------------------------------

async def _read_banner(host: str, port: int, timeout: float, probe: bytes) -> dict:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (asyncio.TimeoutError, OSError) as exc:
        return {"error": f"Could not connect to {host}:{port} ({exc})."}

    banner = b""
    try:
        # Some services speak first; for HTTP-ish ports send a minimal request.
        if probe:
            writer.write(probe)
            await writer.drain()
        try:
            banner = await asyncio.wait_for(reader.read(2048), timeout=timeout)
        except asyncio.TimeoutError:
            banner = b""
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    text = banner.decode("utf-8", "replace").strip()
    return {
        "host": host,
        "port": port,
        "banner": text[:1500] if text else "",
        "bytes": len(banner),
        "empty": not text,
    }


async def banner_grabber(host: str = "", port: int = 0, timeout: float = 5.0) -> dict:
    """Connect to host:port, optionally probe, and read the service banner."""
    host = _clean_host(host)
    if not _valid_hostname(host):
        return {"error": "Provide a valid host or IP address."}
    try:
        port = int(port)
    except (TypeError, ValueError):
        return {"error": "Provide a valid TCP port (1-65535)."}
    if not 1 <= port <= 65535:
        return {"error": "Port must be between 1 and 65535."}
    try:
        timeout = max(1.0, min(float(timeout), 12.0))
    except (TypeError, ValueError):
        timeout = 5.0

    # Send a benign HTTP HEAD on common web ports so they emit a response.
    probe = b""
    if port in (80, 8080, 8000, 8888, 443, 8443):
        probe = (
            f"HEAD / HTTP/1.0\r\nHost: {host}\r\n"
            f"User-Agent: {_USER_AGENT}\r\n\r\n"
        ).encode()

    try:
        result = await _read_banner(host, port, timeout, probe)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Banner grab failed: {exc}"}
    result.setdefault("ethical_note", _ETHICAL_NOTE)
    return result


# ---------------------------------------------------------------------------
# DNS over HTTPS helpers
# ---------------------------------------------------------------------------

async def _doh_query(client: httpx.AsyncClient, name: str, rtype: str) -> list[dict]:
    r = await client.get(
        "https://dns.google/resolve",
        params={"name": name, "type": rtype},
    )
    r.raise_for_status()
    data = r.json()
    return data.get("Answer", []) or []


def _fmt_answers(answers: list[dict], wanted_type: int | None = None) -> list[str]:
    out: list[str] = []
    for a in answers:
        if wanted_type is not None and a.get("type") != wanted_type:
            continue
        val = a.get("data", "")
        if isinstance(val, str):
            out.append(val.strip().rstrip("."))
    return out


# numeric DNS record type codes
_DNS_TYPE = {
    "A": 1, "NS": 2, "CNAME": 5, "SOA": 6, "PTR": 12,
    "MX": 15, "TXT": 16, "AAAA": 28,
}


async def dns_enum(domain: str = "") -> dict:
    """Enumerate A / AAAA / MX / NS / TXT / SOA / CNAME records via DoH."""
    domain = _clean_host(domain)
    if not _valid_hostname(domain):
        return {"error": "Provide a valid domain name."}

    rtypes = ["A", "AAAA", "MX", "NS", "TXT", "SOA", "CNAME"]
    records: dict[str, list[str]] = {}
    try:
        async with _new_client() as client:
            results = await asyncio.gather(
                *(_doh_query(client, domain, rt) for rt in rtypes),
                return_exceptions=True,
            )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"DNS query failed: {exc}"}

    for rt, res in zip(rtypes, results):
        if isinstance(res, Exception):
            records[rt] = []
            continue
        records[rt] = _fmt_answers(res, _DNS_TYPE.get(rt))

    total = sum(len(v) for v in records.values())
    return {"domain": domain, "records": records, "record_count": total}


async def reverse_dns(ip: str = "") -> dict:
    """Reverse-resolve an IP address to PTR hostname(s) via DoH."""
    ip = (ip or "").strip().strip("\"'<>")
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return {"error": "Provide a valid IPv4 or IPv6 address."}

    ptr_name = addr.reverse_pointer  # e.g. 1.0.0.127.in-addr.arpa
    try:
        async with _new_client() as client:
            answers = await _doh_query(client, ptr_name, "PTR")
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Reverse DNS query failed: {exc}"}

    hostnames = _fmt_answers(answers, _DNS_TYPE["PTR"])
    return {
        "ip": str(addr),
        "ptr_query": ptr_name,
        "hostnames": hostnames,
        "found": bool(hostnames),
    }


# ---------------------------------------------------------------------------
# RDAP WHOIS (domain + IP)
# ---------------------------------------------------------------------------

def _rdap_entities(data: dict) -> list[dict]:
    out: list[dict] = []
    for ent in data.get("entities", []) or []:
        roles = ent.get("roles", []) or []
        name = ""
        email = ""
        vcard = ent.get("vcardArray")
        if isinstance(vcard, list) and len(vcard) == 2:
            for item in vcard[1]:
                if not isinstance(item, list) or len(item) < 4:
                    continue
                if item[0] == "fn":
                    name = str(item[3])
                elif item[0] == "email":
                    email = str(item[3])
        out.append({
            "handle": ent.get("handle", ""),
            "roles": roles,
            "name": name,
            "email": email,
        })
    return out


def _rdap_events(data: dict) -> dict:
    events = {}
    for ev in data.get("events", []) or []:
        action = ev.get("eventAction")
        date = ev.get("eventDate")
        if action and date:
            events[action] = date
    return events


async def whois_lookup(domain: str = "") -> dict:
    """RDAP WHOIS lookup for a domain (registrar, status, key dates)."""
    domain = _clean_host(domain)
    if not _valid_hostname(domain) or "." not in domain:
        return {"error": "Provide a valid registrable domain name."}

    try:
        async with _new_client(follow_redirects=True) as client:
            r = await client.get(f"https://rdap.org/domain/{domain}")
            if r.status_code == 404:
                return {"error": f"No RDAP record found for {domain}."}
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as exc:
        return {"error": f"RDAP lookup failed: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"RDAP lookup failed: {exc}"}

    nameservers = [ns.get("ldhName", "") for ns in data.get("nameservers", []) or []]
    return {
        "domain": data.get("ldhName", domain),
        "handle": data.get("handle", ""),
        "status": data.get("status", []),
        "events": _rdap_events(data),
        "nameservers": [n for n in nameservers if n],
        "entities": _rdap_entities(data),
        "secure_dns": (data.get("secureDNS") or {}).get("delegationSigned"),
    }


async def ip_whois(ip: str = "") -> dict:
    """RDAP WHOIS lookup for an IP address (network range, owner, country)."""
    ip = (ip or "").strip().strip("\"'<>")
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return {"error": "Provide a valid IPv4 or IPv6 address."}

    try:
        async with _new_client(follow_redirects=True) as client:
            r = await client.get(f"https://rdap.org/ip/{addr}")
            if r.status_code == 404:
                return {"error": f"No RDAP record found for {addr}."}
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as exc:
        return {"error": f"RDAP lookup failed: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"RDAP lookup failed: {exc}"}

    return {
        "ip": str(addr),
        "handle": data.get("handle", ""),
        "name": data.get("name", ""),
        "type": data.get("type", ""),
        "country": data.get("country", ""),
        "start_address": data.get("startAddress", ""),
        "end_address": data.get("endAddress", ""),
        "cidr": [
            f"{c.get('v4prefix') or c.get('v6prefix')}/{c.get('length')}"
            for c in data.get("cidr0_cidrs", []) or []
            if c.get("length") is not None
        ],
        "status": data.get("status", []),
        "events": _rdap_events(data),
        "entities": _rdap_entities(data),
    }


# ---------------------------------------------------------------------------
# ASN lookup (bgpview)
# ---------------------------------------------------------------------------

async def asn_lookup(ip: str = "") -> dict:
    """Look up ASN / prefix / network owner for an IP via BGPView."""
    ip = (ip or "").strip().strip("\"'<>")
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return {"error": "Provide a valid IPv4 or IPv6 address."}

    try:
        async with _new_client() as client:
            r = await client.get(f"https://api.bgpview.io/ip/{addr}")
            r.raise_for_status()
            payload = r.json()
    except httpx.HTTPError as exc:
        return {"error": f"BGPView lookup failed: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"BGPView lookup failed: {exc}"}

    if payload.get("status") != "ok":
        return {"error": "BGPView returned no data for this IP."}

    data = payload.get("data", {}) or {}
    prefixes = data.get("prefixes", []) or []
    asns = []
    seen = set()
    for pfx in prefixes:
        asn = pfx.get("asn", {}) or {}
        num = asn.get("asn")
        if num is None or num in seen:
            continue
        seen.add(num)
        asns.append({
            "asn": num,
            "name": asn.get("name", ""),
            "description": asn.get("description", ""),
            "country": asn.get("country_code", ""),
            "prefix": pfx.get("prefix", ""),
        })

    rir = (data.get("rir_allocation") or {})
    return {
        "ip": str(addr),
        "ptr": data.get("ptr_record", ""),
        "asns": asns,
        "rir": rir.get("rir_name", ""),
        "allocation_country": rir.get("country_code", ""),
        "asn_count": len(asns),
    }


# ---------------------------------------------------------------------------
# Subdomain enumeration (crt.sh)
# ---------------------------------------------------------------------------

async def subdomain_enum(domain: str = "") -> dict:
    """Enumerate subdomains from Certificate Transparency logs (crt.sh)."""
    domain = _clean_host(domain)
    if not _valid_hostname(domain) or "." not in domain:
        return {"error": "Provide a valid registrable domain name."}

    cap = 200
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=8.0),
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            r = await client.get(
                "https://crt.sh/",
                params={"q": f"%.{domain}", "output": "json"},
            )
            r.raise_for_status()
            try:
                entries = r.json()
            except Exception:  # noqa: BLE001 (crt.sh sometimes returns malformed JSON)
                return {"error": "crt.sh returned an unparseable response; try again."}
    except httpx.HTTPError as exc:
        return {"error": f"crt.sh query failed (may be slow/down): {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"crt.sh query failed: {exc}"}

    found: set[str] = set()
    for entry in entries:
        name_value = entry.get("name_value", "") or entry.get("common_name", "")
        for line in str(name_value).splitlines():
            host = line.strip().lstrip("*.").lower().rstrip(".")
            if not host:
                continue
            if host == domain or host.endswith("." + domain):
                if _HOST_RE.match(host):
                    found.add(host)

    subs = sorted(found)
    truncated = len(subs) > cap
    return {
        "domain": domain,
        "count": len(subs),
        "subdomains": subs[:cap],
        "truncated": truncated,
        "source": "crt.sh (Certificate Transparency)",
    }


# ---------------------------------------------------------------------------
# SSL / TLS info
# ---------------------------------------------------------------------------

def _flatten_name(seq) -> dict:
    """getpeercert() returns issuer/subject as tuple-of-tuple-of-pairs."""
    out: dict[str, str] = {}
    for rdn in seq or ():
        for pair in rdn:
            if len(pair) == 2:
                out[str(pair[0])] = str(pair[1])
    return out


def _blocking_tls_info(host: str, port: int, timeout: float) -> dict:
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            cert = ssock.getpeercert() or {}
            cipher = ssock.cipher()  # (name, protocol, secret_bits)
            proto = ssock.version()

    issuer = _flatten_name(cert.get("issuer"))
    subject = _flatten_name(cert.get("subject"))

    not_after = cert.get("notAfter")
    days_left = None
    expired = None
    if not_after:
        try:
            exp = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(
                tzinfo=timezone.utc
            )
            days_left = (exp - datetime.now(timezone.utc)).days
            expired = days_left < 0
        except ValueError:
            pass

    sans = [v for typ, v in cert.get("subjectAltName", ()) if typ == "DNS"]

    return {
        "host": host,
        "port": port,
        "subject": subject,
        "issuer": issuer,
        "subject_cn": subject.get("commonName", ""),
        "issuer_cn": issuer.get("commonName", issuer.get("organizationName", "")),
        "valid_from": cert.get("notBefore", ""),
        "valid_to": not_after or "",
        "days_until_expiry": days_left,
        "expired": expired,
        "subject_alt_names": sans[:50],
        "serial_number": cert.get("serialNumber", ""),
        "protocol": proto,
        "cipher": cipher[0] if cipher else "",
        "cipher_bits": cipher[2] if cipher else None,
    }


async def ssl_tls_info(host: str = "", port: int = 443, timeout: float = 8.0) -> dict:
    """Inspect a TLS endpoint: issuer, subject, expiry, protocol, cipher."""
    host = _clean_host(host)
    if not _valid_hostname(host):
        return {"error": "Provide a valid host or IP address."}
    try:
        port = int(port) if port else 443
    except (TypeError, ValueError):
        port = 443
    if not 1 <= port <= 65535:
        return {"error": "Port must be between 1 and 65535."}
    try:
        timeout = max(2.0, min(float(timeout), 12.0))
    except (TypeError, ValueError):
        timeout = 8.0

    try:
        return await asyncio.to_thread(_blocking_tls_info, host, port, timeout)
    except ssl.SSLCertVerificationError as exc:
        return {
            "error": f"Certificate failed validation: {exc}",
            "host": host,
            "port": port,
            "cert_invalid": True,
        }
    except (ssl.SSLError, OSError) as exc:
        return {"error": f"TLS connection failed: {exc}", "host": host, "port": port}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"TLS inspection failed: {exc}"}


# ---------------------------------------------------------------------------
# MAC vendor lookup
# ---------------------------------------------------------------------------

async def mac_vendor(mac: str = "") -> dict:
    """Look up the hardware vendor (OUI) for a MAC address."""
    mac = (mac or "").strip()
    # Allow users to paste with or without separators; normalize to colon form.
    raw = re.sub(r"[^0-9A-Fa-f]", "", mac)
    if len(raw) >= 12:
        raw = raw[:12]
        mac_norm = ":".join(raw[i:i + 2] for i in range(0, 12, 2))
    elif len(raw) >= 6:  # an OUI prefix is enough for the API
        mac_norm = ":".join(raw[i:i + 2] for i in range(0, len(raw) - len(raw) % 2, 2))
    else:
        return {"error": "Provide a valid MAC address (e.g. 00:1A:2B:3C:4D:5E)."}

    try:
        async with _new_client() as client:
            r = await client.get(f"https://api.macvendors.com/{mac_norm}")
            if r.status_code == 404:
                return {"mac": mac_norm, "vendor": None, "found": False,
                        "note": "No vendor registered for this OUI."}
            if r.status_code == 429:
                return {"error": "macvendors.com rate limit hit; try again shortly."}
            r.raise_for_status()
            vendor = r.text.strip()
    except httpx.HTTPError as exc:
        return {"error": f"MAC vendor lookup failed: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"MAC vendor lookup failed: {exc}"}

    return {"mac": mac_norm, "vendor": vendor, "found": bool(vendor)}


# ---------------------------------------------------------------------------
# HTTP header grabber
# ---------------------------------------------------------------------------

async def http_headers_grab(url: str = "", method: str = "GET") -> dict:
    """Fetch a URL and return its HTTP response headers + basic metadata."""
    url = (url or "").strip().strip("\"'<>")
    if not url:
        return {"error": "Provide a URL or host."}
    if "://" not in url:
        url = "https://" + url

    parsed = httpx.URL(url)
    if parsed.scheme not in ("http", "https"):
        return {"error": "Only http/https URLs are supported."}
    if not parsed.host:
        return {"error": "Could not parse a host from the URL."}

    method = (method or "GET").strip().upper()
    if method not in ("GET", "HEAD"):
        method = "GET"

    try:
        async with _new_client(follow_redirects=True) as client:
            r = await client.request(method, url)
    except httpx.HTTPError as exc:
        return {"error": f"Request failed: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Request failed: {exc}"}

    headers = {k: v for k, v in r.headers.items()}

    # Highlight common security headers for quick triage.
    sec_keys = [
        "strict-transport-security", "content-security-policy",
        "x-frame-options", "x-content-type-options", "referrer-policy",
        "permissions-policy", "x-xss-protection",
    ]
    security_headers = {k: headers.get(k) for k in sec_keys}
    missing_security = [k for k, v in security_headers.items() if not v]

    return {
        "url": str(r.url),
        "final_status": r.status_code,
        "method": method,
        "http_version": r.http_version,
        "server": headers.get("server", ""),
        "headers": headers,
        "security_headers": security_headers,
        "missing_security_headers": missing_security,
        "redirected": str(r.url) != url,
    }


# ---------------------------------------------------------------------------
# Tool specifications
# ---------------------------------------------------------------------------

SPECS = [
    {
        "name": "port_scanner",
        "label": "Port Scanner",
        "description": "Async TCP-connect scan of a host over a port list or the common ~25-port set.",
        "category": "Network & Recon",
        "tier": "yellow",
        "inputs": [
            {"key": "host", "label": "Host / IP", "type": "text",
             "placeholder": "example.com or 93.184.216.34"},
            {"key": "ports", "label": "Ports", "type": "text",
             "placeholder": "common  |  22,80,443  |  1-1024"},
            {"key": "timeout", "label": "Per-port timeout (s)", "type": "number",
             "placeholder": "1.0"},
        ],
    },
    {
        "name": "banner_grabber",
        "label": "Banner Grabber",
        "description": "Connect to host:port and read the service banner.",
        "category": "Network & Recon",
        "tier": "yellow",
        "inputs": [
            {"key": "host", "label": "Host / IP", "type": "text",
             "placeholder": "example.com"},
            {"key": "port", "label": "Port", "type": "number", "placeholder": "22"},
            {"key": "timeout", "label": "Timeout (s)", "type": "number",
             "placeholder": "5"},
        ],
    },
    {
        "name": "dns_enum",
        "label": "DNS Enumeration",
        "description": "Resolve A/AAAA/MX/NS/TXT/SOA/CNAME records via DNS-over-HTTPS.",
        "category": "Network & Recon",
        "tier": "green",
        "inputs": [
            {"key": "domain", "label": "Domain", "type": "text",
             "placeholder": "example.com"},
        ],
    },
    {
        "name": "reverse_dns",
        "label": "Reverse DNS (PTR)",
        "description": "Reverse-resolve an IP address to its PTR hostname(s) via DoH.",
        "category": "Network & Recon",
        "tier": "green",
        "inputs": [
            {"key": "ip", "label": "IP address", "type": "text",
             "placeholder": "8.8.8.8"},
        ],
    },
    {
        "name": "whois_lookup",
        "label": "WHOIS (Domain)",
        "description": "RDAP WHOIS lookup for a domain: registrar, status, key dates, nameservers.",
        "category": "Network & Recon",
        "tier": "green",
        "inputs": [
            {"key": "domain", "label": "Domain", "type": "text",
             "placeholder": "example.com"},
        ],
    },
    {
        "name": "ip_whois",
        "label": "WHOIS (IP)",
        "description": "RDAP WHOIS lookup for an IP: network range, owner, country.",
        "category": "Network & Recon",
        "tier": "green",
        "inputs": [
            {"key": "ip", "label": "IP address", "type": "text",
             "placeholder": "1.1.1.1"},
        ],
    },
    {
        "name": "asn_lookup",
        "label": "ASN Lookup",
        "description": "Look up ASN, prefix, and network owner for an IP via BGPView.",
        "category": "Network & Recon",
        "tier": "green",
        "inputs": [
            {"key": "ip", "label": "IP address", "type": "text",
             "placeholder": "8.8.8.8"},
        ],
    },
    {
        "name": "subdomain_enum",
        "label": "Subdomain Enum",
        "description": "Enumerate subdomains from Certificate Transparency logs (crt.sh).",
        "category": "Network & Recon",
        "tier": "green",
        "inputs": [
            {"key": "domain", "label": "Domain", "type": "text",
             "placeholder": "example.com"},
        ],
    },
    {
        "name": "ssl_tls_info",
        "label": "SSL/TLS Info",
        "description": "Inspect a TLS endpoint: issuer, subject, expiry, protocol, cipher.",
        "category": "Network & Recon",
        "tier": "green",
        "inputs": [
            {"key": "host", "label": "Host", "type": "text",
             "placeholder": "example.com"},
            {"key": "port", "label": "Port", "type": "number", "placeholder": "443"},
        ],
    },
    {
        "name": "mac_vendor",
        "label": "MAC Vendor",
        "description": "Look up the hardware vendor (OUI) for a MAC address.",
        "category": "Network & Recon",
        "tier": "green",
        "inputs": [
            {"key": "mac", "label": "MAC address", "type": "text",
             "placeholder": "00:1A:2B:3C:4D:5E"},
        ],
    },
    {
        "name": "http_headers_grab",
        "label": "HTTP Headers",
        "description": "Fetch a URL and return all HTTP response headers plus security-header triage.",
        "category": "Network & Recon",
        "tier": "green",
        "inputs": [
            {"key": "url", "label": "URL", "type": "text",
             "placeholder": "https://example.com"},
            {"key": "method", "label": "Method", "type": "select",
             "placeholder": "GET", "options": ["GET", "HEAD"]},
        ],
    },
]
