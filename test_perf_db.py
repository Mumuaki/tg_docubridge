# -*- coding: utf-8 -*-
"""DB round trips: kept pool connections, autocommit, idle-only pre-ping,
transactions where atomicity matters, startup options fallback, background
write queue, dedup + state prefetch, webhook secret_token route, DB diagnostics."""
import json
import os
import runpy
import threading
import time
import unittest
from unittest import mock

import psycopg2
import psycopg2.extensions as ext

import test_ads_start_help  # noqa: F401  (sets SKIP_STARTUP env, imports app)
import docubridge_db_pool_a as pa
import docubridge_db_state as dbs
import docubridge_db_writer as w
import docubridge_handlers as h
import rt_harness as rt

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "postgres://user:S3cretPass@db.example.eu:5432/app?sslmode=require"


class RealPoolBase(unittest.TestCase):
    """Real psycopg2 ThreadedConnectionPool, fake connections (rt_harness.FakeConn)."""
    def setUp(self):
        self.saved = (pa.connection_pool, pa._pool_pid, pa._use_startup_options)
        pa.connection_pool, pa._pool_pid, pa._use_startup_options = None, None, True
        self.ps = [mock.patch.object(pa, "DB_URL", URL),
                   mock.patch("psycopg2.connect", side_effect=self.connect)]
        for p in self.ps:
            p.start()
        self.kwargs = []
        rt.C.reset()

    def connect(self, *a, **k):
        self.kwargs.append(k)
        return rt.FakeConn()

    def tearDown(self):
        for p in self.ps:
            p.stop()
        if pa.connection_pool is not None:
            pa.connection_pool.closeall()
        pa.connection_pool, pa._pool_pid, pa._use_startup_options = self.saved


class TestPoolKeepsConnections(RealPoolBase):
    def test_returned_connection_is_reused_not_closed(self):
        c1 = pa.get_conn(); pa.return_conn(c1)
        c2 = pa.get_conn(); pa.return_conn(c2)
        self.assertIs(c1, c2)
        self.assertFalse(c1.closed)
        self.assertEqual(rt.C.connects, 1)
        self.assertEqual(pa.connection_pool.minconn, pa.DB_POOL_MAX)

    def test_fresh_connection_autocommit_and_timeout_option(self):
        c = pa.get_conn()
        self.assertIs(c.autocommit, True)
        self.assertEqual(self.kwargs[0]["options"], f"-c statement_timeout={pa.DB_STATEMENT_TIMEOUT_MS}")
        self.assertEqual(rt.C.sql, [])                   # no SET / SELECT 1 on a fresh connection
        pa.return_conn(c)

    def test_single_statement_is_one_round_trip(self):
        rt.C.reset()
        c = pa.get_conn(); pa.return_conn(c)
        rt.C.reset()
        pa.db_run("t", lambda conn: conn.cursor().execute("SELECT 42"))
        self.assertEqual(rt.C.rt, 1)                     # no BEGIN / COMMIT / ROLLBACK / ping

    def test_options_rejected_fallback_to_session_set(self):
        calls = {"n": 0}

        def connect(*a, **k):
            calls["n"] += 1
            if "options" in k:
                raise psycopg2.OperationalError('unsupported startup parameter: options')
            return rt.FakeConn()
        with mock.patch("psycopg2.connect", side_effect=connect):
            c = pa.get_conn()
        self.assertIsNotNone(c)
        self.assertFalse(pa._use_startup_options)
        self.assertIn("SET statement_timeout = %s", rt.C.sql)
        pa.return_conn(c)


class TestIdlePing(RealPoolBase):
    def test_no_ping_when_recent_ping_when_idle(self):
        c = pa.get_conn(); pa.return_conn(c)
        rt.C.reset()
        c = pa.get_conn(); pa.return_conn(c)
        self.assertEqual(rt.C.sql, [])
        pa._last_used[id(c)] -= pa.DB_PING_IDLE_S + 1
        c2 = pa.get_conn()
        self.assertIs(c2, c)
        self.assertEqual(rt.C.sql, ["SELECT 1"])
        pa.return_conn(c2)

    def test_dead_after_idle_replaced(self):
        c = pa.get_conn(); pa.return_conn(c)
        pa._last_used[id(c)] -= pa.DB_PING_IDLE_S + 1
        c.closed = 1                                     # server closed it while idle
        c2 = pa.get_conn()
        self.assertIsNot(c2, c)
        self.assertEqual(rt.C.connects, 2)
        pa.return_conn(c2)

    def test_dies_within_idle_window_db_run_retries_fresh(self):
        c = pa.get_conn(); pa.return_conn(c)
        c.closed = 1                                     # no ping (recent) → statement fails
        seen = []

        def fn(conn):
            seen.append(conn)
            conn.cursor().execute("SELECT 7")
            return "ok"
        self.assertEqual(pa.db_run("t", fn, default="DEF"), "ok")
        self.assertIs(seen[0], c); self.assertIsNot(seen[1], c)
        self.assertEqual(rt.C.connects, 2)

    def test_get_state_survives_dead_connection(self):
        c = pa.get_conn(); pa.return_conn(c)
        c.closed = 1
        with mock.patch.object(dbs, "DB_URL", URL):
            st, _ = dbs.get_state(4242)
        self.assertEqual(st, "ask_doc_type")


