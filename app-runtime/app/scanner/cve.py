"""CVE lookup for a product/version via the free NVD 2.0 API, enriched with CISA
KEV (exploited-in-the-wild) + EPSS (exploit probability).

Exposed as an explicit AI tool (not part of the fast scan) so the latency/rate
limits of an external service never break a scan. No API key required (NVD's free
tier is rate-limited to ~5 requests / 30s; failures degrade gracefully).
"""
from __future__ import annotations

import httpx

from app.scanner import threatfeeds

NVD = "https://services.nvd.nist.gov/rest/json/cves/2.0"


def _cvss(metrics: dict):
    """Pick the best available CVSS base score/severity from an NVD metrics block."""
    for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        arr = metrics.get(key) or []
        if arr:
            d = arr[0].get("cvssData") or {}
            return d.get("baseScore"), (d.get("baseSeverity") or arr[0].get("baseSeverity"))
    return None, None


async def lookup_cves(product: str, version: str | None = None, limit: int = 6) -> dict:
    """Look up CVEs for a product (and optional version), ranked by real exploit
    risk (CISA KEV → EPSS → CVSS).

    Returns {"product","version","count","known_exploited_count","cves":[...]}.
    Never raises — returns an 'error' key on failure.
    """
    product = (product or "").strip()
    if not product:
        return {"error": "No product specified."}
    keyword = f"{product} {version}".strip() if version else product
    try:
        async with httpx.AsyncClient(timeout=15.0,
                                     headers={"User-Agent": "Talos/1.0 (+authorized review)"}) as client:
            r = await client.get(NVD, params={"keywordSearch": keyword, "resultsPerPage": 25})
        if r.status_code in (403, 429):
            return {"product": product, "version": version, "count": 0, "cves": [],
                    "error": "NVD rate-limited (free tier ~5 requests/30s) — retry in ~30s."}
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return {"product": product, "version": version, "count": 0, "cves": [],
                "error": f"CVE service unavailable ({type(e).__name__}); "
                         "check the vendor's advisories manually."}

    out = []
    for v in data.get("vulnerabilities", []) or []:
        cve = v.get("cve") or {}
        cid = cve.get("id")
        if not cid:
            continue
        desc = next((d.get("value") for d in (cve.get("descriptions") or [])
                     if d.get("lang") == "en"), "") or ""
        score, sev = _cvss(cve.get("metrics") or {})
        out.append({
            "id": cid,
            "summary": (desc[:240] + "…") if len(desc) > 240 else desc,
            "cvss": score, "severity": sev,
        })

    out = await threatfeeds.enrich_cves(out)   # KEV + EPSS + priority + risk ranking
    out = out[:limit]
    return {
        "product": product, "version": version,
        "count": len(out),
        "known_exploited_count": sum(1 for c in out if c.get("known_exploited")),
        "cves": out,
        "source": "NVD + CISA KEV + EPSS (free, no key)",
    }
