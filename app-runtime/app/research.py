"""Academic research APIs — OpenAlex, Semantic Scholar, CORE.

Every source is normalised to one ``paper`` shape so the API, the UI and the AI
tool can treat results uniformly::

    {id, doi, title, year, type, cited_by, is_oa, oa_url, journal,
     authors[], abstract, topics[], tldr, url, source}

Keys are read from the environment (see app/config.py) — never hardcode them.
"""
from __future__ import annotations

import asyncio

import httpx

from app.config import get_settings

OPENALEX = "https://api.openalex.org"
S2 = "https://api.semanticscholar.org/graph/v1"
CORE = "https://api.core.ac.uk/v3"

S2_FIELDS = ("title,year,citationCount,authors,isOpenAccess,openAccessPdf,"
             "abstract,url,venue,journal,externalIds,s2FieldsOfStudy,tldr")
# The /bulk endpoint supports a smaller field set than /search (no tldr/journal/
# isOpenAccess/s2FieldsOfStudy) — using the full set returns 400.
S2_BULK_FIELDS = "title,year,citationCount,authors,openAccessPdf,abstract,url,venue,externalIds"


def _client(timeout: float) -> httpx.AsyncClient:
    s = get_settings()
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,  # CORE /search/works/ 301s without the trailing slash
        headers={"User-Agent": f"Talos-Research/1.0 (mailto:{s.contact_email})"},
    )


# ----------------------------- normalisation -----------------------------
def _blank(**over) -> dict:
    base = {
        "id": None, "doi": None, "title": "Untitled", "year": None, "type": None,
        "cited_by": 0, "is_oa": False, "oa_url": None, "journal": None,
        "authors": [], "abstract": "", "topics": [], "tldr": None,
        "url": None, "source": None,
    }
    base.update(over)
    return base


def _openalex_abstract(inv: dict | None) -> str:
    """Reconstruct plain text from OpenAlex's inverted index {word: [positions]}."""
    if not inv:
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inv.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return " ".join(w for _, w in positions)


def _norm_openalex(w: dict) -> dict:
    oa = w.get("open_access") or {}
    src = ((w.get("primary_location") or {}).get("source") or {})
    return _blank(
        id=w.get("id"),
        doi=(w.get("doi") or "").replace("https://doi.org/", "") or None,
        title=w.get("title") or w.get("display_name") or "Untitled",
        year=w.get("publication_year"),
        type=w.get("type"),
        cited_by=w.get("cited_by_count", 0),
        is_oa=bool(oa.get("is_oa")),
        oa_url=oa.get("oa_url"),
        journal=src.get("display_name"),
        authors=[a.get("author", {}).get("display_name")
                 for a in (w.get("authorships") or [])][:10],
        abstract=_openalex_abstract(w.get("abstract_inverted_index")),
        topics=[t.get("display_name") for t in (w.get("topics") or [])][:5],
        url=w.get("doi") or w.get("id"),
        source="OpenAlex",
    )


def _norm_s2(p: dict) -> dict:
    ext = p.get("externalIds") or {}
    doi = ext.get("DOI")
    oa = p.get("openAccessPdf") or {}
    return _blank(
        id=p.get("paperId"),
        doi=doi,
        title=p.get("title") or "Untitled",
        year=p.get("year"),
        cited_by=p.get("citationCount", 0),
        is_oa=bool(p.get("isOpenAccess") or oa.get("url")),
        oa_url=oa.get("url"),
        journal=p.get("venue") or (p.get("journal") or {}).get("name"),
        authors=[a.get("name") for a in (p.get("authors") or [])][:10],
        abstract=p.get("abstract") or "",
        topics=[f.get("category") for f in (p.get("s2FieldsOfStudy") or [])][:5],
        tldr=(p.get("tldr") or {}).get("text"),
        url=p.get("url") or (f"https://doi.org/{doi}" if doi else None),
        source="Semantic Scholar",
    )


def _norm_core(w: dict) -> dict:
    doi = w.get("doi")
    return _blank(
        id=str(w.get("id")) if w.get("id") is not None else None,
        doi=doi,
        title=w.get("title") or "Untitled",
        year=w.get("yearPublished"),
        type=w.get("documentType"),
        cited_by=w.get("citationCount") or 0,
        is_oa=True,
        oa_url=w.get("downloadUrl"),
        journal=w.get("publisher"),
        authors=[a.get("name") for a in (w.get("authors") or [])][:10],
        abstract=w.get("abstract") or "",
        url=w.get("downloadUrl") or (f"https://doi.org/{doi}" if doi else None),
        source="CORE",
    )


def _safe_results(r, key: str) -> list:
    """Read ``r.json()[key]`` as a list, returning [] if the body isn't valid JSON
    (a provider can answer 200 with a rate-limit / CDN HTML page)."""
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        return []
    val = data.get(key) if isinstance(data, dict) else None
    return val if isinstance(val, list) else []


