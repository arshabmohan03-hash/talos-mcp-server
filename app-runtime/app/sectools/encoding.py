"""Crypto & Encoding tools for Talos (defensive security assistant).

Self-contained module. Each public function is a tool whose name matches its
SPECS entry. Functions take keyword arguments and ALWAYS return a
JSON-serializable dict. They never raise to the caller: every failure path
returns {"error": "..."}.

Dependencies: Python stdlib + cryptography (Fernet). No network is used here;
everything is local transformation/analysis, which keeps these fast and safe.
"""
from __future__ import annotations

import base64
import binascii
import codecs
import hashlib
import hmac
import math
import re
import struct
import time
import urllib.parse
import html as html_mod

from cryptography.fernet import Fernet, InvalidToken

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_MAX_OUT = 20000  # cap any single returned string to keep payloads sane


def _truncate(s: str, limit: int = _MAX_OUT) -> str:
    if s is None:
        return s
    if len(s) > limit:
        return s[:limit] + f"\n...[truncated, {len(s) - limit} more chars]"
    return s


def _as_bytes(text: str) -> bytes:
    return (text or "").encode("utf-8", errors="replace")


# International Morse code table (letters, digits, common punctuation).
_MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "'": ".----.", "!": "-.-.--",
    "/": "-..-.", "(": "-.--.", ")": "-.--.-", "&": ".-...", ":": "---...",
    ";": "-.-.-.", "=": "-...-", "+": ".-.-.", "-": "-....-", "_": "..--.-",
    '"': ".-..-.", "$": "...-..-", "@": ".--.-.",
}
_MORSE_REV = {v: k for k, v in _MORSE.items()}

# Letter frequency in typical English text (percent), used for scoring/labels.
_ENGLISH_FREQ = {
    "a": 8.2, "b": 1.5, "c": 2.8, "d": 4.3, "e": 12.7, "f": 2.2, "g": 2.0,
    "h": 6.1, "i": 7.0, "j": 0.15, "k": 0.77, "l": 4.0, "m": 2.4, "n": 6.7,
    "o": 7.5, "p": 1.9, "q": 0.095, "r": 6.0, "s": 6.3, "t": 9.1, "u": 2.8,
    "v": 0.98, "w": 2.4, "x": 0.15, "y": 2.0, "z": 0.074,
}


def _printable_ratio(b: bytes) -> float:
    """Fraction of bytes that are printable ASCII / common whitespace."""
    if not b:
        return 0.0
    good = sum(1 for c in b if 32 <= c <= 126 or c in (9, 10, 13))
    return good / len(b)


def _english_score(text: str) -> float:
    """Lower is more English-like (chi-squared vs. expected letter freq)."""
    letters = [c for c in text.lower() if "a" <= c <= "z"]
    total = len(letters)
    if total == 0:
        return 1e9
    counts = {}
    for c in letters:
        counts[c] = counts.get(c, 0) + 1
    chi = 0.0
    for c, exp_pct in _ENGLISH_FREQ.items():
        expected = total * exp_pct / 100.0
        observed = counts.get(c, 0)
        if expected > 0:
            chi += (observed - expected) ** 2 / expected
    # Penalise text with lots of non-letter, non-space junk.
    junk = sum(1 for c in text if not (c.isalnum() or c.isspace() or c in ".,!?'\"-:;()"))
    chi += junk * 5
    return chi


# ---------------------------------------------------------------------------
# Encoders / decoders
# ---------------------------------------------------------------------------

def multi_encoder(text: str = "", scheme: str = "base64") -> dict:
    """Encode text using a chosen scheme."""
    try:
        scheme = (scheme or "base64").strip().lower()
        raw = _as_bytes(text)
        if scheme in ("base64", "b64"):
            out = base64.b64encode(raw).decode("ascii")
        elif scheme in ("base32", "b32"):
            out = base64.b32encode(raw).decode("ascii")
        elif scheme in ("base85", "b85", "ascii85"):
            out = base64.b85encode(raw).decode("ascii")
        elif scheme == "hex":
            out = raw.hex()
        elif scheme in ("url", "urlencode", "percent"):
            out = urllib.parse.quote(text or "", safe="")
        elif scheme in ("html", "htmlentities"):
            out = html_mod.escape(text or "", quote=True)
        elif scheme == "rot13":
            out = codecs.encode(text or "", "rot_13")
        elif scheme in ("binary", "bin"):
            out = " ".join(format(c, "08b") for c in raw)
        elif scheme == "morse":
            parts = []
            for ch in (text or "").upper():
                if ch == " ":
                    parts.append("/")
                elif ch in _MORSE:
                    parts.append(_MORSE[ch])
                # silently drop unsupported chars
            out = " ".join(parts)
        else:
            return {"error": f"Unknown scheme '{scheme}'. Use base64/base32/base85/"
                             "hex/url/html/rot13/binary/morse."}
        return {"scheme": scheme, "input_len": len(text or ""), "output": _truncate(out)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"Encoding failed: {e}"}


