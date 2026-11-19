# -*- coding: utf-8 -*-
"""Google Sheet «Заявки DocuBridge» export — best effort, never breaks an order.

Env:
  GOOGLE_SA_JSON  service-account key: raw JSON *or* base64 of it (auto-detected)
  SHEET_ID        spreadsheet key from its URL; rows go to the first worksheet
  DOCUBRIDGE_SHEETS_SYNC_S=60  outbox sync period (seconds)

Reliability:
  * HTTP timeout (connect 5 s, read 15 s); up to 3 attempts on 429 / 5xx / network errors;
    the cached worksheet is dropped after any error (fresh auth next time).
  * Outbox: shipments.sheet_synced_at marks rows that are in the sheet. A background
    thread (one per process) exports anything missing once a minute — orders created
    while Google was down, during a deploy, or before the sheet was configured.
  * No duplicate rows: a DB lease (sheet_claimed_at) is taken before exporting and the
    ticket column is checked right before append_row.
  * Admin test orders (chat id == ADMIN_CHAT_ID) are exported too, marked «тест».
"""
import base64
import binascii
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import docubridge_config as cfg
from docubridge_db_outbox import (
    claim_sheet_sync, mark_sheet_synced, release_sheet_claim, list_unsynced_shipments,
)

SHEET_HEADER = [
    "Ticket", "Дата (UTC)", "Chat ID", "Роль", "Откуда", "Куда",
    "Тип документа", "Листов", "Вес, г", "Срочность", "Оценка, €",
    "Имя", "Телефон", "Источник", "Статус", "Обновлено (UTC)", "Комментарий",
]
STATUS_COL = SHEET_HEADER.index("Статус") + 1
UPDATED_COL = SHEET_HEADER.index("Обновлено (UTC)") + 1
COMMENT_COL = SHEET_HEADER.index("Комментарий") + 1
ROLE_IDX = SHEET_HEADER.index("Роль")
COMMENT_IDX = SHEET_HEADER.index("Комментарий")

TEST_MARK = "тест"
SHEETS_TIMEOUT = (5, 15)          # (connect, read) seconds for every Google API call
SHEETS_ATTEMPTS = 3
SHEETS_BACKOFF_S = (1.0, 3.0)     # sleeps between attempts
RETRY_HTTP_CODES = {429, 500, 502, 503, 504}
SYNC_EVERY_S = int(os.getenv("DOCUBRIDGE_SHEETS_SYNC_S", "60") or 60)
SYNC_BATCH = 50

_ws_cache = None
_lock = threading.RLock()         # one Google call sequence at a time in this process
_sleep = time.sleep               # patched in tests
_health = {"status": None, "error": None}   # None = not checked yet


# ------------ config ------------

def sheets_enabled() -> bool:
    return bool(os.getenv("GOOGLE_SA_JSON", "").strip() and os.getenv("SHEET_ID", "").strip())


def load_sa_info(raw: Optional[str]) -> Dict:
    """Parse GOOGLE_SA_JSON: raw JSON (starts with «{») or base64/base64url of it.
    Errors never contain the key itself."""
    s = (raw or "").strip()
    if not s:
        raise ValueError("GOOGLE_SA_JSON пуст")
    if s.startswith("{"):
        try:
            info = json.loads(s)
        except ValueError as e:
            raise ValueError(f"GOOGLE_SA_JSON: невалидный JSON (строка {getattr(e, 'lineno', '?')})") from None
    else:
        compact = "".join(s.split())
        compact += "=" * (-len(compact) % 4)
        try:
            decoded = base64.b64decode(compact.replace("-", "+").replace("_", "/"), validate=True)
            info = json.loads(decoded.decode("utf-8"))
        except (binascii.Error, ValueError, UnicodeDecodeError):
            raise ValueError("GOOGLE_SA_JSON: не JSON и не base64 от JSON") from None
    if not isinstance(info, dict) or info.get("type") != "service_account" or not info.get("private_key"):
        raise ValueError("GOOGLE_SA_JSON: это не ключ сервисного аккаунта (type/private_key)")
    return info


def is_test_chat(chat_id) -> bool:
    try:
        return bool(cfg.ADMIN_CHAT_ID) and int(chat_id) == int(cfg.ADMIN_CHAT_ID)
    except (TypeError, ValueError):
        return False


# ------------ Google access with timeout + retry ------------

def _get_worksheet():
    global _ws_cache
    if _ws_cache is not None:
        return _ws_cache
    import gspread
    from google.oauth2.service_account import Credentials

    info = load_sa_info(os.getenv("GOOGLE_SA_JSON"))
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    gc = gspread.authorize(creds)
    gc.http_client.set_timeout(SHEETS_TIMEOUT)
    ws = gc.open_by_key(os.getenv("SHEET_ID").strip()).sheet1
    if not ws.row_values(1):
        ws.append_row(SHEET_HEADER, value_input_option="USER_ENTERED")
    _ws_cache = ws
    return ws


