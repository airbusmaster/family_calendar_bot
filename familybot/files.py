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


def show_draft(chat_id, events, prefix="", head="📸 <b>Вот что я распознал:</b>"):
    """Показать черновик с кнопками; правится обычным текстом, добавляется по кнопке.
    Токен в кнопках защищает от нажатий на устаревшую карточку."""
    token = str(int(time.time() * 1000) % 10**9)
    state_set(f"draft_{chat_id}", json.dumps({"token": token, "events": events},
                                             ensure_ascii=False))
    kb = json.dumps({"inline_keyboard": [[
        {"text": "✅ Добавить", "callback_data": f"draft:add:{token}"},
        {"text": "❌ Отмена", "callback_data": f"draft:no:{token}"}]]})
    reply(chat_id, prefix + head + "\n\n" + draft_lines(events) +
          "\n\nДобавить в календарь? Поправить можно просто текстом: "
          "<i>«время 19:00», «назови день рождения Пети», «второе — 5 августа»</i>.",
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


def _dedupe_trips(trips):
    """Один и тот же билет мог прийти дважды (пересняли, переслали ещё раз).

    Ключ обязательно включает пассажира: два билета на один поезд — это нормально,
    так двое едут одним поездом, и схлопывать их в одну запись нельзя.
    """
    seen, out = set(), []
    for t in trips:
        who = str(t.get("passenger") or "").strip().lower()
        # по пассажиру и времени отправления, а НЕ по названию: модель каждый раз пишет
        # станции по-разному («СПб-Главный» / «Санкт-Петербург-Главный (Московский Вокзал)»)
        key = ((str(t.get("depart") or ""), who) if who else
               (str(t.get("depart") or ""), str(t.get("mode") or ""),
                str(t.get("from") or "").strip().lower(),
                str(t.get("to") or "").strip().lower()))
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def _dedupe_events(events):
    """Одно событие могло попасть в несколько файлов альбома — не показываем дважды."""
    seen, out = set(), []
    for e in events:
        key = ((e.get("title") or "").strip().lower(), str(e.get("when") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def process_files(chat_id, uid, datas, prefix=""):
    """Результаты распознавания одного файла или целого альбома.

    Билеты заводим сразу, события собираем в ОДИН черновик: на каждый файл свою карточку
    делать нельзя — в чистом режиме следующая карточка удаляет предыдущую, а черновик
    в state вообще один, поэтому из четырёх фото доезжало только последнее (баг 2026-08-08).
    """
    datas = [d for d in datas if d]
    failed = sum(1 for d in datas if d.get("_error"))
    if datas and failed == len(datas):
        reply(chat_id, prefix + "🤖 Сервис распознавания сейчас недоступен — пришли файлы "
              "ещё раз через минуту-другую.")
        return
    trips, events = [], []
    for d in datas:
        if d.get("_error"):
            continue
        trips += [t for t in (d.get("trips") or []) if isinstance(t, dict)]
        events += [e for e in (d.get("events") or []) if isinstance(e, dict)]
    trips = _dedupe_trips(trips)
    events = _dedupe_events(events)
    warn = (f"\n⚠️ Ещё {failed} файл(а) разобрать не смог — пришли их отдельно."
            if failed else "")

    rows, dup_trips = add_trips(uid, trips) if trips else ([], 0)
    if dup_trips:
        warn += (f"\n👌 Билетов уже было в календаре: {dup_trips} — не дублирую.")
    trips_txt = ""
    if rows:
        trips_txt = "🧳 <b>Добавил поездку в календарь:</b>\n\n" + block(rows) + "\n\n———\n\n"
        state_set(f"focus_{chat_id}", str(rows[-1]["id"]))
        notify_partner(uid, "➕ {who} добавил(а) поездку", block(rows), rows[-1]["id"])

    if events:
        # поездки и черновик уходят одним сообщением: два reply подряд затирают друг друга
        show_draft(chat_id, events, prefix=prefix + trips_txt)
        return
    if rows:
        reply(chat_id, prefix + trips_txt.replace("\n\n———\n\n", "") + warn,
              reply_markup=del_keyboard(rows[-1]["id"]) if len(rows) == 1 else None)
        return
    if dup_trips:
        reply(chat_id, prefix + f"👌 Эти билеты уже в календаре ({dup_trips} шт.) — "
              "ничего не дублирую.")
        return
    if trips:
        reply(chat_id, prefix + "📎 Билет распознал, но не нашёл дату отправления. "
              "Добавь поездку текстом с датой, пожалуйста." + warn)
        return
    reply(chat_id, prefix + "📎 Не нашёл ни билета, ни события с датой. "
          "Пришли другой файл или напиши данные текстом." + warn)


def add_trips(uid, trips):
    """Из распознанных билетов создать события-поездки. -> (строки БД, сколько пропущено)."""
    added, skipped = [], 0
    for tr in trips:
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
        # Такой же билет уже заводили — второй раз не нужен. Сверяем по пассажиру и времени
        # отправления: сравнение по названию не работает, модель пишет станции то полностью,
        # то сокращённо, и дубль проскакивает (поймано на реальных билетах 08.08.2026).
        if tr.get("passenger"):
            same = db().execute(
                "SELECT 1 FROM items WHERE kind='event' AND category='trip' AND when_dt=? "
                "AND lower(who)=lower(?)", (when_iso, tr["passenger"])).fetchone()
        else:
            same = db().execute(
                "SELECT 1 FROM items WHERE kind='event' AND category='trip' AND when_dt=? "
                "AND lower(title)=lower(?)", (when_iso, title)).fetchone()
        if same:
            skipped += 1
            continue
        iid, _k, _w, _h = add_item(intent, uid)
        added.append(db().execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone())
    return added, skipped
