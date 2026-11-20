# -*- coding: utf-8 -*-
"""Lead notifications: LEADS/ADMIN routing, test orders, status-button rights in a
group, group messages ignored, outbox retries / no duplicates / no legacy resend,
e-mail off/on (Resend, SMTP), daily digest once a day + time zones, Kyiv times,
Sheets «Уведомление» column. Real-Postgres tests run when DOCUBRIDGE_TEST_PG is set."""
import json
import os
import threading
import unittest
from datetime import datetime, timezone, timedelta, date
from types import SimpleNamespace
from unittest import mock

import test_ads_start_help  # noqa: F401  (sets SKIP_STARTUP env, imports app)
import main as m
import docubridge_handlers as h
import docubridge_admin as adm
import docubridge_keyboards as kbm
import docubridge_notify as nt
import docubridge_notify_cfg as ncfg
import docubridge_db_notify as ndb
import docubridge_email as em
import docubridge_sheets as sh
import docubridge_config as cfg
from docubridge_helpers import fmt_local, to_local

ADMIN = 361000001
GROUP = -1002000000001
MANAGER = 4242
STRANGER = 9999
CLIENT = 555
PAYLOAD = {"ticket": "DB5001", "from_country": "Беларусь", "from_city": "Минск",
           "to_country": "Украина", "to_city": "Одесса", "name": "Карина",
           "phone": "+380501112233", "role": "sender", "weight_grams": 300,
           "doc_type": "диплом", "urgency": "обычная"}
ENV_CLEAR = {k: "" for k in ("LEADS_CHAT_ID", "LEADS_MANAGER_IDS", "NOTIFY_TEST_LEADS", "NOTIFY_OUTBOX",
                             "LEADS_EMAIL_TO", "RESEND_API_KEY", "SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD",
                             "SMTP_PORT", "SMTP_FROM", "EMAIL_FROM", "NOTIFY_EMAIL", "DIGEST_ENABLED",
                             "DIGEST_HOUR", "DIGEST_TZ", "SHEET_NOTIFY_COL")}


class TgError(Exception):
    """Shape of telebot.apihelper.ApiTelegramException."""
    def __init__(self, code, description="err", **params):
        super().__init__(description)
        self.error_code = code
        self.description = description
        self.result_json = {"ok": False, "error_code": code, "description": description,
                            "parameters": params}


class FakeNDB:
    """In-memory notify_log / digest_log with the same claim semantics."""
    def __init__(self):
        self.rows = {}
        self.digest = {}
        self.shipments = []

    def enqueue_notify(self, ticket, rows):
        n = 0
        for r in rows:
            key = (ticket, r["channel"])
            if key not in self.rows:
                self.rows[key] = dict(status=r.get("status", "pending"), attempts=0, next_in=0, target=r.get("target"))
                n += 1
        return n

    def claim_notify(self, ticket, channel):
        r = self.rows.get((ticket, channel))
        if not r or r["status"] not in ("pending", "retry") or r["next_in"] > 0:
            return None
        r["status"] = "sending"
        r["attempts"] += 1
        return r["attempts"]

    def finish_notify(self, ticket, channel, status, message_id=None, error=None, retry_in_s=None, target=None):
        r = self.rows[(ticket, channel)]
        r.update(status=status, message_id=message_id, error=error, next_in=retry_in_s or 0)
        return True

    def due(self):
        for r in self.rows.values():
            r["next_in"] = 0          # time passes

    def list_due_notify(self, limit=20):
        return [{"ticket": t, "channel": c, "chat_id": CLIENT, "payload": PAYLOAD, "created_at": None}
                for (t, c), r in self.rows.items() if r["status"] in ("pending", "retry") and r["next_in"] <= 0]

    def list_unqueued_shipments(self, limit=20):
        return [s for s in self.shipments if not any(k[0] == s["ticket"] for k in self.rows)]

    def claim_digest(self, day):
        if self.digest.get(day) in ("sent", "sending"):
            return False
        self.digest[day] = "sending"
        return True

    def finish_digest(self, day, ok, info=""):
        self.digest[day] = "sent" if ok else "failed"

    def digest_shipments(self, exclude):
        return [s for s in self.shipments if s["chat_id"] != exclude]


class Base(unittest.TestCase):
    env = {}

    def setUp(self):
        mock.patch.dict(os.environ, dict(ENV_CLEAR, **self.env)).start()
        mock.patch.object(cfg, "ADMIN_CHAT_ID", ADMIN).start()
        mock.patch.object(adm, "ADMIN_CHAT_ID", ADMIN).start()
        mock.patch.object(kbm, "ADMIN_CHAT_ID", ADMIN).start()
        self.sent = []
        self.msg_id = 100

        def send(chat_id, text, reply_markup=None, **kw):
            self.msg_id += 1
            self.sent.append((chat_id, text, reply_markup))
            return SimpleNamespace(message_id=self.msg_id)
        self.send = mock.patch.object(m.bot, "send_message", side_effect=send).start()
        self.sheet_mark = mock.patch.object(sh, "update_notify_async").start()

    def tearDown(self):
        mock.patch.stopall()

    def with_db(self):
        """Outbox mode with the in-memory notify DB; first attempt runs inline."""
        self.db = FakeNDB()
        mock.patch.object(cfg, "DB_URL", "postgres://x").start()
        for name in ("enqueue_notify", "claim_notify", "finish_notify", "list_due_notify",
                     "list_unqueued_shipments", "claim_digest", "finish_digest", "digest_shipments"):
            mock.patch.object(ndb, name, side_effect=getattr(self.db, name)).start()
        mock.patch.object(nt, "_spawn", side_effect=lambda fn, *a: fn(*a)).start()
        return self.db


