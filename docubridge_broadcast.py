# -*- coding: utf-8 -*-
"""/broadcast_stuck — one-off message to chats stuck in the funnel (admin only).

Flow:
  1. /broadcast_stuck          → dry run: counts per segment and step, the exact text,
                                 a preview sent to the admin only, confirm buttons.
  2. «Отправить N …» button    → single-use token (10 min); sending runs in a
                                 background thread, never inside the webhook handler.
Safety:
  * candidates = CANDIDATES_SQL (docubridge_db_outbox): idle ≥ 48 h, active within
    60 days, no shipment / lead, not the admin, not yet in broadcast_log for the campaign;
  * every chat is claimed in broadcast_log (INSERT … ON CONFLICT DO NOTHING RETURNING)
    BEFORE sending → never twice, also across restarts / two processes;
  * ~20 msg/s; 429 → wait retry_after and retry; 403 → status «blocked», skipped;
  * one run at a time per process; progress + final report to the admin.
"""
import os
import secrets
import threading
import time
from typing import Dict, Optional

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

from docubridge_runtime import *  # noqa: F401,F403
from docubridge_flow_steps import start_send_flow, contact_manager
from docubridge_admin import is_admin, STATE_LABELS_RU

# Approved text (verbatim). «/start» is a tappable command in Telegram.
BROADCAST_TEXT = (
    "Вы недавно рассматривали возможность отправки документов с DocuBridge, "
    "но так и не сделали заказ. Мы упростили оформление: теперь это 4 коротких шага "
    "с кнопками. Продолжить можно здесь 👉 /start\n"
    "Если у Вас остались вопросы, пишите менеджеру @is_logix"
)
BTN_BC_CONTINUE = "Продолжить расчёт"
BTN_BC_MANAGER = "Связаться с менеджером"

BROADCAST_CAMPAIGN = os.getenv("DOCUBRIDGE_BROADCAST_CAMPAIGN", "stuck_2026_11").strip() or "stuck_2026_11"
RATE_PER_S = 20
SEND_INTERVAL_S = 1.0 / RATE_PER_S
MAX_429_RETRIES = 3
TOKEN_TTL_S = 600
PROGRESS_EVERY = 100

CB_USER = "bcu"      # bcu:go / bcu:mgr — buttons under the broadcast message (any user)
CB_ADMIN = "bca"     # bca:<token>:<segment|cancel> — admin confirm buttons
SEGMENT_RU = {"started": "начали оформление", "greeting": "только /start", "all": "все"}

_sleep = time.sleep            # patched in tests
_pending: Dict[str, Dict] = {}  # token -> {"created": ts, "used": bool}
_pending_lock = threading.Lock()
_run_lock = threading.Lock()   # one broadcast at a time in this process
_run_thread = None


# ------------ keyboards ------------

def broadcast_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton(BTN_BC_CONTINUE, callback_data=f"{CB_USER}:go"))
    handle = (MANAGER_CONTACT or "").strip()
    if handle.startswith("@") and len(handle) > 1:
        kb.row(InlineKeyboardButton(BTN_BC_MANAGER, url=f"https://t.me/{handle[1:]}"))
    else:
        kb.row(InlineKeyboardButton(BTN_BC_MANAGER, callback_data=f"{CB_USER}:mgr"))
    return kb


def confirm_keyboard_bc(token: str, counts: Dict) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    for seg, n in (("started", counts["started"]), ("greeting", counts["greeting"]),
                   ("all", counts["started"] + counts["greeting"])):
        if n:
            kb.row(InlineKeyboardButton(f"Отправить {n} — {SEGMENT_RU[seg]}",
                                        callback_data=f"{CB_ADMIN}:{token}:{seg}"))
    kb.row(InlineKeyboardButton("Отмена", callback_data=f"{CB_ADMIN}:{token}:cancel"))
    return kb


# ------------ dry run ------------

def _new_token() -> str:
    tok = secrets.token_hex(4)
    with _pending_lock:
        now = time.time()
        for k in [k for k, v in _pending.items() if now - v["created"] > TOKEN_TTL_S]:
            _pending.pop(k, None)
        _pending[tok] = {"created": now, "used": False}
    return tok


def take_token(token: str) -> Optional[str]:
    """Single use + TTL. Returns None if OK, else a reason."""
    with _pending_lock:
        rec = _pending.get(token)
        if not rec:
            return "Подтверждение устарело — запустите /broadcast_stuck заново."
        if rec["used"]:
            return "Эта кнопка уже использована."
        if time.time() - rec["created"] > TOKEN_TTL_S:
            _pending.pop(token, None)
            return "Прошло больше 10 минут — запустите /broadcast_stuck заново."
        rec["used"] = True
    return None


