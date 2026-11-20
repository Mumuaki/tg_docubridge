# -*- coding: utf-8 -*-
"""Lead e-mail copy: Resend HTTPS API (works on Render free, which blocks SMTP
ports 25/465/587) or plain SMTP (SSL on 465, STARTTLS otherwise) for paid plans.
Secrets (API key, SMTP password) are never logged or put into error texts."""
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import List, Optional

import docubridge_notify_cfg as ncfg

EMAIL_TIMEOUT_S = 10
RESEND_URL = "https://api.resend.com/emails"
DEFAULT_RESEND_FROM = "DocuBridge <onboarding@resend.dev>"


class EmailError(Exception):
    def __init__(self, msg: str, retryable: bool = True, retry_after: Optional[float] = None):
        super().__init__(msg)
        self.retryable = retryable
        self.retry_after = retry_after


def _scrub(text: str) -> str:
    s = str(text or "")
    for name in ("RESEND_API_KEY", "SMTP_PASSWORD"):
        v = (os.getenv(name) or "").strip()
        if v:
            s = s.replace(v, "***")
    return s[:200]


def send_resend(to: List[str], subject: str, body: str, idempotency_key: str) -> str:
    import requests
    headers = {
        "Authorization": f"Bearer {(os.getenv('RESEND_API_KEY') or '').strip()}",
        "Content-Type": "application/json",
        # Resend drops a repeated request with the same key (24 h): retries never duplicate.
        "Idempotency-Key": idempotency_key[:256],
    }
    payload = {"from": (os.getenv("EMAIL_FROM") or "").strip() or DEFAULT_RESEND_FROM,
               "to": to, "subject": subject, "text": body}
    try:
        r = requests.post(RESEND_URL, json=payload, headers=headers, timeout=EMAIL_TIMEOUT_S)
    except requests.RequestException as e:
        raise EmailError(f"resend network: {type(e).__name__}") from None
    if r.status_code in (200, 201):
        try:
            return str(r.json().get("id") or "")
        except ValueError:
            return ""
    retry_after = None
    if r.status_code == 429:
        try:
            retry_after = float(r.headers.get("retry-after") or 0) or None
        except ValueError:
            retry_after = None
    retryable = r.status_code == 429 or r.status_code >= 500
    raise EmailError(f"resend HTTP {r.status_code}: {_scrub(r.text)}", retryable, retry_after)


def send_smtp(to: List[str], subject: str, body: str) -> str:
    host = (os.getenv("SMTP_HOST") or "").strip()
    port = int((os.getenv("SMTP_PORT") or "587").strip() or 587)
    user = (os.getenv("SMTP_USER") or "").strip()
    password = os.getenv("SMTP_PASSWORD") or ""
    msg = EmailMessage()
    msg["From"] = (os.getenv("SMTP_FROM") or "").strip() or user
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=EMAIL_TIMEOUT_S, context=ctx) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=EMAIL_TIMEOUT_S) as s:
                s.starttls(context=ctx)
                s.login(user, password)
                s.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise EmailError(f"smtp auth failed ({e.smtp_code})", retryable=False) from None
    except smtplib.SMTPResponseException as e:
        raise EmailError(f"smtp {e.smtp_code}", retryable=e.smtp_code >= 400 and e.smtp_code < 500) from None
    except (OSError, smtplib.SMTPException) as e:
        raise EmailError(f"smtp {type(e).__name__}: {_scrub(e)}") from None
    return msg.get("Message-ID") or ""


def send_lead_email(subject: str, body: str, idempotency_key: str) -> str:
    """Returns provider message id. Raises EmailError (retryable or not)."""
    provider = ncfg.email_provider()
    to = ncfg.email_to()
    if provider == "resend":
        return send_resend(to, subject, body, idempotency_key)
    if provider == "smtp":
        return send_smtp(to, subject, body)
    raise EmailError("email not configured", retryable=False)
