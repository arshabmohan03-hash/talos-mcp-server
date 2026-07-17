"""Non-destructive web vulnerability scanner.

Public API:
    from app.scanner import scan          # async full scan -> ScanReport
    from app.scanner import lookup_cves    # async CVE lookup tool
"""
from .cve import lookup_cves
from .engine import scan
from .models import Finding, ScanReport, Severity

__all__ = ["scan", "lookup_cves", "ScanReport", "Finding", "Severity"]
