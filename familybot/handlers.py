#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Обработка апдейтов Telegram: сообщения, интенты, батчи, отмена, колбэки кнопок."""

import os
import re
import json
import time
import hashlib
import threading
import traceback
from datetime import datetime

from .db import db, DB_LOCK, state_get, state_set
from .timeutil import now
from .telegram.api import (tg, send, edit_text, typing, delete_msg,
                           download_voice, download_tg_file)
from .telegram.chat import TypingLoop, all_user_ids, user_clean, set_clean, reply
from .items.render import line, block, html_escape, render_list
from .items.repository import (norm_when, add_item, find_target, find_dup, find_dup_place,
                               search_events, delete_item, update_item, remember_act,
                               reinsert)
from .ai.claude import claude_json, service_error_text
from .ai.parser import parse_intent, analyze_file, analyze_forward
from .ai.voice import transcribe
from .ai.prompts import DRAFT_PROMPT
from .config import CLAUDE_MODEL
from .help_text import HELP
from .ui import del_keyboard, reply_ref, notify_partner
from .files import draft_state, show_draft, confirm_draft, process_files
from .access import ensure_access
from . import loans


# ------------------------------------------------------------------ альбомы (несколько файлов)
_MEDIA_GROUPS = {}
_MG_LOCK = threading.Lock()
MEDIA_GROUP_WAIT = 3.0   # сколько ждём остальные файлы альбома после каждого пришедшего


def rm_files(paths):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def dup_files(chat_id, uid, paths):
    """Те же файлы прислали повторно за 10 минут — не плодим дубли событий."""
    h = hashlib.md5()
    try:
        for p in paths:
            with open(p, "rb") as f:
                h.update(f.read())
    except OSError:
        return False
    digest = h.hexdigest()
    prev = state_get(f"lastfile_{uid}")
    if prev:
        try:
            pf = json.loads(prev)
            if (pf.get("hash") == digest
                    and (now() - datetime.fromisoformat(pf["ts"])).total_seconds() < 600):
                rm_files(paths)
                reply(chat_id, "👌 Это я уже обработал только что.")
                return True
        except (ValueError, KeyError):
            pass
    state_set(f"lastfile_{uid}", json.dumps({"hash": digest, "ts": now().isoformat()}))
    return False


def queue_media_group(chat_id, uid, mgid, path, caption):
    """Накопить файлы одного альбома и разобрать их вместе, когда поток прекратится."""
    key = (chat_id, mgid)
    with _MG_LOCK:
        g = _MEDIA_GROUPS.get(key)
        first = g is None
        if first:
            g = _MEDIA_GROUPS[key] = {"uid": uid, "files": [], "captions": [], "timer": None}
        g["files"].append(path)
        if caption:
            g["captions"].append(caption)
        if g["timer"]:
            g["timer"].cancel()
        g["timer"] = threading.Timer(MEDIA_GROUP_WAIT, flush_media_group, args=(chat_id, mgid))
        g["timer"].daemon = True
        g["timer"].start()
    if first:
        reply(chat_id, "📎 Вижу несколько файлов — дождусь остальные и разберу все разом…")


def flush_media_group(chat_id, mgid):
    """Таймер альбома сработал: файлов больше не приходит — разбираем пачку."""
    with _MG_LOCK:
        g = _MEDIA_GROUPS.pop((chat_id, mgid), None)
    if not g or not g["files"]:
        return
    paths, n = g["files"], len(g["files"])
    try:
        # DB_LOCK берём только вокруг работы с базой: распознавание нескольких файлов
        # занимает минуты, и держать на это время общий лок — значит подвесить бота
        with DB_LOCK:
            if dup_files(chat_id, g["uid"], paths):
                return
            print(f"[file] альбом {mgid}: {n} шт.", flush=True)
            reply(chat_id, f"📎 Изучаю файлы ({n} шт.) — это может занять минуту…")
        caption = " ".join(g["captions"]) or None
        datas = []
        with TypingLoop(chat_id):
            for i, p in enumerate(paths, 1):
                datas.append(analyze_file(p, caption))
                print(f"[file] {i}/{n} готов", flush=True)
        with DB_LOCK:
            process_files(chat_id, g["uid"], datas)
    except Exception:
        traceback.print_exc()
        reply(chat_id, "⚠️ Не получилось разобрать файлы. Попробуй прислать их ещё раз.")
    finally:
        rm_files(paths)