# ------------ 1. routing ------------

class TestRouting(Base):
    def test_no_leads_chat_goes_to_admin_with_buttons(self):
        kbm.notify_admin_lead(CLIENT, dict(PAYLOAD))
        self.assertEqual(len(self.sent), 1)
        chat, text, markup = self.sent[0]
        self.assertEqual(chat, ADMIN)
        self.assertIn("Новый лид DocuBridge", text)
        self.assertIn("Минск", text); self.assertIn("(Киев)", text)
        self.assertNotIn("ТЕСТ", text)
        self.assertEqual(markup.keyboard[0][0].callback_data.split(":")[:2], ["st", "DB5001"])

    def test_leads_chat_set_goes_to_group(self):
        with mock.patch.dict(os.environ, {"LEADS_CHAT_ID": str(GROUP)}):
            kbm.notify_admin_lead(CLIENT, dict(PAYLOAD))
        self.assertEqual([s[0] for s in self.sent], [GROUP])
        self.assertIsNotNone(self.sent[0][2])

    def test_admin_test_order_without_leads_chat_skipped(self):
        with mock.patch("builtins.print") as pr:
            kbm.notify_admin_lead(ADMIN, dict(PAYLOAD))
        self.assertEqual(self.sent, [])
        self.assertIn("skip (test mode)", " ".join(str(c[0][0]) for c in pr.call_args_list))

    def test_admin_test_order_with_leads_chat_goes_there_marked(self):
        with mock.patch.dict(os.environ, {"LEADS_CHAT_ID": str(GROUP)}):
            kbm.notify_admin_lead(ADMIN, dict(PAYLOAD))
        self.assertEqual(self.sent[0][0], GROUP)
        self.assertTrue(self.sent[0][1].startswith("🧪 ТЕСТ"))

    def test_test_orders_can_be_switched_off(self):
        with mock.patch.dict(os.environ, {"LEADS_CHAT_ID": str(GROUP), "NOTIFY_TEST_LEADS": "0"}):
            kbm.notify_admin_lead(ADMIN, dict(PAYLOAD))
        self.assertEqual(self.sent, [])

    def test_no_target_logs(self):
        with mock.patch.object(cfg, "ADMIN_CHAT_ID", 0), mock.patch.object(kbm, "ADMIN_CHAT_ID", 0), \
                mock.patch("builtins.print") as pr:
            kbm.notify_admin_lead(CLIENT, dict(PAYLOAD))
        self.assertEqual(self.sent, [])
        self.assertIn("не задан", " ".join(str(c[0][0]) for c in pr.call_args_list))

    def test_send_error_never_raises(self):
        self.send.side_effect = TgError(400, "Bad Request: chat not found")
        kbm.notify_admin_lead(CLIENT, dict(PAYLOAD))       # direct mode: logged, not raised

    def test_card_without_pages_has_no_dash_pages(self):
        text = nt.build_lead_card(CLIENT, PAYLOAD)
        self.assertIn("Объём: вес ≈ 300 г", text)
        self.assertNotIn("листов —", text)

    def test_failed_order_alert_goes_to_target(self):
        with mock.patch.dict(os.environ, {"LEADS_CHAT_ID": str(GROUP)}):
            kbm.notify_admin_order_failed(CLIENT, dict(PAYLOAD))
        self.assertEqual(self.sent[0][0], GROUP)


# ------------ 2. group: rights + ignore ------------

def _call(data, chat_id, chat_type, user_id, username="mgr"):
    return SimpleNamespace(id="1", data=data,
                           message=SimpleNamespace(chat=SimpleNamespace(id=chat_id, type=chat_type)),
                           from_user=SimpleNamespace(id=user_id, username=username, first_name="M"))


class TestGroupRights(Base):
    env = {"LEADS_CHAT_ID": str(GROUP), "LEADS_MANAGER_IDS": f"{MANAGER}, 77"}

    def setUp(self):
        super().setUp()
        self.ans = mock.patch.object(m.bot, "answer_callback_query").start()
        self.apply = mock.patch.object(adm, "apply_status_change", return_value=(True, "✅ DB5001: «доставлено»")).start()

    def test_admin_in_group(self):
        adm.admin_status_button(_call("st:DB5001:delivered", GROUP, "supergroup", ADMIN))
        self.apply.assert_called_once_with("DB5001", "delivered")
        self.assertEqual(self.sent[0][0], GROUP)
        self.assertIn("@mgr", self.sent[0][1])

    def test_manager_in_group(self):
        adm.admin_status_button(_call("st:DB5001:at_hub", GROUP, "supergroup", MANAGER))
        self.apply.assert_called_once()

    def test_stranger_in_group_denied(self):
        adm.admin_status_button(_call("st:DB5001:delivered", GROUP, "supergroup", STRANGER))
        self.apply.assert_not_called()
        self.assertEqual(self.ans.call_args[0][1], "Нет доступа")

    def test_rights_by_user_not_chat(self):
        # pressed in the admin's private chat id but by someone else → denied
        adm.admin_status_button(_call("st:DB5001:delivered", ADMIN, "private", STRANGER))
        self.apply.assert_not_called()

    def test_manager_list_parsing(self):
        self.assertEqual(ncfg.manager_ids(), {MANAGER, 77})
        with mock.patch.dict(os.environ, {"LEADS_MANAGER_IDS": "x, 5;6"}):
            self.assertEqual(ncfg.manager_ids(), {5, 6})


