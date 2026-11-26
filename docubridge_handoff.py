# -*- coding: utf-8 -*-
"""Hand a client over to a human only in non-standard cases.

Triggers (checked BEFORE the AI):
  * the client asks for a person (also the «Связаться с менеджером» / «Нужен человек» buttons);
  * a discount request; a complaint / dispute;
  * a non-standard attachment (photo, file, voice, video …);
  * an order the bot cannot finish alone: weight > 0.5 kg («по согласованию»),
    payment details / handover method not configured by the owner (alert «⚠️ не настроено»);
  * a question the AI could not answer.
Everything else is answered by the bot itself (FAQ, price from helpers, /track).

The card goes to the same Telegram target as the lead cards (LEADS_CHAT_ID, else
ADMIN_CHAT_ID). At most one card per chat per HANDOFF_WINDOW_MIN (handoff_log in the DB;
in-memory fallback when the DB is unavailable). Never raises into the request.
"""
import re
import threading
import time
from typing import Dict, Optional

from docubridge_runtime import *  # noqa: F401,F403
from docubridge_keyboards import main_menu
import docubridge_notify_cfg as ncfg
import docubridge_db_notify as ndb
import docubridge_next_steps as ns
from docubridge_funnel import prompt_step, FUNNEL_STATES, STEP_TITLES_RU

HANDOFF_WINDOW_MIN = 30
CONFIG_WINDOW_MIN = 60 * 24 * 365 * 10   # «не настроено» alert: once per order
TEXT_MAX = 500

REASON_RU = {
    "human": "клиент просит человека",
    "discount": "просьба о скидке",
    "complaint": "жалоба / спор",
    "attachment": "клиент прислал вложение",
    "overweight": "вес больше 0,5 кг — нужен расчёт",
    "not_configured": "не настроены данные для клиента",
    "question": "вопрос, на который бот не ответил",
}

_HUMAN_RE = re.compile(
    r"\bменеджер|\bоператор|живой человек|живого человека|с человеком|позовите|"
    r"\bпозвоните|\bперезвоните|\bсвяжитесь со мной|нужен человек"
)
_DISCOUNT_RE = re.compile(r"\bскидк|\bдешевл|\bподешевле|\bуступит|\bторг(?:уетесь|уемся|анутьс)|\bпромокод")
_COMPLAINT_RE = re.compile(
    r"\bжалоб|\bпожаловат|\bпретензи|\bвозврат|\bверните|\bобман|\bмошенн|\bпотеряли|"
    r"не доставил|не пришл|не получил|\bразвод"
)

ATTACHMENT_TYPES = ["photo", "document", "video", "voice", "audio", "video_note", "animation"]
ATTACHMENT_RU = {
    "photo": "фото", "document": "файл", "video": "видео", "voice": "голосовое",
    "audio": "аудио", "video_note": "видеосообщение", "animation": "GIF",
}

_STEP_RU = dict(STEP_TITLES_RU, greeting="меню", choose_route="выбор маршрута",
                completed="заявка оформлена", edit_choose="исправление заявки",
                await_track="ввод номера для отслеживания")

_mem_lock = threading.Lock()
_mem_last: Dict[tuple, float] = {}       # (chat, scope) → ts, used only when the DB is down


def detect_handoff(text: Optional[str]) -> Optional[str]:
    """'complaint' | 'discount' | 'human' | None for a free-text message."""
    low = (text or "").strip().lower()
    if not low or low.startswith("/"):
        return None
    if _COMPLAINT_RE.search(low):
        return "complaint"
    if _DISCOUNT_RE.search(low):
        return "discount"
    if _HUMAN_RE.search(low):
        return "human"
    return None


def client_reply(reason: str, ticket: Optional[str] = None) -> str:
    tail = f" Заявка {ticket} сохранена." if ticket else ""
    if reason == "discount":
        return (f"Стоимость считается по тарифу: от €{PRICE_ORDINARY_EUR} за конверт до {ENVELOPE_MAX_G} г. "
                f"Ваш вопрос передал менеджеру {MANAGER_CONTACT} — он ответит вам в Telegram.{tail}")
    if reason == "attachment":
        return ("Бот понимает только текст и кнопки. Ваше вложение передал менеджеру "
                f"{MANAGER_CONTACT} — он ответит вам в Telegram.{tail}")
    return f"Передал ваш вопрос менеджеру {MANAGER_CONTACT} — он ответит вам в Telegram.{tail}"


