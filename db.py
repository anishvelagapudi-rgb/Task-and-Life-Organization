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
            CREATE TABLE IF NOT EXISTS projects (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL,
                description TEXT,
                status      TEXT DEFAULT 'active',
                progress    INTEGER DEFAULT 0,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_task_id   INTEGER,
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
                project_id       INTEGER REFERENCES projects(id),
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