def format_dry_run(counts: Dict) -> str:
    lines = [
        f"📣 Рассылка застрявшим — пробный прогон (ничего не отправлено)",
        f"Кампания: {BROADCAST_CAMPAIGN}",
        "",
        f"Начали оформление: {counts['started']}",
        f"Только /start: {counts['greeting']}",
        "",
        "По последнему шагу:",
    ]
    lines += [f"• {STATE_LABELS_RU.get(s, s)}: {n}" for s, n in counts["by_state"]] or ["• —"]
    if counts.get("already"):
        lines += ["", "Уже в журнале этой кампании: "
                  + ", ".join(f"{k} {v}" for k, v in sorted(counts["already"].items()))]
    lines += [
        "",
        "Условия: без заявки, не админ, неактивны ≥ 48 ч, активность за 60 дн., "
        "каждому не более 1 раза за кампанию. Скорость ~20 сообщ./с.",
        "Превью сообщения — ниже. Отправка — только кнопкой (действует 10 мин).",
    ]
    return "\n".join(lines)


@bot.message_handler(commands=["broadcast_stuck"], func=lambda m: is_admin(m.chat.id))
def admin_broadcast_stuck(message):
    chat_id = message.chat.id
    if _run_lock.locked():
        bot.send_message(chat_id, "Рассылка уже идёт — дождитесь отчёта.")
        return
    counts = count_broadcast_candidates(BROADCAST_CAMPAIGN, ADMIN_CHAT_ID)
    if counts is None:
        bot.send_message(chat_id, "Не удалось посчитать: нет подключения к БД.")
        return
    bot.send_message(chat_id, format_dry_run(counts))
    bot.send_message(chat_id, BROADCAST_TEXT, reply_markup=broadcast_keyboard())  # preview, admin only
    total = counts["started"] + counts["greeting"]
    if not total:
        bot.send_message(chat_id, "Отправлять некому.")
        return
    bot.send_message(chat_id, "Кому отправить?",
                     reply_markup=confirm_keyboard_bc(_new_token(), counts))


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith(CB_ADMIN + ":"))
def admin_broadcast_confirm(call):
    if not is_admin(getattr(call.from_user, "id", None)):
        bot.answer_callback_query(call.id, "Нет доступа")
        return
    try:
        _, token, segment = call.data.split(":", 2)
    except ValueError:
        bot.answer_callback_query(call.id, "Некорректные данные")
        return
    reason = take_token(token)
    if reason:
        bot.answer_callback_query(call.id, reason[:190])
        return
    if segment == "cancel":
        bot.answer_callback_query(call.id, "Отменено")
        bot.send_message(call.message.chat.id, "Рассылка отменена, ничего не отправлено.")
        return
    if segment not in SEGMENT_RU:
        bot.answer_callback_query(call.id, "Некорректный сегмент")
        return
    t = start_broadcast(segment, call.message.chat.id)
    if t is None:
        bot.answer_callback_query(call.id, "Рассылка уже идёт")
        return
    bot.answer_callback_query(call.id, "Запущено")
    bot.send_message(call.message.chat.id,
                     f"🚀 Рассылка запущена: {SEGMENT_RU[segment]}. Отчёт придёт по завершении.")


# ------------ sending ------------

