#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Понимание сообщений и файлов через claude: текст -> интент, файл -> билет/событие."""

from .. import config
from ..timeutil import now
from .claude import claude_json
from .context import context_block
from .prompts import PROMPT, FILE_PROMPT

_WEEKDAY = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def parse_intent(text, chat_id, ref_id=None):
    n = now()
    prompt = PROMPT.format(
        now_human=f"{n.day:02d}.{n.month:02d}.{n.year}, {_WEEKDAY[n.weekday()]}",
        today=n.strftime("%Y-%m-%d"),
        time_now=n.strftime("%H:%M"),
        context=context_block(chat_id, ref_id),
        message=text,
    )
    return claude_json(prompt, config.CLAUDE_MODEL, 90)


def analyze_file(path, caption=None):
    n = now()
    hint = f"\nПодпись пользователя к файлу: «{caption}» — учти её." if caption else ""
    prompt = FILE_PROMPT.format(path=path, now=n.strftime("%Y-%m-%d %H:%M"), caption_hint=hint)
    return claude_json(prompt, config.TICKET_MODEL, 180, think=True)
