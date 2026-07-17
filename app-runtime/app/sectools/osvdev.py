"""OSV.dev — open-source dependency vulnerability scanning (free, NO API key).

Queries Google's OSV database by package+version+ecosystem (PyPI, npm, Go, Maven,
RubyGems, crates.io, NuGet, …) or by OSV/GHSA/PYSEC id. Cross-references CVE
aliases with the CISA KEV catalog to flag actively-exploited issues.

This complements the NVD/KEV/EPSS CVE tools: NVD is CVE-/keyword-centric, OSV is
package-/version-precise (and knows the fixed version).
"""
from __future__ import annotations

import re

import httpx

from app.scanner import threatfeeds as tf

_CAT = "OSINT & Threat Intel"
_QUERY = "https://api.osv.dev/v1/query"
_BATCH = "https://api.osv.dev/v1/querybatch"
_VULN = "https://api.osv.dev/v1/vulns/{id}"

_ECOSYSTEMS = ["PyPI", "npm", "Go", "Maven", "RubyGems", "crates.io", "NuGet",
               "Packagist", "Pub", "Hex", "Debian", "Alpine"]
_DEP_RE = re.compile(r"^(@?[\w.\-/]+?)\s*(?:==|@|\s+)\s*v?([0-9][\w.\-+]*)\s*$")


def _err(msg, **extra):
    d = {"error": msg}
    d.update(extra)
    return d


def _fixed_versions(v: dict) -> list:
    fixes = []
    for aff in v.get("affected", []) or []:
        for rng in aff.get("ranges", []) or []:
            for ev in rng.get("events", []) or []:
                if ev.get("fixed"):
                    fixes.append(ev["fixed"])
    return sorted(set(fixes))[:6]


def _severity(v: dict):
    for s in v.get("severity", []) or []:
        if s.get("score"):
            return s["score"]
    for aff in v.get("affected", []) or []:
        sv = (aff.get("ecosystem_specific") or {}).get("severity")
        if sv:
            return sv
    return None


def _compact(v: dict) -> dict:
    summ = v.get("summary") or (v.get("details") or "")[:180]
    return {
        "id": v.get("id"),
        "aliases": [a for a in (v.get("aliases") or []) if a][:6],
        "summary": (summ[:200] + "…") if len(summ) > 200 else summ,
        "severity": _severity(v),
        "fixed_versions": _fixed_versions(v),
        "references": [r.get("url") for r in (v.get("references") or []) if r.get("url")][:5],
    }


async def osv_package(name: str = "", ecosystem: str = "PyPI", version: str = "") -> dict:
    """Vulnerabilities for one open-source package@version via OSV.dev (+ KEV flag)."""
    n = (name or "").strip()
    if not n:
        return _err("Provide a package name, e.g. jinja2.")
    eco = (ecosystem or "PyPI").strip()
    body = {"package": {"name": n, "ecosystem": eco}}
    if (version or "").strip():
        body["version"] = version.strip()
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(_QUERY, json=body)
        if r.status_code == 400:
            return _err("Bad query — check the package name, ecosystem (case-sensitive, "
                        "e.g. 'PyPI' not 'pypi'), and version.")
        r.raise_for_status()
        vulns = r.json().get("vulns", []) or []
    except Exception as e:  # noqa: BLE001
        return _err(f"OSV unavailable: {type(e).__name__}: {e}")

    out = [_compact(v) for v in vulns]
    kev = await tf.kev_catalog()
    for o in out:
        o["known_exploited"] = any(a.upper() in kev for a in o["aliases"]
                                   if a.upper().startswith("CVE-"))
    return {
        "package": n, "ecosystem": eco, "version": version or None,
        "vulnerable": bool(out), "vuln_count": len(out),
        "known_exploited_count": sum(1 for o in out if o["known_exploited"]),
        "vulns": out[:25],
        "source": "OSV.dev + CISA KEV (free, no key)",
    }


