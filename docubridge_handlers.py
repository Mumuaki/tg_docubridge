# -*- coding: utf-8 -*-
"""DocuBridge Telegram handlers / webhook — Phase 2."""
import json
import os
import threading
import time
import traceback
from telebot.types import Update
from flask import request, jsonify

from docubridge_runtime import *  # noqa: F401,F403
from docubridge_keyboards import *  # noqa: F401,F403
from docubridge_flow import *  # noqa: F401,F403
from docubridge_reminders import start_reminder_thread, reminder_health
import docubridge_admin as _admin  # noqa: F401  (registers admin handlers before catch-all)
from docubridge_admin import *  # noqa: F401,F403

# ------------ Handlers ------------

@bot.message_handler(commands=["start"])
def start(message):
    parts = (message.text or "").split(maxsplit=1)
    ref = parts[1].strip() if len(parts) > 1 else None
    action = resolve_start_payload(ref)
    username = getattr(getattr(message, "from_user", None), "username", None)
    print(
        f"[Start] chat_id={message.chat.id} payload={ref or '-'} "
        f"username={('@' + username) if username else '-'} kind={action.get('kind')}"
    )
    record_start_event(message.chat.id, ref, username)

    if action.get("kind") == "route":
        start_send_flow(
            message.chat.id,
            role=action.get("role") or "sender",
            ref=ref,
            from_country=action.get("from_country"),
            to_country=action.get("to_country"),
        )
        return

    data = {}
    if ref:
        data["ref"] = ref
        data["start_payload"] = ref
    set_state(message.chat.id, "greeting", data)
    send_hub(message.chat.id, from_showcase=bool(ref and action.get("kind") == "menu"))


def send_hub(chat_id: int, from_showcase: bool = False):
    head = "Вы пришли с витрины DocuBridge.\n\n" if from_showcase else ""
    msg = (
        head
        + "Добро пожаловать в DocuBridge! Доставляем бумажные документы "
        "между Украиной, Россией и Беларусью.\n\n"
        "Сначала покажем ориентировочную цену и срок — контакты только если решите оформить заявку.\n"
        "Выберите маршрут:"
    )
    save_message(chat_id, "/start", msg)
    bot.send_message(chat_id, msg, reply_markup=hub_inline_keyboard())
    bot.send_message(
        chat_id,
        "Или выберите действие в меню ниже. Справка — /help.",
        reply_markup=main_menu(),
    )


def send_fallback(chat_id: int):
    save_message(chat_id, None, FALLBACK_TEXT)
    bot.send_message(chat_id, FALLBACK_TEXT, reply_markup=main_menu())


@bot.message_handler(commands=["help"])
def help_cmd(message):
    save_message(message.chat.id, "/help", HELP_TEXT)
    bot.send_message(message.chat.id, HELP_TEXT, reply_markup=main_menu())


@bot.callback_query_handler(func=lambda c: parse_hub_callback(c.data) is not None)
def hub_route_cb(call):
    fc, tc = parse_hub_callback(call.data)
    try:
        bot.answer_callback_query(call.id)
    except Exception as e:
        print(f"[hub] answer_callback_query failed: {e}")
    chat_id = call.message.chat.id
    _, data = get_state(chat_id)
    start_send_flow(chat_id, role="sender", ref=(data or {}).get("ref"),
                    from_country=fc, to_country=tc)


@bot.message_handler(commands=["track"])
def track_cmd(message):
    start_track_flow(message.chat.id)


@bot.message_handler(commands=["reset", "menu"])
def reset(message):
    go_menu(message.chat.id, "Сессия сброшена. Выберите действие:")


@bot.message_handler(commands=["ai"])
def ai_ping(message):
    reply = ai_reply("Ответь одним словом: OK")
    save_message(message.chat.id, "/ai", reply)
    bot.send_message(message.chat.id, f"AI: {reply}")


@bot.message_handler(content_types=["contact"])
def contact_shared(message):
    """Telegram request_contact button on the phone step."""
    contact = getattr(message, "contact", None)
    phone = getattr(contact, "phone_number", "") or ""
    state, _ = get_state(message.chat.id)
    print(f"[Funnel] chat={message.chat.id} contact shared in state={state} phone={mask_phone(phone)}")
    if state == DB_ERROR_STATE:
        send_db_error(message.chat.id)
        return
    if state != "ask_phone" or not phone:
        send_fallback(message.chat.id)
        return
    if not phone.startswith("+"):
        phone = "+" + phone.lstrip("0")
    handle_answer(message.chat.id, phone, tg_name=profile_name(getattr(message, "from_user", None)))


@bot.message_handler(func=lambda m: True)
def any_text(message):
    if is_unknown_command(message.text):
        send_fallback(message.chat.id)
        return
    handle_answer(message.chat.id, message.text or "",
                  tg_name=profile_name(getattr(message, "from_user", None)))


# ------------ Webhook ------------

@app.route("/", methods=["GET"])
def index():
    return "OK", 200


