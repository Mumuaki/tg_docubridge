# -*- coding: utf-8 -*-
"""Fork-safe startup: per-PID DB pool / reminder thread / startup, connection
retries, cross-process reminder claim, /health, gunicorn config."""
import os
import runpy
import threading
import unittest
from unittest import mock

import psycopg2

import test_ads_start_help  # noqa: F401  (sets SKIP_STARTUP env, imports app)
import docubridge_db_pool_a as pa
import docubridge_db_state as dbs
import docubridge_reminders as rem
import docubridge_handlers as h

HERE = os.path.dirname(os.path.abspath(__file__))


def good_conn():
    c = mock.MagicMock(name="good")
    return c


def dead_conn():
    c = mock.MagicMock(name="dead")
    c.cursor.return_value.execute.side_effect = psycopg2.OperationalError("SSL connection has been closed unexpectedly")
    return c


class PoolBase(unittest.TestCase):
    def setUp(self):
        self.saved = (pa.connection_pool, pa._pool_pid, list(pa._parked_pools))
        pa.connection_pool, pa._pool_pid = None, None
        pa._parked_pools.clear()
        self.p_url = mock.patch.object(pa, "DB_URL", "postgres://x"); self.p_url.start()

    def tearDown(self):
        self.p_url.stop()
        pa.connection_pool, pa._pool_pid = self.saved[0], self.saved[1]
        pa._parked_pools[:] = self.saved[2]


class TestForkSafePool(PoolBase):
    def test_new_pool_after_pid_change_old_parked_not_closed(self):
        pools = [mock.MagicMock(name="pool_parent"), mock.MagicMock(name="pool_child")]
        with mock.patch.object(pa.pool, "ThreadedConnectionPool", side_effect=pools) as ctor, \
                mock.patch.object(pa.os, "getpid", return_value=100):
            p1 = pa._current_pool(); p1b = pa._current_pool()
        self.assertIs(p1, p1b)
        with mock.patch.object(pa.pool, "ThreadedConnectionPool", side_effect=pools[1:]), \
                mock.patch.object(pa.os, "getpid", return_value=200):
            p2 = pa._current_pool()
        self.assertIs(p2, pools[1])
        self.assertIn(pools[0], pa._parked_pools)
        pools[0].closeall.assert_not_called()
        pools[0].putconn.assert_not_called()
        self.assertEqual(ctor.call_count, 1)

    def test_nothing_connects_at_import(self):
        with mock.patch.object(pa.pool, "ThreadedConnectionPool") as ctor, \
                mock.patch.object(pa.psycopg2, "connect") as conn:
            import importlib
            importlib.reload(pa)
        ctor.assert_not_called(); conn.assert_not_called()

    def test_get_conn_discards_dead_and_returns_live(self):
        # PR #13: only connections idle > DB_PING_IDLE_S are pinged (one SELECT 1, autocommit);
        # statement_timeout comes from the connect options, not SET LOCAL per query.
        bad, good = dead_conn(), good_conn()
        for c in (bad, good):
            c.autocommit = True
            pa._last_used[id(c)] = pa.time.monotonic() - pa.DB_PING_IDLE_S - 1
        fake_pool = mock.MagicMock(); fake_pool.getconn.side_effect = [bad, good]
        with mock.patch.object(pa.pool, "ThreadedConnectionPool", return_value=fake_pool):
            c = pa.get_conn()
        self.assertIs(c, good)
        fake_pool.putconn.assert_called_once_with(bad, close=True)
        sqls = [call.args[0] for call in good.cursor.return_value.execute.call_args_list]
        self.assertNotIn("SET LOCAL statement_timeout = %s", sqls)
        self.assertEqual(sqls, ["SELECT 1"])
        pa._last_used.pop(id(good), None)

    def test_connect_kwargs_timeouts_keepalives(self):
        kw = pa._connect_kwargs()
        for k in ("connect_timeout", "keepalives", "keepalives_idle", "keepalives_interval", "keepalives_count"):
            self.assertIn(k, kw)
        self.assertEqual(kw["connect_timeout"], pa.DB_CONNECT_TIMEOUT_S)

    def test_return_conn_after_fork_closes_instead_of_putconn(self):
        fake_pool = mock.MagicMock()
        pa.connection_pool, pa._pool_pid = fake_pool, 100
        c = good_conn()
        with mock.patch.object(pa.os, "getpid", return_value=200):
            pa.return_conn(c)
        fake_pool.putconn.assert_not_called()
        c.close.assert_called_once()


