# -*- coding: utf-8 -*-
"""DocuBridge Telegram bot — Phase 2 (quote-first, 4 routes, tickets, xAI)."""
# Readable modular entry (no packing). Unit tests: `import main as m`.

from docubridge_helpers import *  # noqa: F401,F403
from docubridge_runtime import (  # noqa: F401
    app,
    bot,
    client,
    SKIP_STARTUP,
    BOT_TOKEN,
    DB_URL,
    WEBHOOK_BASE,
    WEBHOOK_SECRET,
    PORT,
    XAI_API_KEY,
    ADMIN_CHAT_ID,
    MANAGER_CONTACT,
)
import docubridge_runtime as _rt

# Re-export runtime callables tests / handlers may expect on main
for _name in dir(_rt):
    if _name.startswith("_"):
        continue
    if _name not in globals():
        globals()[_name] = getattr(_rt, _name)

# Register handlers / webhook routes
import docubridge_handlers as _handlers  # noqa: F401,E402

# Also expose handler-level UI helpers on main for convenience
for _name in dir(_handlers):
    if _name.startswith("_"):
        continue
    if _name not in globals():
        globals()[_name] = getattr(_handlers, _name)

if __name__ == "__main__":
    if SKIP_STARTUP:
        print("SKIP_STARTUP=1 — not starting server")
    else:
        # startup already ran on import of handlers when not skipping
        pass