def _who(user) -> str:
    if user is None:
        return "—"
    un = getattr(user, "username", None)
    name = " ".join(p for p in (getattr(user, "first_name", None) or "",
                                getattr(user, "last_name", None) or "") if isinstance(p, str) and p).strip()
    parts = [f"@{un}" if un else "", name]
    return " ".join(p for p in parts if p) or "—"


def build_handoff_card(chat_id: int, reason: str, text: Optional[str], state: Optional[str],
                       data: Optional[Dict], user=None, ticket: Optional[str] = None,
                       extra: Optional[str] = None) -> str:
    data = data or {}
    ticket = ticket or data.get("ticket")
    head = f"🙋 Нужен человек: {REASON_RU.get(reason, reason)}"
    if reason == "not_configured":
        head = "⚠️ Не настроено: клиенту обещано «пришлём отдельно»"
    if ncfg.is_test_lead(chat_id):
        head = f"🧪 ТЕСТ — администратор\n{head}"
    lines = [
        head,
        f"Ticket: {ticket or '—'}",
        f"Chat ID: {chat_id}",
        f"Клиент: {_who(user) if user is not None else (data.get('name') or data.get('tg_name') or '—')}",
        f"Шаг: {_STEP_RU.get(state or '', state or '—')}",
    ]
    if data.get("from_country") and data.get("to_country"):
        lines.append(f"Маршрут: {data['from_country']} → {data['to_country']}")
    if text:
        t = text.strip()
        lines.append(f"Сообщение: «{t[:TEXT_MAX]}{'…' if len(t) > TEXT_MAX else ''}»")
    if extra:
        lines.append(extra)
    lines.append(f"Карточка заявки: /order {ticket}" if ticket else "Заявки нет.")
    return "\n".join(lines)


def _claim(chat_id: int, scope: str, reason: str, ticket: Optional[str], window_min: int) -> bool:
    ok = ndb.claim_handoff(chat_id, scope, reason, ticket, window_min)
    if ok is not None:
        return bool(ok)
    key = (int(chat_id), scope)
    now = time.time()
    with _mem_lock:
        last = _mem_last.get(key)
        if last is not None and now - last < window_min * 60:
            return False
        _mem_last[key] = now
    return True


def _target(chat_id: int) -> int:
    if ncfg.is_test_lead(chat_id) and not (ncfg.leads_chat_id() and ncfg.notify_test_leads()):
        return 0                       # admin testing with no group: the admin is the target anyway
    return ncfg.notify_target()


def request_handoff(chat_id: int, reason: str, text: Optional[str] = None, state: Optional[str] = None,
                    data: Optional[Dict] = None, user=None, ticket: Optional[str] = None,
                    forward_message_id: Optional[int] = None, extra: Optional[str] = None,
                    scope: str = "client", window_min: int = HANDOFF_WINDOW_MIN) -> bool:
    """Send the «нужен человек» card (throttled). True if a card was sent. Never raises."""
    try:
        target = _target(chat_id)
        if not target:
            print(f"[Handoff] chat={chat_id} reason={reason}: no LEADS_CHAT_ID/ADMIN_CHAT_ID — skipped")
            return False
        if not _claim(chat_id, scope, reason, ticket or (data or {}).get("ticket"), window_min):
            print(f"[Handoff] chat={chat_id} reason={reason}: throttled ({scope}, {window_min} min)")
            return False
        card = build_handoff_card(chat_id, reason, text, state, data, user, ticket, extra)
        bot.send_message(target, card)
        if forward_message_id:
            try:
                bot.forward_message(target, chat_id, forward_message_id)
            except Exception as e:
                print(f"[Handoff] forward failed: {e}")
        print(f"[Handoff] chat={chat_id} reason={reason} → card sent to {target}")
        return True
    except Exception as e:
        print(f"[Handoff] chat={chat_id} reason={reason} failed: {e}")
        return False


