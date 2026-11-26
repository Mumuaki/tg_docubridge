# -*- coding: utf-8 -*-
"""Bot leads the client alone: «Что дальше» after DBxxxx, status explanations, /track hint,
payment / handover from env with safe fallback + «не настроено» alert, hardened AI,
handoff to a human only on triggers (throttled), attachments."""
import os
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from test_ads_start_help import CHAT, m, fs, fpost
from test_funnel_complete import FunnelBase, USER, kb_texts
import docubridge_handoff as ho
import docubridge_next_steps as ns
import docubridge_admin as adm
import docubridge_db_notify as ndb
import docubridge_runtime as rt

GROUP = -5556349052
ENV_OFF = {"LEADS_CHAT_ID": str(GROUP), "DOCUBRIDGE_HANDOVER_TEXT": "", "DOCUBRIDGE_PAY_CURRENCIES": "",
           "DOCUBRIDGE_PAYMENT_TEXT_UAH": "", "DOCUBRIDGE_PAYMENT_TEXT_EUR": "", "DOCUBRIDGE_PAYMENT_TEXT_USD": ""}
ENV_ON = {"LEADS_CHAT_ID": str(GROUP), "DOCUBRIDGE_PAYMENT_TEXT_EUR": "Оплата по счёту X",
          "DOCUBRIDGE_HANDOVER_TEXT": "Передача по способу Y"}
UAH_TEXT = (
    "Отримувач: ФОП Саєнко Ігор\n"
    "IBAN: UA853220010000026002310087318\n"
    "ІПН/ЄДРПОУ: 2468719172\n"
    "Банк: Акціонерне товариство УНІВЕРСАЛ БАНК, МФО 322001, ЄДРПОУ банку 21133352\n"
    "Призначення платежу: за послуги з організації доставки"
)


def cb(data, chat=CHAT):
    return NS(id="1", data=data, message=NS(chat=NS(id=chat, type="private")), from_user=USER)


class Base(FunnelBase):
    def setUp(self):
        super().setUp()
        ho._mem_last.clear()
        for name in ("get_state", "set_state", "save_message"):
            p = mock.patch.object(ho, name, getattr(fs, name)); p.start(); self.patches.append(p)
        self.env = mock.patch.dict(os.environ, ENV_OFF); self.env.start(); self.patches.append(self.env)
        self.fwd = mock.patch.object(m.bot, "forward_message"); self.fwd_m = self.fwd.start()
        self.patches.append(self.fwd)

    def group_cards(self):
        return [c.args[1] for c in m.bot.send_message.call_args_list if c.args[0] == GROUP]

    def client_msgs(self):
        return [c.args[1] for c in m.bot.send_message.call_args_list if c.args[0] == CHAT]

    def confirm(self, ticket="DB7777"):
        with mock.patch.object(fpost, "create_shipment", return_value=ticket), \
                mock.patch.object(fpost, "notify_admin_lead"), mock.patch.object(fpost, "export_shipment_async"):
            self.say(m.BTN_CONFIRM)