def already_there(chat_id, dup, intent):
    """Ответ на повтор: не добавляем, но даём кнопку — вдруг запись правда нужна дважды."""
    token = str(int(time.time() * 1000) % 10**9)
    state_set(f"force_{chat_id}", json.dumps({"token": token, "intent": intent},
                                             ensure_ascii=False))
    kb = json.dumps({"inline_keyboard": [[
        {"text": "➕ Всё равно добавить", "callback_data": f"force:yes:{token}"},
        {"text": "Не надо", "callback_data": f"force:no:{token}"}]]})
    return {"text": "👌 Это у меня уже есть — не дублирую:\n\n" + line(dup, show_kind=True),
            "markup": kb}


def is_forwarded(msg):
    return bool(msg.get("forward_origin") or msg.get("forward_date")
                or msg.get("forward_from") or msg.get("forward_from_chat"))


def handle_message(msg):
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    mid = msg.get("message_id")

    # доступ / привязка
    if not ensure_access(uid, chat_id, msg):
        return

    clean = user_clean(uid)
    text = (msg.get("text") or "").strip()
    voice = msg.get("voice") or msg.get("audio") or msg.get("video_note")
    voice_prefix = ""

    if not text and voice:
        typing(chat_id)
        fp = download_voice(voice["file_id"])
        if clean:
            delete_msg(chat_id, mid)
            mid = None
        if not fp:
            reply(chat_id, "🎤 Не смог скачать голосовое, попробуй ещё раз.")
            return
        try:
            text = transcribe(fp)
        except Exception:
            traceback.print_exc()
            reply(chat_id, "🎤 Не получилось распознать голос. Можно текстом?")
            return
        finally:
            try:
                os.remove(fp)
            except OSError:
                pass
        if not text:
            reply(chat_id, "🎤 Не расслышал. Повтори или напиши текстом.")
            return
        voice_prefix = f"🎤 <i>«{html_escape(text)}»</i>\n\n"

    # документ или фото (билет, приглашение, запись к врачу) — распознаём; файл НЕ удаляем
    doc = msg.get("document")
    photo = msg.get("photo")
    if not text and (doc or photo):
        typing(chat_id)
        caption = (msg.get("caption") or "").strip()
        if doc:
            file_id = doc["file_id"]
            suffix = os.path.splitext(doc.get("file_name", ""))[1].lower() or ".pdf"
        else:
            file_id = photo[-1]["file_id"]
            suffix = ".jpg"
        fp = download_tg_file(file_id, suffix)
        if not fp:
            reply(chat_id, "📎 Не смог скачать файл, попробуй ещё раз.")
            return
        mgid = msg.get("media_group_id")
        if mgid:
            # альбом: Telegram шлёт каждое фото ОТДЕЛЬНЫМ сообщением — копим и разбираем пачкой
            queue_media_group(chat_id, uid, mgid, fp, caption)
            return
        if dup_files(chat_id, uid, [fp]):
            return
        reply(chat_id, "📎 Изучаю файл, секунду…")
        with TypingLoop(chat_id):
            data = analyze_file(fp, caption)
        rm_files([fp])
        process_files(chat_id, uid, [data])
        return

    # подчистить сообщение пользователя (текст/команда), чтобы чат не рос
    if clean:
        delete_msg(chat_id, mid)

    if not text:
        return

    low = text.lower().strip()
    if text in ("/start", "/help"):
        reply(chat_id, HELP)
        return
    if low in ("/classic", "не удаляй сообщения", "обычный режим"):
        set_clean(uid, False)
        reply(chat_id, "🔓 Ок, больше не подчищаю чат — сообщения остаются. Вернуть: /clean")
        return
    if low in ("/clean", "/window", "чистый режим", "режим окна"):
        set_clean(uid, True)
        reply(chat_id, "🧹 Ок, держу чат чистым — лишние сообщения убираю.")
        return

    # ждём значение для правки кредита — перехватываем до всякого разбора
    pend = loans.pending(chat_id)
    if pend and not text.startswith("/"):
        val, err = loans.parse_value(pend["field"], text)
        if err:
            reply(chat_id, "⚠️ " + err)
            return
        loans.set_field(pend["id"], pend["field"], val)
        loans.clear_pending(chat_id)
        loan = loans.get_loan(pend["id"])
        edit_text(chat_id, pend["msg"], loans.loan_card(loan), loans.card_keyboard(loan))
        return

    if low.startswith(("/kredit", "/credit", "/dolgi", "/dolg")):
        reply(chat_id, *loan_command(text))
        return

    # быстрые пути без LLM: частые запросы — мгновенный ответ
    norm = re.sub(r"[^\wа-яё ]", "", low).strip()
    FAST = {
        "today": ("что сегодня", "сегодня", "план на сегодня", "что у нас сегодня",
                  "расписание на сегодня"),
        "tomorrow": ("что завтра", "завтра", "план на завтра", "что у нас завтра"),
        "week": ("неделя", "что на неделе", "что на этой неделе", "расписание",
                 "расписание на неделю", "план на неделю"),
    }
    for f, keys in FAST.items():
        if norm in keys:
            reply(chat_id, voice_prefix + render_list("event", f))
            return
    if norm in ("места", "список мест", "куда сходить", "куда сходим"):
        reply(chat_id, voice_prefix + render_list("place", None))
        return
    if norm in ("кредиты", "долги", "мои кредиты", "кредит", "долг", "сколько должны",
                "мои долги", "список долгов", "сколько должен", "сколько я должен"):
        loans.clear_pending(chat_id)
        reply(chat_id, voice_prefix + loans.loans_summary(), loans.list_keyboard())
        return

    # защита от дублей: то же сообщение сразу после add (не дождался ответа — повторил)
    prev = state_get(f"lastmsg_{uid}")
    if prev:
        try:
            p = json.loads(prev)
            if (p.get("norm") == norm and p.get("action") == "add"
                    and (now() - datetime.fromisoformat(p["ts"])).total_seconds() < 180):
                reply(chat_id, voice_prefix + "👌 Это я уже записал — сообщение повторилось.")
                return
        except (ValueError, KeyError):
            pass

    # пересланное чужое сообщение (напоминание из клиники, приглашение, бронь) — через черновик
    if is_forwarded(msg) and len(text) >= 25 and not draft_state(chat_id):
        with TypingLoop(chat_id):
            fw = analyze_forward(text)
        evs = [e for e in ((fw or {}).get("events") or [])
               if isinstance(e, dict) and e.get("when")]
        print(f"[forward] events={len(evs)} text={text[:60]!r}", flush=True)
        if evs:
            show_draft(chat_id, evs,
                       prefix=voice_prefix + "📨 <i>Разобрал пересланное сообщение.</i>\n\n",
                       head="<b>Похоже, это событие:</b>")
            return
        # события не нашлось — дальше как обычный текст

    # висит черновик с фото — сообщение может быть его правкой или подтверждением
    dobj = draft_state(chat_id)
    if dobj:
        n = now()
        with TypingLoop(chat_id):
            res = claude_json(DRAFT_PROMPT.format(
                draft=json.dumps(dobj["events"], ensure_ascii=False),
                now=f"{n.day:02d}.{n.month:02d}.{n.year} {n.strftime('%H:%M')}",
                message=text), CLAUDE_MODEL, 90)
        if res and res.get("_error"):
            reply(chat_id, voice_prefix + service_error_text(res, "разобрать правку")
                  + "\nЧерновик я держу.")
            return
        op = (res or {}).get("op")
        print(f"[draft] op={op} text={text!r}", flush=True)
        if op == "edit" and res.get("events"):
            show_draft(chat_id, res["events"], voice_prefix)
            return
        if op == "confirm":
            rows, warns, dups = confirm_draft(chat_id, uid)
            warn_txt = ("\n⚠️ " + "; ".join(warns)) if warns else ""
            if rows:
                body = block(rows)
                reply(chat_id, voice_prefix + "✅ Добавил:\n\n" + body + warn_txt,
                      reply_markup=del_keyboard(rows[-1]["id"]) if len(rows) == 1 else None)
                notify_partner(uid, "➕ {who} добавил(а)", body, rows[-1]["id"])
            elif dups:
                reply(chat_id, voice_prefix + f"👌 Это уже в календаре ({dups} шт.) — "
                      "ничего не добавляю.")
            else:
                reply(chat_id, voice_prefix + "В черновике не хватает даты — "
                      "напиши, например, «5 августа в 18»." + warn_txt)
            return
        if op == "cancel":
            state_set(f"draft_{chat_id}", "")
            reply(chat_id, voice_prefix + "Ок, черновик убрал.")
            return
        # op = other/None — обычная просьба, обрабатываем как всегда (черновик остаётся ждать)

    ref_id = reply_ref(msg)
    if ref_id:
        state_set(f"focus_{chat_id}", str(ref_id))

    print(f"[msg] uid={uid} ref={ref_id} text={text!r}", flush=True)
    with TypingLoop(chat_id):
        intent = parse_intent(text, chat_id, ref_id)
    if isinstance(intent, dict) and intent.get("_error"):
        reply(chat_id, voice_prefix + service_error_text(intent))
        return
    if not intent or not isinstance(intent, dict):
        reply(chat_id, voice_prefix + "🤔 Не понял. Попробуй иначе или /help.")
        return
    print(f"[intent] action={intent.get('action')} kind={intent.get('kind')} "
          f"filter={intent.get('filter')} target={intent.get('target')} "
          f"targets={intent.get('targets')} recur={intent.get('recur')} "
          f"when={intent.get('when')} title={intent.get('title')!r}", flush=True)

    action = intent.get("action")

    try:
        if action in ("batch", "multi"):
            ops = [o for o in (intent.get("ops") or [])
                   if isinstance(o, dict) and o.get("action") not in ("batch", "multi")]
            if not ops:
                reply(chat_id, voice_prefix + "🤔 Не понял, что нужно. /help — примеры.")
                return
            print(f"[batch] {len(ops)} ops: "
                  + "; ".join(f"{o.get('action')}→{o.get('target')}" for o in ops), flush=True)
            res = merge_results([apply_intent(chat_id, uid, o, text, norm) for o in ops])
        else:
            res = apply_intent(chat_id, uid, intent, text, norm)

        if res.get("act"):
            remember_act(chat_id, res["act"])
        reply(chat_id, voice_prefix + res["text"], reply_markup=res.get("markup"))
        if res.get("notify"):
            verb, body, focus = res["notify"]
            notify_partner(uid, verb, body, focus)
    except Exception:
        traceback.print_exc()
        reply(chat_id, "⚠️ Что-то пошло не так, записал в лог. Попробуй ещё раз.")