def _try_b64(s: str):
    s2 = s.strip()
    if len(s2) < 4 or not re.fullmatch(r"[A-Za-z0-9+/=\s]+", s2):
        return None
    compact = re.sub(r"\s+", "", s2)
    if len(compact) % 4 != 0:
        return None
    try:
        dec = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        return None
    if _printable_ratio(dec) < 0.85:
        return None
    return dec.decode("utf-8", errors="replace")


def _try_b32(s: str):
    compact = re.sub(r"\s+", "", s.strip()).upper()
    if len(compact) < 8 or not re.fullmatch(r"[A-Z2-7=]+", compact):
        return None
    if len(compact) % 8 != 0:
        return None
    try:
        dec = base64.b32decode(compact)
    except (binascii.Error, ValueError):
        return None
    if _printable_ratio(dec) < 0.85:
        return None
    return dec.decode("utf-8", errors="replace")


def _try_b85(s: str):
    compact = re.sub(r"\s+", "", s.strip())
    if len(compact) < 4:
        return None
    try:
        dec = base64.b85decode(compact)
    except (binascii.Error, ValueError):
        return None
    if _printable_ratio(dec) < 0.9:
        return None
    return dec.decode("utf-8", errors="replace")


def _try_hex(s: str):
    compact = re.sub(r"[\s:]+", "", s.strip())
    if len(compact) < 2 or len(compact) % 2 != 0:
        return None
    if not re.fullmatch(r"[0-9a-fA-F]+", compact):
        return None
    try:
        dec = bytes.fromhex(compact)
    except ValueError:
        return None
    if _printable_ratio(dec) < 0.85:
        return None
    return dec.decode("utf-8", errors="replace")


def _try_binary(s: str):
    bits = re.sub(r"[\s]+", "", s.strip())
    if not re.fullmatch(r"[01]+", bits) or len(bits) < 8 or len(bits) % 8 != 0:
        return None
    try:
        dec = bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))
    except ValueError:
        return None
    if _printable_ratio(dec) < 0.85:
        return None
    return dec.decode("utf-8", errors="replace")


def _try_url(s: str):
    if "%" not in s:
        return None
    out = urllib.parse.unquote(s)
    if out == s:
        return None
    return out


def _try_html(s: str):
    if "&" not in s or ";" not in s:
        return None
    out = html_mod.unescape(s)
    if out == s:
        return None
    return out


def _try_morse(s: str):
    if not re.fullmatch(r"[.\-/\s]+", s.strip()) or "." not in s and "-" not in s:
        return None
    words = re.split(r"\s*/\s*|\s{2,}", s.strip())
    decoded_words = []
    for w in words:
        letters = []
        for sym in w.split():
            if sym in _MORSE_REV:
                letters.append(_MORSE_REV[sym])
            else:
                return None
        decoded_words.append("".join(letters))
    out = " ".join(decoded_words).strip()
    return out or None


def _try_rot13(s: str):
    out = codecs.encode(s, "rot_13")
    if out == s:
        return None
    return out


def multi_decoder(text: str = "") -> dict:
    """Auto-detect and decode: tries base64/32/85, hex, binary, url, html, morse, rot13."""
    try:
        text = text or ""
        if not text.strip():
            return {"error": "Provide some text to decode."}
        candidates = []
        for name, fn in (
            ("base64", _try_b64), ("base32", _try_b32), ("base85", _try_b85),
            ("hex", _try_hex), ("binary", _try_binary), ("url", _try_url),
            ("html", _try_html), ("morse", _try_morse), ("rot13", _try_rot13),
        ):
            try:
                res = fn(text)
            except Exception:  # noqa: BLE001
                res = None
            if res is not None and res != "":
                candidates.append({"scheme": name, "decoded": _truncate(res, 4000)})
        if not candidates:
            return {"input": _truncate(text, 500), "results": [],
                    "note": "No scheme produced a confident decode."}
        return {"input_len": len(text), "count": len(candidates), "results": candidates}
    except Exception as e:  # noqa: BLE001
        return {"error": f"Decode failed: {e}"}


# ---------------------------------------------------------------------------
# Classic ciphers
# ---------------------------------------------------------------------------

