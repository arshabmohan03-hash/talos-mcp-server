"""TLS/SSL certificate + protocol checks (blocking; run via asyncio.to_thread)."""
from __future__ import annotations

import datetime as dt
import socket
import ssl

from cryptography import x509
from cryptography.hazmat.backends import default_backend

from .models import Finding, Severity
from .net import Target

WEAK_PROTOCOLS = {"SSLv2", "SSLv3", "TLSv1", "TLSv1.1"}


def check_tls(target: Target) -> list[Finding]:
    if target.scheme != "https":
        return [Finding(
            id="tls-none", title="Site not served over HTTPS",
            severity=Severity.HIGH, category="TLS/SSL",
            description="The target uses plain HTTP, so all traffic "
                        "(including credentials) is transmitted unencrypted.",
            recommendation="Obtain a TLS certificate (e.g. Let's Encrypt) and serve over HTTPS.",
        )]

    findings: list[Finding] = []
    host, port = target.host, target.port

    # 1) Validated handshake (hostname + chain). Captures negotiated protocol.
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=8) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                proto = ssock.version()
                der = ssock.getpeercert(binary_form=True)
        if proto in WEAK_PROTOCOLS:
            findings.append(Finding(
                id="tls-weak-proto", title=f"Weak TLS protocol negotiated ({proto})",
                severity=Severity.HIGH, category="TLS/SSL",
                description=f"The server negotiated {proto}, which is deprecated "
                            "and vulnerable to known attacks.",
                recommendation="Disable TLS 1.0/1.1 and SSLv3; require TLS 1.2+.",
            ))
    except ssl.SSLCertVerificationError as e:
        findings.append(Finding(
            id="tls-verify", title="Certificate failed validation",
            severity=Severity.HIGH, category="TLS/SSL",
            description=f"The certificate did not validate: {e.verify_message or e}. "
                        "Could be self-signed, expired, or hostname mismatch.",
            recommendation="Install a valid certificate from a trusted CA covering this hostname.",
        ))
        der = _grab_cert_insecure(host, port)
    except (OSError, ssl.SSLError) as e:
        return [Finding(
            id="tls-error", title="Could not establish TLS connection",
            severity=Severity.MEDIUM, category="TLS/SSL",
            description=f"TLS handshake failed: {e}",
            recommendation="Verify the server's TLS configuration and certificate.",
        )]

    # 2) Parse the certificate for expiry + issuer details
    if der:
        findings.extend(_inspect_cert(der, host))
    return findings


def _grab_cert_insecure(host: str, port: int) -> bytes | None:
    ctx = ssl._create_unverified_context()  # noqa: SLF001 (intentional: inspect bad certs)
    try:
        with socket.create_connection((host, port), timeout=8) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                return ssock.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError):
        return None


def _inspect_cert(der: bytes, host: str) -> list[Finding]:
    findings: list[Finding] = []
    try:
        cert = x509.load_der_x509_certificate(der, default_backend())
    except Exception:  # noqa: BLE001
        return findings

    now = dt.datetime.now(dt.timezone.utc)
    not_after = cert.not_valid_after_utc
    days_left = (not_after - now).days

    if days_left < 0:
        findings.append(Finding(
            id="tls-expired", title="TLS certificate has expired",
            severity=Severity.CRITICAL, category="TLS/SSL",
            description=f"The certificate expired on {not_after:%Y-%m-%d} "
                        f"({-days_left} days ago). Browsers will warn or block users.",
            recommendation="Renew the TLS certificate immediately and automate renewal.",
        ))
    elif days_left < 15:
        findings.append(Finding(
            id="tls-expiring", title=f"TLS certificate expires soon ({days_left} days)",
            severity=Severity.MEDIUM, category="TLS/SSL",
            description=f"The certificate expires on {not_after:%Y-%m-%d}.",
            recommendation="Renew now; automate renewal (e.g. certbot) to avoid outages.",
        ))

    issuer = cert.issuer.rfc4514_string()
    subject = cert.subject.rfc4514_string()
    if issuer == subject:
        findings.append(Finding(
            id="tls-selfsigned", title="Self-signed certificate",
            severity=Severity.HIGH, category="TLS/SSL",
            description="The certificate is self-signed (issuer == subject); "
                        "clients cannot verify the server's identity.",
            recommendation="Replace with a certificate from a trusted CA.",
        ))
    return findings
