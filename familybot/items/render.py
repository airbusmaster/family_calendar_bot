#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Форматирование записей и списков для Telegram (HTML)."""

from ..config import KIND_TITLE, CAT_TITLE, RECUR_TITLE
from ..db import db
from ..timeutil import fmt_dt, parse_iso, window


def html_escape(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def line(r, show_kind=False):
    pre = ""
    if r["kind"] == "event" and r["when_dt"]:
        pre = fmt_dt(r["when_dt"], r["has_time"])
        if r["has_time"] and r["end_dt"]:
            edt, sdt = parse_iso(r["end_dt"]), parse_iso(r["when_dt"])
            if edt and sdt and edt.date() != sdt.date():
                # многодневное («заезд 11.09 15:00 — выезд 13.09 12:00») — показываем дату конца
                pre += f" → {fmt_dt(r['end_dt'], 1)}"
            elif edt:
                pre += f"–{edt.hour:02d}:{edt.minute:02d}"
        pre += " — "
    tags = []
    if show_kind:
        tags.append(KIND_TITLE.get(r["kind"], r["kind"]).split()[0])
    if r["category"] and r["category"] in CAT_TITLE and r["category"] != "general":
        tags.append(CAT_TITLE[r["category"]])
    if r["recur"] in RECUR_TITLE:
        tags.append("🔁 " + RECUR_TITLE[r["recur"]])
    if r["who"]:
        tags.append("👤" + html_escape(r["who"]))
    tail = ("  <i>" + " · ".join(tags) + "</i>") if tags else ""
    note = ("\n   <i>" + html_escape(r["note"]) + "</i>") if r["note"] else ""
    return f"<b>#{r['id']}</b> {pre}{html_escape(r['title'])}{tail}{note}"


def events_in(a, b, status=None):
    """События в окне [a,b). По умолчанию — все (в т.ч. отмеченные), чтобы расписание было полным."""
    q = ("SELECT * FROM items WHERE kind='event' AND when_dt IS NOT NULL "
         "AND when_dt>=? AND when_dt<?")
    args = [a.isoformat(timespec="minutes"), b.isoformat(timespec="minutes")]
    if status:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY when_dt"
    return db().execute(q, args).fetchall()


def render_list(kind, filter_):
    c = db()

    # места «куда сходить»
    if kind == "place":
        rows = c.execute("SELECT * FROM items WHERE kind='place' ORDER BY id DESC").fetchall()
        if not rows:
            return "📍 Список мест пуст. Напиши, например: «хочу сходить в новый океанариум»."
        return "📍 <b>Куда сходить</b>\n\n" + "\n".join(line(r) for r in rows)

    # расписание (по умолчанию — неделя)
    f = filter_ if filter_ in ("today", "tomorrow", "week") else "week"
    a, b = window(f)
    evs = events_in(a, b)
    names = {"today": "Сегодня", "tomorrow": "Завтра", "week": "Расписание на неделю"}
    head = "📅 <b>" + names.get(f, "Расписание") + "</b>"
    body = "\n".join(line(r) for r in evs) if evs else "пусто"
    return head + "\n\n" + body
