# -*- coding: utf-8 -*-
"""DocuBridge DB state/shipments — Phase 2."""
import json
from typing import Optional, Dict, Tuple

import psycopg2
import psycopg2.extras

from docubridge_config import DB_URL
from docubridge_helpers import generate_ticket_candidate
from docubridge_db_pool import get_conn, return_conn, db_run, discard_conn, RETRYABLE_DB_ERRORS

DB_ERROR_STATE = "__db_error__"


def _drop_conn(conn):
    """Close a broken connection instead of returning it to the pool."""
    discard_conn(conn)


def _run(label, fn, default=None, retry=True):
    return db_run(label, fn, default, retry, getconn=lambda: get_conn(), putconn=lambda c: return_conn(c))


class AmbiguousCommit(Exception):
    """Connection lost during COMMIT: the write may or may not have landed — never auto-retry."""


def get_state(chat_id: int) -> Tuple[str, Dict]:
    """Return (state, data).

    No DB configured / no row -> ("greeting", {}).
    DB failure: logged, retried once on a fresh connection; if it still fails
    returns (DB_ERROR_STATE, {}) so the dialog does NOT silently reset the funnel.
    """
    if not DB_URL:
        return ("greeting", {})
    last_err = None
    for attempt in (1, 2):
        conn = None
        try:
            conn = get_conn()
            if not conn:
                raise RuntimeError("no DB connection")
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT state, data FROM user_state WHERE chat_id = %s", (int(chat_id),))
            row = cur.fetchone()
            cur.close()
            return_conn(conn)
            conn = None
            return (row["state"], row["data"] or {}) if row else ("greeting", {})
        except Exception as e:
            last_err = e
            print(f"[DB] get_state error (attempt {attempt}/2, chat {chat_id}): {e}")
            if conn:
                _drop_conn(conn)
                conn = None
        finally:
            if conn:
                return_conn(conn)
    print(f"[DB] get_state FAILED for chat {chat_id} — state unknown, not resetting: {last_err}")
    return (DB_ERROR_STATE, {})


def set_state(chat_id: int, state: str, data: Optional[Dict] = None) -> bool:
    """Upsert state; retried once on a fresh connection. Returns True on success."""
    print(f"[Funnel] chat={chat_id} state={state}")
    if not DB_URL:
        return True
    payload = json.dumps(data or {}, ensure_ascii=False)
    for attempt in (1, 2):
        conn = None
        try:
            conn = get_conn()
            if not conn:
                raise RuntimeError("no DB connection")
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO user_state (chat_id, state, data, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (chat_id) DO UPDATE
                  SET state = EXCLUDED.state,
                      data  = COALESCE(EXCLUDED.data, user_state.data),
                      updated_at = NOW()
                """,
                (int(chat_id), state, payload),
            )
            conn.commit()
            cur.close()
            return_conn(conn)
            conn = None
            return True
        except Exception as e:
            print(f"[DB] set_state error (attempt {attempt}/2, chat {chat_id}, state {state}): {e}")
            if conn:
                _drop_conn(conn)
                conn = None
        finally:
            if conn:
                return_conn(conn)
    print(f"[DB] set_state FAILED for chat {chat_id} (state {state})")
    return False


def update_data(chat_id: int, new_data: Dict):
    if not DB_URL:
        return

    def _do(conn):
        cur = conn.cursor()
        cur.execute(
            "UPDATE user_state SET data = %s, updated_at = NOW() WHERE chat_id = %s",
            (json.dumps(new_data, ensure_ascii=False), int(chat_id)),
        )
        conn.commit()
        cur.close()
        return True

    _run("update_data", _do)


def create_shipment(chat_id: int, payload: Dict) -> Optional[str]:
    """Insert shipment + lead; return ticket or None."""
    if not DB_URL:
        # Offline fallback ticket (still unique enough for demo)
        ticket = generate_ticket_candidate()
        payload["ticket"] = ticket
        return ticket

    def _do(conn):
        cur = conn.cursor()
        ticket = None
        for _ in range(12):
            candidate = generate_ticket_candidate()
            try:
                cur.execute(
                    """
                    INSERT INTO shipments (ticket, chat_id, payload, status)
                    VALUES (%s, %s, %s, 'accepted')
                    """,
                    (candidate, int(chat_id), psycopg2.extras.Json(payload)),
                )
                ticket = candidate
                break
            except psycopg2.IntegrityError:
                conn.rollback()
                continue
        if not ticket:
            cur.close()
            return None
        lead = dict(payload)
        lead["ticket"] = ticket
        cur.execute(
            "INSERT INTO leads (chat_id, payload, ticket) VALUES (%s, %s, %s)",
            (int(chat_id), psycopg2.extras.Json(lead), ticket),
        )
        cur.execute(
            "INSERT INTO shipment_events (ticket, status, comment) VALUES (%s, 'accepted', NULL)",
            (ticket,),
        )
        try:
            conn.commit()
        except RETRYABLE_DB_ERRORS as e:
            raise AmbiguousCommit(f"commit of {ticket} lost: {e}")
        cur.close()
        return ticket

    # Errors before COMMIT roll back server-side → one retry on a fresh connection is safe.
    return _run("create_shipment", _do, default=None)


def get_shipment_by_ticket(ticket: str) -> Optional[Dict]:
    if not DB_URL:
        return None
    def _do(conn):
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT ticket, chat_id, payload, status, created_at, updated_at
              FROM shipments
             WHERE UPPER(ticket) = UPPER(%s)
            """,
            (ticket.strip(),),
        )
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None

    return _run("get_shipment_by_ticket", _do, default=None)
