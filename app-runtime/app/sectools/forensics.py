"""Forensics & Analysis tools for Talos (defensive security assistant).

Self-contained, dependency-light helpers exposed to the UI/AI as tools. Every
public function takes keyword arguments, never raises to the caller (failures are
returned as {"error": ...}), and returns a JSON-serializable dict.

Only Python stdlib + httpx are used here. All tools are non-destructive and
analysis-only: classify a hash, identify a file by magic bytes, measure entropy,
pull printable strings, parse email headers / web + auth logs, test a JWT against
a tiny built-in list of common secrets (educational), reverse a hash via a public
lookup API, and build a chronological timeline.
"""
from __future__ import annotations

import base64
import binascii
import datetime as _dt
import hashlib
import hmac
import json
import math
import re
from collections import Counter

import httpx

# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #

_MAX_BYTES = 1_000_000  # cap decoded blobs at ~1 MB so huge pastes can't choke us


def _to_bytes(blob: str) -> tuple[bytes | None, str | None]:
    """Decode a user-supplied blob that may be hex or base64.

    Returns (data, error). Whitespace and common 0x/\\x prefixes are tolerated.
    Hex is tried first (stricter charset), then base64.
    """
    if blob is None:
        return None, "No data provided."
    s = blob.strip()
    if not s:
        return None, "No data provided."

    # Normalise common hex adornments.
    cleaned = re.sub(r"(?i)\b0x", "", s)
    cleaned = cleaned.replace("\\x", "")
    hex_candidate = re.sub(r"[\s:,-]", "", cleaned)

    if re.fullmatch(r"(?i)[0-9a-f]+", hex_candidate) and len(hex_candidate) % 2 == 0:
        try:
            return binascii.unhexlify(hex_candidate)[:_MAX_BYTES], None
        except (binascii.Error, ValueError):
            pass

    # Base64 (also tolerate url-safe and missing padding).
    b64 = re.sub(r"\s", "", s)
    if re.fullmatch(r"[A-Za-z0-9+/_=-]+", b64):
        padded = b64 + "=" * (-len(b64) % 4)
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                data = decoder(padded, validate=False)
                if data:
                    return data[:_MAX_BYTES], None
            except (binascii.Error, ValueError):
                continue

    return None, "Could not decode input as hex or base64."


def _shannon_entropy(data: bytes) -> float:
    """Shannon entropy in bits/byte (0.0 - 8.0)."""
    if not data:
        return 0.0
    counts = Counter(data)
    n = len(data)
    ent = -sum((c / n) * math.log2(c / n) for c in counts.values())
    return ent + 0.0  # normalise -0.0 -> 0.0


# --------------------------------------------------------------------------- #
# 1. hash_identifier
# --------------------------------------------------------------------------- #

# (regex, length, [candidate types], note)
_HASH_LEN_MAP = {
    32: ["MD5", "NTLM", "MD4", "LM (half)"],
    40: ["SHA-1", "RIPEMD-160", "MySQL5.x (SHA1 of SHA1)"],
    56: ["SHA-224", "SHA3-224"],
    64: ["SHA-256", "SHA3-256", "BLAKE2s", "Keccak-256"],
    96: ["SHA-384", "SHA3-384"],
    128: ["SHA-512", "SHA3-512", "BLAKE2b", "Whirlpool"],
    16: ["MySQL 3.23 (old)", "CRC (hex)"],
}


