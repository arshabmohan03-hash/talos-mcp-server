"""Free, key-less exploit/threat enrichment for CVEs.

  * CISA KEV  — Known Exploited Vulnerabilities catalog (exploited in the wild),
                a public JSON feed (no API key). Cached in-process.
  * EPSS      — Exploit Prediction Scoring System (FIRST.org), the probability a
                CVE will be exploited in the next 30 days. Free HTTP API, no key.

These give analyst-grade *prioritization* (is it actually being exploited? how
likely?) without any paid intelligence subscription.
"""
from __future__ import annotations

import asyncio
import time

import httpx

KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
EPSS_URL = "https://api.first.org/data/v1/epss"
_KEV_TTL = 6 * 3600  # refresh the KEV catalog every 6h

_kev: dict = {"data": {}, "ts": 0.0, "version": None}
_kev_lock = asyncio.Lock()


async def kev_catalog() -> dict:
    """Return {CVE-ID: {vendor, product, name, date_added, due_date, ransomware,
    action}} from the CISA KEV feed (cached, refreshed every 6h)."""
    now = time.time()
    if _kev["data"] and now - _kev["ts"] < _KEV_TTL:
        return _kev["data"]
    async with _kev_lock:
        if _kev["data"] and time.time() - _kev["ts"] < _KEV_TTL:
            return _kev["data"]
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.get(KEV_URL, headers={"Accept": "application/json"})
            data = r.json()
            idx = {}
            for v in data.get("vulnerabilities", []) or []:
                cid = (v.get("cveID") or "").strip().upper()
                if not cid:
                    continue
                idx[cid] = {
                    "vendor": v.get("vendorProject"),
                    "product": v.get("product"),
                    "name": v.get("vulnerabilityName"),
                    "date_added": v.get("dateAdded"),
                    "due_date": v.get("dueDate"),
                    "ransomware": v.get("knownRansomwareCampaignUse"),
                    "action": v.get("requiredAction"),
                }
            _kev["data"] = idx
            _kev["ts"] = time.time()
            _kev["version"] = data.get("catalogVersion")
        except Exception:  # noqa: BLE001 — keep any stale data, never raise
            pass
    return _kev["data"]


async def epss_scores(cve_ids) -> dict:
    """Return {CVE-ID: {epss, percentile}} for the given CVE IDs (FIRST.org EPSS)."""
    ids = sorted({(c or "").strip().upper() for c in cve_ids
                  if (c or "").strip().upper().startswith("CVE-")})
    if not ids:
        return {}
    out: dict = {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            for i in range(0, len(ids), 90):          # API caps the cve= list length
                chunk = ids[i:i + 90]
                r = await c.get(EPSS_URL, params={"cve": ",".join(chunk)})
                if r.status_code != 200:
                    continue
                for row in (r.json().get("data") or []):
                    cid = (row.get("cve") or "").upper()
                    try:
                        out[cid] = {"epss": round(float(row.get("epss", 0) or 0), 4),
                                    "percentile": round(float(row.get("percentile", 0) or 0), 4)}
                    except (TypeError, ValueError):
                        pass
    except Exception:  # noqa: BLE001
        pass
    return out


def priority(is_kev: bool, epss: float | None, cvss: float | None) -> str:
    """A single triage label from exploit reality > likelihood > severity."""
    epss = epss or 0.0
    cvss = cvss or 0.0
    if is_kev:
        return "CRITICAL — known exploited in the wild (CISA KEV)"
    if epss >= 0.5:
        return "HIGH — likely to be exploited (EPSS)"
    if cvss >= 9.0:
        return "HIGH — critical severity"
    if epss >= 0.1 or cvss >= 7.0:
        return "MEDIUM"
    return "LOW"


def _rank_key(c: dict):
    """Sort CVEs: known-exploited first, then EPSS, then CVSS (all descending)."""
    return (1 if c.get("known_exploited") else 0, c.get("epss") or 0.0, c.get("cvss") or 0.0)


async def enrich_cves(cves: list[dict]) -> list[dict]:
    """Add known_exploited (+KEV detail), epss, and priority to a list of CVE dicts
    (each must have an 'id'), then return them ranked by exploit risk."""
    if not cves:
        return cves
    ids = [c.get("id") for c in cves if c.get("id")]
    kev = await kev_catalog()
    epss = await epss_scores(ids)
    for c in cves:
        cid = (c.get("id") or "").upper()
        k = kev.get(cid)
        e = epss.get(cid, {})
        c["known_exploited"] = bool(k)
        if k:
            c["kev"] = {"date_added": k.get("date_added"), "ransomware": k.get("ransomware"),
                        "due_date": k.get("due_date")}
        c["epss"] = e.get("epss")
        c["epss_percentile"] = e.get("percentile")
        c["priority"] = priority(bool(k), e.get("epss"), c.get("cvss"))
    cves.sort(key=_rank_key, reverse=True)
    return cves
