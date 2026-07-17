"""Central configuration, loaded from environment / .env (never hardcoded)."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_local_test_keys() -> dict[str, str]:
    """Load test-only hardcoded keys when deployment env vars are unavailable."""
    try:
        from app.local_test_keys import TEST_KEYS  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return {}
    return {
        str(k).lower(): str(v).strip()
        for k, v in TEST_KEYS.items()
        if v is not None and str(v).strip()
    }


_LOCAL_TEST_KEYS = _load_local_test_keys()


def _test_key(name: str, default: str = "") -> str:
    return _LOCAL_TEST_KEYS.get(name.lower(), default)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Cerebras AI ---
    cerebras_api_key: str = _test_key("CEREBRAS_API_KEY")
    cerebras_api_key_secondary: str = _test_key("CEREBRAS_API_KEY_SECONDARY")
    default_model: str = "gpt-oss-120b"
    fallback_model: str = "zai-glm-4.7"

    # --- Groq AI (fallback provider; also supports function calling) ---
    groq_api_keys: str = _test_key("GROQ_API_KEYS")  # comma-separated; tried in order
    groq_model: str = "openai/gpt-oss-120b"
    groq_fallback_model: str = "openai/gpt-oss-20b"

    # --- Alerts (optional) ---
    alert_email: str = _test_key("ALERT_EMAIL")
    alert_email_password: str = _test_key("ALERT_EMAIL_PASSWORD")
    smtp_host: str = _test_key("SMTP_HOST", "smtp.gmail.com")
    smtp_port: int = int(_test_key("SMTP_PORT", "587") or "587")
    slack_webhook_url: str = _test_key("SLACK_WEBHOOK_URL")
    security_alert_email: str = _test_key("SECURITY_ALERT_EMAIL", "arshabmohan3@gmail.com")  # login-alert recipient

    # --- Self-defense (tune freely; all env-overridable) ---
    defense_enabled: bool = True
    defense_rate_max: int = 100        # requests / 10s / IP before it's flooding
    defense_login_max_fails: int = 5   # failed logins / 5min before brute force
    defense_distinct_users: int = 5    # distinct usernames / 5min before cred-stuffing
    defense_block_minutes: int = 15    # auto-block duration

    # --- Admin dashboard (separate login for /admin) ---
    admin_username: str = "admin"
    admin_password: str = "change-me"

    # --- Scanner ---
    scan_timeout: float = 12.0
    scan_max_concurrency: int = 8
    user_agent: str = (
        "Talos-Security-Scanner/1.0 "
        "(+authorized security assessment; non-destructive checks)"
    )

    # --- Brute-force detector ---
    auth_log_path: str = "auth.log"
    attempt_threshold: int = 5

    # --- Research APIs (OpenAlex / Semantic Scholar / CORE) ---
    openalex_api_key: str = _test_key("OPENALEX_API_KEY")
    core_api_key: str = _test_key("CORE_API_KEY")
    semantic_scholar_api_key: str = _test_key("SEMANTIC_SCHOLAR_API_KEY")
    research_timeout: float = 12.0
    contact_email: str = "research@talos.local"

    # --- Firebase (auth + Firestore) ---
    firebase_credentials: str = "key.json"
    firebase_project_id: str = ""
    auth_required: bool = False  # when True, /api/chat requires a valid Firebase ID token

    # --- Server ---
    host: str = "127.0.0.1"
    port: int = 8000

    # --- Resource library (uploaded books -> searchable, page-numbered markdown) ---
    resources_dir: str = "resources"      # on-disk library root (one folder per book)
    max_upload_mb: int = 50               # reject larger uploads
    max_pdf_pages: int = 2000             # cap pages parsed per book
    resource_search_limit: int = 20       # max paragraph matches returned to the AI
    resource_snippet_chars: int = 400     # max chars per search snippet
    resource_chars_per_page: int = 3000   # pseudo-pagination for .txt/.md without page breaks

    # --- OCR for scanned / image-only PDFs (needs the Tesseract binary installed) ---
    ocr_enabled: bool = True               # OCR pages that have no embedded text layer
    ocr_dpi: int = 300                     # render resolution for OCR (higher = slower)
    ocr_lang: str = "eng"                  # Tesseract language(s), e.g. "eng+fra"
    ocr_max_pages: int = 100               # cap OCR'd pages per book (OCR is slow)
    tesseract_path: str = ""               # explicit path to tesseract(.exe), optional

    @property
    def ai_enabled(self) -> bool:
        return bool(self.cerebras_api_key or self.groq_keys())

    def groq_keys(self) -> list[str]:
        """All configured Groq keys, in order (for failover)."""
        return [k.strip() for k in (self.groq_api_keys or "").split(",") if k.strip()]

    @property
    def email_enabled(self) -> bool:
        return bool(self.alert_email and self.alert_email_password)

    def api_keys(self) -> list[str]:
        """All configured Cerebras keys, primary first (for failover)."""
        return [k for k in (self.cerebras_api_key, self.cerebras_api_key_secondary) if k]


@lru_cache
def get_settings() -> Settings:
    return Settings()
