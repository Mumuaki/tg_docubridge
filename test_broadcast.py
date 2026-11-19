# -*- coding: utf-8 -*-
"""/broadcast_stuck: verbatim text, buttons, dry run (nothing sent to users),
single-use confirm token, atomic claim, 20 msg/s, 429 retry_after, 403 blocked,
exclusions, one run at a time, report, «Продолжить расчёт» → new funnel."""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from telebot.apihelper import ApiTelegramException

import test_ads_start_help  # noqa: F401  (sets SKIP_STARTUP env, imports app)
import docubridge_handlers as h  # noqa: F401  (registers handlers)
import docubridge_broadcast as bc
import docubridge_admin as adm
import docubridge_db_outbox as dbo

ADMIN = 777
APPROVED_TEXT = (
    "Вы недавно рассматривали возможность отправки документов с DocuBridge, но так и не сделали заказ. "
    "Мы упростили оформление: теперь это 4 коротких шага с кнопками. Продолжить можно здесь 👉 /start\n"
    "Если у Вас остались вопросы, пишите менеджеру @is_logix"
)
COUNTS = {"by_state": [("ask_from_city", 5), ("greeting", 7), ("quote_shown", 2)],
          "started": 7, "greeting": 7, "already": {}}


def tg_err(code, retry_after=None):
    rj = {"ok": False, "error_code": code, "description": "x"}
    if retry_after is not None:
        rj["parameters"] = {"retry_after": retry_after}
    return ApiTelegramException("sendMessage", SimpleNamespace(status_code=code), rj)


def msg(text, chat_id=ADMIN):
    return SimpleNamespace(text=text, chat=SimpleNamespace(id=chat_id))


def call(data, user=ADMIN, chat=ADMIN):
    return SimpleNamespace(id="cb1", data=data, from_user=SimpleNamespace(id=user),
                           message=SimpleNamespace(chat=SimpleNamespace(id=chat)))


class Base(unittest.TestCase):
    def setUp(self):
        for mod in (bc, adm):
            mock.patch.object(mod, "ADMIN_CHAT_ID", ADMIN).start()
        self.send = mock.patch.object(bc.bot, "send_message").start()
        self.answer = mock.patch.object(bc.bot, "answer_callback_query").start()
        self.sleeps = []
        mock.patch.object(bc, "_sleep", side_effect=self.sleeps.append).start()
        bc._pending.clear()

    def tearDown(self):
        mock.patch.stopall()
        bc._pending.clear()
        if bc._run_lock.locked():
            bc._run_lock.release()


class TestTextAndButtons(Base):
    def test_text_is_verbatim(self):
        self.assertEqual(bc.BROADCAST_TEXT, APPROVED_TEXT)
        self.assertEqual(bc.BROADCAST_TEXT.count("\n"), 1)
        self.assertIn("👉 /start", bc.BROADCAST_TEXT)

    def test_buttons(self):
        rows = bc.broadcast_keyboard().keyboard
        self.assertEqual([b.text for r in rows for b in r], ["Продолжить расчёт", "Связаться с менеджером"])
        self.assertEqual(rows[0][0].callback_data, "bcu:go")
        self.assertEqual(rows[1][0].url, "https://t.me/is_logix")
        with mock.patch.object(bc, "MANAGER_CONTACT", "+380 00 000"):
            self.assertEqual(bc.broadcast_keyboard().keyboard[1][0].callback_data, "bcu:mgr")


