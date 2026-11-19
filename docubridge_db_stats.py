# -*- coding: utf-8 -*-
"""DocuBridge funnel stats: /start events + aggregates for admin /stats."""
from typing import Optional, Dict, List, Tuple

from docubridge_config import DB_URL
from docubridge_db_pool_a import get_conn, return_conn, db_run


def _run(label, fn, default=None, retry=True):
    return db_run(label, fn, default, retry, getconn=lambda: get_conn(), putconn=lambda c: return_conn(c))


def record_start_event(chat_id: int, payload: Optional[str], username: Optional[str]) -> None:
    """Persist one /start (deep-link payload + optional @username). Never raises."""
    if not DB_URL:
        return

    def _do(conn):
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO start_events (chat_id, payload, username) VALUES (%s, %s, %s)",
            (int(chat_id), payload or None, username or None),
        )
        conn.commit()
        cur.close()
        return True

    _run("record_start_event", _do)


def _rows(cur, sql: str, args: Tuple) -> List[Tuple]:
    cur.execute(sql, args)
    return list(cur.fetchall())


def get_funnel_stats(days: int) -> Optional[Dict]:
    """Aggregates for the last `days` days; None if DB unavailable."""
    if not DB_URL:
        return None
    def _do(conn):
        cur = conn.cursor()
        since = "NOW() - (%s * INTERVAL '1 day')"
        d = int(days)
        starts, start_chats = _rows(
            cur, f"SELECT COUNT(*), COUNT(DISTINCT chat_id) FROM start_events WHERE created_at >= {since}", (d,)
        )[0]
        by_payload = _rows(
            cur,
            f"""SELECT COALESCE(NULLIF(payload, ''), '(без метки)') AS p, COUNT(*)
                  FROM start_events WHERE created_at >= {since}
                 GROUP BY p ORDER BY 2 DESC, 1""",
            (d,),
        )
        orders = _rows(cur, f"SELECT COUNT(*) FROM shipments WHERE created_at >= {since}", (d,))[0][0]
        # Orders from chats whose /start was logged (start_events exists since PR #9):
        # keeps conversion honest instead of 200–300 % from pre-logging chats.
        orders_tracked = _rows(
            cur,
            f"""SELECT COUNT(*) FROM shipments s
                 WHERE s.created_at >= {since}
                   AND EXISTS (SELECT 1 FROM start_events e
                                WHERE e.chat_id = s.chat_id AND e.created_at <= s.created_at)""",
            (d,),
        )[0][0]
        reminders = _rows(
            cur, f"SELECT COUNT(*) FROM funnel_reminders WHERE reminded_at >= {since}", (d,)
        )[0][0]
        by_route = _rows(
            cur,
            f"""SELECT COALESCE(payload->>'from_country', '?') || ' → ' || COALESCE(payload->>'to_country', '?') AS r,
                       COUNT(*)
                  FROM shipments WHERE created_at >= {since}
                 GROUP BY r ORDER BY 2 DESC, 1""",
            (d,),
        )
        by_state = _rows(
            cur,
            f"""SELECT state, COUNT(*) FROM user_state WHERE updated_at >= {since}
                 GROUP BY state ORDER BY 2 DESC, 1""",
            (d,),
        )
        cur.close()
        return {
            "days": d,
            "starts": int(starts or 0),
            "start_chats": int(start_chats or 0),
            "by_payload": [(p, int(n)) for p, n in by_payload],
            "orders": int(orders or 0),
            "orders_tracked": int(orders_tracked or 0),
            "reminders": int(reminders or 0),
            "by_route": [(r, int(n)) for r, n in by_route],
            "by_state": [(s, int(n)) for s, n in by_state],
        }

    return _run("get_funnel_stats", _do, default=None)