def _upd(chat_type, text="привет", user=STRANGER, chat_id=GROUP):
    return SimpleNamespace(message=SimpleNamespace(chat=SimpleNamespace(id=chat_id, type=chat_type),
                                                   text=text, from_user=SimpleNamespace(id=user)),
                           edited_message=None, callback_query=None, channel_post=None,
                           edited_channel_post=None)


class TestGroupIgnored(Base):
    def test_update_filter(self):
        self.assertTrue(nt.update_allowed(_upd("private", chat_id=CLIENT)))
        self.assertFalse(nt.update_allowed(_upd("group")))
        self.assertFalse(nt.update_allowed(_upd("supergroup", "/start")))
        self.assertFalse(nt.update_allowed(_upd("supergroup", "/chatid", user=STRANGER)))
        self.assertTrue(nt.update_allowed(_upd("supergroup", "/chatid@islogix_bot", user=ADMIN)))
        self.assertTrue(nt.update_allowed(_upd("group", "/digest", user=ADMIN)))
        cq = SimpleNamespace(message=None, edited_message=None, channel_post=None, edited_channel_post=None,
                             callback_query=_call("st:DB1:at_hub", GROUP, "supergroup", STRANGER))
        self.assertTrue(nt.update_allowed(cq))      # rights are checked in the handler
        cq.callback_query = _call("hub:ua:ru", GROUP, "supergroup", ADMIN)
        self.assertFalse(nt.update_allowed(cq))
        ch = SimpleNamespace(message=None, edited_message=None, callback_query=None,
                             channel_post=object(), edited_channel_post=None)
        self.assertFalse(nt.update_allowed(ch))

    def _post(self, body):
        with mock.patch.object(h, "is_update_processed", return_value=False), \
                mock.patch.object(h, "check_update_and_state", return_value=None), \
                mock.patch.object(h, "mark_update_processed") as mark, \
                mock.patch.object(h, "handle_answer") as ha, \
                mock.patch.object(h, "start_send_flow") as ssf:
            r = h.app.test_client().post(f"/webhook/{h.WEBHOOK_SECRET}", data=json.dumps(body),
                                         headers={"content-type": "application/json"})
        return r, mark, ha, ssf

    def test_group_text_never_reaches_funnel(self):
        body = {"update_id": 77001, "message": {"message_id": 1, "date": 0, "text": "Оформить заявку",
                "chat": {"id": GROUP, "type": "supergroup", "title": "Заявки"},
                "from": {"id": STRANGER, "is_bot": False, "first_name": "X"}}}
        r, mark, ha, ssf = self._post(body)
        self.assertEqual(r.status_code, 200)
        ha.assert_not_called(); ssf.assert_not_called()
        mark.assert_called_once_with(77001)
        self.assertEqual(self.sent, [])

    def test_group_start_ignored(self):
        body = {"update_id": 77002, "message": {"message_id": 1, "date": 0, "text": "/start",
                "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
                "chat": {"id": GROUP, "type": "group", "title": "Заявки"},
                "from": {"id": ADMIN, "is_bot": False, "first_name": "A"}}}
        self._post(body)
        self.assertEqual(self.sent, [])

    def test_chatid_in_group_by_admin(self):
        body = {"update_id": 77003, "message": {"message_id": 1, "date": 0, "text": "/chatid",
                "entities": [{"type": "bot_command", "offset": 0, "length": 7}],
                "chat": {"id": GROUP, "type": "supergroup", "title": "Заявки"},
                "from": {"id": ADMIN, "is_bot": False, "first_name": "A"}}}
        self._post(body)
        self.assertEqual(self.sent[0][0], GROUP)
        self.assertIn(str(GROUP), self.sent[0][1])

    def test_catch_all_guard(self):
        with mock.patch.object(h, "handle_answer") as ha:
            h.any_text(SimpleNamespace(text="x", chat=SimpleNamespace(id=GROUP, type="group"),
                                       from_user=SimpleNamespace(id=1)))
        ha.assert_not_called()

    def test_bot_added_to_group_tells_admin(self):
        body = {"update_id": 77004, "my_chat_member": {
            "chat": {"id": GROUP, "type": "supergroup", "title": "Заявки DocuBridge"},
            "from": {"id": ADMIN, "is_bot": False, "first_name": "A"}, "date": 0,
            "old_chat_member": {"user": {"id": 1, "is_bot": True, "first_name": "b"}, "status": "left"},
            "new_chat_member": {"user": {"id": 1, "is_bot": True, "first_name": "b"}, "status": "member"}}}
        with mock.patch("builtins.print") as pr:
            self._post(body)
        self.assertEqual(self.sent[0][0], ADMIN)
        self.assertIn("«Заявки DocuBridge», id -1002000000001", self.sent[0][1])
        self.assertTrue(any("my_chat_member" in str(c[0][0]) for c in pr.call_args_list))


