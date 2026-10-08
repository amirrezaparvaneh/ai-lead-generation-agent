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
    stage TEXT NOT NULL, is_lead INTEGER NOT NULL, score INTEGER, reason TEXT,
    analysis TEXT, draft TEXT, tokens INTEGER NOT NULL, cost REAL NOT NULL,
    feedback INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
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
        if 'phone' not in {row['name'] for row in c.execute('PRAGMA table_info(users)')}:
            c.execute('ALTER TABLE users ADD COLUMN phone TEXT')
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS users_phone ON users(phone)')


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