async def osv_dependency_audit(requirements: str = "", ecosystem: str = "PyPI") -> dict:
    """Audit a pasted dependency list (e.g. requirements.txt) against OSV.dev in one batch."""
    text = requirements or ""
    if not text.strip():
        return _err("Paste a dependency list — lines like 'requests==2.31.0' (PyPI) or 'lodash 4.17.20'.")
    eco = (ecosystem or "PyPI").strip()
    pkgs = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        line = re.sub(r"\[[^\]]*\]", "", line)        # strip pip extras: pkg[extra]==x
        m = _DEP_RE.match(line)
        if m:
            pkgs.append((m.group(1), m.group(2)))
    if not pkgs:
        return _err("No pinned 'name==version' entries found. Use exact versions, e.g. 'flask==2.0.1'.")
    pkgs = pkgs[:50]
    queries = [{"package": {"name": n, "ecosystem": eco}, "version": v} for n, v in pkgs]
    try:
        async with httpx.AsyncClient(timeout=25.0) as c:
            r = await c.post(_BATCH, json={"queries": queries})
        r.raise_for_status()
        results = r.json().get("results", []) or []
    except Exception as e:  # noqa: BLE001
        return _err(f"OSV unavailable: {type(e).__name__}: {e}")

    rows = []
    for (n, v), res in zip(pkgs, results):
        ids = [x.get("id") for x in (res.get("vulns", []) or []) if x.get("id")]
        rows.append({"package": n, "version": v, "vuln_count": len(ids), "vulns": ids[:10]})
    vuln_rows = sorted((x for x in rows if x["vuln_count"]), key=lambda x: -x["vuln_count"])
    return {
        "ecosystem": eco, "checked": len(pkgs),
        "vulnerable_packages": len(vuln_rows),
        "clean_packages": len(pkgs) - len(vuln_rows),
        "results": vuln_rows,
        "note": ("Run osv_package on a flagged package for fix versions + details."
                 if vuln_rows else "No known vulnerabilities in the listed versions. ✓"),
        "source": "OSV.dev querybatch (free, no key)",
    }


async def osv_vuln(vuln_id: str = "") -> dict:
    """Full OSV record for an OSV / GHSA / PYSEC / CVE id."""
    vid = (vuln_id or "").strip()
    if not vid:
        return _err("Provide an id, e.g. GHSA-xxxx-xxxx-xxxx, PYSEC-2022-28, or OSV-2020-111.")
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(_VULN.format(id=vid))
        if r.status_code == 404:
            return _err(f"'{vid}' not found in OSV.")
        r.raise_for_status()
        v = r.json()
    except Exception as e:  # noqa: BLE001
        return _err(f"OSV unavailable: {type(e).__name__}: {e}")
    o = _compact(v)
    o["affected_packages"] = sorted({(a.get("package") or {}).get("name")
                                     for a in (v.get("affected") or [])
                                     if (a.get("package") or {}).get("name")})[:12]
    o["published"] = v.get("published")
    o["modified"] = v.get("modified")
    o["source"] = "OSV.dev (free, no key)"
    return o


SPECS = [
    {"name": "osv_package", "label": "OSV Package Scan", "tier": "green", "category": _CAT,
     "description": "Vulnerabilities for an open-source package@version (OSV.dev) — fixed versions + KEV flag.",
     "inputs": [{"key": "name", "label": "Package", "type": "text", "placeholder": "jinja2"},
                {"key": "ecosystem", "label": "Ecosystem", "type": "select", "options": _ECOSYSTEMS},
                {"key": "version", "label": "Version", "type": "text", "placeholder": "2.4.1"}]},
    {"name": "osv_dependency_audit", "label": "Dependency Audit", "tier": "green", "category": _CAT,
     "description": "Paste a requirements.txt / dependency list — batch-audit every pinned package against OSV.dev.",
     "inputs": [{"key": "requirements", "label": "Dependencies (name==version per line)", "type": "textarea",
                 "placeholder": "flask==2.0.1\njinja2==2.4.1\nrequests==2.20.0"},
                {"key": "ecosystem", "label": "Ecosystem", "type": "select", "options": _ECOSYSTEMS}]},
    {"name": "osv_vuln", "label": "OSV Advisory Lookup", "tier": "green", "category": _CAT,
     "description": "Full advisory record for an OSV / GHSA / PYSEC / CVE id (summary, severity, fixes, refs).",
     "inputs": [{"key": "vuln_id", "label": "Advisory ID", "type": "text", "placeholder": "GHSA-462w-v97r-4m45"}]},
]
