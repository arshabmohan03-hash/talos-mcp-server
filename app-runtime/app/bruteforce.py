"""Brute-force log analysis (migrated + hardened from the original detector).

Improvements over the original:
  * also catches "invalid user" lines and captures targeted usernames
  * no hardcoded secrets / no side-effecting email on import
  * graceful fallback if the ML model is missing
  * returns structured data for the AI tool / API instead of writing files
  * derives *genuine, independent* features (attempt count, username spread,
    privileged-account targeting) and runs both a supervised classifier and an
    unsupervised anomaly detector — no more circular `login_rate`/`time_gap`.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import joblib

from app.config import get_settings

# Matches both "Failed password for root from 1.2.3.4" and
# "Failed password for invalid user admin from 1.2.3.4".
LINE_RE = re.compile(
    r"Failed password(?: for (?:invalid user )?(?P<user>\S+))? from (?P<ip>\d{1,3}(?:\.\d{1,3}){3})"
)

# Accounts an attacker disproportionately targets.
PRIVILEGED_USERS = {
    "root", "admin", "administrator", "oracle", "mysql", "postgres",
    "sa", "ftp", "www-data", "sysadmin",
}

# Feature order MUST match generate_dataset.py / train_model.py.
FEATURES = ["failed_attempts", "unique_users", "attempts_per_user", "targets_privileged"]

# attack_model.pkl lives at the project root, regardless of the process CWD.
_MODEL_PATH = Path(__file__).resolve().parent.parent / "attack_model.pkl"
_MODEL = None  # lazy; False once we know it's unavailable/incompatible


def _model():
    """Return the model bundle {classifier, anomaly, features} or None.

    Old-format pickles (a bare estimator with the legacy 3-feature shape) are
    ignored so they can't raise a feature-count error at predict time.
    """
    global _MODEL
    if _MODEL is None:
        try:
            obj = joblib.load(_MODEL_PATH)
            _MODEL = obj if isinstance(obj, dict) and "classifier" in obj else False
        except Exception:  # noqa: BLE001
            _MODEL = False
    return _MODEL or None


def _features(attempts: int, users: set[str]) -> list[float]:
    unique = max(1, len(users))
    return [
        float(attempts),
        float(unique),
        round(attempts / unique, 3),
        1.0 if users & PRIVILEGED_USERS else 0.0,
    ]


def analyze_log(path: str | None = None, threshold: int | None = None) -> dict:
    """Analyze an auth log for brute-force activity. Returns structured findings."""
    s = get_settings()
    path = path or s.auth_log_path
    threshold = threshold if threshold is not None else s.attempt_threshold

    p = Path(path)
    if not p.exists():
        return {"error": f"Log file not found: {path}"}

    counts: dict[str, int] = defaultdict(int)
    users: dict[str, set] = defaultdict(set)
    failed = 0
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = LINE_RE.search(line)
        if m:
            failed += 1
            ip = m.group("ip")
            counts[ip] += 1
            if m.group("user"):
                users[ip].add(m.group("user"))

    bundle = _model()
    results = []
    for ip, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        ip_users = users[ip]

        # Heuristic baseline (kept so detection works even without the model).
        if count >= threshold + 1:
            status = "attack"
        elif count >= max(1, threshold - 1):
            status = "suspicious"
        else:
            status = "normal"

        confidence = None
        anomaly = None
        if bundle is not None:
            try:
                feats = [_features(count, ip_users)]
                clf, iso = bundle["classifier"], bundle["anomaly"]
                pred = int(clf.predict(feats)[0])
                proba = float(clf.predict_proba(feats)[0][1])  # P(attack)
                confidence = round(proba * 100, 1)
                anomaly = bool(iso.predict(feats)[0] == -1)
                if pred == 1 and count >= max(1, threshold - 1):
                    status = "attack"
                elif anomaly and status == "normal":
                    status = "suspicious"
            except Exception:  # noqa: BLE001
                pass

        results.append({
            "ip": ip,
            "attempts": count,
            "status": status,
            "unique_users": len(ip_users),
            "targeted_usernames": sorted(ip_users)[:8],
            "ai_confidence": confidence,
            "anomaly": anomaly,
        })

    summary = {
        "total_failed_logins": failed,
        "unique_ips": len(counts),
        "attacks": sum(1 for r in results if r["status"] == "attack"),
        "suspicious": sum(1 for r in results if r["status"] == "suspicious"),
        "anomalies": sum(1 for r in results if r["anomaly"]),
    }
    return {
        "summary": summary,
        "results": results,
        "threshold": threshold,
        "log_path": str(p),
        "note": "Features (attempt count, distinct usernames, focus, privileged "
                "targeting) feed a RandomForest classifier plus an IsolationForest "
                "anomaly detector. Trained on synthetic data — advisory, not "
                "a substitute for real IDS telemetry.",
    }
