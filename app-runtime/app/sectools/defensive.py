"""Defensive / Blue-team tooling for Talos.

Pure-logic, non-destructive helpers a defender uses day-to-day: mapping findings
to MITRE ATT&CK, emitting Sigma detection rules and firewall blocklists, drafting
incident reports, prioritising vulnerabilities by CVSS, sanity-checking findings
against the OWASP Top 10 (2021), and recommending a password policy.

Every tool is a plain function that takes keyword arguments and returns a
JSON-serialisable dict. Failures are caught and returned as {"error": ...} — these
never raise to the caller. No network access, no external binaries: stdlib only.
"""
from __future__ import annotations

import datetime
import ipaddress
import re

# --------------------------------------------------------------------------- #
# 1. MITRE ATT&CK mapper
# --------------------------------------------------------------------------- #

# ~25 common enterprise techniques. Each entry: keywords -> (tactic, tech id, name).
_ATTACK_DB: list[dict] = [
    {"id": "T1110", "name": "Brute Force", "tactic": "Credential Access",
     "keywords": ["brute force", "brute-force", "password guessing", "password spray",
                  "credential stuffing", "failed login", "auth log", "ssh attack"]},
    {"id": "T1110.001", "name": "Password Guessing", "tactic": "Credential Access",
     "keywords": ["password guessing", "guess password", "rdp brute"]},
    {"id": "T1110.003", "name": "Password Spraying", "tactic": "Credential Access",
     "keywords": ["password spray", "spraying", "single password many accounts"]},
    {"id": "T1078", "name": "Valid Accounts", "tactic": "Defense Evasion",
     "keywords": ["valid account", "stolen credential", "compromised account",
                  "legitimate credential", "default account"]},
    {"id": "T1190", "name": "Exploit Public-Facing Application", "tactic": "Initial Access",
     "keywords": ["exploit", "public-facing", "web exploit", "rce", "remote code execution",
                  "unauthenticated", "cve", "vulnerable app", "deserialization"]},
    {"id": "T1566", "name": "Phishing", "tactic": "Initial Access",
     "keywords": ["phish", "phishing", "spearphish", "malicious link", "credential harvest email"]},
    {"id": "T1059", "name": "Command and Scripting Interpreter", "tactic": "Execution",
     "keywords": ["command injection", "shell", "powershell", "bash", "cmd.exe",
                  "script execution", "webshell", "web shell"]},
    {"id": "T1059.001", "name": "PowerShell", "tactic": "Execution",
     "keywords": ["powershell", "encodedcommand", "invoke-expression", "iex "]},
    {"id": "T1053", "name": "Scheduled Task/Job", "tactic": "Persistence",
     "keywords": ["scheduled task", "cron", "crontab", "at job", "schtasks"]},
    {"id": "T1543", "name": "Create or Modify System Process", "tactic": "Persistence",
     "keywords": ["new service", "systemd", "service install", "daemon persist"]},
    {"id": "T1547", "name": "Boot or Logon Autostart Execution", "tactic": "Persistence",
     "keywords": ["registry run key", "autostart", "startup folder", "logon script"]},
    {"id": "T1548", "name": "Abuse Elevation Control Mechanism", "tactic": "Privilege Escalation",
     "keywords": ["sudo", "setuid", "uac bypass", "privilege escalation", "elevation"]},
    {"id": "T1068", "name": "Exploitation for Privilege Escalation", "tactic": "Privilege Escalation",
     "keywords": ["kernel exploit", "local privilege escalation", "lpe", "dirty pipe"]},
    {"id": "T1003", "name": "OS Credential Dumping", "tactic": "Credential Access",
     "keywords": ["mimikatz", "lsass", "credential dump", "sam dump", "ntds.dit", "hashdump"]},
    {"id": "T1552", "name": "Unsecured Credentials", "tactic": "Credential Access",
     "keywords": ["hardcoded password", "secret in code", "exposed key", "api key leak",
                  ".env exposed", "credentials in config"]},
    {"id": "T1110.004", "name": "Credential Stuffing", "tactic": "Credential Access",
     "keywords": ["credential stuffing", "reused password", "breach replay"]},
    {"id": "T1046", "name": "Network Service Discovery", "tactic": "Discovery",
     "keywords": ["port scan", "nmap", "service scan", "network scan", "enumeration scan"]},
    {"id": "T1018", "name": "Remote System Discovery", "tactic": "Discovery",
     "keywords": ["host discovery", "ping sweep", "remote system discovery"]},
    {"id": "T1021", "name": "Remote Services", "tactic": "Lateral Movement",
     "keywords": ["lateral movement", "rdp", "smb", "psexec", "ssh pivot", "winrm"]},
    {"id": "T1071", "name": "Application Layer Protocol", "tactic": "Command and Control",
     "keywords": ["c2", "command and control", "beacon", "http c2", "dns c2", "https c2"]},
    {"id": "T1048", "name": "Exfiltration Over Alternative Protocol", "tactic": "Exfiltration",
     "keywords": ["exfiltration", "data exfil", "dns tunneling", "data theft", "stolen data upload"]},
    {"id": "T1486", "name": "Data Encrypted for Impact", "tactic": "Impact",
     "keywords": ["ransomware", "encrypt files", "ransom note", "file encryption attack"]},
    {"id": "T1498", "name": "Network Denial of Service", "tactic": "Impact",
     "keywords": ["ddos", "dos", "denial of service", "flood", "syn flood", "volumetric"]},
    {"id": "T1499", "name": "Endpoint Denial of Service", "tactic": "Impact",
     "keywords": ["resource exhaustion", "application dos", "slowloris"]},
    {"id": "T1562", "name": "Impair Defenses", "tactic": "Defense Evasion",
     "keywords": ["disable firewall", "disable antivirus", "clear logs", "tamper logging",
                  "stop edr", "defense evasion"]},
    {"id": "T1070", "name": "Indicator Removal", "tactic": "Defense Evasion",
     "keywords": ["log deletion", "clear event log", "wipe history", "delete logs", "timestomp"]},
    {"id": "T1056", "name": "Input Capture", "tactic": "Collection",
     "keywords": ["keylogger", "keystroke", "input capture", "form grab"]},
]


