# -*- coding: utf-8 -*-
"""DocuBridge dialog handle_answer — Phase 2."""
from typing import Optional
from docubridge_runtime import *  # noqa: F401,F403
from docubridge_flow_steps import send_db_error
from docubridge_flow_dialog_pre import handle_answer_pre
from docubridge_flow_dialog_post import handle_answer_post


def profile_name(user) -> Optional[str]:
    """«Имя Фамилия» from the Telegram profile if it looks like a real name."""
    if user is None:
        return None
    parts = [getattr(user, "first_name", None) or "", getattr(user, "last_name", None) or ""]
    name = " ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())[:60]
    return name if name and valid_name(name) else None


def handle_answer(chat_id: int, text: str, tg_name: Optional[str] = None):
    state, data = get_state(chat_id)
    print(f"[Handler] handle_answer: chat_id={chat_id}, state={state}, text={mask_for_log(state, text)!r}")
    if state == DB_ERROR_STATE:
        save_message(chat_id, text, None)
        send_db_error(chat_id)
        return
    data = dict(data or {})
    if tg_name and data.get("tg_name") != tg_name:
        data["tg_name"] = tg_name
    save_message(chat_id, text, None)
    if handle_answer_pre(chat_id, text, state, data):
        return
    handle_answer_post(chat_id, text, state, data)