# ------------ 3. outbox ------------

class TestOutbox(Base):
    env = {"LEADS_CHAT_ID": str(GROUP)}

    def test_first_attempt_sent_once_and_logged(self):
        db = self.with_db()
        with mock.patch("builtins.print") as pr:
            kbm.notify_admin_lead(CLIENT, dict(PAYLOAD))
            nt.process_due_once(); nt.process_due_once()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(db.rows[("DB5001", "telegram")]["status"], "sent")
        self.assertTrue(any("[NOTIFY] sent DB5001 tg msg=101" in str(c[0][0]) for c in pr.call_args_list))
        self.sheet_mark.assert_called_with("DB5001", "да")

    def test_reply_not_waiting(self):
        """Outbox mode: notify_admin_lead only spawns."""
        mock.patch.object(cfg, "DB_URL", "postgres://x").start()
        with mock.patch.object(nt, "_spawn") as sp:
            kbm.notify_admin_lead(CLIENT, dict(PAYLOAD))
        sp.assert_called_once()
        self.assertEqual(self.sent, [])

    def test_retry_with_backoff_then_sent(self):
        db = self.with_db()
        self.send.side_effect = [ConnectionError("net down"), SimpleNamespace(message_id=7)]
        kbm.notify_admin_lead(CLIENT, dict(PAYLOAD))
        r = db.rows[("DB5001", "telegram")]
        self.assertEqual((r["status"], r["next_in"]), ("retry", 60))
        self.assertEqual(nt.process_due_once(), 0)          # not due yet
        db.due()
        self.assertEqual(nt.process_due_once(), 1)
        self.assertEqual((r["status"], r["message_id"], r["attempts"]), ("sent", 7, 2))

    def test_429_uses_retry_after(self):
        db = self.with_db()
        self.send.side_effect = TgError(429, "Too Many Requests", retry_after=17)
        kbm.notify_admin_lead(CLIENT, dict(PAYLOAD))
        self.assertEqual(db.rows[("DB5001", "telegram")]["next_in"], 17.0)

    def test_gives_up_after_max_attempts_and_alerts_admin(self):
        db = self.with_db()
        with mock.patch.dict(os.environ, {"NOTIFY_MAX_ATTEMPTS": "2"}):
            def send(chat_id, text, reply_markup=None, **kw):
                if chat_id == GROUP:
                    raise TgError(403, "Forbidden: bot was kicked")
                self.sent.append((chat_id, text, None))
                return SimpleNamespace(message_id=1)
            self.send.side_effect = send
            kbm.notify_admin_lead(CLIENT, dict(PAYLOAD))
            db.due(); nt.process_due_once()
            db.due(); nt.process_due_once()
        r = db.rows[("DB5001", "telegram")]
        self.assertEqual((r["status"], r["attempts"]), ("failed", 2))
        self.assertEqual(self.sent[0][0], ADMIN)
        self.assertIn("DB5001", self.sent[0][1])
        self.sheet_mark.assert_called_with("DB5001", "ошибка")

    def test_claim_lost_means_no_send(self):
        db = self.with_db()
        db.enqueue_notify("DB5001", [{"channel": "telegram", "status": "pending"}])
        db.claim_notify("DB5001", "telegram")                       # someone else holds it
        self.assertIsNone(nt.deliver("DB5001", "telegram", CLIENT, PAYLOAD))
        self.assertEqual(self.sent, [])

    def test_supergroup_migration_followed(self):
        self.with_db()
        new_id = -1009999
        calls = []

        def send(chat_id, text, reply_markup=None, **kw):
            calls.append(chat_id)
            if chat_id == GROUP:
                raise TgError(400, "group chat was upgraded to a supergroup chat", migrate_to_chat_id=new_id)
            return SimpleNamespace(message_id=5)
        self.send.side_effect = send
        kbm.notify_admin_lead(CLIENT, dict(PAYLOAD))
        self.assertEqual(calls, [GROUP, ADMIN, new_id])

    def test_lost_enqueue_is_swept(self):
        db = self.with_db()
        db.shipments.append({"ticket": "DB7777", "chat_id": CLIENT, "payload": PAYLOAD, "created_at": None})
        nt.process_due_once()
        self.assertEqual(db.rows[("DB7777", "telegram")]["status"], "sent")

    def test_test_lead_row_skipped_not_swept_again(self):
        db = self.with_db()
        with mock.patch.dict(os.environ, {"LEADS_CHAT_ID": ""}):
            kbm.notify_admin_lead(ADMIN, dict(PAYLOAD))
            nt.process_due_once()
        self.assertEqual(db.rows[("DB5001", "telegram")]["status"], "skipped")
        self.assertEqual(self.sent, [])

    def test_outbox_switch_off(self):
        mock.patch.object(cfg, "DB_URL", "postgres://x").start()
        with mock.patch.dict(os.environ, {"NOTIFY_OUTBOX": "0"}), mock.patch.object(nt, "_spawn") as sp:
            kbm.notify_admin_lead(CLIENT, dict(PAYLOAD))
            self.assertEqual(nt.process_due_once(), 0)
        sp.assert_not_called()
        self.assertEqual([s[0] for s in self.sent], [GROUP])          # direct send

    def test_thread_and_health(self):
        with mock.patch.object(cfg, "DB_URL", None):
            self.assertIsNone(nt.start_notify_thread())
            self.assertEqual(nt.notify_health(), "off")