def mitre_attack_mapper(finding: str = "", max_results: int = 5) -> dict:
    """Map a free-text finding / keyword to likely MITRE ATT&CK techniques."""
    try:
        text = (finding or "").strip().lower()
        if not text:
            return {"error": "Provide a finding or keyword to map."}
        try:
            limit = max(1, min(10, int(max_results or 5)))
        except (TypeError, ValueError):
            limit = 5

        scored: list[tuple[int, dict]] = []
        for entry in _ATTACK_DB:
            hits = [kw for kw in entry["keywords"] if kw in text]
            if hits:
                # Longer keyword matches are more specific -> higher score.
                score = sum(len(kw) for kw in hits)
                scored.append((score, {
                    "technique_id": entry["id"],
                    "technique": entry["name"],
                    "tactic": entry["tactic"],
                    "matched_keywords": hits,
                    "reference": f"https://attack.mitre.org/techniques/{entry['id'].replace('.', '/')}/",
                }))

        scored.sort(key=lambda x: x[0], reverse=True)
        matches = [m for _, m in scored[:limit]]
        return {
            "finding": finding,
            "match_count": len(matches),
            "matches": matches,
            "note": ("No ATT&CK technique matched — try keywords like 'brute force', "
                     "'phishing', 'ransomware', 'port scan'.")
                    if not matches else
                    "Heuristic keyword mapping — verify against the full ATT&CK matrix.",
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
# 2. Sigma rule generator
# --------------------------------------------------------------------------- #

_SIGMA_LEVELS = {"low", "medium", "high", "critical", "informational"}


def _yaml_scalar(value: str) -> str:
    """Quote a scalar for YAML if it contains characters that need it."""
    s = str(value)
    if s == "":
        return "''"
    if re.search(r"[:\{\}\[\],&\*#\?\|\-<>=!%@`\"']", s) or s.strip() != s:
        return "'" + s.replace("'", "''") + "'"
    return s


def sigma_rule_generator(title: str = "", logsource: str = "", field: str = "",
                         value: str = "", level: str = "medium",
                         description: str = "") -> dict:
    """Emit a valid Sigma YAML rule from a single field=value detection condition.

    `logsource` accepts either a bare product (e.g. 'windows') or a
    'category/product' or 'product:service' style hint that is split sensibly.
    """
    try:
        title = (title or "").strip()
        field = (field or "").strip()
        value = (value or "").strip()
        if not title:
            return {"error": "A rule title is required."}
        if not field:
            return {"error": "A detection field is required (e.g. 'EventID')."}

        lvl = (level or "medium").strip().lower()
        if lvl not in _SIGMA_LEVELS:
            lvl = "medium"

        # Parse the logsource hint into product / category / service.
        raw = (logsource or "").strip()
        product = category = service = ""
        if raw:
            parts = re.split(r"[\s/:,]+", raw)
            parts = [p for p in parts if p]
            if len(parts) == 1:
                product = parts[0]
            else:
                product, service = parts[0], parts[1]

        # Build a slug id-ish title and ISO date deterministically.
        today = datetime.date.today().isoformat()
        desc = (description or "").strip() or f"Detects {field} equal to {value}."

        lines: list[str] = []
        lines.append(f"title: {_yaml_scalar(title)}")
        lines.append(f"status: experimental")
        lines.append(f"description: {_yaml_scalar(desc)}")
        lines.append("author: Talos")
        lines.append(f"date: {today}")
        lines.append("logsource:")
        if category:
            lines.append(f"    category: {_yaml_scalar(category)}")
        if product:
            lines.append(f"    product: {_yaml_scalar(product)}")
        if service:
            lines.append(f"    service: {_yaml_scalar(service)}")
        if not (category or product or service):
            lines.append("    product: generic")
        lines.append("detection:")
        lines.append("    selection:")
        lines.append(f"        {field}: {_yaml_scalar(value)}")
        lines.append("    condition: selection")
        lines.append("falsepositives:")
        lines.append("    - Unknown")
        lines.append(f"level: {lvl}")

        yaml = "\n".join(lines) + "\n"
        return {
            "title": title,
            "level": lvl,
            "sigma_yaml": yaml,
            "note": "Validate with sigmac / pySigma before deploying to your SIEM.",
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
# 3. Firewall rule generator
# --------------------------------------------------------------------------- #

_FW_PLATFORMS = {"iptables", "ufw", "nftables", "cisco-asa", "pf", "windows"}


def _parse_ips(raw) -> tuple[list[str], list[str]]:
    """Split free-text input into (valid IPs/CIDRs, rejected tokens)."""
    if isinstance(raw, (list, tuple)):
        raw = " ".join(str(x) for x in raw)
    valid: list[str] = []
    rejected: list[str] = []
    seen: set[str] = set()
    tokens = re.split(r"[\s,;]+", str(raw or "").strip())
    for tok in tokens:
        if not tok:
            continue
        try:
            if "/" in tok:
                net = ipaddress.ip_network(tok, strict=False)
                norm = str(net)
            else:
                norm = str(ipaddress.ip_address(tok))
            if norm not in seen:
                seen.add(norm)
                valid.append(norm)
        except ValueError:
            rejected.append(tok)
    return valid, rejected


def _is_v6(addr: str) -> bool:
    host = addr.split("/")[0]
    try:
        return ipaddress.ip_address(host).version == 6
    except ValueError:
        return False


def firewall_rule_generator(ips: str = "", platform: str = "iptables",
                            direction: str = "in") -> dict:
    """Generate copy-paste block rules for a set of IPs/CIDRs on a given platform."""
    try:
        plat = (platform or "iptables").strip().lower()
        if plat not in _FW_PLATFORMS:
            return {"error": f"Unsupported platform '{platform}'. "
                             f"Choose one of: {', '.join(sorted(_FW_PLATFORMS))}."}
        valid, rejected = _parse_ips(ips)
        if not valid:
            return {"error": "No valid IP addresses or CIDR ranges provided.",
                    "rejected": rejected}

        rules: list[str] = []
        if plat == "iptables":
            rules.append("# iptables / ip6tables — generated by Talos")
            for ip in valid:
                cmd = "ip6tables" if _is_v6(ip) else "iptables"
                rules.append(f"{cmd} -A INPUT -s {ip} -j DROP")
        elif plat == "ufw":
            rules.append("# ufw — generated by Talos")
            for ip in valid:
                rules.append(f"ufw deny from {ip}")
        elif plat == "nftables":
            v4 = [ip for ip in valid if not _is_v6(ip)]
            v6 = [ip for ip in valid if _is_v6(ip)]
            rules.append("# nftables — generated by Talos")
            rules.append("table inet filter {")
            rules.append("    chain input {")
            rules.append("        type filter hook input priority 0; policy accept;")
            if v4:
                rules.append(f"        ip saddr {{ {', '.join(v4)} }} drop")
            if v6:
                rules.append(f"        ip6 saddr {{ {', '.join(v6)} }} drop")
            rules.append("    }")
            rules.append("}")
        elif plat == "cisco-asa":
            rules.append("! Cisco ASA — generated by Talos")
            for ip in valid:
                if _is_v6(ip):
                    rules.append(f"ipv6 access-list TALOS_BLOCK deny ipv6 {ip} any")
                else:
                    host = ip.split("/")[0]
                    if "/" in ip:
                        net = ipaddress.ip_network(ip, strict=False)
                        mask = str(net.netmask)
                        rules.append(f"access-list TALOS_BLOCK extended deny ip "
                                     f"{net.network_address} {mask} any")
                    else:
                        rules.append(f"access-list TALOS_BLOCK extended deny ip "
                                     f"host {host} any")
        elif plat == "pf":
            rules.append("# pf (BSD/macOS) — generated by Talos")
            rules.append("table <talos_block> persist { " + ", ".join(valid) + " }")
            rules.append("block drop in quick from <talos_block> to any")
        elif plat == "windows":
            rules.append(":: Windows Firewall (netsh) — generated by Talos")
            for ip in valid:
                rules.append(
                    f'netsh advfirewall firewall add rule '
                    f'name="Talos block {ip}" dir=in action=block remoteip={ip}')

        result = {
            "platform": plat,
            "ip_count": len(valid),
            "ips": valid,
            "rules": "\n".join(rules),
            "note": "Review before applying — blocking your own address can lock you out.",
        }
        if rejected:
            result["rejected"] = rejected
        return result
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
# 4. Incident report generator
# --------------------------------------------------------------------------- #

_SEVERITIES = {"critical", "high", "medium", "low", "informational"}


def _parse_lines(raw) -> list[str]:
    if isinstance(raw, list):
        items = [str(x).strip() for x in raw]
    else:
        items = [ln.strip() for ln in str(raw or "").splitlines()]
    return [i for i in items if i]


def incident_report_generator(title: str = "", severity: str = "medium",
                              summary: str = "", affected: str = "",
                              timeline: str = "") -> dict:
    """Produce a structured Markdown incident report.

    `affected` and `timeline` may be newline-separated text or a list. Timeline
    entries of the form 'timestamp - description' are rendered as a table.
    """
    try:
        title = (title or "").strip()
        if not title:
            return {"error": "An incident title is required."}
        sev = (severity or "medium").strip().lower()
        if sev not in _SEVERITIES:
            sev = "medium"

        affected_items = _parse_lines(affected)
        timeline_items = _parse_lines(timeline)
        generated = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC")

        md: list[str] = []
        md.append(f"# Incident Report: {title}")
        md.append("")
        md.append(f"- **Severity:** {sev.capitalize()}")
        md.append(f"- **Report generated:** {generated}")
        md.append(f"- **Status:** Under investigation")
        md.append("")
        md.append("## Summary")
        md.append("")
        md.append((summary or "").strip() or "_No summary provided._")
        md.append("")
        md.append("## Affected Assets")
        md.append("")
        if affected_items:
            md.extend(f"- {a}" for a in affected_items)
        else:
            md.append("_None recorded._")
        md.append("")
        md.append("## Timeline")
        md.append("")
        if timeline_items:
            # Render as a table when entries look like 'time - description'.
            rows = []
            for item in timeline_items:
                m = re.match(r"^(.*?)\s*[-–—]\s*(.*)$", item)
                if m and m.group(1) and m.group(2):
                    rows.append((m.group(1).strip(), m.group(2).strip()))
                else:
                    rows.append(("", item))
            md.append("| Time | Event |")
            md.append("| --- | --- |")
            for when, what in rows:
                md.append(f"| {when or '—'} | {what} |")
        else:
            md.append("_No timeline entries recorded._")
        md.append("")
        md.append("## Recommended Next Steps")
        md.append("")
        steps = {
            "critical": ["Activate the incident response team immediately.",
                         "Isolate affected systems from the network.",
                         "Preserve volatile evidence (memory, logs) before remediation.",
                         "Notify leadership and assess regulatory disclosure duties."],
            "high": ["Contain affected hosts and rotate exposed credentials.",
                     "Collect and preserve relevant logs.",
                     "Begin root-cause analysis."],
            "medium": ["Investigate scope and confirm impact.",
                       "Apply mitigations / patches for the affected component.",
                       "Monitor for recurrence."],
            "low": ["Track as a ticket and remediate during normal change windows.",
                    "Confirm no escalation path exists."],
            "informational": ["Document for awareness; no immediate action required."],
        }[sev]
        md.extend(f"{i}. {s}" for i, s in enumerate(steps, 1))
        md.append("")
        report = "\n".join(md)
        return {
            "title": title,
            "severity": sev,
            "affected_count": len(affected_items),
            "timeline_count": len(timeline_items),
            "markdown": report,
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
# 5. Vulnerability prioritizer
# --------------------------------------------------------------------------- #

# CVSS v3 severity bands + a recommended remediation SLA per bucket.
_VULN_BUCKETS = [
    ("Critical", 9.0, 10.0, "24 hours"),
    ("High", 7.0, 8.9, "7 days"),
    ("Medium", 4.0, 6.9, "30 days"),
    ("Low", 0.1, 3.9, "90 days"),
    ("None", 0.0, 0.0, "No action required"),
]


def _bucket_for(score: float) -> tuple[str, str]:
    for name, lo, hi, sla in _VULN_BUCKETS:
        if name == "None":
            continue
        if lo <= score <= hi:
            return name, sla
    if score <= 0:
        return "None", "No action required"
    return "Low", "90 days"


def _coerce_vulns(raw) -> list[dict]:
    """Accept a list of dicts, or newline/CSV text like 'CVE-2024-1 9.8'."""
    out: list[dict] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                out.append({"name": str(item.get("name", "")).strip() or "unnamed",
                            "cvss": item.get("cvss")})
            else:
                out.append({"name": str(item).strip() or "unnamed", "cvss": None})
        return out
    for line in str(raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # Last number on the line is treated as the CVSS score.
        nums = re.findall(r"\d+(?:\.\d+)?", line)
        score = nums[-1] if nums else None
        name = line
        if score is not None:
            name = re.sub(r"[\s,;:]*" + re.escape(score) + r"\s*$", "", line).strip()
        out.append({"name": name or "unnamed", "cvss": score})
    return out


def vuln_prioritizer(vulnerabilities="") -> dict:
    """Sort vulnerabilities by CVSS and bucket them with a recommended SLA."""
    try:
        items = _coerce_vulns(vulnerabilities)
        if not items:
            return {"error": "Provide vulnerabilities as a list of {name, cvss} "
                             "or lines like 'CVE-2024-1234 9.8'."}
        graded = []
        for it in items:
            raw_score = it.get("cvss")
            try:
                score = round(float(raw_score), 1)
            except (TypeError, ValueError):
                score = None
            if score is None:
                bucket, sla, sortable = "Unknown", "Assess — assign a CVSS score", -1.0
            else:
                score = max(0.0, min(10.0, score))
                bucket, sla = _bucket_for(score)
                sortable = score
            graded.append({
                "name": it.get("name") or "unnamed",
                "cvss": score,
                "severity": bucket,
                "recommended_sla": sla,
                "_sort": sortable,
            })

        graded.sort(key=lambda x: x["_sort"], reverse=True)
        for g in graded:
            g.pop("_sort", None)

        order = ["Critical", "High", "Medium", "Low", "None", "Unknown"]
        counts = {b: 0 for b in order}
        for g in graded:
            counts[g["severity"]] = counts.get(g["severity"], 0) + 1

        return {
            "total": len(graded),
            "counts": {k: v for k, v in counts.items() if v},
            "prioritized": graded,
            "note": "SLAs follow common CVSS v3 severity bands; adjust to your risk policy.",
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
# 6. Compliance checker (OWASP Top 10 - 2021)
# --------------------------------------------------------------------------- #

_OWASP_2021: list[dict] = [
    {"id": "A01", "name": "Broken Access Control",
     "keywords": ["access control", "idor", "privilege", "authorization", "forced browsing",
                  "directory traversal", "path traversal", "missing authz", "broken access"]},
    {"id": "A02", "name": "Cryptographic Failures",
     "keywords": ["crypto", "cryptographic", "plaintext", "weak cipher", "tls", "ssl",
                  "no https", "weak hash", "md5", "sha1", "unencrypted", "cleartext"]},
    {"id": "A03", "name": "Injection",
     "keywords": ["injection", "sql injection", "sqli", "xss", "cross-site scripting",
                  "command injection", "ldap injection", "nosql injection"]},
    {"id": "A04", "name": "Insecure Design",
     "keywords": ["insecure design", "threat model", "missing control", "business logic",
                  "design flaw"]},
    {"id": "A05", "name": "Security Misconfiguration",
     "keywords": ["misconfiguration", "default password", "default credential", "verbose error",
                  "open s3", "exposed admin", "directory listing", "unnecessary feature",
                  "security header", "cors"]},
    {"id": "A06", "name": "Vulnerable and Outdated Components",
     "keywords": ["outdated", "vulnerable component", "old version", "unpatched", "eol",
                  "end of life", "known cve", "dependency"]},
    {"id": "A07", "name": "Identification and Authentication Failures",
     "keywords": ["authentication", "weak password", "brute force", "credential stuffing",
                  "session fixation", "mfa", "no rate limit", "weak login", "session id"]},
    {"id": "A08", "name": "Software and Data Integrity Failures",
     "keywords": ["integrity", "insecure deserialization", "deserialization", "unsigned update",
                  "supply chain", "ci/cd", "auto-update"]},
    {"id": "A09", "name": "Security Logging and Monitoring Failures",
     "keywords": ["logging", "no logs", "monitoring", "no alert", "audit", "log tampering",
                  "missing logging", "no detection"]},
    {"id": "A10", "name": "Server-Side Request Forgery",
     "keywords": ["ssrf", "server-side request forgery", "request forgery", "internal url fetch"]},
]


def compliance_checker(findings="") -> dict:
    """Map finding keywords to OWASP Top 10 (2021) and mark each category pass/fail.

    A category is 'fail' when at least one finding maps to it, otherwise 'pass'.
    """
    try:
        if isinstance(findings, list):
            tokens = [str(x).strip() for x in findings if str(x).strip()]
        else:
            tokens = [ln.strip() for ln in str(findings or "").splitlines() if ln.strip()]
        if not tokens:
            return {"error": "Provide one or more finding keywords (one per line or a list)."}

        low_tokens = [(t, t.lower()) for t in tokens]
        results = []
        failed = 0
        unmatched = list(tokens)
        unmatched_set = set(tokens)

        for cat in _OWASP_2021:
            hits = []
            for original, low in low_tokens:
                if any(kw in low for kw in cat["keywords"]):
                    hits.append(original)
                    unmatched_set.discard(original)
            status = "fail" if hits else "pass"
            if hits:
                failed += 1
            results.append({
                "id": cat["id"],
                "category": f"{cat['id']}:2021 - {cat['name']}",
                "status": status,
                "matched_findings": hits,
            })

        unmatched = [t for t in unmatched if t in unmatched_set]
        return {
            "standard": "OWASP Top 10 - 2021",
            "categories_total": len(_OWASP_2021),
            "categories_failed": failed,
            "categories_passed": len(_OWASP_2021) - failed,
            "results": results,
            "unmapped_findings": unmatched,
            "note": "'pass' only means no supplied finding mapped to that category — "
                    "it is not a guarantee of compliance.",
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
# 7. Password policy generator
# --------------------------------------------------------------------------- #

def password_policy_generator(org_size: str = "medium",
                              sensitivity: str = "medium") -> dict:
    """Recommend a password / authentication policy from org size + data sensitivity."""
    try:
        size = (org_size or "medium").strip().lower()
        if size not in {"small", "medium", "large", "enterprise"}:
            size = "medium"
        sens = (sensitivity or "medium").strip().lower()
        if sens not in {"low", "medium", "high"}:
            sens = "medium"

        # Base on sensitivity, then nudge by org size.
        base = {
            "low":    {"min_length": 10, "history": 5,  "max_age_days": 365,
                       "lockout_threshold": 10, "mfa": "recommended"},
            "medium": {"min_length": 12, "history": 10, "max_age_days": 180,
                       "lockout_threshold": 5,  "mfa": "required for remote/admin access"},
            "high":   {"min_length": 16, "history": 24, "max_age_days": 90,
                       "lockout_threshold": 5,  "mfa": "required for all access"},
        }[sens]
        policy = dict(base)

        if size in {"large", "enterprise"} and sens != "low":
            policy["mfa"] = "required for all access"
        if size == "enterprise":
            policy["min_length"] = max(policy["min_length"], 14)

        complexity = ("Require at least 3 of 4 character classes (upper, lower, digit, symbol)"
                      if sens != "high" else
                      "Require all 4 character classes (upper, lower, digit, symbol)")

        recommendations = [
            f"Minimum length: {policy['min_length']} characters "
            "(prefer long passphrases over forced complexity).",
            complexity + ".",
            "Screen new passwords against a breached-password list (e.g. HaveIBeenPwned) "
            "and a common-password dictionary.",
            f"Account lockout after {policy['lockout_threshold']} failed attempts; "
            "use exponential backoff or temporary lockout.",
            f"Multi-factor authentication: {policy['mfa']}.",
            f"Remember the last {policy['history']} passwords to prevent reuse.",
            ("Avoid periodic forced rotation; rotate only on suspicion of compromise."
             if sens != "high" else
             f"Rotate passwords every {policy['max_age_days']} days for high-sensitivity systems."),
            "Store passwords with a slow, salted hash (Argon2id, scrypt, or bcrypt). "
            "Never store or log plaintext.",
        ]

        return {
            "org_size": size,
            "data_sensitivity": sens,
            "policy": policy,
            "complexity_rule": complexity,
            "recommendations": recommendations,
            "aligned_with": "NIST SP 800-63B guidance",
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
# Tool specs
# --------------------------------------------------------------------------- #

SPECS = [
    {
        "name": "mitre_attack_mapper",
        "label": "ATT&CK Mapper",
        "description": "Map a finding or keyword to likely MITRE ATT&CK tactics and techniques.",
        "category": "Defensive / Blue-team",
        "tier": "green",
        "inputs": [
            {"key": "finding", "label": "Finding / keyword", "type": "textarea",
             "placeholder": "e.g. repeated failed SSH logins from one IP"},
            {"key": "max_results", "label": "Max techniques", "type": "number",
             "placeholder": "5"},
        ],
    },
    {
        "name": "sigma_rule_generator",
        "label": "Sigma Rule Generator",
        "description": "Generate a valid Sigma detection rule from a single field=value condition.",
        "category": "Defensive / Blue-team",
        "tier": "green",
        "inputs": [
            {"key": "title", "label": "Rule title", "type": "text",
             "placeholder": "Suspicious Admin Logon"},
            {"key": "logsource", "label": "Log source", "type": "text",
             "placeholder": "windows/security"},
            {"key": "field", "label": "Field", "type": "text", "placeholder": "EventID"},
            {"key": "value", "label": "Value", "type": "text", "placeholder": "4625"},
            {"key": "level", "label": "Level", "type": "select",
             "options": ["informational", "low", "medium", "high", "critical"]},
            {"key": "description", "label": "Description (optional)", "type": "textarea",
             "placeholder": "Detects failed logon events"},
        ],
    },
    {
        "name": "firewall_rule_generator",
        "label": "Firewall Rule Generator",
        "description": "Emit copy-paste block rules for IPs/CIDRs on common firewall platforms.",
        "category": "Defensive / Blue-team",
        "tier": "green",
        "inputs": [
            {"key": "ips", "label": "IPs / CIDRs", "type": "textarea",
             "placeholder": "203.0.113.5, 198.51.100.0/24"},
            {"key": "platform", "label": "Platform", "type": "select",
             "options": ["iptables", "ufw", "nftables", "cisco-asa", "pf", "windows"]},
        ],
    },
    {
        "name": "incident_report_generator",
        "label": "Incident Report",
        "description": "Draft a structured Markdown incident report from a few fields.",
        "category": "Defensive / Blue-team",
        "tier": "green",
        "inputs": [
            {"key": "title", "label": "Title", "type": "text",
             "placeholder": "Unauthorized access to web server"},
            {"key": "severity", "label": "Severity", "type": "select",
             "options": ["informational", "low", "medium", "high", "critical"]},
            {"key": "summary", "label": "Summary", "type": "textarea",
             "placeholder": "What happened, in a sentence or two"},
            {"key": "affected", "label": "Affected assets (one per line)", "type": "textarea",
             "placeholder": "web-prod-01\napp database"},
            {"key": "timeline", "label": "Timeline (one entry per line)", "type": "textarea",
             "placeholder": "09:14 UTC - first failed login\n09:31 UTC - account locked"},
        ],
    },
    {
        "name": "vuln_prioritizer",
        "label": "Vulnerability Prioritizer",
        "description": "Sort vulnerabilities by CVSS and bucket them with a recommended SLA.",
        "category": "Defensive / Blue-team",
        "tier": "green",
        "inputs": [
            {"key": "vulnerabilities", "label": "Vulnerabilities (name + CVSS per line)",
             "type": "textarea",
             "placeholder": "CVE-2024-3094 10.0\nCVE-2023-1234 5.4\nOutdated OpenSSL 7.5"},
        ],
    },
    {
        "name": "compliance_checker",
        "label": "OWASP Top 10 Checker",
        "description": "Map finding keywords to OWASP Top 10 (2021) categories with pass/fail.",
        "category": "Defensive / Blue-team",
        "tier": "green",
        "inputs": [
            {"key": "findings", "label": "Findings (one per line)", "type": "textarea",
             "placeholder": "SQL injection in search\nNo HTTPS\nDefault admin password"},
        ],
    },
    {
        "name": "password_policy_generator",
        "label": "Password Policy",
        "description": "Recommend a password/auth policy from org size and data sensitivity.",
        "category": "Defensive / Blue-team",
        "tier": "green",
        "inputs": [
            {"key": "org_size", "label": "Organization size", "type": "select",
             "options": ["small", "medium", "large", "enterprise"]},
            {"key": "sensitivity", "label": "Data sensitivity", "type": "select",
             "options": ["low", "medium", "high"]},
        ],
    },
]