def hash_identifier(hash_value: str = "") -> dict:
    """Classify a hash string by its length, charset and known prefixes."""
    try:
        h = (hash_value or "").strip()
        if not h:
            return {"error": "No hash provided."}

        # Prefixed / structured formats take priority — they're unambiguous.
        prefixed = [
            (r"^\$2[abxy]\$\d{2}\$[./A-Za-z0-9]{53}$", "bcrypt"),
            (r"^\$2[abxy]\$", "bcrypt (truncated/partial)"),
            (r"^\$argon2(id|i|d)\$", "Argon2"),
            (r"^\$scrypt\$", "scrypt"),
            (r"^\$y\$", "yescrypt (crypt)"),
            (r"^\$6\$", "sha512crypt (Unix crypt $6$)"),
            (r"^\$5\$", "sha256crypt (Unix crypt $5$)"),
            (r"^\$1\$", "md5crypt (Unix crypt $1$)"),
            (r"^\$P\$|^\$H\$", "phpass (WordPress/phpBB)"),
            (r"^\{SSHA\}", "SSHA (salted SHA-1, LDAP)"),
            (r"^\{SHA\}", "SHA-1 (Base64, LDAP)"),
            (r"^[0-9a-fA-F]{32}:[0-9a-fA-F]{32}$", "NTLMv2 / salted MD5 (hash:salt)"),
            (r"^sha256\$", "Django PBKDF2-SHA256"),
            (r"^pbkdf2_sha256\$", "Django PBKDF2-SHA256"),
        ]
        for pattern, name in prefixed:
            if re.match(pattern, h):
                return {
                    "input": h[:80],
                    "length": len(h),
                    "format": "structured",
                    "likely_types": [name],
                    "confidence": "high",
                    "note": "Matched a known hash/crypt prefix.",
                }

        is_hex = bool(re.fullmatch(r"(?i)[0-9a-f]+", h))
        n = len(h)

        if is_hex and n in _HASH_LEN_MAP:
            candidates = list(_HASH_LEN_MAP[n])
            note = "Hex digest matched by length; multiple algorithms share this length."
            if n == 32:
                note = ("32 hex chars: most often MD5. NTLM is identical in length "
                        "(uppercase hex is a weak hint toward NTLM/Windows).")
            return {
                "input": h[:80],
                "length": n,
                "format": "hex",
                "likely_types": candidates,
                "confidence": "medium",
                "note": note,
            }

        if is_hex:
            return {
                "input": h[:80],
                "length": n,
                "format": "hex",
                "likely_types": ["Unknown hex digest"],
                "confidence": "low",
                "note": f"{n} hex chars does not match a common digest length.",
            }

        # Non-hex: could be Base64-encoded digest.
        if re.fullmatch(r"[A-Za-z0-9+/=_-]+", h):
            approx = (len(h.rstrip("=")) * 6) // 8
            return {
                "input": h[:80],
                "length": n,
                "format": "base64-ish",
                "likely_types": [f"Base64-encoded digest (~{approx} raw bytes)"],
                "confidence": "low",
                "note": "Looks Base64-encoded rather than hex.",
            }

        return {
            "input": h[:80],
            "length": n,
            "format": "unknown",
            "likely_types": [],
            "confidence": "none",
            "note": "Charset is neither pure hex nor Base64; not a recognised hash.",
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
# 2. magic_bytes_id
# --------------------------------------------------------------------------- #

# (signature bytes, offset, label, extension)
_SIGNATURES: list[tuple[bytes, int, str, str]] = [
    (b"\x89PNG\r\n\x1a\n", 0, "PNG image", "png"),
    (b"\xff\xd8\xff", 0, "JPEG image", "jpg"),
    (b"GIF87a", 0, "GIF image (87a)", "gif"),
    (b"GIF89a", 0, "GIF image (89a)", "gif"),
    (b"BM", 0, "BMP image", "bmp"),
    (b"\x00\x00\x01\x00", 0, "ICO icon", "ico"),
    (b"%PDF-", 0, "PDF document", "pdf"),
    (b"PK\x03\x04", 0, "ZIP archive (or DOCX/XLSX/PPTX/JAR/APK)", "zip"),
    (b"PK\x05\x06", 0, "ZIP archive (empty)", "zip"),
    (b"PK\x07\x08", 0, "ZIP archive (spanned)", "zip"),
    (b"Rar!\x1a\x07\x00", 0, "RAR archive (v1.5+)", "rar"),
    (b"Rar!\x1a\x07\x01\x00", 0, "RAR archive (v5)", "rar"),
    (b"\x1f\x8b", 0, "GZIP archive", "gz"),
    (b"BZh", 0, "BZIP2 archive", "bz2"),
    (b"\xfd7zXZ\x00", 0, "XZ archive", "xz"),
    (b"7z\xbc\xaf\x27\x1c", 0, "7-Zip archive", "7z"),
    (b"\x7fELF", 0, "ELF executable (Linux/Unix)", "elf"),
    (b"MZ", 0, "PE executable / DLL (Windows)", "exe"),
    (b"\xca\xfe\xba\xbe", 0, "Java class / Mach-O fat binary", "class"),
    (b"\xfe\xed\xfa\xce", 0, "Mach-O binary (32-bit)", "macho"),
    (b"\xfe\xed\xfa\xcf", 0, "Mach-O binary (64-bit)", "macho"),
    (b"\xcf\xfa\xed\xfe", 0, "Mach-O binary (64-bit, LE)", "macho"),
    (b"OggS", 0, "OGG container", "ogg"),
    (b"ID3", 0, "MP3 audio (ID3)", "mp3"),
    (b"\xff\xfb", 0, "MP3 audio (no ID3)", "mp3"),
    (b"fLaC", 0, "FLAC audio", "flac"),
    (b"RIFF", 0, "RIFF container (WAV/AVI/WEBP)", "riff"),
    (b"\x1aE\xdf\xa3", 0, "Matroska/WebM (EBML)", "mkv"),
    (b"ftyp", 4, "ISO Media (MP4/MOV/HEIC)", "mp4"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", 0, "MS Office legacy (OLE2: doc/xls/ppt)", "ole"),
    (b"<?xml", 0, "XML document", "xml"),
    (b"<!DOCTYPE html", 0, "HTML document", "html"),
    (b"<html", 0, "HTML document", "html"),
    (b"-----BEGIN ", 0, "PEM / ASCII-armored key or certificate", "pem"),
    (b"SQLite format 3\x00", 0, "SQLite 3 database", "sqlite"),
    (b"\xed\xab\xee\xdb", 0, "RPM package", "rpm"),
    (b"!<arch>\n", 0, "Unix ar archive / .deb", "ar"),
    (b"\xca\xfe\xd0\x0d", 0, "Java pack200", "pack"),
    (b"Cr24", 0, "Chrome extension (CRX)", "crx"),
    (b"wOFF", 0, "WOFF font", "woff"),
    (b"wOF2", 0, "WOFF2 font", "woff2"),
    (b"\x00\x01\x00\x00\x00", 0, "TrueType font", "ttf"),
    (b"OTTO", 0, "OpenType font", "otf"),
]


def magic_bytes_id(data: str = "") -> dict:
    """Identify a file type from the hex or base64 of its leading bytes."""
    try:
        raw, err = _to_bytes(data)
        if err:
            return {"error": err}
        head = raw[:64]
        hits = []
        for sig, off, label, ext in _SIGNATURES:
            if len(head) >= off + len(sig) and head[off:off + len(sig)] == sig:
                hits.append({"type": label, "extension": ext, "offset": off})

        # Refine the generic RIFF container if we can see the form type.
        for h in hits:
            if h["extension"] == "riff" and len(head) >= 12:
                form = head[8:12]
                mapping = {b"WAVE": ("WAV audio", "wav"),
                           b"AVI ": ("AVI video", "avi"),
                           b"WEBP": ("WebP image", "webp")}
                if form in mapping:
                    h["type"], h["extension"] = mapping[form]

        printable = "".join(chr(b) if 32 <= b < 127 else "." for b in head[:24])
        result = {
            "bytes_examined": len(head),
            "hex_preview": head[:16].hex(" "),
            "ascii_preview": printable,
            "matches": hits,
        }
        if not hits:
            result["note"] = "No known signature matched the leading bytes."
        return result
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
# 3. entropy_analyzer
# --------------------------------------------------------------------------- #

def entropy_analyzer(data: str = "", treat_as: str = "auto") -> dict:
    """Compute Shannon entropy (0-8 bits/byte) of a blob and interpret it.

    treat_as: 'auto' (decode hex/base64, else fall back to UTF-8 text),
              'text' (always use raw UTF-8 bytes), or 'binary' (require hex/base64).
    """
    try:
        if not (data or "").strip():
            return {"error": "No data provided."}

        mode = (treat_as or "auto").lower()
        raw: bytes
        source: str
        if mode == "text":
            raw, source = data.encode("utf-8", "replace"), "utf-8 text"
        else:
            decoded, err = _to_bytes(data)
            if decoded is not None:
                raw, source = decoded, "decoded hex/base64"
            elif mode == "binary":
                return {"error": err or "Could not decode as hex/base64."}
            else:
                raw, source = data.encode("utf-8", "replace"), "utf-8 text"

        if not raw:
            return {"error": "Decoded data is empty."}

        ent = _shannon_entropy(raw)
        ratio = ent / 8.0
        if ent < 1.0:
            interp = "Extremely low — highly repetitive (padding, nulls, or single value)."
        elif ent < 3.5:
            interp = "Low — structured/plain text or simple data."
        elif ent < 6.0:
            interp = "Medium — typical source code, text, or lightly compressed data."
        elif ent < 7.5:
            interp = "High — compressed, encoded, or binary data."
        else:
            interp = "Very high — likely encrypted or packed (random-looking)."

        return {
            "source": source,
            "byte_count": len(raw),
            "entropy_bits_per_byte": round(ent, 4),
            "entropy_ratio": round(ratio, 4),
            "scale": "0 (uniform) .. 8 (random)",
            "interpretation": interp,
            "likely_encrypted_or_packed": ent >= 7.5,
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
# 4. strings_extractor
# --------------------------------------------------------------------------- #

def strings_extractor(data: str = "", min_length: int = 4, limit: int = 200) -> dict:
    """Extract printable ASCII runs (>= min_length) from a hex/base64 blob."""
    try:
        raw, err = _to_bytes(data)
        if err:
            return {"error": err}
        try:
            min_len = max(1, min(64, int(min_length)))
        except (TypeError, ValueError):
            min_len = 4
        try:
            cap = max(1, min(2000, int(limit)))
        except (TypeError, ValueError):
            cap = 200

        pattern = re.compile(rb"[\x20-\x7e]{%d,}" % min_len)
        found = [m.group().decode("ascii", "ignore") for m in pattern.finditer(raw)]
        total = len(found)
        truncated = total > cap
        out = found[:cap]
        return {
            "byte_count": len(raw),
            "min_length": min_len,
            "total_strings": total,
            "returned": len(out),
            "truncated": truncated,
            "strings": out,
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
# 5. email_header_analyzer
# --------------------------------------------------------------------------- #

def _unfold_headers(raw: str) -> list[tuple[str, str]]:
    """Unfold RFC 5322 continuation lines into (name, value) pairs."""
    headers: list[tuple[str, str]] = []
    for line in raw.replace("\r\n", "\n").split("\n"):
        if not line:
            continue
        if line[0] in " \t" and headers:
            name, val = headers[-1]
            headers[-1] = (name, val + " " + line.strip())
        elif ":" in line:
            name, _, val = line.partition(":")
            headers.append((name.strip(), val.strip()))
    return headers


def _extract_addr(value: str) -> str:
    m = re.search(r"<([^>]+)>", value or "")
    if m:
        return m.group(1).strip().lower()
    m = re.search(r"[\w.+-]+@[\w.-]+\.\w+", value or "")
    return m.group(0).strip().lower() if m else ""


def email_header_analyzer(headers: str = "") -> dict:
    """Parse raw email headers: Received hops, From/Return-Path alignment, auth."""
    try:
        if not (headers or "").strip():
            return {"error": "No headers provided."}

        pairs = _unfold_headers(headers)
        if not pairs:
            return {"error": "Could not parse any 'Name: value' headers."}

        by_name: dict[str, list[str]] = {}
        for name, val in pairs:
            by_name.setdefault(name.lower(), []).append(val)

        def first(name: str) -> str:
            vals = by_name.get(name.lower(), [])
            return vals[0] if vals else ""

        # Received hops are listed newest-first; reverse to show delivery order.
        received = by_name.get("received", [])
        hops = []
        for i, hop in enumerate(reversed(received)):
            frm = re.search(r"from\s+([^\s;]+)", hop, re.I)
            by = re.search(r"by\s+([^\s;]+)", hop, re.I)
            ip = re.search(r"\[?(\d{1,3}(?:\.\d{1,3}){3})\]?", hop)
            hops.append({
                "hop": i + 1,
                "from": frm.group(1) if frm else None,
                "by": by.group(1) if by else None,
                "ip": ip.group(1) if ip else None,
            })

        from_addr = _extract_addr(first("from"))
        return_path = _extract_addr(first("return-path"))
        reply_to = _extract_addr(first("reply-to"))

        # Authentication-Results / individual auth headers.
        auth_blob = " ".join(
            by_name.get("authentication-results", [])
            + by_name.get("received-spf", [])
            + by_name.get("dkim-signature", [])
        ).lower()

        def _verdict(mech: str) -> str:
            m = re.search(rf"{mech}=(\w+)", auth_blob)
            return m.group(1) if m else "not found"

        spf = _verdict("spf")
        dkim = _verdict("dkim")
        dmarc = _verdict("dmarc")
        if dkim == "not found" and by_name.get("dkim-signature"):
            dkim = "present (unverified)"
        if spf == "not found" and by_name.get("received-spf"):
            m = re.match(r"\s*(\w+)", by_name["received-spf"][0])
            spf = m.group(1).lower() if m else "present"

        flags = []
        from_domain = from_addr.split("@")[-1] if "@" in from_addr else ""
        rp_domain = return_path.split("@")[-1] if "@" in return_path else ""
        if from_addr and return_path and from_domain != rp_domain:
            flags.append(f"From domain ({from_domain}) != Return-Path domain ({rp_domain}) — possible spoofing.")
        if from_addr and reply_to and reply_to.split("@")[-1] != from_domain:
            flags.append(f"Reply-To domain differs from From ({reply_to.split('@')[-1]}) — common in phishing.")
        for mech, verdict in (("SPF", spf), ("DKIM", dkim), ("DMARC", dmarc)):
            if verdict in ("fail", "softfail", "none", "permerror", "temperror"):
                flags.append(f"{mech} = {verdict}.")
        if not flags:
            flags.append("No obvious header-based red flags detected.")

        return {
            "from": from_addr or None,
            "return_path": return_path or None,
            "reply_to": reply_to or None,
            "subject": first("subject")[:200] or None,
            "message_id": first("message-id") or None,
            "date": first("date") or None,
            "hop_count": len(hops),
            "received_hops": hops[:15],
            "authentication": {"spf": spf, "dkim": dkim, "dmarc": dmarc},
            "red_flags": flags,
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
# 6. log_analyzer
# --------------------------------------------------------------------------- #

_IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
# Common Log Format-ish: ... "METHOD /path HTTP/x" status size
_ACCESS_RE = re.compile(
    r'"(?P<method>GET|POST|PUT|DELETE|HEAD|OPTIONS|PATCH|CONNECT|TRACE)\s+'
    r'(?P<path>[^"\s]+)[^"]*"\s+(?P<status>\d{3})'
)
_SUSPICIOUS_PATTERNS = [
    (re.compile(r"(\.\./|\.\.%2f|%2e%2e)", re.I), "path traversal"),
    (re.compile(r"(union\s+select|select.+from|or\s+1=1|sleep\(|information_schema)", re.I), "SQL injection"),
    (re.compile(r"(<script|onerror=|onload=|javascript:|%3cscript)", re.I), "XSS attempt"),
    (re.compile(r"(/etc/passwd|/bin/sh|cmd\.exe|powershell|wget\s|curl\s)", re.I), "command injection / LFI"),
    (re.compile(r"(\bwp-login|\bxmlrpc\.php|/phpmyadmin|/\.env|/\.git)", re.I), "sensitive path probe"),
    (re.compile(r"(sqlmap|nikto|nmap|masscan|acunetix|nessus|dirbuster|gobuster|hydra)", re.I), "scanner/tool UA"),
    (re.compile(r"(failed password|authentication failure|invalid user)", re.I), "auth failure"),
]


def log_analyzer(log_text: str = "", top_n: int = 10) -> dict:
    """Parse pasted Apache/Nginx access or auth logs into a quick triage summary."""
    try:
        if not (log_text or "").strip():
            return {"error": "No log lines provided."}
        try:
            top = max(1, min(50, int(top_n)))
        except (TypeError, ValueError):
            top = 10

        lines = [ln for ln in log_text.replace("\r\n", "\n").split("\n") if ln.strip()]
        ip_counter: Counter[str] = Counter()
        status_counter: Counter[str] = Counter()
        path_counter: Counter[str] = Counter()
        auth_fail_by_ip: Counter[str] = Counter()
        suspicious: list[dict] = []

        for ln in lines:
            ipm = _IP_RE.search(ln)
            ip = ipm.group(1) if ipm else None
            if ip:
                ip_counter[ip] += 1

            am = _ACCESS_RE.search(ln)
            if am:
                status_counter[am.group("status")] += 1
                path_counter[am.group("path")[:120]] += 1

            low = ln.lower()
            if "failed password" in low or "authentication failure" in low or "invalid user" in low:
                if ip:
                    auth_fail_by_ip[ip] += 1

            for rx, label in _SUSPICIOUS_PATTERNS:
                if rx.search(ln):
                    if len(suspicious) < 100:
                        suspicious.append({"type": label, "ip": ip, "sample": ln.strip()[:200]})
                    break

        susp_types = Counter(s["type"] for s in suspicious)
        return {
            "lines_total": len(lines),
            "unique_ips": len(ip_counter),
            "top_ips": [{"ip": ip, "count": c} for ip, c in ip_counter.most_common(top)],
            "status_codes": dict(status_counter.most_common()),
            "top_paths": [{"path": p, "count": c} for p, c in path_counter.most_common(top)],
            "auth_failures_by_ip": [{"ip": ip, "failures": c}
                                    for ip, c in auth_fail_by_ip.most_common(top)],
            "suspicious_count": len(suspicious),
            "suspicious_breakdown": dict(susp_types),
            "suspicious_samples": suspicious[:25],
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
# 7. jwt_weak_secret_check  (educational)
# --------------------------------------------------------------------------- #

_COMMON_JWT_SECRETS = [
    "secret", "secret123", "password", "123456", "admin", "changeme",
    "jwt", "jwtsecret", "your-256-bit-secret", "your_jwt_secret", "supersecret",
    "test", "key", "private", "mysecret", "qwerty", "letmein", "root",
    "token", "default",
]
_JWT_ALG_HASH = {"HS256": hashlib.sha256, "HS384": hashlib.sha384, "HS512": hashlib.sha512}


def _b64url_decode(seg: str) -> bytes:
    seg += "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg)


def jwt_weak_secret_check(token: str = "") -> dict:
    """Decode a JWT and test its HMAC signature against ~20 common weak secrets.

    Educational only: tries a tiny built-in wordlist against the *provided* token,
    never a brute force against a live service.
    """
    try:
        parts = (token or "").strip().split(".")
        if len(parts) != 3:
            return {"error": "Not a JWT — expected header.payload.signature."}
        h_b64, p_b64, sig_b64 = parts

        try:
            header = json.loads(_b64url_decode(h_b64))
            payload = json.loads(_b64url_decode(p_b64))
        except (binascii.Error, ValueError, json.JSONDecodeError) as e:
            return {"error": f"Could not decode JWT segments: {e}"}

        alg = str(header.get("alg", "")).upper()
        result = {
            "header": header,
            "payload": payload,
            "algorithm": alg,
            "secrets_tested": len(_COMMON_JWT_SECRETS),
        }

        if alg == "NONE":
            result["weak_secret_found"] = None
            result["vulnerability"] = "alg=none — signature is not verified at all (critical)."
            return result

        if alg not in _JWT_ALG_HASH:
            result["weak_secret_found"] = None
            result["note"] = (f"alg={alg or 'unknown'} is not HMAC; this educational "
                              "check only covers HS256/HS384/HS512.")
            return result

        try:
            expected_sig = _b64url_decode(sig_b64)
        except (binascii.Error, ValueError):
            return {"error": "Could not decode the signature segment."}

        signing_input = f"{h_b64}.{p_b64}".encode("ascii")
        digest = _JWT_ALG_HASH[alg]
        found = None
        for secret in _COMMON_JWT_SECRETS:
            calc = hmac.new(secret.encode(), signing_input, digest).digest()
            if hmac.compare_digest(calc, expected_sig):
                found = secret
                break

        result["weak_secret_found"] = found
        if found:
            result["vulnerability"] = (
                f"Token is signed with the trivial secret '{found}'. Anyone can forge "
                "tokens. Rotate to a long, random secret immediately.")
        else:
            result["note"] = "None of the common weak secrets matched (signature not cracked)."
        return result
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
# 8. hash_lookup  (online reverse via public API)
# --------------------------------------------------------------------------- #

async def hash_lookup(hash_value: str = "") -> dict:
    """Attempt to reverse a hash via public lookup services (best effort)."""
    try:
        h = (hash_value or "").strip().lower()
        if not h:
            return {"error": "No hash provided."}
        if not re.fullmatch(r"[0-9a-f]+", h):
            return {"error": "Provide a plain hex hash (e.g. an MD5/SHA-1 digest)."}
        if len(h) not in (32, 40, 64):
            return {"error": "Only MD5 (32), SHA-1 (40) or SHA-256 (64) hex hashes are supported."}

        async with httpx.AsyncClient(
            timeout=10, follow_redirects=True,
            headers={"User-Agent": "Talos-Security"},
        ) as client:
            # 1) md5decrypt-style plain endpoint (returns the plaintext or "not found").
            try:
                r = await client.get(f"https://md5decrypt.net/en/Api/api.php?hash={h}&hash_type={'md5' if len(h)==32 else 'sha1' if len(h)==40 else 'sha256'}&email=&code=")
                text = (r.text or "").strip()
                if r.status_code == 200 and text and "ERROR" not in text.upper() and len(text) < 200:
                    return {"hash": h, "found": True, "plaintext": text, "source": "md5decrypt.net"}
            except (httpx.HTTPError, ValueError):
                pass

            # 2) Nitrxgen MD5 DB (MD5 only).
            if len(h) == 32:
                try:
                    r = await client.get(f"https://www.nitrxgen.net/md5db/{h}")
                    text = (r.text or "").strip()
                    if r.status_code == 200 and text:
                        return {"hash": h, "found": True, "plaintext": text, "source": "nitrxgen.net"}
                except (httpx.HTTPError, ValueError):
                    pass

        return {
            "hash": h,
            "found": False,
            "note": "No plaintext found in the public lookup databases (or services unavailable).",
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "hash": (hash_value or "")[:80]}


# --------------------------------------------------------------------------- #
# 9. timeline_builder
# --------------------------------------------------------------------------- #

_TS_FORMATS = [
    "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%d/%b/%Y:%H:%M:%S %z", "%d/%b/%Y:%H:%M:%S",
    "%b %d %H:%M:%S", "%m/%d/%Y %H:%M:%S",
]


def _parse_ts(s: str) -> tuple[_dt.datetime | None, str | None]:
    s = s.strip()
    iso = s.replace("Z", "+00:00")
    try:
        return _dt.datetime.fromisoformat(iso), None
    except ValueError:
        pass
    for fmt in _TS_FORMATS:
        try:
            return _dt.datetime.strptime(s, fmt), None
        except ValueError:
            continue
    return None, s


def timeline_builder(events: str = "") -> dict:
    """Sort lines of 'iso_ts | event' into a chronological timeline.

    Accepts '|', tab, or ' - ' as the separator between timestamp and event.
    """
    try:
        if not (events or "").strip():
            return {"error": "No events provided."}

        parsed: list[tuple[_dt.datetime, str, str]] = []
        unparsed: list[str] = []
        for line in events.replace("\r\n", "\n").split("\n"):
            line = line.strip()
            if not line:
                continue
            if "|" in line:
                ts_part, _, ev = line.partition("|")
            elif "\t" in line:
                ts_part, _, ev = line.partition("\t")
            elif " - " in line:
                ts_part, _, ev = line.partition(" - ")
            else:
                m = re.match(r"(\S+[ T]\S+)\s+(.*)", line)
                if m:
                    ts_part, ev = m.group(1), m.group(2)
                else:
                    unparsed.append(line[:200])
                    continue
            dt, _err = _parse_ts(ts_part.strip())
            if dt is None:
                unparsed.append(line[:200])
            else:
                parsed.append((dt, ts_part.strip(), ev.strip()))

        # Sort timezone-aware and naive separately won't compare; normalise to naive UTC key.
        def _key(item: tuple[_dt.datetime, str, str]) -> _dt.datetime:
            d = item[0]
            if d.tzinfo is not None:
                return d.astimezone(_dt.timezone.utc).replace(tzinfo=None)
            return d

        parsed.sort(key=_key)

        timeline = [{"timestamp": ts, "iso": dt.isoformat(), "event": ev}
                    for dt, ts, ev in parsed]

        span = None
        if len(parsed) >= 2:
            delta = _key(parsed[-1]) - _key(parsed[0])
            span = {
                "start": parsed[0][0].isoformat(),
                "end": parsed[-1][0].isoformat(),
                "duration_seconds": delta.total_seconds(),
            }

        return {
            "event_count": len(timeline),
            "unparsed_count": len(unparsed),
            "span": span,
            "timeline": timeline,
            "unparsed": unparsed[:20],
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
# SPECS
# --------------------------------------------------------------------------- #

SPECS = [
    {
        "name": "hash_identifier",
        "label": "Hash Identifier",
        "description": "Classify a hash by length, charset and prefix to guess its algorithm.",
        "category": "Forensics & Analysis",
        "tier": "green",
        "inputs": [
            {"key": "hash_value", "label": "Hash", "type": "text",
             "placeholder": "5f4dcc3b5aa765d61d8327deb882cf99"},
        ],
    },
    {
        "name": "magic_bytes_id",
        "label": "Magic Bytes / File Type",
        "description": "Identify a file type from the hex or base64 of its first bytes.",
        "category": "Forensics & Analysis",
        "tier": "green",
        "inputs": [
            {"key": "data", "label": "Header bytes (hex or base64)", "type": "textarea",
             "placeholder": "89 50 4E 47 0D 0A 1A 0A   or   iVBORw0KGgo="},
        ],
    },
    {
        "name": "entropy_analyzer",
        "label": "Entropy Analyzer",
        "description": "Shannon entropy (0-8) of a blob to spot encrypted or packed data.",
        "category": "Forensics & Analysis",
        "tier": "green",
        "inputs": [
            {"key": "data", "label": "Data (hex, base64, or text)", "type": "textarea",
             "placeholder": "Paste hex/base64 bytes or raw text"},
            {"key": "treat_as", "label": "Interpret input as", "type": "select",
             "options": ["auto", "text", "binary"]},
        ],
    },
    {
        "name": "strings_extractor",
        "label": "Strings Extractor",
        "description": "Pull printable ASCII runs from a hex/base64 binary blob.",
        "category": "Forensics & Analysis",
        "tier": "green",
        "inputs": [
            {"key": "data", "label": "Binary blob (hex or base64)", "type": "textarea",
             "placeholder": "4D5A90000300...  or base64"},
            {"key": "min_length", "label": "Min run length", "type": "number",
             "placeholder": "4"},
            {"key": "limit", "label": "Max strings to return", "type": "number",
             "placeholder": "200"},
        ],
    },
    {
        "name": "email_header_analyzer",
        "label": "Email Header Analyzer",
        "description": "Parse raw email headers: Received hops, From/Return-Path mismatch, SPF/DKIM/DMARC.",
        "category": "Forensics & Analysis",
        "tier": "green",
        "inputs": [
            {"key": "headers", "label": "Raw email headers", "type": "textarea",
             "placeholder": "Paste full headers (Received:, From:, Authentication-Results: ...)"},
        ],
    },
    {
        "name": "log_analyzer",
        "label": "Log Analyzer",
        "description": "Triage pasted Apache/Nginx/auth logs: top IPs, status codes, suspicious patterns.",
        "category": "Forensics & Analysis",
        "tier": "green",
        "inputs": [
            {"key": "log_text", "label": "Log lines", "type": "textarea",
             "placeholder": "Paste access.log / auth.log lines"},
            {"key": "top_n", "label": "Top N", "type": "number", "placeholder": "10"},
        ],
    },
    {
        "name": "jwt_weak_secret_check",
        "label": "JWT Weak-Secret Check",
        "description": "Decode a JWT and test its HMAC signature against ~20 common weak secrets (educational).",
        "category": "Forensics & Analysis",
        "tier": "edu",
        "inputs": [
            {"key": "token", "label": "JWT", "type": "textarea",
             "placeholder": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.<sig>"},
        ],
    },
    {
        "name": "hash_lookup",
        "label": "Hash Reverse Lookup",
        "description": "Try to reverse an MD5/SHA-1/SHA-256 hash via public lookup databases.",
        "category": "Forensics & Analysis",
        "tier": "green",
        "inputs": [
            {"key": "hash_value", "label": "Hex hash", "type": "text",
             "placeholder": "5f4dcc3b5aa765d61d8327deb882cf99"},
        ],
    },
    {
        "name": "timeline_builder",
        "label": "Timeline Builder",
        "description": "Sort 'iso_ts | event' lines into a chronological incident timeline.",
        "category": "Forensics & Analysis",
        "tier": "green",
        "inputs": [
            {"key": "events", "label": "Events (one per line: ts | event)", "type": "textarea",
             "placeholder": "2026-05-31T10:00:00Z | login failed\n2026-05-31T10:01:30Z | account locked"},
        ],
    },
]
