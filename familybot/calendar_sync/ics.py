#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сборка iCalendar (VEVENT) из записи БД."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..config import TZ, RECUR_RRULE
from ..timeutil import parse_iso


def ical_escape(s):
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def item_uid(item_id):
    return f"family-bot-{item_id}@familybot"


def item_ics(item):
    """Запись из БД -> VEVENT. Со временем — в UTC, без времени — событие на весь день."""
    dt = parse_iso(item["when_dt"])
    stamp = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    if item["has_time"]:
        start = dt.replace(tzinfo=TZ).astimezone(ZoneInfo("UTC"))
        end = start + timedelta(hours=1)
        edt = parse_iso(item.get("end_dt")) if item.get("end_dt") else None
        if edt and edt > dt:
            end = edt.replace(tzinfo=TZ).astimezone(ZoneInfo("UTC"))
        ds = "DTSTART:" + start.strftime("%Y%m%dT%H%M%SZ")
        de = "DTEND:" + end.strftime("%Y%m%dT%H%M%SZ")
    else:
        ds = "DTSTART;VALUE=DATE:" + dt.strftime("%Y%m%d")
        de = "DTEND;VALUE=DATE:" + (dt + timedelta(days=1)).strftime("%Y%m%d")
    desc = " · ".join(filter(None, [("👤 " + item["who"]) if item["who"] else None,
                                    item["note"] or None]))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//family-bot//RU",
        "BEGIN:VEVENT",
        "UID:" + (item.get("ical_uid") or item_uid(item["id"])),
        "DTSTAMP:" + stamp,
        ds,
        de,
        "SUMMARY:" + ical_escape(item["title"]),
    ]
    if desc:
        lines.append("DESCRIPTION:" + ical_escape(desc))
    if item.get("recur") in RECUR_RRULE:
        lines.append("RRULE:" + RECUR_RRULE[item["recur"]])
    if item.get("remind_before_min") is not None:
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Напоминание",
                  f"TRIGGER:-PT{int(item['remind_before_min'])}M", "END:VALARM"]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines)
