# -*- coding: utf-8 -*-
"""Google Sheet export: env parsing (raw/base64), timeout, retry, cache reset,
outbox without duplicates, status batch_update / missing row, test-order mark,
startup check, /health field, off mode."""
import base64
import json
import os
import threading
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import requests

import test_ads_start_help  # noqa: F401  (sets SKIP_STARTUP env, imports app)
import docubridge_sheets as sh
import docubridge_db_outbox as dbo
import docubridge_db_pool_a as pa
import docubridge_handlers as h

ADMIN = 777
SA = {"type": "service_account", "client_email": "bot@x.iam.gserviceaccount.com",
      "private_key": "-----BEGIN PRIVATE KEY-----\nSECRETKEYDATA\n-----END PRIVATE KEY-----\n"}
ENV_ON = {"GOOGLE_SA_JSON": json.dumps(SA), "SHEET_ID": "sheet123"}
ENV_OFF = {"GOOGLE_SA_JSON": "", "SHEET_ID": ""}
SHIP = {"ticket": "DB5001", "chat_id": 555, "status": "accepted",
        "payload": {"from_country": "Украина", "to_country": "Россия", "role": "sender",
                    "name": "Иван", "phone": "+380501112233", "weight_grams": 300,
                    "doc_type": "диплом", "ref": "ua_ru"},
        "created_at": datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)}


class APIError(Exception):
    def __init__(self, code):
        super().__init__(f"APIError {code}")
        self.response = SimpleNamespace(status_code=code)


class FakeSheet:
    """In-memory worksheet: column A tickets."""
    def __init__(self, tickets=()):
        self.rows = [list(sh.SHEET_HEADER)] + [[t] + [""] * 16 for t in tickets]
        self.title = "Лист1"
        self.batch = []

    def col_values(self, n):
        return [r[n - 1] for r in self.rows]

    def append_row(self, row, value_input_option=None):
        self.rows.append(list(row))

    def batch_update(self, req, value_input_option=None):
        self.batch.append(req)

    def tickets(self):
        return [r[0] for r in self.rows[1:]]


class SheetsBase(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV_ON); self.env.start()
        sh._ws_cache = None
        sh._health.update(status=None, error=None)
        self.sleeps = []
        self.p_sleep = mock.patch.object(sh, "_sleep", side_effect=self.sleeps.append); self.p_sleep.start()
        self.p_admin = mock.patch.object(sh.cfg, "ADMIN_CHAT_ID", ADMIN); self.p_admin.start()

    def tearDown(self):
        mock.patch.stopall()
        sh._ws_cache = None
        sh._health.update(status=None, error=None)


class TestEnvParsing(unittest.TestCase):
    def test_raw_json(self):
        self.assertEqual(sh.load_sa_info(json.dumps(SA)), SA)

    def test_base64_and_base64url_without_padding_and_newlines(self):
        raw = json.dumps(SA).encode()
        b64 = base64.b64encode(raw).decode()
        self.assertEqual(sh.load_sa_info(b64), SA)
        url = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        self.assertEqual(sh.load_sa_info(url), SA)
        wrapped = "\n".join(b64[i:i + 60] for i in range(0, len(b64), 60))
        self.assertEqual(sh.load_sa_info(wrapped), SA)

    def test_errors_do_not_leak_key(self):
        for bad in ("", "{not json SECRETKEYDATA", "%%%SECRETKEYDATA%%%",
                    json.dumps({"type": "user", "private_key": "SECRETKEYDATA"})):
            with self.assertRaises(ValueError) as cm:
                sh.load_sa_info(bad)
            self.assertNotIn("SECRETKEYDATA", str(cm.exception))


class TestClientTimeoutAndCache(SheetsBase):
    def test_timeout_set_and_header_written(self):
        gc = mock.MagicMock()
        ws = gc.open_by_key.return_value.sheet1
        ws.row_values.return_value = []
        with mock.patch("gspread.authorize", return_value=gc) as auth, \
                mock.patch("google.oauth2.service_account.Credentials.from_service_account_info") as cr:
            self.assertIs(sh._get_worksheet(), ws)
            self.assertIs(sh._get_worksheet(), ws)          # cached
        self.assertEqual(auth.call_count, 1)
        cr.assert_called_once()
        self.assertEqual(cr.call_args[0][0], SA)
        gc.http_client.set_timeout.assert_called_once_with((5, 15))
        gc.open_by_key.assert_called_once_with("sheet123")
        ws.append_row.assert_called_once_with(sh.SHEET_HEADER, value_input_option="USER_ENTERED")

    def test_base64_env_accepted_by_client(self):
        gc = mock.MagicMock(); gc.open_by_key.return_value.sheet1.row_values.return_value = ["Ticket"]
        with mock.patch.dict(os.environ, {"GOOGLE_SA_JSON": base64.b64encode(json.dumps(SA).encode()).decode()}), \
                mock.patch("gspread.authorize", return_value=gc), \
                mock.patch("google.oauth2.service_account.Credentials.from_service_account_info") as cr:
            sh._get_worksheet()
        self.assertEqual(cr.call_args[0][0], SA)


