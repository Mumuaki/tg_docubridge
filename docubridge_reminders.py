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


_thread_lock = threading.Lock()
_thread = None
_thread_pid = None
_last_tick = 0.0          # heartbeat of the loop (time.time()), for /health


def _loop(initial_delay: float = 60.0):
    global _last_tick
    _last_tick = time.time()
    time.sleep(initial_delay)
    while True:
        _last_tick = time.time()
        try:
            run_reminders_once()
        except Exception as e:  # never let the thread die
            print(f"[Remind] loop error: {e}")
        _last_tick = time.time()
        time.sleep(CHECK_EVERY_S)


def start_reminder_thread():
    """Start the reminder loop — exactly one per process (lock + PID check).

    After a fork the parent's thread does not exist in the child, so a PID change
    always starts a fresh thread here. Double sends across processes are prevented
    in the DB by claim_reminder()."""
    global _thread, _thread_pid
    if not reminders_enabled():
        print("[Remind] disabled (DOCUBRIDGE_REMINDERS=0)")
        return None
    pid = os.getpid()
    with _thread_lock:
        if _thread is not None and _thread_pid == pid and _thread.is_alive():
            return _thread
        t = threading.Thread(target=_loop, name="docubridge-reminders", daemon=True)
        t.start()
        _thread, _thread_pid = t, pid
    print(f"[Remind] thread started (pid {pid}): after {REMIND_AFTER_MIN} min idle, "
          f"max age {REMIND_MAX_AGE_H} h, once per chat")
    return t


def reminder_health() -> str:
    """'disabled' | 'alive' | 'dead' (not started in this process, died, or hung)."""
    if not reminders_enabled():
        return "disabled"
    t = _thread
    if t is None or _thread_pid != os.getpid() or not t.is_alive():
        return "dead"
    if time.time() - _last_tick > CHECK_EVERY_S * 2 + 120:
        return "dead"
    return "alive"
