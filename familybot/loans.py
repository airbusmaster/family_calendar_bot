#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Кредиты: хранение, аннуитетный пересчёт остатка и ручная правка.

Остаток не хранится «как есть», а считается: в базе лежит опорная пара
(principal, anchor_date) — сколько было должны на конкретную дату — плюс ставка
и платёж. Остаток на сегодня получается прокруткой графика от платежа к платежу:
    проценты = остаток * ставка * дней_между_платежами / 365
    остаток  = остаток + проценты - платёж
День-в-день, а не ставка/12: именно так считают банки, и на дорогих кредитах
разница принципиальна. У закрытой уже Альфы платёж был 52 900 против процентов
52 371 — тело гасилось по 529 ₽ в месяц, и приближение «/12» переворачивает
знак этой разницы.

Ступенчатая ставка (rate2 с даты rate2_from) поддерживается: у того же кредита
Альфы 49,35% до 17.07.2028 и 5,31% после, без этого срок и переплата врут в разы.

Так цифра не устаревает сама по себе, а любая ручная правка остатка просто
переставляет опорную точку на сегодня.

Кредитки здесь сознательно не ведутся: у них плавающий минимальный платёж
и льготный период, аннуитет их не описывает.
"""

import json
import math
import sqlite3
from calendar import monthrange
from datetime import date, datetime

from .db import db, state_get, state_set
from .items.render import html_escape
from .timeutil import now

FIELDS = {
    "rate":    ("ставку, % годовых", "напр. <code>22,9</code>"),
    "balance": ("остаток долга, ₽", "напр. <code>1 914 738</code>"),
    "payment": ("ежемесячный платёж, ₽", "напр. <code>63 000</code>"),
    "day":     ("день списания", "число от 1 до 31"),
    "bank":    ("банк", "напр. <code>Альфа-Банк</code>"),
    "name":    ("название", "напр. <code>Потребкредит</code>"),
}

# Стартовое наполнение при первом запуске. Личные цифры в код не зашиваем —
# кредиты заводятся командой /dolgi добавить (или /kredit добавить) и живут в БД.
# Кредитки (Халва, рассрочки) сюда не берём: плавающий минимальный платёж и льготный
# период — аннуитет их просто не описывает.
# (name, bank, principal, anchor, rate, rate2, rate2_from, payment, pay_day, known, closed, note)
SEED = []


# ------------------------------------------------------------------ схема
def init_loans():
    c = db()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS loans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            bank TEXT,
            principal REAL NOT NULL,
            rate REAL NOT NULL,
            rate2 REAL,
            rate2_from TEXT,
            payment REAL NOT NULL,
            pay_day INTEGER,
            anchor_date TEXT NOT NULL,
            rate_known INTEGER DEFAULT 0,
            closed INTEGER DEFAULT 0,
            note TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        """
    )
    c.commit()
    # ступенчатая ставка добавлена позже — ALTER молча пропускаем, как в db.init_db
    for ddl in ("ALTER TABLE loans ADD COLUMN rate2 REAL",
                "ALTER TABLE loans ADD COLUMN rate2_from TEXT"):
        try:
            c.execute(ddl)
            c.commit()
        except sqlite3.OperationalError:
            pass
    if state_get("loans_seeded"):
        return
    ts = now().isoformat(timespec="seconds")
    for (name, bank, principal, anchor, rate, rate2, rate2_from,
         payment, pay_day, known, closed, note) in SEED:
        c.execute("INSERT INTO loans(name,bank,principal,rate,rate2,rate2_from,payment,"
                  "pay_day,anchor_date,rate_known,closed,note,created_at,updated_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (name, bank, principal, rate, rate2, rate2_from, payment,
                   pay_day, anchor, known, closed, note, ts, ts))
    c.commit()
    state_set("loans_seeded", "1")


# ------------------------------------------------------------------ аннуитет
def _clamp_day(y, m, day):
    return date(y, m, min(day, monthrange(y, m)[1]))


def _next_month(d, day):
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return _clamp_day(y, m, day)


def _pay_dates(anchor, pay_day, until):
    """Даты списаний в интервале (anchor, until]."""
    d = _clamp_day(anchor.year, anchor.month, pay_day)
    if d <= anchor:
        d = _next_month(d, pay_day)
    out = []
    while d <= until and len(out) < 600:
        out.append(d)
        d = _next_month(d, pay_day)
    return out


