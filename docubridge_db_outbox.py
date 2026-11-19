# -*- coding: utf-8 -*-
"""Outbox-style persistence for side channels:

* Google Sheet export of shipments (sheet_synced_at / sheet_claimed_at lease);
* one-off admin broadcasts (broadcast_log, one row per campaign + chat).

All claims are atomic UPDATE/INSERT … RETURNING without automatic retry: if the
connection drops around COMMIT we cannot know whether the claim landed, and
«skipped now, picked up later» is safer than «done twice».
"""
from typing import Dict, List, Optional

import psycopg2.extras

from docubridge_config import DB_URL
from docubridge_db_pool_a import get_conn, return_conn, db_run

SHEET_CLAIM_LEASE_MIN = 5
BROADCAST_MIN_IDLE_H = 48     # never overlap with the automatic 90-min/48-h reminders
BROADCAST_MAX_AGE_DAYS = 60
BROADCAST_SEGMENTS = ("started", "greeting", "all")


def _run(label, fn, default=None, retry=True):
    return db_run(label, fn, default, retry, getconn=lambda: get_conn(), putconn=lambda c: return_conn(c))


# ------------ Google Sheet outbox ------------

def claim_sheet_sync(ticket: str) -> bool:
    """Take a 5-min lease on exporting `ticket`. False if already synced / leased.
    Offline mode (no DATABASE_URL): always True (nothing to coordinate)."""
    if not DB_URL:
        return True

    def _do(conn):
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE shipments SET sheet_claimed_at = NOW()
             WHERE ticket = %s
               AND sheet_synced_at IS NULL
               AND (sheet_claimed_at IS NULL
                    OR sheet_claimed_at < NOW() - (%s * INTERVAL '1 minute'))
            RETURNING ticket
            """,
            (ticket, SHEET_CLAIM_LEASE_MIN),
        )
        ok = cur.fetchone() is not None
        conn.commit()
        cur.close()
        return ok

    return bool(_run("claim_sheet_sync", _do, default=False, retry=False))


def mark_sheet_synced(ticket: str) -> bool:
    if not DB_URL:
        return True

    def _do(conn):
        cur = conn.cursor()
        cur.execute(
            "UPDATE shipments SET sheet_synced_at = NOW(), sheet_claimed_at = NULL WHERE ticket = %s",
            (ticket,),
        )
        conn.commit()
        cur.close()
        return True

    return bool(_run("mark_sheet_synced", _do, default=False))


def release_sheet_claim(ticket: str) -> None:
    """Export failed: drop the lease so the next sync tick retries right away."""
    if not DB_URL:
        return

    def _do(conn):
        cur = conn.cursor()
        cur.execute(
            "UPDATE shipments SET sheet_claimed_at = NULL WHERE ticket = %s AND sheet_synced_at IS NULL",
            (ticket,),
        )
        conn.commit()
        cur.close()
        return True

    _run("release_sheet_claim", _do)


def list_unsynced_shipments(limit: int = 50) -> List[Dict]:
    """Oldest shipments not yet in the sheet (and not leased right now)."""
    if not DB_URL:
        return []

    def _do(conn):
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT ticket, chat_id, payload, status, created_at, updated_at
              FROM shipments
             WHERE sheet_synced_at IS NULL
               AND (sheet_claimed_at IS NULL
                    OR sheet_claimed_at < NOW() - (%s * INTERVAL '1 minute'))
             ORDER BY created_at ASC
             LIMIT %s
            """,
            (SHEET_CLAIM_LEASE_MIN, int(limit)),
        )
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows

    return _run("list_unsynced_shipments", _do, default=[])


# ------------ Broadcast to chats stuck in the funnel ------------