class TestTransactions(RealPoolBase):
    def test_create_shipment_is_one_transaction_and_conn_back_to_autocommit(self):
        with mock.patch.object(dbs, "DB_URL", URL):
            t = dbs.create_shipment(1, {"from_country": "Украина"})
        self.assertTrue(t)
        conn = pa.connection_pool._pool[0]
        self.assertIs(conn.autocommit, True)
        self.assertEqual(conn.info.transaction_status, ext.TRANSACTION_STATUS_IDLE)
        # BEGIN + 3 INSERTs + COMMIT (+ handshake of the fresh connection)
        self.assertEqual(rt.C.rt - rt.HANDSHAKE_RT, 5)

    def test_error_inside_transaction_rolled_back_and_reusable(self):
        def fn(conn):
            pa.begin_transaction(conn)
            conn.cursor().execute("INSERT 1")
            raise ValueError("boom")
        self.assertEqual(pa.db_run("t", fn, default="DEF"), "DEF")
        conn = pa.connection_pool._pool[0]
        self.assertIs(conn.autocommit, True)
        self.assertEqual(conn.info.transaction_status, ext.TRANSACTION_STATUS_IDLE)

    def test_update_status_uses_transaction(self):
        import docubridge_db_tracking as tr
        with mock.patch.object(tr, "DB_URL", URL), \
                mock.patch.object(tr, "begin_transaction", wraps=pa.begin_transaction) as bt:
            tr.update_shipment_status("DB1", "at_hub")
        bt.assert_called_once()


class TestRoundTripsPerStep(unittest.TestCase):
    def test_funnel_step_round_trips(self):
        res = rt.measure(h, steps=3)
        cold, warm = res[0], res[2]
        self.assertEqual(warm[2], 0)                     # no new connections on a warm pool
        self.assertLessEqual(warm[0], 2)                 # dedup+state, set_state
        self.assertLessEqual(warm[1], 3)                 # 2× chat_history + processed mark (background)
        self.assertLessEqual(cold[2], 2)                 # cold pool: reply path + writer thread

    def test_sync_mode_still_few_round_trips(self):
        with mock.patch.dict(os.environ, {"DOCUBRIDGE_ASYNC_WRITES": "0"}):
            res = rt.measure(h, steps=2)
        self.assertLessEqual(res[1][0], 5)
        self.assertEqual(res[1][1], 0)


class TestWriter(unittest.TestCase):
    def setUp(self):
        w.flush(2)

    def test_background_fifo_and_flush(self):
        out, gate = [], threading.Event()
        w.submit("a", lambda: (gate.wait(2), out.append(1)))
        for i in range(2, 6):
            w.submit(str(i), lambda i=i: out.append(i))
        self.assertEqual(out, [])                        # caller did not wait
        gate.set()
        self.assertTrue(w.flush(3))
        self.assertEqual(out, [1, 2, 3, 4, 5])

    def test_error_does_not_kill_worker(self):
        out = []
        w.submit("bad", lambda: 1 / 0)
        w.submit("good", lambda: out.append("ok"))
        self.assertTrue(w.flush(3))
        self.assertEqual(out, ["ok"])

    def test_queue_full_runs_inline(self):
        gate = threading.Event()
        with mock.patch.object(w, "WRITE_QUEUE_MAX", 1), mock.patch.object(w, "_q", None):
            w.submit("block", lambda: gate.wait(2))
            time.sleep(0.05)
            w.submit("fill", lambda: None)
            out = []
            self.assertFalse(w.submit("inline", lambda: out.append(threading.current_thread().name)))
            self.assertEqual(out, [threading.current_thread().name])
            gate.set(); w.flush(3)

    def test_disabled_runs_inline(self):
        out = []
        with mock.patch.dict(os.environ, {"DOCUBRIDGE_ASYNC_WRITES": "0"}):
            self.assertFalse(w.submit("x", lambda: out.append(1)))
        self.assertEqual(out, [1])

    def test_new_queue_after_fork(self):
        w.submit("x", lambda: None); w.flush(2)
        q_parent = w._q
        with mock.patch.object(w.os, "getpid", return_value=os.getpid() + 1):
            w.submit("y", lambda: None)
            self.assertIsNot(w._q, q_parent)
            w.flush(2)
        w._q, w._pid = q_parent, os.getpid()

    def test_save_message_and_mark_go_to_queue(self):
        import docubridge_db_pool_b as pb
        import docubridge_db_stats as stt
        with mock.patch.object(pb, "DB_URL", URL), mock.patch.object(stt, "DB_URL", URL), \
                mock.patch.object(pb, "submit") as s1, mock.patch.object(stt, "submit") as s2:
            pb.save_message(1, "a", "b"); pb.mark_update_processed(5); stt.record_start_event(1, "x", None)
        self.assertEqual([c[0][0] for c in s1.call_args_list], ["save_message", "mark_update_processed"])
        self.assertEqual(s2.call_args[0][0], "record_start_event")

    def test_gunicorn_worker_exit_flushes(self):
        cfg = runpy.run_path(os.path.join(HERE, "gunicorn.conf.py"))
        server = mock.MagicMock()
        with mock.patch.object(w, "pending", return_value=3), mock.patch.object(w, "flush", return_value=True) as fl:
            cfg["worker_exit"](server, mock.MagicMock())
        fl.assert_called_once()