@app.route(f"/webhook/{WEBHOOK_SECRET}", methods=["POST"])
def telegram_webhook():
    try:
        if request.headers.get("content-type") == "application/json":
            json_data = json.loads(request.get_data().decode("utf-8"))
            update = Update.de_json(json_data)
            update_id = update.update_id
            print(f"[Webhook] Received update_id: {update_id}")

            if is_update_processed(update_id):
                print(f"[Webhook] Update {update_id} уже обработан, пропускаем")
                return "OK", 200

            print(f"[Webhook] Processing update_id: {update_id}")
            try:
                bot.process_new_updates([update])
            except Exception as e:
                # Not marked as processed: a redelivery of this update will be handled again.
                print(f"[Webhook] Update {update_id} FAILED (not marked processed): {e}")
                traceback.print_exc()
                return "OK", 200
            mark_update_processed(update_id)
            print(f"[Webhook] Update {update_id} processed successfully")
        else:
            print("[Webhook] Unsupported content-type")
    except Exception as e:
        print("[Webhook] error:", e)
        traceback.print_exc()
    return "OK", 200


def ensure_webhook() -> bool:
    """Idempotent setWebhook (same URL is simply re-set). Never raises: False on failure."""
    try:
        if not WEBHOOK_BASE:
            print("❌ ERROR: WEBHOOK_BASE не задан — бот не будет получать сообщения!")
            return False
        url = f"{WEBHOOK_BASE}/webhook/{WEBHOOK_SECRET}"
        # Keep updates that queued up while the service was deploying/restarting
        # (e.g. /start from an ad): they are de-duplicated via processed_updates.
        ok = bot.set_webhook(url=url, drop_pending_updates=False)
        if ok:
            print(f"✅ Webhook set to: {WEBHOOK_BASE}/webhook/<secret>")
            return True
        print("❌ ERROR: set_webhook returned False")
    except Exception as e:
        print(f"❌ [Webhook] set error: {e}")
    return False


# ------------ Startup (lazy, per process, fork-safe) ------------
# Nothing is opened at import time: with gunicorn --preload the module is imported
# in the master before fork, so DB sockets / threads created there would be shared
# with (or missing in) the worker. Startup runs in the serving process instead:
# from gunicorn's post_worker_init hook and, as a fallback, on the first request.

STARTUP_RETRY_S = 30
_startup_lock = threading.Lock()
_startup = {"pid": None, "tables": False, "webhook": False, "last_try": 0.0}


def startup_status() -> dict:
    same = _startup["pid"] == os.getpid()
    return {"tables": same and _startup["tables"], "webhook": same and _startup["webhook"]}


def ensure_started(force: bool = False) -> dict:
    """Idempotent: tables + webhook + reminder thread, once per process.
    Failed parts are retried on a later call (at most every STARTUP_RETRY_S)."""
    pid = os.getpid()
    if (_startup["pid"] == pid and _startup["tables"] and _startup["webhook"]
            and reminder_health() != "dead"):
        return startup_status()
    if not _startup_lock.acquire(timeout=30 if force else 0):
        return startup_status()  # another thread is starting right now
    try:
        if _startup["pid"] != pid:
            _startup.update(pid=pid, tables=False, webhook=False, last_try=0.0)
        now = time.time()
        if not force and now - _startup["last_try"] < STARTUP_RETRY_S:
            return startup_status()
        _startup["last_try"] = now
        print(f"[Startup] pid {pid}: tables={_startup['tables']} webhook={_startup['webhook']}")
        if not _startup["tables"]:
            try:
                _startup["tables"] = bool(ensure_tables())
            except Exception as e:
                print(f"[Startup] ensure_tables error: {e}")
        if not _startup["webhook"]:
            _startup["webhook"] = ensure_webhook()
        try:
            start_reminder_thread()
        except Exception as e:
            print(f"[Startup] reminder thread error: {e}")
        return startup_status()
    finally:
        _startup_lock.release()


@app.before_request
def _lazy_startup():
    if SKIP_STARTUP:
        return None
    try:
        ensure_started()
    except Exception as e:  # never block a request because of startup
        print(f"[Startup] error: {e}")
    return None


def db_ping() -> bool:
    if not DB_URL:
        return False

    def _do(conn):
        cur = conn.cursor()
        cur.execute("SELECT 1")
        ok = cur.fetchone() is not None
        cur.close()
        return ok

    return bool(db_run("health", _do, default=False))


@app.route("/health", methods=["GET"])
def health():
    db_ok = db_ping()
    rem = reminder_health()
    st = startup_status()
    ok = db_ok and rem != "dead" and st["webhook"]
    body = {"ok": ok, "db": db_ok, "reminders": rem, "webhook": st["webhook"],
            "tables": st["tables"], "pid": os.getpid()}
    return jsonify(body), (200 if ok else 503)


# ------------ Entrypoint ------------
if __name__ == "__main__":
    if not SKIP_STARTUP:
        ensure_started(force=True)
    app.run(host="0.0.0.0", port=PORT, debug=False)
