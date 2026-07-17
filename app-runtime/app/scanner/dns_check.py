"""DNS + email-security checks (SPF / DMARC / CAA). Blocking; run via to_thread."""
from __future__ import annotations

import dns.resolver

from .models import Finding, Severity
from .net import Target


def _txt(domain: str) -> list[str]:
    try:
        ans = dns.resolver.resolve(domain, "TXT", lifetime=6)
        return ["".join(s.decode() if isinstance(s, bytes) else s
                        for s in r.strings) for r in ans]
    except Exception:  # noqa: BLE001 (NXDOMAIN, timeout, etc.)
        return []


def _has(domain: str, rtype: str) -> bool:
    try:
        dns.resolver.resolve(domain, rtype, lifetime=6)
        return True
    except Exception:  # noqa: BLE001
        return False


def _registrable(host: str) -> str:
    parts = host.split(".")
    if len(parts) > 2:
        return ".".join(parts[-2:])
    return host


def check_dns_email(target: Target) -> list[Finding]:
    if target.is_ip:
        return []
    findings: list[Finding] = []
    domain = _registrable(target.host)

    # SPF
    txt = _txt(domain)
    if not any(t.lower().startswith("v=spf1") for t in txt):
        findings.append(Finding(
            id="dns-spf", title="No SPF record",
            severity=Severity.MEDIUM, category="Email security",
            description=f"{domain} has no SPF (v=spf1) TXT record, so attackers "
                        "can more easily spoof email from this domain.",
            recommendation="Publish an SPF record listing your authorized mail servers.",
            references=["https://www.rfc-editor.org/rfc/rfc7208"],
        ))

    # DMARC
    dmarc = _txt("_dmarc." + domain)
    dmarc_rec = next((t for t in dmarc if t.lower().startswith("v=dmarc1")), None)
    if not dmarc_rec:
        findings.append(Finding(
            id="dns-dmarc", title="No DMARC record",
            severity=Severity.MEDIUM, category="Email security",
            description=f"{domain} has no DMARC policy, so spoofed mail is not "
                        "reported or rejected by receivers.",
            recommendation="Publish a _dmarc TXT record, starting at p=none then tightening to p=reject.",
            references=["https://dmarc.org/"],
        ))
    elif "p=none" in dmarc_rec.lower():
        findings.append(Finding(
            id="dns-dmarc-none", title="DMARC policy is 'none' (monitor only)",
            severity=Severity.LOW, category="Email security",
            description="DMARC is set to p=none, which only monitors and does "
                        "not block spoofed mail.",
            evidence=dmarc_rec[:160],
            recommendation="Move toward p=quarantine then p=reject once aligned.",
        ))

    # CAA
    if not _has(domain, "CAA"):
        findings.append(Finding(
            id="dns-caa", title="No CAA record",
            severity=Severity.INFO, category="DNS",
            description=f"{domain} has no CAA record; any CA may issue "
                        "certificates for it.",
            recommendation="Add a CAA record naming your certificate authority.",
        ))
    return findings
