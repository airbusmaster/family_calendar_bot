#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Доступ к боту: авто-привязка первых MAX_USERS пользователей, дальше закрыто."""

from .config import MAX_USERS
from .db import db
from .timeutil import now
from .telegram.api import send
from .items.render import html_escape
from .help_text import HELP


def ensure_access(uid, chat_id, msg):
    c = db()
    if c.execute("SELECT 1 FROM users WHERE tg_id=?", (uid,)).fetchone():
        return True
    cnt = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    if cnt < MAX_USERS:
        name = msg["from"].get("first_name") or "член семьи"
        c.execute("INSERT INTO users(tg_id,name,enrolled_at) VALUES(?,?,?)",
                  (uid, name, now().isoformat(timespec="seconds")))
        c.commit()
        send(chat_id, f"Привет, {html_escape(name)}! Ты привязан(а) к семейному органайзеру. 🎉\n\n"
             + HELP)
        return False
    send(chat_id, f"Этот бот только для семьи. Твой ID: <code>{uid}</code>")
    return False