def rate_on(loan, d):
    """Ставка, действующая на дату платежа: у ступенчатых кредитов их две."""
    try:
        if loan["rate2"] is not None and loan["rate2_from"] \
                and d > date.fromisoformat(loan["rate2_from"]):
            return float(loan["rate2"])
    except (IndexError, KeyError, TypeError, ValueError):
        pass
    return float(loan["rate"])


def _roll(loan, bal, start, until=None, limit=720):
    """Прокрутка графика платежей. until=None — до полного погашения.

    Проценты считаются по фактическим дням (days/365), как в банковском графике,
    а не как ставка/12: на кредите под 49% это разница между «тело гасится» и
    «долг растёт». -> (остаток, платежей, уплаченные проценты)."""
    pay = float(loan["payment"])
    prev, n, paid = start, 0, 0.0
    d = _clamp_day(start.year, start.month, loan["pay_day"] or start.day)
    if d <= start:
        d = _next_month(d, loan["pay_day"] or start.day)
    while n < limit:
        if until and d > until:
            break
        acc = bal * rate_on(loan, d) * (d - prev).days / 365
        if until is None and bal + acc <= pay:
            return 0.0, n + 1, paid + acc
        bal, paid, prev, n = bal + acc - pay, paid + acc, d, n + 1
        d = _next_month(d, loan["pay_day"] or start.day)
    return bal, n, paid


def balance_now(loan, on=None):
    """Расчётный остаток на дату: прокрутка графика от опорной точки."""
    if loan["closed"]:
        return 0.0
    on = on or now().date()
    anchor = date.fromisoformat(loan["anchor_date"])
    if on <= anchor:
        return float(loan["principal"])
    bal, _, _ = _roll(loan, float(loan["principal"]), anchor, until=on)
    return max(0.0, bal)


def forecast(loan, bal=None):
    """(месяцев до закрытия, будущая переплата). inf — платёж не гасит тело."""
    bal = balance_now(loan) if bal is None else bal
    if bal <= 0:
        return 0, 0.0
    left, n, paid = _roll(loan, bal, now().date())
    if left > 0:
        return math.inf, math.inf
    return n, paid


