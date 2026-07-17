"""Resource library: turn uploaded books (PDF / TXT / MD) into page-numbered,
case-insensitively searchable markdown, and let the AI query them.

One global, on-disk library under ``settings.resources_dir``. Each book lives in
its own folder::

    resources/<book_id>/
        original.<ext>   uploaded bytes (re-extract / download source)
        book.md          "# Title\n\n## PAGE NO 1\n<text>\n\n## PAGE NO 2\n..."
        pages.json       ["page 1 text", "page 2 text", ...]   (canonical fast store)
        meta.json        {book_id, title, filename, ext, pages, paragraphs,
                          bytes, sha256, created_at, truncated}

``pages.json`` is the hot-path store (O(1) page lookup + the paragraph source for
the search index). ``book.md`` is the human/AI-readable artifact the user asked
for. Full text never goes to Firestore.

Search is dependency-free and **case-insensitive** (IDF-weighted, length
normalised) and returns at most ``settings.resource_search_limit`` (20) paragraph
snippets with book + page provenance, so the agent's context never floods.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import threading
import time
import unicodedata
from pathlib import Path

from app.config import get_settings

try:  # pypdf is optional at import time (defensive, like main.py's Firebase import).
    from pypdf import PdfReader
    _PYPDF_OK = True
except Exception:  # noqa: BLE001
    PdfReader = None  # type: ignore
    _PYPDF_OK = False

try:  # OCR stack — optional; only used for scanned / image PDFs (no text layer).
    import pypdfium2 as pdfium     # render PDF pages -> images (permissive, no binary)
    _PDFIUM_OK = True
except Exception:  # noqa: BLE001
    pdfium = None  # type: ignore
    _PDFIUM_OK = False

try:  # primary OCR engine — pure-pip ONNX (RapidOCR); NO system binary required
    from rapidocr_onnxruntime import RapidOCR
    _RAPIDOCR_OK = True
except Exception:  # noqa: BLE001
    RapidOCR = None  # type: ignore
    _RAPIDOCR_OK = False

try:  # optional fallback engine — only used if a system 'tesseract' binary exists
    import pytesseract
    _PYTESS_OK = True
except Exception:  # noqa: BLE001
    pytesseract = None  # type: ignore
    _PYTESS_OK = False

_rapid_engine = None  # lazily-built RapidOCR singleton (ONNX models load once)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Page marker written into book.md — parsed back case-insensitively.
_PAGE_MARKER_RE = re.compile(r"^[ \t]*##[ \t]*PAGE[ \t]*NO[ \t]*(\d+)[ \t]*$",
                             re.IGNORECASE | re.MULTILINE)
_MIN_PARA_CHARS = 25                  # drop shorter blocks (page numbers, headers)
_SUPPORTED_EXTS = {".pdf", ".txt", ".md", ".markdown"}
_MAX_RESULT_CHARS = 11000             # keep tool output under the agent's 12k truncation


# --------------------------------------------------------------------------- #
#  paths / helpers
# --------------------------------------------------------------------------- #
def _root() -> Path:
    rd = Path(get_settings().resources_dir)
    if not rd.is_absolute():
        rd = _PROJECT_ROOT / rd
    rd.mkdir(parents=True, exist_ok=True)
    return rd


def _slug(name: str) -> str:
    """Filesystem-safe book id derived from a filename/title."""
    stem = Path(name or "").stem.lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", stem).strip("-._")
    return slug[:64] or "book"


def _book_dir(book_id: str) -> Path | None:
    """Resolve a book folder, refusing any path-traversal escape."""
    if not book_id or "/" in book_id or "\\" in book_id or ".." in book_id:
        return None
    root = _root().resolve()
    d = (root / book_id).resolve()
    return d if (d != root and root in d.parents) else None


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text or "")


def _title_from_filename(filename: str) -> str:
    stem = re.sub(r"[_-]+", " ", Path(filename or "book").stem).strip()
    return stem.title() if stem else "Untitled"


# --------------------------------------------------------------------------- #
#  extraction:  bytes -> list[page_text]
# --------------------------------------------------------------------------- #
_TESS_DIRS = [
    r"C:\Program Files\Tesseract-OCR",
    r"C:\Program Files (x86)\Tesseract-OCR",
    r"C:\ProgramData\chocolatey\bin",
    str(_PROJECT_ROOT / "tools"),
]


def _tesseract_cmd() -> str | None:
    """Locate the tesseract binary: config path, then PATH, then common install dirs."""
    cfg = (get_settings().tesseract_path or "").strip()
    if cfg and Path(cfg).exists():
        return cfg
    found = shutil.which("tesseract")
    if found:
        return found
    exe = "tesseract.exe" if os.name == "nt" else "tesseract"
    for d in _TESS_DIRS:
        p = Path(d) / exe
        if p.exists():
            return str(p)
    return None


def _get_rapidocr():
    """Lazily build the RapidOCR engine once (ONNX models load on first use)."""
    global _rapid_engine
    if _rapid_engine is None and _RAPIDOCR_OK:
        _rapid_engine = RapidOCR()
    return _rapid_engine


def _ocr_engine_name() -> str | None:
    """Which OCR engine is usable: pip RapidOCR preferred, tesseract binary fallback."""
    if _RAPIDOCR_OK:
        return "rapidocr"
    if _PYTESS_OK and _tesseract_cmd():
        return "tesseract"
    return None


def ocr_status() -> dict:
    """Diagnostic: which OCR engine is active and whether OCR is ready."""
    s = get_settings()
    engine = _ocr_engine_name()
    return {
        "enabled": s.ocr_enabled,
        "engine": engine,             # 'rapidocr' (pip, no binary) | 'tesseract' | None
        "render": _PDFIUM_OK,
        "ready": bool(s.ocr_enabled and _PDFIUM_OK and engine),
    }


def _ocr_available() -> bool:
    """True if we can render PDF pages AND have some OCR engine."""
    return bool(_PDFIUM_OK and _ocr_engine_name())


def _ocr_image(img, lang: str) -> str:
    """OCR a PIL image — RapidOCR (pip, no binary) preferred, Tesseract as fallback."""
    if _RAPIDOCR_OK:
        import numpy as np
        result, _ = _get_rapidocr()(np.asarray(img))
        return _normalize("\n".join(line[1] for line in (result or [])))
    if _PYTESS_OK and _tesseract_cmd():
        pytesseract.pytesseract.tesseract_cmd = _tesseract_cmd()
        return _normalize(pytesseract.image_to_string(img, lang=lang or "eng"))
    return ""


def _ocr_pdf_page(pdf, idx: int, dpi: int, lang: str) -> str:
    """Render one page of an open pdfium document to an image and OCR it."""
    bitmap = pdf[idx].render(scale=max(0.5, dpi / 72.0))
    img = bitmap.to_pil()
    try:
        return _ocr_image(img, lang)
    finally:
        img.close()


def _extract_pdf_pages(pdf_path: str | Path, ocr: bool | None = None) -> tuple[list[str], int]:
    """Extract text per page; OCR any page that has no text layer (when OCR is on and
    an engine is available). ``ocr`` overrides the global ``ocr_enabled`` per call.
    Returns (pages, ocr_page_count). Blocking. Raises RuntimeError if pypdf is missing."""
    if not _PYPDF_OK:
        raise RuntimeError("PDF support not installed (pip install pypdf).")
    settings = get_settings()
    ocr_on = settings.ocr_enabled if ocr is None else bool(ocr)
    reader = PdfReader(str(pdf_path))
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")  # many "encrypted" PDFs use an empty owner password
        except Exception:  # noqa: BLE001
            pass
    pages: list[str] = []
    empty_idx: list[int] = []
    for i, page in enumerate(reader.pages):
        if i >= settings.max_pdf_pages:
            break
        try:
            txt = page.extract_text() or ""
        except Exception:  # noqa: BLE001 — one bad page must not kill the whole book
            txt = ""
        txt = _normalize(txt)
        pages.append(txt)
        if not txt.strip():
            empty_idx.append(i)

    ocr_count = 0
    if empty_idx and ocr_on and _ocr_available():
        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            for i in empty_idx[:max(0, settings.ocr_max_pages)]:
                try:
                    t = _ocr_pdf_page(pdf, i, settings.ocr_dpi, settings.ocr_lang)
                except Exception:  # noqa: BLE001
                    t = ""
                if t.strip():
                    pages[i] = t
                    ocr_count += 1
        finally:
            pdf.close()
    return pages, ocr_count


def extract_pdf_pages(pdf_path: str | Path) -> list[str]:
    """One extracted-text string per page (OCR fills pages with no text layer when
    enabled + Tesseract present). Blocking — wrap in ``asyncio.to_thread``."""
    return _extract_pdf_pages(pdf_path)[0]


def _no_text_message(ext: str, ocr: bool | None = None) -> str:
    """A helpful rejection message when nothing could be extracted from an upload."""
    if ext != ".pdf":
        return "The file appears to be empty."
    s = get_settings()
    ocr_on = s.ocr_enabled if ocr is None else bool(ocr)
    if not ocr_on:
        return ("No text layer found (scanned PDF?). OCR was turned off for this "
                "upload — switch the OCR toggle on and upload it again.")
    if not _PDFIUM_OK:
        return ("No text layer found (scanned PDF?). OCR renderer missing — "
                "pip install pypdfium2 pillow.")
    if not _ocr_engine_name():
        return ("No text layer found (scanned PDF?). OCR engine missing — "
                "pip install rapidocr-onnxruntime.")
    return "No readable text could be extracted from this PDF, even with OCR."


def extract_text_pages(text: str, *, chars_per_page: int | None = None) -> list[str]:
    """Split plain text / markdown into pages. Honors existing '## PAGE NO n'
    markers (a re-uploaded book.md) and form-feed page breaks; otherwise chunks
    into pseudo-pages at paragraph boundaries so page provenance stays useful."""
    text = _normalize(text)
    if _PAGE_MARKER_RE.search(text):
        return markdown_to_pages(text)
    if "\f" in text:
        return [p.strip() for p in text.split("\f") if p.strip()]
    limit = chars_per_page or get_settings().resource_chars_per_page
    pages: list[str] = []
    cur = ""
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        if cur and len(cur) + len(block) + 2 > limit:
            pages.append(cur)
            cur = block
        else:
            cur = f"{cur}\n\n{block}" if cur else block
    if cur:
        pages.append(cur)
    return pages or ([text.strip()] if text.strip() else [])


# --------------------------------------------------------------------------- #
#  markdown <-> pages
# --------------------------------------------------------------------------- #
def pages_to_markdown(pages: list[str]) -> str:
    """Join page texts with explicit '## PAGE NO n' markers (n is 1-based)."""
    parts = []
    for i, page in enumerate(pages, start=1):
        body = (page or "").strip()
        parts.append(f"## PAGE NO {i}\n\n{body}" if body else f"## PAGE NO {i}")
    return "\n\n".join(parts) + "\n"


def markdown_to_pages(md: str) -> list[str]:
    """Inverse of pages_to_markdown: split on '## PAGE NO n' markers into ordered
    page texts (marker order wins; tolerant of gaps/duplicates). Any preamble
    before the first marker — e.g. the '# Title' line — is dropped."""
    md = _normalize(md)
    matches = list(_PAGE_MARKER_RE.finditer(md))
    if not matches:
        return [md.strip()] if md.strip() else []
    pages: list[str] = []
    for idx, m in enumerate(matches):
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(md)
        pages.append(md[start:end].strip())
    return pages


def _book_markdown(title: str, pages: list[str]) -> str:
    head = f"# {title}\n\n" if title else ""
    return head + pages_to_markdown(pages)


def split_paragraphs(page_text: str) -> list[str]:
    """Segment a page's text into clean paragraphs (the search unit)."""
    text = (page_text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"-\n(\w)", r"\1", text)          # de-hyphenate line-wrapped words
    paras: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        joined = re.sub(r"\s+", " ", block.replace("\n", " ")).strip()
        if len(joined) >= _MIN_PARA_CHARS:
            paras.append(joined)
    return paras


