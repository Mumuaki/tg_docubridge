# -*- coding: utf-8 -*-
"""New-lead notifications: Telegram card (LEADS_CHAT_ID or ADMIN_CHAT_ID) + optional
e-mail copy, delivered through the notify_log outbox with retries; daily digest;
/chatid, /digest and the «bot added to a group» notice.

Delivery:
  * request path: notify_new_lead() only spawns a thread (enqueue + first attempt),
    the client's «Заявка оформлена» is not delayed;
  * the loop (one thread per process, NOTIFY_LOOP_S=60) re-sends due rows with
    backoff (429 → retry_after), re-queues shipments whose enqueue was lost and
    sends the digest once a day;
  * one atomic claim per attempt → no duplicates; old orders are 'legacy'.
NOTIFY_OUTBOX=0 (or no DATABASE_URL) → the previous direct one-shot send.
"""
import os
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from docubridge_runtime import *  # noqa: F401,F403
from docubridge_keyboards import (
    route_line, role_label, status_inline_keyboard, STATUS_CB_PREFIX,
)
import docubridge_notify_cfg as ncfg
import docubridge_db_notify as ndb
from docubridge_email import send_lead_email, EmailError

TEST_PREFIX = "🧪 ТЕСТ"
BACKOFF_S = (60, 120, 300, 600, 1800, 3600, 3600, 3 * 3600, 6 * 3600, 12 * 3600)
TG_MAX_LEN = 4000
DIGEST_SECTION_MAX = 40

_sleep = time.sleep          # patched in tests
_now = lambda: datetime.now(timezone.utc)   # noqa: E731  (patched in tests)


# ------------ texts ------------

def _volume(p: Dict) -> str:
    parts = []
    if p.get("pages_a4"):
        parts.append(f"листов {p['pages_a4']}")
    if p.get("weight_grams"):
        parts.append(f"вес ≈ {p['weight_grams']} г")
    return ", ".join(parts) or "—"


def build_lead_card(source_chat_id: int, payload: Dict, created_at=None, test: bool = False) -> str:
    q = compute_quote(payload)
    price_line = (f"Оценка: €{q['price_eur']} (до {q['threshold_g']} г)"
                  if q["price_eur"] is not None else "Оценка: по согласованию")
    eta_line = (f"Срок: ориентировочно {q['eta_working']} рабочих дней"
                if q.get("eta_working") else "Срок: требует подтверждения")
    ts = fmt_local(created_at or _now(), "%d.%m.%Y %H:%M", label=True)
    ref = payload.get("ref") or payload.get("start_payload") or "—"
    head = "Новый лид DocuBridge"
    if test:
        head = f"{TEST_PREFIX} — заявка администратора\n{head}"
    lines = [
        head,
        f"Ticket: {payload.get('ticket', '—')}",
        f"Chat ID: {source_chat_id}",
        f"Time: {ts}",
        f"Source/ref: {ref}",
        "",
        f"Роль: {role_label(payload.get('role') or 'sender')}",
        f"Маршрут: {route_line(payload)}",
        f"Тип документа: {payload.get('doc_type', '—')}",
        f"Объём: {_volume(payload)}",
        f"Срочность: {payload.get('urgency', '—')}",
        price_line,
        eta_line,
        "",
        f"Имя: {payload.get('name', '—')}",
        f"Телефон: {payload.get('phone', '—')}",
    ]
    return "\n".join(lines)


def email_subject(payload: Dict) -> str:
    return f"Новая заявка DocuBridge {payload.get('ticket', '')}: {route_line(payload)}"


# ------------ Telegram send with error classification ------------

def _tg_error_info(e) -> Dict:
    """{'retry_after': s|None, 'migrate_to': id|None, 'code': int|None, 'text': str}"""
    code = getattr(e, "error_code", None)
    params = {}
    rj = getattr(e, "result_json", None)
    if isinstance(rj, dict):
        params = rj.get("parameters") or {}
    return {"code": code, "retry_after": params.get("retry_after"),
            "migrate_to": params.get("migrate_to_chat_id"),
            "text": str(getattr(e, "description", None) or e)[:200]}