class TestDryRun(Base):
    def test_dry_run_sends_only_to_admin_and_claims_nothing(self):
        with mock.patch.object(bc, "count_broadcast_candidates", return_value=COUNTS) as cnt, \
                mock.patch.object(bc, "claim_broadcast") as claim, \
                mock.patch.object(bc, "find_broadcast_candidates") as find:
            bc.admin_broadcast_stuck(msg("/broadcast_stuck"))
        cnt.assert_called_once_with(bc.BROADCAST_CAMPAIGN, ADMIN)
        claim.assert_not_called(); find.assert_not_called()
        self.assertTrue(all(c[0][0] == ADMIN for c in self.send.call_args_list))
        texts = [c[0][1] for c in self.send.call_args_list]
        self.assertIn("пробный прогон", texts[0])
        self.assertIn("Начали оформление: 7", texts[0]); self.assertIn("Только /start: 7", texts[0])
        self.assertIn("город отправки: 5", texts[0])
        self.assertEqual(texts[1], APPROVED_TEXT)                       # preview
        kb = self.send.call_args_list[2][1]["reply_markup"].keyboard
        labels = [r[0].text for r in kb]
        self.assertEqual(labels, ["Отправить 7 — начали оформление", "Отправить 7 — только /start",
                                  "Отправить 14 — все", "Отмена"])
        self.assertEqual(len(bc._pending), 1)

    def test_db_down_and_nobody(self):
        with mock.patch.object(bc, "count_broadcast_candidates", return_value=None):
            bc.admin_broadcast_stuck(msg("/broadcast_stuck"))
        self.assertIn("нет подключения", self.send.call_args[0][1])
        self.send.reset_mock()
        with mock.patch.object(bc, "count_broadcast_candidates",
                               return_value={"by_state": [], "started": 0, "greeting": 0, "already": {}}):
            bc.admin_broadcast_stuck(msg("/broadcast_stuck"))
        self.assertEqual(self.send.call_args[0][1], "Отправлять некому.")

    def test_command_registered_admin_only(self):
        hs = [x for x in bc.bot.message_handlers
              if "broadcast_stuck" in (x["filters"].get("commands") or [])]
        self.assertEqual(len(hs), 1)
        self.assertFalse(hs[0]["filters"]["func"](msg("/broadcast_stuck", chat_id=555)))
        self.assertTrue(hs[0]["filters"]["func"](msg("/broadcast_stuck")))
        # registered before the catch-all text handler
        idx = bc.bot.message_handlers.index(hs[0])
        catch_all = [i for i, x in enumerate(bc.bot.message_handlers)
                     if x["function"].__name__ == "any_text"][0]
        self.assertLess(idx, catch_all)


class TestConfirm(Base):
    def test_non_admin_cannot_confirm(self):
        tok = bc._new_token()
        with mock.patch.object(bc, "start_broadcast") as sb:
            bc.admin_broadcast_confirm(call(f"bca:{tok}:all", user=555))
        sb.assert_not_called()
        self.assertEqual(self.answer.call_args[0][1], "Нет доступа")

    def test_token_single_use_and_ttl(self):
        tok = bc._new_token()
        with mock.patch.object(bc, "start_broadcast", return_value=mock.MagicMock()) as sb:
            bc.admin_broadcast_confirm(call(f"bca:{tok}:started"))
            bc.admin_broadcast_confirm(call(f"bca:{tok}:started"))
        sb.assert_called_once_with("started", ADMIN)
        self.assertIn("уже использована", self.answer.call_args[0][1])
        old = bc._new_token()
        bc._pending[old]["created"] = time.time() - bc.TOKEN_TTL_S - 1
        self.assertIn("10 минут", bc.take_token(old))
        self.assertIn("устарело", bc.take_token("nope"))

    def test_cancel_sends_nothing(self):
        tok = bc._new_token()
        with mock.patch.object(bc, "start_broadcast") as sb:
            bc.admin_broadcast_confirm(call(f"bca:{tok}:cancel"))
        sb.assert_not_called()
        self.assertIn("ничего не отправлено", self.send.call_args[0][1])

    def test_one_run_at_a_time(self):
        gate = threading.Event()
        with mock.patch.object(bc, "run_broadcast", side_effect=lambda *a: gate.wait(3)):
            t1 = bc.start_broadcast("all", ADMIN)
            self.assertIsNotNone(t1)
            self.assertIsNone(bc.start_broadcast("all", ADMIN))
            with mock.patch.object(bc, "count_broadcast_candidates") as cnt:
                bc.admin_broadcast_stuck(msg("/broadcast_stuck"))
            cnt.assert_not_called()
            self.assertIn("уже идёт", self.send.call_args[0][1])
            gate.set(); t1.join(3)
        self.assertFalse(bc._run_lock.locked())
        with mock.patch.object(bc, "run_broadcast"):
            t2 = bc.start_broadcast("all", ADMIN); t2.join(3)
        self.assertIsNotNone(t2)

    def test_crash_releases_lock_and_reports(self):
        with mock.patch.object(bc, "run_broadcast", side_effect=RuntimeError("db gone")):
            bc.start_broadcast("all", ADMIN).join(3)
        self.assertFalse(bc._run_lock.locked())
        self.assertIn("прервана", self.send.call_args[0][1])


