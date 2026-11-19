# -*- coding: utf-8 -*-
"""DocuBridge dialog — apply/confirm phase — Phase 2."""
from typing import Dict
from telebot.types import ReplyKeyboardRemove

from docubridge_runtime import *  # noqa: F401,F403
from docubridge_keyboards import *  # noqa: F401,F403
from docubridge_flow_steps import *  # noqa: F401,F403
from docubridge_funnel import *  # noqa: F401,F403
from docubridge_funnel import _off_step_answer
from docubridge_sheets import export_shipment_async


def handle_answer_post(chat_id: int, text: str, state: str, data: Dict) -> None:
    s = (text or "").strip()

    if state == "confirm":
        if s == BTN_CANCEL or s.lower() in {"отмена", "нет"}:
            go_menu(chat_id, "Заявка отменена. Вы в главном меню.")
            return
        if s != BTN_CONFIRM and s.lower() not in {"подтвердить", "да", "ok", "ок"}:
            if is_step_question(s):
                prompt_step(chat_id, "confirm", data, prefix=_off_step_answer(s))
                return
            bot.send_message(
                chat_id,
                "Нажмите «Подтвердить», «✏️ Исправить» или «Отмена».",
                reply_markup=confirm_keyboard(),
            )
            return

        ticket = create_shipment(chat_id, data)
        print(f"[Ticket] chat={chat_id} create_shipment -> {ticket or 'FAILED'} ref={data.get('ref') or '-'}")
        if not ticket:
            print(f"[Ticket] create_shipment failed for chat {chat_id} — no ticket issued")
            notify_admin_order_failed(chat_id, data)
            err = (
                "⚠️ Не удалось сохранить заявку из-за технической ошибки.\n"
                "Номер отправления не присвоен. Попробуйте ещё раз через несколько минут "
                f"или напишите менеджеру: {MANAGER_CONTACT}."
            )
            save_message(chat_id, s, err)
            bot.send_message(chat_id, err, reply_markup=confirm_keyboard())
            return
        data["ticket"] = ticket

        notify_admin_lead(chat_id, data)
        try:
            export_shipment_async(chat_id, data, compute_quote(data).get("price_eur"))
        except Exception as e:  # never break the order
            print(f"[Sheets] export error: {e}")

        thanks = (
            f"✅ Заявка оформлена.\n"
            f"Номер отправления: {ticket}\n\n"
            f"Маршрут: {route_line(data)}\n\n"
            "Что дальше:\n"
            f"1. Менеджер свяжется с вами по телефону {data.get('phone')}, "
            "чтобы согласовать детали и передачу документов.\n"
            f"2. Статус — кнопка «Отследить отправление» или /track и номер {ticket}.\n"
            f"Вопросы — {MANAGER_CONTACT} (укажите номер {ticket})."
        )
        save_message(chat_id, s, thanks)
        bot.send_message(chat_id, thanks, reply_markup=main_menu())
        set_state(chat_id, "completed", {"ticket": ticket})
        return

    if state in {"greeting", "completed"} or not state:
        low = s.lower()
        if is_calc_intent(s):
            # «Рассчитать точно», «сколько стоит», «цена» … → straight into the quote.
            route = resolve_start_payload(data.get("ref"))
            if route.get("kind") == "route":
                data["role"] = data.get("role") or "sender"
                data["from_country"] = route["from_country"]
                data["to_country"] = route["to_country"]
                ask_doc_type(chat_id, data)
            else:
                start_send_flow(chat_id, role="sender", ref=data.get("ref"))
            return
        if "отправ" in low or "заявк" in low:
            start_send_flow(chat_id, role="sender", ref=data.get("ref"))
            return
        if "получ" in low:
            start_send_flow(chat_id, role="receiver", ref=data.get("ref"))
            return
        if "отслед" in low or "трек" in low or "status" in low:
            start_track_flow(chat_id)
            return
        reply = (ai_reply(s) or "").strip() if looks_like_question(s) else ""
        if not reply or reply.lower().startswith(("ошибка", "error")):
            reply = FALLBACK_TEXT
        else:
            reply += "\n\nМеню: /start, справка: /help."
        save_message(chat_id, s, reply)
        bot.send_message(chat_id, reply, reply_markup=main_menu())
        return

    go_menu(chat_id, "Сессия сброшена. Выберите действие в меню.")