def _send_card(target: int, text: str, ticket: Optional[str]):
    markup = status_inline_keyboard(ticket) if ticket else None
    return bot.send_message(target, text, reply_markup=markup)


def send_telegram_card(ticket: str, source_chat_id: int, payload: Dict, created_at=None,
                       admin_override=None):
    """Returns (message_id, target). Follows a group → supergroup migration once."""
    test = ncfg.is_test_lead(source_chat_id, admin_override)
    target = ncfg.notify_target(admin_override)
    text = build_lead_card(source_chat_id, dict(payload, ticket=ticket), created_at, test=test)
    try:
        m = _send_card(target, text, ticket)
    except Exception as e:
        info = _tg_error_info(e)
        if not info["migrate_to"]:
            raise
        new_id = int(info["migrate_to"])
        print(f"[NOTIFY] LEADS chat {target} became supergroup {new_id} — update LEADS_CHAT_ID")
        _alert_admin(f"⚠️ Группа заявок стала супергруппой. Обновите LEADS_CHAT_ID на {new_id}.",
                     admin_override)
        m = _send_card(new_id, text, ticket)
        target = new_id
    return getattr(m, "message_id", None), target


def _alert_admin(text: str, admin_override=None) -> bool:
    a = ncfg.admin_id(admin_override)
    if not a:
        return False
    try:
        bot.send_message(a, text)
        return True
    except Exception as e:
        print(f"[NOTIFY] admin alert failed: {_tg_error_info(e)['text']}")
        return False


def _backoff(attempt: int) -> float:
    return float(BACKOFF_S[min(max(attempt, 1) - 1, len(BACKOFF_S) - 1)])


# ------------ outbox ------------

def plan_rows(chat_id: int, payload: Dict) -> List[Dict]:
    rows = []
    tg = ncfg.telegram_plan(chat_id)
    if tg == "send":
        rows.append({"channel": "telegram", "status": "pending", "target": ncfg.notify_target()})
    else:
        rows.append({"channel": "telegram", "status": "skipped",
                     "last_error": "admin test order" if tg == "skip_test" else "no ADMIN_CHAT_ID/LEADS_CHAT_ID"})
    if ncfg.email_enabled() and not ncfg.is_test_lead(chat_id):
        rows.append({"channel": "email", "status": "pending", "target": ",".join(ncfg.email_to())})
    return rows


def _sheet_mark(ticket: str, value: str):
    try:
        from docubridge_sheets import update_notify_async
        update_notify_async(ticket, value)
    except Exception as e:
        print(f"[Sheets] notify mark skipped: {e}")


def deliver(ticket: str, channel: str, chat_id: int, payload: Dict, created_at=None) -> Optional[str]:
    """One attempt for one (ticket, channel). Returns the resulting status or None (not claimed)."""
    attempt = ndb.claim_notify(ticket, channel)
    if attempt is None:
        return None
    payload = dict(payload or {}, ticket=ticket)
    try:
        if channel == "telegram":
            if ncfg.telegram_plan(chat_id) != "send":
                ndb.finish_notify(ticket, channel, "skipped", error="disabled by env")
                return "skipped"
            msg_id, target = send_telegram_card(ticket, chat_id, payload, created_at)
            ndb.finish_notify(ticket, channel, "sent", message_id=msg_id, target=target)
            print(f"[NOTIFY] sent {ticket} tg msg={msg_id} chat={target}")
            _sheet_mark(ticket, "да")
            return "sent"
        if channel == "email":
            if not ncfg.email_enabled():
                ndb.finish_notify(ticket, channel, "skipped", error="email disabled by env")
                return "skipped"
            mid = send_lead_email(email_subject(payload), build_lead_card(chat_id, payload, created_at),
                                  idempotency_key=f"docubridge-lead-{ticket}")
            ndb.finish_notify(ticket, channel, "sent", message_id=mid)
            print(f"[NOTIFY] sent {ticket} email id={mid or '-'} via {ncfg.email_provider()}")
            return "sent"
        ndb.finish_notify(ticket, channel, "skipped", error=f"unknown channel {channel}")
        return "skipped"
    except Exception as e:
        if isinstance(e, EmailError):
            err, retry_after, permanent = str(e), e.retry_after, not e.retryable
        else:
            info = _tg_error_info(e)
            err, retry_after, permanent = f"{info['code'] or type(e).__name__}: {info['text']}", info["retry_after"], False
        final = permanent or attempt >= ncfg.max_attempts()
        if final:
            ndb.finish_notify(ticket, channel, "failed", error=err)
            print(f"[NOTIFY] FAILED {ticket} {channel} after {attempt} attempt(s): {err}")
            if channel == "telegram":
                _sheet_mark(ticket, "ошибка")
                if ncfg.notify_target() != ncfg.admin_id():
                    _alert_admin(f"⚠️ Не удалось доставить уведомление о заявке {ticket} в группу: {err}\n"
                                 f"Карточка: /order {ticket}")
            return "failed"
        delay = float(retry_after) if retry_after else _backoff(attempt)
        ndb.finish_notify(ticket, channel, "retry", error=err, retry_in_s=delay)
        print(f"[NOTIFY] {ticket} {channel} attempt {attempt} failed: {err} — retry in {delay:.0f}s")
        return "retry"


