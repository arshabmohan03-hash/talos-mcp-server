"""URL normalization + HTTP fetch helpers (safe, non-destructive)."""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import httpx

from app.config import get_settings


class TargetError(ValueError):
    """Raised when a target URL is invalid or cannot be safely scanned."""


@dataclass
class Target:
    raw: str
    url: str          # normalized, with scheme + path
    scheme: str
    host: str
    port: int
    is_ip: bool
    is_private: bool


def normalize_target(raw: str) -> Target:
    raw = (raw or "").strip()
    if not raw:
        raise TargetError("Empty target.")
    # strip surrounding quotes / trailing punctuation a user might paste
    raw = raw.strip().strip("\"'<>")
    if "://" not in raw:
        raw = "https://" + raw

    p = urlparse(raw)
    if p.scheme not in ("http", "https"):
        raise TargetError(f"Unsupported scheme: {p.scheme!r} (use http/https).")
    if not p.hostname:
        raise TargetError("Could not parse a hostname from the target.")

    host = p.hostname
    port = p.port or (443 if p.scheme == "https" else 80)

    is_ip = False
    is_private = False
    try:
        ip = ipaddress.ip_address(host)
        is_ip = True
        is_private = ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        # hostname; best-effort private check via resolution happens later
        if host in ("localhost",) or host.endswith(".local"):
            is_private = True

    url = urlunparse((p.scheme, p.netloc, p.path or "/", "", p.query, ""))
    return Target(
        raw=raw, url=url, scheme=p.scheme, host=host,
        port=port, is_ip=is_ip, is_private=is_private,
    )


def resolve_ip(host: str) -> str | None:
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


def make_client(verify: bool = False) -> httpx.AsyncClient:
    """An async client for content fetches.

    verify=False so we can still analyze sites with broken certs — TLS
    validity is judged authoritatively by tls_check, not here.
    """
    s = get_settings()
    return httpx.AsyncClient(
        timeout=httpx.Timeout(s.scan_timeout, connect=min(s.scan_timeout, 8.0)),
        follow_redirects=True,
        max_redirects=5,
        verify=verify,
        headers={
            "User-Agent": s.user_agent,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