def _shift_text(text: str, shift: int) -> str:
    out = []
    for ch in text:
        if "a" <= ch <= "z":
            out.append(chr((ord(ch) - 97 + shift) % 26 + 97))
        elif "A" <= ch <= "Z":
            out.append(chr((ord(ch) - 65 + shift) % 26 + 65))
        else:
            out.append(ch)
    return "".join(out)


def caesar_cipher(text: str = "", shift: int = 3, mode: str = "encrypt") -> dict:
    """Caesar shift cipher (encrypt/decrypt by N positions)."""
    try:
        try:
            shift = int(shift)
        except (TypeError, ValueError):
            return {"error": "shift must be an integer."}
        mode = (mode or "encrypt").strip().lower()
        eff = shift if mode.startswith("enc") else -shift
        out = _shift_text(text or "", eff)
        return {"mode": mode, "shift": shift, "output": _truncate(out)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"Caesar failed: {e}"}


def rot_n(text: str = "", n: int = 13) -> dict:
    """ROT-N: rotate letters by N (ROT13 when N=13). Self-inverse only at 13."""
    try:
        try:
            n = int(n) % 26
        except (TypeError, ValueError):
            return {"error": "n must be an integer."}
        out = _shift_text(text or "", n)
        return {"n": n, "output": _truncate(out)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"ROT-N failed: {e}"}


def vigenere_cipher(text: str = "", key: str = "", mode: str = "encrypt") -> dict:
    """Vigenere polyalphabetic cipher with a keyword."""
    try:
        key = key or ""
        letters = [c for c in key.lower() if "a" <= c <= "z"]
        if not letters:
            return {"error": "Key must contain at least one letter A-Z."}
        mode = (mode or "encrypt").strip().lower()
        encrypt = mode.startswith("enc")
        out = []
        ki = 0
        for ch in (text or ""):
            if ch.isalpha():
                k = ord(letters[ki % len(letters)]) - 97
                if not encrypt:
                    k = -k
                if ch.islower():
                    out.append(chr((ord(ch) - 97 + k) % 26 + 97))
                else:
                    out.append(chr((ord(ch) - 65 + k) % 26 + 65))
                ki += 1
            else:
                out.append(ch)
        return {"mode": mode, "key": "".join(letters), "output": _truncate("".join(out))}
    except Exception as e:  # noqa: BLE001
        return {"error": f"Vigenere failed: {e}"}


def atbash(text: str = "") -> dict:
    """Atbash cipher: mirror the alphabet (A<->Z). Self-inverse."""
    try:
        out = []
        for ch in (text or ""):
            if "a" <= ch <= "z":
                out.append(chr(219 - ord(ch)))   # 219 = ord('a') + ord('z')
            elif "A" <= ch <= "Z":
                out.append(chr(155 - ord(ch)))   # 155 = ord('A') + ord('Z')
            else:
                out.append(ch)
        return {"output": _truncate("".join(out))}
    except Exception as e:  # noqa: BLE001
        return {"error": f"Atbash failed: {e}"}


def rail_fence(text: str = "", rails: int = 3, mode: str = "encrypt") -> dict:
    """Rail-fence transposition cipher over N rails."""
    try:
        try:
            rails = int(rails)
        except (TypeError, ValueError):
            return {"error": "rails must be an integer."}
        text = text or ""
        if rails < 2:
            return {"error": "rails must be >= 2."}
        if rails >= len(text) or not text:
            return {"mode": mode, "rails": rails, "output": text,
                    "note": "rails >= length: output identical to input."}
        mode = (mode or "encrypt").strip().lower()

        # Build the zig-zag rail index for each position.
        pattern = []
        r, step = 0, 1
        for _ in range(len(text)):
            pattern.append(r)
            if r == 0:
                step = 1
            elif r == rails - 1:
                step = -1
            r += step

        if mode.startswith("enc"):
            buckets = ["" for _ in range(rails)]
            for ch, row in zip(text, pattern):
                buckets[row] += ch
            out = "".join(buckets)
        else:
            counts = [pattern.count(i) for i in range(rails)]
            buckets, idx = [], 0
            for c in counts:
                buckets.append(list(text[idx:idx + c]))
                idx += c
            pos = [0] * rails
            out_chars = []
            for row in pattern:
                out_chars.append(buckets[row][pos[row]])
                pos[row] += 1
            out = "".join(out_chars)
        return {"mode": mode, "rails": rails, "output": _truncate(out)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"Rail-fence failed: {e}"}