class TestWhatNext(Base):
    def test_fallback_texts_and_buttons_and_alert_once(self):
        self.to_confirm(); self.confirm()
        thanks = self.client_msgs()[-2]
        self.assertIn("выберите валюту оплаты", thanks)
        self.assertIn(ns.HANDOVER_FALLBACK, thanks)
        self.assertIn("В какой валюте будете оплачивать заявку DB7777?", self.client_msgs()[-1])
        self.assertIn("€140 (конверт до 500 г)", thanks)
        self.assertIn("10–14 рабочих дней", thanks)
        cl = [c for c in m.bot.send_message.call_args_list if c.args[0] == CHAT]
        self.assertIsInstance(cl[-2].kwargs["reply_markup"], m.ReplyKeyboardMarkup)   # confirm keys replaced
        kb = cl[-1].kwargs["reply_markup"]
        self.assertIsInstance(kb, m.InlineKeyboardMarkup)
        self.assertEqual([b.callback_data for r in kb.keyboard for b in r],
                         ["pc:DB7777:UAH", "pc:DB7777:EUR", "pc:DB7777:USD",
                          "ns:hand:DB7777", "ns:track:DB7777", "ns:human:DB7777"])
        self.assertEqual([b.text for b in kb.keyboard[0]], ["UAH", "EUR", "USD"])
        cards = self.group_cards()
        self.assertEqual(len(cards), 1)
        self.assertIn("⚠️ Не настроено", cards[0])
        self.assertIn("DOCUBRIDGE_HANDOVER_TEXT", cards[0])
        self.assertIn("/order DB7777", cards[0])
        self.assertIn("Шаг: заявка оформлена", cards[0]); self.assertIn("Ірина Коваль", cards[0])
        self.assertFalse(ho.alert_not_configured(CHAT, "DB7777"))              # once per order

    def test_configured_env_shown_verbatim_no_alert(self):
        with mock.patch.dict(os.environ, ENV_ON):
            self.to_confirm(); self.confirm()
            thanks = self.client_msgs()[-2]
            self.assertIn("Передача по способу Y", thanks)
            self.assertEqual(self.group_cards(), [])
            ho.next_steps_cb(cb("ns:hand:DB7777")); self.assertIn("Передача по способу Y", self.last()[0])

    def test_overweight_goes_to_human(self):
        self.to_quote()
        self.states[CHAT][1]["weight_grams"] = 900
        self.say(m.BTN_APPLY, "Киев", "Минск", "✅ Ірина Коваль", "067 123 45 67")
        self.confirm()
        self.assertIn("по согласованию", self.client_msgs()[-2])
        self.assertTrue(any("вес больше 0,5 кг" in c for c in self.group_cards()))

    def test_no_discount_or_manager_call_promise(self):
        self.to_confirm(); self.confirm()
        t = self.client_msgs()[-2].lower()
        for w in ("скидк", "свяжется с вами по телефону"):
            self.assertNotIn(w, t)


class TestButtonsAndTrack(Base):
    def test_human_button_sends_card(self):
        ho.next_steps_cb(cb("ns:human:DB7777"))
        self.assertIn(m.MANAGER_CONTACT, self.last()[0])
        cards = self.group_cards()
        self.assertEqual(len(cards), 1)
        self.assertIn("клиент просит человека", cards[0])
        self.assertIn("DB7777", cards[0])

    def test_fallback_buttons(self):
        ho.next_steps_cb(cb("ns:pay:DB9"))
        self.assertIn("В какой валюте", self.last()[0])
        self.assertEqual(self.last()[1].keyboard[0][0].callback_data, "pc:DB9:UAH")
        ho.next_steps_cb(cb("ns:hand:DB9"))
        self.assertIn(ns.HANDOVER_FALLBACK, self.last()[0])

    def test_track_button_and_hint_own_chat_only(self):
        row = {"ticket": "DB7777", "chat_id": CHAT, "status": "accepted", "created_at": None}
        with mock.patch.object(fs, "get_shipment_by_ticket", return_value=row), \
                mock.patch.object(fs, "get_shipment_events", return_value=[]):
            ho.next_steps_cb(cb("ns:track:DB7777"))
            self.assertIn("Что дальше", self.last()[0])
            self.assertIn("выберите валюту", self.last()[0])
            fs.handle_track_ticket(999, "DB7777")
            self.assertNotIn("Что дальше", self.last()[0])
            row["status"] = "at_hub"
            fs.handle_track_ticket(CHAT, "DB7777")
            self.assertIn("Следующий статус придёт сюда автоматически", self.last()[0])

    def test_status_message_explains(self):
        t = adm.client_status_message("DB7777", "departed_hub", "TRK1")
        self.assertIn("отправлено в хаб", t); self.assertIn("TRK1", t)
        self.assertIn("Следующий статус придёт сюда автоматически", t)
        self.assertIn("/track и номер DB7777", t)
        self.assertIn("доставлено", adm.client_status_message("DB7777", "delivered", None))
        self.assertIn("Спасибо", adm.client_status_message("DB7777", "delivered", None))