def upd(uid=501, chat=4242, text="hi"):
    return json.dumps({"update_id": uid, "message": {"message_id": 1, "date": 0,
                       "chat": {"id": chat, "type": "private"}, "text": text}})


class TestDedupPrefetch(unittest.TestCase):
    def post(self, pre, side_effect=None, path=None):
        seen = {}

        def proc(updates):
            seen["state"] = dbs.get_state(4242)
            if side_effect:
                raise side_effect
        with mock.patch.object(h, "check_update_and_state", return_value=pre) as chk, \
                mock.patch.object(h, "is_update_processed", return_value=False) as isp, \
                mock.patch.object(h, "mark_update_processed") as mark, \
                mock.patch.object(dbs, "DB_URL", URL), \
                mock.patch.object(dbs, "get_conn", return_value=None), \
                mock.patch.object(h.bot, "process_new_updates", side_effect=proc) as pr:
            r = h.app.test_client().post(path or f"/webhook/{h.WEBHOOK_SECRET}", data=upd(),
                                         headers={"content-type": "application/json"})
        return r, chk, isp, mark, pr, seen

    def test_duplicate_skipped_one_query(self):
        r, chk, isp, mark, pr, _ = self.post((True, ("greeting", {})))
        self.assertEqual(r.status_code, 200)
        chk.assert_called_once_with(501, 4242)
        isp.assert_not_called(); pr.assert_not_called(); mark.assert_not_called()

    def test_prefetched_state_used_and_cleared(self):
        r, chk, isp, mark, pr, seen = self.post((False, ("ask_volume", {"a": 1})))
        self.assertEqual(seen["state"], ("ask_volume", {"a": 1}))   # no DB query (get_conn would fail)
        mark.assert_called_once_with(501)
        self.assertIsNone(dbs._cache())

    def test_failure_not_marked_and_cleared(self):
        r, *_, mark, pr, seen = self.post((False, ("ask_volume", {})), side_effect=RuntimeError("x"))
        mark.assert_not_called()
        self.assertIsNone(dbs._cache())

    def test_fallback_when_combined_query_fails(self):
        r, chk, isp, mark, pr, seen = self.post(None)
        isp.assert_called_once_with(501)
        self.assertEqual(seen["state"][0], dbs.DB_ERROR_STATE)     # real get_state ran (no conn)
        mark.assert_called_once_with(501)

    def test_set_state_and_update_data_keep_cache_in_sync(self):
        dbs.prefetch_state(7, "ask_doc_type", {"x": 1})
        try:
            conn = mock.MagicMock()
            with mock.patch.object(dbs, "DB_URL", URL), mock.patch.object(dbs, "get_conn", return_value=conn), \
                    mock.patch.object(dbs, "return_conn"):
                self.assertTrue(dbs.set_state(7, "ask_volume", {"x": 2}))
                self.assertEqual(dbs.get_state(7), ("ask_volume", {"x": 2}))
                st, data = dbs.get_state(7); data["x"] = 99            # callers get a copy
                self.assertEqual(dbs.get_state(7), ("ask_volume", {"x": 2}))
                with mock.patch.object(dbs, "_run", return_value=True):
                    dbs.update_data(7, {"y": 3})
                self.assertEqual(dbs.get_state(7), ("ask_volume", {"y": 3}))
        finally:
            dbs.clear_prefetch()

    def test_combined_query_sql(self):
        conn = mock.MagicMock()
        conn.cursor.return_value.fetchone.return_value = {"processed": False, "state": None, "data": None}
        with mock.patch.object(dbs, "DB_URL", URL), \
                mock.patch.object(dbs, "db_run", side_effect=lambda l, f, *a, **k: f(conn)):
            self.assertEqual(dbs.check_update_and_state(5, 7), (False, ("greeting", {})))
        sql = conn.cursor.return_value.execute.call_args[0][0]
        self.assertIn("processed_updates", sql); self.assertIn("LEFT JOIN user_state", sql)

    def test_callback_chat_id(self):
        from telebot.types import Update
        u = Update.de_json({"update_id": 1, "callback_query": {
            "id": "1", "chat_instance": "c", "data": "x", "from": {"id": 9, "is_bot": False, "first_name": "a"},
            "message": {"message_id": 1, "date": 0, "chat": {"id": 31, "type": "private"}}}})
        self.assertEqual(h._update_chat_id(u), 31)


