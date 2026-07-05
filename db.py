import re
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


_RECURRING_VALUES = {"daily", "weekly"}


def enforce_recurring_invariant(fields: dict, existing_recurring: str | None = None) -> None:
    """Recurring tasks must never carry a due_date. Checks the task's *effective*
    recurring value — the incoming payload's `recurring` key if present, otherwise
    whatever is already persisted (`existing_recurring`) — so a partial update that
    touches due_date without mentioning recurring (or sets recurring without
    mentioning due_date) can't desync the two. Whenever the effective value is
    recurring, due_date is unconditionally forced to None in `fields`, regardless of
    whether the caller's payload already included a due_date key — a partial update
    that only sets `recurring` must still clear any due_date already stored on the row.
    Invalid recurring values are dropped rather than erroring, consistent with how
    other fields are handled leniently by callers of this whitelist pattern.

    Shared by api.py's REST endpoints and services/ai/service.py's AI tool executor so
    the rule lives in one place — it was previously duplicated in both, and drifted.
    Mutates `fields` in place."""
    if "recurring" in fields and fields["recurring"] not in _RECURRING_VALUES:
        fields["recurring"] = None
    effective_recurring = fields["recurring"] if "recurring" in fields else existing_recurring
    if effective_recurring:
        fields["due_date"] = None