class TestHandoffTriggers(Base):
    def test_detect(self):
        for t, r in (("хочу скидку", "discount"), ("а можно дешевле?", "discount"),
                     ("это жалоба, документы потеряли", "complaint"), ("верните деньги", "complaint"),
                     ("позовите менеджера", "human"), ("нужен живой человек", "human")):
            self.assertEqual(ho.detect_handoff(t), r, t)
        for t in ("Киев", "паспорт можно?", "сколько стоит", "Доверенность", "/start", "Ірина Коваль"):
            self.assertIsNone(ho.detect_handoff(t), t)

    def test_discount_on_step_keeps_step_no_ai(self):
        self.to_quote(); self.say(m.BTN_APPLY, "а скидку дадите?")
        self.assertEqual(self.state(), "ask_from_city")
        text, kb = self.last()
        self.assertIn("по тарифу", text); self.assertIn("€140", text)
        self.assertIn("Из какого города", text); self.assertIn("Киев", kb_texts(kb))
        self.fai_m.assert_not_called(); self.ai_m.assert_not_called()
        cards = self.group_cards()
        self.assertEqual(len(cards), 1)
        self.assertIn("просьба о скидке", cards[0]); self.assertIn("а скидку дадите?", cards[0])
        self.assertIn("@u", cards[0])

    def test_throttle_30_min(self):
        self.say("хочу поговорить с человеком", "позовите менеджера", "это жалоба")
        self.assertEqual(len(self.group_cards()), 1)
        self.assertEqual(len([t for t in self.client_msgs() if m.MANAGER_CONTACT in t]), 3)
        with mock.patch.object(ho.time, "time", return_value=ho.time.time() + 31 * 60):
            self.say("позовите менеджера")
        self.assertEqual(len(self.group_cards()), 2)

    def test_manager_menu_button_keeps_behaviour_and_notifies(self):
        self.to_quote(); self.say(m.BTN_APPLY, m.MENU_MANAGER)
        self.assertEqual(self.state(), "ask_from_city")
        self.assertIn(m.MANAGER_CONTACT, self.last()[0])
        self.assertEqual(len(self.group_cards()), 1)

    def test_no_card_without_target(self):
        with mock.patch.dict(os.environ, {"LEADS_CHAT_ID": ""}):
            self.say("хочу скидку")
        self.assertEqual(self.group_cards(), [])
        self.assertIn(m.MANAGER_CONTACT, self.last()[0])

    def test_db_throttle_used_when_available(self):
        with mock.patch.object(ndb, "claim_handoff", return_value=False) as c:
            self.say("хочу скидку")
        c.assert_called_once()
        self.assertEqual(self.group_cards(), [])


class TestPaymentFaq(Base):
    def test_payment_question_on_step_canned(self):
        self.to_quote(); self.say(m.BTN_APPLY, "а как оплатить?")
        self.assertEqual(self.state(), "ask_from_city")
        self.assertIn("после оформления заявки", self.last()[0])
        self.fai_m.assert_not_called()
        self.assertEqual(self.group_cards(), [])

    def test_handover_question_in_menu(self):
        self.states[CHAT] = ("greeting", {})
        self.say("как передать документы?")
        self.assertIn("Передача документов", self.last()[0])
        self.ai_m.assert_not_called()

    def test_payment_after_order_asks_currency_then_shows_chosen(self):
        self.states[CHAT] = ("completed", {"ticket": "DB7777"})
        self.say("куда платить?")
        self.assertIn("В какой валюте будете оплачивать заявку DB7777?", self.last()[0])
        with mock.patch.dict(os.environ, ENV_ON):
            self.states[CHAT] = ("completed", {"ticket": "DB7777", "pay_currency": "EUR"})
            self.say("куда платить?")
            self.assertIn("Оплата по счёту X", self.last()[0])