def merge_results(results):
    """Склеить результаты нескольких команд из одного сообщения в один ответ."""
    texts = [r["text"] for r in results if r.get("text")]
    marks = [r["markup"] for r in results if r.get("markup")]
    notes = [r["notify"] for r in results if r.get("notify")]
    acts = [r["act"] for r in results if r.get("act")]
    out = {"text": "\n\n".join(texts) or "Готово.",
           # кнопку оставляем, только если её просит ровно одна команда — иначе непонятно, к чему она
           "markup": marks[0] if len(marks) == 1 else None}
    if len(notes) == 1:
        out["notify"] = notes[0]
    elif notes:
        focus = next((n[2] for n in reversed(notes) if n[2]), None)
        out["notify"] = ("🔄 {who} поменял(а) расписание",
                         "\n\n".join(n[1] for n in notes), focus)
    if len(acts) == 1:
        out["act"] = acts[0]
    elif acts:
        out["act"] = {"action": "batch", "acts": acts}
    return out


def apply_intent(chat_id, uid, intent, text, norm):
    """Выполнить ОДНУ команду. Возвращает {"text", "markup", "notify", "act"} — сам ничего
    не отвечает и не запоминает, чтобы на несколько команд из одного сообщения
    (action "batch") вышел один общий ответ, одно уведомление напарнику и одна «отмена»."""
    action = intent.get("action")

    if action == "add":
        kind = intent.get("kind") or "event"
        when_iso, _ht = norm_when(intent.get("when"))
        if kind != "place" and not when_iso:
            return {"text": "📅 Я сейчас веду только <b>календарь</b> (события с датой) и "
                            "<b>места куда сходить</b>. Списки дел вернём позже.\n\n"
                            "Если это событие — добавь дату/время, например: «в пятницу в 18 …»."}
        # повтор не заводим вовсе: раньше бот добавлял и лишь потом писал «похоже на дубль»,
        # и в календаре оседали задвоенные записи (найдено в базе 08.08.2026)
        dup = (find_dup_place(intent.get("title")) if kind == "place"
               else find_dup((intent.get("title") or "").strip(), when_iso,
                             who=intent.get("who")))
        if dup:
            return already_there(chat_id, dup, intent)
        item_id, kind, when_iso, has_time = add_item(intent, uid)
        row = db().execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        state_set(f"focus_{chat_id}", str(item_id))
        state_set(f"lastmsg_{uid}", json.dumps(
            {"norm": norm, "action": "add", "ts": now().isoformat()}))
        txt = f"✅ {intent.get('reply') or 'Записал.'}\n\n{line(row, show_kind=True)}"
        if dup:
            txt += "\n⚠️ Похоже на дубль — если лишнее, скажи «отмени»."
        return {"text": txt, "markup": del_keyboard(item_id),
                "act": {"action": "add", "id": item_id},
                "notify": ("➕ {who} добавил(а)", line(row, show_kind=True), item_id)}

    if action == "add_multi":
        evs = [e for e in (intent.get("events") or []) if isinstance(e, dict)]
        rows = []
        dups = 0
        for ev in evs:
            w_iso, _wh = norm_when(ev.get("when"))
            if not w_iso and ev.get("kind") != "place":
                continue
            if find_dup((ev.get("title") or "").strip(), w_iso, who=ev.get("who")):
                dups += 1
                continue
            iid, _k, _w, _h = add_item(ev, uid)
            rows.append(db().execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone())
        if not rows:
            if dups:
                return {"text": f"👌 Всё это уже в календаре ({dups} шт.) — не дублирую."}
            return {"text": "📅 Не понял даты событий — добавь их, "
                            "например: «вт и пт в 10 тренировка»."}
        state_set(f"focus_{chat_id}", str(rows[-1]["id"]))
        state_set(f"lastmsg_{uid}", json.dumps(
            {"norm": norm, "action": "add", "ts": now().isoformat()}))
        body = block(rows, show_kind=True)
        txt = f"✅ {intent.get('reply') or 'Записал.'}\n\n" + body
        if dups:
            txt += f"\n👌 Ещё {dups} уже были в календаре — не дублирую."
        skipped = len(evs) - len(rows) - dups
        if skipped:
            txt += (f"\n⚠️ Ещё {skipped} не добавил — не разобрал дату. "
                    "Добавь их отдельными сообщениями, пожалуйста.")
        return {"text": txt, "act": {"action": "bulk_add", "ids": [r["id"] for r in rows]},
                "notify": ("➕ {who} добавил(а)", body, rows[-1]["id"])}

    if action == "list":
        return {"text": render_list(intent.get("kind"), intent.get("filter"))}

    if action == "find":
        matches = search_events(intent.get("target") or text)
        if not matches:
            return {"text": "🔎 Не нашёл похожих событий. Напиши «расписание» — покажу всё."}
        state_set(f"focus_{chat_id}", str(matches[0]["id"]))
        return {"text": "🔎 Вот что нашёл:\n\n" + block(matches)}

    if action in ("delete", "update"):
        row, candidates = find_target(intent.get("target"), intent.get("kind"))
        if row is None and candidates:
            # номеров в карточках больше нет — просим уточнить датой, она видна в списке
            return {"text": "Уточни, какое именно — назови дату:\n\n" + block(candidates)}
        if row is None:
            return {"text": "Не нашёл такую запись. Глянь расписание и назови её точнее "
                            "или ответь (reply) на карточку."}
        state_set(f"focus_{chat_id}", str(row["id"]))
        if action == "delete":
            snapshot = dict(row)
            delete_item(row["id"])
            return {"text": f"🗑 Удалил: {html_escape(row['title'])}\n"
                            "<i>Если ошибся — напиши «отмени».</i>",
                    "act": {"action": "delete", "before": snapshot},
                    "notify": ("🗑 {who} удалил(а)", line(row), None)}
        snapshot = dict(row)
        changed, warns = update_item(row, intent)
        warn_txt = ("\n⚠️ " + "; ".join(warns)) if warns else ""
        if changed:
            fresh = db().execute("SELECT * FROM items WHERE id=?", (row["id"],)).fetchone()
            return {"text": f"✏️ {intent.get('reply') or 'Изменил.'}\n\n"
                            f"{line(fresh, show_kind=True)}" + warn_txt,
                    "markup": del_keyboard(fresh["id"]),
                    "act": {"action": "update", "before": snapshot},
                    "notify": ("✏️ {who} изменил(а)", line(fresh, show_kind=True), fresh["id"])}
        if warns:
            return {"text": "⚠️ " + "; ".join(warns)
                            + ". Попробуй сказать дату иначе, напр. «перенеси йогу на пятницу 16:00»."}
        return {"text": "Не понял, что поменять. Напр.: «перенеси йогу на пятницу 16:00»."}

    if action == "bulk_delete":
        tgs = intent.get("targets")
        if isinstance(tgs, str):
            tgs = re.findall(r"\d+", tgs)
        ids = []
        for t in (tgs or []):
            try:
                ids.append(int(str(t).lstrip("#")))
            except (ValueError, TypeError):
                pass
        rows = [db().execute("SELECT * FROM items WHERE id=?", (i,)).fetchone() for i in ids]
        rows = [r for r in rows if r]
        if not rows:
            return {"text": "Не понял, что именно удалить. Назови точнее."}
        token = str(int(time.time() * 1000) % 10**9)
        state_set(f"bulkdel_{chat_id}", json.dumps(
            {"token": token, "ids": [r["id"] for r in rows]}))
        kb = json.dumps({"inline_keyboard": [[
            {"text": f"✅ Удалить ({len(rows)})", "callback_data": f"bulk:yes:{token}"},
            {"text": "Отмена", "callback_data": f"bulk:no:{token}"}]]})
        return {"text": "🗑 Удалить эти записи?\n\n" + block(rows),
                "markup": kb}

    if action == "undo":
        la = state_get(f"lastact_{chat_id}")
        if not la:
            return {"text": "↩️ Нечего отменять — недавних действий не помню."}
        state_set(f"lastact_{chat_id}", "")
        txt, notify = undo_act(json.loads(la))
        return {"text": txt, "notify": notify}

    if action == "help":
        return {"text": HELP}
    return {"text": "🤔 Не понял, что нужно. /help — примеры."}