def _enqueue_and_deliver(ticket: str, chat_id: int, payload: Dict):
    try:
        ndb.enqueue_notify(ticket, plan_rows(chat_id, payload))
        for ch in ndb.CHANNELS:
            deliver(ticket, ch, chat_id, payload)
    except Exception as e:  # the loop will pick it up
        print(f"[NOTIFY] first attempt error {ticket}: {e}")


def _spawn(target, *args):
    t = threading.Thread(target=target, args=args, daemon=True, name="docubridge-notify-first")
    t.start()
    return t


def notify_new_lead(source_chat_id: int, payload: Dict, admin_override=None):
    """Entry point after create_shipment. Never raises."""
    try:
        ticket = payload.get("ticket")
        tg = ncfg.telegram_plan(source_chat_id, admin_override)
        if tg == "no_target":
            print("[ADMIN] ADMIN_CHAT_ID не задан — уведомление не отправлено")
        elif tg == "skip_test":
            print("[ADMIN] ADMIN_CHAT_ID == user chat — skip (test mode)")
        if ticket and ncfg.outbox_enabled():
            return _spawn(_enqueue_and_deliver, ticket, source_chat_id, dict(payload))
        # direct mode (NOTIFY_OUTBOX=0 / no DB): the previous one-shot behaviour
        if tg == "send":
            try:
                msg_id, target = send_telegram_card(ticket, source_chat_id, payload,
                                                    admin_override=admin_override)
                print(f"[NOTIFY] sent {ticket or '-'} tg msg={msg_id} chat={target} (direct)")
            except Exception as e:
                print(f"[ADMIN notify] error: {_tg_error_info(e)['text']}")
        if ncfg.email_enabled() and not ncfg.is_test_lead(source_chat_id, admin_override):
            _spawn(_email_direct, dict(payload), source_chat_id)
    except Exception as e:
        print(f"[ADMIN notify] error: {e}")
    return None


def _email_direct(payload: Dict, chat_id: int):
    try:
        send_lead_email(email_subject(payload), build_lead_card(chat_id, payload),
                        idempotency_key=f"docubridge-lead-{payload.get('ticket')}")
        print(f"[NOTIFY] sent {payload.get('ticket') or '-'} email (direct)")
    except Exception as e:
        print(f"[NOTIFY] email (direct) failed: {e}")


def process_due_once(limit: int = 20) -> int:
    """Sweep lost enqueues, then (re)send due rows. Returns number of 'sent'."""
    if not ncfg.outbox_enabled():
        return 0
    for sh in ndb.list_unqueued_shipments():
        print(f"[NOTIFY] {sh['ticket']} had no notify rows — queued now")
        ndb.enqueue_notify(sh["ticket"], plan_rows(sh["chat_id"], sh.get("payload") or {}))
    sent = 0
    for row in ndb.list_due_notify(limit):
        if deliver(row["ticket"], row["channel"], row["chat_id"], row.get("payload") or {},
                   row.get("created_at")) == "sent":
            sent += 1
    return sent