def send_group_note(chat_id: int, text: str, scope: str, window_min: int = CONFIG_WINDOW_MIN) -> bool:
    """A one-line note to the leads target (same target/test rules as the cards), once per scope."""
    try:
        target = _target(chat_id)
        if not target or not _claim(chat_id, scope, "note", None, window_min):
            return False
        if ncfg.is_test_lead(chat_id):
            text = f"🧪 ТЕСТ — администратор\n{text}"
        bot.send_message(target, text)
        print(f"[Handoff] chat={chat_id} note {scope} → {target}")
        return True
    except Exception as e:
        print(f"[Handoff] note {scope} failed: {e}")
        return False


def alert_not_configured(chat_id: int, ticket: str, data: Optional[Dict] = None, user=None) -> bool:
    """Once per order: tell the operator the client was promised «пришлём отдельно»."""
    missing = ns.missing_config()
    if not missing:
        return False
    extra = ("В боте не настроено: " + "; ".join(missing) + ".\n"
             "Напишите клиенту эти данные вручную. Чтобы бот выдавал их сам, "
             "владелец задаёт переменные окружения.")
    return request_handoff(chat_id, "not_configured", None, "completed", data, user, ticket,
                           extra=extra, scope=f"config:{ticket}", window_min=CONFIG_WINDOW_MIN)


# ------------ replying in the chat while keeping the funnel step ------------

def reply_keep_step(chat_id: int, text: str, state: Optional[str], data: Dict) -> None:
    if state in FUNNEL_STATES or state == "route_offer":
        prompt_step(chat_id, state, data, prefix=text)
        return
    save_message(chat_id, None, text)
    bot.send_message(chat_id, text, reply_markup=main_menu())


_BUTTON_TEXTS = None


def _is_button(s: str) -> bool:
    global _BUTTON_TEXTS
    if _BUTTON_TEXTS is None:
        _BUTTON_TEXTS = {MENU_SEND, MENU_RECEIVE, MENU_TRACK, MENU_ALLOWED, MENU_MANAGER, BTN_APPLY,
                         BTN_MENU, BTN_CONFIRM, BTN_CANCEL, BTN_CALC, BTN_BACK, BTN_EDIT, BTN_OTHER_CITY,
                         BTN_EDIT_DONE, BTN_SHARE_CONTACT, DOC_OTHER, *DOC_TYPE_BUTTONS,
                         *VOLUME_PRESETS.keys(), *ROUTE_LABELS, *EDIT_FIELDS.keys()}
    return s in _BUTTON_TEXTS or s.startswith(NAME_BTN_PREFIX)


def handle_special_text(chat_id: int, text: str, state: Optional[str], data: Dict, user=None) -> bool:
    """Handoff triggers and payment/handover FAQ, before the funnel and the AI.
    True if the message was answered here."""
    s = (text or "").strip()
    if not s or s.startswith("/"):
        return False
    if s == MENU_MANAGER:
        # The button keeps its old behaviour (contact shown, step kept) + the operator is told.
        request_handoff(chat_id, "human", s, state, data, user)
        return False
    if _is_button(s) or state == "await_track":
        return False
    reason = detect_handoff(s)
    if reason:
        ticket = data.get("ticket")
        request_handoff(chat_id, reason, s, state, data, user, ticket)
        reply_keep_step(chat_id, client_reply(reason, ticket), state, data)
        return True
    intent = ns.faq_intent(s)
    if intent:
        ticket = data.get("ticket") if state == "completed" else None
        if intent == "payment" and ticket and not data.get("pay_currency"):
            ask_currency(chat_id, ticket)
            return True
        ans = (ns.payment_answer(ticket, data.get("pay_currency")) if intent == "payment"
               else ns.handover_answer(ticket))
        reply_keep_step(chat_id, ans, state, data)
        return True
    return False


# ------------ attachments ------------

