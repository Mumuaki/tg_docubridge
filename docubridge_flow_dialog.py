# -*- coding: utf-8 -*-
"""DocuBridge dialog handle_answer — Phase 2."""
from typing import Optional, Dict
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

from docubridge_runtime import *  # noqa: F401,F403
from docubridge_keyboards import *  # noqa: F401,F403
from docubridge_flow_steps import *  # noqa: F401,F403

def handle_answer(chat_id: int, text: str):
    print(f"[Handler] handle_answer: chat_id={chat_id}, text={text!r}")
    state, data = get_state(chat_id)
    data = dict(data or {})
    save_message(chat_id, text, None)
    s = (text or "").strip()

    # Global menu shortcuts
    if s == BTN_MENU or s.lower() in {"/menu", "меню"}:
        go_menu(chat_id)
        return
    if s == MENU_SEND:
        start_send_flow(chat_id, role="sender", ref=data.get("ref"))
        return
    if s == MENU_RECEIVE:
        start_send_flow(chat_id, role="receiver", ref=data.get("ref"))
        return
    if s == MENU_TRACK or s.lower() in {"/track", "track"}:
        start_track_flow(chat_id)
        return
    if s == MENU_ALLOWED:
        show_allowed_docs(chat_id)
        return
    if s == MENU_MANAGER:
        contact_manager(chat_id)
        return

    if state == "await_track":
        handle_track_ticket(chat_id, s)
        return

    if state == "choose_route":
        pair = parse_route_label(s)
        if not pair:
            bot.send_message(
                chat_id,
                "Пожалуйста, выберите один из 4 маршрутов на клавиатуре. "
                "Маршруты RU↔BY и направления в ЕС недоступны.",
                reply_markup=route_keyboard(),
            )
            return
        fc, tc = pair
        if not is_allowed_route(fc, tc):
            bot.send_message(
                chat_id,
                "Этот маршрут недоступен. Выберите из 4 разрешённых.",
                reply_markup=route_keyboard(),
            )
            return
        data["from_country"] = fc
        data["to_country"] = tc
        set_state(chat_id, "ask_from_city", data)
        bot.send_message(
            chat_id,
            f"Маршрут: {fc} → {tc}.\nИз какого города отправляем?",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if state == "ask_from_city":
        if len(s) < 2:
            bot.send_message(chat_id, "Укажите город отправки.")
            return
        data["from_city"] = s.title()
        set_state(chat_id, "ask_to_city", data)
        bot.send_message(chat_id, "В какой город доставляем?")
        return

    if state == "ask_to_city":
        if len(s) < 2:
            bot.send_message(chat_id, "Укажите город доставки.")
            return
        data["to_city"] = s.title()
        set_state(chat_id, "ask_doc_type", data)
        bot.send_message(
            chat_id,
            f"Какой тип документа?\n(например: {DOC_TYPES_HINT})\n"
            "Важно: паспорта и ID-карты не принимаем.",
        )
        return

    if state == "ask_doc_type":
        low = s.lower()
        banned = ["паспорт", "passport", "id-карт", "id карт", "удостоверен", "national id"]
        if any(b in low for b in banned):
            bot.send_message(
                chat_id,
                "Паспорта, ID-карты и подобные оригиналы не принимаем. "
                "Укажите другой тип документа (доверенность, диплом, свидетельство и т.п.).",
            )
            return
        if len(s) < 2:
            bot.send_message(chat_id, "Укажите тип документа.")
            return
        data["doc_type"] = s
        set_state(chat_id, "ask_volume", data)
        bot.send_message(
            chat_id,
            "Укажите объём: вес в граммах (конверт до 500 г) и/или число листов A4.\n"
            "Примеры: «300 г», «40 листов», «0.4 кг».",
        )
        return

    if state == "ask_volume":
        pages, weight, err = parse_volume(s)
        if err:
            bot.send_message(chat_id, err)
            return
        if pages is not None:
            data["pages_a4"] = pages
        data["weight_grams"] = weight
        set_state(chat_id, "ask_urgency", data)
        bot.send_message(
            chat_id,
            "Срочность доставки?",
            reply_markup=urgency_keyboard(),
        )
        return

    if state == "ask_urgency":
        u = infer_urgency(s) or (s.lower() if s.lower() in {"обычная", "срочная"} else None)
        if not u:
            bot.send_message(
                chat_id,
                "Выберите: обычная или срочная.",
                reply_markup=urgency_keyboard(),
            )
            return
        data["urgency"] = u
        # QUOTE FIRST — no contacts yet
        set_state(chat_id, "quote_shown", data)
        msg = format_quote_message(data)
        save_message(chat_id, None, msg)
        bot.send_message(chat_id, msg, reply_markup=quote_keyboard())
        return

    if state == "quote_shown":
        if s == BTN_APPLY or s.lower() in {"оформить", "заявка", "да"}:
            set_state(chat_id, "ask_name", data)
            bot.send_message(
                chat_id,
                "Как к вам обращаться (имя/фамилия)?",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        if s == BTN_MENU:
            go_menu(chat_id)
            return
        bot.send_message(
            chat_id,
            "Нажмите «Оформить заявку» или «В меню».",
            reply_markup=quote_keyboard(),
        )
        return

    if state == "ask_name":
        if not valid_name(s):
            bot.send_message(
                chat_id,
                "Введите имя/фамилию (буквы, пробелы и дефисы; не короче 2 символов).",
            )
            return
        data["name"] = s
        set_state(chat_id, "ask_phone", data)
        bot.send_message(chat_id, "Контактный телефон (+380 / +7 / +375):")
        return

    if state == "ask_phone":
        phone = s.replace(" ", "").replace("-", "")
        if not valid_phone(phone):
            bot.send_message(
                chat_id,
                "Телефон должен начинаться с +380 / +7 / +375.",
            )
            return
        data["phone"] = phone
        set_state(chat_id, "confirm", data)
        msg = format_confirm_summary(data)
        save_message(chat_id, None, msg)
        bot.send_message(chat_id, msg, reply_markup=confirm_keyboard())
        return

    if state == "confirm":
        if s == BTN_CANCEL or s.lower() in {"отмена", "нет"}:
            go_menu(chat_id, "Заявка отменена. Вы в главном меню.")
            return
        if s != BTN_CONFIRM and s.lower() not in {"подтвердить", "да", "ok", "ок"}:
            bot.send_message(
                chat_id,
                "Нажмите «Подтвердить» или «Отмена».",
                reply_markup=confirm_keyboard(),
            )
            return

        ticket = create_shipment(chat_id, data)
        if not ticket:
            ticket = generate_ticket_candidate()
            data["ticket"] = ticket
            print(f"[Ticket] Fallback ticket {ticket} (DB insert failed)")
        else:
            data["ticket"] = ticket

        notify_admin_lead(chat_id, data)

        thanks = (
            f"✅ Заявка оформлена.\n"
            f"Номер отправления: {ticket}\n\n"
            f"Маршрут: {route_line(data)}\n"
            f"Мы свяжемся с вами по телефону {data.get('phone')}.\n"
            f"Статус можно проверить: «Отследить отправление» или /track."
        )
        save_message(chat_id, s, thanks)
        bot.send_message(chat_id, thanks, reply_markup=main_menu())
        set_state(chat_id, "completed", {"ticket": ticket})
        return

    # Free text outside wizard → short AI or menu hint
    if state in {"greeting", "completed"} or not state:
        low = s.lower()
        if "отправ" in low or "заявк" in low or "расчёт" in low or "расчет" in low:
            start_send_flow(chat_id, role="sender", ref=data.get("ref"))
            return
        if "получ" in low:
            start_send_flow(chat_id, role="receiver", ref=data.get("ref"))
            return
        if "отслед" in low or "трек" in low or "status" in low:
            start_track_flow(chat_id)
            return
        reply = ai_reply(s)
        save_message(chat_id, s, reply)
        bot.send_message(chat_id, reply, reply_markup=main_menu())
        return

    # Unknown state recovery
    go_menu(chat_id, "Сессия сброшена. Выберите действие в меню.")


