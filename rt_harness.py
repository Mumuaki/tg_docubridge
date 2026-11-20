# -*- coding: utf-8 -*-
"""Round-trip counter for one funnel step through the real webhook + DB code,
with psycopg2.connect replaced by an in-memory fake that counts server round trips
(psycopg2 semantics: implicit BEGIN outside autocommit, COMMIT/ROLLBACK only when
a transaction is open). Works on old and new code (no new names required)."""
import json
import os
import sys
from unittest import mock

import threading

import psycopg2
import psycopg2.extensions as ext

HANDSHAKE_RT = 4   # TCP + TLS + startup/auth (conservative)


class Counter:
    def __init__(self):
        self.reset()

    def reset(self):
        self.rt = 0          # all threads
        self.bg_rt = 0       # background writer thread only
        self.connects = 0
        self.sql = []        # statements from the request thread

    def add(self, n=1):
        self.rt += n
        if threading.current_thread().name.startswith("docubridge-db-writer"):
            self.bg_rt += n


C = Counter()


class Info:
    def __init__(self):
        self.transaction_status = ext.TRANSACTION_STATUS_IDLE


class FakeCursor:
    def __init__(self, conn, dict_mode):
        self.conn, self.dict_mode, self.row = conn, dict_mode, None
        self.rowcount = 1

    def execute(self, sql, args=None):
        c = self.conn
        if c.closed:
            raise psycopg2.InterfaceError("connection already closed")
        if not c.autocommit and c.info.transaction_status == ext.TRANSACTION_STATUS_IDLE:
            C.add()                       # implicit BEGIN
            c.info.transaction_status = ext.TRANSACTION_STATUS_INTRANS
        C.add()
        q = " ".join(str(sql).split())
        if not threading.current_thread().name.startswith("docubridge-db-writer"):
            C.sql.append(q[:70])
        self.row = self._answer(q)

    def _answer(self, q):
        st = {"state": "ask_doc_type", "data": {"role": "sender", "from_country": "Украина",
                                                "to_country": "Россия", "ref": "ua_ru"}}
        if "EXISTS (SELECT 1 FROM processed_updates" in q:
            return dict(processed=False, **st)
        if q.startswith("SELECT state, data FROM user_state"):
            return dict(st) if self.dict_mode else (st["state"], st["data"])
        if q.startswith("SELECT 1 FROM processed_updates"):
            return None
        if q.startswith("SELECT 1"):
            return (1,)
        return None

    def fetchone(self):
        return self.row

    def fetchall(self):
        return []

    def close(self):
        pass


class FakeConn:
    def __init__(self, *a, **k):
        C.connects += 1
        C.add(HANDSHAKE_RT)
        self.autocommit = False
        self.closed = 0
        self.info = Info()

    def cursor(self, cursor_factory=None, **k):
        return FakeCursor(self, cursor_factory is not None)

    def _end(self):
        if self.info.transaction_status != ext.TRANSACTION_STATUS_IDLE:
            C.add()
            self.info.transaction_status = ext.TRANSACTION_STATUS_IDLE

    def commit(self):
        self._end()

    def rollback(self):
        self._end()

    def close(self):
        self.closed = 1

    def __setattr__(self, k, v):
        if k == "autocommit" and getattr(self, "info", None) is not None \
                and self.info.transaction_status != ext.TRANSACTION_STATUS_IDLE:
            raise psycopg2.ProgrammingError("set_session cannot be used inside a transaction")
        object.__setattr__(self, k, v)


def body(update_id, text):
    return json.dumps({"update_id": update_id, "message": {
        "message_id": update_id, "date": 0, "chat": {"id": 4242, "type": "private"},
        "from": {"id": 4242, "is_bot": False, "first_name": "Test"}, "text": text}})


def measure(h, steps=3):
    """Returns list of (sync_rt, async_rt, connects) per step (first = cold pool)."""
    mods = [m for n, m in sys.modules.items() if n.startswith("docubridge") and hasattr(m, "DB_URL")]
    patches = [mock.patch.object(m, "DB_URL", "postgres://u:p@db.example:5432/x") for m in mods]
    patches += [mock.patch("psycopg2.connect", side_effect=FakeConn),
                mock.patch.object(h.bot, "send_message")]
    for p in patches:
        p.start()
    try:
        import docubridge_db_pool_a as pa
        pa.connection_pool, pa._pool_pid = None, None
        try:
            import docubridge_db_writer as w
        except ImportError:
            w = None
        from docubridge_helpers import DOC_TYPE_BUTTONS
        client = h.app.test_client()
        out = []
        for i in range(steps):
            C.reset()
            r = client.post(f"/webhook/{h.WEBHOOK_SECRET}", data=body(9000 + i, DOC_TYPE_BUTTONS[0]),
                            headers={"content-type": "application/json"})
            assert r.status_code == 200
            if w is not None:
                w.flush(5)
            out.append((C.rt - C.bg_rt, C.bg_rt, C.connects, list(C.sql)))
        return out
    finally:
        for p in reversed(patches):
            p.stop()


if __name__ == "__main__":
    os.environ["DOCUBRIDGE_SKIP_STARTUP"] = "1"
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:TEST_TOKEN_FOR_UNIT_TESTS")
    sys.path.insert(0, os.getcwd())
    import main  # noqa
    import docubridge_handlers as h
    for i, (s, a, c, sql) in enumerate(measure(h)):
        print(f"step {i + 1}: sync round trips={s} (connects {c}), background={a}")
        if "-v" in sys.argv:
            for q in sql:
                print("    ", q)
