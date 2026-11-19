# -*- coding: utf-8 -*-
"""DocuBridge: persistence for one-time «продолжить оформление» reminders.

Double-send protection works across threads AND processes: claim_reminder() is an
atomic INSERT … ON CONFLICT DO NOTHING RETURNING done before the message is sent.
"""
from typing import Dict, List, Sequence

import psycopg2.extras

from docubridge_config import DB_URL
from docubridge_db_pool_a import get_conn, return_conn, db_run


def _run(label, fn, default=None, retry=True):
    return db_run(label, fn, default, retry, getconn=lambda: get_conn(), putconn=lambda c: return_conn(c))


def find_reminder_candidates(states: Sequence[str], idle_minutes: int, max_age_hours: int,
                             limit: int = 20) -> List[Dict]:
    """Chats idle on an unfinished step for ≥ idle_minutes but < max_age_hours,
    never reminded before and without any order."""
    if not DB_URL:
        return []

    def _do(conn):
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT us.chat_id, us.state, us.data
              FROM user_state us
             WHERE us.state = ANY(%s)
               AND us.updated_at <  NOW() - (%s * INTERVAL '1 minute')
               AND us.updated_at >  NOW() - (%s * INTERVAL '1 hour')
               AND NOT EXISTS (SELECT 1 FROM funnel_reminders r WHERE r.chat_id = us.chat_id)
               AND NOT EXISTS (SELECT 1 FROM shipments s WHERE s.chat_id = us.chat_id)
             ORDER BY us.updated_at ASC
             LIMIT %s
            """,
            (list(states), int(idle_minutes), int(max_age_hours), int(limit)),
        )
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows

    return _run("find_reminder_candidates", _do, default=[])


def claim_reminder(chat_id: int, state: str) -> bool:
    """Atomically mark the chat as reminded. True only for the first claim ever.

    No automatic retry: if the connection drops around COMMIT we cannot know whether
    the claim landed, and «not sent» is safer than «sent twice»."""
    if not DB_URL:
        return False

    def _do(conn):
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO funnel_reminders (chat_id, state) VALUES (%s, %s)
            ON CONFLICT (chat_id) DO NOTHING
            RETURNING chat_id
            """,
            (int(chat_id), state),
        )
        ok = cur.fetchone() is not None
        conn.commit()
        cur.close()
        return ok

    return bool(_run("claim_reminder", _do, default=False, retry=False))
