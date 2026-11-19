# -*- coding: utf-8 -*-
"""Gunicorn settings for DocuBridge (auto-loaded from the repo root: ./gunicorn.conf.py).

Command-line flags (Render start command) and GUNICORN_CMD_ARGS override these;
the app is fork-safe either way (lazy, per-PID DB pool / reminder thread / webhook).
"""
import os
import sys

bind = f"0.0.0.0:{os.getenv('PORT', '10000')}"
workers = 1              # one process: state in Postgres, one reminder loop
threads = 1
worker_class = "sync"
preload_app = False      # import the app in the worker, not in the master
timeout = 120
graceful_timeout = 30
keepalive = 5


def _line_buffered_logs():
    """print() lines reach Render logs immediately and in order."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass


_line_buffered_logs()


def post_fork(server, worker):
    _line_buffered_logs()


def post_worker_init(worker):
    """Runs inside the worker after the app is imported: start DB/webhook/reminders here."""
    try:
        import docubridge_handlers as h
        if not h.SKIP_STARTUP:
            st = h.ensure_started(force=True)
            worker.log.info("DocuBridge startup in worker pid %s: %s", os.getpid(), st)
    except Exception as e:  # never kill the worker: before_request retries later
        worker.log.error("DocuBridge startup failed (will retry on next request): %s", e)
