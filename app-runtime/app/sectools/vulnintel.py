"""Vulnerability & exploit intelligence — free, no API key.

Sources: NVD (authoritative CVE records), CISA KEV (known-exploited-in-the-wild),
EPSS (exploit-probability). Gives analyst-grade prioritization without a paid
intelligence subscription.
"""
from __future__ import annotations

import re

import httpx

from app.scanner import threatfeeds as tf

_CAT = "OSINT & Threat Intel"
_NVD = "https://services.nvd.nist.gov/rest/json/cves/2.0"
_CVE_RE = re.compile(r"^CVE-\d{4}-\d{4,}$", re.I)


def _err(msg, **extra):
    d = {"error": msg}
    d.update(extra)
    return d


def _cvss_from_nvd(metrics: dict):
    for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        arr = metrics.get(key) or []
        if arr:
            d = arr[0].get("cvssData") or {}
            sev = d.get("baseSeverity") or arr[0].get("baseSeverity")
            return d.get("baseScore"), sev, d.get("vectorString")
    return None, None, None


async def cve_lookup(cve_id: str = "") -> dict:
    """Full record for one CVE: NVD details + CVSS + CISA KEV + EPSS + priority."""
    cid = (cve_id or "").strip().upper()
    if not _CVE_RE.match(cid):
        return _err("Provide a CVE ID, e.g. CVE-2021-44228.")
    rec, note = {}, None
    try:
        async with httpx.AsyncClient(timeout=15.0,
                                     headers={"User-Agent": "Talos/1.0 (+authorized review)"}) as c:
            r = await c.get(_NVD, params={"cveId": cid})
        if r.status_code == 200:
            vulns = r.json().get("vulnerabilities") or []
            rec = (vulns[0].get("cve") if vulns else {}) or {}
        elif r.status_code in (403, 429):
            note = "NVD rate-limited (free tier ~5 req/30s) — KEV/EPSS still shown below."
        else:
            note = f"NVD HTTP {r.status_code}."
    except Exception as e:  # noqa: BLE001
        note = f"NVD unavailable: {type(e).__name__}"

    kev = (await tf.kev_catalog()).get(cid)
    epss = (await tf.epss_scores([cid])).get(cid, {})
    desc = next((d.get("value") for d in (rec.get("descriptions") or []) if d.get("lang") == "en"), None)
    score, sev, vector = _cvss_from_nvd(rec.get("metrics") or {})
    out = {
        "cve": cid, "summary": desc, "cvss": score, "severity": sev, "cvss_vector": vector,
        "published": rec.get("published"), "modified": rec.get("lastModified"),
        "known_exploited": bool(kev), "kev": kev,
        "epss": epss.get("epss"), "epss_percentile": epss.get("percentile"),
        "priority": tf.priority(bool(kev), epss.get("epss"), score),
        "references": [x.get("url") for x in (rec.get("references") or [])][:8],
        "source": "NVD + CISA KEV + EPSS (free, no key)",
    }
    if note:
        out["note"] = note
    return out


async def cve_search(product: str = "", version: str = "") -> dict:
    """Find CVEs for a product/version, ranked by real exploit risk (KEV → EPSS → CVSS)."""
    from app.scanner.cve import lookup_cves
    p = (product or "").strip()
    if not p:
        return _err("Provide a product name, e.g. nginx or 'apache log4j'.")
    return await lookup_cves(p, (version or "").strip() or None, limit=8)


async def kev_check(query: str = "") -> dict:
    """Is a CVE known-exploited? Or search the CISA KEV catalog by vendor/product."""
    q = (query or "").strip()
    if not q:
        return _err("Provide a CVE ID, or a vendor/product keyword (e.g. 'fortinet').")
    cat = await tf.kev_catalog()
    if not cat:
        return _err("Could not load the CISA KEV catalog (network issue).", kev_total=0)
    if _CVE_RE.match(q.upper()):
        cid = q.upper()
        k = cat.get(cid)
        epss = (await tf.epss_scores([cid])).get(cid, {})
        return {
            "cve": cid, "known_exploited": bool(k), "details": k,
            "epss": epss.get("epss"), "kev_catalog_size": len(cat),
            "verdict": ("Actively exploited in the wild — patch immediately (CISA KEV)."
                        if k else "Not in the CISA KEV catalog (no confirmed in-the-wild exploitation)."),
        }
    ql = q.lower()
    hits = [{"cve": cid, **v} for cid, v in cat.items()
            if ql in f"{v.get('vendor','')} {v.get('product','')} {v.get('name','')}".lower()]
    hits.sort(key=lambda h: h.get("date_added") or "", reverse=True)
    return {"query": q, "kev_catalog_size": len(cat), "matches": len(hits), "results": hits[:30]}


SPECS = [
    {"name": "cve_lookup", "label": "CVE Lookup", "tier": "green", "category": _CAT,
     "description": "Full CVE record (NVD) + CVSS + CISA KEV (exploited-in-the-wild) + EPSS + priority.",
     "inputs": [{"key": "cve_id", "label": "CVE ID", "type": "text", "placeholder": "CVE-2021-44228"}]},
    {"name": "cve_search", "label": "CVE Search", "tier": "green", "category": _CAT,
     "description": "Find CVEs for a product/version, ranked by real exploit risk (KEV → EPSS → CVSS).",
     "inputs": [{"key": "product", "label": "Product", "type": "text", "placeholder": "nginx"},
                {"key": "version", "label": "Version (optional)", "type": "text", "placeholder": "1.18.0"}]},
    {"name": "kev_check", "label": "KEV Exploit Check", "tier": "green", "category": _CAT,
     "description": "Is a CVE actively exploited? Or search CISA's Known-Exploited catalog by vendor/product.",
     "inputs": [{"key": "query", "label": "CVE ID or vendor/product", "type": "text", "placeholder": "CVE-2023-27997 or fortinet"}]},
]