# ---------------------------------------------------------------------------
# XOR tools
# ---------------------------------------------------------------------------

def _parse_input_bytes(data: str, encoding: str):
    """Return (bytes, None) or (None, error_msg)."""
    encoding = (encoding or "auto").strip().lower()
    s = data or ""
    if encoding == "hex" or (encoding == "auto" and re.fullmatch(r"[0-9a-fA-F\s:]+", s.strip() or "x")):
        compact = re.sub(r"[\s:]+", "", s.strip())
        if compact and len(compact) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", compact):
            try:
                return bytes.fromhex(compact), None
            except ValueError:
                pass
        if encoding == "hex":
            return None, "Input is not valid hex."
    if encoding == "base64":
        try:
            return base64.b64decode(re.sub(r"\s+", "", s), validate=False), None
        except (binascii.Error, ValueError):
            return None, "Input is not valid base64."
    return s.encode("utf-8", errors="replace"), None


def xor_bruteforce(data: str = "", encoding: str = "auto", top: int = 5) -> dict:
    """Single-byte XOR brute force. Tries all 256 keys, ranks by English-likeness."""
    try:
        raw, err = _parse_input_bytes(data, encoding)
        if err:
            return {"error": err}
        if not raw:
            return {"error": "No input bytes to analyse."}
        if len(raw) > 8192:
            raw = raw[:8192]
        try:
            top = max(1, min(int(top), 25))
        except (TypeError, ValueError):
            top = 5
        scored = []
        for key in range(256):
            dec = bytes(b ^ key for b in raw)
            txt = dec.decode("latin-1")
            pr = _printable_ratio(dec)
            if pr < 0.7:
                continue
            score = _english_score(txt) / (pr + 0.01)
            scored.append((score, key, txt, pr))
        scored.sort(key=lambda x: x[0])
        candidates = [
            {"key_dec": k, "key_hex": f"0x{k:02x}",
             "key_char": chr(k) if 32 <= k <= 126 else ".",
             "printable_ratio": round(pr, 3),
             "score": round(sc, 2),
             "plaintext": _truncate(txt, 2000)}
            for sc, k, txt, pr in scored[:top]
        ]
        return {"input_bytes": len(raw), "candidates": candidates,
                "note": "Lower score = more English-like. Best guess listed first."}
    except Exception as e:  # noqa: BLE001
        return {"error": f"XOR brute force failed: {e}"}


def xor_cipher(data: str = "", key: str = "", encoding: str = "auto",
               output: str = "auto") -> dict:
    """Repeating-key XOR. Key is hex (e.g. 'deadbeef') or plain text if not valid hex."""
    try:
        raw, err = _parse_input_bytes(data, encoding)
        if err:
            return {"error": err}
        if raw is None or len(raw) == 0:
            return {"error": "No input bytes."}
        key = key or ""
        kcompact = re.sub(r"[\s:]+", "", key)
        if kcompact and len(kcompact) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", kcompact):
            keyb = bytes.fromhex(kcompact)
        else:
            keyb = key.encode("utf-8", errors="replace")
        if not keyb:
            return {"error": "Key is empty."}
        out = bytes(b ^ keyb[i % len(keyb)] for i, b in enumerate(raw))
        output = (output or "auto").strip().lower()
        as_text = out.decode("utf-8", errors="replace")
        if output == "hex" or (output == "auto" and _printable_ratio(out) < 0.85):
            shown, fmt = out.hex(), "hex"
        else:
            shown, fmt = as_text, "text"
        return {"key_bytes": len(keyb), "output_format": fmt,
                "output": _truncate(shown), "output_hex": _truncate(out.hex(), 4000)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"XOR failed: {e}"}


# ---------------------------------------------------------------------------
# Analysis / identification
# ---------------------------------------------------------------------------

