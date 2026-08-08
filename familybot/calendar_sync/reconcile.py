#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Двусторонняя сверка БД и iCloud: правки с телефона тянем к себе,
недоехавшее доталкиваем, созданное руками в календаре импортируем."""

import time
import traceback
from datetime import datetime, timedelta

from ..config import TZ
from ..db import db
from ..timeutil import now, parse_iso, fmt_dt
from ..telegram.chat import broadcast
from ..items.render import html_escape
from .ics import item_uid
from .client import (CAL_SYNC, CAL_QUEUE, icloud_calendar, cal_reset, cal_push)

try:
    import icalendar
except ImportError:
    icalendar = None


def _dt_from_ical(prop):
    """DTSTART/DTEND из ICS -> (наивный МСК datetime, has_time)."""
    v = prop.dt
    if isinstance(v, datetime):
        if v.tzinfo is None:
            return v.replace(second=0, microsecond=0), 1
        return v.astimezone(TZ).replace(tzinfo=None, second=0, microsecond=0), 1
    return datetime(v.year, v.month, v.day), 0


def _aware(s):
    """Строка даты из БД -> aware datetime (наивные считаем МСК)."""
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return datetime(1970, 1, 1, tzinfo=TZ)
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def _recur_from_rrule(comp):
    rr = comp.get("RRULE")
    if not rr:
        return None
    freq = str((rr.get("FREQ") or [""])[0])
    try:
        interval = int((rr.get("INTERVAL") or [1])[0])
    except (ValueError, TypeError):
        interval = 1
    if rr.get("BYDAY") and len(rr.get("BYDAY")) > 1:
        return None  # сложные серии (несколько дней недели) не моделируем
    if freq == "DAILY" and interval == 1:
        return "daily"
    if freq == "WEEKLY" and interval == 1:
        return "weekly"
    if freq == "WEEKLY" and interval == 2:
        return "biweekly"
    if freq == "MONTHLY" and interval == 1:
        return "monthly"
    return None


def _remote_events(cal):
    """Все события календаря: uid -> поля мастер-VEVENT."""
    out = {}
    for ev in cal.events():
        try:
            master = None
            for comp in icalendar.Calendar.from_ical(ev.data).walk("VEVENT"):
                if not comp.get("RECURRENCE-ID"):
                    master = comp
                    break
            if master is None or not master.get("DTSTART") or not master.get("UID"):
                continue
            start, has_time = _dt_from_ical(master.get("DTSTART"))
            end = None
            if has_time and master.get("DTEND"):
                e, eh = _dt_from_ical(master.get("DTEND"))
                if eh and e > start:
                    end = e
            lm = master.get("LAST-MODIFIED") or master.get("DTSTAMP")
            loc = str(master.get("LOCATION") or "").strip()
            desc = str(master.get("DESCRIPTION") or "").strip()
            out[str(master.get("UID"))] = {
                "summary": str(master.get("SUMMARY") or "").strip(),
                "start": start, "end": end, "has_time": has_time,
                "lm": (lm.dt if lm else None), "url": str(ev.url),
                "recur": _recur_from_rrule(master),
                "note": "; ".join(x for x in (loc, desc) if x) or None,
            }
        except Exception as e:
            print("[sync] parse error:", e, flush=True)
    return out


def cal_reconcile():
    """Сверка БД и iCloud в обе стороны: правки с телефона тянем к себе,
    недоехавшее — доталкиваем, созданное руками в календаре — импортируем."""
    if not CAL_SYNC:
        return
    try:
        cal = icloud_calendar()
        remote = _remote_events(cal)
    except Exception as e:
        print("[sync] fetch error:", e, flush=True)
        cal_reset()
        return
    horizon = now().replace(tzinfo=None) - timedelta(days=1)
    c = db()
    rows = c.execute("SELECT * FROM items WHERE kind='event' AND when_dt IS NOT NULL "
                     "AND when_dt >= ?", (horizon.isoformat(timespec="minutes"),)).fetchall()
    changes = []
    seen = set()

    # Предохранитель: пропавшее в iCloud событие мы удаляем и у себя — это правильно, когда
    # его стёрли с телефона. Но если CalDAV вернёт успешный, но НЕПОЛНЫЙ ответ (сбой на стороне
    # Apple, не тот календарь, ошибка разбора), тем же кодом снесёт всё будущее разом. Поэтому
    # сначала считаем пропажи и на подозрительном масштабе не удаляем ничего.
    synced = [r for r in rows if r["cal_synced"]]
    missing = [r for r in synced if (r["ical_uid"] or item_uid(r["id"])) not in remote]
    bulk_loss = missing and (len(missing) >= 3 and len(missing) > len(synced) / 3)
    if bulk_loss:
        print(f"[sync] ОТКАЗ УДАЛЯТЬ: в iCloud не нашлось {len(missing)} из {len(synced)} "
              f"синхронизированных записей — похоже на сбой, а не на правку с телефона",
              flush=True)
        broadcast("⚠️ Календарь iCloud вернул подозрительно мало событий "
                  f"(не хватает {len(missing)} из {len(synced)}). На всякий случай ничего "
                  "не удаляю — записи целы. Если ты правда чистил календарь, скажи мне, "
                  "и я приведу расписание в порядок.")

    for r in rows:
        uid = r["ical_uid"] or item_uid(r["id"])
        seen.add(uid)
        rem = remote.get(uid)
        if rem is None:
            if r["cal_synced"]:
                if bulk_loss:
                    continue  # массовая пропажа — не трогаем, ждём следующей сверки
                c.execute("DELETE FROM items WHERE id=?", (r["id"],))
                c.commit()
                changes.append("🗑 удалено: " + r["title"])
            else:
                cal_push(r["id"])  # ещё не доехало до iCloud — дотолкнём
            continue
        eff_end = r["end_dt"]
        if not eff_end and r["has_time"]:
            eff_end = (parse_iso(r["when_dt"]) + timedelta(hours=1)).isoformat(timespec="minutes")
        rem_start = rem["start"].isoformat(timespec="minutes")
        rem_end = rem["end"].isoformat(timespec="minutes") if rem["end"] else None
        differs = ((rem["summary"] and rem["summary"] != r["title"])
                   or rem_start != r["when_dt"]
                   or int(bool(rem["has_time"])) != r["has_time"]
                   or (rem_end is not None and rem_end != eff_end))
        if not differs:
            if not r["cal_synced"]:
                c.execute("UPDATE items SET cal_synced=1 WHERE id=?", (r["id"],))
                c.commit()
            continue
        foreign_series = bool(r["ical_uid"]) and bool(r["recur"])
        if rem["lm"] and rem["lm"] > _aware(r["updated_at"] or r["created_at"]):
            # на телефоне правили позже — тянем к себе
            c.execute("UPDATE items SET title=?, when_dt=?, end_dt=?, has_time=?, "
                      "remind_sent=0, updated_at=?, cal_synced=1 WHERE id=?",
                      (rem["summary"] or r["title"], rem_start,
                       rem_end if rem["has_time"] else None,
                       1 if rem["has_time"] else 0,
                       rem["lm"].isoformat(timespec="seconds"), r["id"]))
            c.commit()
            changes.append("✏️ " + (rem["summary"] or r["title"]) + " — теперь "
                           + fmt_dt(rem_start, 1 if rem["has_time"] else 0))
        elif not foreign_series:
            cal_push(r["id"])  # наша версия новее — доталкиваем в календарь
    for uid, rem in remote.items():
        if uid in seen:
            continue
        if uid.startswith("family-bot-"):
            # запись бота удалена в БД, а из iCloud не удалилась — дочистим
            CAL_QUEUE.put(("delete", {"id": uid, "ical_url": rem["url"]}, 0))
            continue
        if rem["start"] < horizon and not rem["recur"]:
            continue  # прошедшие разовые не импортируем
        ts = now().isoformat(timespec="seconds")
        lm_iso = rem["lm"].isoformat(timespec="seconds") if rem["lm"] else ts
        c.execute("""INSERT INTO items(kind,title,when_dt,end_dt,has_time,category,note,status,
                                       recur,created_at,updated_at,ical_uid,ical_url,cal_synced)
                     VALUES('event',?,?,?,?,'general',?,'open',?,?,?,?,?,1)""",
                  (rem["summary"] or "без названия",
                   rem["start"].isoformat(timespec="minutes"),
                   rem["end"].isoformat(timespec="minutes") if (rem["end"] and rem["has_time"]) else None,
                   1 if rem["has_time"] else 0, rem["note"], rem["recur"],
                   ts, lm_iso, uid, rem["url"]))
        c.commit()
        changes.append("➕ " + (rem["summary"] or "событие") + " — "
                       + fmt_dt(rem["start"].isoformat(timespec="minutes"),
                                1 if rem["has_time"] else 0))
    if changes:
        print("[sync] " + " | ".join(changes), flush=True)
        broadcast("📱 <b>Обновления из календаря iCloud:</b>\n"
                  + "\n".join("• " + html_escape(x) for x in changes))


def sync_loop():
    while True:
        try:
            cal_reconcile()
        except Exception:
            traceback.print_exc()
        time.sleep(300)
