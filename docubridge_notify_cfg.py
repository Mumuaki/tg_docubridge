# -*- coding: utf-8 -*-
"""Lead-notification settings. Everything is read from the environment at call
time, so each feature is switched on/off in Render without a code change.

  LEADS_CHAT_ID        group/supergroup id (negative) for lead cards; empty → ADMIN_CHAT_ID
  LEADS_MANAGER_IDS    extra Telegram user ids allowed to press status buttons (comma list)
  NOTIFY_TEST_LEADS=1  admin test orders also go to LEADS_CHAT_ID with «🧪 ТЕСТ» (0 = skip)
  NOTIFY_OUTBOX=1      DB outbox with retries (0 = old direct one-shot send)
  NOTIFY_LOOP_S=60     outbox / digest loop period
  NOTIFY_MAX_ATTEMPTS=10
  Email (off until configured; NOTIFY_EMAIL=0 forces off):
    LEADS_EMAIL_TO + RESEND_API_KEY [+ EMAIL_FROM]            — HTTPS, works on Render free
    LEADS_EMAIL_TO + SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASSWORD [+ SMTP_FROM]
                                                              — SMTP, paid Render plans only
  DIGEST_ENABLED=1, DIGEST_HOUR=9, DIGEST_TZ=Europe/Budapest
"""
import os
from typing import List, Optional, Set

import docubridge_config as cfg


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def _flag(name: str, default: bool) -> bool:
    v = _env(name)
    if not v:
        return default
    return v.lower() not in {"0", "false", "no", "off"}


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def admin_id(override=None) -> int:
    try:
        return int(override if override is not None else (cfg.ADMIN_CHAT_ID or 0))
    except (TypeError, ValueError):
        return 0


def leads_chat_id() -> int:
    """LEADS_CHAT_ID as int, 0 if unset/invalid."""
    try:
        return int(_env("LEADS_CHAT_ID") or 0)
    except ValueError:
        return 0


def notify_target(admin_override=None) -> int:
    return leads_chat_id() or admin_id(admin_override)


def manager_ids() -> Set[int]:
    out = set()
    for part in _env("LEADS_MANAGER_IDS").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


def is_test_lead(chat_id, admin_override=None) -> bool:
    a = admin_id(admin_override)
    try:
        return bool(a) and int(chat_id) == a
    except (TypeError, ValueError):
        return False


def notify_test_leads() -> bool:
    return _flag("NOTIFY_TEST_LEADS", True)


def telegram_plan(chat_id, admin_override=None) -> Optional[str]:
    """'send' | 'skip_test' | 'no_target' for the Telegram lead card."""
    if not notify_target(admin_override):
        return "no_target"
    if is_test_lead(chat_id, admin_override):
        return "send" if (leads_chat_id() and notify_test_leads()) else "skip_test"
    return "send"


def outbox_enabled() -> bool:
    return _flag("NOTIFY_OUTBOX", True) and bool(cfg.DB_URL)


def loop_period_s() -> int:
    return max(5, _int("NOTIFY_LOOP_S", 60))


def max_attempts() -> int:
    return max(1, _int("NOTIFY_MAX_ATTEMPTS", 10))


# ------------ email ------------

def email_to() -> List[str]:
    return [a.strip() for a in _env("LEADS_EMAIL_TO").replace(";", ",").split(",") if a.strip()]


def email_provider() -> Optional[str]:
    """'resend' | 'smtp' | None (email off)."""
    if not _flag("NOTIFY_EMAIL", True) or not email_to():
        return None
    if _env("RESEND_API_KEY"):
        return "resend"
    if _env("SMTP_HOST") and _env("SMTP_USER") and _env("SMTP_PASSWORD"):
        return "smtp"
    return None


def email_enabled() -> bool:
    return email_provider() is not None


# ------------ digest ------------

def digest_enabled() -> bool:
    return _flag("DIGEST_ENABLED", True)


def digest_hour() -> int:
    h = _int("DIGEST_HOUR", 9)
    return h if 0 <= h <= 23 else 9


def digest_tz():
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(_env("DIGEST_TZ") or "Europe/Budapest")
    except Exception:
        return ZoneInfo("Europe/Budapest")