def cipher_identifier(text: str = "") -> dict:
    """Heuristically guess the encoding/cipher used for a sample string."""
    try:
        s = (text or "").strip()
        if not s:
            return {"error": "Provide a sample to identify."}
        guesses = []
        compact = re.sub(r"\s+", "", s)

        # Hash-length fingerprints (hex only).
        if re.fullmatch(r"[0-9a-fA-F]+", compact):
            hlen = {32: "MD5 / NTLM", 40: "SHA-1", 56: "SHA-224", 64: "SHA-256",
                    96: "SHA-384", 128: "SHA-512"}.get(len(compact))
            if hlen:
                guesses.append((90, f"{hlen} hash (hex, {len(compact)} chars)"))
            else:
                guesses.append((55, f"Hex-encoded bytes ({len(compact)} hex chars)"))

        if re.fullmatch(r"[01\s]+", s) and "0" in s and "1" in s:
            guesses.append((85, "Binary (base-2) digits"))
        if re.fullmatch(r"[.\-/\s]+", s) and ("." in s or "-" in s):
            guesses.append((85, "Morse code"))
        if re.fullmatch(r"[A-Z2-7=\s]+", s) and "=" in s:
            guesses.append((70, "Base32 (RFC 4648)"))
        if re.fullmatch(r"[A-Za-z0-9+/=\s]+", s) and len(compact) % 4 == 0 and "=" in s:
            guesses.append((75, "Base64"))
        elif re.fullmatch(r"[A-Za-z0-9+/]+", compact) and len(compact) % 4 == 0 and len(compact) >= 8:
            guesses.append((50, "Possibly Base64 (no padding)"))
        if "%" in s and re.search(r"%[0-9a-fA-F]{2}", s):
            guesses.append((80, "URL / percent-encoding"))
        if re.search(r"&[a-zA-Z]+;|&#\d+;", s):
            guesses.append((78, "HTML entity encoding"))
        if compact.startswith("$2") and len(compact) >= 55:
            guesses.append((92, "bcrypt password hash"))
        if compact.startswith(("$1$", "$5$", "$6$")):
            guesses.append((88, "Unix crypt hash (md5/sha-256/sha-512)"))
        if re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", s):
            guesses.append((93, "JWT (JSON Web Token)"))
        if re.fullmatch(r"[A-Za-z\s.,!?'\";:()\-]+", s) and any(c.isalpha() for c in s):
            eng = _english_score(s)
            if eng < 80:
                guesses.append((40, "Looks like plain English text (possibly Caesar/Vigenere/Atbash if scrambled)"))
            else:
                guesses.append((45, "Alphabetic ciphertext (try Caesar/Vigenere/Atbash/substitution)"))
        if re.fullmatch(r"[!-uz]+", compact) and len(compact) >= 5 and not re.fullmatch(r"[A-Za-z0-9+/=]+", compact):
            guesses.append((35, "Possibly Base85/Ascii85"))

        if not guesses:
            guesses.append((20, "Unknown — does not match common encodings."))
        guesses.sort(key=lambda x: -x[0])
        seen, ranked = set(), []
        for conf, label in guesses:
            if label in seen:
                continue
            seen.add(label)
            ranked.append({"confidence": conf, "guess": label})
        return {"sample_len": len(s), "guesses": ranked[:6]}
    except Exception as e:  # noqa: BLE001
        return {"error": f"Identification failed: {e}"}


def frequency_analysis(text: str = "") -> dict:
    """Letter-frequency analysis of ciphertext (with English comparison & IoC)."""
    try:
        s = text or ""
        if not s:
            return {"error": "Provide ciphertext to analyse."}
        counts = {chr(c): 0 for c in range(ord("a"), ord("z") + 1)}
        total = 0
        for ch in s.lower():
            if "a" <= ch <= "z":
                counts[ch] += 1
                total += 1
        if total == 0:
            return {"error": "No alphabetic characters found."}
        freq = []
        for letter in sorted(counts, key=lambda k: -counts[k]):
            if counts[letter] == 0:
                continue
            pct = round(100 * counts[letter] / total, 2)
            freq.append({"letter": letter, "count": counts[letter], "percent": pct,
                         "english_percent": _ENGLISH_FREQ[letter]})
        # Index of Coincidence: ~0.066 for English, ~0.038 for random/polyalphabetic.
        ioc = sum(n * (n - 1) for n in counts.values()) / (total * (total - 1)) if total > 1 else 0.0
        if ioc >= 0.060:
            ioc_hint = "High IoC (~English): likely monoalphabetic (Caesar/substitution) or plaintext."
        elif ioc <= 0.045:
            ioc_hint = "Low IoC: likely polyalphabetic (Vigenere) or near-random."
        else:
            ioc_hint = "Intermediate IoC: short text or mixed cipher."
        most = freq[0]["letter"] if freq else None
        return {"total_letters": total, "unique_letters": len(freq),
                "index_of_coincidence": round(ioc, 4), "ioc_hint": ioc_hint,
                "most_common": most,
                "caesar_shift_if_e": ((ord(most) - ord("e")) % 26) if most else None,
                "frequencies": freq}
    except Exception as e:  # noqa: BLE001
        return {"error": f"Frequency analysis failed: {e}"}


# ---------------------------------------------------------------------------
# Hashing / integrity
# ---------------------------------------------------------------------------