class TestRun(Base):
    def run_with(self, rows, claimed, outcomes):
        """outcomes: chat_id -> list of side effects for successive send attempts."""
        results = []
        it = {k: iter(v) for k, v in outcomes.items()}

        def fake_send(chat_id, text, reply_markup=None):
            if chat_id == ADMIN:
                return None                   # admin progress/report
            self.assertEqual(text, APPROVED_TEXT)
            eff = next(it[chat_id])
            if isinstance(eff, Exception):
                raise eff
            return None
        self.send.side_effect = fake_send
        with mock.patch.object(bc, "find_broadcast_candidates", return_value=rows) as find, \
                mock.patch.object(bc, "claim_broadcast", side_effect=lambda c, cid, s: cid in claimed) as claim, \
                mock.patch.object(bc, "set_broadcast_result",
                                  side_effect=lambda c, cid, st, err=None: results.append((cid, st))), \
                mock.patch.object(bc, "save_message"):
            stats = bc.run_broadcast("all", ADMIN)
        return stats, results, find, claim

    def test_statuses_throttle_429_and_report(self):
        rows = [{"chat_id": c} for c in (1, 2, 3, 4, 5, ADMIN)]
        stats, results, find, claim = self.run_with(
            rows, claimed={1, 2, 4, 5, ADMIN},
            outcomes={1: [None], 2: [tg_err(403)], 4: [tg_err(429, retry_after=7), None],
                      5: [tg_err(400)]})
        find.assert_called_once_with(bc.BROADCAST_CAMPAIGN, "all", ADMIN)
        self.assertEqual(stats, {"sent": 2, "blocked": 1, "failed": 1, "skipped": 2})
        self.assertEqual(results, [(1, "sent"), (2, "blocked"), (4, "sent"), (5, "failed")])
        self.assertNotIn(ADMIN, [c[0][1] for c in claim.call_args_list])   # admin never claimed
        self.assertIn(7.5, self.sleeps)                                      # retry_after honoured
        self.assertEqual(self.sleeps.count(bc.SEND_INTERVAL_S), 4)          # one pause per send
        self.assertAlmostEqual(bc.SEND_INTERVAL_S, 0.05)
        report = self.send.call_args[0][1]
        self.assertEqual(self.send.call_args[0][0], ADMIN)
        self.assertIn("Отправлено: 2", report); self.assertIn("Заблокировали бота: 1", report)
        self.assertIn("Ошибок: 1", report); self.assertIn("Пропущено (уже было): 2", report)

    def test_claim_before_send(self):
        order = []
        self.send.side_effect = lambda *a, **k: order.append("send")
        with mock.patch.object(bc, "find_broadcast_candidates", return_value=[{"chat_id": 9}]), \
                mock.patch.object(bc, "claim_broadcast", side_effect=lambda *a: order.append("claim") or True), \
                mock.patch.object(bc, "set_broadcast_result"), mock.patch.object(bc, "save_message"):
            bc.run_broadcast("started", ADMIN)
        self.assertEqual(order[:2], ["claim", "send"])

    def test_429_exhausted_is_failed_not_resent(self):
        stats, results, _, _ = self.run_with(
            [{"chat_id": 1}], claimed={1}, outcomes={1: [tg_err(429, 1)] * (bc.MAX_429_RETRIES + 1)})
        self.assertEqual(results, [(1, "failed")])
        self.assertEqual(stats["failed"], 1)

    def test_progress_every_100(self):
        rows = [{"chat_id": c} for c in range(1000, 1205)]
        self.run_with(rows, claimed={r["chat_id"] for r in rows},
                      outcomes={r["chat_id"]: [None] for r in rows})
        notes = [c[0][1] for c in self.send.call_args_list if c[0][0] == ADMIN]
        self.assertEqual(len([n for n in notes if n.startswith("…")]), 2)


