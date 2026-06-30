import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

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

IMPORTANT RULES:
- Do exactly what the user asks — no more. Do not create extra tasks, subtasks, or related items unless explicitly requested.
- Only claim an action was taken after a tool call confirms it succeeded.
- Keep replies short. Confirm what you did in one sentence. Do not list attributes or explain your work unless asked.
- Never volunteer information, suggestions, or follow-up actions unless the user asks.
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

_TASK_WRITE_FIELDS = {
    "title", "description", "priority", "status", "energy_type",
    "due_date", "project_id", "fear_level", "ambiguity_level", "estimated_effort",
}

# Passive RAG skip: patterns that signal a pure task/project mutation command
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
        base_system = f"""{_SYSTEM_PROMPT}

CURRENT PROJECTS:
{project_lines}

CURRENT TASKS:
{task_block}"""

        # Passive RAG: retrieve vault context unless this is a pure task mutation.
        # base_system is saved separately so that if a tool call is made in round 1,
        # subsequent rounds use base_system (no injected context) to avoid the
        # Gemini bug where system-context + tool-result together produce empty output.
        system = base_system
        last_user = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )
        if last_user and not _should_skip_rag(last_user):
            try:
                from services.rag.retriever import retrieve
                from services.rag.injector import build_context
                chunks = retrieve(last_user, k=5)
                ctx = build_context(chunks)
                if ctx:
                    system = (
                        f"{base_system}\n\n{ctx}\n\n"
                        "[Note: vault context already retrieved above — "
                        "answer directly from it rather than calling search_vault.]"
                    )
            except Exception:
                logger.exception("Passive RAG retrieval failed")

        # Agentic loop: keep executing tool calls until the model stops.
        # Cap at 5 rounds to prevent runaway chains.
        internal = list(messages)
        for _ in range(5):
            reply_text, tool_calls = self.provider.chat_with_tools(system, internal, TOOLS)

            if not tool_calls:
                return reply_text or ""

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

            for tc in tool_calls:
                result = _execute_tool(db, tc["name"], tc["arguments"])
                internal.append({
                    "role": "tool",
                    "tool_call_id": tc["call_id"],
                    "content": json.dumps(result),
                })

        return self.provider.chat(base_system, internal) or ""