class TestCurrency(Base):
    def choose(self, cur, stored=True, ticket="DB7777"):
        with mock.patch.object(ndb, "set_pay_currency", return_value=stored) as sp:
            ho.currency_cb(cb(f"pc:{ticket}:{cur}"))
        return sp

    def test_currencies_from_env(self):
        import docubridge_payment_config as pcfg
        self.assertEqual(pcfg.currencies(), ["UAH", "EUR", "USD"])
        with mock.patch.dict(os.environ, {"DOCUBRIDGE_PAY_CURRENCIES": "eur, pln;bad1,EUR"}):
            self.assertEqual(pcfg.currencies(), ["EUR", "PLN"])
            kb = ns.currency_keyboard("DB1", with_actions=False)
            self.assertEqual([b.callback_data for b in kb.keyboard[0]], ["pc:DB1:EUR", "pc:DB1:PLN"])

    def test_uah_shows_approved_details_verbatim(self):
        self.states[CHAT] = ("completed", {"ticket": "DB7777"})
        sp = self.choose("UAH")
        sp.assert_called_once_with("DB7777", CHAT, "UAH")
        text = self.client_msgs()[-1]
        self.assertEqual(text, "💳 Реквизиты для оплаты заявки DB7777 (UAH):\n" + UAH_TEXT)
        self.assertNotIn("DB7777", text.split("Призначення платежу:")[1])   # no ticket in purpose
        for w in ("грн", "курс", "₴"):
            self.assertNotIn(w, text)                                     # no amount / rate
        self.assertEqual(self.states[CHAT], ("completed", {"ticket": "DB7777", "pay_currency": "UAH"}))
        self.assertEqual(self.group_cards(), ["💱 DB7777: клиент выбрал UAH, реквизиты выданы ботом."])

    def test_eur_fallback_and_group_alert_once(self):
        self.choose("EUR")
        self.assertEqual(self.client_msgs()[-1],
                         "💳 Оплата заявки DB7777 в EUR: реквизиты пришлём отдельным сообщением в этом чате.")
        self.assertEqual(self.group_cards(), ["⚠️ DB7777: клиент выбрал EUR, реквизитов нет, пришлите вручную"])
        self.choose("EUR")
        self.assertEqual(len(self.group_cards()), 1)

    def test_env_overrides_text(self):
        with mock.patch.dict(os.environ, {"DOCUBRIDGE_PAYMENT_TEXT_UAH": "Нові реквізити\\nрядок 2",
                                          "DOCUBRIDGE_PAYMENT_TEXT_USD": "USD реквизиты"}):
            self.choose("UAH")
            self.assertIn("Нові реквізити\nрядок 2", self.client_msgs()[-1])
            self.assertNotIn("UA853220010000026002310087318", self.client_msgs()[-1])
            self.choose("USD")
            self.assertIn("USD реквизиты", self.client_msgs()[-1])
            self.assertIn("💱 DB7777: клиент выбрал USD, реквизиты выданы ботом.", self.group_cards())

    def test_stale_currency_button_keeps_state(self):
        self.states[CHAT] = ("ask_name", {"from_city": "Киев"})
        sp = self.choose("PLN")
        sp.assert_not_called()
        self.assertEqual(self.states[CHAT], ("ask_name", {"from_city": "Киев"}))
        self.assertIn("устарела", self.client_msgs()[-1])
        self.assertEqual(self.group_cards(), [])

    def test_foreign_or_unknown_order_button_keeps_state(self):
        self.states[CHAT] = ("ask_doc_type", {"x": 1})
        self.choose("UAH", stored=False, ticket="DB1111")
        self.assertEqual(self.states[CHAT], ("ask_doc_type", {"x": 1}))
        self.assertIn("не найдена", self.client_msgs()[-1])
        self.assertNotIn("UA853220010000026002310087318", self.texts())
        self.assertEqual(self.group_cards(), [])

    def test_other_order_in_state_not_touched_and_db_down_noted(self):
        self.states[CHAT] = ("completed", {"ticket": "DB2222"})
        self.choose("EUR", stored=None)
        self.assertEqual(self.states[CHAT], ("completed", {"ticket": "DB2222"}))
        self.assertIn("БД недоступна", self.group_cards()[0])

    def test_change_currency_button(self):
        self.choose("EUR")
        kb = [c for c in m.bot.send_message.call_args_list if c.args[0] == CHAT][-1].kwargs["reply_markup"]
        self.assertEqual(kb.keyboard[0][0].callback_data, "ns:pay:DB7777")
        ho.next_steps_cb(cb("ns:pay:DB7777"))
        self.assertIn("В какой валюте", self.last()[0])

    def test_thanks_says_nothing_about_when_or_how_much_to_pay(self):
        t = ns.thanks_text("DB1", "A → B", {"weight_grams": 100}).lower()
        for w in ("предоплат", "до передачи", "после передачи", "100%", "грн", "курс"):
            self.assertNotIn(w, t)


