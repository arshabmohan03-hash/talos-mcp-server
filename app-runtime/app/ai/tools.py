"""Tool schemas + dispatch bridging the AI model to the scanner / detector."""
from __future__ import annotations

import asyncio
import re
from collections import Counter

from app import alerts, defense, mitigation, research, resources, secutils, sectools
from app.bruteforce import analyze_log
from app.scanner import lookup_cves, scan

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "scan_website",
            "description": "Run a safe, non-destructive security scan of a website. "
                           "Checks HTTPS/TLS certificate, security headers, cookie "
                           "flags, exposed sensitive files, DNS/email security (SPF/"
                           "DMARC), and technology fingerprint. Returns a graded report "
                           "with findings. Use for any website/URL the user wants checked.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The website to scan, e.g. 'example.com' or "
                                       "'https://example.com'.",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_cves",
            "description": "Look up known public vulnerabilities (CVEs) for a software "
                           "product and optional version, e.g. after a scan reveals an "
                           "outdated web server like 'nginx 1.18.0'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product": {"type": "string", "description": "Software/product name, e.g. 'nginx'."},
                    "version": {"type": "string", "description": "Version string, e.g. '1.18.0' (optional)."},
                },
                "required": ["product"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_auth_log",
            "description": "Analyze a server authentication log for brute-force / "
                           "password-guessing attacks. Reports attacking IPs, attempt "
                           "counts, targeted usernames, and risk status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the log file (optional; defaults to the configured auth log)."},
                    "threshold": {"type": "integer", "description": "Failed-attempt count to flag as suspicious (optional)."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_research",
            "description": "Search real academic literature for peer-reviewed papers — "
                           "attack techniques, detection methods, defenses, or any "
                           "scientific/teaching topic. By DEFAULT it queries ALL databases "
                           "(OpenAlex + Semantic Scholar + CORE) at once and merges/dedupes "
                           "the results. Use it to ground answers in real research and cite "
                           "sources. Returns papers with title, authors, year, citations and "
                           "open-access links.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query / topic, e.g. 'SSH brute-force detection'."},
                    "source": {"type": "string", "enum": ["all", "openalex", "semantic_scholar", "core"], "description": "Which database to search. Defaults to 'all' (merges OpenAlex + Semantic Scholar + CORE) — only pick a single source if the user explicitly asks for one."},
                    "year_from": {"type": "integer", "description": "Only papers from this year onward (optional)."},
                    "open_access": {"type": "boolean", "description": "Only papers with a free full-text PDF (optional)."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_blocklist",
            "description": "Generate ready-to-apply firewall blocking rules (fail2ban, "
                           "iptables, ufw, Windows firewall) for attacking IPs — defensive "
                           "mitigation after a brute-force analysis. Does NOT execute them; "
                           "returns rules for the user to review and apply.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ips": {"type": "array", "items": {"type": "string"},
                            "description": "Attacking IPv4 addresses to block."},
                    "threshold": {"type": "integer", "description": "fail2ban maxretry (optional, default 5)."},
                },
                "required": ["ips"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_alert",
            "description": "Send a security alert to the user's configured channels "
                           "(email and/or Slack) — e.g. to notify them about detected "
                           "attacks. Returns which channels delivered.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "The alert body text."},
                    "subject": {"type": "string", "description": "Short subject/title (optional)."},
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_password_strength",
            "description": "Analyze a password's strength: entropy, estimated crack time, "
                           "issues, and whether it appears in known breaches (HaveIBeenPwned "
                           "k-anonymity — only a hash prefix leaves the server).",
            "parameters": {"type": "object", "properties": {
                "password": {"type": "string", "description": "The password to analyze."}},
                "required": ["password"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_password",
            "description": "Generate a strong random password.",
            "parameters": {"type": "object", "properties": {
                "length": {"type": "integer", "description": "Length (8–128, default 20)."},
                "symbols": {"type": "boolean", "description": "Include symbols (default true)."}},
                "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hash_text",
            "description": "Compute MD5/SHA-1/SHA-256/SHA-512 hashes of text.",
            "parameters": {"type": "object", "properties": {
                "text": {"type": "string"},
                "algo": {"type": "string", "enum": ["md5", "sha1", "sha256", "sha512"]}},
                "required": ["text"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "decode_jwt",
            "description": "Decode a JWT's header and payload (no signature verification).",
            "parameters": {"type": "object", "properties": {
                "token": {"type": "string", "description": "The JWT string."}},
                "required": ["token"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_ip",
            "description": "Look up an IP's geolocation + reputation (country, city, ISP, ASN, "
                           "reverse DNS, proxy/VPN or datacenter flags). Great for investigating "
                           "attacking IPs from a brute-force analysis.",
            "parameters": {"type": "object", "properties": {
                "ip": {"type": "string", "description": "IPv4/IPv6 address."}},
                "required": ["ip"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_defense_status",
            "description": "Get Talos's live self-defense status: currently blocked IPs, "
                           "total attacks auto-blocked, a breakdown by attack type, and recent "
                           "events. Use when the user asks about attacks on the app, blocked "
                           "IPs, or the app's own security posture.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_security_tools",
            "description": "SEARCH Talos's 80+ built-in security tools by KEYWORD. Put what you "
                           "want to do in `search` (e.g. 'scan ports', 'decode base64', 'whois "
                           "domain', 'password strength', 'subnet calculator', 'identify hash') "
                           "and it returns only the best-matching tools with their input keys — "
                           "not the whole list. Then call run_security_tool with the chosen name. "
                           "Always search by keyword; with no keyword it returns only a category "
                           "summary.",
            "parameters": {"type": "object", "properties": {
                "search": {"type": "string", "description": "Keywords for the task/tool you need, e.g. 'port scan', 'hash identify', 'phishing domain', 'jwt'."},
                "category": {"type": "string", "description": "Optional category filter: Crypto & Encoding, Network & Recon, Web App Testing, OSINT & Threat Intel, Forensics & Analysis, Defensive / Blue-team."}},
                "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_security_tool",
            "description": "Run one of Talos's 70+ built-in security tools by its EXACT name "
                           "(discover names via list_security_tools). Covers encoders/decoders & "
                           "hashers, DNS/subdomain/port/network recon, TLS & security-header "
                           "checks, OSINT (crt.sh cert transparency, Wayback history, domain "
                           "profiling, username enumeration, Google dorks), forensic analyzers, "
                           "and blue-team helpers. Binary-backed tools (nmap, traceroute, yara, "
                           "ping, nslookup, openssl) run the real CLI when it is installed and "
                           "otherwise report how to install it. Defensive use on systems you own "
                           "or are authorized to test ONLY.",
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string", "description": "Exact tool name, e.g. 'dns_enumeration', 'cert_transparency', 'traceroute', 'base64_encode'."},
                "args": {"type": "object", "description": "Keyword arguments matching the tool's input keys, e.g. {\"domain\": \"example.com\"} or {\"target\": \"example.com\"}."}},
                "required": ["name"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_resources",
            "description": "Search the user's uploaded RESOURCE LIBRARY (their own books, "
                           "manuals, PDFs) by KEYWORD — case-insensitive. Returns the top "
                           "matching paragraphs, each with its book title + page number, so "
                           "you can ground answers in the user's own material and cite the "
                           "page. Use it whenever a question may be answered by their uploaded "
                           "material (or they say 'my book', 'the document I uploaded', 'my "
                           "notes'). Returns at most 20 snippets; if a snippet isn't enough, "
                           "call get_resource_page for the full page.",
            "parameters": {"type": "object", "properties": {
                "keywords": {"type": "string", "description": "Search keywords/phrase, e.g. 'tcp three-way handshake' or 'sql injection prevention'."},
                "limit": {"type": "integer", "description": "Max results to return (1–20, default 20)."}},
                "required": ["keywords"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_resource_page",
            "description": "Fetch the FULL text of one page of one book in the resource "
                           "library — use when a search_resources snippet isn't enough. "
                           "Identify the book by the `book_id` from a search_resources or "
                           "list_resources result, and give the 1-based `page` number.",
            "parameters": {"type": "object", "properties": {
                "book_id": {"type": "string", "description": "The book's id (from search_resources / list_resources results)."},
                "page": {"type": "integer", "description": "1-based page number."}},
                "required": ["book_id", "page"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_resources",
            "description": "List the books/documents in the user's resource library (title, "
                           "id, page count). Use to discover what reference material is "
                           "available, or to find a book_id before fetching a page.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


async def dispatch(name: str, args: dict) -> dict:
    """Execute a tool by name and return a JSON-serializable result."""
    try:
        if name == "scan_website":
            report = await scan(args.get("url", ""))
            return report.to_compact_dict()
        if name == "lookup_cves":
            return await lookup_cves(args.get("product", ""), args.get("version"))
        if name == "analyze_auth_log":
            return await asyncio.to_thread(
                analyze_log, args.get("path"), args.get("threshold")
            )
        if name == "search_research":
            return await _research(args)
        if name == "generate_blocklist":
            return mitigation.build_mitigation(args.get("ips", []), args.get("threshold", 5))
        if name == "send_alert":
            return await asyncio.to_thread(
                alerts.send_alert, args.get("message", ""),
                args.get("subject", "Talos security alert"))
        if name == "check_password_strength":
            return await asyncio.to_thread(secutils.check_password_strength, args.get("password", ""))
        if name == "generate_password":
            return secutils.generate_password(args.get("length", 20), args.get("symbols", True))
        if name == "hash_text":
            return secutils.hash_text(args.get("text", ""), args.get("algo", "sha256"))
        if name == "decode_jwt":
            return secutils.decode_jwt(args.get("token", ""))
        if name == "lookup_ip":
            return await secutils.lookup_ip(args.get("ip", ""))
        if name == "get_defense_status":
            return defense.status()
        if name == "list_security_tools":
            return _list_security_tools(args)
        if name == "run_security_tool":
            tool_name = (args.get("name") or "").strip()
            if not tool_name:
                return {"error": "Provide the exact 'name' of a security tool to run."}
            return await sectools.run(tool_name, args.get("args") or {})
        if name == "search_resources":
            return await asyncio.to_thread(
                resources.search_resources, args.get("keywords", ""), args.get("limit", 20))
        if name == "get_resource_page":
            return await asyncio.to_thread(
                resources.get_resource_page, args.get("book_id", ""), args.get("page", 0))
        if name == "list_resources":
            return await asyncio.to_thread(lambda: {"books": resources.list_books()})
        return {"error": f"Unknown tool: {name}"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"Tool '{name}' failed: {type(e).__name__}: {e}"}


_TOOL_TOP_N = 12


def _compact_tool(s: dict) -> dict:
    return {
        "name": s.get("name"),
        "category": s.get("category"),
        "description": s.get("description"),
        "inputs": [i.get("key") for i in s.get("inputs", [])],
    }


def _list_security_tools(args: dict) -> dict:
    """KEYWORD SEARCH over the 80+ sectools — returns only the best-matching tools
    (ranked, capped), never the whole catalog, so the model's context stays small
    and focused. With no keyword it returns just a category summary."""
    full = sectools.catalog()
    cat = (args.get("category") or "").strip().lower()
    q = (args.get("search") or args.get("query") or "").strip().lower()
    cats = sorted({s.get("category", "") for s in full})
    pool = [s for s in full if not cat or (s.get("category") or "").lower() == cat]

    # No keyword: return a compact summary, NOT every tool.
    if not q:
        if cat:
            return {"category": cat, "count": len(pool),
                    "tools": [_compact_tool(s) for s in pool]}
        n = Counter(s.get("category", "") for s in full)
        return {
            "total": len(full),
            "categories": [{"name": c, "tools": n.get(c, 0)} for c in cats],
            "hint": "Search again with a keyword (e.g. 'port scan', 'decode base64', "
                    "'whois domain', 'password strength') to get the matching tools.",
        }

    # Keyword relevance ranking.
    terms = [t for t in re.split(r"[^a-z0-9]+", q) if t]
    scored = []
    for s in pool:
        name = (s.get("name") or "").lower()
        label = (s.get("label") or "").lower()
        desc = (s.get("description") or "").lower()
        category = (s.get("category") or "").lower()
        keys = " ".join((i.get("key") or "") for i in s.get("inputs", [])).lower()
        score = 0
        for t in terms:
            if t in name or t in label:
                score += 5
            if t in category:
                score += 3
            if t in desc:
                score += 2
            if t in keys:
                score += 1
        if q in f"{name} {label} {desc}":
            score += 4
        if score:
            scored.append((score, s))
    scored.sort(key=lambda x: (-x[0], x[1].get("name", "")))
    top = [_compact_tool(s) for _, s in scored[:_TOOL_TOP_N]]
    return {
        "query": q,
        "match_count": len(scored),
        "showing": len(top),
        "tools": top,
        "note": ("Top matches only — refine the keyword for more."
                 if len(scored) > len(top) else None),
        "next": "Pick one and call run_security_tool with its 'name' + inputs.",
    }


def _compact_paper(p: dict) -> dict:
    text = p.get("tldr") or p.get("abstract") or ""
    return {
        "title": p.get("title"),
        "authors": (p.get("authors") or [])[:4],
        "year": p.get("year"),
        "journal": p.get("journal"),
        "cited_by": p.get("cited_by"),
        "is_oa": p.get("is_oa"),
        "url": p.get("oa_url") or p.get("url"),
        "doi": p.get("doi"),
        "summary": (text[:240] + "…") if len(text) > 240 else text,
        "source": p.get("source"),
    }


async def _research(args: dict) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "Empty research query."}
    source = (args.get("source") or "all").lower()   # default: search ALL providers
    year_from = args.get("year_from")
    open_access = bool(args.get("open_access"))
    if source == "all":
        papers = await research.multi_search(
            query, sources=["openalex", "semantic_scholar", "core"],
            year_from=year_from, open_access=open_access, limit=8)
    elif source == "core":
        papers = await research.search_core(query, year_from=year_from, limit=8)
    else:
        papers = await research.run_search(
            source, query, year_from=year_from, open_access=open_access, limit=8)
    providers = sorted({p.get("source") for p in papers if p.get("source")})
    return {
        "query": query, "source": source, "providers": providers,
        "count": len(papers),
        "papers": [_compact_paper(p) for p in papers[:12]],
    }


def summarize_result(name: str, result: dict) -> str:
    """A short, human-readable line for the UI's live tool-activity feed."""
    if result.get("error"):
        return result["error"]
    if name == "scan_website":
        return f"Grade {result.get('grade')} ({result.get('score')}/100) — " \
               f"{len(result.get('findings', []))} findings on {result.get('target')}"
    if name == "lookup_cves":
        return f"{result.get('count', 0)} CVE(s) for {result.get('product')}"
    if name == "analyze_auth_log":
        s = result.get("summary", {})
        return f"{s.get('attacks', 0)} attacking IP(s), {s.get('suspicious', 0)} suspicious"
    if name == "search_research":
        prov = result.get("providers") or []
        src = ", ".join(prov) if prov else result.get("source")
        return f"{result.get('count', 0)} papers · {src}"
    if name == "generate_blocklist":
        return f"block rules for {result.get('ip_count', 0)} IP(s)"
    if name == "send_alert":
        if result.get("sent"):
            chans = [k for k, v in (result.get("channels") or {}).items() if v == "sent"]
            return "alert sent via " + ", ".join(chans)
        return result.get("note") or "no alert channel configured"
    if name == "check_password_strength":
        return f"{result.get('strength')} · {result.get('entropy_bits')} bits"
    if name == "generate_password":
        return f"{result.get('length')}-char {result.get('strength')} password"
    if name == "hash_text":
        return f"{result.get('algorithm')} hash"
    if name == "decode_jwt":
        return result.get("error") or "JWT decoded"
    if name == "lookup_ip":
        if result.get("error"):
            return result["error"]
        if result.get("note") and not result.get("country"):
            return result["note"]
        return f"{result.get('city') or ''} {result.get('country') or ''} · {result.get('isp') or ''}".strip(" ·")
    if name == "get_defense_status":
        return f"{result.get('blocked_count', 0)} IP(s) blocked · {result.get('events_total', 0)} attacks"
    if name == "list_security_tools":
        return f"{result.get('count', 0)} tools across {len(result.get('categories', []))} categories"
    if name == "run_security_tool":
        if result.get("needs_binary"):
            return f"needs '{result.get('needs_binary')}' installed"
        if result.get("error"):
            return result["error"]
        return "tool ran ✓"
    if name == "search_resources":
        if result.get("error"):
            return result["error"]
        return f"{result.get('showing', 0)} of {result.get('match_count', 0)} " \
               f"paragraph(s) for '{result.get('query', '')}'"
    if name == "get_resource_page":
        if result.get("error"):
            return result["error"]
        return f"{result.get('book_title', '')} · p.{result.get('page')} " \
               f"({result.get('chars', 0)} chars)"
    if name == "list_resources":
        return f"{len(result.get('books', []))} book(s) in library"
    return "done"