# ------------ 4. e-mail ------------

class FakeResp:
    def __init__(self, code, body=None, headers=None):
        self.status_code, self._body, self.headers = code, body or {}, headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class TestEmail(Base):
    RESEND = {"LEADS_EMAIL_TO": "owner@example.com", "RESEND_API_KEY": "re_SECRET123"}

    def test_off_by_default(self):
        self.assertIsNone(ncfg.email_provider())
        self.assertEqual([r["channel"] for r in nt.plan_rows(CLIENT, PAYLOAD)], ["telegram"])

    def test_on_with_resend_and_not_for_tests(self):
        with mock.patch.dict(os.environ, self.RESEND):
            self.assertEqual(ncfg.email_provider(), "resend")
            self.assertEqual([r["channel"] for r in nt.plan_rows(CLIENT, PAYLOAD)], ["telegram", "email"])
            self.assertEqual([r["channel"] for r in nt.plan_rows(ADMIN, PAYLOAD)], ["telegram"])
            with mock.patch.dict(os.environ, {"NOTIFY_EMAIL": "0"}):
                self.assertIsNone(ncfg.email_provider())

    def test_smtp_needs_all_fields(self):
        with mock.patch.dict(os.environ, {"LEADS_EMAIL_TO": "a@b.c", "SMTP_HOST": "smtp.x", "SMTP_USER": "u"}):
            self.assertIsNone(ncfg.email_provider())
            with mock.patch.dict(os.environ, {"SMTP_PASSWORD": "pw"}):
                self.assertEqual(ncfg.email_provider(), "smtp")

    def test_resend_request_and_idempotency(self):
        with mock.patch.dict(os.environ, self.RESEND), \
                mock.patch("requests.post", return_value=FakeResp(200, {"id": "em_1"})) as post:
            self.assertEqual(em.send_lead_email("S", "B", "docubridge-lead-DB5001"), "em_1")
        kw = post.call_args[1]
        self.assertEqual(kw["timeout"], 10)
        self.assertEqual(kw["headers"]["Idempotency-Key"], "docubridge-lead-DB5001")
        self.assertEqual(kw["json"]["to"], ["owner@example.com"])
        self.assertEqual(kw["json"]["from"], em.DEFAULT_RESEND_FROM)

    def test_resend_errors_classified_and_secret_scrubbed(self):
        with mock.patch.dict(os.environ, self.RESEND):
            with mock.patch("requests.post", return_value=FakeResp(429, {"m": "slow"}, {"retry-after": "30"})):
                with self.assertRaises(em.EmailError) as c:
                    em.send_lead_email("S", "B", "k")
            self.assertTrue(c.exception.retryable); self.assertEqual(c.exception.retry_after, 30.0)
            with mock.patch("requests.post", return_value=FakeResp(401, {"m": "bad key re_SECRET123"})):
                with self.assertRaises(em.EmailError) as c:
                    em.send_lead_email("S", "B", "k")
            self.assertFalse(c.exception.retryable)
            self.assertNotIn("re_SECRET123", str(c.exception))

    def test_smtp_ssl_465_and_starttls_587(self):
        env = {"LEADS_EMAIL_TO": "a@b.c", "SMTP_HOST": "smtp.x", "SMTP_USER": "u", "SMTP_PASSWORD": "pw_SECRET"}
        with mock.patch.dict(os.environ, dict(env, SMTP_PORT="465")), \
                mock.patch("smtplib.SMTP_SSL") as ssl_cls, mock.patch("smtplib.SMTP") as plain:
            em.send_lead_email("S", "B", "k")
        ssl_cls.assert_called_once(); plain.assert_not_called()
        self.assertEqual(ssl_cls.call_args[1]["timeout"], 10)
        with mock.patch.dict(os.environ, dict(env, SMTP_PORT="587")), \
                mock.patch("smtplib.SMTP") as plain:
            em.send_lead_email("S", "B", "k")
        plain.return_value.__enter__.return_value.starttls.assert_called_once()

    def test_smtp_error_hides_password(self):
        env = {"LEADS_EMAIL_TO": "a@b.c", "SMTP_HOST": "smtp.x", "SMTP_USER": "u",
               "SMTP_PASSWORD": "pw_SECRET", "SMTP_PORT": "587"}
        with mock.patch.dict(os.environ, env), \
                mock.patch("smtplib.SMTP", side_effect=OSError("timed out pw_SECRET")):
            with self.assertRaises(em.EmailError) as c:
                em.send_lead_email("S", "B", "k")
        self.assertNotIn("pw_SECRET", str(c.exception))
        self.assertTrue(c.exception.retryable)

    def test_email_through_outbox_with_retry(self):
        db = self.with_db()
        with mock.patch.dict(os.environ, self.RESEND), \
                mock.patch("requests.post", side_effect=[FakeResp(500), FakeResp(200, {"id": "em_2"})]) as post:
            kbm.notify_admin_lead(CLIENT, dict(PAYLOAD))
            self.assertEqual(db.rows[("DB5001", "email")]["status"], "retry")
            db.due(); nt.process_due_once()
        self.assertEqual(db.rows[("DB5001", "email")]["status"], "sent")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(db.rows[("DB5001", "telegram")]["status"], "sent")
        self.assertIn("Минск", post.call_args[1]["json"]["text"])

    def test_permanent_email_error_fails_without_retry(self):
        db = self.with_db()
        with mock.patch.dict(os.environ, self.RESEND), \
                mock.patch("requests.post", return_value=FakeResp(422, {"m": "invalid from"})):
            kbm.notify_admin_lead(CLIENT, dict(PAYLOAD))
        self.assertEqual(db.rows[("DB5001", "email")]["status"], "failed")


