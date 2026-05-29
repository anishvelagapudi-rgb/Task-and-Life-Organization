#!/usr/bin/env python3
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from classes.Project import Project


def ok(label):
    print(f"  [PASS] {label}")

def section(title):
    print(f"\n{title}")
    print("-" * len(title))


# ─── object tests ─────────────────────────────────────────────────────────────

section("1. Defaults")
p = Project(title="Transfer Applications")
assert p.id is None
assert p.title == "Transfer Applications"
assert p.description is None
assert p.status == "active"
assert p.progress == 0
assert p.created_at is not None
assert p.updated_at is not None
ok("all defaults correct")

section("2. Full construction")
p2 = Project(
    title="Fitness",
    description="Get to 180lbs by end of year",
    status="active",
    progress=30,
)
assert p2.title == "Fitness"
assert p2.description == "Get to 180lbs by end of year"
assert p2.status == "active"
assert p2.progress == 30
ok("all fields set correctly")

section("3. repr")
r = repr(p2)
assert "Fitness" in r
assert "active" in r
assert "30" in r
ok("repr contains key fields")

section("4. to_dict")
d = p2.to_dict()
for key in ("id", "title", "description", "status", "progress", "created_at", "updated_at"):
    assert key in d, f"to_dict missing key: {key}"
assert d["title"] == "Fitness"
assert d["progress"] == 30
ok("to_dict has all fields with correct values")

section("5. Mutation")
p2.status = "paused"
p2.progress = 50
assert p2.status == "paused"
assert p2.progress == 50
ok("fields update correctly")


# ─── db tests ─────────────────────────────────────────────────────────────────

section("6. DB — setup in-memory SQLite")
conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.execute("""
    CREATE TABLE projects (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        title       TEXT NOT NULL,
        description TEXT,
        status      TEXT DEFAULT 'active',
        progress    INTEGER DEFAULT 0,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
""")
conn.commit()
ok("in-memory DB ready")

section("7. DB — insert")
p3 = Project(title="Learn Piano", description="Get through beginner curriculum")
assert p3.id is None
p3.db_push(conn)
assert p3.id is not None
inserted_id = p3.id
ok(f"inserted with id={inserted_id}")

section("8. DB — verify insert")
row = conn.execute("SELECT * FROM projects WHERE id = ?", (inserted_id,)).fetchone()
assert row is not None
assert row["title"] == "Learn Piano"
assert row["description"] == "Get through beginner curriculum"
assert row["status"] == "active"
assert row["progress"] == 0
ok("inserted row matches project fields")

section("9. DB — update")
p3.title = "Learn Piano (updated)"
p3.status = "paused"
p3.progress = 40
p3.db_push(conn)

row = conn.execute("SELECT * FROM projects WHERE id = ?", (inserted_id,)).fetchone()
assert row["title"] == "Learn Piano (updated)"
assert row["status"] == "paused"
assert row["progress"] == 40
ok("update reflected in DB")

section("10. DB — cleanup")
conn.execute("DELETE FROM projects WHERE id = ?", (inserted_id,))
conn.commit()
assert conn.execute("SELECT id FROM projects WHERE id = ?", (inserted_id,)).fetchone() is None
conn.close()
ok("test row deleted, connection closed")


print("\n✓ All tests passed.")
