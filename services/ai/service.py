import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import dateparser
from dateparser.search import search_dates

from .provider import AIProvider

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a personal execution assistant for a single user.

Your job is to help them figure out what to work on right now, cut through ambiguity,
and break down tasks when needed. You understand their psychological model of tasks:

TASK FIELDS YOU'LL SEE:
- fear_level (1-5): how much the task is being avoided due to anxiety or uncertainty
- ambiguity_level (1-5): how unclear the task is (what "done" looks like)
- energy_type: deep_focus | light_admin | social | creative | low_energy
- estimated_effort: minutes of expected work
- priority: critical | high | medium | low
- status: inbox | active | done | blocked

RECOMMENDATION PHILOSOPHY:
Recommend tasks that maximize: urgency + actionability + momentum - resistance.
- Prefer tasks with approaching due dates
- Prefer tasks where it's clear what to do next (low ambiguity)
- Deprioritize tasks with high fear unless they're also high urgency
- Match energy type to what the user is likely capable of right now

KNOWLEDGE BASE (VAULT):
You have access to the user's personal vault — notes organized into four folders:
people (notes about specific people), projects (notes tied to a named project or class),
reference (look-up material: how-tos, facts, definitions), journal (personal entries and reflections),
and inbox (unsorted or recently added files).

Rules for vault usage:
- Prefer vault content over general knowledge when answering questions about the user's life/work.
- Cite the source file when using vault content (e.g. "According to projects/cs101.md...").
- If no vault context was retrieved and you're using general knowledge, you MUST literally
  append the exact characters "(GK)" to the end of your response — no exceptions. Then on
  a new line, ask if the user wants you to search the vault more broadly. Example ending:
  "...the answer is 3,422°C. (GK)\n\nWant me to search your vault for anything related?"
- Retrieved chunks may not always be perfectly relevant — use your judgment about whether
  they actually apply to the question.
- Use search_vault when the user asks about notes or when you want to look something up explicitly.
- Use read_document to read a complete vault file when full coverage matters (e.g., course
  requirements, syllabi, full reference docs). Prefer this over search_vault when you need the
  entire document, not just relevant snippets. The path is vault-relative (e.g. 'reference/unc-cs-requirements.html').
- Use create_note to save important insights to the vault when the user asks or when you spot
  a clear knowledge gap worth documenting.

CALENDAR:
The user has a local calendar and, optionally, a connected Google Calendar. When a calendar
question is asked, an UPCOMING EVENTS block is injected into context covering both — local
events (editable) and Google Calendar events tagged "(Google, read-only)". Answer directly
from that block; you have no tool to fetch Google Calendar yourself, so never call a tool
to check it and never claim you lack access to it.
Use list_events to look up local events beyond the injected 14-day window. Use create_event
to add new events to LOCAL calendars only. Use update_event to modify existing local events.
You cannot create, update, or delete Google Calendar events — if the user asks you to, tell
them it's read-only from here and to use Google Calendar directly.
When creating an event, prefer a calendar_id from the LOCAL CALENDARS list injected into
context. If the user does not specify a calendar, omit calendar_id and the system will
use the first available calendar.
When the user asks for one event (e.g. "block 2 hours tomorrow for X") and doesn't give a
specific start time, call create_event exactly ONCE with one reasonable start time — never
create multiple candidate events at different times to hedge. Pick a single sensible time
(e.g. mid-morning) and go with it.

CALENDAR DATE RULES — follow these strictly, never ask the user about them:
- When the user asks what's on their calendar with no range specified, default to today
  through 60 days from now.
- When the user gives a vague range ("all time", "everything", "this year"), pick a
  reasonable wide range yourself — e.g. 2000-01-01 to 2030-12-31.
- When the user gives natural language dates ("next week", "2 years from now", "January"),
  convert them to YYYY-MM-DD yourself using today's date as the reference.
- Never ask the user to provide dates in a specific format. Never ask for clarification
  about date ranges. Just pick a reasonable interpretation and call the tool.