# ------------ 5. digest ------------

def _ship(ticket, created, is_new, status="accepted", chat_id=CLIENT, pages=None):
    p = dict(PAYLOAD, ticket=ticket)
    if pages:
        p["pages_a4"] = pages
    return {"ticket": ticket, "chat_id": chat_id, "payload": p, "status": status,
            "created_at": created, "is_new": is_new}


UTC = timezone.utc


class TestDigest(Base):
    def setUp(self):
        super().setUp()
        nt._digest_done.update(day=None, pid=None)

    def test_empty(self):
        self.assertEqual(nt.build_digest([], "21.11.2026"),
                         "📋 Сводка DocuBridge 21.11.2026: за сутки заявок нет, необработанных нет.")

    def test_lines(self):
        rows = [_ship("DB1", datetime(2026, 11, 20, 12, 29, tzinfo=UTC), True),
                _ship("DB2", datetime(2026, 11, 18, 8, 52, tzinfo=UTC), False, pages=3),
                _ship("DB3", datetime(2026, 11, 17, 8, 0, tzinfo=UTC), False, status="delivered")]
        text = nt.build_digest(rows, "21.11.2026")
        self.assertIn("Новые за 24 ч (1):\nDB1 · 20.11 14:29 · Беларусь/Минск → Украина/Одесса · Карина · +380501112233", text)
        self.assertIn("(1):\nDB2 · 18.11 10:52 · Беларусь/Минск → Украина/Одесса · Карина · +380501112233 · 3 л.", text)
        self.assertNotIn("DB3", text)
        self.assertNotIn("— л.", text)
        self.assertIn("время — Киев", text)

    def test_once_a_day_after_hour_budapest(self):
        db = self.with_db()
        db.shipments.append(_ship("DB1", datetime(2026, 11, 20, 12, 0, tzinfo=UTC), True))
        db.shipments.append(_ship("DBT", datetime(2026, 11, 20, 12, 0, tzinfo=UTC), True, chat_id=ADMIN))
        # 07:59 UTC = 08:59 Budapest → too early
        self.assertFalse(nt.maybe_send_digest(datetime(2026, 11, 21, 7, 59, tzinfo=UTC)))
        self.assertTrue(nt.maybe_send_digest(datetime(2026, 11, 21, 8, 0, tzinfo=UTC)))
        self.assertFalse(nt.maybe_send_digest(datetime(2026, 11, 21, 8, 1, tzinfo=UTC)))
        nt._digest_done.update(day=None, pid=None)        # «restart»: DB marker still blocks
        self.assertFalse(nt.maybe_send_digest(datetime(2026, 11, 21, 15, 0, tzinfo=UTC)))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], ADMIN)
        self.assertIn("DB1", self.sent[0][1]); self.assertNotIn("DBT", self.sent[0][1])
        self.assertTrue(nt.maybe_send_digest(datetime(2026, 11, 22, 8, 30, tzinfo=UTC)))   # next day

    def test_late_wakeup_sends_same_day(self):
        self.with_db()
        self.assertTrue(nt.maybe_send_digest(datetime(2026, 11, 21, 21, 40, tzinfo=UTC)))

    def test_goes_to_leads_chat_and_hour_tz_env(self):
        self.with_db()
        with mock.patch.dict(os.environ, {"LEADS_CHAT_ID": str(GROUP), "DIGEST_HOUR": "18", "DIGEST_TZ": "Europe/Kyiv"}):
            self.assertFalse(nt.maybe_send_digest(datetime(2026, 11, 21, 15, 59, tzinfo=UTC)))  # 17:59 Kyiv
            self.assertTrue(nt.maybe_send_digest(datetime(2026, 11, 21, 16, 0, tzinfo=UTC)))
        self.assertEqual(self.sent[0][0], GROUP)

    def test_disabled(self):
        self.with_db()
        with mock.patch.dict(os.environ, {"DIGEST_ENABLED": "0"}):
            self.assertFalse(nt.maybe_send_digest(datetime(2026, 11, 21, 12, 0, tzinfo=UTC)))
        self.assertEqual(self.sent, [])

    def test_send_failure_not_marked_sent(self):
        db = self.with_db()
        self.send.side_effect = TgError(500, "Internal")
        self.assertFalse(nt.maybe_send_digest(datetime(2026, 11, 21, 9, 0, tzinfo=UTC)))
        self.assertEqual(db.digest[date(2026, 11, 21)], "failed")

    def test_digest_command_no_mark(self):
        db = self.with_db()
        nt.digest_cmd(SimpleNamespace(chat=SimpleNamespace(id=ADMIN, type="private"), text="/digest",
                                      from_user=SimpleNamespace(id=ADMIN)))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(db.digest, {})

    def test_long_digest_split(self):
        rows = [_ship(f"DB{i}", datetime(2026, 11, 20, 12, 0, tzinfo=UTC), False) for i in range(400)]
        parts = nt._chunks(nt.build_digest(rows, "x"))
        self.assertTrue(all(len(p) <= nt.TG_MAX_LEN for p in parts))
        self.assertIn("…и ещё 360", "\n".join(parts))