def undo_act(d):
    """Откатить одно запомненное действие. -> (текст ответа, notify | None)."""
    a = d.get("action")
    if a == "batch":
        texts, bodies = [], []
        for sub in reversed(d.get("acts", [])):   # разворачиваем в обратном порядке
            t, nt = undo_act(sub)
            texts.append(t)
            if nt:
                bodies.append(nt[1])
        notify = ("↩️ {who} отменил(а) последнее", "\n\n".join(bodies), None) if bodies else None
        return "\n\n".join(texts), notify
    if a == "add":
        row = db().execute("SELECT * FROM items WHERE id=?", (d["id"],)).fetchone()
        if not row:
            return "↩️ Той записи уже нет.", None
        delete_item(row["id"])
        return (f"↩️ Отменил — удалил «{html_escape(row['title'])}». "
                "Скажи, как правильно, — запишу заново.",
                ("↩️ {who} отменил(а) добавление", line(row), None))
    if a == "bulk_add":
        gone = []
        for iid in d.get("ids", []):
            row = db().execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
            if row:
                delete_item(iid)
                gone.append(row)
        if not gone:
            return "↩️ Тех записей уже нет.", None
        body = block(gone)
        return ("↩️ Отменил — удалил:\n\n" + body,
                ("↩️ {who} отменил(а) добавление", body, None))
    if a == "update":
        reinsert(d["before"])
        fresh = db().execute("SELECT * FROM items WHERE id=?", (d["before"]["id"],)).fetchone()
        return ("↩️ Вернул как было:\n\n" + line(fresh, show_kind=True),
                ("↩️ {who} отменил(а) изменение", line(fresh), None))
    # delete / bulk_delete
    snaps = d["before"] if a == "bulk_delete" else [d["before"]]
    for s in snaps:
        reinsert(s)
    back = [db().execute("SELECT * FROM items WHERE id=?", (s["id"],)).fetchone() for s in snaps]
    body = block(back)
    return "↩️ Вернул:\n\n" + body, ("↩️ {who} вернул(а) удалённое", body, None)