class TestWebhookToken(unittest.TestCase):
    def post(self, token_env, header=None):
        hdr = {"content-type": "application/json"}
        if header is not None:
            hdr["X-Telegram-Bot-Api-Secret-Token"] = header
        with mock.patch.object(h, "WEBHOOK_SECRET_TOKEN", token_env), \
                mock.patch.object(h, "_handle_update_json") as handle:
            r = h.app.test_client().post("/webhook", data=upd(), headers=hdr)
        return r.status_code, handle.called

    def test_correct_token(self):
        self.assertEqual(self.post("tok_ABC-123", "tok_ABC-123"), (200, True))

    def test_wrong_or_missing_header(self):
        self.assertEqual(self.post("tok_ABC-123", "tok_ABC-124"), (403, False))
        self.assertEqual(self.post("tok_ABC-123", None), (403, False))
        self.assertEqual(self.post("tok_ABC-123", ""), (403, False))

    def test_env_not_set_rejects(self):
        self.assertEqual(self.post("", "anything"), (403, False))
        self.assertEqual(self.post("", ""), (403, False))

    def test_uses_compare_digest(self):
        with mock.patch.object(h.hmac, "compare_digest", return_value=False) as cd:
            self.assertEqual(self.post("tok", "tok"), (403, False))
        cd.assert_called_once()

    def test_legacy_route_still_works(self):
        with mock.patch.object(h, "_handle_update_json") as handle:
            r = h.app.test_client().post(f"/webhook/{h.WEBHOOK_SECRET}", data=upd(),
                                         headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 200); handle.assert_called_once()

    def test_set_webhook_with_token_only_when_configured(self):
        with mock.patch.object(h, "WEBHOOK_BASE", "https://x"), \
                mock.patch.object(h, "WEBHOOK_SECRET_TOKEN", "Zq9_tokenvalue"), \
                mock.patch.object(h.bot, "set_webhook", return_value=True) as sw, \
                mock.patch("builtins.print") as pr:
            self.assertTrue(h.ensure_webhook())
        sw.assert_called_once_with(url="https://x/webhook", secret_token="Zq9_tokenvalue", drop_pending_updates=False)
        self.assertNotIn("Zq9_tokenvalue", pr.call_args[0][0])
        with mock.patch.object(h, "WEBHOOK_BASE", "https://x"), \
                mock.patch.object(h, "WEBHOOK_SECRET_TOKEN", ""), \
                mock.patch.object(h.bot, "set_webhook", return_value=True) as sw:
            h.ensure_webhook()
        self.assertEqual(sw.call_args[1]["url"], f"https://x/webhook/{h.WEBHOOK_SECRET}")
        self.assertNotIn("secret_token", sw.call_args[1])


class TestDiagnostics(RealPoolBase):
    def test_host_only_and_median(self):
        with mock.patch.object(h, "DB_URL", URL), mock.patch("builtins.print") as pr:
            med = h.db_diagnostics()
        self.assertIsNotNone(med)
        line = pr.call_args[0][0]
        self.assertIn("host=db.example.eu", line); self.assertIn("median", line)
        for secret in ("S3cretPass", "user", "5432", "sslmode"):
            self.assertNotIn(secret, line)
        self.assertEqual(rt.C.sql.count("SELECT 1"), 5)

    def test_host_from_keyvalue_dsn(self):
        with mock.patch.object(h, "DB_URL", "host=pg.internal dbname=x password=zzz"):
            self.assertEqual(h.db_host(), "pg.internal")
        with mock.patch.object(h, "DB_URL", None):
            self.assertEqual(h.db_host(), "-")


if __name__ == "__main__":
    unittest.main()
