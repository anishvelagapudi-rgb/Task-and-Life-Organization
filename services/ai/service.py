import json
import uuid
from datetime import datetime, timezone
from .provider import AIProvider


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

IMPORTANT RULES:
- Do exactly what the user asks — no more. Do not create extra tasks, subtasks, or related items unless explicitly requested.
- Only claim an action was taken after a tool call confirms it succeeded.
- Keep replies short. Confirm what you did in one sentence. Do not list attributes or explain your work unless asked.
- Never volunteer information, suggestions, or follow-up actions unless the user asks.
"""

# Tools the model can call. Kept here so provider implementations stay generic.
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
]

_TASK_WRITE_FIELDS = {
    "title", "description", "priority", "status", "energy_type",
    "due_date", "project_id", "fear_level", "ambiguity_level", "estimated_effort",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _execute_tool(db, name: str, args: dict) -> dict:
    """Run a tool call against the DB and return a result dict the model can read."""
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

    return {"success": False, "error": f"Unknown tool: {name}"}


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
        """
        Called when chat() fails. Makes a plain (no-tool) call so the model can
        explain what likely went wrong and ask the user for clarification.
        """
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

    def chat(self, db, messages: list[dict]) -> str:
        rows = db.execute("""
            SELECT id, title, status, priority, due_date, fear_level, ambiguity_level, energy_type, project_id
            FROM tasks
            WHERE status IN ('inbox', 'active')
            ORDER BY priority DESC, due_date ASC
        """).fetchall()
        tasks = [dict(r) for r in rows]

        projects = db.execute("SELECT id, title, status FROM projects ORDER BY title").fetchall()
        project_lines = "\n".join(f"  [{p['id']}] {p['title']} ({p['status']})" for p in projects) or "  None"

        task_block = _serialize_tasks(tasks)
        system = f"""{_SYSTEM_PROMPT}

CURRENT PROJECTS:
{project_lines}

CURRENT TASKS:
{task_block}"""

        # Agentic loop: keep executing tool calls until the model stops calling tools.
        # Cap at 5 rounds to prevent runaway chains.
        internal = list(messages)
        for _ in range(5):
            reply_text, tool_calls = self.provider.chat_with_tools(system, internal, TOOLS)

            if not tool_calls:
                return reply_text or ""

            # Add the assistant's tool-calling turn to the internal history so the
            # next call has full context of what was invoked and why.
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

            for tc in tool_calls:
                result = _execute_tool(db, tc["name"], tc["arguments"])
                internal.append({
                    "role": "tool",
                    "tool_call_id": tc["call_id"],
                    "content": json.dumps(result),
                })

        # Fallback if we somehow hit the round limit — ask for a plain text summary
        return self.provider.chat(system, internal) or ""
