#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite: соединение, схема, миграции и key-value хранилище состояния."""

import sqlite3
import threading

from . import config

# сериализует обработку одного апдейта целиком (см. familybot/__main__.py)
DB_LOCK = threading.Lock()
_db_local = threading.local()


def db():
    """Соединение на поток: у бота 4 потока (обработчик, планировщик, синк, реконсайлер),
    один общий коннект SQLite между ними не потокобезопасен. WAL даёт спокойную
    параллельную запись."""
    if not hasattr(_db_local, "conn"):
        conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _db_local.conn = conn
    return _db_local.conn


def init_db():
    c = db()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            when_dt TEXT,
            has_time INTEGER DEFAULT 0,
            category TEXT,
            who TEXT,
            note TEXT,
            status TEXT DEFAULT 'open',
            remind_before_min INTEGER,
            remind_sent INTEGER DEFAULT 0,
            created_by INTEGER,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS users (
            tg_id INTEGER PRIMARY KEY,
            name TEXT,
            enrolled_at TEXT
        );
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    c.commit()
    # миграции: колонки добавлялись по мере роста бота, ALTER молча пропускаем
    for ddl in ("ALTER TABLE users ADD COLUMN clean_mode INTEGER DEFAULT 1",
                "ALTER TABLE items ADD COLUMN recur TEXT",
                "ALTER TABLE items ADD COLUMN end_dt TEXT",
                "ALTER TABLE items ADD COLUMN ical_uid TEXT",
                "ALTER TABLE items ADD COLUMN ical_url TEXT",
                "ALTER TABLE items ADD COLUMN cal_synced INTEGER DEFAULT 0"):
        try:
            c.execute(ddl)
            c.commit()
        except sqlite3.OperationalError:
            pass  # колонка уже есть


# ------------------------------------------------------------------ состояние (key-value)
def state_get(key):
    r = db().execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def state_set(key, value):
    c = db()
    c.execute("INSERT INTO state(key,value) VALUES(?,?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    c.commit()