# ------------ 6. Kyiv time ------------

class TestKyivTime(unittest.TestCase):
    def test_utc_to_kyiv_with_label(self):
        dt = datetime(2026, 11, 18, 8, 52, tzinfo=UTC)
        self.assertEqual(fmt_local(dt), "18.11.2026 10:52")
        self.assertEqual(fmt_local(dt, label=True), "18.11.2026 10:52 (Киев)")
        self.assertEqual(fmt_local(datetime(2026, 7, 1, 8, 0, tzinfo=UTC)), "01.07.2026 11:00")   # summer +3

    def test_naive_untouched_and_none(self):
        self.assertEqual(fmt_local(datetime(2026, 10, 20, 10, 0)), "20.10.2026 10:00")
        self.assertEqual(fmt_local(None), "")
        self.assertIsNone(to_local(None))

    def test_track_and_order_card(self):
        row = {"ticket": "DB1", "chat_id": 1, "status": "accepted", "payload": PAYLOAD,
               "created_at": datetime(2026, 11, 18, 8, 52, tzinfo=UTC),
               "updated_at": datetime(2026, 11, 18, 8, 52, tzinfo=UTC)}
        track = m.format_track_message(row, [])
        self.assertIn("принято — 18.11.2026 10:52", track)
        self.assertIn("Время — Киев.", track)
        card = adm.format_order_card(row, [])
        self.assertIn("Создана: 18.11.2026 10:52 (Киев)", card)

    def test_sheets_stay_utc(self):
        self.assertEqual(sh._fmt_utc(datetime(2026, 11, 18, 10, 52, tzinfo=timezone(timedelta(hours=2)))),
                         "2026-11-18 08:52")


# ------------ 7. Sheets «Уведомление» column ------------

class NotifySheet:
    def __init__(self, header, cols=26):
        self.rows = [list(header)]
        self.col_count = cols
        self.cells = {}
        self.added = 0

    def row_values(self, n):
        return list(self.rows[n - 1]) if n <= len(self.rows) else []

    def col_values(self, n):
        return [r[n - 1] if len(r) >= n else "" for r in self.rows]

    def append_row(self, row, value_input_option=None):
        self.rows.append(list(row))

    def update_cell(self, r, c, v):
        self.cells[(r, c)] = v
        while len(self.rows[r - 1]) < c:
            self.rows[r - 1].append("")
        self.rows[r - 1][c - 1] = v

    def add_cols(self, n):
        self.added += n
        self.col_count += n


class TestSheetNotifyColumn(unittest.TestCase):
    def setUp(self):
        mock.patch.dict(os.environ, {"GOOGLE_SA_JSON": '{"type":"service_account","private_key":"k"}',
                                     "SHEET_ID": "s", "SHEET_NOTIFY_COL": ""}).start()
        sh._notify_col_ok.clear()

    def tearDown(self):
        mock.patch.stopall()
        sh._ws_cache = None
        sh._notify_col_ok.clear()

    def _use(self, ws):
        mock.patch.object(sh, "_get_worksheet", return_value=ws).start()
        sh._ws_cache = ws

    def test_header_added_at_end_and_value_written(self):
        ws = NotifySheet(sh.SHEET_HEADER, cols=17)
        self._use(ws)
        with mock.patch.object(sh, "notify_value", return_value="да"):
            sh._append_if_missing(["DB1"] + [""] * 16)
        self.assertEqual(ws.rows[0][:17], sh.SHEET_HEADER)
        self.assertEqual(ws.rows[0][17], "Уведомление")
        self.assertEqual(ws.added, 1)
        self.assertEqual(len(ws.rows[1]), 18); self.assertEqual(ws.rows[1][17], "да")
        sh._update_notify("DB1", "ошибка")
        self.assertEqual(ws.rows[1][17], "ошибка")

    def test_foreign_column_r_left_alone(self):
        ws = NotifySheet(sh.SHEET_HEADER + ["Мои заметки"])
        self._use(ws)
        sh._append_if_missing(["DB1"] + [""] * 16)
        self.assertEqual(ws.rows[0][17], "Мои заметки")
        self.assertEqual(len(ws.rows[1]), 17)
        sh._update_notify("DB1", "да")
        self.assertEqual(ws.cells, {})

    def test_switch_off(self):
        ws = NotifySheet(sh.SHEET_HEADER)
        self._use(ws)
        with mock.patch.dict(os.environ, {"SHEET_NOTIFY_COL": "0"}):
            sh._append_if_missing(["DB1"] + [""] * 16)
            self.assertIsNone(sh.update_notify_async("DB1", "да"))
        self.assertEqual(len(ws.rows[0]), 17); self.assertEqual(len(ws.rows[1]), 17)

    def test_value_mapping(self):
        with mock.patch.object(ndb, "get_notify_status", side_effect=["sent", "failed", "pending", None, "legacy"]):
            self.assertEqual([sh.notify_value("x") for _ in range(5)], ["да", "ошибка", "нет", "нет", "нет"])


