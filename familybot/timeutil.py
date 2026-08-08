#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Работа со временем и датами (МСК-наивные datetime внутри БД)."""

from datetime import datetime, timedelta

from . import config


def now():
    return datetime.now(config.TZ)


def parse_iso(s):
    """Строка из БД -> datetime (МСК-наивная)."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def fmt_dt(s, has_time):
    dt = parse_iso(s)
    if not dt:
        return ""
    days = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    base = f"{dt.day:02d}.{dt.month:02d} ({days[dt.weekday()]})"
    if has_time:
        return f"{base} {dt.hour:02d}:{dt.minute:02d}"
    return base


_MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня",
               "июля", "августа", "сентября", "октября", "ноября", "декабря"]
_WEEKDAYS_FULL = ["понедельник", "вторник", "среда", "четверг",
                  "пятница", "суббота", "воскресенье"]


def fmt_day(dt):
    """Дата для заголовка списка на день: «8 августа, пятница»."""
    return f"{dt.day} {_MONTHS_GEN[dt.month - 1]}, {_WEEKDAYS_FULL[dt.weekday()]}"


def window(filter_):
    n = now()
    start = n.replace(hour=0, minute=0, second=0, microsecond=0)
    if filter_ == "today":
        return start, start + timedelta(days=1)
    if filter_ == "tomorrow":
        return start + timedelta(days=1), start + timedelta(days=2)
    if filter_ == "week":
        return start, start + timedelta(days=7)
    return None, None