IMPORTANT RULES:
- Do exactly what the user asks — no more. Do not create extra tasks, subtasks, or related items unless explicitly requested.
- Only claim an action was taken after a tool call confirms it succeeded.
- Keep replies short. Confirm what you did in one sentence. Do not list attributes or explain your work unless asked.
- Never volunteer information, suggestions, or follow-up actions unless the user asks.
- You ALWAYS have full access to the local calendar, and Google Calendar data is already injected into context when relevant. Never say you lack calendar access, cannot access the calendar (local or Google), or need permission. If a user asks about their calendar, use the UPCOMING EVENTS block already in context, or call list_events for local events beyond that window — do not refuse.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_project",
            "description": "Create a new project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title":       {"type": "string"},
                    "description": {"type": "string"},
                    "status":      {"type": "string", "enum": ["active", "paused", "done"]},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": "Create a new task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title":            {"type": "string"},
                    "description":      {"type": "string"},
                    "priority":         {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                    "status":           {"type": "string", "enum": ["inbox", "active", "blocked"]},
                    "energy_type":      {"type": "string", "enum": ["deep_focus", "light_admin", "social", "creative", "low_energy"]},
                    "due_date":         {"type": "string", "description": "YYYY-MM-DD"},
                    "project_id":       {"type": "integer"},
                    "fear_level":       {"type": "integer", "minimum": 1, "maximum": 5},
                    "ambiguity_level":  {"type": "integer", "minimum": 1, "maximum": 5},
                    "estimated_effort": {"type": "integer", "description": "Minutes"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_project",
            "description": "Permanently delete a project by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_task",
            "description": "Permanently delete a task by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_task",
            "description": "Update one or more fields on an existing task. Use this to reprioritize, reschedule, or change the status of a task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id":               {"type": "integer"},
                    "title":            {"type": "string"},
                    "description":      {"type": "string"},
                    "priority":         {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                    "status":           {"type": "string", "enum": ["inbox", "active", "done", "blocked"]},
                    "energy_type":      {"type": "string", "enum": ["deep_focus", "light_admin", "social", "creative", "low_energy"]},
                    "due_date":         {"type": "string", "description": "YYYY-MM-DD"},
                    "project_id":       {"type": "integer"},
                    "fear_level":       {"type": "integer", "minimum": 1, "maximum": 5},
                    "ambiguity_level":  {"type": "integer", "minimum": 1, "maximum": 5},
                    "estimated_effort": {"type": "integer", "description": "Minutes"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_vault",
            "description": (
                "Search the personal knowledge vault for notes on any topic. "
                "Returns relevant text chunks with source citations. "
                "Use when the user asks about notes, past information, or anything the vault might contain."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query",
                    },
                    "collection": {
                        "type": "string",
                        "description": (
                            "Scope to a specific folder: "
                            "people | projects | reference | journal | inbox"
                        ),
                    },
                    "k": {
                        "type": "integer",
                        "description": "Number of results (1-10, default 5)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": (
                "Read the full content of a vault document by its vault-relative path. "
                "Use when you need complete coverage of a reference file — e.g. course "
                "requirements, syllabi, full how-to guides. Prefer this over search_vault "
                "when the question requires the entire document, not just relevant snippets."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Vault-relative path (e.g. 'reference/unc-cs-requirements.html')",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_events",
            "description": "List local calendar events and task deadlines for a date range. Does NOT include Google Calendar. Default to today + 60 days when the user does not specify a range. Always convert natural language dates to YYYY-MM-DD yourself — never ask the user to provide a format or clarify a date range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "Start date (YYYY-MM-DD, inclusive)"},
                    "end_date":   {"type": "string", "description": "End date (YYYY-MM-DD, inclusive)"},
                },
                "required": ["start_date", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_event",
            "description": "Create a new event in a local calendar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title":          {"type": "string"},
                    "start_datetime": {"type": "string", "description": "ISO 8601 datetime or YYYY-MM-DD for all-day"},
                    "end_datetime":   {"type": "string", "description": "ISO 8601 end time (optional)"},
                    "all_day":        {"type": "boolean"},
                    "calendar_id":    {"type": "string", "description": "ID from LOCAL CALENDARS; omit to use default"},
                    "description":    {"type": "string"},
                    "location":       {"type": "string"},
                },
                "required": ["title", "start_datetime"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_event",
            "description": "Update fields on an existing local calendar event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id":             {"type": "string"},
                    "title":          {"type": "string"},
                    "start_datetime": {"type": "string"},
                    "end_datetime":   {"type": "string"},
                    "all_day":        {"type": "boolean"},
                    "calendar_id":    {"type": "string"},
                    "description":    {"type": "string"},
                    "location":       {"type": "string"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_note",
            "description": (
                "Create a new markdown note in the AI-generated vault. "
                "Use when the user explicitly asks to save a note, or when you identify a "
                "knowledge gap worth documenting."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Filename without extension, use kebab-case (e.g. 'study-plan-cs101')",
                    },
                    "title": {"type": "string", "description": "Human-readable note title"},
                    "topic": {"type": "string", "description": "Topic or subject of the note"},
                    "content": {
                        "type": "string",
                        "description": "Markdown content (without frontmatter)",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of tags",
                    },
                },
                "required": ["filename", "title", "topic", "content"],
            },
        },
    },
]

_EVENT_WRITE_FIELDS = {
    "title", "description", "start_datetime", "end_datetime",
    "all_day", "location", "calendar_id",
}

_TASK_WRITE_FIELDS = {
    "title", "description", "priority", "status", "energy_type",
    "due_date", "project_id", "fear_level", "ambiguity_level", "estimated_effort",
}

_CALENDAR_WORDS = re.compile(
    r"\b(calendar|event|events|schedule|scheduled|appointment|meeting|"
    r"tomorrow|tonight|yesterday|today|this week|next week|last week|"
    r"this weekend|last weekend|this month|last month|next month|"
    r"what.s on|what do i have|what happened|anything (today|tomorrow|this week)|"
    r"free|busy|available|"
    # explicit dates: "June 28th", "6/28", "2026-06-28", "the 28th", "on the 3rd"
    r"jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|jun(e)?|jul(y)?|aug(ust)?|"
    r"sep(t|tember)?|oct(ober)?|nov(ember)?|dec(ember)?|"
    r"\d{1,2}(st|nd|rd|th)|\d{1,2}/\d{1,2}|\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)

_MUTATION_START = re.compile(
    r"^\s*(mark|complete|delete|remove|update|set|change|rename|archive|"
    r"assign|unassign|create\s+(a\s+)?(new\s+)?(task|project)|"
    r"add\s+(a\s+)?(new\s+)?(task|project))\b",
    re.IGNORECASE,
)
_KNOWLEDGE_WORDS = re.compile(
    r"\b(what|which|who|when|where|why|how|tell|explain|describe|summarize|"
    r"know|notes?|vault|remember|recall|find|search|show me|list all|think|feel)\b",
    re.IGNORECASE,
)


def _should_skip_rag(message: str) -> bool:
    """True if message is clearly a task/project CRUD command with no knowledge component."""
    if _KNOWLEDGE_WORDS.search(message):
        return False
    return bool(_MUTATION_START.match(message))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_TOOL_CONFIRM_VERBS = {
    "create_project": "Created project",
    "create_task": "Created task",
    "delete_project": "Deleted project",
    "delete_task": "Deleted task",
    "update_task": "Updated task",
    "create_event": "Created",
    "update_event": "Updated",
    "create_note": "Saved note",
}


def _synthesize_tool_confirmation(name: str, args: dict, result: dict) -> str:
    """Gemini sometimes emits 0 output tokens on the round right after a tool call
    (documented elsewhere in this file for search_vault) — the action still went
    through, but the user would see a blank reply. Build a plain confirmation straight
    from the tool's actual result instead of leaving it empty or asking the model again."""
    if not result.get("success", True):
        return f"That didn't work: {result.get('error', 'unknown error')}"
    label = result.get("title") or args.get("title") or result.get("id", "")
    verb = _TOOL_CONFIRM_VERBS.get(name, "Done —")
    return f'{verb} "{label}".' if label else f"{verb}."


def _execute_tool(db, name: str, args: dict) -> dict:
    if name == "create_project":
        new_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO projects (id, title, description, status, progress, created_at, updated_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
            (new_id, args["title"], args.get("description"), args.get("status", "active"), _now(), _now()),
        )
        db.commit()
        return {"success": True, "id": new_id, "title": args["title"]}

    if name == "create_task":
        fields = {k: v for k, v in args.items() if k in _TASK_WRITE_FIELDS}
        fields.setdefault("status", "inbox")
        fields.setdefault("priority", "medium")
        fields["id"] = str(uuid.uuid4())
        fields["ai_generated"] = 1
        fields["created_at"] = _now()
        fields["updated_at"] = _now()
        cols = ", ".join(fields)
        placeholders = ", ".join("?" * len(fields))
        db.execute(
            f"INSERT INTO tasks ({cols}) VALUES ({placeholders})",
            list(fields.values()),
        )
        db.commit()
        return {"success": True, "id": fields["id"], "title": args["title"]}

    if name == "delete_project":
        db.execute("DELETE FROM projects WHERE id = ?", (args["id"],))
        db.commit()
        return {"success": True, "id": args["id"]}

    if name == "delete_task":
        db.execute("DELETE FROM tasks WHERE id = ?", (args["id"],))
        db.commit()
        return {"success": True, "id": args["id"]}

    if name == "update_task":
        task_id = args["id"]
        updates = {k: v for k, v in args.items() if k in _TASK_WRITE_FIELDS}
        if not updates:
            return {"success": False, "error": "No valid fields to update"}
        updates["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        db.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", [*updates.values(), task_id])
        db.commit()
        return {"success": True, "id": task_id}

    if name == "list_events":
        start_date = args.get("start_date", "")
        end_date = args.get("end_date", "")
        rows = db.execute("""
            SELECT e.id, e.title, e.start_datetime, e.end_datetime, e.all_day,
                   e.description, e.location, c.name as calendar_name
            FROM events e JOIN calendars c ON e.calendar_id = c.id
            WHERE substr(e.start_datetime, 1, 10) >= ?
              AND substr(e.start_datetime, 1, 10) <= ?
            ORDER BY e.start_datetime
        """, (start_date, end_date)).fetchall()
        task_rows = db.execute("""
            SELECT id, title, due_date, priority, status FROM tasks
            WHERE due_date >= ? AND due_date <= ? AND status != 'done'
            ORDER BY due_date
        """, (start_date, end_date)).fetchall()
        return {
            "events": [dict(r) for r in rows],
            "task_deadlines": [dict(t) for t in task_rows],
        }

    if name == "create_event":
        if not args.get("title") or not args.get("start_datetime"):
            return {"success": False, "error": "title and start_datetime are required to create an event"}
        cal_id = args.get("calendar_id")
        if not cal_id:
            first = db.execute("SELECT id FROM calendars ORDER BY created_at LIMIT 1").fetchone()
            if not first:
                return {"success": False, "error": "No local calendars exist. Ask the user to create one on the calendar page first."}
            cal_id = first["id"]
        new_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO events
               (id, calendar_id, title, description, start_datetime, end_datetime, all_day, location, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (new_id, cal_id, args["title"], args.get("description"),
             args["start_datetime"], args.get("end_datetime"),
             int(bool(args.get("all_day", False))), args.get("location"), _now(), _now()),
        )
        db.commit()
        return {"success": True, "id": new_id, "title": args["title"]}

    if name == "update_event":
        if not args.get("id"):
            return {"success": False, "error": "id is required to update an event"}
        event_id = args["id"]
        updates = {k: v for k, v in args.items() if k in _EVENT_WRITE_FIELDS}
        if "all_day" in updates:
            updates["all_day"] = int(bool(updates["all_day"]))
        if not updates:
            return {"success": False, "error": "No valid fields to update"}
        updates["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        db.execute(f"UPDATE events SET {set_clause} WHERE id = ?", [*updates.values(), event_id])
        db.commit()
        return {"success": True, "id": event_id}

    if name == "read_document":
        try:
            from services.rag.chunker import _extract_text
            from services.rag.indexer import VAULT_ROOT

            rel = args["path"].lstrip("/")
            abs_path = os.path.realpath(os.path.join(VAULT_ROOT, rel))
            vault_root = os.path.realpath(VAULT_ROOT)
            if not abs_path.startswith(vault_root + os.sep):
                return {"found": False, "error": "Path is outside the vault"}
            if not os.path.exists(abs_path):
                return {"found": False, "error": f"File not found: {args['path']}"}
            _, content = _extract_text(abs_path)
            return {"found": True, "path": args["path"], "content": content}
        except Exception as e:
            logger.exception("read_document tool failed")
            return {"found": False, "error": str(e)}

    if name == "search_vault":
        try:
            from services.rag.retriever import retrieve
            from services.rag.injector import build_context
            query = args["query"]
            col = args.get("collection")
            k = min(int(args.get("k", 5)), 10)
            results = retrieve(query, k=k, collections=[col] if col else None)
            if not results:
                return {"found": False, "message": "No relevant notes found in the vault."}
            return {
                "found": True,
                "chunks": [
                    {
                        "source": c.source_path.split("/data/vault/", 1)[-1]
                        if "/data/vault/" in c.source_path
                        else c.source_path,
                        "collection": c.collection,
                        "heading": c.heading,
                        "text": c.text[:1200],
                    }
                    for c in results
                ],
            }
        except Exception as e:
            logger.exception("search_vault tool failed")
            return {"found": False, "error": str(e)}

    if name == "create_note":
        try:
            import frontmatter as fm
            from services.rag.indexer import VAULT_ROOT, index_file

            slug = re.sub(r"[^\w\-]", "-", args["filename"].lower().strip()).strip("-")
            if not slug:
                slug = "note"
            filename = slug + ".md"
            path = os.path.join(VAULT_ROOT, "inbox", filename)

            if os.path.exists(path):
                ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
                path = os.path.join(VAULT_ROOT, "inbox", f"{slug}-{ts}.md")

            meta = {
                "title": args["title"],
                "topic": args["topic"],
                "tags": args.get("tags", []),
                "created_at": _now(),
                "updated_at": _now(),
            }
            post = fm.Post(args["content"], **meta)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(fm.dumps(post))

            # Index immediately so it's searchable in this same session
            try:
                index_file(path)
            except Exception:
                logger.warning("Immediate index of created note failed; watcher will retry")

            rel = path.split("/data/vault/", 1)[-1] if "/data/vault/" in path else path
            return {"success": True, "path": rel}
        except Exception as e:
            logger.exception("create_note tool failed")
            return {"success": False, "error": str(e)}

    return {"success": False, "error": f"Unknown tool: {name}"}


_DEFAULT_CALENDAR_WINDOW_DAYS = 14

_WEEKDAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_WEEKDAY_RANGE_RE = re.compile(
    r"\b(last|next|this)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)


def _weekday_range(last_user: str, today) -> tuple[str, str, str] | None:
    """Deterministic date arithmetic for "last/next/this <weekday>" — the model was
    inconsistent about whether "last Sunday" means the nearest past Sunday or a full
    week back, depending on today's own weekday. Never ask it to guess this."""
    from datetime import timedelta

    m = _WEEKDAY_RANGE_RE.search(last_user)
    if not m:
        return None
    qualifier, day_name = m.group(1).lower(), m.group(2).lower()
    target = _WEEKDAY_NAMES[day_name]
    if qualifier == "last":
        offset = (today.weekday() - target) % 7
        offset = offset or 7  # "last Sunday" said on a Sunday means a week ago, not today
        d = today - timedelta(days=offset)
    elif qualifier == "next":
        offset = (target - today.weekday()) % 7
        offset = offset or 7  # "next Sunday" said on a Sunday means a week from now
        d = today + timedelta(days=offset)
    else:  # "this"
        offset = (target - today.weekday()) % 7
        d = today + timedelta(days=offset)
    iso = d.isoformat()
    return iso, iso, f"{qualifier} {day_name}"


# Checked in order — multi-word phrases must come before the single words they contain
# ("day after tomorrow" before "tomorrow"), or the shorter pattern would match first and
# give the wrong offset.
_RELATIVE_DAY_PATTERNS = [
    (re.compile(r"\bday after tomorrow\b", re.IGNORECASE), 2, "day after tomorrow"),
    (re.compile(r"\bday before yesterday\b", re.IGNORECASE), -2, "day before yesterday"),
    (re.compile(r"\btomorrow\b", re.IGNORECASE), 1, "tomorrow"),
    (re.compile(r"\byesterday\b", re.IGNORECASE), -1, "yesterday"),
    (re.compile(r"\btoday\b|\btonight\b", re.IGNORECASE), 0, "today"),
]
_N_DAYS_AHEAD_RE = re.compile(r"\b(\d+)\s+days?\s+from now\b|\bin\s+(\d+)\s+days?\b", re.IGNORECASE)
_N_DAYS_AGO_RE = re.compile(r"\b(\d+)\s+days?\s+ago\b", re.IGNORECASE)


def _relative_day_range(last_user: str, today) -> tuple[str, str, str] | None:
    """Deterministic date arithmetic for self-contained relative-day phrases. The model
    got "day after tomorrow" wrong by a day (computed it as plain "tomorrow") — this is
    arithmetic, not language understanding, so never delegate it."""
    from datetime import timedelta

    m = _N_DAYS_AHEAD_RE.search(last_user)
    if m:
        n = int(m.group(1) or m.group(2))
        d = today + timedelta(days=n)
        iso = d.isoformat()
        return iso, iso, f"{n} days from now"
    m = _N_DAYS_AGO_RE.search(last_user)
    if m:
        n = int(m.group(1))
        d = today - timedelta(days=n)
        iso = d.isoformat()
        return iso, iso, f"{n} days ago"
    for pattern, offset, label in _RELATIVE_DAY_PATTERNS:
        if pattern.search(last_user):
            d = today + timedelta(days=offset)
            iso = d.isoformat()
            return iso, iso, label
    return None


# Guards dateparser's search_dates against matching stray common words (it once matched
# just "on" in "what's on my calendar?" and resolved it to an arbitrary date) — the
# matched span must actually look like a date reference.
_DATE_TOKEN_RE = re.compile(
    r"\d|week|month|jan|feb|mar|apr|may\b|jun|jul|aug|sep|oct|nov|dec",
    re.IGNORECASE,
)


def _absolute_date_range(last_user: str, today) -> tuple[str, str, str] | None:
    """Deterministic parsing of explicit/absolute date references — "June 28th",
    "the 15th", "07/15", "in two weeks" — via the dateparser library instead of asking
    the model to do date arithmetic. This is what a mature parsing library is actually
    for; only genuinely context-dependent phrasing (no date words at all, e.g. "the one
    after that") should ever reach the AI fallback below."""
    settings = {
        "RELATIVE_BASE": datetime.combine(today, datetime.min.time()),
        "PREFER_DATES_FROM": "future",
    }
    try:
        matches = search_dates(last_user, settings=settings)
    except Exception:
        logger.exception("dateparser search_dates failed")
        return None
    if not matches:
        return None
    for matched_text, parsed in matches:
        if not _DATE_TOKEN_RE.search(matched_text):
            continue
        iso = parsed.date().isoformat()
        return iso, iso, matched_text.strip()
    return None


def _resolve_calendar_range(provider, last_user: str, today_str: str, convo_excerpt: str = "") -> tuple[str, str, str]:
    """Ask the model whether the message implies a specific date/range (a named day,
    "last week", "in March", etc.) and use that instead of the default forward window.
    This is a plain non-tool call purely for date parsing — it never decides whether
    to fetch calendar data, only how to scope what's already cached/queryable.

    convo_excerpt carries a few recent turns so elliptical follow-ups ("what about the
    day after?") can be resolved relative to what was just discussed (e.g. "tomorrow")
    instead of only ever seeing the latest message in isolation."""
    from datetime import date, timedelta

    today = date.fromisoformat(today_str)

    weekday_match = _weekday_range(last_user, today)
    if weekday_match:
        return weekday_match

    relative_match = _relative_day_range(last_user, today)
    if relative_match:
        return relative_match

    absolute_match = _absolute_date_range(last_user, today)
    if absolute_match:
        return absolute_match

    default_end = (today + timedelta(days=_DEFAULT_CALENDAR_WINDOW_DAYS)).isoformat()
    default = (today_str, default_end, f"today through next {_DEFAULT_CALENDAR_WINDOW_DAYS} days")

    convo_block = f"Recent conversation:\n{convo_excerpt}\n\n" if convo_excerpt else ""
    prompt = (
        f"Today's date is {today_str}. {convo_block}"
        f'The user\'s latest message is: "{last_user}"\n\n'
        "Does this message name or imply a specific date or date range — either directly "
        '("last week", "next month", "in March", a past date) or by referring back to the '
        'conversation (e.g. "what about the day after?" following a message about '
        '"tomorrow")? Resolve any such reference to an absolute date using the conversation '
        "above. Respond with raw JSON only, no markdown:\n"
        '{"start": "YYYY-MM-DD or null", "end": "YYYY-MM-DD or null"}\n'
        "Use null for both only if the message is generic with no implied range "
        "(e.g. \"what's on my calendar\", \"anything coming up\")."
    )
    try:
        raw = provider.chat("Respond with raw JSON only — no markdown, no extra text.",
                             [{"role": "user", "content": prompt}])
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()
        data = json.loads(cleaned)
        start, end = data.get("start"), data.get("end")
        if start and end:
            return start, end, f"{start} to {end}"
    except Exception:
        logger.exception("Calendar range extraction failed")
    return default


def _serialize_tasks(tasks: list[dict]) -> str:
    if not tasks:
        return "No active tasks."
    lines = []
    for t in tasks:
        parts = [f"[{t['id']}] {t['title']}"]
        if t.get("due_date"):
            parts.append(f"due:{t['due_date']}")
        if t.get("priority"):
            parts.append(f"priority:{t['priority']}")
        if t.get("fear_level"):
            parts.append(f"fear:{t['fear_level']}/5")
        if t.get("ambiguity_level"):
            parts.append(f"ambiguity:{t['ambiguity_level']}/5")
        if t.get("energy_type"):
            parts.append(f"energy:{t['energy_type']}")
        if t.get("estimated_effort"):
            parts.append(f"effort:{t['estimated_effort']}min")
        if t.get("description"):
            parts.append(f'desc:"{t["description"][:120]}"')
        lines.append("  " + " | ".join(parts))
    return "\n".join(lines)


class AIService:
    def __init__(self, provider: AIProvider):
        self.provider = provider

    def get_recommendations(self, db) -> dict:
        rows = db.execute("""
            SELECT id, title, description, status, priority, due_date,
                   fear_level, ambiguity_level, energy_type, estimated_effort
            FROM tasks
            WHERE status IN ('inbox', 'active')
            ORDER BY priority DESC, due_date ASC
        """).fetchall()
        tasks = [dict(r) for r in rows]

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        task_block = _serialize_tasks(tasks)

        user_msg = f"""Current time: {now}

Active tasks:
{task_block}

Return a JSON object with this exact shape:
{{
  "recommendations": [
    {{"id": <task_id>, "title": "<title>", "reason": "<1-2 sentence explanation>"}},
    ...
  ],
  "insight": "<optional 1-sentence observation about the overall task list>"
}}

Pick the top 3 tasks to work on right now. Only include tasks from the list above.
Respond with raw JSON only — no markdown, no extra text."""

        raw = self.provider.chat(_SYSTEM_PROMPT, [{"role": "user", "content": user_msg}])

        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return {"recommendations": [], "insight": raw}

    def explain_error(self, db, user_content: str, error: str) -> str:
        projects = db.execute("SELECT id, title, status FROM projects ORDER BY title").fetchall()
        project_lines = "\n".join(f"  [{p['id']}] {p['title']} ({p['status']})" for p in projects) or "  None"

        system = f"""{_SYSTEM_PROMPT}

CURRENT PROJECTS:
{project_lines}"""

        message = (
            f'The user said: "{user_content}"\n\n'
            f"You attempted to handle this but hit an error: {error}\n\n"
            "In one or two sentences, state specifically what was ambiguous or missing "
            "(e.g. multiple items share that name, or the item doesn't exist), then ask "
            "the user for the exact clarification needed — like an ID or a type (task vs project). "
            "Do not say 'temporary limitations' or blame the system. Do not show the raw error."
        )
        try:
            return self.provider.chat(system, [{"role": "user", "content": message}])
        except Exception:
            return "Something went wrong on my end. Could you clarify what you meant?"

    def chat(self, db, messages: list[dict], client_tz: str | None = None) -> str:
        rows = db.execute("""
            SELECT id, title, status, priority, due_date, fear_level, ambiguity_level, energy_type, project_id
            FROM tasks
            WHERE status IN ('inbox', 'active')
            ORDER BY priority DESC, due_date ASC
        """).fetchall()
        tasks = [dict(r) for r in rows]

        projects = db.execute("SELECT id, title, status FROM projects ORDER BY title").fetchall()
        project_lines = "\n".join(f"  [{p['id']}] {p['title']} ({p['status']})" for p in projects) or "  None"

        calendars = db.execute("SELECT id, name FROM calendars ORDER BY name").fetchall()
        cal_lines = "\n".join(f"  [{c['id']}] {c['name']}" for c in calendars) or "  None"

        task_block = _serialize_tasks(tasks)
        now_utc = datetime.now(timezone.utc)
        today_str = now_utc.strftime("%Y-%m-%d")
        if client_tz:
            try:
                from zoneinfo import ZoneInfo
                today_str = now_utc.astimezone(ZoneInfo(client_tz)).strftime("%Y-%m-%d")
            except Exception:
                logger.warning("Invalid client timezone %r, falling back to UTC", client_tz)
        base_system = f"""{_SYSTEM_PROMPT}

CURRENT DATE: {today_str} (use this as "today" for all relative date reasoning)

CURRENT PROJECTS:
{project_lines}

LOCAL CALENDARS:
{cal_lines}

CURRENT TASKS:
{task_block}"""

        # Passive RAG: retrieve vault context unless this is a pure task mutation.
        # base_system is saved separately so that if a tool call is made in round 1,
        # subsequent rounds use base_system (no injected context) to avoid the
        # Gemini bug where system-context + tool-result together produce empty output.
        system = base_system
        recent_user_msgs = [m["content"] for m in reversed(messages) if m["role"] == "user"][:3]
        last_user = recent_user_msgs[0] if recent_user_msgs else ""

        # A follow-up like "what about the day after?" won't itself match any calendar
        # keyword — check the last couple of user turns so a calendar conversation stays
        # "sticky" for elliptical continuations instead of dropping context every message.
        calendar_triggered = any(_CALENDAR_WORDS.search(u) for u in recent_user_msgs[:2])

        if last_user and calendar_triggered:
            try:
                convo_excerpt = "\n".join(
                    f"{m['role']}: {m['content']}" for m in messages[-6:] if m.get("content")
                )
                range_start, range_end, range_label = _resolve_calendar_range(
                    self.provider, last_user, today_str, convo_excerpt
                )
                upcoming_rows = db.execute("""
                    SELECT e.id, e.title, e.start_datetime, e.end_datetime,
                           e.location, c.name as cal_name
                    FROM events e JOIN calendars c ON e.calendar_id = c.id
                    WHERE substr(e.start_datetime, 1, 10) >= ?
                      AND substr(e.start_datetime, 1, 10) <= ?
                    ORDER BY e.start_datetime
                """, (range_start, range_end)).fetchall()
                lines = []
                for e in upcoming_rows:
                    e = dict(e)
                    line = f"  [{e['id']}] {e['title']} | {e['start_datetime'][:16]}"
                    if e.get("end_datetime"):
                        line += f" → {e['end_datetime'][:16]}"
                    if e.get("location"):
                        line += f" | loc:{e['location']}"
                    line += f" | cal:{e['cal_name']}"
                    lines.append(line)

                from services.calendar.gcal_service import get_cached_upcoming
                for e in get_cached_upcoming():
                    start = e.get("start") or ""
                    if not (range_start <= start[:10] <= range_end):
                        continue
                    line = f"  {e['title']} | {start[:16]}"
                    if e.get("end"):
                        line += f" → {e['end'][:16]}"
                    if e.get("location"):
                        line += f" | loc:{e['location']}"
                    line += f" | cal:{e.get('calendar_name', 'Google Calendar')} (Google, read-only)"
                    lines.append(line)

                cal_ctx = "\n".join(lines) if lines else "  None"

                # Tools stay available either way — never guess read vs. write from the
                # message text. A misclassified write landing in a no-tools path is how
                # the model ended up fabricating "I've created your event" with nothing
                # behind it. Let the model's own tool-calling judgment decide.
                system = (
                    f"{base_system}\n\n"
                    f"EVENTS ({range_label}):\n{cal_ctx}\n\n"
                    "[Calendar data injected above — answer directly from it if this is a "
                    "read question. Do not call list_events for this same range; it's "
                    "already here. If the user is asking you to create/update something, "
                    "use the appropriate tool — never claim you did before the tool call "
                    "confirms it.]"
                )
            except Exception:
                logger.exception("Passive calendar injection failed")

        if last_user and not _should_skip_rag(last_user):
            try:
                from services.rag.retriever import retrieve
                from services.rag.injector import build_context
                chunks = retrieve(last_user, k=5)
                ctx = build_context(chunks)
                if ctx:
                    system = (
                        f"{system}\n\n{ctx}\n\n"
                        "[Note: vault context already retrieved above — "
                        "answer directly from it rather than calling search_vault.]"
                    )
            except Exception:
                logger.exception("Passive RAG retrieval failed")

        # Agentic loop: keep executing tool calls until the model stops.
        # Cap at 5 rounds to prevent runaway chains.
        internal = list(messages)
        last_tool_results: list[tuple[str, dict, dict]] = []
        for _ in range(5):
            reply_text, tool_calls = self.provider.chat_with_tools(system, internal, TOOLS)

            if not tool_calls:
                if reply_text:
                    return reply_text
                if last_tool_results:
                    return "\n".join(
                        _synthesize_tool_confirmation(n, a, r) for n, a, r in last_tool_results
                    )
                return ""

            # search_vault tool responses cause Gemini 2.5 Flash Lite to emit 0 output
            # tokens in the follow-up round (confirmed via costs.log: out=0 every time).
            # Bypass the tool-response format entirely: perform the retrieval here,
            # inject the results via the system prompt, and answer with a plain chat()
            # call (no tools) — the same injection path used by passive RAG.
            if len(tool_calls) == 1 and tool_calls[0]["name"] == "search_vault":
                tc = tool_calls[0]
                try:
                    from services.rag.retriever import retrieve
                    from services.rag.injector import build_context
                    query = tc["arguments"]["query"]
                    col = tc["arguments"].get("collection")
                    k_val = min(int(tc["arguments"].get("k", 5)), 10)
                    chunks = retrieve(query, k=k_val, collections=[col] if col else None)
                    ctx = build_context(chunks)
                except Exception:
                    logger.exception("search_vault injection fallback failed")
                    ctx = ""
                fresh_system = f"{base_system}\n\n{ctx}" if ctx else base_system
                return self.provider.chat(fresh_system, messages) or ""

            if len(tool_calls) == 1 and tool_calls[0]["name"] == "list_events":
                tc = tool_calls[0]
                result = _execute_tool(db, "list_events", tc["arguments"])
                lines = []
                events = result.get("events", [])
                deadlines = result.get("task_deadlines", [])
                gcal_lines = []
                req_start = tc["arguments"].get("start_date", "")
                req_end = tc["arguments"].get("end_date", "")
                if req_start and req_end:
                    from services.calendar.gcal_service import get_cached_upcoming
                    for e in get_cached_upcoming():
                        start = e.get("start") or ""
                        if not (req_start <= start[:10] <= req_end):
                            continue
                        line = f"  {e['title']} | {start[:16]}"
                        if e.get("end"):
                            line += f" → {e['end'][:16]}"
                        if e.get("location"):
                            line += f" | loc:{e['location']}"
                        line += f" | cal:{e.get('calendar_name', 'Google Calendar')} (Google, read-only)"
                        gcal_lines.append(line)
                if events or gcal_lines:
                    lines.append("CALENDAR EVENTS:")
                    for e in events:
                        line = f"  [{e['id']}] {e['title']} | {e['start_datetime'][:16]}"
                        if e.get("end_datetime"):
                            line += f" → {e['end_datetime'][:16]}"
                        if e.get("calendar_name"):
                            line += f" | cal:{e['calendar_name']}"
                        if e.get("location"):
                            line += f" | loc:{e['location']}"
                        lines.append(line)
                    lines.extend(gcal_lines)
                else:
                    lines.append("CALENDAR EVENTS: none in this range")
                if deadlines:
                    lines.append("TASK DEADLINES:")
                    for t in deadlines:
                        lines.append(f"  [{t['id']}] {t['title']} | due:{t['due_date']} | priority:{t['priority']}")
                ctx = "\n".join(lines)
                fresh_system = f"{base_system}\n\n{ctx}"
                return self.provider.chat(fresh_system, messages) or ""

            if len(tool_calls) == 1 and tool_calls[0]["name"] == "read_document":
                tc = tool_calls[0]
                result = _execute_tool(db, "read_document", tc["arguments"])
                if result.get("found"):
                    path = tc["arguments"]["path"]
                    ctx = (
                        f"DOCUMENT CONTEXT ({path}):\n"
                        f"{result['content']}\n"
                        "---"
                    )
                else:
                    ctx = (
                        f"[read_document failed for '{tc['arguments']['path']}': "
                        f"{result.get('error', 'unknown error')}. "
                        f"Tell the user the file was not found in the vault.]"
                    )
                fresh_system = f"{base_system}\n\n{ctx}"
                return self.provider.chat(fresh_system, messages) or ""

            # Drop passive RAG context for subsequent rounds — tool results already
            # provide the context and the combined injection causes empty Gemini responses.
            system = base_system

            internal.append({
                "role": "assistant",
                "content": reply_text or None,
                "tool_calls": [
                    {
                        "id": tc["call_id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["arguments"]),
                        },
                    }
                    for tc in tool_calls
                ],
            })

            last_tool_results = []
            for tc in tool_calls:
                result = _execute_tool(db, tc["name"], tc["arguments"])
                last_tool_results.append((tc["name"], tc["arguments"], result))
                internal.append({
                    "role": "tool",
                    "tool_call_id": tc["call_id"],
                    "content": json.dumps(result),
                })

        final_reply = self.provider.chat(base_system, internal) or ""
        if final_reply:
            return final_reply
        if last_tool_results:
            return "\n".join(
                _synthesize_tool_confirmation(n, a, r) for n, a, r in last_tool_results
            )
        return ""
