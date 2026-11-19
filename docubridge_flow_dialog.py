# -*- coding: utf-8 -*-
"""DocuBridge dialog handle_answer — Phase 2."""
from docubridge_runtime import *  # noqa: F401,F403
from docubridge_flow_steps import send_db_error
from docubridge_flow_dialog_pre import handle_answer_pre
from docubridge_flow_dialog_post import handle_answer_post


def handle_answer(chat_id: int, text: str):
    state, data = get_state(chat_id)
    print(f"[Handler] handle_answer: chat_id={chat_id}, state={state}, text={mask_for_log(state, text)!r}")
    if state == DB_ERROR_STATE:
        save_message(chat_id, text, None)
        send_db_error(chat_id)
        return
    data = dict(data or {})
    save_message(chat_id, text, None)
    if handle_answer_pre(chat_id, text, state, data):
        return
    handle_answer_post(chat_id, text, state, data)
