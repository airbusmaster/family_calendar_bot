#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Обработка присланных файлов: билеты добавляем сразу, события — через черновик."""

import json
import time

from .config import TRIP_EMOJI
from .db import db, state_get, state_set
from .timeutil import now, parse_iso, fmt_dt
from .telegram.chat import reply
from .items.render import block, html_escape
from .items.repository import norm_when, add_item, find_dup, remember_act
from .ui import del_keyboard, notify_partner


def draft_lines(events):
    """Номер у пункта ставим, только когда событий несколько — иначе он лишний шум."""
    out = []
    many = len(events) > 1
    for i, ev in enumerate(events, 1):
        when_iso, ht = norm_when(ev.get("when"))
        dt_txt = fmt_dt(when_iso, ht) if when_iso else "❓ дата не распознана"
        if when_iso and ht:
            e_iso, e_ht = norm_when(ev.get("when_end"))
            if e_iso and e_ht and e_iso > when_iso:
                edt = parse_iso(e_iso)
                dt_txt += f"–{edt.hour:02d}:{edt.minute:02d}"
        num = f"{i}. " if many else ""
        parts = [f"{num}<b>{html_escape(ev.get('title') or 'без названия')}</b> — {dt_txt}"]
        if ev.get("who"):
            parts.append("👤 " + html_escape(str(ev["who"])))
        if ev.get("note"):
            parts.append("<i>" + html_escape(str(ev["note"])) + "</i>")
        out.append("\n   ".join(parts))
    return "\n\n".join(out)


def draft_state(chat_id):
    """Текущий черновик: {"token": ..., "events": [...]} или None."""
    raw = state_get(f"draft_{chat_id}")
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    if isinstance(d, list):  # старый формат без токена
        d = {"token": None, "events": d}
    return d if d.get("events") else None


def show_draft(chat_id, events, prefix=""):
    """Показать черновик с кнопками; правится обычным текстом, добавляется по кнопке.
    Токен в кнопках защищает от нажатий на устаревшую карточку."""
    token = str(int(time.time() * 1000) % 10**9)
    state_set(f"draft_{chat_id}", json.dumps({"token": token, "events": events},
                                             ensure_ascii=False))
    kb = json.dumps({"inline_keyboard": [[
        {"text": "✅ Добавить", "callback_data": f"draft:add:{token}"},
        {"text": "❌ Отмена", "callback_data": f"draft:no:{token}"}]]})
    reply(chat_id, prefix + "📸 <b>Вот что я распознал:</b>\n\n" + draft_lines(events) +
          "\n\nДобавить в календарь? Поправить можно просто текстом: "
          "<i>«время 19:00», «назови день рождения Пети», «дата 5 августа»</i>.",
          reply_markup=kb)


def confirm_draft(chat_id, uid):
    """Добавить события из черновика. -> (rows, warns); rows: None — черновика нет,
    [] — нет ни одной даты (черновик сохраняется, чтобы дату дописали)."""
    d = draft_state(chat_id)
    if not d:
        return None, []
    rows, warns = [], []
    for ev in d["events"]:
        when_iso, _ht = norm_when(ev.get("when"))
        if not when_iso:
            warns.append(f"«{ev.get('title') or 'без названия'}» — без даты, не добавил")
            continue
        dup = find_dup((ev.get("title") or "").strip(), when_iso)
        intent = {"kind": "event", "title": ev.get("title"), "when": ev.get("when"),
                  "when_end": ev.get("when_end"),
                  "category": ev.get("category") or "general", "who": ev.get("who"),
                  "note": ev.get("note"), "remind_before_min": None}
        iid, _k, _w, _h = add_item(intent, uid)
        rows.append(db().execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone())
        if dup:
            warns.append(f"похоже на дубль #{dup['id']} — если лишнее, скажи «отмени»")
    if not rows:
        return [], warns
    state_set(f"draft_{chat_id}", "")
    state_set(f"focus_{chat_id}", str(rows[-1]["id"]))
    if len(rows) == 1:
        remember_act(chat_id, {"action": "add", "id": rows[-1]["id"]})
    else:
        remember_act(chat_id, {"action": "bulk_add", "ids": [r["id"] for r in rows]})
    return rows, warns


def process_file(chat_id, uid, data):
    """Распознанный файл: билет добавляем сразу, событие — через черновик с подтверждением."""
    if data and data.get("_error"):
        reply(chat_id, "🤖 Сервис распознавания сейчас недоступен — пришли файл ещё раз "
              "через минуту-другую.")
        return
    if data and data.get("trips"):
        process_ticket(chat_id, uid, data)
    elif data and data.get("events"):
        show_draft(chat_id, data["events"])
    else:
        reply(chat_id, "📎 Не нашёл в файле ни билета, ни события с датой. "
              "Пришли другой файл или напиши данные текстом.")


def process_ticket(chat_id, uid, data):
    """Из распознанного билета создать события-поездки в календаре."""
    if not data or not data.get("trips"):
        reply(chat_id, "📎 Не нашёл в файле билет или поездку. Пришли другой файл "
              "(PDF/фото билета) или напиши данные текстом.")
        return
    added = []
    for tr in data["trips"]:
        when_iso, _ht = norm_when(tr.get("depart"))
        if not when_iso:
            continue
        emoji = TRIP_EMOJI.get(tr.get("mode") or "other", "🧳")
        title = f"{emoji} {tr.get('from') or '?'} → {tr.get('to') or '?'}"
        np = []
        if tr.get("number"):
            np.append(str(tr["number"]))
        if tr.get("seat"):
            np.append("место " + str(tr["seat"]))
        if tr.get("note"):
            np.append(str(tr["note"]))
        intent = {"kind": "event", "title": title, "when": tr.get("depart"),
                  "category": "trip", "who": tr.get("passenger"),
                  "note": "; ".join(np) or None, "remind_before_min": None}
        iid, _k, _w, _h = add_item(intent, uid)
        added.append(db().execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone())
    if not added:
        reply(chat_id, "📎 Файл распознал, но не нашёл дату отправления. "
              "Добавь поездку текстом с датой, пожалуйста.")
        return
    state_set(f"focus_{chat_id}", str(added[-1]["id"]))
    body = block(added)
    reply(chat_id, "🧳 <b>Добавил поездку в календарь:</b>\n\n" + body,
          reply_markup=del_keyboard(added[-1]["id"]) if len(added) == 1 else None)
    notify_partner(uid, "➕ {who} добавил(а) поездку", body, added[-1]["id"])
