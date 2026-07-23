#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Мелкие UI-помощники: клавиатура удаления, уведомление напарника, reply-ссылки."""

import re
import json

from .db import db, state_set
from .telegram.chat import all_user_ids, push
from .items.render import html_escape


def del_keyboard(item_id):
    return json.dumps({"inline_keyboard": [[
        {"text": "🗑 Удалить", "callback_data": f"del:{item_id}"}]]})


def reply_ref(msg):
    """Если пользователь ответил (reply) на карточку записи — вернуть её #id."""
    rt = msg.get("reply_to_message")
    if not rt:
        return None
    txt = rt.get("text") or rt.get("caption") or ""
    m = re.search(r"#(\d+)", txt)
    if not m:
        return None
    iid = int(m.group(1))
    return iid if db().execute("SELECT 1 FROM items WHERE id=?", (iid,)).fetchone() else None


def notify_partner(author_uid, verb, body, focus_id=None):
    """Сообщить второму о действии. verb — шаблон с {who}: '➕ {who} добавил(а)'."""
    a = db().execute("SELECT name FROM users WHERE tg_id=?", (author_uid,)).fetchone()
    who = a["name"] if a and a["name"] else "Партнёр"
    text = verb.format(who=html_escape(who)) + ":\n" + body
    for uid in all_user_ids():
        if uid != author_uid:
            if focus_id:
                state_set(f"focus_{uid}", str(focus_id))
            push(uid, text, "partner")
