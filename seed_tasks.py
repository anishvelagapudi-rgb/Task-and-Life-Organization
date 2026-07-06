"""
Seed script — populates dev.db with varied test tasks for UI testing.
Includes multi-level parent/child relationships to test the tree view.
Run from the project root: python seed_tasks.py
"""
from app import app
from db import init_db, get_db
from classes.Task import Task


# Top-level tasks (no parent).
# Format: (title, status, priority, energy_type, due_date, fear, ambiguity, effort, tags)
ROOT_TASKS = [
    ("Write research paper outline",   "active",  "critical", "deep_focus",  "2026-06-05", 8, 7, 120, ["research", "writing"]),
    ("Reply to professor email",       "inbox",   "high",     "light_admin", "2026-06-04", 2, 1,  10, ["email"]),
    ("Fix auth bug in prod",           "active",  "critical", "deep_focus",  "2026-06-03", 5, 6,  90, ["coding", "bug"]),
    ("Review PR from teammate",        "inbox",   "medium",   "deep_focus",  "2026-06-06", 1, 2,  30, ["coding", "review"]),
    ("Buy groceries",                  "inbox",   "low",      "low_energy",  None,         1, 1,  20, ["personal"]),
    ("Brainstorm project ideas",       "active",  "medium",   "creative",    "2026-06-10", 3, 8,  60, ["creative", "research"]),
    ("Schedule dentist appointment",   "inbox",   "low",      "light_admin", "2026-06-15", 1, 1,   5, ["personal", "health"]),
    ("Study for algorithms exam",      "blocked", "critical", "deep_focus",  "2026-06-08", 9, 5, 180, ["school", "studying"]),
    ("Update resume",                  "inbox",   "medium",   "light_admin", "2026-06-20", 4, 3,  45, ["career"]),
    ("Call mom",                       "inbox",   "low",      "social",      None,         1, 1,  15, ["personal"]),
    ("Design system architecture doc", "active",  "high",     "deep_focus",  "2026-06-07", 6, 7, 150, ["coding", "writing"]),
    ("Clean up old Notion pages",      "inbox",   "low",      "low_energy",  None,         1, 2,  30, ["admin"]),
    ("Practice leetcode mediums",      "active",  "medium",   "deep_focus",  "2026-06-09", 4, 4,  60, ["coding", "studying"]),
    ("Draft cold email to recruiter",  "inbox",   "high",     "creative",    "2026-06-06", 5, 4,  25, ["career", "email"]),
    ("Push week 3 assignment",         "done",    "high",     "deep_focus",  "2026-05-30", 3, 2,  90, ["school", "coding"]),
    ("Read chapter 4 of textbook",     "active",  "medium",   "low_energy",  "2026-06-11", 2, 3,  40, ["school", "reading"]),
    ("Team standup prep",              "inbox",   "medium",   "social",      "2026-06-03", 1, 1,  10, ["work"]),
    ("Refactor DB layer",              "blocked", "high",     "deep_focus",  "2026-06-12", 6, 8, 120, ["coding"]),
    ("Organize desktop files",         "inbox",   "low",      "low_energy",  None,         1, 1,  15, ["admin"]),
    ("Write cover letter",             "inbox",   "high",     "creative",    "2026-06-14", 6, 5,  60, ["career", "writing"]),
]

# Subtask definitions.
# Each entry is (parent_title, title, status, priority, energy_type, due_date, fear, ambiguity, effort, tags).
# parent_title must match a ROOT_TASKS title exactly so we can look up its id.
# Some subtasks also have their own children defined below (LEVEL2_TASKS).
SUBTASKS = [
    # children of "Write research paper outline"
    ("Write research paper outline", "Find relevant sources",     "done",    "high",     "deep_focus",  "2026-06-03", 3, 4,  60, ["research"]),
    ("Write research paper outline", "Write intro section",       "active",  "high",     "deep_focus",  "2026-06-04", 6, 5,  90, ["writing"]),
    ("Write research paper outline", "Write conclusion",          "inbox",   "medium",   "deep_focus",  "2026-06-05", 4, 6,  45, ["writing"]),

    # children of "Fix auth bug in prod"
    ("Fix auth bug in prod",         "Reproduce the bug locally", "done",    "critical", "deep_focus",  "2026-06-03", 2, 3,  30, ["coding", "bug"]),
    ("Fix auth bug in prod",         "Write failing test",        "active",  "high",     "deep_focus",  "2026-06-03", 3, 2,  20, ["coding"]),
    ("Fix auth bug in prod",         "Implement the fix",         "inbox",   "critical", "deep_focus",  "2026-06-03", 5, 6,  60, ["coding"]),

    # children of "Study for algorithms exam"
    ("Study for algorithms exam",    "Review sorting algorithms", "done",    "high",     "deep_focus",  "2026-06-06", 4, 3,  60, ["school", "studying"]),
    ("Study for algorithms exam",    "Review graph traversal",    "active",  "critical", "deep_focus",  "2026-06-07", 7, 5,  90, ["school", "studying"]),
    ("Study for algorithms exam",    "Review dynamic programming","inbox",   "critical", "deep_focus",  "2026-06-08", 9, 8, 120, ["school", "studying"]),

    # children of "Refactor DB layer"
    ("Refactor DB layer",            "Map out current schema",    "done",    "medium",   "deep_focus",  "2026-06-10", 2, 3,  30, ["coding"]),
    ("Refactor DB layer",            "Write migration script",    "active",  "high",     "deep_focus",  "2026-06-11", 5, 7,  90, ["coding"]),
    ("Refactor DB layer",            "Update all query calls",    "inbox",   "high",     "deep_focus",  "2026-06-12", 4, 6,  60, ["coding"]),
]

