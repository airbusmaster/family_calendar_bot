# Архитектура

Бот — один процесс на long-polling (`getUpdates`) с несколькими фоновыми потоками.
Хранилище — один файл SQLite. Внешнего API-сервиса нет: разбор сообщений идёт через
локально установленный Claude CLI, распознавание голоса — через faster-whisper.

## Потоки

| Поток | Что делает |
|-------|-----------|
| главный (`__main__.main`) | принимает апдейты, под `DB_LOCK` обрабатывает каждый целиком |
| `scheduler.scheduler_loop` | утренняя сводка, напоминания, перекат повторяющихся событий |
| `calendar_sync.client.cal_worker` | очередь отправки записей в iCloud (PUT/DELETE) |
| `calendar_sync.reconcile.sync_loop` | раз в 5 минут сверяет БД и iCloud в обе стороны |

Каждый поток открывает **своё** соединение SQLite (`threading.local`), база работает в режиме
WAL с `busy_timeout` — параллельная запись безопасна. Обработка одного апдейта дополнительно
сериализуется `DB_LOCK`, чтобы составные команды видели согласованное состояние.

## Поток данных сообщения

```
Telegram getUpdates
      │
      ▼
handlers.handle_message
      │  голос → ai.voice.transcribe
      │  файл  → ai.parser.analyze_file → files.process_file (билет | черновик)
      │  текст → быстрые пути (regex) ─────────────► render_list
      │         └─► ai.parser.parse_intent (Claude → JSON-интент)
      ▼
handlers.apply_intent  (add / list / find / update / delete / bulk_delete / undo)
      │  batch → несколько apply_intent → merge_results
      ▼
items.repository (запись в БД) ──► calendar_sync.client.cal_push (очередь в iCloud)
      │
      ▼
items.render.line → telegram.chat.reply  +  ui.notify_partner
```

## Слои и зависимости

Зависимости направлены «вниз», циклов нет:

- `config`, `db`, `timeutil` — фундамент, ни от кого не зависят.
- `telegram/` — поверх `config`/`db`.
- `items/render` — форматирование, зависит только от фундамента.
- `calendar_sync/` — `client` не знает про `items`, `reconcile` использует `items.render`.
- `items/repository` — CRUD, зовёт `calendar_sync.client.cal_push`.
- `ai/` — Claude и голос, зависит от `config`/`db`/`timeutil`.
- `ui`, `files`, `access`, `handlers`, `scheduler` — прикладной слой поверх всего.

## Хранилище (SQLite)

- `items` — единая таблица записей: `kind` (`event`/`place`), дата/интервал (`when_dt`/`end_dt`),
  `recur`, категория, `who`, `note`, напоминание, поля синка (`ical_uid`/`ical_url`/`cal_synced`).
- `users` — привязанные пользователи (`MAX_USERS`), флаг `clean_mode`.
- `state` — key-value: фокус записи, черновики, токены кнопок, снапшоты для «отмени».

Схема наращивалась через `ALTER TABLE` в `db.init_db()` — миграции идемпотентны
(`OperationalError` при существующей колонке молча проглатывается).

## Синк с iCloud

Односторонняя отправка (`cal_worker`) пишет VEVENT напрямую по фиксированному URL
`…/family-bot-<id>.ics` — поиск по UID (REPORT) iCloud отклоняет (412). Обратная сторона
(`cal_reconcile`) тянет правки с телефона: чья запись новее (`LAST-MODIFIED` vs `updated_at`),
та и побеждает; созданные вручную в календаре «Семья» события импортируются, удалённые —
удаляются. Всё это включается только при заданных `ICLOUD_EMAIL`/`ICLOUD_APP_PASSWORD`.

## Понимание через Claude

`ai.claude.claude_json` запускает `claude -p … --output-format text` и вытаскивает JSON из
ответа. Для разбора текста extended thinking выключен (`MAX_THINKING_TOKENS=0`) — иначе haiku
жгла тысячи токенов «размышлений» и упиралась в таймаут; для чтения файлов (sonnet) thinking
оставлен ради точности. Промпту передаётся `context_block` — сводка открытых записей и запись
«в фокусе», чтобы модель понимала ссылки «то же самое», «перенеси его».
