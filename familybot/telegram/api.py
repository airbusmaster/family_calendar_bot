#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тонкая обёртка над Telegram Bot API + загрузка файлов."""

import os

import requests

from .. import config

API = f"https://api.telegram.org/bot{config.TG_TOKEN}"


def tg(method, **params):
    try:
        r = requests.post(f"{API}/{method}", json=params, timeout=30)
        return r.json()
    except Exception as e:
        print("tg error:", method, e, flush=True)
        return {}


def send(chat_id, text, reply_markup=None):
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True}
    if reply_markup:
        params["reply_markup"] = reply_markup
    return tg("sendMessage", **params)


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
        print("voice download error", e, flush=True)
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
        print("file download error", e, flush=True)
        return None
    tmp = f"/tmp/tgfile_{file_id[:16]}{suffix}"
    with open(tmp, "wb") as f:
        f.write(data)
    return tmp