def loan_command(text):
    """/dolgi или /kredit … -> (текст, клавиатура). Для тех, кому быстрее текстом."""
    parts = text.split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    low = arg.lower()

    if not arg or low in ("список", "все"):
        return loans.loans_summary(), loans.list_keyboard()

    if low.startswith(("добавить", "новый")):
        parsed, err = loans.parse_add(arg.split(maxsplit=1)[1] if " " in arg else "")
        if err:
            return "⚠️ " + err, None
        name, principal, rate, payment = parsed
        loans.add_loan(name, principal, rate, payment)
        return loans.loans_summary(), loans.list_keyboard()

    # «/kredit 2 ставка 22,9» — правка одной строкой
    m = re.match(r"(\d+)\s*(.*)", arg)
    if not m:
        return ("⚠️ Не понял. <code>/dolgi</code> — список, "
                "<code>/dolgi 2 ставка 22,9</code> — правка."), None
    loan = loans.get_loan(int(m.group(1)))
    if not loan:
        return "⚠️ Такого долга нет.", loans.list_keyboard()
    rest = m.group(2).strip()
    if not rest:
        return loans.loan_card(loan), loans.card_keyboard(loan)

    ALIAS = {"ставка": "rate", "процент": "rate", "проценты": "rate",
             "остаток": "balance", "долг": "balance", "сумма": "balance",
             "платёж": "payment", "платеж": "payment",
             "день": "day", "число": "day",
             "банк": "bank", "название": "name", "имя": "name"}
    w = rest.split(maxsplit=1)
    field = ALIAS.get(w[0].lower())
    if not field:
        return ("⚠️ Поля: ставка, остаток, платёж, день, банк, название.\n"
                "Напр. <code>/dolgi " + str(loan["id"]) + " ставка 22,9</code>"), None
    if len(w) < 2:
        return "⚠️ Не хватает значения. " + loans.FIELDS[field][1], None
    val, err = loans.parse_value(field, w[1])
    if err:
        return "⚠️ " + err, None
    loans.set_field(loan["id"], field, val)
    return loans.loan_card(loans.get_loan(loan["id"])), loans.card_keyboard(loans.get_loan(loan["id"]))


