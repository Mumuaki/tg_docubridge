# -*- coding: utf-8 -*-
"""DocuBridge DB part B: processed updates + chat history (via db_run, 1 retry)."""
from typing import Optional

from docubridge_config import DB_URL
from docubridge_db_pool_a import get_conn, return_conn, db_run


def _run(label, fn, default=None, retry=True):
    return db_run(label, fn, default, retry, getconn=lambda: get_conn(), putconn=lambda c: return_conn(c))


def is_update_processed(update_id: int) -> bool:
    if not DB_URL:
        return False

    def _do(conn):
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM processed_updates WHERE update_id = %s", (update_id,))
        exists = cur.fetchone() is not None
        cur.close()
        return exists

    return bool(_run("is_update_processed", _do, default=False))


def mark_update_processed(update_id: int):
    if not DB_URL:
        return

    def _do(conn):
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO processed_updates (update_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (update_id,),
        )
        conn.commit()
        cur.close()
        return True

    _run("mark_update_processed", _do)


def cleanup_old_updates():
    if not DB_URL:
        return

    def _do(conn):
        cur = conn.cursor()
        cur.execute("DELETE FROM processed_updates WHERE processed_at < NOW() - INTERVAL '7 days'")
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        print(f"[DB] Cleaned up {deleted} old update records")
        return deleted

    return _run("cleanup_old_updates", _do, default=0)


def save_message(chat_id: int, user_text: Optional[str], bot_reply: Optional[str]):
    if not DB_URL:
        return

    def _do(conn):
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chat_history (chat_id, user_message, bot_reply) VALUES (%s, %s, %s)",
            (int(chat_id), user_text, bot_reply),
        )
        conn.commit()
        cur.close()
        return True

    _run("save_message", _do)
