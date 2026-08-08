#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CRUD над записями (items) + поиск, дедуп и снапшоты для отмены.

Каждая мутация зеркалится в iCloud через calendar_sync.cal_push.
"""

import re
import json
from datetime import date, datetime, timedelta

from ..config import RECUR_TITLE
from ..db import db, state_set
from ..timeutil import now, parse_iso
from ..calendar_sync.client import CAL_SYNC, CAL_QUEUE, cal_push


def norm_when(when):
    """Из строки when -> (iso_or_None, has_time)."""
    if not when or when == "null":
        return None, 0
    when = when.strip()
    try:
        if "T" in when:
            dt = datetime.fromisoformat(when)
            return dt.replace(second=0, microsecond=0).isoformat(timespec="minutes"), 1
        d = date.fromisoformat(when)
        return datetime(d.year, d.month, d.day).isoformat(timespec="minutes"), 0
    except ValueError:
        return None, 0


def add_item(intent, created_by):
    kind = intent.get("kind") or "event"
    if kind not in ("event", "place"):
        kind = "event"
    when_iso, has_time = norm_when(intent.get("when"))
    rb = intent.get("remind_before_min")
    try:
        rb = int(rb) if rb not in (None, "null", "") else None
    except (ValueError, TypeError):
        rb = None
    # напоминание ставим ТОЛЬКО если его явно попросили (без дефолта на каждое событие)
    recur = intent.get("recur")
    if recur not in RECUR_TITLE:
        recur = None
    end_iso = None
    if has_time:
        e_iso, e_ht = norm_when(intent.get("when_end"))
        if e_iso and e_ht and e_iso > when_iso:
            end_iso = e_iso
    ts = now().isoformat(timespec="seconds")
    c = db()
    cur = c.execute(
        """INSERT INTO items(kind,title,when_dt,end_dt,has_time,category,who,note,status,
                             remind_before_min,recur,created_by,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?, 'open', ?,?,?,?,?)""",
        (kind, (intent.get("title") or "без названия").strip(), when_iso, end_iso, has_time,
         intent.get("category"), intent.get("who"), intent.get("note"),
         rb, recur, created_by, ts, ts),
    )
    c.commit()
    cal_push(cur.lastrowid)
    return cur.lastrowid, kind, when_iso, has_time


def find_target(target, kind=None):
    """Найти запись по номеру или тексту среди открытых. -> (row | None, candidates)."""
    c = db()
    if target is None:
        return None, []
    target = str(target).strip()
    m = re.search(r"\d+", target)
    if m and target.replace("#", "").strip().isdigit():
        row = c.execute("SELECT * FROM items WHERE id=?", (int(m.group(0)),)).fetchone()
        if row:
            return row, []
    q = "SELECT * FROM items WHERE 1=1"
    args = []
    if kind:
        q += " AND kind=?"
        args.append(kind)
    rows = c.execute(q, args).fetchall()
    words = [w for w in re.findall(r"\w+", target.lower()) if len(w) >= 3]
    scored = []
    for r in rows:
        hay = " ".join(filter(None, [r["title"], r["who"], r["note"]])).lower()
        score = 0.0
        for w in words:
            if w in hay:
                score += 2          # точное слово
            elif len(w) >= 4 and w[:4] in hay:
                score += 1          # по корню (йогу/йоге → йог…)
            elif w[:3] in hay:
                score += 0.5
        if target.lower() in hay:
            score += 2
        if score:
            scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return None, []
    if len(scored) == 1 or scored[0][0] > scored[1][0]:
        return scored[0][1], []
    return None, [r for _, r in scored[:6]]


def remember_act(chat_id, data):
    """Запомнить последнее действие для «не верно / отмени»."""
    state_set(f"lastact_{chat_id}", json.dumps(data, ensure_ascii=False))


def reinsert(snap):
    """Вернуть запись из снапшота (undo удаления/изменения) — с прежним id."""
    cols = ["id", "kind", "title", "when_dt", "end_dt", "has_time", "category", "who", "note",
            "status", "remind_before_min", "remind_sent", "created_by",
            "created_at", "updated_at", "recur", "ical_uid", "ical_url", "cal_synced"]
    c = db()
    c.execute(f"INSERT OR REPLACE INTO items({','.join(cols)}) "
              f"VALUES({','.join('?' * len(cols))})",
              [snap.get(k) for k in cols])
    c.commit()
    cal_push(snap["id"])


_WORDS = re.compile(r"[а-яёa-z0-9]+", re.I)


def _title_words(s):
    """Слова названия, обрезанные до корня: падежи не должны мешать сравнению
    («Дарьей» и «Дарьи» — одно и то же имя). Тот же приём, что в find_target."""
    return {w[:4] for w in _WORDS.findall((s or "").lower()) if len(w) >= 3}


def same_title(a, b):
    """Одно и то же событие, даже если названо иначе.

    Сравнивать строки целиком нельзя: одно и то же модель формулирует каждый раз
    по-своему («Занятие с Дарьей Гуляевой» / «Занятие у Дарьи»). Считаем совпадением,
    если набор слов одного названия входит в другое или они пересекаются на две трети.
    """
    wa, wb = _title_words(a), _title_words(b)
    if not wa or not wb:
        return (a or "").strip().lower() == (b or "").strip().lower()
    if wa <= wb or wb <= wa:
        return True
    return len(wa & wb) / len(wa | wb) >= 0.6


def find_dup(title, when_iso, who=None, exclude_id=None):
    """Похожая запись на то же время — вероятный повтор. -> строка БД или None.

    Разные участники — разные записи: два билета на один поезд у двух пассажиров
    дублями не считаются.
    """
    if not when_iso:
        return None
    q = "SELECT * FROM items WHERE kind='event' AND when_dt=?"
    args = [when_iso]
    if exclude_id:
        q += " AND id!=?"
        args.append(exclude_id)
    nw = (who or "").strip().lower()
    for r in db().execute(q, args).fetchall():
        rw = (r["who"] or "").strip().lower()
        if rw and nw and rw != nw:
            continue
        if same_title(title, r["title"]):
            return r
    return None


def find_dup_place(title):
    """Место с тем же названием уже в списке."""
    if not title:
        return None
    for r in db().execute("SELECT * FROM items WHERE kind='place'").fetchall():
        if same_title(title, r["title"]):
            return r
    return None


def search_events(query, limit=5):
    """Поиск события по словам («когда у сына осмотр?») — по всем датам, будущие первыми."""
    words = [w for w in re.findall(r"\w+", str(query or "").lower()) if len(w) >= 3]
    if not words:
        return []
    nn = now().replace(tzinfo=None).isoformat(timespec="minutes")
    scored = []
    for r in db().execute(
            "SELECT * FROM items WHERE kind='event' AND when_dt IS NOT NULL").fetchall():
        hay = " ".join(filter(None, [r["title"], r["who"], r["note"]])).lower()
        score = 0.0
        for w in words:
            if w in hay:
                score += 2
            elif len(w) >= 4 and w[:4] in hay:
                score += 1
            elif w[:3] in hay:
                score += 0.5
        if score:
            scored.append((score, r["when_dt"] >= nn, r))
    scored.sort(key=lambda x: (-x[0], not x[1], x[2]["when_dt"]))
    return [r for _s, _f, r in scored[:limit]]


def set_status(item_id, status):
    c = db()
    c.execute("UPDATE items SET status=?, updated_at=? WHERE id=?",
              (status, now().isoformat(timespec="seconds"), item_id))
    c.commit()


def delete_item(item_id):
    c = db()
    row = c.execute("SELECT ical_url FROM items WHERE id=?", (item_id,)).fetchone()
    c.execute("DELETE FROM items WHERE id=?", (item_id,))
    c.commit()
    if CAL_SYNC:
        CAL_QUEUE.put(("delete",
                       {"id": item_id, "ical_url": row["ical_url"] if row else None}, 0))


def update_item(row, intent):
    """Обновить только переданные (не-null) поля. -> (изменилось, [предупреждения])."""
    sets, args, warns = [], [], []
    when = intent.get("when")
    if when not in (None, "null"):
        if str(when).strip().lower() == "clear":
            sets += ["when_dt=?", "has_time=?", "remind_sent=0"]
            args += [None, 0]
        else:
            when_iso, has_time = norm_when(when)
            if when_iso:
                sets += ["when_dt=?", "has_time=?", "remind_sent=0"]
                args += [when_iso, has_time]
            else:
                warns.append(f"дату «{when}» не понял — оставил прежнюю")
    for field in ("title", "category", "who", "note"):
        v = intent.get(field)
        if v not in (None, "null", ""):
            sets.append(f"{field}=?")
            args.append(str(v).strip())
    rb = intent.get("remind_before_min")
    if rb not in (None, "null", ""):
        # int() считаем ДО того, как трогаем sets: иначе на «напомни за час» вместо числа
        # плейсхолдер уже добавлен, аргумент нет, и UPDATE падает на несовпадении (баг 08.08)
        try:
            rb_val = int(rb)
        except (ValueError, TypeError):
            warns.append(f"напоминание «{rb}» не понял — оставил прежнее")
        else:
            sets += ["remind_before_min=?", "remind_sent=0"]
            args.append(rb_val)
    rec = intent.get("recur")
    if rec in RECUR_TITLE:
        sets.append("recur=?")
        args.append(rec)
    elif str(rec).strip().lower() == "clear":
        sets.append("recur=?")
        args.append(None)
    we = intent.get("when_end")
    if we not in (None, "null", ""):
        if str(we).strip().lower() == "clear":
            sets.append("end_dt=?")
            args.append(None)
        else:
            e_iso, e_ht = norm_when(we)
            if e_iso and e_ht:
                sets.append("end_dt=?")
                args.append(e_iso)
            else:
                warns.append(f"время окончания «{we}» не понял — не менял")
    if not sets:
        return False, warns
    sets.append("updated_at=?")
    args.append(now().isoformat(timespec="seconds"))
    args.append(row["id"])
    db().execute(f"UPDATE items SET {', '.join(sets)} WHERE id=?", args)
    db().commit()
    cal_push(row["id"])
    return True, warns
