"""Scan orchestrator: runs all checks concurrently and assembles a report."""
from __future__ import annotations

import asyncio
import time

import httpx

from . import web_checks
from .dns_check import check_dns_email
from .models import ScanReport
from .net import TargetError, make_client, normalize_target, resolve_ip
from .tls_check import check_tls


async def _http_phase(target) -> tuple[list, list[str], list[str]]:
    """Returns (findings, checks_run, errors)."""
    findings: list = []
    checks: list[str] = []
    errors: list[str] = []
    try:
        async with make_client() as client:
            resp = await client.get(target.url)
            final = str(resp.url)
            ctype = resp.headers.get("content-type", "")
            html = resp.text if ctype.startswith("text") else ""

            findings += web_checks.check_security_headers(resp); checks.append("security-headers")
            findings += web_checks.check_cookies(resp); checks.append("cookies")
            fdisc, _detections = web_checks.check_information_disclosure(resp)
            findings += fdisc; checks.append("information-disclosure")
            findings += web_checks.fingerprint(resp, html); checks.append("fingerprint")
            findings += web_checks.check_https_redirect(target, resp); checks.append("https-redirect")
            findings += web_checks.check_mixed_content(resp, html); checks.append("mixed-content")

            ef, st = await asyncio.gather(
                web_checks.check_exposed_files(client, final),
                web_checks.check_security_txt(client, final),
            )
            findings += ef; checks.append("exposed-files")
            findings += st; checks.append("security-txt")
            return findings, checks, errors
    except httpx.HTTPError as e:
        errors.append(f"HTTP fetch failed: {type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001
        errors.append(f"HTTP phase error: {type(e).__name__}: {e}")
    return findings, checks, errors


async def scan(raw_target: str) -> ScanReport:
    """Run a full, non-destructive security scan of a single web target."""
    t0 = time.perf_counter()
    try:
        target = normalize_target(raw_target)
    except TargetError as e:
        return ScanReport(target=raw_target, errors=[str(e)])

    report = ScanReport(target=target.url)
    report.ip = await asyncio.to_thread(resolve_ip, target.host)

    http_res, tls_res, dns_res = await asyncio.gather(
        _http_phase(target),
        asyncio.to_thread(check_tls, target),
        asyncio.to_thread(check_dns_email, target),
        return_exceptions=True,
    )

    if isinstance(http_res, tuple):
        findings, checks, errors = http_res
        report.findings += findings
        report.checks_run += checks
        report.errors += errors
    else:
        report.errors.append(f"HTTP phase crashed: {http_res}")

    if isinstance(tls_res, list):
        report.findings += tls_res
        report.checks_run.append("tls")
    else:
        report.errors.append(f"TLS check crashed: {tls_res}")

    if isinstance(dns_res, list):
        report.findings += dns_res
        report.checks_run.append("dns-email")
    else:
        report.errors.append(f"DNS check crashed: {dns_res}")

    report.final_url = report.final_url or target.url
    report.duration_ms = int((time.perf_counter() - t0) * 1000)
    return report