class TestDbRunRetry(unittest.TestCase):
    def run_db(self, fn, retry=True):
        conns = [good_conn(), good_conn()]
        get = mock.MagicMock(side_effect=conns)
        put, disc = mock.MagicMock(), mock.MagicMock()
        r = pa.db_run("t", fn, default="DEF", retry=retry, getconn=get, putconn=put, discard=disc)
        return r, conns, get, put, disc

    def test_retry_once_on_connection_error(self):
        calls = []

        def fn(conn):
            calls.append(conn)
            if len(calls) == 1:
                raise psycopg2.OperationalError("SSL connection has been closed unexpectedly")
            return "OK"
        r, conns, get, put, disc = self.run_db(fn)
        self.assertEqual(r, "OK")
        self.assertEqual(get.call_count, 2)
        disc.assert_called_once_with(conns[0]); put.assert_called_once_with(conns[1])

    def test_interface_error_twice_returns_default(self):
        def fn(conn):
            raise psycopg2.InterfaceError("connection already closed")
        r, conns, get, put, disc = self.run_db(fn)
        self.assertEqual(r, "DEF"); self.assertEqual(disc.call_count, 2)

    def test_logic_error_not_retried(self):
        def fn(conn):
            raise ValueError("bad sql")
        r, conns, get, put, disc = self.run_db(fn)
        self.assertEqual(r, "DEF"); self.assertEqual(get.call_count, 1)
        conns[0].rollback.assert_called_once(); put.assert_called_once_with(conns[0])

    def test_no_retry_flag(self):
        def fn(conn):
            raise psycopg2.OperationalError("x")
        r, conns, get, put, disc = self.run_db(fn, retry=False)
        self.assertEqual(get.call_count, 1)

    def test_create_shipment_retries_before_commit_not_after(self):
        first, second = good_conn(), good_conn()
        first.cursor.return_value.execute.side_effect = psycopg2.OperationalError("server closed")
        with mock.patch.object(dbs, "DB_URL", "postgres://x"), \
                mock.patch.object(dbs, "get_conn", side_effect=[first, second]), \
                mock.patch.object(dbs, "return_conn"), mock.patch.object(pa, "discard_conn"):
            t = dbs.create_shipment(1, {"from_country": "Украина"})
        self.assertTrue(t and t.startswith("DB"))
        second.commit.assert_called_once()
        # connection lost during COMMIT → ambiguous → no second attempt
        c = good_conn(); c.commit.side_effect = psycopg2.OperationalError("lost")
        get = mock.MagicMock(side_effect=[c, good_conn()])
        with mock.patch.object(dbs, "DB_URL", "postgres://x"), \
                mock.patch.object(dbs, "get_conn", get), mock.patch.object(dbs, "return_conn"):
            self.assertIsNone(dbs.create_shipment(1, {}))
        self.assertEqual(get.call_count, 1)