@bot.message_handler(content_types=ATTACHMENT_TYPES)
def attachment_received(message):
    chat = getattr(message, "chat", None)
    if getattr(chat, "type", "private") != "private":
        return
    chat_id = chat.id
    kind = getattr(message, "content_type", None) or "file"
    state, data = get_state(chat_id)
    data = dict(data or {}) if state != DB_ERROR_STATE else {}
    print(f"[Handoff] chat={chat_id} attachment {kind} in state={state}")
    caption = getattr(message, "caption", None)
    request_handoff(chat_id, "attachment", caption, state, data, getattr(message, "from_user", None),
                    data.get("ticket"), forward_message_id=getattr(message, "message_id", None),
                    extra=f"Тип вложения: {ATTACHMENT_RU.get(kind, kind)} (переслано ниже)")
    save_message(chat_id, f"[{kind}]", None)
    reply_keep_step(chat_id, client_reply("attachment", data.get("ticket")),
                    state if state != DB_ERROR_STATE else None, data)


# ------------ payment currency ------------

def ask_currency(chat_id: int, ticket: str, with_actions: bool = False) -> None:
    msg = ns.currency_question(ticket)
    save_message(chat_id, None, msg)
    bot.send_message(chat_id, msg, reply_markup=ns.currency_keyboard(ticket, with_actions=with_actions))


@bot.callback_query_handler(func=lambda c: ns.parse_pc_callback(c.data) is not None)
def currency_cb(call):
    """Currency chosen. Never changes the funnel step; stale / foreign buttons are harmless."""
    try:
        bot.answer_callback_query(call.id)
    except Exception as e:
        print(f"[Pay] answer_callback_query failed: {e}")
    chat = getattr(getattr(call, "message", None), "chat", None)
    if chat is None or getattr(chat, "type", "private") != "private":
        return
    chat_id = chat.id
    ticket, cur = ns.parse_pc_callback(call.data)
    if cur not in ns.pcfg.currencies():
        print(f"[Pay] chat={chat_id} stale currency button {cur} for {ticket}")
        msg = "Эта кнопка устарела. Выберите валюту оплаты ещё раз:"
        save_message(chat_id, None, msg)
        bot.send_message(chat_id, msg, reply_markup=ns.currency_keyboard(ticket, with_actions=False))
        return
    stored = ndb.set_pay_currency(ticket, chat_id, cur)
    if stored is False:
        print(f"[Pay] chat={chat_id} currency button for unknown/foreign order {ticket}")
        msg = f"Заявка {ticket} не найдена в этом чате. Проверьте номер: /track."
        save_message(chat_id, None, msg)
        bot.send_message(chat_id, msg, reply_markup=main_menu())
        return
    state, data = get_state(chat_id)
    if state == "completed" and (data or {}).get("ticket") == ticket:
        set_state(chat_id, "completed", dict(data, pay_currency=cur))
    print(f"[Pay] chat={chat_id} {ticket} currency={cur} stored={stored}")
    msg = ns.currency_payment_text(ticket, cur)
    save_message(chat_id, None, msg)
    bot.send_message(chat_id, msg, reply_markup=ns.after_payment_keyboard(ticket))
    send_group_note(chat_id, ns.group_currency_note(ticket, cur, stored), scope=f"cur:{ticket}:{cur}")


# ------------ buttons under «Что дальше» ------------

@bot.callback_query_handler(func=lambda c: ns.parse_ns_callback(c.data) is not None)
def next_steps_cb(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception as e:
        print(f"[NextSteps] answer_callback_query failed: {e}")
    chat = getattr(getattr(call, "message", None), "chat", None)
    if chat is None or getattr(chat, "type", "private") != "private":
        return
    chat_id = chat.id
    action, ticket = ns.parse_ns_callback(call.data)
    if action == "pay":
        ask_currency(chat_id, ticket)
        return
    elif action == "hand":
        msg = ns.handover_answer(ticket)
    elif action == "track":
        from docubridge_flow_steps import handle_track_ticket
        handle_track_ticket(chat_id, ticket)
        return
    else:
        state, data = get_state(chat_id)
        request_handoff(chat_id, "human", "кнопка «Нужен человек»", state, dict(data or {}),
                        getattr(call, "from_user", None), ticket)
        msg = client_reply("human", ticket)
    save_message(chat_id, None, msg)
    bot.send_message(chat_id, msg, reply_markup=main_menu())
