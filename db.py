import sqlite3
from flask import g

DATABASE = "dev.db"


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        db = get_db()
        db.executescript("""
            CREATE TABLE IF NOT EXISTS tokens (
                provider     TEXT PRIMARY KEY,
                access_token TEXT NOT NULL,
                refresh_token TEXT,
                token_type   TEXT NOT NULL DEFAULT 'Bearer',
                expires_at   TEXT
            );
            CREATE TABLE IF NOT EXISTS calendars (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                color      TEXT NOT NULL DEFAULT '#4a9eff',
                source     TEXT NOT NULL DEFAULT 'local',
                ics_url    TEXT,
                visible    INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id             TEXT PRIMARY KEY,
                calendar_id    TEXT NOT NULL REFERENCES calendars(id),
                title          TEXT NOT NULL,
                description    TEXT,
                start_datetime TEXT NOT NULL,
                end_datetime   TEXT,
                all_day        INTEGER NOT NULL DEFAULT 0,
                location       TEXT,
                source_uid     TEXT,
                created_at     TEXT NOT NULL,
                updated_at     TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chats (
                id         TEXT PRIMARY KEY,
                title      TEXT NOT NULL DEFAULT 'New Chat',
                indexed    INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_messages (
                id         TEXT PRIMARY KEY,
                chat_id    TEXT NOT NULL,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projects (
                id          TEXT PRIMARY KEY,
                title       TEXT NOT NULL,
                description TEXT,
                status      TEXT DEFAULT 'active',
                progress    INTEGER DEFAULT 0,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id               TEXT PRIMARY KEY,
                parent_task_id   TEXT,
                title            TEXT NOT NULL,
                description      TEXT,
                status           TEXT DEFAULT 'inbox',
                priority         TEXT DEFAULT 'medium',
                due_date         TEXT,
                completed_at     TEXT,
                estimated_effort INTEGER,
                energy_type      TEXT,
                fear_level       INTEGER,
                ambiguity_level  INTEGER,
                project_id       TEXT REFERENCES projects(id),
                source_type      TEXT DEFAULT 'manual',
                ai_generated     INTEGER DEFAULT 0,
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL,
                tags             TEXT DEFAULT '[]',
                dependencies     TEXT DEFAULT '[]',
                task_notes       TEXT DEFAULT '[]'
            );
        """)
        db.commit()