def _table_exists(db, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(db, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _id_column_type(db, table: str) -> str | None:
    for row in db.execute(f"PRAGMA table_info({table})").fetchall():
        if row[1] == "id":
            return row[2]  # e.g. 'INTEGER' or 'TEXT'
    return None


def _migrate_legacy_schema(db) -> tuple[bool, bool]:
    """
    One-time migration for `tasks`/`projects` tables created before this project's
    switch to TEXT (UUID) ids. `CREATE TABLE IF NOT EXISTS` is a no-op against an
    already-existing table, so a `dev.db` created under the old schema (INTEGER
    PRIMARY KEY AUTOINCREMENT ids, and `tasks` missing tags/dependencies/task_notes)
    never picks up the new schema on its own — and classes/Task.py + classes/Project.py
    generate string UUIDs as ids, which raises `IntegrityError: datatype mismatch`
    against an INTEGER PRIMARY KEY column. Detected here and fixed forward.

    Renames the old table aside; the CREATE TABLE IF NOT EXISTS block that runs right
    after this builds the correct new table fresh. Data is copied back in by
    _finish_legacy_migration() once the new table exists, with every id-shaped column
    CAST to TEXT so relationships (tasks.project_id -> projects.id,
    tasks.parent_task_id -> tasks.id) stay consistent under the new affinity — SQLite
    treats TEXT '5' and INTEGER 5 as unequal, so casting must be uniform.

    Idempotent: once a table is on the new schema this is a no-op, safe on every boot.
    """
    migrate_projects = _table_exists(db, "projects") and (
        (_id_column_type(db, "projects") or "").upper() == "INTEGER"
    )
    migrate_tasks = _table_exists(db, "tasks") and (
        "tags" not in _columns(db, "tasks")
        or (_id_column_type(db, "tasks") or "").upper() == "INTEGER"
    )
    if migrate_projects:
        db.execute("ALTER TABLE projects RENAME TO projects_legacy")
    if migrate_tasks:
        db.execute("ALTER TABLE tasks RENAME TO tasks_legacy")
    return migrate_projects, migrate_tasks


def _finish_legacy_migration(db, migrate_projects: bool, migrate_tasks: bool) -> None:
    """Copies rows from the renamed-aside legacy tables into the freshly created
    new-schema tables, then drops the legacy tables. Must run after the
    CREATE TABLE IF NOT EXISTS block so the destination tables exist."""
    if migrate_projects:
        db.execute("""
            INSERT INTO projects (id, title, description, status, progress, created_at, updated_at)
            SELECT CAST(id AS TEXT), title, description, status, progress, created_at, updated_at
            FROM projects_legacy
        """)
        db.execute("DROP TABLE projects_legacy")
    if migrate_tasks:
        db.execute("""
            INSERT INTO tasks (
                id, parent_task_id, title, description, status, priority,
                due_date, completed_at, estimated_effort, energy_type,
                fear_level, ambiguity_level, project_id, source_type, ai_generated,
                created_at, updated_at, tags, dependencies, task_notes
            )
            SELECT
                CAST(id AS TEXT), CAST(parent_task_id AS TEXT), title, description, status, priority,
                due_date, completed_at, estimated_effort, energy_type,
                fear_level, ambiguity_level, CAST(project_id AS TEXT), source_type, ai_generated,
                created_at, updated_at, '[]', '[]', '[]'
            FROM tasks_legacy
        """)
        db.execute("DROP TABLE tasks_legacy")


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ALLOWED_COLTYPES = {"TEXT", "INTEGER", "REAL"}


def _ensure_column(db, table: str, column: str, coltype: str) -> None:
    """Additive column migration for tables that already exist. Safe to call every
    boot — no-ops once the column is present. Used for small, non-destructive schema
    growth (e.g. tasks.psych_reasoning, chat_messages.sources) where a fresh
    CREATE TABLE IF NOT EXISTS alone wouldn't reach an already-existing table.

    All current callers pass hardcoded literals, never user/AI-derived values — SQLite's
    ALTER TABLE doesn't support parameterized identifiers, so this validates against a
    strict allowlist pattern before interpolating, rather than trusting callers forever."""
    if not _IDENTIFIER_RE.match(table) or not _IDENTIFIER_RE.match(column):
        raise ValueError(f"Unsafe identifier in _ensure_column: table={table!r} column={column!r}")
    if coltype not in _ALLOWED_COLTYPES:
        raise ValueError(f"Unsupported column type in _ensure_column: {coltype!r}")
    if column not in _columns(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_db(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        db = get_db()
        migrate_projects, migrate_tasks = _migrate_legacy_schema(db)
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
            CREATE TABLE IF NOT EXISTS note_connections (
                id                 TEXT PRIMARY KEY,
                source_path        TEXT NOT NULL,
                target_path        TEXT NOT NULL,
                source_collection  TEXT,
                target_collection  TEXT,
                distance           REAL,
                summary            TEXT,
                created_at         TEXT NOT NULL
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
        _finish_legacy_migration(db, migrate_projects, migrate_tasks)
        _ensure_column(db, "tasks", "psych_reasoning", "TEXT")
        _ensure_column(db, "chat_messages", "sources", "TEXT")
        _ensure_column(db, "tasks", "recurring", "TEXT")
        _ensure_column(db, "calendars", "import_as", "TEXT")
        _ensure_column(db, "tasks", "source_uid", "TEXT")
        _ensure_column(db, "tasks", "source_calendar_id", "TEXT")
        db.commit()


_TZ_NAME_RE = re.compile(r"^[A-Za-z0-9_+\-/]+$")


def _resolve_tz(client_tz: str | None):
    """Validates and resolves a client-supplied IANA zone name, falling back to UTC on
    anything missing/invalid. The regex + length cap are defense in depth on top of
    ZoneInfo's own lookup — a bad/unknown zone name here should never break a caller."""
    from datetime import timezone

    if client_tz and len(client_tz) <= 64 and _TZ_NAME_RE.match(client_tz):
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(client_tz)
        except Exception:
            # Broad on purpose: a regex-valid key can still fail in ways beyond
            # ZoneInfoNotFoundError/ValueError — e.g. "America" or "Etc" are valid
            # zoneinfo *directory* prefixes, not resolvable zones, and raise
            # IsADirectoryError. Mirrors the same defensive bare-except style already
            # used for client_tz handling in services/ai/service.py's chat().
            pass
    return timezone.utc


def reset_due_recurring_tasks(db, client_tz: str | None = None) -> None:
    """Auto-uncheck recurring tasks once their period has elapsed since completion.

    Runs lazily on read (called from the routes that display recurring state) rather
    than via a background scheduler — deliberately reuses `completed_at` (already set/
    cleared by the complete-toggle routes) as the recurrence clock instead of adding a
    separate `last_completed_at` column.

    Both the "today"/"week start" boundary AND the completed_at timestamp being compared
    against it are converted into `client_tz` (an IANA name, e.g. "America/New_York")
    before comparing local calendar dates — mirrors the client_tz pattern already used
    for date reasoning in services/ai/service.py's chat(). completed_at is always stored
    as a UTC timestamp (set by the complete-toggle routes), so comparing its raw string
    prefix directly against a locally-computed boundary would be off by up to a day near
    midnight; parsing it and re-anchoring to client_tz avoids that. The caller reads
    client_tz from the `tz` cookie set client-side.

    Selects only the narrow set of candidates (recurring set AND status='done') into
    Python for that per-row conversion, then issues one UPDATE by id — never scans the
    bulk of the tasks table, so this stays cheap even without a dedicated index at this
    single-user scale.
    """
    from datetime import datetime, timezone, timedelta

    tzinfo = _resolve_tz(client_tz)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tzinfo)
    today_local = now_local.date()
    week_start_local = today_local - timedelta(days=today_local.weekday())  # Monday

    rows = db.execute(
        """SELECT id, recurring, completed_at FROM tasks
           WHERE recurring IN ('daily', 'weekly') AND status='done' AND completed_at IS NOT NULL"""
    ).fetchall()

    due_ids = []
    for row in rows:
        raw = row["completed_at"]
        try:
            completed_dt = datetime.fromisoformat(raw)
        except ValueError:
            continue  # unparseable timestamp — leave the task as-is rather than guess
        if completed_dt.tzinfo is None:
            completed_dt = completed_dt.replace(tzinfo=timezone.utc)  # legacy rows: assume UTC, this codebase's convention
        completed_local_date = completed_dt.astimezone(tzinfo).date()

        boundary = today_local if row["recurring"] == "daily" else week_start_local
        if completed_local_date < boundary:
            due_ids.append(row["id"])

    if not due_ids:
        return

    placeholders = ", ".join("?" * len(due_ids))
    db.execute(
        f"""UPDATE tasks SET status='inbox', completed_at=NULL, updated_at=?
            WHERE id IN ({placeholders})""",
        [now_utc.isoformat(), *due_ids],
    )
    db.commit()