class TestAiFallbackHandoff(Base):
    def test_unknown_question_in_menu_goes_to_human(self):
        self.ai_m.return_value = "Сейчас умные ответы не знают ответа на этот вопрос."
        self.states[CHAT] = ("greeting", {})
        self.say("а у вас есть страховка?")
        self.assertIn(m.MANAGER_CONTACT, self.last()[0])
        self.assertTrue(any("вопрос, на который бот не ответил" in c for c in self.group_cards()))

    def test_ai_answer_used_when_known(self):
        self.states[CHAT] = ("greeting", {})
        self.say("а это надёжный сервис?")
        self.assertIn("Ответ AI", self.last()[0])
        self.assertEqual(self.group_cards(), [])


class TestAttachments(Base):
    def test_photo_forwarded_step_kept(self):
        self.to_quote(); self.say(m.BTN_APPLY)
        msg = NS(chat=NS(id=CHAT, type="private"), content_type="photo", caption="вот скан",
                 message_id=42, from_user=USER)
        ho.attachment_received(msg)
        self.assertEqual(self.state(), "ask_from_city")
        self.assertIn("только текст и кнопки", self.last()[0])
        cards = self.group_cards()
        self.assertEqual(len(cards), 1)
        self.assertIn("клиент прислал вложение", cards[0]); self.assertIn("фото", cards[0])
        self.assertIn("вот скан", cards[0])
        self.fwd_m.assert_called_once_with(GROUP, CHAT, 42)

    def test_group_attachment_ignored(self):
        msg = NS(chat=NS(id=GROUP, type="group"), content_type="document", caption=None,
                 message_id=1, from_user=USER)
        ho.attachment_received(msg)
        self.assertEqual(m.bot.send_message.call_count, 0)

    def test_registered_for_attachment_types(self):
        hs = [x for x in m.bot.message_handlers if x["function"] is ho.attachment_received]
        self.assertEqual(len(hs), 1)
        self.assertIn("photo", hs[0]["filters"]["content_types"])
        self.assertIn("document", hs[0]["filters"]["content_types"])


class TestAiPromptAndDdl(unittest.TestCase):
    def test_prompt_facts_and_rules(self):
        p = rt.AI_SYSTEM_PROMPT
        for w in ("€140", "500 г", "10–14", "паспорта", "Скидок", "реквизиты", rt.AI_UNKNOWN_MARK):
            self.assertIn(w, p)

    def test_unknown_mark_becomes_fallback_and_low_temperature(self):
        fake = mock.MagicMock()
        fake.chat.completions.create.return_value = NS(choices=[NS(message=NS(content="[менеджер]"))])
        with mock.patch.object(rt, "client", fake):
            out = rt.ai_reply("какие законы?")
        self.assertTrue(out.lower().startswith(rt.AI_FAIL_PREFIXES))
        kw = fake.chat.completions.create.call_args.kwargs
        self.assertLessEqual(kw["temperature"], 0.2)
        self.assertEqual(kw["messages"][0]["content"], rt.AI_SYSTEM_PROMPT)

    def test_handoff_ddl_idempotent(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS handoff_log", ndb.NOTIFY_DDL)
        self.assertIn("CREATE INDEX IF NOT EXISTS handoff_log_chat_idx", ndb.NOTIFY_DDL)

    def test_claim_handoff_without_db_is_none(self):
        with mock.patch.object(ndb, "DB_URL", None):
            self.assertIsNone(ndb.claim_handoff(1, "client", "human", None, 30))


if __name__ == "__main__":
    unittest.main()