class TestRetry(SheetsBase):
    def test_retries_429_and_5xx_then_ok_and_resets_cache(self):
        ws = FakeSheet()
        calls = {"n": 0}

        def get_ws():
            calls["n"] += 1
            if calls["n"] == 1:
                raise APIError(429)
            if calls["n"] == 2:
                raise APIError(503)
            return ws

        sh._ws_cache = "stale"
        with mock.patch.object(sh, "_get_worksheet", side_effect=get_ws):
            self.assertEqual(sh.with_retry("t", lambda w: "done"), "done")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(self.sleeps, [1.0, 3.0])
        self.assertEqual(sh.sheets_health(), "ok")

    def test_network_error_and_timeout_are_retryable(self):
        self.assertTrue(sh.is_retryable(requests.exceptions.ConnectTimeout("t")))
        self.assertTrue(sh.is_retryable(requests.exceptions.ReadTimeout("t")))
        self.assertTrue(sh.is_retryable(requests.exceptions.ConnectionError("c")))
        self.assertTrue(sh.is_retryable(APIError(500)))
        self.assertFalse(sh.is_retryable(APIError(403)))
        self.assertFalse(sh.is_retryable(ValueError("bad json")))

    def test_non_retryable_fails_fast_and_cache_dropped(self):
        sh._ws_cache = mock.MagicMock()
        with self.assertRaises(APIError):
            sh.with_retry("t", lambda w: (_ for _ in ()).throw(APIError(403)))
        self.assertIsNone(sh._ws_cache)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(sh.sheets_health(), "error")

    def test_gives_up_after_3_attempts(self):
        with mock.patch.object(sh, "_get_worksheet", side_effect=APIError(502)) as g:
            with self.assertRaises(APIError):
                sh.with_retry("t", lambda w: 1)
        self.assertEqual(g.call_count, sh.SHEETS_ATTEMPTS)
        self.assertEqual(len(self.sleeps), sh.SHEETS_ATTEMPTS - 1)


class FakeOutboxDB:
    """claim/mark/release semantics of docubridge_db_outbox in memory (thread-safe)."""
    def __init__(self, ships):
        self.ships = {s["ticket"]: dict(s, synced=False, claimed=False) for s in ships}
        self.lock = threading.Lock()

    def claim(self, t):
        with self.lock:
            s = self.ships.get(t)
            if not s or s["synced"] or s["claimed"]:
                return False
            s["claimed"] = True
            return True

    def mark(self, t):
        with self.lock:
            self.ships[t].update(synced=True, claimed=False)
        return True

    def release(self, t):
        with self.lock:
            self.ships[t]["claimed"] = False

    def unsynced(self, limit=50):
        return [dict(s) for s in self.ships.values() if not s["synced"] and not s["claimed"]][:limit]

    def patch(self):
        return [mock.patch.object(sh, "claim_sheet_sync", side_effect=self.claim),
                mock.patch.object(sh, "mark_sheet_synced", side_effect=self.mark),
                mock.patch.object(sh, "release_sheet_claim", side_effect=self.release),
                mock.patch.object(sh, "list_unsynced_shipments", side_effect=self.unsynced)]


