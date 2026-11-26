# -*- coding: utf-8 -*-
"""Payment configuration approved by the owner. Logic never hard-codes these values.

Env overrides (the owner sets them; Render is not changed by code):
  DOCUBRIDGE_PAY_CURRENCIES      — currency buttons after the order, default «UAH,EUR,USD»
  DOCUBRIDGE_PAYMENT_TEXT_<CUR>  — payment details for one currency, shown verbatim
                                   («\\n» in the value = new line). Empty → the default below.

Approved by the owner 26.11.2026: UAH details (verbatim, the payment purpose has NO
ticket number). Other currencies: no details yet → «пришлём отдельным сообщением»
+ a note to the leads group. No UAH amount / exchange rate is ever calculated.
"""
import os
import re
from typing import Dict, List, Optional

DEFAULT_CURRENCIES = "UAH,EUR,USD"

# Approved text — do not edit without the owner.
PAYMENT_TEXT_DEFAULTS: Dict[str, str] = {
    "UAH": (
        "Отримувач: ФОП Саєнко Ігор\n"
        "IBAN: UA853220010000026002310087318\n"
        "ІПН/ЄДРПОУ: 2468719172\n"
        "Банк: Акціонерне товариство УНІВЕРСАЛ БАНК, МФО 322001, ЄДРПОУ банку 21133352\n"
        "Призначення платежу: за послуги з організації доставки"
    ),
}

# ---- Open questions to the owner (placeholders; None = the bot says nothing about it) ----
PREPAYMENT_RULE: Optional[str] = None      # full / partial prepayment — not decided
PAYMENT_TIMING: Optional[str] = None       # pay before / after handing over documents — not decided
RESPONSE_TIME: Optional[str] = None        # how fast a human answers — not decided
OVERWEIGHT_PRICE: Optional[str] = None     # price over 500 g — «по согласованию» until decided

_CUR_RE = re.compile(r"^[A-Z]{3}$")


def currencies() -> List[str]:
    raw = (os.getenv("DOCUBRIDGE_PAY_CURRENCIES") or "").strip() or DEFAULT_CURRENCIES
    out = []
    for part in raw.replace(";", ",").split(","):
        c = part.strip().upper()
        if _CUR_RE.match(c) and c not in out:
            out.append(c)
    return out or DEFAULT_CURRENCIES.split(",")


def payment_text_for(currency: Optional[str]) -> Optional[str]:
    """Verbatim details for the currency, or None if there are none yet."""
    cur = (currency or "").strip().upper()
    if not _CUR_RE.match(cur):
        return None
    env = (os.getenv(f"DOCUBRIDGE_PAYMENT_TEXT_{cur}") or "").strip()
    if env:
        return env.replace("\\n", "\n")
    return PAYMENT_TEXT_DEFAULTS.get(cur)