class TestSQL(unittest.TestCase):
    def test_candidate_sql_exclusions(self):
        s = dbo.CANDIDATES_SQL
        for part in ("NOT EXISTS (SELECT 1 FROM shipments", "NOT EXISTS (SELECT 1 FROM leads",
                     "FROM broadcast_log b", "us.chat_id <> %(admin)s",
                     "'completed', 'await_track', '__db_error__'",
                     "us.state <> 'greeting'", "us.state = 'greeting'"):
            self.assertIn(part, s)
        a = dbo._cand_args("c1", "started", ADMIN)
        self.assertEqual((a["idle_h"], a["max_days"], a["admin"]), (48, 60, ADMIN))
        with self.assertRaises(ValueError):
            dbo._cand_args("c1", "everyone; DROP", ADMIN)

    def _run(self, fn, *args, fetchone=(1,), fetchall=()):
        conn = mock.MagicMock(); cur = conn.cursor.return_value
        cur.fetchone.return_value = fetchone; cur.fetchall.return_value = list(fetchall)
        seen = {}

        def fake(label, f, default=None, retry=True, getconn=None, putconn=None, discard=None):
            seen["retry"] = retry
            return f(conn)
        with mock.patch.object(dbo, "DB_URL", "postgres://x"), mock.patch.object(dbo, "db_run", side_effect=fake):
            res = fn(*args)
        return res, cur, seen

    def test_claim_atomic_no_retry(self):
        res, cur, seen = self._run(dbo.claim_broadcast, "c1", 5, "all")
        sql = cur.execute.call_args[0][0]
        self.assertIn("ON CONFLICT (campaign, chat_id) DO NOTHING", sql); self.assertIn("RETURNING", sql)
        self.assertTrue(res); self.assertIs(seen["retry"], False)
        res, _, _ = self._run(dbo.claim_broadcast, "c1", 5, "all", fetchone=None)
        self.assertFalse(res)

    def test_count_segments(self):
        conn = mock.MagicMock(); cur = conn.cursor.return_value
        cur.fetchall.side_effect = [[("greeting", 4), ("ask_phone", 2), ("ask_urgency", 1)], [("sent", 3)]]
        with mock.patch.object(dbo, "DB_URL", "postgres://x"), \
                mock.patch.object(dbo, "db_run", side_effect=lambda l, f, *a, **k: f(conn)):
            c = dbo.count_broadcast_candidates("c1", ADMIN)
        self.assertEqual((c["started"], c["greeting"], c["already"]), (3, 4, {"sent": 3}))

    def test_no_db(self):
        with mock.patch.object(dbo, "DB_URL", None):
            self.assertEqual(dbo.find_broadcast_candidates("c", "all", 1), [])
            self.assertIsNone(dbo.count_broadcast_candidates("c", 1))
            self.assertFalse(dbo.claim_broadcast("c", 1, "all"))


class TestUserButtons(Base):
    def flow(self, state, data):
        with mock.patch.object(bc, "get_state", return_value=(state, data)), \
                mock.patch.object(bc, "start_send_flow") as ssf:
            bc.broadcast_user_button(call("bcu:go", user=555, chat=555))
        return ssf

    def test_known_route_goes_to_offer(self):
        ssf = self.flow("ask_urgency", {"from_country": "Украина", "to_country": "Россия", "ref": "x"})
        ssf.assert_called_once_with(555, role="sender", ref="x", from_country="Украина", to_country="Россия")

    def test_route_from_ref(self):
        ssf = self.flow("greeting", {"ref": "ua_by"})
        self.assertEqual(ssf.call_args[1]["from_country"], "Украина")
        self.assertEqual(ssf.call_args[1]["to_country"], "Беларусь")

    def test_unknown_route_goes_to_choice(self):
        ssf = self.flow("ask_from_city", {})
        self.assertIsNone(ssf.call_args[1]["from_country"])

    def test_real_flow_shows_offer_or_choice(self):
        with mock.patch.object(bc, "get_state", return_value=("ask_from_city", {"ref": "ua_ru"})), \
                mock.patch("docubridge_flow_steps.set_state") as ss, mock.patch("docubridge_flow_steps.save_message"):
            bc.broadcast_user_button(call("bcu:go", user=555, chat=555))
        self.assertEqual(ss.call_args[0][1], "route_offer")
        with mock.patch.object(bc, "get_state", return_value=("greeting", {})), \
                mock.patch("docubridge_flow_steps.set_state") as ss, mock.patch("docubridge_flow_steps.save_message"):
            bc.broadcast_user_button(call("bcu:go", user=555, chat=555))
        self.assertEqual(ss.call_args[0][1], "choose_route")

    def test_manager_button(self):
        with mock.patch.object(bc, "contact_manager") as cm:
            bc.broadcast_user_button(call("bcu:mgr", user=555, chat=555))
        cm.assert_called_once_with(555)

    def test_callbacks_registered(self):
        datas = ["bcu:go", "bcu:mgr", "bca:tok:all"]
        for d in datas:
            hits = [x for x in bc.bot.callback_query_handlers if x["filters"]["func"](call(d))]
            self.assertTrue(hits, d)
            self.assertTrue(hits[0]["function"].__module__ == "docubridge_broadcast", d)


if __name__ == "__main__":
    unittest.main()
