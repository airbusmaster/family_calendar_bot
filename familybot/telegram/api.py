#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тонкая обёртка над Telegram Bot API + загрузка файлов."""

import os

import requests

from .. import config
from ..cards import extract_cards

API = f"https://api.telegram.org/bot{config.TG_TOKEN}"


def mask(text):
    """Убрать токен бота из текста ошибки: requests кладёт в исключение полный URL,
    и при сбое DNS журнал за сутки набивается сотнями строк с токеном (найдено 08.08.2026)."""
    s = str(text)
    return s.replace(config.TG_TOKEN, "<TOKEN>") if config.TG_TOKEN else s


def tg(method, **params):
    try:
        r = requests.post(f"{API}/{method}", json=params, timeout=30)
        return r.json()
    except Exception as e:
        print("tg error:", method, mask(e), flush=True)
        return {}


def remember_card(chat_id, message_id, ids):
    """Запомнить, о какой записи это сообщение, — чтобы reply на карточку нашёл её без #номера."""
    if not ids or not message_id:
        return
    from ..db import db, state_set
    state_set(f"card_{chat_id}_{message_id}", str(ids[0]))
    # держим только последние 300 карточек, иначе таблица состояния растёт бесконечно
    c = db()
    c.execute("DELETE FROM state WHERE key LIKE 'card\\_%' ESCAPE '\\' AND rowid NOT IN "
              "(SELECT rowid FROM state WHERE key LIKE 'card\\_%' ESCAPE '\\' "
              "ORDER BY rowid DESC LIMIT 300)")
    c.commit()


def send(chat_id, text, reply_markup=None):
    ids, text = extract_cards(text)
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True}
    if reply_markup:
        params["reply_markup"] = reply_markup
    r = tg("sendMessage", **params)
    if r.get("ok"):
        remember_card(chat_id, r["result"]["message_id"], ids)
    return r


def edit_text(chat_id, message_id, text, reply_markup=None):
    ids, text = extract_cards(text)
    params = {"chat_id": chat_id, "message_id": message_id, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup:
        params["reply_markup"] = reply_markup
    r = tg("editMessageText", **params)
    if r.get("ok"):
        remember_card(chat_id, message_id, ids)
    return r


def typing(chat_id):
    tg("sendChatAction", chat_id=chat_id, action="typing")


def delete_msg(chat_id, message_id):
    if message_id:
        tg("deleteMessage", chat_id=chat_id, message_id=message_id)


def download_voice(file_id):
    r = tg("getFile", file_id=file_id)
    if not r.get("ok"):
        return None
    path = r["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{config.TG_TOKEN}/{path}"
    try:
        data = requests.get(url, timeout=60).content
    except Exception as e:
        print("voice download error", mask(e), flush=True)
        return None
    tmp = f"/tmp/voice_{file_id[:16]}.ogg"
    with open(tmp, "wb") as f:
        f.write(data)
    return tmp


def download_tg_file(file_id, suffix):
    """Скачать произвольный файл (документ/фото) во временный путь."""
    r = tg("getFile", file_id=file_id)
    if not r.get("ok"):
        return None
    path = r["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{config.TG_TOKEN}/{path}"
    try:
        data = requests.get(url, timeout=120).content
    except Exception as e:
        print("file download error", mask(e), flush=True)
        return None
    tmp = f"/tmp/tgfile_{file_id[:16]}{suffix}"
    with open(tmp, "wb") as f:
        f.write(data)
    return tmp
