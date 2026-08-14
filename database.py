"""
database.py
================
SQLite persistence layer for the fitness tracker MVP.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

BASE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = BASE_DIR / "schema.sql"
DB_PATH = BASE_DIR / "fitness.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine: Engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)

def _translate_schema_to_sqlite(raw_sql: str) -> str:
    sql = raw_sql
    sql = re.sub(r"\bSERIAL\s+PRIMARY\s+KEY\b", "INTEGER PRIMARY KEY AUTOINCREMENT", sql)
    sql = re.sub(r"\bTIMESTAMPTZ\s+NOT\s+NULL\s+DEFAULT\s+NOW\(\)", "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP", sql)
    sql = re.sub(r"\bTIMESTAMPTZ\b", "TEXT", sql)
    return sql

def _load_translated_schema() -> str:
    raw_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    return _translate_schema_to_sqlite(raw_sql)

def init_db(force: bool = False) -> bool:
    if force and DB_PATH.exists():
        try:
            DB_PATH.unlink()
        except Exception:
            pass

    if DB_PATH.exists() and not force:
        return False

    translated_sql = _load_translated_schema()

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(translated_sql)
        conn.commit()
    finally:
        conn.close()

    return True

def get_db() -> Generator[Connection, None, None]:
    conn = engine.connect()
    try:
        yield conn
    finally:
        conn.close()

def seed_demo_data(conn: Connection) -> None:
    # ביטלנו את יצירת משתמש הדמו - מעכשיו כל אחד יירשם דרך האפליקציה!
    pass