# ------------ daily digest ------------

def _digest_line(r: Dict) -> str:
    p = r.get("payload") or {}
    parts = [str(r.get("ticket")), fmt_local(r.get("created_at"), "%d.%m %H:%M"),
             route_line(p), p.get("name") or "—", p.get("phone") or "—"]
    if p.get("pages_a4"):
        parts.append(f"{p['pages_a4']} л.")
    return " · ".join(parts)


def build_digest(rows: List[Dict], day_label: str) -> str:
    new = [r for r in rows if r.get("is_new")]
    stale = [r for r in rows if not r.get("is_new")
             and (normalize_status(r.get("status")) or "accepted") == "accepted"]
    if not new and not stale:
        return f"📋 Сводка DocuBridge {day_label}: за сутки заявок нет, необработанных нет."
    out = [f"📋 Сводка DocuBridge {day_label} (время — {DISPLAY_TZ_LABEL})", ""]
    for title, items in ((f"Новые за 24 ч ({len(new)}):", new),
                         (f"Не обработаны — «принято» дольше 24 ч ({len(stale)}):", stale)):
        out.append(title)
        out += [_digest_line(r) for r in items[-DIGEST_SECTION_MAX:]] or ["— нет"]
        if len(items) > DIGEST_SECTION_MAX:
            out.append(f"…и ещё {len(items) - DIGEST_SECTION_MAX} (см. /orders)")
        out.append("")
    return "\n".join(out).rstrip()


def _chunks(text: str, limit: int = TG_MAX_LEN) -> List[str]:
    out, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + 1 + len(line) > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def send_digest(chat_id: int, day_label: str) -> bool:
    rows = ndb.digest_shipments(ncfg.admin_id())
    if rows is None:
        print("[DIGEST] no DB / DB error — not sent")
        return False
    for part in _chunks(build_digest(rows, day_label)):
        bot.send_message(chat_id, part)
    return True


def cfg_db_url():
    import docubridge_config as cfg
    return cfg.DB_URL


_digest_done = {"day": None, "pid": None}


def maybe_send_digest(now: Optional[datetime] = None) -> bool:
    """At/after DIGEST_HOUR local time, once per local day (DB marker survives restarts)."""
    if not ncfg.digest_enabled() or not cfg_db_url():
        return False
    local = (now or _now()).astimezone(ncfg.digest_tz())
    if local.hour < ncfg.digest_hour():
        return False
    day = local.date()
    if _digest_done["day"] == day and _digest_done["pid"] == os.getpid():
        return False
    target = ncfg.notify_target()
    if not target:
        return False
    if not ndb.claim_digest(day):
        _digest_done.update(day=day, pid=os.getpid())
        return False
    try:
        ok = send_digest(target, local.strftime("%d.%m.%Y"))
    except Exception as e:
        ok = False
        print(f"[DIGEST] send failed: {_tg_error_info(e)['text']}")
    ndb.finish_digest(day, ok, "" if ok else "send failed")
    if ok:
        _digest_done.update(day=day, pid=os.getpid())
        print(f"[DIGEST] sent for {day} to chat {target}")
    return ok


# ------------ loop thread ------------

_thread_lock = threading.Lock()
_thread = None
_thread_pid = None


def _loop():
    while True:
        try:
            process_due_once()
        except Exception as e:
            print(f"[NOTIFY] loop error: {e}")
        try:
            maybe_send_digest()
        except Exception as e:
            print(f"[DIGEST] loop error: {e}")
        _sleep(ncfg.loop_period_s())


def start_notify_thread():
    global _thread, _thread_pid
    if not cfg_db_url():
        return None
    pid = os.getpid()
    with _thread_lock:
        if _thread is not None and _thread_pid == pid and _thread.is_alive():
            return _thread
        t = threading.Thread(target=_loop, name="docubridge-notify", daemon=True)
        t.start()
        _thread, _thread_pid = t, pid
    lc = ncfg.leads_chat_id()
    print(f"[NOTIFY] thread started (pid {pid}): every {ncfg.loop_period_s()}s; "
          f"telegram → {'LEADS_CHAT_ID ' + str(lc) if lc else 'ADMIN_CHAT_ID'}; "
          f"outbox {'on' if ncfg.outbox_enabled() else 'off'}; "
          f"email {ncfg.email_provider() or 'off'}; "
          f"digest {'%02d:00 %s' % (ncfg.digest_hour(), ncfg.digest_tz()) if ncfg.digest_enabled() else 'off'}")
    return t