def hash_everything(text: str = "") -> dict:
    """Compute md5/sha1/sha256/sha512/sha3_256/blake2b digests of text."""
    try:
        raw = _as_bytes(text)
        return {
            "input_len": len(text or ""),
            "md5": hashlib.md5(raw).hexdigest(),
            "sha1": hashlib.sha1(raw).hexdigest(),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "sha512": hashlib.sha512(raw).hexdigest(),
            "sha3_256": hashlib.sha3_256(raw).hexdigest(),
            "blake2b": hashlib.blake2b(raw).hexdigest(),
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"Hashing failed: {e}"}


def crc32_tool(text: str = "") -> dict:
    """Compute the CRC-32 checksum of text (hex + unsigned decimal)."""
    try:
        raw = _as_bytes(text)
        val = binascii.crc32(raw) & 0xFFFFFFFF
        return {"input_len": len(text or ""), "crc32_hex": f"{val:08x}",
                "crc32_decimal": val}
    except Exception as e:  # noqa: BLE001
        return {"error": f"CRC32 failed: {e}"}


def hmac_tool(text: str = "", key: str = "", mode: str = "generate",
              expected: str = "") -> dict:
    """Generate or verify an HMAC-SHA256 over text with a secret key."""
    try:
        if not key:
            return {"error": "A secret key is required."}
        raw = _as_bytes(text)
        keyb = _as_bytes(key)
        digest = hmac.new(keyb, raw, hashlib.sha256).hexdigest()
        mode = (mode or "generate").strip().lower()
        if mode.startswith("ver"):
            exp = re.sub(r"\s+", "", (expected or "")).lower()
            if not exp:
                return {"error": "Provide the 'expected' HMAC to verify."}
            valid = hmac.compare_digest(digest, exp)
            return {"mode": "verify", "valid": valid, "computed_hmac": digest}
        return {"mode": "generate", "algorithm": "HMAC-SHA256", "hmac": digest}
    except Exception as e:  # noqa: BLE001
        return {"error": f"HMAC failed: {e}"}


# ---------------------------------------------------------------------------
# Fernet (AES) demo
# ---------------------------------------------------------------------------

def _fernet_from_pass(passphrase: str) -> Fernet:
    digest = hashlib.sha256(passphrase.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def aes_demo(text: str = "", passphrase: str = "", mode: str = "encrypt") -> dict:
    """Authenticated symmetric encryption via Fernet (AES-128-CBC + HMAC).

    The Fernet key is derived as urlsafe_b64(sha256(passphrase)). NOTE: a raw
    SHA-256 of a passphrase is a demo KDF, not a substitute for scrypt/argon2.
    """
    try:
        if not passphrase:
            return {"error": "A passphrase is required."}
        f = _fernet_from_pass(passphrase)
        mode = (mode or "encrypt").strip().lower()
        if mode.startswith("enc"):
            token = f.encrypt(_as_bytes(text)).decode("ascii")
            return {"mode": "encrypt", "scheme": "Fernet (AES-128-CBC+HMAC-SHA256)",
                    "token": _truncate(token)}
        else:
            try:
                plain = f.decrypt(_as_bytes(text)).decode("utf-8", errors="replace")
            except InvalidToken:
                return {"error": "Decryption failed: wrong passphrase or corrupted/"
                                 "non-Fernet token."}
            return {"mode": "decrypt", "plaintext": _truncate(plain)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"AES demo failed: {e}"}


# ---------------------------------------------------------------------------
# TOTP (RFC 6238)
# ---------------------------------------------------------------------------

def totp_tool(secret: str = "", digits: int = 6, period: int = 30,
              algorithm: str = "SHA1") -> dict:
    """Generate the current TOTP code from a Base32 secret (RFC 6238)."""
    try:
        if not secret:
            return {"error": "A Base32 secret is required."}
        try:
            digits = int(digits)
        except (TypeError, ValueError):
            digits = 6
        try:
            period = int(period)
        except (TypeError, ValueError):
            period = 30
        if digits < 6 or digits > 10:
            return {"error": "digits must be between 6 and 10."}
        if period < 1:
            return {"error": "period must be >= 1 second."}

        algo = (algorithm or "SHA1").strip().upper()
        hashmod = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256,
                   "SHA512": hashlib.sha512}.get(algo)
        if hashmod is None:
            return {"error": "algorithm must be SHA1, SHA256, or SHA512."}

        compact = re.sub(r"\s+", "", secret).upper()
        compact += "=" * ((8 - len(compact) % 8) % 8)  # pad to multiple of 8
        try:
            key = base64.b32decode(compact, casefold=True)
        except (binascii.Error, ValueError):
            return {"error": "Secret is not valid Base32."}
        if not key:
            return {"error": "Decoded secret is empty."}

        now = int(time.time())
        counter = now // period

        def _code_for(ctr: int) -> str:
            msg = struct.pack(">Q", ctr)
            hs = hmac.new(key, msg, hashmod).digest()
            offset = hs[-1] & 0x0F
            binv = ((hs[offset] & 0x7F) << 24 |
                    (hs[offset + 1] & 0xFF) << 16 |
                    (hs[offset + 2] & 0xFF) << 8 |
                    (hs[offset + 3] & 0xFF))
            return str(binv % (10 ** digits)).zfill(digits)

        remaining = period - (now % period)
        return {
            "code": _code_for(counter),
            "algorithm": algo,
            "digits": digits,
            "period": period,
            "seconds_remaining": remaining,
            "prev_code": _code_for(counter - 1),
            "next_code": _code_for(counter + 1),
            "note": "Codes valid for prev/current/next window to allow clock skew.",
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"TOTP failed: {e}"}


