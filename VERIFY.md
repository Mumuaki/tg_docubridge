# Phase 2 — Verify on Render / webhook

## Env (Render)

Required:
- `TELEGRAM_BOT_TOKEN`
- `WEBHOOK_BASE` — public HTTPS base, e.g. `https://your-service.onrender.com`
- `WEBHOOK_SECRET` — path segment for `/webhook/<secret>`
- `DATABASE_URL` — Postgres
- `ADMIN_CHAT_ID` — Telegram user/chat for manager cards
- `XAI_API_KEY` — xAI key (`https://api.x.ai/v1`, model `grok-4.5`)

Optional:
- `MANAGER_CONTACT` — shown in «Связаться с менеджером» (default `@DocuBridgeSupport`)
- `PORT` — default `5000` (Render sets this)

Do **not** rely on `OPENAI_API_KEY`; Phase 2 uses `XAI_API_KEY` only.

## After deploy

1. Open `GET https://<WEBHOOK_BASE>/` → expect `OK`.
2. In Telegram: `/start` → reply keyboard with 5 items:
   - Отправить документы / Получить документы / Отследить отправление / Что можно отправлять / Связаться с менеджером
3. **Quote-first:** Отправить документы → pick one of 4 routes → cities → doc type → volume → urgency → see **price + 10–14 days** **without** name/phone → «Оформить заявку» → name → phone → confirm → ticket `DBxxxx`.
4. Deny: try free-text RU→BY / EU — bot should refuse (only 4 labeled routes).
5. «Что можно отправлять» mentions passports/ID **not** accepted.
6. `/track` or menu → enter ticket → status pipeline (default `accepted` / принято).
7. Manager chat receives plain-text card with ticket, role, route, cities, volume, urgency, contacts, ref, timestamp.
8. Logs: `[xAI] client is ON` when key set; `[Webhook]` idempotent on duplicate `update_id`.

## Local unit tests (no token)

```bash
cd /workspace/tg_docubridge
DOCUBRIDGE_SKIP_STARTUP=1 python test_phase2.py
```

## PR #12 — Google Sheet outbox + /broadcast_stuck

Render env (Dashboard → Environment), then redeploy:
- `GOOGLE_SA_JSON` — service-account key: whole JSON **or** base64 of it (auto-detected).
- `SHEET_ID` — key from the sheet URL `https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit`.
- Share the sheet with the key's `client_email` as **Editor**.
- Optional: `DOCUBRIDGE_SHEETS_SYNC_S` (default 60), `DOCUBRIDGE_BROADCAST_CAMPAIGN` (default `stuck_2026_11`).

Logs after deploy: `[Sheets] outbox thread started (pid N)`, then `[Sheets] OK: лист «…»`
(or `[Sheets] ERROR: … — reason`), then `[Sheets] outbox: exported K` (old orders backfilled).
`/health` → `"sheets": "off" | "pending" | "ok" | "error"` (does not affect `ok`).

Broadcast: admin sends `/broadcast_stuck` → dry run + preview (to admin only) → button
«Отправить N …» (single use, 10 min). Report arrives when done. Re-running never resends
to the same chat for the same campaign (`broadcast_log`).

## PR — DB round trips + webhook secret_token

### What changed
- Pool keeps connections (psycopg2 closed every returned connection because `minconn=0`).
- Autocommit for single statements; `statement_timeout` via connect `options`
  (fallback to a session `SET` if a pooler rejects `options`); `SELECT 1` pre-ping only
  after `DB_PING_IDLE_S` (default 60 s) idle. Transactions kept for `create_shipment`,
  `update_shipment_status`, `ensure_tables`, outbox/broadcast claims.
- One query for dedup + user state (`check_update_and_state`), state prefetched for the update.
- Background writer (one daemon thread, bounded queue) for `save_message`,
  `mark_update_processed`, `record_start_event`; flushed on gunicorn `worker_exit` / atexit.
  `DOCUBRIDGE_ASYNC_WRITES=0` restores synchronous writes.
- Startup log: `[DB] host=<hostname> SELECT 1 ×5: median N ms (...)` (hostname only).
- New `POST /webhook` with `X-Telegram-Bot-Api-Secret-Token` (`WEBHOOK_SECRET_TOKEN`).

Local count (`python rt_harness.py`, one funnel step, warm pool): before 6 DB calls on
6 new connections (~52 round trips); after 2 round trips on the reply path + 3 in background.

### After deploy (no env change yet)
1. Logs: `[DB] host=… median … ms`, `✅ Webhook set to: …/webhook/<secret>` (old route still used).
2. Send /start to the bot; replies should be noticeably faster. No `[DBW] … failed` lines.

### Switching the webhook to secret_token
1. Render → Environment → add `WEBHOOK_SECRET_TOKEN` (1–256 chars, only `A-Z a-z 0-9 _ -`).
   Adding a var triggers a deploy (merge mode, never replace).
2. Logs: `✅ Webhook set to: https://…/webhook (secret_token header)`; send /start, check reply.
   `[Webhook] /webhook rejected (403): token missing|mismatch` lines = foreign or misconfigured requests.
3. After a few days without traffic on the old path: follow-up PR removing `/webhook/<WEBHOOK_SECRET>`.
4. Then rotate `WEBHOOK_SECRET` (the old value appeared in access logs).
5. Remove `--access-logfile` from `GUNICORN_CMD_ARGS` if present.

Rollback: remove `WEBHOOK_SECRET_TOKEN` → next start calls `set_webhook` with the old URL.

Optional env: `DB_PING_IDLE_S` (60), `DOCUBRIDGE_ASYNC_WRITES` (1),
`DOCUBRIDGE_WRITE_QUEUE_MAX` (1000), `DOCUBRIDGE_FLUSH_TIMEOUT_S` (10).