def notify_health() -> str:
    if not cfg_db_url():
        return "off"
    if _thread is None or _thread_pid != os.getpid():
        return "not_started"
    return "alive" if _thread.is_alive() else "dead"


# ------------ group handling / commands ------------

def can_manage(user_id) -> bool:
    """Status buttons / group commands: admin + LEADS_MANAGER_IDS (by from_user.id)."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    return (bool(ncfg.admin_id()) and uid == ncfg.admin_id()) or uid in ncfg.manager_ids()


GROUP_COMMANDS = {"chatid", "digest"}


def update_allowed(update) -> bool:
    """Non-private chats never reach the client funnel: only status buttons and
    /chatid, /digest from admin/managers pass; my_chat_member always passes."""
    for attr in ("message", "edited_message"):
        m = getattr(update, attr, None)
        if m is None:
            continue
        if getattr(m.chat, "type", "private") == "private":
            return True
        uid = getattr(getattr(m, "from_user", None), "id", None)
        return command_name(getattr(m, "text", None)) in GROUP_COMMANDS and can_manage(uid)
    for attr in ("channel_post", "edited_channel_post"):
        if getattr(update, attr, None) is not None:
            return False
    cq = getattr(update, "callback_query", None)
    if cq is not None:
        msg = getattr(cq, "message", None)
        if msg is None or getattr(msg.chat, "type", "private") == "private":
            return True
        return (cq.data or "").startswith(STATUS_CB_PREFIX + ":")
    return True


def _is_private(message) -> bool:
    return getattr(message.chat, "type", "private") == "private"


@bot.message_handler(commands=["chatid"], func=lambda m: can_manage(getattr(m.from_user, "id", None)))
def chatid_cmd(message):
    chat = message.chat
    title = getattr(chat, "title", None)
    lc = ncfg.leads_chat_id()
    note = ""
    if not _is_private(message):
        note = ("\nЭто текущий LEADS_CHAT_ID ✅" if lc == chat.id
                else "\nЧтобы слать сюда заявки: Render → Environment → LEADS_CHAT_ID = этот id.")
    print(f"[NOTIFY] /chatid in chat {chat.id} ({getattr(chat, 'type', '?')})")
    bot.send_message(chat.id, f"Chat ID: {chat.id}" + (f" («{title}»)" if title else "") + note)


@bot.message_handler(commands=["digest"], func=lambda m: can_manage(getattr(m.from_user, "id", None)))
def digest_cmd(message):
    """Digest right now to this chat; does not mark the day as sent."""
    day = _now().astimezone(ncfg.digest_tz()).strftime("%d.%m.%Y")
    try:
        if not send_digest(message.chat.id, day):
            bot.send_message(message.chat.id, "Сводка недоступна: нет связи с БД.")
    except Exception as e:
        print(f"[DIGEST] /digest failed: {_tg_error_info(e)['text']}")


@bot.my_chat_member_handler()
def on_my_chat_member(upd):
    chat = upd.chat
    if getattr(chat, "type", "private") == "private":
        return
    old = getattr(upd.old_chat_member, "status", None)
    new = getattr(upd.new_chat_member, "status", None)
    title = getattr(chat, "title", None) or "—"
    who = getattr(getattr(upd, "from_user", None), "id", None)
    if new in ("member", "administrator") and old in ("left", "kicked", None):
        text = (f"Бот добавлен в группу «{title}», id {chat.id}.\n"
                f"Чтобы заявки шли туда: Render → Environment → LEADS_CHAT_ID = {chat.id}.")
    elif new in ("left", "kicked"):
        text = f"Бот удалён из группы «{title}», id {chat.id}."
    else:
        return
    print(f"[NOTIFY] my_chat_member: chat {chat.id} ({chat.type}) {old} → {new} by user {who}")
    _alert_admin(text)