def _status_code(exc) -> Optional[int]:
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    if code is None:
        code = getattr(exc, "code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def is_retryable(exc) -> bool:
    """429 / 5xx from Google, or a network-level error/timeout."""
    code = _status_code(exc)
    if code is not None:
        return code in RETRY_HTTP_CODES
    try:
        import requests
        if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
            return True
    except ImportError:  # pragma: no cover
        pass
    return isinstance(exc, (TimeoutError, ConnectionError))


def _short(exc) -> str:
    code = _status_code(exc)
    msg = str(exc).replace("\n", " ")[:200]
    return f"{type(exc).__name__}{f' {code}' if code else ''}: {msg}"


def _set_health(ok: bool, exc=None):
    _health["status"] = "ok" if ok else "error"
    _health["error"] = None if ok else _short(exc)


def with_retry(label: str, fn):
    """Run fn(ws) with a fresh-on-error worksheet; retries only transient errors."""
    global _ws_cache
    last = None
    for attempt in range(SHEETS_ATTEMPTS):
        try:
            with _lock:
                result = fn(_get_worksheet())
            _set_health(True)
            return result
        except Exception as e:
            last = e
            _ws_cache = None            # never keep a possibly broken client
            if not is_retryable(e) or attempt == SHEETS_ATTEMPTS - 1:
                break
            delay = SHEETS_BACKOFF_S[min(attempt, len(SHEETS_BACKOFF_S) - 1)]
            print(f"[Sheets] {label}: {_short(e)} — retry {attempt + 2}/{SHEETS_ATTEMPTS} in {delay:.0f}s")
            _sleep(delay)
    _set_health(False, last)
    raise last


def sheets_health() -> str:
    """'off' | 'ok' | 'error' | 'pending' (enabled, first check not finished yet)."""
    if not sheets_enabled():
        return "off"
    return _health["status"] or "pending"


def sheets_check() -> bool:
    """Startup check with a clear log line: [Sheets] OK … / [Sheets] ERROR: reason."""
    if not sheets_enabled():
        print("[Sheets] off: GOOGLE_SA_JSON/SHEET_ID не заданы — экспорт выключен")
        return False
    try:
        title = with_retry("check", lambda ws: getattr(ws, "title", "?"))
        print(f"[Sheets] OK: лист «{title}», колонок {len(SHEET_HEADER)}")
        return True
    except Exception as e:
        hint = ""
        code = _status_code(e)
        if code in (403, 404):
            hint = " — откройте доступ «Редактор» для client_email сервисного аккаунта и проверьте SHEET_ID"
        print(f"[Sheets] ERROR: {_short(e)}{hint}")
        return False


# ------------ rows ------------

def _fmt_utc(dt) -> str:
    if dt is None:
        dt = datetime.now(timezone.utc)
    try:
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(dt)


def _now() -> str:
    return _fmt_utc(None)


def build_row(chat_id: int, payload: Dict, price_eur: Optional[int] = None,
              status_ru: str = "принято", created_at=None, updated_at=None,
              comment: str = "") -> list:
    p = payload or {}
    row = [
        p.get("ticket", ""), _fmt_utc(created_at), str(chat_id), p.get("role", ""),
        p.get("from_country", ""), p.get("to_country", ""), p.get("doc_type", ""),
        p.get("pages_a4", ""), p.get("weight_grams", ""), p.get("urgency", ""),
        "" if price_eur is None else price_eur, p.get("name", ""),
        # leading ' keeps "+380…" as text
        ("'" + p["phone"]) if p.get("phone") else "",
        p.get("ref") or p.get("start_payload") or "", status_ru,
        _fmt_utc(updated_at or created_at), comment or "",
    ]
    if is_test_chat(chat_id):
        role = row[ROLE_IDX]
        row[ROLE_IDX] = f"{TEST_MARK}: {role}" if role else TEST_MARK
        row[COMMENT_IDX] = f"{TEST_MARK} (заявка администратора)" + (f"; {comment}" if comment else "")
    return row


def _row_from_shipment(sh: Dict, comment: str = "") -> list:
    from docubridge_helpers import compute_quote, normalize_status, TRACK_STATUS_RU
    payload = dict(sh.get("payload") or {})
    payload.setdefault("ticket", sh.get("ticket"))
    price = sh.get("price_eur")
    if price is None:
        try:
            price = compute_quote(payload).get("price_eur")
        except Exception:
            price = None
    st = normalize_status(sh.get("status")) or "accepted"
    return build_row(sh.get("chat_id"), payload, price, TRACK_STATUS_RU.get(st, st),
                     sh.get("created_at"), sh.get("updated_at"), comment)


def _ticket_row(ws, ticket: str) -> Optional[int]:
    """1-based sheet row of `ticket` in column A (exact match), or None."""
    values = ws.col_values(1)
    if not isinstance(values, list):
        return None
    t = (ticket or "").strip().upper()
    for i, v in enumerate(values, start=1):
        if str(v).strip().upper() == t:
            return i
    return None


def _append_if_missing(row: list) -> str:
    def _do(ws):
        if _ticket_row(ws, row[0]):
            return "exists"
        ws.append_row(row, value_input_option="USER_ENTERED")
        return "appended"
    return with_retry(f"append {row[0]}", _do)


def sync_shipment(sh: Dict) -> bool:
    """Export one shipment (outbox step). True if it is in the sheet now."""
    ticket = sh.get("ticket")
    if not ticket or not sheets_enabled():
        return False
    if not claim_sheet_sync(ticket):
        return False                       # already synced or another worker is on it
    try:
        res = _append_if_missing(_row_from_shipment(sh))
    except Exception as e:
        release_sheet_claim(ticket)
        print(f"[Sheets] {ticket} export failed (will retry): {_short(e)}")
        return False
    mark_sheet_synced(ticket)
    print(f"[Sheets] {ticket} {'appended' if res == 'appended' else 'already in sheet'}")
    return True


def sync_pending_once(limit: int = SYNC_BATCH) -> int:
    """Export shipments not yet in the sheet; stops at the first failure (Google down)."""
    if not sheets_enabled():
        return 0
    done = 0
    for sh in list_unsynced_shipments(limit):
        if sync_shipment(sh):
            done += 1
        elif sheets_health() == "error":
            break
    if done:
        print(f"[Sheets] outbox: exported {done}")
    return done


def _update_status(ticket: str, status_ru: str, comment: Optional[str]):
    def _do(ws):
        r = _ticket_row(ws, ticket)
        if not r:
            return None
        from gspread.utils import rowcol_to_a1
        last_col = COMMENT_COL if comment else UPDATED_COL
        values = [status_ru, _now()] + ([comment] if comment else [])
        ws.batch_update([{
            "range": f"{rowcol_to_a1(r, STATUS_COL)}:{rowcol_to_a1(r, last_col)}",
            "values": [values],
        }], value_input_option="USER_ENTERED")
        return r

    try:
        r = with_retry(f"status {ticket}", _do)
    except Exception as e:
        print(f"[Sheets] status update skipped: {_short(e)}")
        return
    if r:
        print(f"[Sheets] {ticket} → {status_ru}")
        return
    # Row missing (export lost earlier, or deleted by hand): append the full current
    # row from the DB (status already updated there). The ticket-column check inside
    # _append_if_missing keeps it single even if the outbox runs at the same time.
    from docubridge_db_state import get_shipment_by_ticket
    sh = get_shipment_by_ticket(ticket)
    if not sh:
        print(f"[Sheets] {ticket} not in sheet and not in DB — status not written")
        return
    try:
        _append_if_missing(_row_from_shipment(sh, comment or ""))
        mark_sheet_synced(sh["ticket"])
        print(f"[Sheets] {ticket} was missing — appended with current status")
    except Exception as e:
        print(f"[Sheets] {ticket} was missing, append failed (outbox will retry): {_short(e)}")


# ------------ async entry points (never raise) ------------

def _spawn(target, *args):
    t = threading.Thread(target=target, args=args, daemon=True)
    t.start()
    return t


def _export_new(chat_id: int, payload: Dict, price_eur: Optional[int]):
    try:
        sh = {"ticket": payload.get("ticket"), "chat_id": chat_id, "payload": payload,
              "status": "accepted", "created_at": None, "updated_at": None,
              "price_eur": price_eur}
        sync_shipment(sh)
    except Exception as e:
        print(f"[Sheets] append skipped: {_short(e)}")


def export_shipment_async(chat_id: int, payload: Dict, price_eur: Optional[int] = None):
    if not sheets_enabled():
        print("[Sheets] GOOGLE_SA_JSON/SHEET_ID не заданы — экспорт пропущен (догрузится после включения)")
        return None
    try:
        return _spawn(_export_new, chat_id, dict(payload or {}), price_eur)
    except Exception as e:
        print(f"[Sheets] export skipped: {e}")
        return None


def update_status_async(ticket: str, status_ru: str, comment: Optional[str] = None):
    if not sheets_enabled():
        return None
    try:
        return _spawn(_update_status, ticket, status_ru, comment)
    except Exception as e:
        print(f"[Sheets] status update skipped: {e}")
        return None


# ------------ background outbox thread (one per process) ------------

_thread_lock = threading.Lock()
_thread = None
_thread_pid = None


def _loop():
    checked = False
    while True:
        try:
            if not checked:
                checked = sheets_check()
            sync_pending_once()
        except Exception as e:  # never let the thread die
            print(f"[Sheets] loop error: {_short(e)}")
        _sleep(SYNC_EVERY_S)


def start_sheets_thread():
    global _thread, _thread_pid
    if not sheets_enabled():
        if _health.get("logged_off") != os.getpid():
            print("[Sheets] off: GOOGLE_SA_JSON/SHEET_ID не заданы — экспорт выключен")
            _health["logged_off"] = os.getpid()
        return None
    pid = os.getpid()
    with _thread_lock:
        if _thread is not None and _thread_pid == pid and _thread.is_alive():
            return _thread
        t = threading.Thread(target=_loop, name="docubridge-sheets", daemon=True)
        t.start()
        _thread, _thread_pid = t, pid
    print(f"[Sheets] outbox thread started (pid {pid}): sync every {SYNC_EVERY_S}s")
    return t