def _error_code(e) -> Optional[int]:
    code = getattr(e, "error_code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def _retry_after(e) -> float:
    rj = getattr(e, "result_json", None) or {}
    try:
        return float((rj.get("parameters") or {}).get("retry_after") or 5)
    except (TypeError, ValueError, AttributeError):
        return 5.0


def send_one(chat_id: int) -> str:
    """'sent' | 'blocked' | 'failed:<reason>'."""
    for attempt in range(MAX_429_RETRIES + 1):
        try:
            bot.send_message(chat_id, BROADCAST_TEXT, reply_markup=broadcast_keyboard())
            return "sent"
        except Exception as e:
            code = _error_code(e)
            if code == 429 and attempt < MAX_429_RETRIES:
                wait = _retry_after(e)
                print(f"[Broadcast] 429 — waiting {wait:.0f}s")
                _sleep(wait + 0.5)
                continue
            if code == 403:
                return "blocked"
            return f"failed:{code or type(e).__name__}: {str(e)[:150]}"
    return "failed:429 retries exhausted"


def _admin_note(admin_chat: int, text: str):
    try:
        bot.send_message(admin_chat, text)
    except Exception as e:
        print(f"[Broadcast] admin note failed: {e}")


def run_broadcast(segment: str, admin_chat: int) -> Dict:
    """Synchronous body (the thread target). Returns the summary dict."""
    stats = {"sent": 0, "blocked": 0, "failed": 0, "skipped": 0}
    rows = find_broadcast_candidates(BROADCAST_CAMPAIGN, segment, ADMIN_CHAT_ID)
    print(f"[Broadcast] {BROADCAST_CAMPAIGN}/{segment}: {len(rows)} candidates")
    for i, row in enumerate(rows, start=1):
        chat_id = int(row["chat_id"])
        if ADMIN_CHAT_ID and chat_id == int(ADMIN_CHAT_ID):
            stats["skipped"] += 1
            continue
        if not claim_broadcast(BROADCAST_CAMPAIGN, chat_id, segment):
            stats["skipped"] += 1          # already sent/claimed (other run or process)
            continue
        res = send_one(chat_id)
        if res == "sent":
            stats["sent"] += 1
            set_broadcast_result(BROADCAST_CAMPAIGN, chat_id, "sent")
            try:
                save_message(chat_id, None, BROADCAST_TEXT)
            except Exception:
                pass
        elif res == "blocked":
            stats["blocked"] += 1
            set_broadcast_result(BROADCAST_CAMPAIGN, chat_id, "blocked", "403")
        else:
            stats["failed"] += 1
            set_broadcast_result(BROADCAST_CAMPAIGN, chat_id, "failed", res[len("failed:"):])
        if i % PROGRESS_EVERY == 0:
            _admin_note(admin_chat, f"… {i}/{len(rows)}: отправлено {stats['sent']}, "
                                    f"заблокировали {stats['blocked']}, ошибок {stats['failed']}")
        _sleep(SEND_INTERVAL_S)
    report = (f"✅ Рассылка {BROADCAST_CAMPAIGN} ({SEGMENT_RU.get(segment, segment)}) завершена.\n"
              f"Кандидатов: {len(rows)}\nОтправлено: {stats['sent']}\n"
              f"Заблокировали бота: {stats['blocked']}\nОшибок: {stats['failed']}\n"
              f"Пропущено (уже было): {stats['skipped']}")
    print("[Broadcast] " + report.replace("\n", " | "))
    _admin_note(admin_chat, report)
    return stats


def _run_guarded(segment: str, admin_chat: int):
    try:
        run_broadcast(segment, admin_chat)
    except Exception as e:
        print(f"[Broadcast] crashed: {e}")
        _admin_note(admin_chat, f"⚠️ Рассылка прервана ошибкой: {e}. "
                                "Повторный запуск не задублирует отправленным.")
    finally:
        _run_lock.release()


def start_broadcast(segment: str, admin_chat: int):
    """Start the background run; None if one is already running."""
    global _run_thread
    if not _run_lock.acquire(blocking=False):
        return None
    try:
        t = threading.Thread(target=_run_guarded, args=(segment, admin_chat),
                             name="docubridge-broadcast", daemon=True)
        t.start()
    except Exception:
        _run_lock.release()
        raise
    _run_thread = t
    return t


# ------------ buttons under the broadcast message (any user) ------------

@bot.callback_query_handler(func=lambda c: (c.data or "") in {f"{CB_USER}:go", f"{CB_USER}:mgr"})
def broadcast_user_button(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception as e:
        print(f"[Broadcast] answer_callback_query failed: {e}")
    chat_id = call.message.chat.id
    if call.data.endswith(":mgr"):
        contact_manager(chat_id)
        return
    continue_flow(chat_id)


def continue_flow(chat_id: int):
    """New funnel: route offer if the route is known, otherwise route choice."""
    state, data = get_state(chat_id)
    data = dict(data or {}) if state != DB_ERROR_STATE else {}
    ref = data.get("ref") or data.get("start_payload")
    fc, tc = data.get("from_country"), data.get("to_country")
    if not (fc and tc and is_allowed_route(fc, tc)):
        route = resolve_start_payload(ref) if ref else {}
        if route.get("kind") == "route":
            fc, tc = route.get("from_country"), route.get("to_country")
        else:
            fc = tc = None
    print(f"[Broadcast] chat={chat_id} continue from {state} → "
          f"{'route_offer' if fc and tc else 'choose_route'}")
    start_send_flow(chat_id, role=data.get("role") or "sender", ref=ref,
                    from_country=fc, to_country=tc)