class TestReminderThread(unittest.TestCase):
    def setUp(self):
        self.saved = (rem._thread, rem._thread_pid)
        rem._thread, rem._thread_pid = None, None
        self.env = mock.patch.dict("os.environ", {"DOCUBRIDGE_REMINDERS": "1"}); self.env.start()

    def tearDown(self):
        self.env.stop()
        rem._thread, rem._thread_pid = self.saved

    def fake_thread(self, alive=True):
        t = mock.MagicMock(); t.is_alive.return_value = alive
        return t

    def test_exactly_one_per_process_and_new_after_fork(self):
        threads = [self.fake_thread(), self.fake_thread()]
        with mock.patch.object(rem.threading, "Thread", side_effect=threads) as th:
            with mock.patch.object(rem.os, "getpid", return_value=100):
                a = rem.start_reminder_thread(); b = rem.start_reminder_thread()
            self.assertIs(a, b); self.assertEqual(th.call_count, 1)
            with mock.patch.object(rem.os, "getpid", return_value=200):
                c = rem.start_reminder_thread()
            self.assertIs(c, threads[1]); self.assertEqual(th.call_count, 2)

    def test_concurrent_start_one_thread(self):
        created = []

        def mk(*a, **k):
            t = self.fake_thread(); created.append(t); return t
        ws = [threading.Thread(target=rem.start_reminder_thread) for _ in range(8)]  # real threads
        with mock.patch.object(rem.threading, "Thread", side_effect=mk):
            [w.start() for w in ws]; [w.join() for w in ws]
        self.assertEqual(len(created), 1)

    def test_dead_thread_restarted_and_health(self):
        dead = self.fake_thread(alive=False)
        with mock.patch.object(rem.threading, "Thread", side_effect=[dead, self.fake_thread()]) as th:
            rem.start_reminder_thread()
            self.assertEqual(rem.reminder_health(), "dead")
            rem.start_reminder_thread()
        self.assertEqual(th.call_count, 2)
        rem._last_tick = rem.time.time()
        self.assertEqual(rem.reminder_health(), "alive")
        rem._last_tick = rem.time.time() - rem.CHECK_EVERY_S * 3 - 200
        self.assertEqual(rem.reminder_health(), "dead")
        with mock.patch.dict("os.environ", {"DOCUBRIDGE_REMINDERS": "0"}):
            self.assertEqual(rem.reminder_health(), "disabled")

    def test_loop_survives_exceptions(self):
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            if calls["n"] >= 3:
                raise SystemExit  # stop the test loop
            raise RuntimeError("db down")
        with mock.patch.object(rem, "run_reminders_once", side_effect=boom), \
                mock.patch.object(rem.time, "sleep"):
            with self.assertRaises(SystemExit):
                rem._loop(initial_delay=0)
        self.assertEqual(calls["n"], 3)

    def test_concurrent_claim_sends_once(self):
        claimed, lock = set(), threading.Lock()

        def claim(chat_id, state):  # behaves like INSERT … ON CONFLICT DO NOTHING RETURNING
            with lock:
                if chat_id in claimed:
                    return False
                claimed.add(chat_id)
                return True
        rows = [{"chat_id": 42, "state": "ask_name", "data": {}}]
        with mock.patch.object(rem, "find_reminder_candidates", return_value=rows), \
                mock.patch.object(rem, "claim_reminder", side_effect=claim), \
                mock.patch.object(rem, "prompt_step") as prompt, mock.patch.object(rem, "ADMIN_CHAT_ID", 0):
            ws = [threading.Thread(target=rem.run_reminders_once) for _ in range(6)]
            [w.start() for w in ws]; [w.join() for w in ws]
        self.assertEqual(prompt.call_count, 1)