CANDIDATES_SQL = """
    SELECT us.chat_id, us.state, us.updated_at, us.data
      FROM user_state us
     WHERE us.updated_at < NOW() - (%(idle_h)s * INTERVAL '1 hour')
       AND us.updated_at > NOW() - (%(max_days)s * INTERVAL '1 day')
       AND us.state NOT IN ('completed', 'await_track', '__db_error__')
       AND (%(segment)s = 'all'
            OR (%(segment)s = 'started' AND us.state <> 'greeting')
            OR (%(segment)s = 'greeting' AND us.state = 'greeting'))
       AND us.chat_id <> %(admin)s
       AND NOT EXISTS (SELECT 1 FROM shipments s WHERE s.chat_id = us.chat_id)
       AND NOT EXISTS (SELECT 1 FROM leads l WHERE l.chat_id = us.chat_id)
       AND NOT EXISTS (SELECT 1 FROM broadcast_log b
                        WHERE b.campaign = %(campaign)s AND b.chat_id = us.chat_id)
"""


def _cand_args(campaign: str, segment: str, admin_id: int) -> Dict:
    if segment not in BROADCAST_SEGMENTS:
        raise ValueError(f"unknown segment {segment!r}")
    return {"idle_h": BROADCAST_MIN_IDLE_H, "max_days": BROADCAST_MAX_AGE_DAYS,
            "segment": segment, "admin": int(admin_id or 0), "campaign": campaign}


def find_broadcast_candidates(campaign: str, segment: str, admin_id: int,
                              limit: int = 5000) -> List[Dict]:
    if not DB_URL:
        return []
    args = _cand_args(campaign, segment, admin_id)
    args["limit"] = int(limit)

    def _do(conn):
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(CANDIDATES_SQL + " ORDER BY us.updated_at ASC LIMIT %(limit)s", args)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows

    return _run("find_broadcast_candidates", _do, default=[])


def count_broadcast_candidates(campaign: str, admin_id: int) -> Optional[Dict]:
    """{'by_state': [(state, n), …], 'started': n, 'greeting': n, 'already': n}; None if DB down."""
    if not DB_URL:
        return None
    args = _cand_args(campaign, "all", admin_id)

    def _do(conn):
        cur = conn.cursor()
        cur.execute(
            "SELECT state, COUNT(*) FROM (" + CANDIDATES_SQL + ") c GROUP BY state ORDER BY 2 DESC, 1",
            args,
        )
        by_state = [(s, int(n)) for s, n in cur.fetchall()]
        cur.execute(
            "SELECT status, COUNT(*) FROM broadcast_log WHERE campaign = %s GROUP BY status",
            (campaign,),
        )
        already = {s: int(n) for s, n in cur.fetchall()}
        cur.close()
        greeting = sum(n for s, n in by_state if s == "greeting")
        started = sum(n for s, n in by_state if s != "greeting")
        return {"by_state": by_state, "started": started, "greeting": greeting,
                "already": already}

    return _run("count_broadcast_candidates", _do, default=None)


def claim_broadcast(campaign: str, chat_id: int, segment: str) -> bool:
    """Atomically reserve (campaign, chat) BEFORE sending. True only for the first claim ever."""
    if not DB_URL:
        return False

    def _do(conn):
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO broadcast_log (campaign, chat_id, segment, status)
            VALUES (%s, %s, %s, 'sending')
            ON CONFLICT (campaign, chat_id) DO NOTHING
            RETURNING chat_id
            """,
            (campaign, int(chat_id), segment),
        )
        ok = cur.fetchone() is not None
        conn.commit()
        cur.close()
        return ok

    return bool(_run("claim_broadcast", _do, default=False, retry=False))


def set_broadcast_result(campaign: str, chat_id: int, status: str, error: Optional[str] = None) -> None:
    """status: sent | blocked | failed."""
    if not DB_URL:
        return

    def _do(conn):
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE broadcast_log
               SET status = %s, error = %s,
                   sent_at = CASE WHEN %s = 'sent' THEN NOW() ELSE sent_at END
             WHERE campaign = %s AND chat_id = %s
            """,
            (status, (error or None) and str(error)[:300], status, campaign, int(chat_id)),
        )
        conn.commit()
        cur.close()
        return True

    _run("set_broadcast_result", _do)
