#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Точка входа: long-polling getUpdates и запуск фоновых потоков.

Запуск: `python -m familybot` (нужна переменная окружения TG_TOKEN).
"""

import time
import threading
import traceback

import requests

from . import config
from .db import DB_LOCK, init_db
from .telegram.api import API, tg
from .handlers import handle_message, handle_callback
from .scheduler import scheduler_loop
from .calendar_sync.client import CAL_SYNC, cal_worker
from .calendar_sync.reconcile import sync_loop


def main():
    if not config.TG_TOKEN:
        raise SystemExit("TG_TOKEN не задан — укажи его в окружении (см. .env.example)")

    init_db()
    tg("setMyCommands", commands=[
        {"command": "start", "description": "о боте и как пользоваться"},
        {"command": "help", "description": "примеры команд"},
        {"command": "clean", "description": "чистый чат: убирать лишние сообщения (по умолч.)"},
        {"command": "classic", "description": "обычный режим: ничего не удалять"},
    ])
    threading.Thread(target=scheduler_loop, daemon=True).start()
    if CAL_SYNC:
        threading.Thread(target=cal_worker, daemon=True).start()
        threading.Thread(target=sync_loop, daemon=True).start()
        print("caldav sync enabled (two-way):", config.ICLOUD_CALENDAR, flush=True)
    else:
        print("caldav sync disabled (no ICLOUD_EMAIL/ICLOUD_APP_PASSWORD)", flush=True)
    print("family-bot started", flush=True)

    offset = None
    while True:
        try:
            r = requests.get(f"{API}/getUpdates",
                             params={"timeout": 50, "offset": offset}, timeout=60).json()
        except Exception as e:
            print("getUpdates error", e, flush=True)
            time.sleep(3)
            continue
        if not r.get("ok"):
            time.sleep(2)
            continue
        for upd in r.get("result", []):
            offset = upd["update_id"] + 1
            try:
                with DB_LOCK:
                    if "message" in upd:
                        handle_message(upd["message"])
                    elif "callback_query" in upd:
                        handle_callback(upd["callback_query"])
            except Exception:
                traceback.print_exc()


if __name__ == "__main__":
    main()