class TestOutbox(SheetsBase):
    def setUp(self):
        super().setUp()
        self.ws = FakeSheet()
        mock.patch.object(sh, "_get_worksheet", return_value=self.ws).start()

    def db(self, ships):
        d = FakeOutboxDB(ships)
        for p in d.patch():
            p.start()
        return d

    def test_backfill_exports_all_old_orders_once(self):
        ships = [dict(SHIP, ticket=f"DB50{i}") for i in range(3)]
        d = self.db(ships)
        self.assertEqual(sh.sync_pending_once(), 3)
        self.assertEqual(sh.sync_pending_once(), 0)          # second tick: nothing left
        self.assertEqual(self.ws.tickets(), ["DB500", "DB501", "DB502"])
        self.assertTrue(all(s["synced"] for s in d.ships.values()))
        row = self.ws.rows[1]
        self.assertEqual(row[1], "2026-11-01 10:00")          # created_at, not «now»
        self.assertEqual(row[10], 140)
        self.assertEqual(row[12], "'+380501112233")
        self.assertEqual(row[14], "принято")

    def test_ticket_already_in_sheet_not_appended_again(self):
        self.ws = FakeSheet(["DB5001"])
        sh._get_worksheet.return_value = self.ws
        d = self.db([SHIP])
        self.assertTrue(sh.sync_shipment(dict(SHIP)))
        self.assertEqual(self.ws.tickets(), ["DB5001"])
        self.assertTrue(d.ships["DB5001"]["synced"])

    def test_new_order_and_outbox_race_single_row(self):
        d = self.db([SHIP])
        barrier = threading.Barrier(4)

        def go():
            barrier.wait()
            sh.sync_shipment(dict(SHIP))
        ts = [threading.Thread(target=go) for _ in range(4)]
        [t.start() for t in ts]; [t.join(5) for t in ts]
        sh.sync_pending_once()
        self.assertEqual(self.ws.tickets(), ["DB5001"])
        self.assertTrue(d.ships["DB5001"]["synced"])

    def test_claim_taken_elsewhere_no_google_call(self):
        with mock.patch.object(sh, "claim_sheet_sync", return_value=False):
            self.assertFalse(sh.sync_shipment(dict(SHIP)))
        sh._get_worksheet.assert_not_called()

    def test_failure_releases_claim_and_next_tick_retries(self):
        d = self.db([SHIP, dict(SHIP, ticket="DB5002")])
        sh._get_worksheet.side_effect = APIError(403)
        self.assertEqual(sh.sync_pending_once(), 0)
        self.assertEqual(sh._get_worksheet.call_count, 1)    # stopped at first failure (Google down)
        self.assertFalse(any(s["claimed"] or s["synced"] for s in d.ships.values()))
        sh._get_worksheet.side_effect = None
        sh._get_worksheet.return_value = self.ws
        self.assertEqual(sh.sync_pending_once(), 2)

    def test_export_async_on_new_order(self):
        self.db([SHIP])
        t = sh.export_shipment_async(555, dict(SHIP["payload"], ticket="DB5001"), 140)
        t.join(3)
        self.assertEqual(self.ws.tickets(), ["DB5001"])
        self.assertEqual(self.ws.rows[1][10], 140)

    def test_admin_test_order_exported_with_mark(self):
        self.db([dict(SHIP, chat_id=ADMIN)])
        sh.sync_pending_once()
        row = self.ws.rows[1]
        self.assertEqual(row[0], "DB5001")
        self.assertEqual(row[sh.ROLE_IDX], "тест: sender")
        self.assertTrue(row[sh.COMMENT_IDX].startswith("тест"))
        plain = sh.build_row(555, SHIP["payload"])
        self.assertEqual(plain[sh.ROLE_IDX], "sender"); self.assertEqual(plain[sh.COMMENT_IDX], "")

    def test_status_batch_update_without_comment_keeps_comment_cell(self):
        self.ws = FakeSheet(["DB1", "DB5001"])
        sh._get_worksheet.return_value = self.ws
        sh._update_status("DB5001", "в пути", None)
        req = self.ws.batch[0][0]
        self.assertEqual(req["range"], "O3:P3")
        self.assertEqual(req["values"][0][0], "в пути")

    def test_status_on_missing_row_appends_current_row(self):
        d = self.db([dict(SHIP, status="at_hub")])
        d.ships["DB5001"]["synced"] = True                   # was «synced», row deleted by hand
        with mock.patch("docubridge_db_state.get_shipment_by_ticket",
                        return_value=dict(SHIP, status="at_hub")):
            sh._update_status("DB5001", "на хабе", "CDEK 1")
        self.assertEqual(self.ws.tickets(), ["DB5001"])
        row = self.ws.rows[1]
        self.assertEqual(row[sh.COMMENT_IDX], "CDEK 1")
        self.assertEqual(row[14], "на хабе")
        self.assertEqual(self.ws.batch, [])


