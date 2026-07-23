#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Помощники поверх Telegram API: индикатор набора, рассылка, «чистый чат»."""

import threading

from ..db import db, state_get, state_set
from .api import tg, send, typing, delete_msg


class TypingLoop:
    """Держит «печатает…» всё время обработки — сам по себе индикатор гаснет через ~5 с."""

    def __init__(self, chat_id):
        self.chat_id = chat_id
        self._stop = threading.Event()

    def __enter__(self):
        def loop():
            while not self._stop.is_set():
                typing(self.chat_id)
                self._stop.wait(4.5)
        threading.Thread(target=loop, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._stop.set()


def all_user_ids():
    return [r["tg_id"] for r in db().execute("SELECT tg_id FROM users").fetchall()]


def broadcast(text, exclude=None):
    for uid in all_user_ids():
        if uid != exclude:
            send(uid, text)


# ------------------------------------------------------------------ чистый чат (одно окно)
def user_clean(uid):
    r = db().execute("SELECT clean_mode FROM users WHERE tg_id=?", (uid,)).fetchone()
    return True if not r or r["clean_mode"] is None else bool(r["clean_mode"])


def set_clean(uid, val):
    c = db()
    c.execute("UPDATE users SET clean_mode=? WHERE tg_id=?", (1 if val else 0, uid))
    c.commit()


def reply(chat_id, text, reply_markup=None):
    """Ответ бота с подчисткой: удаляет прошлый свой ответ, держит в чате только последний."""
    prev = state_get(f"lastbot_{chat_id}")
    if prev:
        delete_msg(chat_id, int(prev))
    r = send(chat_id, text, reply_markup=reply_markup)
    if r.get("ok"):
        state_set(f"lastbot_{chat_id}", str(r["result"]["message_id"]))
    return r


def push(chat_id, text, slot):
    """Проактивное уведомление (сводка): заменяет предыдущее того же типа, не копится."""
    prev = state_get(f"{slot}_{chat_id}")
    if prev:
        delete_msg(chat_id, int(prev))
    r = send(chat_id, text)
    if r.get("ok"):
        state_set(f"{slot}_{chat_id}", str(r["result"]["message_id"]))
    return r
