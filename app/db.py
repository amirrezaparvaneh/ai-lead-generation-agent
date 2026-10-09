import os
import sqlite3
from collections import Counter
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", "data/app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, pw TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
    token TEXT PRIMARY KEY, user_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS products(
    user_id INTEGER PRIMARY KEY, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS results(
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, text TEXT NOT NULL,
    sender_name TEXT, sender_username TEXT, sender_id TEXT, message_link TEXT,
    telegram_update_id TEXT,
    stage TEXT NOT NULL, is_lead INTEGER NOT NULL, score INTEGER, reason TEXT,
    analysis TEXT, draft TEXT, tokens INTEGER NOT NULL, cost REAL NOT NULL,
    feedback INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS wallets(
    user_id INTEGER PRIMARY KEY, message_credit INTEGER NOT NULL DEFAULT 0,
    rial_credit INTEGER NOT NULL DEFAULT 0, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS reports(
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('bug', 'suggestion')),
    title TEXT NOT NULL, description TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    admin_note TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP, reviewed_at TEXT);
CREATE TABLE IF NOT EXISTS wallet_transactions(
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, report_id INTEGER UNIQUE,
    message_credit INTEGER NOT NULL DEFAULT 0, rial_credit INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS admin_sessions(
    token TEXT PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""


@contextmanager
def conn():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        columns = {row['name'] for row in c.execute('PRAGMA table_info(users)')}
        if 'phone' not in columns:
            c.execute('ALTER TABLE users ADD COLUMN phone TEXT')
        if 'first_name' not in columns:
            c.execute('ALTER TABLE users ADD COLUMN first_name TEXT')
        if 'last_name' not in columns:
            c.execute('ALTER TABLE users ADD COLUMN last_name TEXT')
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS users_phone ON users(phone)')
        result_columns = {row['name'] for row in c.execute('PRAGMA table_info(results)')}
        for column in ('sender_name', 'sender_username', 'sender_id', 'message_link', 'telegram_update_id'):
            if column not in result_columns:
                c.execute(f'ALTER TABLE results ADD COLUMN {column} TEXT')


def query(sql, args=()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def execute(sql, args=()):
    with conn() as c:
        return c.execute(sql, args).lastrowid


def stats(user_id):
    rows = query(
        "SELECT stage, is_lead, cost, feedback FROM results WHERE user_id=?", (user_id,)
    )
    n = len(rows)
    total = sum(r["cost"] for r in rows)
    deep = [r["cost"] for r in rows if r["stage"] in ("fit", "draft")]
    avg_deep = sum(deep) / len(deep) if deep else 0.0
    rated = [r for r in rows if r["is_lead"] and r["feedback"] is not None]
    good = sum(1 for r in rated if r["feedback"] == 1)
    return {
        "messages": n,
        "leads": sum(r["is_lead"] for r in rows),
        "by_stage": dict(Counter(r["stage"] for r in rows)),
        "total_cost": total,
        "avg_cost_per_message": total / n if n else 0.0,
        "cost_if_all_deep": avg_deep * n,
        "rated_leads": len(rated),
        "precision": good / len(rated) if rated else None,
    }

def feedback_examples(user_id, limit=5):
    return query(
        "SELECT text, score, feedback FROM results "
        "WHERE user_id=? AND feedback IS NOT NULL AND score IS NOT NULL "
        "ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )


def wallet(user_id):
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO wallets(user_id) VALUES(?)", (user_id,))
        return dict(c.execute("SELECT message_credit, rial_credit FROM wallets WHERE user_id=?", (user_id,)).fetchone())


def consume_message_credit(user_id, amount):
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO wallets(user_id) VALUES(?)", (user_id,))
        balance = c.execute("SELECT message_credit FROM wallets WHERE user_id=?", (user_id,)).fetchone()["message_credit"]
        used = min(balance, amount)
        if used:
            c.execute("UPDATE wallets SET message_credit=message_credit-?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (used, user_id))
            c.execute("INSERT INTO wallet_transactions(user_id, message_credit, rial_credit, description) VALUES(?,?,?,?)", (user_id, -used, 0, "مصرف اعتبار پیام برای تحلیل"))
        return used


def credit_report(report_id, message_credit, rial_credit, note):
    with conn() as c:
        report = c.execute("SELECT user_id, status FROM reports WHERE id=?", (report_id,)).fetchone()
        if not report:
            raise ValueError("report not found")
        if report["status"] != "pending":
            raise ValueError("report already reviewed")
        c.execute("UPDATE reports SET status='approved', admin_note=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?", (note, report_id))
        c.execute("INSERT OR IGNORE INTO wallets(user_id) VALUES(?)", (report["user_id"],))
        c.execute("UPDATE wallets SET message_credit=message_credit+?, rial_credit=rial_credit+?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (message_credit, rial_credit, report["user_id"]))
        c.execute("INSERT INTO wallet_transactions(user_id, report_id, message_credit, rial_credit, description) VALUES(?,?,?,?,?)", (report["user_id"], report_id, message_credit, rial_credit, note or "پاداش گزارش"))


def reject_report(report_id, note):
    with conn() as c:
        updated = c.execute("UPDATE reports SET status='rejected', admin_note=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'", (note, report_id)).rowcount
        if not updated:
            raise ValueError("report not found or already reviewed")


def credit_user(user_id, message_credit, rial_credit, note):
    with conn() as c:
        if not c.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise ValueError("user not found")
        c.execute("INSERT OR IGNORE INTO wallets(user_id) VALUES(?)", (user_id,))
        c.execute("UPDATE wallets SET message_credit=message_credit+?, rial_credit=rial_credit+?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (message_credit, rial_credit, user_id))
        c.execute("INSERT INTO wallet_transactions(user_id, message_credit, rial_credit, description) VALUES(?,?,?,?)", (user_id, message_credit, rial_credit, note or "اعتبار دستی مدیر"))


def admin_session(token):
    return bool(query("SELECT 1 FROM admin_sessions WHERE token=?", (token,)))