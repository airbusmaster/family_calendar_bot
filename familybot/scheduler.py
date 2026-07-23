#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Фоновый планировщик: утренняя сводка, напоминания, перекат повторов."""

import time
import calendar
import traceback
from datetime import timedelta

from .config import TZ, MORNING_HOUR
from .db import db, state_get, state_set
from .timeutil import now, parse_iso, window
from .telegram.chat import all_user_ids, broadcast, push
from .items.render import line, events_in
from .calendar_sync.client import cal_push


def digest_morning():
    a, b = window("today")
    evs = events_in(a, b)
    body = "\n".join(line(r) for r in evs) if evs else "свободный день 🙂"
    return "☀️ <b>Доброе утро! План на сегодня</b>\n\n📅 " + body


def digest_evening():
    a, b = window("tomorrow")
    evs = events_in(a, b)
    if not evs:
        body = "На завтра событий пока нет — можно выдохнуть 🙂"
    else:
        body = "\n".join(line(r) for r in evs)
    return "🌙 <b>План на завтра</b>\n\n📅 " + body


def next_occurrence(dt, recur):
    if recur == "daily":
        return dt + timedelta(days=1)
    if recur == "weekly":
        return dt + timedelta(days=7)
    if recur == "biweekly":
        return dt + timedelta(days=14)
    if recur == "monthly":
        y = dt.year + (1 if dt.month == 12 else 0)
        m = 1 if dt.month == 12 else dt.month + 1
        return dt.replace(year=y, month=m, day=min(dt.day, calendar.monthrange(y, m)[1]))
    return None


def advance_recurring():
    """Прошедшее повторяющееся событие перекатывается на следующий раз."""
    n = now().replace(tzinfo=None)
    rows = db().execute(
        "SELECT * FROM items WHERE recur IS NOT NULL AND when_dt IS NOT NULL").fetchall()
    for r in rows:
        dt = parse_iso(r["when_dt"])
        if not dt:
            continue
        dur = None
        if r["end_dt"]:
            edt = parse_iso(r["end_dt"])
            if edt and edt > dt:
                dur = edt - dt
        grace = timedelta(hours=1) if r["has_time"] else timedelta(days=1)
        moved = False
        while dt and dt + grace < n:
            dt = next_occurrence(dt, r["recur"])
            moved = True
        if moved and dt:
            end_iso = (dt + dur).isoformat(timespec="minutes") if dur else None
            db().execute("UPDATE items SET when_dt=?, end_dt=?, remind_sent=0, updated_at=? "
                         "WHERE id=?",
                         (dt.isoformat(timespec="minutes"), end_iso,
                          now().isoformat(timespec="seconds"), r["id"]))
            db().commit()
            # чужие серии (созданы в календаре) не перезаписываем — iCloud сам ведёт их повтор
            if not r["ical_uid"]:
                cal_push(r["id"])


def scheduler_loop():
    while True:
        try:
            n = now()
            today = n.strftime("%Y-%m-%d")
            advance_recurring()

            if n.hour == MORNING_HOUR and state_get("morning") != today:
                for uid in all_user_ids():
                    push(uid, digest_morning(), "dig_morning")
                state_set("morning", today)

            # напоминания — только для событий, где напоминание попросили явно
            rows = db().execute(
                "SELECT * FROM items WHERE kind='event' "
                "AND remind_before_min IS NOT NULL AND remind_sent=0 AND when_dt IS NOT NULL"
            ).fetchall()
            for r in rows:
                wdt = parse_iso(r["when_dt"])
                if not wdt:
                    continue
                if not r["has_time"]:
                    wdt = wdt.replace(hour=10)  # для «на весь день» отсчитываем от 10 утра
                wdt = wdt.replace(tzinfo=TZ)
                remind_at = wdt - timedelta(minutes=r["remind_before_min"])
                if remind_at <= n:
                    if n <= wdt + timedelta(minutes=30):
                        broadcast(f"⏰ <b>Скоро:</b>\n{line(r)}")
                    db().execute("UPDATE items SET remind_sent=1 WHERE id=?", (r["id"],))
                    db().commit()
        except Exception:
            traceback.print_exc()
        time.sleep(30)
