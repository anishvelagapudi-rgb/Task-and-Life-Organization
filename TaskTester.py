#!/usr/bin/env python3
import sqlite3
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from classes.Task import Task


def ok(label):
    print(f"  [PASS] {label}")

def section(title):
    print(f"\n{title}")
    print("-" * len(title))


# ─── object tests ─────────────────────────────────────────────────────────────

section("1. Defaults")
t = Task(title="Reply to advisor email")
assert t.id is None
assert t.title == "Reply to advisor email"
assert t.status == "inbox"
assert t.priority == "medium"
assert t.source_type == "manual"
assert t.ai_generated is False
assert t.completed_at is None
assert t.created_at is not None
assert t.updated_at is not None
ok("all defaults correct")

section("2. Full construction")
due = datetime(2026, 6, 1, tzinfo=timezone.utc)
t2 = Task(
    title="Finish transfer essay",
    description="Personal statement for UC transfers",
    status="active",
    priority="critical",
    due_date=due,
    estimated_effort=120,
    energy_type="deep_focus",
    fear_level=8,
    ambiguity_level=6,
)
assert t2.title == "Finish transfer essay"
assert t2.status == "active"
assert t2.priority == "critical"
assert t2.due_date == due
assert t2.estimated_effort == 120
assert t2.energy_type == "deep_focus"
assert t2.fear_level == 8
assert t2.ambiguity_level == 6
ok("all fields set correctly")

section("3. repr")
r = repr(t2)
assert "Finish transfer essay" in r
assert "active" in r
assert "critical" in r
assert "deep_focus" in r
assert "8" in r
ok("repr contains key fields")

section("4. to_dict")
d = t2.to_dict()
for key in ("id", "title", "description", "status", "priority", "due_date",
            "estimated_effort", "energy_type", "fear_level", "ambiguity_level",
            "project_id", "parent_task_id", "source_type", "ai_generated",
            "created_at", "updated_at", "completed_at"):
    assert key in d, f"to_dict missing key: {key}"
assert d["title"] == "Finish transfer essay"
assert d["fear_level"] == 8
ok("to_dict has all fields with correct values")

section("5. Mutation")
t2.status = "done"
t2.completed_at = datetime.now(timezone.utc)
assert t2.status == "done"
assert t2.completed_at is not None
ok("fields update correctly")


# ─── db tests ─────────────────────────────────────────────────────────────────

section("6. DB — setup in-memory SQLite")
conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.execute("""
    CREATE TABLE tasks (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        parent_task_id  INTEGER,
        title           TEXT NOT NULL,
        description     TEXT,
        status          TEXT DEFAULT 'inbox',
        priority        TEXT DEFAULT 'medium',
        due_date        TEXT,
        completed_at    TEXT,
        estimated_effort INTEGER,
        energy_type     TEXT,
        fear_level      INTEGER,
        ambiguity_level INTEGER,
        project_id      INTEGER,
        source_type     TEXT DEFAULT 'manual',
        ai_generated    INTEGER DEFAULT 0,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
""")
conn.commit()
ok("in-memory DB ready")

section("7. DB — insert")
t3 = Task(
    title="Test insert task",
    description="Created by TaskTester",
    status="inbox",
    priority="low",
    fear_level=2,
    ambiguity_level=3,
)
assert t3.id is None
t3.db_push(conn)
assert t3.id is not None, "id should be set after insert"
inserted_id = t3.id
ok(f"inserted with id={inserted_id}")

section("8. DB — verify insert")
row = conn.execute("SELECT * FROM tasks WHERE id = ?", (inserted_id,)).fetchone()
assert row is not None
assert row["title"] == "Test insert task"
assert row["status"] == "inbox"
assert row["fear_level"] == 2
assert row["ambiguity_level"] == 3
ok("inserted row matches task fields")

section("9. DB — update")
t3.title = "Test insert task (updated)"
t3.status = "active"
t3.fear_level = 9
t3.db_push(conn)

row = conn.execute("SELECT * FROM tasks WHERE id = ?", (inserted_id,)).fetchone()
assert row["title"] == "Test insert task (updated)"
assert row["status"] == "active"
assert row["fear_level"] == 9
ok("update reflected in DB")

section("10. DB — cleanup")
conn.execute("DELETE FROM tasks WHERE id = ?", (inserted_id,))
conn.commit()
assert conn.execute("SELECT id FROM tasks WHERE id = ?", (inserted_id,)).fetchone() is None
conn.close()
ok("test row deleted, connection closed")


print("\n✓ All tests passed.")
