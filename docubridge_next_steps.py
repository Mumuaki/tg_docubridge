# -*- coding: utf-8 -*-
"""«Что дальше» after an order DBxxxx, status explanations, payment / handover answers.

Facts come only from the code (helpers: price, envelope limit, ETA) and from env set
by the owner. Nothing is invented: when the owner has not configured something,
the client gets a safe fallback and the operator gets an alert (see docubridge_handoff).

Env (values are set by the owner; empty/unset → fallback text):
  DOCUBRIDGE_PAYMENT_TEXT   — how to pay / payment details, shown verbatim
  DOCUBRIDGE_HANDOVER_TEXT  — how to hand over the paper documents, shown verbatim
"""
import os
import re
from typing import Dict, Optional

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

from docubridge_config import MANAGER_CONTACT
from docubridge_helpers import (
    compute_quote, ENVELOPE_MAX_G, TRACK_STATUS_RU, normalize_status,
)

ENV_PAYMENT = "DOCUBRIDGE_PAYMENT_TEXT"
ENV_HANDOVER = "DOCUBRIDGE_HANDOVER_TEXT"

PAYMENT_FALLBACK = "реквизиты для оплаты пришлём отдельным сообщением в этом чате."
HANDOVER_FALLBACK = "как передать документы, пришлём отдельным сообщением в этом чате."

BTN_NS_PAY = "💳 Как оплатить"
BTN_NS_HANDOVER = "📨 Как передать документы"
BTN_NS_TRACK = "📍 Отследить"
BTN_NS_HUMAN = "🙋 Нужен человек"
NS_CB_PREFIX = "ns"                 # ns:<action>:<ticket>
NS_ACTIONS = ("pay", "hand", "track", "human")


def _env_text(name: str) -> Optional[str]:
    v = (os.getenv(name) or "").strip()
    return v.replace("\\n", "\n") if v else None


def payment_text() -> Optional[str]:
    return _env_text(ENV_PAYMENT)


def handover_text() -> Optional[str]:
    return _env_text(ENV_HANDOVER)


def missing_config() -> list:
    """Human labels of what the owner has not configured yet."""
    out = []
    if not payment_text():
        out.append("реквизиты / способ оплаты (DOCUBRIDGE_PAYMENT_TEXT)")
    if not handover_text():
        out.append("способ передачи документов (DOCUBRIDGE_HANDOVER_TEXT)")
    return out


def price_line(data: Dict) -> str:
    q = compute_quote(data or {})
    if q.get("price_eur") is not None:
        return f"Стоимость: €{q['price_eur']} (конверт до {ENVELOPE_MAX_G} г)."
    return "Стоимость: по согласованию (вес больше 0,5 кг) — менеджер пришлёт расчёт в этом чате."


def eta_line(data: Dict) -> str:
    q = compute_quote(data or {})
    if q.get("eta_working"):
        return f"Срок: ориентировочно {q['eta_working']} рабочих дней."
    return "Срок: уточним после подтверждения маршрута."


def thanks_text(ticket: str, route: str, data: Dict) -> str:
    return (
        "✅ Заявка оформлена.\n"
        f"Номер отправления: {ticket}\n"
        f"Маршрут: {route}\n"
        f"{price_line(data)}\n"
        f"{eta_line(data)}\n\n"
        "Что дальше:\n"
        f"1. Оплата: {payment_text() or PAYMENT_FALLBACK}\n"
        f"2. Передача документов: {handover_text() or HANDOVER_FALLBACK}\n"
        "3. Статусы будут приходить сюда автоматически. Проверить в любой момент: "
        f"/track и номер {ticket} или кнопка «Отследить».\n\n"
        f"Нужна помощь человека — кнопка «Нужен человек» или {MANAGER_CONTACT}."
    )


def actions_text(ticket: str) -> str:
    return f"Действия по заявке {ticket}:"


def ns_callback(action: str, ticket: str) -> str:
    return f"{NS_CB_PREFIX}:{action}:{ticket}"


def parse_ns_callback(data: Optional[str]):
    """'ns:pay:DB1234' → ('pay', 'DB1234'); None if not ours."""
    parts = (data or "").split(":", 2)
    if len(parts) != 3 or parts[0] != NS_CB_PREFIX or parts[1] not in NS_ACTIONS:
        return None
    return parts[1], parts[2]


def next_steps_keyboard(ticket: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton(BTN_NS_PAY, callback_data=ns_callback("pay", ticket)))
    kb.row(InlineKeyboardButton(BTN_NS_HANDOVER, callback_data=ns_callback("hand", ticket)))
    kb.row(InlineKeyboardButton(BTN_NS_TRACK, callback_data=ns_callback("track", ticket)))
    kb.row(InlineKeyboardButton(BTN_NS_HUMAN, callback_data=ns_callback("human", ticket)))
    return kb


def payment_answer(ticket: Optional[str] = None) -> str:
    t = payment_text()
    if t:
        return f"💳 Оплата{(' заявки ' + ticket) if ticket else ''}:\n{t}"
    if ticket:
        return f"💳 Оплата заявки {ticket}: {PAYMENT_FALLBACK}"
    return "💳 Оплата: реквизиты для оплаты пришлём в этом чате после оформления заявки."


def handover_answer(ticket: Optional[str] = None) -> str:
    t = handover_text()
    if t:
        return f"📨 Передача документов{(' по заявке ' + ticket) if ticket else ''}:\n{t}"
    if ticket:
        return f"📨 Передача документов по заявке {ticket}: {HANDOVER_FALLBACK}"
    return "📨 Передача документов: как передать документы, пришлём в этом чате после оформления заявки."


# ------------ status explanations (no new promises: only what the stage means) ------------

STATUS_NEXT_RU = {
    "accepted": "Заявка принята. Дальше — оплата и передача документов "
                "(см. «Что дальше» в сообщении о заявке).",
    "departed_hub": "Документы в пути. Следующий статус придёт сюда автоматически.",
    "at_hub": "Документы на хабе. Следующий статус придёт сюда автоматически.",
    "destination_country": "Отправление в стране назначения. Следующий статус придёт сюда автоматически.",
    "delivered": "Отправление доставлено. Спасибо, что выбрали DocuBridge!",
}


def status_next(status: Optional[str]) -> Optional[str]:
    return STATUS_NEXT_RU.get(normalize_status(status) or "")


def track_hint(status: Optional[str]) -> Optional[str]:
    """Extra line under /track for the order's own chat."""
    st = normalize_status(status) or "accepted"
    if st == "accepted":
        return ("Что дальше:\n"
                f"• Оплата: {payment_text() or PAYMENT_FALLBACK}\n"
                f"• Передача документов: {handover_text() or HANDOVER_FALLBACK}")
    nxt = status_next(st)
    return f"Что дальше: {nxt}" if nxt else None


# ------------ FAQ intents answered without AI ------------

PAYMENT_RE = re.compile(r"оплат|оплач|реквизит|заплат|платить|плачу|предоплат|счёт на|счет на|iban|\bкарт(?:ой|у)\b")
HANDOVER_RE = re.compile(
    r"передать документ|передач[аиу] документ|как передать|куда (?:принести|привезти|отправить|отнести)"
    r"|адрес для|ваш адрес|забер[её]те|курьер|нов[аой]+ пошт|почтой"
)


def faq_intent(text: Optional[str]) -> Optional[str]:
    """'payment' | 'handover' | None."""
    low = (text or "").lower()
    if PAYMENT_RE.search(low):
        return "payment"
    if HANDOVER_RE.search(low):
        return "handover"
    return None
