"""Binary-backed security tools (Network & Recon).

Each tool shells out to a real CLI binary IF it is installed, otherwise returns
a clear "install X" message. Safety:
  * subprocess is always run with a LIST of args and shell=False (no shell);
  * targets are validated against a strict hostname/IP regex (no metacharacters);
  * scan profiles map to FIXED flag sets — raw user flags are never accepted;
  * every call has a timeout.
Only use against hosts you own or are explicitly authorized to test.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

_HOST_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,253}$")   # hostname or IP, no shell metachars


def _valid_host(h: str) -> bool:
    return bool(_HOST_RE.match((h or "").strip()))


# Install dirs to check when a freshly-installed binary isn't on this process's
# PATH yet (e.g. installed via Chocolatey AFTER the server started — env vars are
# captured at process launch, so PATH won't refresh until restart).
_PROJECT_TOOLS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tools")

_EXTRA_BIN_DIRS = [
    _PROJECT_TOOLS, os.path.join(_PROJECT_TOOLS, "bin"),   # project-local portable binaries
    r"C:\ProgramData\chocolatey\bin",
    r"C:\Program Files\Nmap",
    r"C:\Program Files (x86)\Nmap",
    r"C:\Program Files\YARA",
    r"C:\tools\yara64",
    r"C:\tools\yara",
    r"C:\Strawberry\perl\bin",
    r"C:\Strawberry\c\bin",
    r"C:\Program Files\masscan",
    r"C:\Program Files\Sysinternals",
    "/usr/bin", "/usr/local/bin", "/opt/homebrew/bin",
]


def _which(name: str) -> str | None:
    p = shutil.which(name)
    if p:
        return p
    # Fallback: scan known install dirs (handles stale PATH right after install).
    for d in _EXTRA_BIN_DIRS:
        for ext in ("", ".exe", ".bat", ".cmd"):
            cand = os.path.join(d, name + ext)
            if os.path.isfile(cand):
                return cand
    return None


def _missing(binary: str, url: str = "") -> dict:
    msg = f"'{binary}' is not installed on this machine."
    if url:
        msg += f" Install it from {url} to enable this tool."
    return {"error": msg, "needs_binary": binary}


def _run(cmd: list[str], timeout: int = 30, stdin: str | None = None) -> dict:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           shell=False, input=stdin)
        return {
            "command": " ".join(cmd),
            "returncode": p.returncode,
            "stdout": (p.stdout or "")[-8000:],
            "stderr": (p.stderr or "")[-2000:],
        }
    except subprocess.TimeoutExpired:
        return {"error": f"Timed out after {timeout}s.", "command": " ".join(cmd)}
    except FileNotFoundError:
        return {"error": "Binary not found.", "command": " ".join(cmd)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "command": " ".join(cmd)}


# --------------------------------------------------------------------------- #
_NMAP_PROFILES = {
    "quick": ["-T4", "-F", "-Pn"],
    "service": ["-T4", "-sV", "--top-ports", "100", "-Pn"],
    "os": ["-T4", "-O", "-Pn"],
    "ping-sweep": ["-sn"],
}


def nmap_scan(target: str = "", scan_type: str = "quick") -> dict:
    """Run nmap with a fixed, safe profile (no raw flags)."""
    if not _valid_host(target):
        return {"error": "Invalid target — a hostname or IP only."}
    nmap = _which("nmap")
    if not nmap:
        return _missing("nmap", "https://nmap.org/download")
    flags = _NMAP_PROFILES.get((scan_type or "quick").lower(), _NMAP_PROFILES["quick"])
    out = _run([nmap, *flags, target], timeout=180)
    out["note"] = "Only scan hosts you own or are authorized to test."
    return out


def traceroute(target: str = "", max_hops: int = 20) -> dict:
    """Map the network path to a host (tracert on Windows, traceroute on *nix)."""
    if not _valid_host(target):
        return {"error": "Invalid target — a hostname or IP only."}
    try:
        hops = max(1, min(40, int(max_hops or 20)))
    except (TypeError, ValueError):
        hops = 20
    if os.name == "nt" and _which("tracert"):
        return _run(["tracert", "-d", "-h", str(hops), "-w", "1500", target], timeout=90)
    tr = _which("traceroute")
    if tr:
        return _run([tr, "-n", "-m", str(hops), "-w", "2", target], timeout=90)
    return _missing("traceroute/tracert")


def ping_host(target: str = "", count: int = 4) -> dict:
    """ICMP echo (latency / reachability)."""
    if not _valid_host(target):
        return {"error": "Invalid target — a hostname or IP only."}
    ping = _which("ping")
    if not ping:
        return _missing("ping")
    try:
        n = max(1, min(10, int(count or 4)))
    except (TypeError, ValueError):
        n = 4
    flag = "-n" if os.name == "nt" else "-c"
    return _run([ping, flag, str(n), target], timeout=30)


def nslookup_dns(target: str = "", record_type: str = "A") -> dict:
    """DNS lookup via the system resolver (nslookup)."""
    if not _valid_host(target):
        return {"error": "Invalid target — a hostname or IP only."}
    nsl = _which("nslookup")
    if not nsl:
        return _missing("nslookup")
    rt = (record_type or "A").upper()
    if rt not in ("A", "AAAA", "MX", "NS", "TXT", "SOA", "CNAME", "PTR", "ANY"):
        rt = "A"
    return _run([nsl, "-type=" + rt, target], timeout=20)


def arp_neighbors() -> dict:
    """Show the local ARP table (LAN neighbours)."""
    arp = _which("arp")
    if not arp:
        return _missing("arp")
    return _run([arp, "-a"], timeout=15)


def openssl_cert(target: str = "", port: int = 443) -> dict:
    """Fetch + summarise the TLS certificate chain with the openssl binary."""
    if not _valid_host(target):
        return {"error": "Invalid target — a hostname or IP only."}
    ossl = _which("openssl")
    if not ossl:
        return _missing("openssl", "https://www.openssl.org/")
    try:
        p = max(1, min(65535, int(port or 443)))
    except (TypeError, ValueError):
        p = 443
    out = _run([ossl, "s_client", "-connect", f"{target}:{p}", "-servername", target],
               timeout=20, stdin="Q\n")
    text = out.get("stdout", "") + out.get("stderr", "")
    summary = {}
    for key, pat in (("subject", r"subject=.*"), ("issuer", r"issuer=.*"),
                     ("protocol", r"Protocol\s*:\s*\S+"), ("cipher", r"Cipher\s*:\s*\S+"),
                     ("verify", r"Verify return code:.*")):
        m = re.search(pat, text)
        if m:
            summary[key] = m.group(0).strip()[:200]
    out["summary"] = summary
    return out


def yara_scan(rule: str = "", sample: str = "") -> dict:
    """Match a sample string against a YARA rule (needs the yara binary)."""
    yara = _which("yara")
    if not yara:
        return _missing("yara", "https://virustotal.github.io/yara/")
    if not (rule or "").strip():
        return {"error": "Provide a YARA rule."}
    rf = sf = None
    try:
        rf = tempfile.NamedTemporaryFile("w", suffix=".yar", delete=False, encoding="utf-8")
        sf = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
        rf.write(rule); rf.close()
        sf.write(sample or ""); sf.close()
        out = _run([yara, "-s", rf.name, sf.name], timeout=20)
        out["matched"] = bool(out.get("stdout", "").strip())
        return out
    finally:
        for f in (rf, sf):
            try:
                if f:
                    os.unlink(f.name)
            except Exception:  # noqa: BLE001
                pass


def _ext_scanner(binary: str, url_help: str, target: str, extra: list[str], timeout: int) -> dict:
    if not _valid_host(target):
        return {"error": "Invalid target — a hostname or IP only."}
    b = _which(binary)
    if not b:
        return _missing(binary, url_help)
    out = _run([b, *extra, target], timeout=timeout)
    out["note"] = "Authorized testing only."
    return out


def masscan_scan(target: str = "", ports: str = "1-1000") -> dict:
    """High-speed port scan with masscan (needs the masscan binary)."""
    safe_ports = ports if re.fullmatch(r"[0-9,\-]{1,40}", ports or "") else "1-1000"
    return _ext_scanner("masscan", "https://github.com/robertdavidgraham/masscan",
                        target, ["-p", safe_ports, "--rate", "1000"], 120)


def nikto_scan(target: str = "") -> dict:
    """Web-server vulnerability scan with Nikto (needs the nikto binary)."""
    return _ext_scanner("nikto", "https://github.com/sullo/nikto",
                        target, ["-host"], 180)


def sslscan_audit(target: str = "") -> dict:
    """Cipher/protocol audit with sslscan (needs the sslscan binary)."""
    return _ext_scanner("sslscan", "https://github.com/rbsec/sslscan",
                        target, [], 60)


_CAT = "Network & Recon"
_NOTE = "Runs a real CLI binary; only scan hosts you own or are authorized to test."

SPECS: list[dict] = [
    {"name": "nmap_scan", "label": "Nmap Scan", "description": "Real nmap scan (quick / service-version / OS / ping-sweep profiles).", "category": _CAT, "tier": "yellow",
     "inputs": [{"key": "target", "label": "Host / IP", "type": "text", "placeholder": "scanme.nmap.org"},
                {"key": "scan_type", "label": "Profile", "type": "select", "options": ["quick", "service", "os", "ping-sweep"]}]},
    {"name": "traceroute", "label": "Traceroute", "description": "Map network hops to a host (tracert/traceroute).", "category": _CAT, "tier": "yellow",
     "inputs": [{"key": "target", "label": "Host / IP", "type": "text", "placeholder": "example.com"},
                {"key": "max_hops", "label": "Max hops", "type": "number", "placeholder": "20"}]},
    {"name": "ping_host", "label": "Ping", "description": "ICMP reachability + latency to a host.", "category": _CAT, "tier": "yellow",
     "inputs": [{"key": "target", "label": "Host / IP", "type": "text", "placeholder": "8.8.8.8"},
                {"key": "count", "label": "Count", "type": "number", "placeholder": "4"}]},
    {"name": "nslookup_dns", "label": "nslookup", "description": "DNS lookup via the system resolver.", "category": _CAT, "tier": "green",
     "inputs": [{"key": "target", "label": "Host / domain", "type": "text", "placeholder": "example.com"},
                {"key": "record_type", "label": "Record", "type": "select", "options": ["A", "AAAA", "MX", "NS", "TXT", "SOA", "CNAME", "PTR", "ANY"]}]},
    {"name": "arp_neighbors", "label": "ARP Table", "description": "Local ARP table (LAN neighbours / MAC↔IP).", "category": _CAT, "tier": "green", "inputs": []},
    {"name": "openssl_cert", "label": "OpenSSL Cert Inspect", "description": "Fetch + summarise the TLS cert chain via the openssl binary.", "category": _CAT, "tier": "green",
     "inputs": [{"key": "target", "label": "Host", "type": "text", "placeholder": "example.com"},
                {"key": "port", "label": "Port", "type": "number", "placeholder": "443"}]},
    {"name": "yara_scan", "label": "YARA Scan", "description": "Match a sample against a YARA rule (needs the yara binary).", "category": _CAT, "tier": "yellow",
     "inputs": [{"key": "rule", "label": "YARA rule", "type": "textarea", "placeholder": "rule demo { strings: $a = \"evil\" condition: $a }"},
                {"key": "sample", "label": "Sample text", "type": "textarea", "placeholder": "text to scan"}]},
    {"name": "masscan_scan", "label": "Masscan", "description": "High-speed port scan (needs the masscan binary).", "category": _CAT, "tier": "yellow",
     "inputs": [{"key": "target", "label": "Host / IP", "type": "text", "placeholder": "192.0.2.1"},
                {"key": "ports", "label": "Ports", "type": "text", "placeholder": "1-1000"}]},
    {"name": "nikto_scan", "label": "Nikto Web Scan", "description": "Web-server vuln scan (needs the nikto binary).", "category": _CAT, "tier": "yellow",
     "inputs": [{"key": "target", "label": "Host / URL", "type": "text", "placeholder": "example.com"}]},
    {"name": "sslscan_audit", "label": "sslscan Audit", "description": "TLS cipher/protocol audit (needs the sslscan binary).", "category": _CAT, "tier": "green",
     "inputs": [{"key": "target", "label": "Host", "type": "text", "placeholder": "example.com"}]},
]
