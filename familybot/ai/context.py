#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Контекст для парсера: сводка открытых записей + запись «в фокусе».

Нужен, чтобы claude понимал ссылки «то же самое», «перенеси его», «такое же в пятницу».
"""

from ..db import db, state_get
from ..timeutil import fmt_dt, now, parse_iso


def item_brief(r):
    parts = [f"#{r['id']}", r["kind"], f"«{r['title']}»"]
    if r["when_dt"]:
        parts.append(fmt_dt(r["when_dt"], r["has_time"]))
    if r["end_dt"]:
        edt = parse_iso(r["end_dt"])
        if edt:
            parts.append(f"до {edt.hour:02d}:{edt.minute:02d}")
    if r["category"] and r["category"] != "general":
        parts.append("кат:" + r["category"])
    if r["recur"]:
        parts.append("повтор:" + r["recur"])
    if r["who"]:
        parts.append("кто:" + r["who"])
    if r["note"]:
        parts.append("прим:" + r["note"][:40])
    return " ".join(parts)


def context_block(chat_id, ref_id=None):
    """Сводка открытых записей + «запись в фокусе» — чтобы парсер понимал ссылки."""
    c = db()
    lines = []
    if ref_id:
        r = c.execute("SELECT * FROM items WHERE id=?", (int(ref_id),)).fetchone()
        if r:
            lines.append("Пользователь ОТВЕТИЛ (reply) на эту запись — действие относится ИМЕННО "
                         "к ней (источник для копии/переноса/изменения): " + item_brief(r))
    else:
        fid = state_get(f"focus_{chat_id}")
        if fid:
            r = c.execute("SELECT * FROM items WHERE id=?", (int(fid),)).fetchone()
            if r:
                lines.append("В фокусе (последняя запись): " + item_brief(r))
    # ВАЖНО: сначала будущее. Раньше был один запрос "ORDER BY when_dt LIMIT 30" —
    # он отдавал 30 САМЫХ СТАРЫХ записей, и с ростом архива модель переставала видеть
    # ближайшие события вообще (на 08.08.2026 из 9 будущих в контекст попадало одно).
    nn = now().replace(tzinfo=None).isoformat(timespec="minutes")
    future = c.execute(
        "SELECT * FROM items WHERE kind='event' AND (when_dt IS NULL OR when_dt>=?) "
        "ORDER BY when_dt IS NULL, when_dt LIMIT 25", (nn,)).fetchall()
    past = c.execute(
        "SELECT * FROM items WHERE kind='event' AND when_dt<? "
        "ORDER BY when_dt DESC LIMIT 5", (nn,)).fetchall()
    places = c.execute(
        "SELECT * FROM items WHERE kind!='event' ORDER BY id DESC LIMIT 5").fetchall()
    if future:
        lines.append("Записи (ближайшие сначала):")
        lines += ["  " + item_brief(r) for r in future]
    if past:
        lines.append("Уже прошли (обычно речь не о них):")
        lines += ["  " + item_brief(r) for r in past]
    if places:
        lines.append("Места:")
        lines += ["  " + item_brief(r) for r in places]
    return "\n".join(lines) if lines else "(пока пусто)"
