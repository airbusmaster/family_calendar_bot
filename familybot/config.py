#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Конфигурация: переменные окружения и общие словари-константы.

Все секреты берутся только из окружения (см. .env.example) — в коде их нет.
"""

import os
from zoneinfo import ZoneInfo

# корень установки: рядом с ним лежат family.db и models/
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ------------------------------------------------------------------ секреты
TG_TOKEN = os.environ.get("TG_TOKEN", "")

# iCloud CalDAV: события зеркалятся в календарь iCloud (виден на айфонах).
# Включается, если заданы ICLOUD_EMAIL + ICLOUD_APP_PASSWORD (пароль приложения Apple ID).
ICLOUD_EMAIL = os.environ.get("ICLOUD_EMAIL")
ICLOUD_APP_PASSWORD = os.environ.get("ICLOUD_APP_PASSWORD")
ICLOUD_CALENDAR = os.environ.get("ICLOUD_CALENDAR", "Семья")

# ------------------------------------------------------------------ окружение
DB_PATH = os.environ.get("FAMILY_DB", os.path.join(BASE_DIR, "family.db"))
TZ = ZoneInfo(os.environ.get("BOT_TZ", "Europe/Moscow"))

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "haiku")
TICKET_MODEL = os.environ.get("TICKET_MODEL", "sonnet")  # для чтения билетов/файлов — точнее
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
MODELS_DIR = os.environ.get("WHISPER_DIR", os.path.join(BASE_DIR, "models"))

MAX_USERS = int(os.environ.get("MAX_USERS", "2"))
MORNING_HOUR = int(os.environ.get("MORNING_HOUR", "8"))  # утренняя сводка

# ------------------------------------------------------------------ словари
KIND_TITLE = {"event": "📅 Событие", "place": "📍 Место"}
CAT_TITLE = {"home": "🏠 дом", "child": "🧒 ребёнок", "couple": "❤️ вдвоём",
             "trip": "🧳 поездка", "general": "общее"}
TRIP_EMOJI = {"plane": "✈️", "train": "🚆", "bus": "🚌", "other": "🧳"}
RECUR_TITLE = {"daily": "каждый день", "weekly": "еженедельно",
               "biweekly": "раз в 2 недели", "monthly": "ежемесячно"}
RECUR_RRULE = {"daily": "FREQ=DAILY", "weekly": "FREQ=WEEKLY",
               "biweekly": "FREQ=WEEKLY;INTERVAL=2", "monthly": "FREQ=MONTHLY"}