def payoff_date(loan, n=None):
    n = forecast(loan)[0] if n is None else n
    if not n or n == math.inf:
        return None
    d = now().date()
    total = d.month - 1 + int(n)
    return _clamp_day(d.year + total // 12, total % 12 + 1, loan["pay_day"] or d.day)


# ------------------------------------------------------------------ доступ
def all_loans(with_closed=False):
    q = "SELECT * FROM loans"
    if not with_closed:
        q += " WHERE closed=0"
    return db().execute(q + " ORDER BY closed, id").fetchall()


def get_loan(lid):
    return db().execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone()


def set_field(lid, field, value):
    """Ручная правка одного поля. Правка остатка переставляет опорную дату на сегодня,
    иначе пересчёт снова прокрутил бы уже учтённые платежи."""
    c = db()
    ts = now().isoformat(timespec="seconds")
    today = now().date().isoformat()
    if field == "balance":
        c.execute("UPDATE loans SET principal=?, anchor_date=?, updated_at=? WHERE id=?",
                  (value, today, ts, lid))
    elif field == "rate":
        # ставку меняем вместе с фиксацией текущего остатка: до этой минуты он
        # считался по старой ставке, и пересчитывать задним числом неверно
        cur = balance_now(get_loan(lid))
        c.execute("UPDATE loans SET rate=?, rate_known=1, principal=?, anchor_date=?, "
                  "updated_at=? WHERE id=?", (value, cur, today, ts, lid))
    elif field == "payment":
        cur = balance_now(get_loan(lid))
        c.execute("UPDATE loans SET payment=?, principal=?, anchor_date=?, updated_at=? "
                  "WHERE id=?", (value, cur, today, ts, lid))
    elif field == "day":
        c.execute("UPDATE loans SET pay_day=?, updated_at=? WHERE id=?", (value, ts, lid))
    elif field in ("bank", "name"):
        c.execute(f"UPDATE loans SET {field}=?, updated_at=? WHERE id=?", (value, ts, lid))
    else:
        return False
    c.commit()
    return True


def set_closed(lid, closed):
    c = db()
    c.execute("UPDATE loans SET closed=?, updated_at=? WHERE id=?",
              (1 if closed else 0, now().isoformat(timespec="seconds"), lid))
    c.commit()


def add_loan(name, principal, rate, payment, bank="", pay_day=None):
    c = db()
    ts = now().isoformat(timespec="seconds")
    today = now().date()
    c.execute("INSERT INTO loans(name,bank,principal,rate,payment,pay_day,anchor_date,"
              "rate_known,closed,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,0,?,?)",
              (name, bank, principal, rate, payment, pay_day or today.day,
               today.isoformat(), ts, ts))
    c.commit()
    return c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


# ------------------------------------------------------------------ разбор введённого значения
def parse_value(field, raw):
    """-> (значение, None) либо (None, текст ошибки)."""
    s = (raw or "").strip()
    if field in ("bank", "name"):
        return (s[:60], None) if s else (None, "Пустая строка.")
    num = s.replace("%", "").replace("₽", "").replace(" ", "").replace(" ", "")
    num = num.replace(",", ".")
    try:
        v = float(num)
    except ValueError:
        return None, "Не понял число. " + FIELDS[field][1]
    if field == "rate":
        if v > 1:            # «22,9» — это проценты, «0.229» — уже доля
            v /= 100
        if not 0 < v < 1.5:
            return None, "Ставка вне разумного диапазона."
        return v, None
    if field == "day":
        v = int(v)
        return (v, None) if 1 <= v <= 31 else (None, "День должен быть от 1 до 31.")
    if v < 0:
        return None, "Сумма не может быть отрицательной."
    return v, None


# ------------------------------------------------------------------ вывод
def rub(x):
    if x == math.inf:
        return "∞"
    return f"{round(x):,}".replace(",", " ") + " ₽"


def pct(x):
    s = f"{x * 100:.2f}".rstrip("0").rstrip(".")
    return s.replace(".", ",") + "%"


def _months_word(n):
    n = int(n)
    if 11 <= n % 100 <= 14:
        return "месяцев"
    return {1: "месяц", 2: "месяца", 3: "месяца", 4: "месяца"}.get(n % 10, "месяцев")


def rate_line(loan):
    """Строка ставки. У ступенчатых показываем обе — иначе срок выглядит абсурдным."""
    txt = pct(float(loan["rate"]))
    try:
        if loan["rate2"] is not None and loan["rate2_from"]:
            d = date.fromisoformat(loan["rate2_from"])
            txt += f" → {pct(float(loan['rate2']))} с {d.strftime('%d.%m.%Y')}"
    except (IndexError, KeyError, TypeError, ValueError):
        pass
    return txt if loan["rate_known"] else txt + "  ⚠️ допущение"


def loan_card(loan):
    bal = balance_now(loan)
    head = f"💰 <b>{html_escape(loan['name'])}</b>"
    if loan["bank"]:
        head += f" · {html_escape(loan['bank'])}"
    if loan["closed"]:
        txt = head + "\n\n✅ Закрыт."
        if loan["note"]:
            txt += f"\n\n<i>{html_escape(loan['note'])}</i>"
        return txt
    n, over = forecast(loan, bal)
    rate = rate_on(loan, now().date())
    month_int = bal * rate * 30 / 365
    lines = [head, ""]
    lines.append(f"Остаток   <b>{rub(bal)}</b>")
    lines.append(f"Ставка    {rate_line(loan)}")
    lines.append(f"Платёж    {rub(loan['payment'])} · {loan['pay_day']} числа")
    lines.append(f"Проценты  {rub(month_int)} в месяц — "
                 f"{rub(max(0, float(loan['payment']) - month_int))} в тело")
    if n == math.inf:
        lines.append("\n⚠️ Платёж не покрывает проценты — долг растёт.")
    else:
        d = payoff_date(loan, n)
        lines.append(f"Осталось  {int(n)} {_months_word(n)}"
                     + (f", до {d.strftime('%m.%Y')}" if d else ""))
        lines.append(f"Переплата {rub(over)}")
    if loan["note"]:
        lines.append(f"\n<i>{html_escape(loan['note'])}</i>")
    anchor = date.fromisoformat(loan["anchor_date"])
    lines.append(f"<i>Остаток посчитан от {anchor.strftime('%d.%m.%Y')}.</i>")
    return "\n".join(lines)


def loans_summary():
    """Общий список долгов: активные сверху по цене денег, закрытые — строкой снизу."""
    rows = all_loans(with_closed=True)
    active = [r for r in rows if not r["closed"]]
    done = [r for r in rows if r["closed"]]
    if not rows:
        return "💰 Долгов нет. Добавить: <code>/dolgi добавить Название 500000 20 15000</code>"
    today = now().date()
    data = []
    for r in active:
        bal = balance_now(r)
        rate = rate_on(r, today)
        data.append((r, bal, rate, bal * rate * 30 / 365))
    # порядок — по цене денег: сверху то, что дороже всего обходится каждый месяц
    data.sort(key=lambda x: -x[3])
    total = sum(d[1] for d in data)
    pay = sum(float(d[0]["payment"]) for d in data)
    month_int = sum(d[3] for d in data)
    out = ["💰 <b>Долги</b>", ""]
    for r, bal, rate, mi in data:
        body = float(r["payment"]) - mi
        bank = f" · {html_escape(r['bank'])}" if r["bank"] else ""
        warn = "" if r["rate_known"] else " ?"
        out.append(f"<b>{html_escape(r['name'])}</b>{bank}\n"
                   f"   {rub(bal)} · {pct(rate)}{warn} · платёж {rub(r['payment'])}\n"
                   f"   в проценты {rub(mi)}, в тело {rub(body)}")
    out.append("")
    out.append(f"<b>Итого долг {rub(total)}</b>")
    out.append(f"Платежей {rub(pay)} в месяц, из них {rub(month_int)} — проценты")
    if done:
        out.append("\n✅ Закрыты: " + ", ".join(html_escape(r["name"]) for r in done))
    if any(not r[0]["rate_known"] for r in data):
        out.append("\n<i>? — ставка не подтверждена по договору, взята как допущение.</i>")
    out.append("<i>Кредитки здесь не ведутся — у них нет аннуитета.</i>")
    return "\n".join(out)


# ------------------------------------------------------------------ клавиатуры
def list_keyboard():
    rows = all_loans(with_closed=True)
    kb = [[{"text": ("✅ " if r["closed"] else "") + r["name"],
            "callback_data": f"loan:show:{r['id']}"}] for r in rows]
    kb.append([{"text": "🔄 Обновить", "callback_data": "loan:list"}])
    return json.dumps({"inline_keyboard": kb})


def card_keyboard(loan):
    lid = loan["id"]
    if loan["closed"]:
        return json.dumps({"inline_keyboard": [[
            {"text": "↩️ Вернуть в активные", "callback_data": f"loan:open:{lid}"},
            {"text": "← Назад", "callback_data": "loan:list"}]]})
    return json.dumps({"inline_keyboard": [
        [{"text": "✏️ Ставка", "callback_data": f"loan:set:{lid}:rate"},
         {"text": "✏️ Остаток", "callback_data": f"loan:set:{lid}:balance"}],
        [{"text": "✏️ Платёж", "callback_data": f"loan:set:{lid}:payment"},
         {"text": "✏️ День", "callback_data": f"loan:set:{lid}:day"}],
        [{"text": "🏦 Банк", "callback_data": f"loan:set:{lid}:bank"},
         {"text": "✅ Закрыт", "callback_data": f"loan:close:{lid}"}],
        [{"text": "← Назад", "callback_data": "loan:list"}],
    ]})


# ------------------------------------------------------------------ ожидание значения
def pending(chat_id):
    raw = state_get(f"loanedit_{chat_id}")
    return json.loads(raw) if raw else None


def set_pending(chat_id, data):
    state_set(f"loanedit_{chat_id}", json.dumps(data) if data else "")


def clear_pending(chat_id):
    state_set(f"loanedit_{chat_id}", "")


# ------------------------------------------------------------------ текстовые команды
def parse_add(text):
    """«/kredit добавить Альфа 500000 20 15000» -> (name, principal, rate, payment)."""
    parts = text.split()
    if len(parts) < 4:          # минимум: название + сумма + ставка + платёж
        return None, ("Формат: <code>/kredit добавить Название сумма ставка платёж</code>\n"
                      "напр. <code>/kredit добавить Альфа 500000 20 15000</code>")
    payment, rate, principal = parts[-1], parts[-2], parts[-3]
    name = " ".join(parts[:-3]).strip()
    if not name:
        return None, "Не хватает названия."
    vals = {}
    for field, raw in (("payment", payment), ("rate", rate), ("balance", principal)):
        v, err = parse_value(field, raw)
        if err:
            return None, err
        vals[field] = v
    return (name, vals["balance"], vals["rate"], vals["payment"]), None
