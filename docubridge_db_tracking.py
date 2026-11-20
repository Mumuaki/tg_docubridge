# -*- coding: utf-8 -*-
"""DocuBridge tracking persistence: admin lists, status updates, events (db_run, 1 retry)."""
from typing import Optional, Dict, List

import psycopg2
import psycopg2.extras

from docubridge_config import DB_URL
from docubridge_db_pool_a import get_conn, return_conn, db_run, begin_transaction


def _run(label, fn, default=None, retry=True):
    return db_run(label, fn, default, retry, getconn=lambda: get_conn(), putconn=lambda c: return_conn(c))


def list_recent_shipments(limit: int = 20) -> List[Dict]:
    if not DB_URL:
        return []

    def _do(conn):
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT ticket, chat_id, payload, status, created_at, updated_at
              FROM shipments
             ORDER BY created_at DESC
             LIMIT %s
            """,
            (int(limit),),
        )
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows

    return _run("list_recent_shipments", _do, default=[])


def update_shipment_status(ticket: str, status: str, comment: Optional[str] = None) -> Optional[Dict]:
    """UPDATE status/updated_at + insert event (one transaction). Returns updated row or None."""
    if not DB_URL:
        return None

    def _do(conn):
        begin_transaction(conn)   # status + event: all or nothing
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            UPDATE shipments SET status = %s, updated_at = NOW()
             WHERE UPPER(ticket) = UPPER(%s)
            RETURNING ticket, chat_id, payload, status, created_at, updated_at
            """,
            (status, ticket.strip()),
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            cur.close()
            return None
        cur.execute(
            "INSERT INTO shipment_events (ticket, status, comment) VALUES (%s, %s, %s)",
            (row["ticket"], status, comment),
        )
        conn.commit()
        cur.close()
        return dict(row)

    return _run("update_shipment_status", _do, default=None)


def get_shipment_events(ticket: str) -> List[Dict]:
    if not DB_URL:
        return []

    def _do(conn):
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT ticket, status, comment, created_at
              FROM shipment_events
             WHERE UPPER(ticket) = UPPER(%s)
             ORDER BY created_at ASC, id ASC
            """,
            (ticket.strip(),),
        )
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows

    return _run("get_shipment_events", _do, default=[])
