# -*- coding: utf-8 -*-
"""One reminder per chat for abandoned orders (Render free: 1 gunicorn worker,
kept awake by UptimeRobot → a daemon thread checking every few minutes is enough).

Env (optional):
  DOCUBRIDGE_REMINDERS=0          — disable
  DOCUBRIDGE_REMIND_AFTER_MIN=90  — idle minutes before the reminder
"""
import os
import threading
import time

from docubridge_runtime import *  # noqa: F401,F403
from docubridge_funnel import REMIND_STATES, REMINDER_TEXT, STEP_TITLES_RU, prompt_step

REMIND_AFTER_MIN = int(os.getenv("DOCUBRIDGE_REMIND_AFTER_MIN", "90") or 90)
REMIND_MAX_AGE_H = 48  # older stuck chats (e.g. the old city-first funnel) are never pinged
CHECK_EVERY_S = 300


def reminders_enabled() -> bool:
    return os.getenv("DOCUBRIDGE_REMINDERS", "1").strip().lower() not in {"0", "false", "no", "off"}


def run_reminders_once() -> int:
    """Send due reminders; returns how many were sent."""
    sent = 0
    for row in find_reminder_candidates(REMIND_STATES, REMIND_AFTER_MIN, REMIND_MAX_AGE_H):
        chat_id, state, data = int(row["chat_id"]), row["state"], dict(row.get("data") or {})
        if ADMIN_CHAT_ID and chat_id == int(ADMIN_CHAT_ID):
            continue
        if not claim_reminder(chat_id, state):
            continue
        try:
            prompt_step(chat_id, state, data,
                        prefix=REMINDER_TEXT.format(step=STEP_TITLES_RU.get(state, state)))
            sent += 1
            print(f"[Remind] chat={chat_id} reminded on {state}")
        except Exception as e:  # user blocked the bot, etc. — already marked, never retried
            print(f"[Remind] chat={chat_id} send failed: {e}")
    return sent


def _loop():
    time.sleep(60)
    while True:
        try:
            run_reminders_once()
        except Exception as e:
            print(f"[Remind] loop error: {e}")
        time.sleep(CHECK_EVERY_S)


def start_reminder_thread():
    if not reminders_enabled():
        print("[Remind] disabled (DOCUBRIDGE_REMINDERS=0)")
        return None
    t = threading.Thread(target=_loop, name="docubridge-reminders", daemon=True)
    t.start()
    print(f"[Remind] thread started: after {REMIND_AFTER_MIN} min idle, max age {REMIND_MAX_AGE_H} h, once per chat")
    return t