class TestCheckAndHealth(SheetsBase):
    def test_check_ok_and_error_logs(self):
        with mock.patch.object(sh, "_get_worksheet", return_value=FakeSheet()), \
                mock.patch("builtins.print") as pr:
            self.assertTrue(sh.sheets_check())
        self.assertIn("[Sheets] OK", pr.call_args[0][0])
        self.assertEqual(sh.sheets_health(), "ok")
        with mock.patch.object(sh, "_get_worksheet", side_effect=APIError(403)), \
                mock.patch("builtins.print") as pr:
            self.assertFalse(sh.sheets_check())
        line = pr.call_args[0][0]
        self.assertIn("[Sheets] ERROR", line); self.assertIn("Редактор", line)
        self.assertEqual(sh.sheets_health(), "error")

    def test_pending_before_first_check(self):
        self.assertEqual(sh.sheets_health(), "pending")

    def test_health_field_does_not_change_ok(self):
        for state in ("off", "ok", "error"):
            with mock.patch.object(h, "db_ping", return_value=True), \
                    mock.patch.object(h, "reminder_health", return_value="alive"), \
                    mock.patch.object(h, "sheets_health", return_value=state), \
                    mock.patch.object(h, "startup_status", return_value={"tables": True, "webhook": True}):
                r = h.app.test_client().get("/health")
            self.assertEqual(r.status_code, 200, state)
            self.assertEqual(r.get_json()["sheets"], state)
            self.assertTrue(r.get_json()["ok"])

    def test_startup_starts_sheets_thread(self):
        saved = dict(h._startup)
        h._startup.update(pid=None, tables=False, webhook=False, last_try=0.0)
        try:
            with mock.patch.object(h, "ensure_tables", return_value=True), \
                    mock.patch.object(h, "ensure_webhook", return_value=True), \
                    mock.patch.object(h, "start_reminder_thread"), \
                    mock.patch.object(h, "start_sheets_thread", side_effect=RuntimeError("x")) as st:
                self.assertEqual(h.ensure_started(force=True), {"tables": True, "webhook": True})
            st.assert_called_once()
        finally:
            h._startup.clear(); h._startup.update(saved)

    def test_thread_one_per_process(self):
        started = []
        with mock.patch.object(sh, "_loop", side_effect=lambda: started.append(1) or threading.Event().wait(2)):
            sh._thread = None
            t1 = sh.start_sheets_thread(); t2 = sh.start_sheets_thread()
        self.assertIs(t1, t2)
        sh._thread = None


class TestOffMode(unittest.TestCase):
    def test_everything_noop_without_env(self):
        with mock.patch.dict(os.environ, ENV_OFF), \
                mock.patch.object(sh, "_get_worksheet") as g, \
                mock.patch.object(sh, "list_unsynced_shipments") as lst:
            self.assertEqual(sh.sheets_health(), "off")
            self.assertIsNone(sh.start_sheets_thread())
            self.assertEqual(sh.sync_pending_once(), 0)
            self.assertFalse(sh.sheets_check())
            self.assertIsNone(sh.export_shipment_async(1, {"ticket": "DB1"}))
            self.assertIsNone(sh.update_status_async("DB1", "принято"))
        g.assert_not_called(); lst.assert_not_called()


class TestOutboxSQL(unittest.TestCase):
    def run_sql(self, fn, *args, fetch=("DB1",)):
        conn = mock.MagicMock()
        cur = conn.cursor.return_value
        cur.fetchone.return_value = fetch
        cur.fetchall.return_value = []
        seen = {}

        def fake_db_run(label, f, default=None, retry=True, getconn=None, putconn=None, discard=None):
            seen["retry"] = retry
            return f(conn)
        with mock.patch.object(dbo, "DB_URL", "postgres://x"), mock.patch.object(dbo, "db_run", side_effect=fake_db_run):
            res = fn(*args)
        return res, " ".join(c[0][0] for c in cur.execute.call_args_list), seen

    def test_claim_is_atomic_lease_without_retry(self):
        res, sql, seen = self.run_sql(dbo.claim_sheet_sync, "DB1")
        self.assertTrue(res)
        self.assertIn("sheet_synced_at IS NULL", sql); self.assertIn("RETURNING", sql)
        self.assertIs(seen["retry"], False)
        res, _, _ = self.run_sql(dbo.claim_sheet_sync, "DB1", fetch=None)
        self.assertFalse(res)

    def test_list_unsynced_sql(self):
        _, sql, _ = self.run_sql(dbo.list_unsynced_shipments, 10)
        self.assertIn("sheet_synced_at IS NULL", sql); self.assertIn("ORDER BY created_at", sql)

    def test_ensure_tables_adds_outbox_columns_and_broadcast_log(self):
        conn = mock.MagicMock()
        with mock.patch.object(pa, "DB_URL", "postgres://x"), \
                mock.patch.object(pa, "get_conn", return_value=conn), mock.patch.object(pa, "return_conn"):
            self.assertTrue(pa.ensure_tables())
        sql = " ".join(c[0][0] for c in conn.cursor.return_value.execute.call_args_list)
        self.assertIn("ADD COLUMN IF NOT EXISTS sheet_synced_at", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS sheet_claimed_at", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS broadcast_log", sql)
        self.assertIn("PRIMARY KEY (campaign, chat_id)", sql)


if __name__ == "__main__":
    unittest.main()