# ----------------------------- per-source search -----------------------------
async def search_openalex(query: str, *, year_from: int | None = None,
                          open_access: bool = False, limit: int = 10,
                          sort: str = "relevance_score:desc") -> list[dict]:
    s = get_settings()
    params: dict = {"search": query, "per_page": min(limit, 200), "sort": sort}
    filters = []
    if year_from:
        filters.append(f"publication_year:>{year_from - 1}")
    if open_access:
        filters.append("is_oa:true")
    if filters:
        params["filter"] = ",".join(filters)
    if s.openalex_api_key:
        params["api_key"] = s.openalex_api_key
    async with _client(s.research_timeout) as c:
        r = await c.get(f"{OPENALEX}/works", params=params)
        r.raise_for_status()
        return [_norm_openalex(w) for w in _safe_results(r, "results")]


async def search_s2(query: str, *, year_from: int | None = None,
                    open_access: bool = False, limit: int = 10) -> list[dict]:
    """Semantic Scholar. Tries the trained-ranker *relevance* endpoint first (best
    ordering, but resource-intensive and heavily rate-limited for anonymous users);
    on a 429 it falls back to the lighter **bulk** search endpoint (sorted by
    citations) which works WITHOUT an API key. Set ``SEMANTIC_SCHOLAR_API_KEY`` for a
    guaranteed rate + relevance ranking."""
    s = get_settings()
    headers = {"x-api-key": s.semantic_scholar_api_key} if s.semantic_scholar_api_key else {}
    async with _client(s.research_timeout) as c:
        # 1) relevance search (S2's custom-trained ranker)
        params: dict = {"query": query, "limit": min(limit, 100), "fields": S2_FIELDS}
        if year_from:
            params["year"] = f"{year_from}-"
        if open_access:
            params["openAccessPdf"] = ""
        r = await c.get(f"{S2}/paper/search", params=params, headers=headers)
        if r.status_code != 429:
            r.raise_for_status()
            return [_norm_s2(p) for p in _safe_results(r, "data")][:limit]
        # 2) rate-limited -> bulk search (lighter; works anonymously). Sort by citations.
        bp: dict = {"query": query, "fields": S2_BULK_FIELDS, "sort": "citationCount:desc"}
        if year_from:
            bp["year"] = f"{year_from}-"
        if open_access:
            bp["openAccessPdf"] = ""
        rb = await c.get(f"{S2}/paper/search/bulk", params=bp, headers=headers)
        rb.raise_for_status()
        return [_norm_s2(p) for p in _safe_results(rb, "data")][:limit]


async def search_core(query: str, *, year_from: int | None = None,
                      limit: int = 10) -> list[dict]:
    s = get_settings()
    if not s.core_api_key:
        raise RuntimeError("CORE API key not configured.")
    q = query if not year_from else f"{query} AND yearPublished>{year_from - 1}"
    params = {"q": q, "limit": min(limit, 100), "offset": 0}
    headers = {"Authorization": f"Bearer {s.core_api_key}"}
    async with _client(s.research_timeout) as c:
        r = await c.get(f"{CORE}/search/works/", params=params, headers=headers)
        r.raise_for_status()
        return [_norm_core(w) for w in _safe_results(r, "results")]


# ----------------------------- combined -----------------------------
_SOURCES = {
    "openalex": search_openalex,
    "semantic_scholar": search_s2,
    "s2": search_s2,
    "core": search_core,
}


async def run_search(source: str, query: str, **opts) -> list[dict]:
    """Search a single named source (openalex | semantic_scholar | core)."""
    fn = _SOURCES.get((source or "openalex").lower())
    if fn is None:
        raise ValueError(f"Unknown research source: {source}")
    return await fn(query, **opts)


def _dedup(papers: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for p in papers:
        key = (p.get("doi") or p.get("title") or str(p.get("id") or "")).strip().lower() or str(id(p))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


async def multi_search(query: str, *, sources: list[str] | None = None,
                       year_from: int | None = None, open_access: bool = False,
                       limit: int = 8) -> list[dict]:
    """Search several sources concurrently, merge + dedup, sort by citations."""
    sources = sources or ["openalex"]
    tasks = []
    for src in sources:
        fn = _SOURCES.get(src.lower())
        if fn:
            kw = {"year_from": year_from, "limit": limit}
            if src.lower() != "core":
                kw["open_access"] = open_access
            tasks.append(fn(query, **kw))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    merged: list[dict] = []
    for res in results:
        if isinstance(res, list):
            merged.extend(res)
    merged = _dedup(merged)
    merged.sort(key=lambda p: p.get("cited_by") or 0, reverse=True)
    return merged[: limit * 2]
