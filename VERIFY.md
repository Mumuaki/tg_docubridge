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
