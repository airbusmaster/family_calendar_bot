#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Невидимая метка записи в тексте карточки.

Номера (#12) из карточек убраны — они мозолили глаза. Но ответ (reply) на карточку
должен по-прежнему указывать боту на конкретную запись, поэтому id едет в тексте
служебными символами: `send()` вырезает метку перед отправкой и запоминает
соответствие message_id -> id записи (см. telegram/api.py и ui.reply_ref).
"""

import re

MARK_START = "\x01"
MARK_END = "\x02"
_MARK_RE = re.compile(MARK_START + r"(\d+)" + MARK_END)


def card_mark(item_id):
    return f"{MARK_START}{item_id}{MARK_END}"


def extract_cards(text):
    """-> (список id записей в порядке появления, текст без меток)."""
    if not text or MARK_START not in text:
        return [], text
    ids = [int(m) for m in _MARK_RE.findall(text)]
    return ids, _MARK_RE.sub("", text)
