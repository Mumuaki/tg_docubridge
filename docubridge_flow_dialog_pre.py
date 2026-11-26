# -*- coding: utf-8 -*-
"""DocuBridge dialog — route/quote phase — Phase 2."""
from typing import Dict
from telebot.types import ReplyKeyboardRemove

from docubridge_runtime import *  # noqa: F401,F403
from docubridge_keyboards import *  # noqa: F401,F403
from docubridge_flow_steps import *  # noqa: F401,F403
from docubridge_funnel import *  # noqa: F401,F403


def handle_answer_pre(chat_id: int, text: str, state: str, data: Dict) -> bool:
    """Handle menu + quote wizard. True if done."""
    s = (text or "").strip()

    if state in FUNNEL_STATES and handle_funnel(chat_id, s, state, data):
        return True
    if s == BTN_BACK and state in {"route_offer", "choose_route"}:
        go_menu(chat_id)
        return True
    if s == MENU_MANAGER and state == "route_offer":
        prompt_step(chat_id, state, data, prefix=MANAGER_INLINE_TEXT.format(manager=MANAGER_CONTACT))
        return True

    if s == BTN_MENU or s.lower() in {"/menu", "меню"}:
        go_menu(chat_id)
        return True
    if s == MENU_SEND:
        start_send_flow(chat_id, role="sender", ref=data.get("ref"))
        return True
    if s == MENU_RECEIVE:
        start_send_flow(chat_id, role="receiver", ref=data.get("ref"))
        return True
    if s == MENU_TRACK or s.lower() in {"/track", "track"}:
        start_track_flow(chat_id)
        return True
    if s == MENU_ALLOWED:
        if state == "route_offer":
            show_allowed_docs(chat_id, offer=True)
        else:
            show_allowed_docs(chat_id)
        return True
    if s == MENU_MANAGER:
        contact_manager(chat_id)
        return True
    if state == "await_track":
        handle_track_ticket(chat_id, s)
        return True

    if state == "choose_route":
        pair = parse_route_label(s)
        if not pair:
            bot.send_message(
                chat_id,
                "Пожалуйста, выберите один из 4 маршрутов на клавиатуре. "
                "Маршруты RU↔BY и направления в ЕС недоступны.",
                reply_markup=route_keyboard(),
            )
            return True
        fc, tc = pair
        if not is_allowed_route(fc, tc):
            bot.send_message(
                chat_id,
                "Этот маршрут недоступен. Выберите из 4 разрешённых.",
                reply_markup=route_keyboard(),
            )
            return True
        data["from_country"] = fc
        data["to_country"] = tc
        show_route_offer(chat_id, data)
        return True

    if state == "route_offer":
        low = s.lower()
        if s == BTN_CALC or "рассчит" in low or "расчет" in low or "расчёт" in low or low in {"да", "ок", "ok"}:
            prompt_step(chat_id, "ask_doc_type", data)
            return True
        reply = step_question_answer() if is_step_question(s) else ""
        send_offer_message(
            chat_id,
            (reply + "\n\n" if reply else "") + "Нажмите «Рассчитать точно», чтобы получить цену.",
        )
        return True

    return False
