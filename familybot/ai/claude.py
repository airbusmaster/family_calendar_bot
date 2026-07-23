#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Запуск claude CLI с JSON-ответом и извлечение JSON из текста модели."""

import os
import re
import json
import subprocess


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


def claude_json(prompt, model, timeout, think=False):
    """Запуск claude -p с JSON-ответом.

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
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--model", model,
             "--permission-mode", "bypassPermissions", "--output-format", "text"],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        if proc.returncode != 0:
            print("claude rc", proc.returncode, proc.stderr[:300], flush=True)
            return {"_error": True}
        return extract_json(proc.stdout.strip())
    except subprocess.TimeoutExpired:
        print("claude timeout", flush=True)
        return {"_error": True}
    except Exception as e:
        print("claude error", e, flush=True)
        return {"_error": True}