def handle_callback(cb):
    data = cb.get("data", "")
    chat_id = cb["message"]["chat"]["id"]
    uid = cb["from"]["id"]
    if uid not in all_user_ids():
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Нет доступа")
        return
    if ":" not in data:
        return
    act, arg = data.split(":", 1)

    if act == "loan":
        sub = arg.split(":")
        cmd = sub[0]
        mid = cb["message"]["message_id"]
        if cmd == "list":
            loans.clear_pending(chat_id)
            edit_text(chat_id, mid, loans.loans_summary(), loans.list_keyboard())
            tg("answerCallbackQuery", callback_query_id=cb["id"])
            return
        loan = loans.get_loan(int(sub[1])) if len(sub) > 1 and sub[1].isdigit() else None
        if not loan:
            tg("answerCallbackQuery", callback_query_id=cb["id"], text="Кредита уже нет")
            return
        if cmd == "show":
            loans.clear_pending(chat_id)
        elif cmd == "close":
            loans.set_closed(loan["id"], True)
            loan = loans.get_loan(loan["id"])
        elif cmd == "open":
            loans.set_closed(loan["id"], False)
            loan = loans.get_loan(loan["id"])
        elif cmd == "cancel":
            loans.clear_pending(chat_id)
        elif cmd == "set" and len(sub) > 2 and sub[2] in loans.FIELDS:
            field = sub[2]
            loans.set_pending(chat_id, {"id": loan["id"], "field": field, "msg": mid})
            title, hint = loans.FIELDS[field]
            edit_text(chat_id, mid,
                      loans.loan_card(loan) + f"\n\n✏️ Пришли новое значение: <b>{title}</b>\n{hint}",
                      json.dumps({"inline_keyboard": [[
                          {"text": "✖️ Отмена", "callback_data": f"loan:cancel:{loan['id']}"}]]}))
            tg("answerCallbackQuery", callback_query_id=cb["id"], text="Жду значение")
            return
        edit_text(chat_id, mid, loans.loan_card(loan), loans.card_keyboard(loan))
        tg("answerCallbackQuery", callback_query_id=cb["id"])
        return

    if act == "draft":
        sub = arg.split(":")
        cmd, tok = sub[0], (sub[1] if len(sub) > 1 else None)
        d = draft_state(chat_id)
        if d and d.get("token") and tok and d["token"] != tok:
            tg("answerCallbackQuery", callback_query_id=cb["id"],
               text="Эта карточка устарела — работаем с новым черновиком")
            return
        if cmd == "add":
            rows, warns, dups = confirm_draft(chat_id, uid)
            warn_txt = ("\n⚠️ " + "; ".join(warns)) if warns else ""
            if rows is None:
                tg("answerCallbackQuery", callback_query_id=cb["id"], text="Черновика уже нет")
                return
            if not rows:
                if dups:
                    tg("answerCallbackQuery", callback_query_id=cb["id"], text="Уже есть")
                    edit_text(chat_id, cb["message"]["message_id"],
                              f"👌 Это уже в календаре ({dups} шт.) — ничего не добавляю.")
                    return
                tg("answerCallbackQuery", callback_query_id=cb["id"], text="Не хватает даты")
                send(chat_id, "В черновике не хватает даты — напиши, например, «5 августа в 18».")
                return
            body = block(rows)
            tg("answerCallbackQuery", callback_query_id=cb["id"], text="Добавлено ✅")
            edit_text(chat_id, cb["message"]["message_id"],
                      "✅ Добавил в календарь:\n\n" + body + warn_txt)
            notify_partner(uid, "➕ {who} добавил(а)", body, rows[-1]["id"])
        else:
            state_set(f"draft_{chat_id}", "")
            tg("answerCallbackQuery", callback_query_id=cb["id"], text="Отменено")
            edit_text(chat_id, cb["message"]["message_id"], "Ок, ничего не добавляю.")
        return

    if act == "force":
        sub = arg.split(":")
        cmd, tok = sub[0], (sub[1] if len(sub) > 1 else None)
        raw = state_get(f"force_{chat_id}")
        pend = None
        if raw:
            try:
                pend = json.loads(raw)
            except ValueError:
                pend = None
        if not pend or (pend.get("token") and tok and pend["token"] != tok):
            tg("answerCallbackQuery", callback_query_id=cb["id"], text="Карточка устарела")
            return
        state_set(f"force_{chat_id}", "")
        if cmd != "yes":
            tg("answerCallbackQuery", callback_query_id=cb["id"], text="Ок, не добавляю")
            edit_text(chat_id, cb["message"]["message_id"], "Ок, ничего не добавляю.")
            return
        item_id, _k, _w, _h = add_item(pend["intent"], uid)
        row = db().execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        remember_act(chat_id, {"action": "add", "id": item_id})
        state_set(f"focus_{chat_id}", str(item_id))
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Добавлено ✅")
        edit_text(chat_id, cb["message"]["message_id"],
                  "✅ Добавил второй записью:\n\n" + line(row, show_kind=True))
        notify_partner(uid, "➕ {who} добавил(а)", line(row, show_kind=True), item_id)
        return

    if act == "bulk":
        sub = arg.split(":")
        cmd, tok = sub[0], (sub[1] if len(sub) > 1 else None)
        raw = state_get(f"bulkdel_{chat_id}")
        pend = None
        if raw:
            try:
                pend = json.loads(raw)
                if isinstance(pend, list):  # старый формат
                    pend = {"token": None, "ids": pend}
            except ValueError:
                pend = None
        if pend and pend.get("token") and tok and pend["token"] != tok:
            tg("answerCallbackQuery", callback_query_id=cb["id"],
               text="Эта карточка устарела — запроси удаление заново")
            return
        state_set(f"bulkdel_{chat_id}", "")
        if cmd != "yes" or not pend:
            tg("answerCallbackQuery", callback_query_id=cb["id"], text="Отменено")
            edit_text(chat_id, cb["message"]["message_id"], "Ок, ничего не удаляю.")
            return
        ids = pend["ids"]
        rows = [db().execute("SELECT * FROM items WHERE id=?", (i,)).fetchone() for i in ids]
        rows = [r for r in rows if r]
        snaps = [dict(r) for r in rows]
        for r in rows:
            delete_item(r["id"])
        remember_act(chat_id, {"action": "bulk_delete", "before": snaps})
        tg("answerCallbackQuery", callback_query_id=cb["id"], text=f"Удалено: {len(rows)}")
        edit_text(chat_id, cb["message"]["message_id"],
                  f"🗑 Удалил записей: {len(rows)}. Вернуть всё — напиши «отмени».")
        notify_partner(uid, "🗑 {who} удалил(а) записи",
                       "\n".join("• " + html_escape(s["title"]) for s in snaps))
        return

    row = db().execute("SELECT * FROM items WHERE id=?", (int(arg),)).fetchone()
    if not row:
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Уже нет")
        return
    if act == "del":
        remember_act(chat_id, {"action": "delete", "before": dict(row)})
        delete_item(row["id"])
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Удалено 🗑")
        edit_text(chat_id, cb["message"]["message_id"],
                  f"🗑 <s>{html_escape(row['title'])}</s>")
        notify_partner(uid, "🗑 {who} удалил(а)", line(row))
