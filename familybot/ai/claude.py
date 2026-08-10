#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Запуск claude CLI с JSON-ответом и извлечение JSON из текста модели."""

import os
import re
import json
import subprocess


# «You've hit your weekly limit · resets Aug 12, 8pm (UTC)» и подобное
LIMIT_RE = re.compile(r"(hit your .*limit|usage limit|rate limit|лимит)", re.I)


def service_error_text(res, what="разобрать сообщение"):
    """Честный ответ пользователю, когда модель недоступна."""
    limit = (res or {}).get("_limit")
    if limit:
        safe = limit.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return ("🤖 Кончился лимит Claude по подписке — пока не смогу "
                f"{what}.\n<i>{safe}</i>\n\nВсё записанное на месте: расписание, "
                "напоминания и утренняя сводка работают. Спроси «что сегодня» — отвечу.")
    return ("🤖 Сервис распознавания сейчас недоступен — попробуй через минуту-другую. "
            "Записи в календаре целы.")


def extract_json(s):
    if not s:
        return None
    s = s.strip()
    s = re.sub(r"^```(json)?", "", s).strip()
    s = re.sub(r"```$", "", s).strip()
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def claude_json(prompt, model, timeout, think=False, tools=""):
    """Запуск claude -p с JSON-ответом.

    tools — какие инструменты доступны модели: "" (никаких) для разбора текста,
    "Read" для чтения присланного файла. Это защита от инъекции: в промпт попадает
    произвольный чужой текст (пересланное письмо, содержимое PDF), а рабочий каталог
    процесса — /opt/family-bot, где лежат env с токенами и база. Без ограничения
    инструментов подсунутая инструкция могла бы выполнить команду в системе.

    None — модель ответила, но JSON не разобрался (сообщение непонятно);
    {"_error": True} — сам сервис недоступен (упал CLI, таймаут, протух токен),
    чтобы пользователю честно сказать «сервис прилёг», а не «не понял тебя».

    think=False глушит extended thinking (MAX_THINKING_TOKENS=0). Без этого haiku на
    сложной фразе жгла ~9000 токенов «размышлений» и отвечала 85 с вместо 5 с —
    из-за чего разбор упирался в таймаут (баг 2026-07-23). Для чтения файлов
    (билеты/афиши) размышления оставляем — там важнее точность, а не скорость.
    """
    env = dict(os.environ)
    if not think:
        env["MAX_THINKING_TOKENS"] = "0"
    cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "text",
           "--tools", tools]
    if tools:
        cmd += ["--permission-mode", "bypassPermissions"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env,
            # без этого CLI три секунды ждёт данные на stdin, которых мы не шлём, —
            # ровно три секунды задержки на каждом сообщении (найдено 08.08.2026)
            stdin=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            # причину CLI пишет в stdout, а не в stderr: раньше в журнал попадало
            # голое «claude rc 1» без единого намёка, что случилось (08.08.2026)
            out, err = (proc.stdout or "").strip(), (proc.stderr or "").strip()
            print("claude rc", proc.returncode, "|", (out or err or "без вывода")[:300],
                  flush=True)
            if LIMIT_RE.search(out):
                return {"_error": True, "_limit": out[:200]}
            return {"_error": True}
        return extract_json(proc.stdout.strip())
    except subprocess.TimeoutExpired:
        print("claude timeout", flush=True)
        return {"_error": True}
    except Exception as e:
        print("claude error", e, flush=True)
        return {"_error": True}