# ---------------------------------------------------------------------------
# SPECS
# ---------------------------------------------------------------------------

_CAT = "Crypto & Encoding"

SPECS = [
    {
        "name": "multi_encoder", "label": "Multi Encoder",
        "description": "Encode text to base64/base32/base85/hex/url/html/rot13/binary/morse.",
        "category": _CAT, "tier": "green",
        "inputs": [
            {"key": "text", "label": "Text", "type": "textarea",
             "placeholder": "Text to encode"},
            {"key": "scheme", "label": "Scheme", "type": "select",
             "options": ["base64", "base32", "base85", "hex", "url", "html",
                         "rot13", "binary", "morse"]},
        ],
    },
    {
        "name": "multi_decoder", "label": "Multi Decoder (auto)",
        "description": "Auto-detect and decode base64/32/85, hex, binary, url, html, morse, rot13.",
        "category": _CAT, "tier": "green",
        "inputs": [
            {"key": "text", "label": "Encoded text", "type": "textarea",
             "placeholder": "Paste an encoded string"},
        ],
    },
    {
        "name": "caesar_cipher", "label": "Caesar Cipher",
        "description": "Encrypt/decrypt with a Caesar shift of N positions.",
        "category": _CAT, "tier": "green",
        "inputs": [
            {"key": "text", "label": "Text", "type": "textarea", "placeholder": "Message"},
            {"key": "shift", "label": "Shift", "type": "number", "placeholder": "3"},
            {"key": "mode", "label": "Mode", "type": "select",
             "options": ["encrypt", "decrypt"]},
        ],
    },
    {
        "name": "rot_n", "label": "ROT-N",
        "description": "Rotate letters by N (ROT13 at N=13).",
        "category": _CAT, "tier": "green",
        "inputs": [
            {"key": "text", "label": "Text", "type": "textarea", "placeholder": "Message"},
            {"key": "n", "label": "N", "type": "number", "placeholder": "13"},
        ],
    },
    {
        "name": "vigenere_cipher", "label": "Vigenere Cipher",
        "description": "Polyalphabetic Vigenere cipher with a keyword (encrypt/decrypt).",
        "category": _CAT, "tier": "green",
        "inputs": [
            {"key": "text", "label": "Text", "type": "textarea", "placeholder": "Message"},
            {"key": "key", "label": "Keyword", "type": "text", "placeholder": "SECRET"},
            {"key": "mode", "label": "Mode", "type": "select",
             "options": ["encrypt", "decrypt"]},
        ],
    },
    {
        "name": "atbash", "label": "Atbash Cipher",
        "description": "Mirror-alphabet Atbash cipher (A<->Z). Self-inverse.",
        "category": _CAT, "tier": "green",
        "inputs": [
            {"key": "text", "label": "Text", "type": "textarea", "placeholder": "Message"},
        ],
    },
    {
        "name": "rail_fence", "label": "Rail Fence Cipher",
        "description": "Zig-zag rail-fence transposition over N rails (encrypt/decrypt).",
        "category": _CAT, "tier": "green",
        "inputs": [
            {"key": "text", "label": "Text", "type": "textarea", "placeholder": "Message"},
            {"key": "rails", "label": "Rails", "type": "number", "placeholder": "3"},
            {"key": "mode", "label": "Mode", "type": "select",
             "options": ["encrypt", "decrypt"]},
        ],
    },
    {
        "name": "xor_bruteforce", "label": "XOR Brute Force",
        "description": "Recover single-byte XOR key; ranks candidates by English-likeness.",
        "category": _CAT, "tier": "edu",
        "inputs": [
            {"key": "data", "label": "Ciphertext", "type": "textarea",
             "placeholder": "Hex (e.g. 1b37...) or raw text"},
            {"key": "encoding", "label": "Input format", "type": "select",
             "options": ["auto", "hex", "base64", "text"]},
            {"key": "top", "label": "Top N candidates", "type": "number",
             "placeholder": "5"},
        ],
    },
    {
        "name": "xor_cipher", "label": "XOR Cipher (repeating key)",
        "description": "Repeating-key XOR with a hex or text key.",
        "category": _CAT, "tier": "green",
        "inputs": [
            {"key": "data", "label": "Data", "type": "textarea",
             "placeholder": "Hex, base64, or text"},
            {"key": "key", "label": "Key", "type": "text",
             "placeholder": "hex like deadbeef, or text"},
            {"key": "encoding", "label": "Input format", "type": "select",
             "options": ["auto", "hex", "base64", "text"]},
            {"key": "output", "label": "Output format", "type": "select",
             "options": ["auto", "text", "hex"]},
        ],
    },
    {
        "name": "cipher_identifier", "label": "Cipher / Encoding Identifier",
        "description": "Guess the encoding or cipher from a sample string.",
        "category": _CAT, "tier": "green",
        "inputs": [
            {"key": "text", "label": "Sample", "type": "textarea",
             "placeholder": "Paste a token, hash, or ciphertext"},
        ],
    },
    {
        "name": "frequency_analysis", "label": "Frequency Analysis",
        "description": "Letter frequency + Index of Coincidence to fingerprint ciphertext.",
        "category": _CAT, "tier": "green",
        "inputs": [
            {"key": "text", "label": "Ciphertext", "type": "textarea",
             "placeholder": "Ciphertext to analyse"},
        ],
    },
    {
        "name": "hash_everything", "label": "Hash Everything",
        "description": "md5/sha1/sha256/sha512/sha3_256/blake2b digests of text.",
        "category": _CAT, "tier": "green",
        "inputs": [
            {"key": "text", "label": "Text", "type": "textarea",
             "placeholder": "Text to hash"},
        ],
    },
    {
        "name": "crc32_tool", "label": "CRC-32 Checksum",
        "description": "Compute the CRC-32 checksum (hex + decimal) of text.",
        "category": _CAT, "tier": "green",
        "inputs": [
            {"key": "text", "label": "Text", "type": "textarea",
             "placeholder": "Text to checksum"},
        ],
    },
    {
        "name": "hmac_tool", "label": "HMAC-SHA256",
        "description": "Generate or verify an HMAC-SHA256 over text with a secret key.",
        "category": _CAT, "tier": "green",
        "inputs": [
            {"key": "text", "label": "Message", "type": "textarea",
             "placeholder": "Message to authenticate"},
            {"key": "key", "label": "Secret key", "type": "text",
             "placeholder": "shared secret"},
            {"key": "mode", "label": "Mode", "type": "select",
             "options": ["generate", "verify"]},
            {"key": "expected", "label": "Expected HMAC (verify)", "type": "text",
             "placeholder": "hex digest to compare"},
        ],
    },
    {
        "name": "aes_demo", "label": "AES (Fernet) Encrypt/Decrypt",
        "description": "Authenticated symmetric encryption via Fernet; key from sha256(passphrase).",
        "category": _CAT, "tier": "green",
        "inputs": [
            {"key": "text", "label": "Text / token", "type": "textarea",
             "placeholder": "Plaintext to encrypt, or token to decrypt"},
            {"key": "passphrase", "label": "Passphrase", "type": "text",
             "placeholder": "shared passphrase"},
            {"key": "mode", "label": "Mode", "type": "select",
             "options": ["encrypt", "decrypt"]},
        ],
    },
    {
        "name": "totp_tool", "label": "TOTP Generator",
        "description": "Generate the current RFC 6238 TOTP code from a Base32 secret.",
        "category": _CAT, "tier": "green",
        "inputs": [
            {"key": "secret", "label": "Base32 secret", "type": "text",
             "placeholder": "JBSWY3DPEHPK3PXP"},
            {"key": "digits", "label": "Digits", "type": "number", "placeholder": "6"},
            {"key": "period", "label": "Period (s)", "type": "number", "placeholder": "30"},
            {"key": "algorithm", "label": "Algorithm", "type": "select",
             "options": ["SHA1", "SHA256", "SHA512"]},
        ],
    },
]