class TestStartup(unittest.TestCase):
    def setUp(self):
        self.saved = dict(h._startup)
        h._startup.update(pid=None, tables=False, webhook=False, last_try=0.0)
        self.p = [mock.patch.object(h, "ensure_tables", return_value=True),
                  mock.patch.object(h, "ensure_webhook", return_value=True),
                  mock.patch.object(h, "start_reminder_thread"),
                  mock.patch.object(h, "reminder_health", return_value="alive")]
        self.tables, self.hook, self.thread, self.health = [x.start() for x in self.p]

    def tearDown(self):
        for x in self.p:
            x.stop()
        h._startup.clear(); h._startup.update(self.saved)

    def test_once_per_process(self):
        self.assertEqual(h.ensure_started(force=True), {"tables": True, "webhook": True})
        h.ensure_started(); h.ensure_started()
        self.assertEqual(self.tables.call_count, 1); self.assertEqual(self.hook.call_count, 1)

    def test_failure_retried_later_not_crash(self):
        self.hook.return_value = False
        with mock.patch.object(h.time, "time", return_value=1000.0):
            h.ensure_started(force=True)
        with mock.patch.object(h.time, "time", return_value=1010.0):
            h.ensure_started()  # throttled
        self.assertEqual(self.hook.call_count, 1)
        self.hook.return_value = True
        with mock.patch.object(h.time, "time", return_value=1000.0 + h.STARTUP_RETRY_S + 1):
            st = h.ensure_started()
        self.assertTrue(st["webhook"]); self.assertEqual(self.hook.call_count, 2)
        self.assertEqual(self.tables.call_count, 1)

    def test_pid_change_reruns(self):
        h.ensure_started(force=True)
        with mock.patch.object(h.os, "getpid", return_value=os.getpid() + 1):
            h.ensure_started(force=True)
        self.assertEqual(self.tables.call_count, 2); self.assertEqual(self.thread.call_count, 2)

    def test_ensure_webhook_never_raises(self):
        self.p[1].stop()
        try:
            with mock.patch.object(h, "WEBHOOK_BASE", None):
                self.assertFalse(h.ensure_webhook())
            with mock.patch.object(h, "WEBHOOK_BASE", "https://x"), \
                    mock.patch.object(h.bot, "set_webhook", side_effect=RuntimeError("telegram down")):
                self.assertFalse(h.ensure_webhook())
        finally:
            self.p[1].start()

    def test_before_request_lazy_startup(self):
        client = h.app.test_client()
        with mock.patch.object(h, "SKIP_STARTUP", False), mock.patch.object(h, "ensure_started") as es:
            r = client.get("/")
        self.assertEqual((r.status_code, r.get_data(as_text=True)), (200, "OK"))
        es.assert_called_once_with()
        with mock.patch.object(h, "SKIP_STARTUP", False), \
                mock.patch.object(h, "ensure_started", side_effect=RuntimeError("boom")):
            self.assertEqual(client.get("/").status_code, 200)


class TestHealth(unittest.TestCase):
    def get(self, db, rem_state, webhook):
        with mock.patch.object(h, "db_ping", return_value=db), \
                mock.patch.object(h, "reminder_health", return_value=rem_state), \
                mock.patch.object(h, "startup_status", return_value={"tables": True, "webhook": webhook}):
            r = h.app.test_client().get("/health")
        return r.status_code, r.get_json()

    def test_ok(self):
        code, body = self.get(True, "alive", True)
        self.assertEqual(code, 200); self.assertTrue(body["ok"])
        self.assertEqual((body["db"], body["reminders"], body["webhook"]), (True, "alive", True))

    def test_disabled_reminders_still_ok(self):
        self.assertEqual(self.get(True, "disabled", True)[0], 200)

    def test_503_cases(self):
        for args in ((False, "alive", True), (True, "dead", True), (True, "alive", False)):
            code, body = self.get(*args)
            self.assertEqual(code, 503, args); self.assertFalse(body["ok"])

    def test_db_ping_uses_select_1(self):
        conn = good_conn(); conn.cursor.return_value.fetchone.return_value = (1,)
        with mock.patch.object(h, "DB_URL", "postgres://x"), \
                mock.patch.object(pa, "get_conn", return_value=conn), mock.patch.object(pa, "return_conn"):
            self.assertTrue(h.db_ping())
        conn.cursor.return_value.execute.assert_called_with("SELECT 1")


class TestGunicornConf(unittest.TestCase):
    def test_settings_and_hook(self):
        cfg = runpy.run_path(os.path.join(HERE, "gunicorn.conf.py"))
        self.assertIs(cfg["preload_app"], False)
        self.assertEqual((cfg["workers"], cfg["threads"], cfg["timeout"]), (1, 1, 120))
        self.assertIn("graceful_timeout", cfg)
        worker = mock.MagicMock()
        with mock.patch.object(h, "SKIP_STARTUP", False), \
                mock.patch.object(h, "ensure_started", return_value={"tables": True, "webhook": True}) as es:
            cfg["post_worker_init"](worker)
        es.assert_called_once_with(force=True)
        with mock.patch.object(h, "SKIP_STARTUP", False), \
                mock.patch.object(h, "ensure_started", side_effect=RuntimeError("db down")):
            cfg["post_worker_init"](worker)  # must not raise
        worker.log.error.assert_called_once()


if __name__ == "__main__":
    unittest.main()
