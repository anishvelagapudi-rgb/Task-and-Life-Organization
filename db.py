import os
import re
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import g

_PLACEHOLDER_RE = re.compile(r"\?")


class _PGCursor:
    """Wraps a psycopg2 cursor so `.execute()` also rewrites `?`->`%s` — needed
    because classes/Task.py and classes/Project.py call conn.cursor() directly
    rather than going through _PGConnection.execute()."""

    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql, params=()):
        self._cur.execute(_PLACEHOLDER_RE.sub("%s", sql), params)
        return self

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _PGConnection:
    """Thin wrapper giving a psycopg2 connection the sqlite3.Connection.execute()
    convenience method (psycopg2 only exposes it on cursors), rewriting the
    codebase's sqlite-style `?` placeholders to psycopg2's `%s`, and defaulting to
    RealDictCursor so `row["col"]`/`dict(row)` call sites keep working unchanged.
    Not autocommit — callers already call db.commit() explicitly everywhere, matching
    sqlite3's default (also non-autocommit) behavior."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(_PLACEHOLDER_RE.sub("%s", sql), params)
        return cur

    def cursor(self, **kwargs):
        kwargs.setdefault("cursor_factory", RealDictCursor)
        return _PGCursor(self._conn.cursor(**kwargs))

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def get_db():
    if "db" not in g:
        g.db = _PGConnection(psycopg2.connect(os.environ["DATABASE_URL"]))
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


def enforce_no_self_parent(fields: dict, task_id: str) -> None:
    """A task can never be its own parent — silently drops parent_task_id from
    `fields` if it equals the row's own id, consistent with how other invalid
    values in this whitelist pattern are handled leniently elsewhere (see
    enforce_recurring_invariant) rather than erroring the whole request.

    Guards against a self-referential row causing an infinite loop in app.py's
    parent-chain walk (`while cur.get('parent_task_id'): ...`, used to render the
    subtask tree) — surfaced empirically: an AI model asked to "add a subtask to
    X" sometimes calls update_task(id=X, parent_task_id=X) on the parent itself
    instead of setting parent_task_id on the new child. Only guards direct
    self-reference, not longer cycles (A→B→A) — those were already possible via
    the REST API before parent_task_id was ever exposed to the AI and are a
    separate, pre-existing gap, not something this specific fix is scoped to close.

    Shared by api.py's REST endpoint and services/ai/service.py's AI tool executor,
    same pattern as enforce_recurring_invariant. Mutates `fields` in place. Only
    meaningful on UPDATE (task_id already exists) — on CREATE the new row's id
    doesn't exist yet when fields are being built, so self-reference is structurally
    impossible there."""
    if fields.get("parent_task_id") == task_id:
        fields["parent_task_id"] = None


def enforce_parent_exists(fields: dict, db) -> None:
    """parent_task_id must reference a real, currently-existing task row — silently
    dropped (same lenient pattern as enforce_no_self_parent/enforce_recurring_invariant
    above) if it doesn't, rather than left as a dangling reference.

    Surfaced by testing an alternate AI provider (NVIDIA-hosted Gemma-4-31B-IT) against
    the exact "1 parent + N subtasks" bulk-create shape that originally exposed the
    round-cap truncation bug (see README) — this provider batched the parent's
    create_task plus 50 subtask create_task calls into a single tool-calling round,
    so none of the subtask calls could know the parent's real server-generated UUID
    yet (it isn't returned until that round's tool results come back on the next
    turn). Rather than omit parent_task_id, the model filled it with the parent's
    title string. Nothing previously validated the value at all, so every subtask
    silently stored a parent_task_id matching no real row: app.py's parent-chain walk
    (`task_map.get(cur['parent_task_id'])`) treats an unresolvable id exactly like "no
    parent" and gives up, so the subtasks would have rendered as orphaned root-level
    tasks with zero visible link to their intended parent — while looking, by count
    and title alone, like a fully successful bulk create. This is a general schema
    gap, not specific to that provider or to the AI tool-calling path — api.py's own
    external REST create/update endpoints accept parent_task_id from any caller with
    the same lack of validation, so both call this, same as the other invariants here.
    Mutates `fields` in place."""
    pid = fields.get("parent_task_id")
    if not pid:
        return
    if not db.execute("SELECT 1 FROM tasks WHERE id = ?", (pid,)).fetchone():
        fields["parent_task_id"] = None


def init_db(app):
    """Schema is created once via supabase_setup.sql run directly against the
    Supabase project (see README) — Postgres isn't a per-boot local file, so there's
    no equivalent of the old create-if-missing/migrate-on-boot dance SQLite needed.
    This just wires up per-request connection cleanup."""
    app.teardown_appcontext(close_db)


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
