#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Подключение к iCloud CalDAV и фоновая очередь отправки (бот -> iCloud)."""

import queue
import threading

from .. import config
from ..db import db, state_get, state_set
from ..timeutil import now
from ..telegram.chat import broadcast
from .ics import item_ics

try:
    import caldav
    import icalendar  # noqa: F401  (нужен реконсайлеру, проверяем доступность здесь)
except ImportError:
    caldav = None

# Синк включён, только если есть библиотека и заданы реквизиты Apple ID
CAL_SYNC = bool(caldav and config.ICLOUD_EMAIL and config.ICLOUD_APP_PASSWORD)

_cal = None
_cal_lock = threading.Lock()
CAL_QUEUE = queue.Queue()


def icloud_calendar():
    """Ленивое подключение к календарю iCloud (ищем по имени, нет — создаём)."""
    global _cal
    with _cal_lock:
        if _cal is not None:
            return _cal
        client = caldav.DAVClient(url="https://caldav.icloud.com/",
                                  username=config.ICLOUD_EMAIL,
                                  password=config.ICLOUD_APP_PASSWORD)
        principal = client.principal()
        target = config.ICLOUD_CALENDAR.strip().lower()
        for c in principal.calendars():
            if (c.get_display_name() or "").strip().lower() == target:
                _cal = c
                return _cal
        _cal = principal.make_calendar(name=config.ICLOUD_CALENDAR)
        return _cal


def cal_reset():
    global _cal
    with _cal_lock:
        _cal = None


def _sync_alert():
    """Честно сказать в чат, что iCloud не синхронится (не чаще раза в час)."""
    from datetime import datetime
    last = state_get("cal_alert_ts")
    if last:
        try:
            if (now() - datetime.fromisoformat(last)).total_seconds() < 3600:
                return
        except ValueError:
            pass
    state_set("cal_alert_ts", now().isoformat())
    broadcast("⚠️ Не получается синхронизировать с календарём iCloud. Записи не потерялись — "
              "они сохранены у меня, и я продолжу пытаться отправить их в календарь.")


def cal_worker():
    """Фоновая очередь синка: не задерживает ответы бота, ошибки не роняют его.

    Пишем напрямую PUT/DELETE по фиксированному адресу события — iCloud
    на поиск по UID (REPORT) отвечает 412, поэтому event_by_uid не годится.
    Ошибка → до 3 повторов с растущей паузой, затем честный алерт в чат;
    реконсайлер (cal_reconcile) в итоге дотащит всё, что не доехало.
    """
    while True:
        task = CAL_QUEUE.get()
        op, payload, attempt = task if len(task) == 3 else (task[0], task[1], 0)
        try:
            cal = icloud_calendar()
            if isinstance(payload, dict):
                item_id, ical_url = payload.get("id"), payload.get("ical_url")
            else:
                item_id, ical_url = payload, None
            url = ical_url or (str(cal.url).rstrip("/") + f"/family-bot-{item_id}.ics")
            if op == "delete":
                r = cal.client.delete(url)
                if r.status not in (200, 204, 404):
                    raise RuntimeError(f"delete status {r.status}")
            else:  # upsert: PUT создаёт или перезаписывает
                r = cal.client.put(url, item_ics(payload),
                                   {"Content-Type": 'text/calendar; charset="utf-8"'})
                if r.status not in (200, 201, 204):
                    raise RuntimeError(f"put status {r.status}")
                db().execute("UPDATE items SET cal_synced=1 WHERE id=?", (item_id,))
                db().commit()
        except Exception as e:
            print("caldav error:", op, e, flush=True)
            cal_reset()
            if attempt < 3:
                threading.Timer(30 * (4 ** attempt),
                                CAL_QUEUE.put, args=((op, payload, attempt + 1),)).start()
            else:
                _sync_alert()
        finally:
            CAL_QUEUE.task_done()


def cal_push(item_id):
    """Отразить запись в iCloud: событие с датой — upsert, иначе — убрать."""
    if not CAL_SYNC:
        return
    row = db().execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if row and row["kind"] == "event" and row["when_dt"]:
        CAL_QUEUE.put(("upsert", dict(row), 0))
    else:
        CAL_QUEUE.put(("delete",
                       {"id": item_id, "ical_url": row["ical_url"] if row else None}, 0))
