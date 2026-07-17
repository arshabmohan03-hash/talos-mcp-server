"""Send security alerts via email (SMTP) and/or Slack (incoming webhook).

Both channels are optional — configure them in .env. Nothing is sent unless a
channel is configured; the result says exactly what happened on each channel.
Functions are synchronous (smtplib/httpx blocking) — call via asyncio.to_thread.
"""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

import httpx

from app.config import get_settings


def _send_email(subject: str, body: str, to: str | None = None) -> tuple[bool, str]:
    s = get_settings()
    if not s.email_enabled:
        return False, "not configured"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s.alert_email
    msg["To"] = to or s.alert_email
    msg.set_content(body)
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as server:
            server.starttls(context=ctx)
            server.login(s.alert_email, s.alert_email_password)
            server.send_message(msg)
        return True, "sent"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _send_slack(text: str) -> tuple[bool, str]:
    s = get_settings()
    if not s.slack_webhook_url:
        return False, "not configured"
    try:
        r = httpx.post(s.slack_webhook_url, json={"text": text}, timeout=15)
        return (True, "sent") if r.status_code // 100 == 2 else (False, f"HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def send_alert(message: str, subject: str = "Talos security alert",
               channels: list[str] | None = None) -> dict:
    s = get_settings()
    channels = channels or ["email", "slack"]
    results: dict[str, str] = {}
    sent_any = False
    if "email" in channels:
        ok, info = _send_email(subject, message)
        results["email"] = info
        sent_any = sent_any or ok
    if "slack" in channels:
        ok, info = _send_slack(f"*{subject}*\n{message}")
        results["slack"] = info
        sent_any = sent_any or ok
    configured = []
    if s.email_enabled:
        configured.append("email")
    if s.slack_webhook_url:
        configured.append("slack")
    return {
        "sent": sent_any,
        "channels": results,
        "configured_channels": configured,
        "note": "" if configured else
                "No alert channel configured. Set ALERT_EMAIL + ALERT_EMAIL_PASSWORD "
                "(Gmail app password) and/or SLACK_WEBHOOK_URL in .env.",
    }


def send_security_alert(subject: str, body: str) -> dict:
    """Email the security-alert recipient (used by the self-defense engine)."""
    s = get_settings()
    recipient = s.security_alert_email or s.alert_email
    if not s.email_enabled or not recipient:
        return {"sent": False, "note": "SMTP sender not configured."}
    ok, info = _send_email(subject, body, to=recipient)
    return {"sent": ok, "recipient": recipient, "info": info}


def notify_login(email: str, ip: str | None = None, success: bool = True) -> dict:
    """Email the security-alert recipient whenever someone signs in to Talos."""
    from datetime import datetime
    s = get_settings()
    recipient = s.security_alert_email or s.alert_email
    if not s.email_enabled or not recipient:
        return {"sent": False, "recipient": recipient,
                "note": "Login alerts need a sender: set ALERT_EMAIL + "
                        "ALERT_EMAIL_PASSWORD (Gmail app password) in .env."}
    status = "successful sign-in" if success else "sign-in attempt"
    when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = (f"A {status} on your Talos instance.\n\n"
            f"Account: {email or 'unknown'}\nWhen: {when}\nFrom IP: {ip or 'unknown'}\n\n"
            f"If this wasn't you, secure the account and review access.")
    ok, info = _send_email(f"[Talos] {status}: {email}", body, to=recipient)
    return {"sent": ok, "recipient": recipient, "info": info}
