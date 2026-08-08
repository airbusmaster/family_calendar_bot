#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ночной бэкап базы бота.

Копировать family.db обычным cp НЕЛЬЗЯ: база работает в режиме WAL, и свежие записи
лежат в family.db-wal, которого копия не захватит. Проверено 08.08.2026 — копия одного
файла содержала данные двухнедельной давности (24 записи вместо 40). Поэтому только
sqlite3 .backup: он снимает согласованный снимок вместе с WAL.

Запуск из крона пользователя content:
    10 4 * * * /opt/family-bot/.venv/bin/python /opt/family-bot/deploy/backup.py
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta

DB = os.environ.get("FAMILY_DB", "/opt/family-bot/family.db")
DEST = os.environ.get("FAMILY_BACKUP_DIR", "/opt/family-bot/backup")
KEEP_DAYS = int(os.environ.get("FAMILY_BACKUP_KEEP", "14"))


def main():
    if not os.path.exists(DB):
        print(f"backup: нет базы {DB}", file=sys.stderr)
        return 1
    os.makedirs(DEST, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(DEST, f"family-{stamp}.db")

    src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    dst = sqlite3.connect(path)
    with dst:
        src.backup(dst)
    n = dst.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    dst.close()
    src.close()

    edge = datetime.now() - timedelta(days=KEEP_DAYS)
    dropped = 0
    for name in os.listdir(DEST):
        if not (name.startswith("family-") and name.endswith(".db")):
            continue
        full = os.path.join(DEST, name)
        if datetime.fromtimestamp(os.path.getmtime(full)) < edge:
            os.remove(full)
            dropped += 1

    size = os.path.getsize(path)
    print(f"backup: {path} — {n} записей, {size} байт; удалено старых: {dropped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