# Grandchild tasks — children of specific SUBTASKS.
# parent_title must match a SUBTASKS title exactly.
LEVEL2_TASKS = [
    # children of "Review graph traversal"
    ("Review graph traversal", "Practice BFS problems", "active",  "high", "deep_focus", "2026-06-07", 5, 3, 45, ["school", "studying"]),
    ("Review graph traversal", "Practice DFS problems", "inbox",   "high", "deep_focus", "2026-06-07", 5, 4, 45, ["school", "studying"]),

    # children of "Write intro section"
    ("Write intro section",    "Draft thesis statement","active",  "high", "deep_focus", "2026-06-04", 7, 6, 30, ["writing"]),
    ("Write intro section",    "Gather supporting quotes","inbox", "medium","deep_focus","2026-06-04", 3, 4, 20, ["research", "writing"]),

    # children of "Write migration script"
    ("Write migration script", "Back up dev.db first",  "done",    "critical","deep_focus","2026-06-11",1, 1, 10, ["coding"]),
    ("Write migration script", "Test rollback path",    "inbox",   "high", "deep_focus", "2026-06-11", 6, 7, 30, ["coding"]),
]


def make_task(title, status, priority, energy, due, fear, ambiguity, effort, tags, parent_id=None):
    return Task(
        title=title,
        status=status,
        priority=priority,
        energy_type=energy,
        due_date=due,
        fear_level=fear,
        ambiguity_level=ambiguity,
        estimated_effort=effort,
        tags=tags,
        parent_task_id=parent_id,
    )


def seed():
    init_db(app)
    with app.app_context():
        db = get_db()
        count = db.execute("SELECT COUNT(*) AS count FROM tasks").fetchone()["count"]
        if count > 0:
            print(f"DB already has {count} tasks. Truncate the tasks table first if you want a clean seed.")
            return

        # id_by_title lets us look up the auto-assigned DB id of any inserted task
        # so we can set parent_task_id correctly for subtasks.
        id_by_title = {}

        print("Root tasks:")
        for row in ROOT_TASKS:
            t = make_task(*row)
            t.db_push(db)
            id_by_title[t.title] = t.id
            print(f"  [{t.status:8}] [{t.priority:8}] {t.title}  (id={t.id})")

        print("\nSubtasks (depth 1):")
        for (parent_title, *rest) in SUBTASKS:
            parent_id = id_by_title.get(parent_title)
            if parent_id is None:
                print(f"  SKIP — parent '{parent_title}' not found")
                continue
            t = make_task(*rest, parent_id=parent_id)
            t.db_push(db)
            id_by_title[t.title] = t.id
            print(f"  [{t.status:8}] [{t.priority:8}] {t.title}  (id={t.id}, parent={parent_id})")

        print("\nSubtasks (depth 2):")
        for (parent_title, *rest) in LEVEL2_TASKS:
            parent_id = id_by_title.get(parent_title)
            if parent_id is None:
                print(f"  SKIP — parent '{parent_title}' not found")
                continue
            t = make_task(*rest, parent_id=parent_id)
            t.db_push(db)
            id_by_title[t.title] = t.id
            print(f"  [{t.status:8}] [{t.priority:8}] {t.title}  (id={t.id}, parent={parent_id})")

        total = len(ROOT_TASKS) + len(SUBTASKS) + len(LEVEL2_TASKS)
        print(f"\nSeeded {total} tasks ({len(ROOT_TASKS)} root, {len(SUBTASKS)} depth-1, {len(LEVEL2_TASKS)} depth-2).")


if __name__ == "__main__":
    seed()