# ------------ 8. real Postgres (optional) ------------

PG = os.getenv("DOCUBRIDGE_TEST_PG")


@unittest.skipUnless(PG, "DOCUBRIDGE_TEST_PG not set")
class TestRealPostgres(Base):
    env = {"LEADS_CHAT_ID": str(GROUP)}

    def setUp(self):
        super().setUp()
        import sys
        import psycopg2
        import docubridge_db_pool_a as pa
        self.pa = pa
        for name, mod in list(sys.modules.items()):
            if name.startswith("docubridge") and hasattr(mod, "DB_URL"):
                mock.patch.object(mod, "DB_URL", PG).start()
        pa.connection_pool, pa._pool_pid = None, None
        c = psycopg2.connect(PG); c.autocommit = True
        c.cursor().execute("DROP TABLE IF EXISTS notify_log, digest_log, shipments, leads, shipment_events CASCADE")
        c.close()
        self.assertTrue(pa.ensure_tables())
        import docubridge_db_state as ds
        self.ds = ds
        self.old = ds.create_shipment(CLIENT, dict(PAYLOAD))
        self.assertTrue(ndb.ensure_notify_tables())
        mock.patch.object(nt, "_spawn", side_effect=lambda fn, *a: fn(*a)).start()

    def tearDown(self):
        super().tearDown()
        if self.pa.connection_pool is not None:
            self.pa.connection_pool.closeall()
        self.pa.connection_pool, self.pa._pool_pid = None, None

    def _status(self, t, ch="telegram"):
        return ndb.get_notify_status(t, ch)

    def test_legacy_never_sent_new_sent_once(self):
        self.assertEqual(self._status(self.old), "legacy")
        self.assertEqual(self._status(self.old, "email"), "legacy")
        self.assertTrue(ndb.ensure_notify_tables())           # second run: no new legacy rows
        p = dict(PAYLOAD)
        t = self.ds.create_shipment(CLIENT, p)
        p["ticket"] = t
        kbm.notify_admin_lead(CLIENT, p)
        for _ in range(3):
            nt.process_due_once()
        self.assertEqual([s[0] for s in self.sent], [GROUP])
        self.assertIn(t, self.sent[0][1])
        self.assertEqual(self._status(t), "sent")
        self.assertEqual(self._status(self.old), "legacy")

    def test_concurrent_deliveries_send_once(self):
        t = self.ds.create_shipment(CLIENT, dict(PAYLOAD))
        ndb.enqueue_notify(t, nt.plan_rows(CLIENT, PAYLOAD))
        threads = [threading.Thread(target=nt.deliver, args=(t, "telegram", CLIENT, PAYLOAD)) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(len(self.sent), 1)
        self.assertIsNone(nt.deliver(t, "telegram", CLIENT, PAYLOAD))   # sent rows are never re-claimed
        self.assertEqual(len(self.sent), 1)

    def test_retry_schedules_next_at(self):
        t = self.ds.create_shipment(CLIENT, dict(PAYLOAD))
        ndb.enqueue_notify(t, nt.plan_rows(CLIENT, PAYLOAD))
        self.send.side_effect = TgError(502, "Bad Gateway")
        self.assertEqual(nt.deliver(t, "telegram", CLIENT, PAYLOAD), "retry")
        self.assertEqual(ndb.list_due_notify(), [])          # next_at in the future
        self.assertIsNone(ndb.claim_notify(t, "telegram"))

    def test_sweeper_and_digest_marker(self):
        import psycopg2
        t = self.ds.create_shipment(CLIENT, dict(PAYLOAD))
        c = psycopg2.connect(PG); c.autocommit = True
        c.cursor().execute("UPDATE shipments SET created_at = NOW() - INTERVAL '5 minutes' WHERE ticket = %s", (t,))
        c.close()
        nt.process_due_once()
        self.assertEqual(self._status(t), "sent")
        d = date(2026, 11, 21)
        self.assertTrue(ndb.claim_digest(d)); self.assertFalse(ndb.claim_digest(d))
        ndb.finish_digest(d, True)
        self.assertFalse(ndb.claim_digest(d))
        rows = ndb.digest_shipments(ADMIN)
        self.assertEqual({r["ticket"] for r in rows}, {self.old, t})


if __name__ == "__main__":
    unittest.main()