# --------------------------------------------------------------------------- #
#  metadata helpers
# --------------------------------------------------------------------------- #
def _iter_meta() -> list[dict]:
    metas: list[dict] = []
    root = _root()
    for d in root.iterdir():
        if not d.is_dir():
            continue
        mp = d / "meta.json"
        if mp.exists():
            try:
                metas.append(json.loads(mp.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                continue
    return metas


def _find_by_sha(sha: str) -> dict | None:
    for m in _iter_meta():
        if m.get("sha256") == sha:
            return m
    return None


def _unique_book_id(base: str) -> str:
    root = _root()
    book_id, n = base, 2
    while (root / book_id).exists():
        book_id = f"{base}-{n}"
        n += 1
    return book_id


def _load_meta(book_id: str) -> dict | None:
    d = _book_dir((book_id or "").strip())
    if d is None:
        return None
    mp = d / "meta.json"
    if mp.exists():
        try:
            return json.loads(mp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
    return None


def _load_pages(book_id: str) -> list[str] | None:
    d = _book_dir((book_id or "").strip())
    if d is None or not d.exists():
        return None
    pj = d / "pages.json"
    if pj.exists():
        try:
            return json.loads(pj.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    md = d / "book.md"
    if md.exists():
        return markdown_to_pages(md.read_text(encoding="utf-8"))
    return None


# --------------------------------------------------------------------------- #
#  persistence:  upload -> book folder
# --------------------------------------------------------------------------- #
def save_book(data: bytes, filename: str, *, title: str | None = None,
              ocr: bool | None = None) -> dict:
    """Persist an uploaded book: extract pages, write original/book.md/pages.json/
    meta.json, refresh the search index. Raises ValueError on bad input. Returns
    the book's meta dict (with ``duplicate: True`` if identical bytes already exist)."""
    settings = get_settings()
    ext = (Path(filename or "").suffix.lower() or ".pdf")
    if ext == ".markdown":
        ext = ".md"
    if ext not in _SUPPORTED_EXTS:
        raise ValueError(f"Unsupported file type '{ext}'. Use PDF, TXT or MD.")
    if not data:
        raise ValueError("Empty file.")
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise ValueError(f"File exceeds {settings.max_upload_mb} MB.")

    sha = hashlib.sha256(data).hexdigest()
    existing = _find_by_sha(sha)
    if existing:
        return {**existing, "duplicate": True}

    # --- extract pages (OCR fills scanned / image pages when enabled) ---
    ocr_count = 0
    if ext == ".pdf":
        if data[:5] != b"%PDF-":
            raise ValueError("Not a valid PDF file.")
        tmp = _root() / f".upload-{sha[:16]}.pdf"
        tmp.write_bytes(data)
        try:
            pages, ocr_count = _extract_pdf_pages(tmp, ocr=ocr)
        finally:
            tmp.unlink(missing_ok=True)
    else:
        pages = extract_text_pages(data.decode("utf-8", errors="replace"))

    if not any(p.strip() for p in pages):
        raise ValueError(_no_text_message(ext, ocr=ocr))

    truncated = ext == ".pdf" and len(pages) >= settings.max_pdf_pages
    book_title = (title or "").strip() or _title_from_filename(filename)
    book_id = _unique_book_id(_slug(title or filename or "book"))
    d = _book_dir(book_id)
    if d is None:
        raise ValueError("Could not allocate a safe book id.")
    d.mkdir(parents=True, exist_ok=True)

    meta = {
        "book_id": book_id,
        "title": book_title,
        "filename": filename or f"book{ext}",
        "ext": ext,
        "pages": len(pages),
        "paragraphs": sum(len(split_paragraphs(p)) for p in pages),
        "bytes": len(data),
        "sha256": sha,
        "created_at": int(time.time()),
        "truncated": truncated,
        "ocr_pages": ocr_count,
    }

    (d / f"original{ext}").write_bytes(data)
    (d / "pages.json").write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
    (d / "book.md").write_text(_book_markdown(book_title, pages), encoding="utf-8")
    (d / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    rebuild_index()
    return meta


def list_books() -> list[dict]:
    """Lightweight cards for the UI / model, newest first. Never includes text."""
    metas = _iter_meta()
    metas.sort(key=lambda m: m.get("created_at", 0), reverse=True)
    return [{
        "book_id": m.get("book_id"),
        "title": m.get("title"),
        "pages": m.get("pages", 0),
        "paragraphs": m.get("paragraphs", 0),
        "bytes": m.get("bytes", 0),
        "ext": m.get("ext"),
        "created_at": m.get("created_at", 0),
        "ocr_pages": m.get("ocr_pages", 0),
    } for m in metas]


def get_resource_page(book_id: str, page: int) -> dict:
    """Full text of one page (1-based) of one book."""
    book_id = (book_id or "").strip()
    pages = _load_pages(book_id)
    if pages is None:
        return {"error": f"Book not found: {book_id!r}. Use list_resources to see ids."}
    try:
        page = int(page)
    except (TypeError, ValueError):
        return {"error": "Page must be a whole number."}
    if page < 1 or page > len(pages):
        return {"error": f"Page {page} out of range (1–{len(pages)})."}
    meta = _load_meta(book_id) or {}
    text = pages[page - 1]
    return {
        "book_id": book_id,
        "book_title": meta.get("title", book_id),
        "page": page,
        "page_count": len(pages),
        "text": text,
        "chars": len(text),
    }


def delete_book(book_id: str) -> dict:
    d = _book_dir((book_id or "").strip())
    if d is None or not d.exists():
        return {"error": f"Book not found: {book_id!r}"}
    shutil.rmtree(d, ignore_errors=True)
    rebuild_index()
    return {"deleted": book_id}


def book_markdown_path(book_id: str) -> Path | None:
    d = _book_dir((book_id or "").strip())
    if d is None:
        return None
    p = d / "book.md"
    return p if p.exists() else None


# --------------------------------------------------------------------------- #
#  in-memory paragraph index + case-insensitive search
# --------------------------------------------------------------------------- #
_LOCK = threading.RLock()
_INDEX: list[dict] = []          # {book_id, book_title, page, para_idx, text}
_DF: dict[str, int] = {}         # token -> # paragraphs containing it (for IDF)
_N: int = 0                      # len(_INDEX)
_LOADED = False


def _tokens(text: str) -> list[str]:
    """Lowercase ASCII alnum tokens — the same scheme used elsewhere in Talos, so
    search is case-insensitive end to end. (Known limit: no CJK/Cyrillic split.)"""
    return [t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t]


def rebuild_index() -> None:
    """(Re)build the paragraph index + document frequencies from disk. The heavy
    parse runs OUTSIDE the lock; only the atomic rebind is locked, so an in-flight
    search is never blocked while a new book is being parsed."""
    global _INDEX, _DF, _N, _LOADED
    index: list[dict] = []
    df: dict[str, int] = {}
    for m in _iter_meta():
        book_id = m.get("book_id")
        title = m.get("title", book_id)
        for pno, page_text in enumerate(_load_pages(book_id) or [], start=1):
            for pidx, para in enumerate(split_paragraphs(page_text)):
                index.append({"book_id": book_id, "book_title": title,
                              "page": pno, "para_idx": pidx, "text": para})
                for tok in set(_tokens(para)):
                    df[tok] = df.get(tok, 0) + 1
    with _LOCK:
        _INDEX, _DF, _N, _LOADED = index, df, len(index), True


def _ensure_index() -> None:
    if not _LOADED:
        rebuild_index()


def _snippet(text: str, terms: list[str], width: int) -> str:
    """Whitespace-collapsed window centered on the first keyword hit."""
    clean = re.sub(r"\s+", " ", text).strip()
    if len(clean) <= width:
        return clean
    low = clean.lower()
    hits = [low.find(t) for t in terms]
    pos = min((p for p in hits if p != -1), default=-1)
    if pos == -1:
        return clean[:width].rstrip() + "…"
    end = min(len(clean), max(pos - width // 3, 0) + width)
    start = max(0, end - width)
    return ("…" if start > 0 else "") + clean[start:end].strip() + ("…" if end < len(clean) else "")


def _shrink_to_budget(out: dict) -> None:
    """Keep the serialized result under the agent's tool-result budget."""
    if len(json.dumps(out, ensure_ascii=False)) <= _MAX_RESULT_CHARS:
        return
    for r in out["results"]:
        if len(r["snippet"]) > 200:
            r["snippet"] = r["snippet"][:200].rstrip() + "…"
    while out["results"] and len(json.dumps(out, ensure_ascii=False)) > _MAX_RESULT_CHARS:
        out["results"].pop()
    out["showing"] = len(out["results"])


def search_resources(keywords, limit: int = 20) -> dict:
    """Case-insensitive keyword search across ALL resources' paragraphs. Returns
    the top <= ``resource_search_limit`` (20) matching paragraphs, each with its
    book + page so the model can cite — or call get_resource_page for more."""
    settings = get_settings()
    if isinstance(keywords, (list, tuple)):
        keywords = " ".join(str(k) for k in keywords)
    query = (keywords or "").strip()
    terms = list(dict.fromkeys(_tokens(query)))  # dedup, keep order
    if not terms:
        return {"error": "Provide one or more keywords to search."}

    _ensure_index()
    with _LOCK:                      # snapshot a consistent (index, df, n) triple
        records, df, n = _INDEX, _DF, _N
    if not records:
        return {"query": query, "match_count": 0, "showing": 0, "results": [],
                "note": "The resource library is empty — upload a book first."}

    def idf(tok: str) -> float:
        return math.log((n + 1) / (df.get(tok, 0) + 1)) + 1.0

    scored: list[tuple[float, dict]] = []
    for rec in records:                          # scoring runs outside the lock
        low = rec["text"].lower()
        hits = {t: low.count(t) for t in terms}
        distinct = sum(1 for t in terms if hits[t])
        if not distinct:
            continue
        raw = sum(idf(t) * (1 + math.log(hits[t])) for t in terms if hits[t])
        raw *= (1 + 0.5 * distinct)              # reward covering MORE keywords
        raw /= math.sqrt(max(len(rec["text"].split()), 1))   # length-normalise
        scored.append((raw, rec))

    scored.sort(key=lambda x: (-x[0], x[1]["book_title"], x[1]["page"], x[1]["para_idx"]))
    cap = max(1, min(int(limit or settings.resource_search_limit),
                     settings.resource_search_limit))
    width = settings.resource_snippet_chars
    top = scored[:cap]
    results = [{
        "book_id": rec["book_id"],
        "book_title": rec["book_title"],
        "page": rec["page"],
        "para_idx": rec["para_idx"],
        "score": round(score, 2),
        "snippet": _snippet(rec["text"], terms, width),
    } for score, rec in top]

    out = {
        "query": query,
        "match_count": len(scored),
        "showing": len(results),
        "results": results,
        "note": (f"Top {len(results)} of {len(scored)} — refine keywords, or call "
                 "get_resource_page(book_id, page) for the full page."
                 if len(scored) > len(results) else None),
    }
    if not scored:
        out["note"] = ("No matching paragraphs. Try different keywords, or call "
                       "list_resources to see what's available.")
    _shrink_to_budget(out)
    return out
