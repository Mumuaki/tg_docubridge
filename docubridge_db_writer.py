# -*- coding: utf-8 -*-
"""Background write queue for DB writes that do not affect the reply to the user
(chat_history, /start events, «update processed» marks).

* One daemon thread per process (re-created after fork), bounded queue.
* Queue full → the write runs inline (back-pressure, nothing is dropped).
* Errors are logged and never propagate (each job already uses db_run with retry).
* flush() on worker exit (gunicorn worker_exit hook) and at interpreter exit.
* DOCUBRIDGE_ASYNC_WRITES=0 → every write runs inline (old behaviour).
"""
import atexit
import os
import queue
import threading
import time
from typing import Callable

WRITE_QUEUE_MAX = int(os.getenv("DOCUBRIDGE_WRITE_QUEUE_MAX", "1000") or 1000)

_lock = threading.Lock()
_q = None
_thread = None
_pid = None
_stats = {"done": 0, "failed": 0, "inline": 0}


def async_enabled() -> bool:
    return os.getenv("DOCUBRIDGE_ASYNC_WRITES", "1").strip().lower() not in {"0", "false", "no", "off"}


def _run_job(label: str, fn: Callable[[], object]):
    try:
        fn()
        _stats["done"] += 1
    except Exception as e:  # never kill the writer
        _stats["failed"] += 1
        print(f"[DBW] {label} failed: {e}")


def _worker(q):
    while True:
        label, fn = q.get()
        try:
            _run_job(label, fn)
        finally:
            q.task_done()


def _ensure_thread():
    """Queue + thread for the current PID (an inherited queue/thread is abandoned)."""
    global _q, _thread, _pid
    pid = os.getpid()
    with _lock:
        if _q is not None and _pid == pid and _thread is not None and _thread.is_alive():
            return _q
        if _q is None or _pid != pid:
            _q = queue.Queue(maxsize=WRITE_QUEUE_MAX)
        t = threading.Thread(target=_worker, args=(_q,), name="docubridge-db-writer", daemon=True)
        t.start()
        _thread, _pid = t, pid
        return _q


def submit(label: str, fn: Callable[[], object]) -> bool:
    """Queue fn() for the background writer. Returns True if queued, False if run inline."""
    if not async_enabled():
        _run_job(label, fn)
        return False
    try:
        q = _ensure_thread()
        q.put_nowait((label, fn))
        return True
    except queue.Full:
        _stats["inline"] += 1
        print(f"[DBW] queue full ({WRITE_QUEUE_MAX}) — {label} written inline")
    except Exception as e:
        print(f"[DBW] enqueue failed ({e}) — {label} written inline")
    _run_job(label, fn)
    return False


def pending() -> int:
    q = _q
    return q.unfinished_tasks if (q is not None and _pid == os.getpid()) else 0


def flush(timeout: float = 5.0) -> bool:
    """Wait until queued writes are done (True) or timeout (False)."""
    q = _q
    if q is None or _pid != os.getpid():
        return True
    deadline = time.monotonic() + timeout
    with q.all_tasks_done:
        while q.unfinished_tasks:
            left = deadline - time.monotonic()
            if left <= 0:
                print(f"[DBW] flush timeout: {q.unfinished_tasks} write(s) not done")
                return False
            q.all_tasks_done.wait(left)
    return True


atexit.register(flush)
