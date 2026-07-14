import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import dateparser
from dateparser.search import search_dates

from db import enforce_recurring_invariant, enforce_no_self_parent
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
- recurring: null (one-off) | "daily" | "weekly" — a recurring task auto-resets to
  not-done at the start of the next day/week after it's completed. Recurring tasks
  must NEVER have a due_date — if you set recurring on create_task or update_task,
  any due_date you also pass is ignored and cleared server-side. Only set recurring
  when the user explicitly describes a repeating habit/chore, not for one-off tasks.

These 4 psychological fields (fear_level, ambiguity_level, energy_type,
estimated_effort) are never shown to the user as a data-entry form by default — you
are expected to infer them yourself when creating or updating a task, based on how
the user describes it (tone, hedging, vagueness about what "done" looks like, stated
or implied deadline pressure, etc.). Whenever you set or change any of these 4 fields
via create_task or update_task, you MUST also pass psych_reasoning: a short 1-2
sentence explanation of why you inferred those specific values. This is shown to the
user directly next to the fields so they can see and correct your reasoning — never
skip it when you set one of these fields, and never fabricate a reason if you didn't
actually infer anything (e.g. don't set psych_reasoning if the user gave you an
explicit number for one of these and you were not inferring anything).

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
- Do NOT cite source filenames or paths inline in your answer text (e.g. do not write things
  like "According to projects/cs101.md..."). The app surfaces sources separately as a footnote
  below your reply — just answer naturally using the vault content, without naming where it
  came from in the text itself.
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
- Use find_connections when the user asks how a note relates to their other notes, what
  connects to something, or wants a different angle on how they're thinking about a topic —
  it surfaces non-obvious cross-folder links, not simple keyword/topic matches (that's what
  search_vault is for).

CALENDAR:
The user has a local calendar and, optionally, a connected Google Calendar. When a calendar
question is asked, an UPCOMING EVENTS block is injected into context covering both — local
events (editable) and Google Calendar events tagged "(Google, read-only)". Answer directly
from that block; you have no tool to fetch Google Calendar yourself, so never call a tool
to check it and never claim you lack access to it.
Use list_events to look up local events beyond the injected 14-day window. Use create_event
to add new events to LOCAL calendars only. Use update_event to modify, and delete_event to
remove, existing local events.
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
- You ALWAYS have every one of the user's tasks (every status — inbox, active, done, blocked, archived) already injected into context as a CURRENT TASKS block. There is no separate tool for listing tasks — when asked to list, show, count, or summarize tasks, answer directly from that block. Never say you're unable to list tasks, lack access to them, or ask what to name a task when the user is asking to see existing tasks rather than create one.
- Never create a task, project, event, or note as a substitute for answering a listing/query request you're unsure how to fulfill. If you don't have a tool for what's being asked, say so plainly instead of taking an unrelated action.
- Never ask the user to supply a task/project/event ID, under any circumstances, for any single-item or multi-item request. You already have every task's id/title/status and every project's id/title/status in the CURRENT TASKS/CURRENT PROJECTS blocks below, and events are one list_events call away — resolve which item(s) a name refers to yourself by matching against those. Asking a person to look up and paste back an internal database ID is never an acceptable substitute for you doing that lookup.
- The ONLY time you should ask the user a clarifying question before acting on a named reference (e.g. "add a subtask to my English Class HW", "mark the dentist appointment done") is when that name genuinely matches more than one existing item and you cannot tell which one is meant from context. In that case, ask by describing the candidates in plain language (title, status, due date, project) so the user can pick — never by asking for an ID, and never as a reflexive first step when there's really only one plausible match.

BULK OR AMBIGUOUS REFERENCES ("all tasks", "all inbox tasks", "these three", "the ones I completed today", "add these 5 tasks", "all my projects"):
- Creating multiple items in one message (e.g. "add tasks A, B, and C" or "create 5 tasks for my trip") is not a bulk-delete situation — just call create_task/create_event once per item in the same turn, back to back, with no confirmation step and no asking one at a time.
- For any bulk action that updates more than one task, just call update_task once per matching task — do not ask for confirmation yourself and do not hold back the calls.
- delete_tasks_matching ONLY applies when the request is about deleting TASKS. Call it when tasks are referred to by a shared keyword/prefix in the title ("all tasks that start with TEST", "every task with 'draft' in the title") or by blanket task scope ("delete all my tasks", "delete all tasks") — call it ONCE with that pattern (or match_type "all" for the blanket-task case) instead of picking matching tasks out yourself, and do not also call delete_task for the same items. Picking matches out of the CURRENT TASKS block yourself is unreliable on longer lists and can silently miss real matches; delete_tasks_matching resolves matches server-side and cannot miss one.
- CRITICAL — do not let delete_tasks_matching leak into requests about a different resource type: "delete all projects" means projects only, "delete all events" means events only. The noun after "all"/"my" is what determines scope — only call delete_tasks_matching when that noun is "tasks" (or the request is unambiguously about tasks). A request to delete all projects must call delete_project per project and must NOT also call delete_tasks_matching — tasks were never mentioned and must not be touched, even though both requests start with the same "delete all ___" shape.
- Only fall back to calling delete_task individually per task when the user names distinct tasks by their own separate titles (e.g. "delete 'Buy milk' and 'Pay rent'") — there's no shared pattern to give delete_tasks_matching there.
- For deleting multiple PROJECTS or EVENTS on their own, or a mix of tasks/projects/events the user explicitly names together, call delete_project/delete_event/delete_task once per matching item — do not ask the user for confirmation yourself and do not hold back the calls.
- A system-level safeguard automatically intercepts any request (via delete_task, delete_tasks_matching, delete_project, and/or delete_event, in any combination) that would delete more than one item total and asks the user to confirm before anything is actually removed, so you don't need to ask for confirmation yourself — attempting the delete calls normally is the correct behavior even for "delete all tasks" or "delete all my projects".
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
                    "due_date":         {"type": "string", "description": "YYYY-MM-DD. Ignored/cleared if recurring is also set."},
                    "project_id":       {"type": "integer"},
                    "fear_level":       {"type": "integer", "minimum": 1, "maximum": 5},
                    "ambiguity_level":  {"type": "integer", "minimum": 1, "maximum": 5},
                    "estimated_effort": {"type": "integer", "description": "Minutes"},
                    "recurring":        {"type": "string", "enum": ["daily", "weekly"], "description": "Only for repeating habits/chores. Never combine with due_date."},
                    "parent_task_id":   {"type": "string", "description": "id of an existing task to nest this one under as a subtask. Omit for a root-level task."},
                    "psych_reasoning":  {
                        "type": "string",
                        "description": (
                            "Required whenever you set fear_level, ambiguity_level, energy_type, "
                            "or estimated_effort on this call: a short (1-2 sentence) explanation "
                            "of why you inferred those specific values. Shown to the user next to "
                            "the fields so they can see and correct your reasoning."
                        ),
                    },
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_project",
            "description": "Update one or more fields on an existing project (rename, change description, or change status).",
            "parameters": {
                "type": "object",
                "properties": {
                    "id":          {"type": "string"},
                    "title":       {"type": "string"},
                    "description": {"type": "string"},
                    "status":      {"type": "string", "enum": ["active", "paused", "done"]},
                },
                "required": ["id"],
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
            "name": "delete_tasks_matching",
            "description": (
                "Delete every task whose title matches a text pattern — matched "
                "server-side against every task, not by you picking matches out of the "
                "CURRENT TASKS block yourself. Use this whenever the user refers to a "
                "group of tasks by a shared keyword/prefix or by blanket scope, rather "
                "than by naming distinct individual titles — e.g. \"delete all tasks "
                "that start with TEST\", \"delete all my SMOKETEST tasks\", \"remove "
                "every task with 'draft' in the title\", \"delete all my tasks\". Still "
                "goes through the same confirmation step as any other multi-item delete "
                "before anything is actually removed — call this once and stop; do not "
                "also call delete_task for the same items."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Text to match against task titles, case-insensitive. Not required when match_type is \"all\".",
                    },
                    "match_type": {
                        "type": "string",
                        "enum": ["contains", "starts_with", "all"],
                        "description": (
                            "\"starts_with\" for \"begins with X\"/\"prefixed X\" phrasing, "
                            "\"contains\" (default) for \"has X in the title\"/generic keyword "
                            "phrasing, \"all\" for literally every task (\"delete all my "
                            "tasks\", \"delete everything\") — pattern is ignored when \"all\"."
                        ),
                    },
                },
                "required": [],
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
                    "due_date":         {"type": "string", "description": "YYYY-MM-DD. Ignored/cleared if recurring is also set."},
                    "project_id":       {"type": "integer"},
                    "fear_level":       {"type": "integer", "minimum": 1, "maximum": 5},
                    "ambiguity_level":  {"type": "integer", "minimum": 1, "maximum": 5},
                    "estimated_effort": {"type": "integer", "description": "Minutes"},
                    "recurring":        {"type": "string", "enum": ["daily", "weekly"], "description": "Only for repeating habits/chores. Never combine with due_date."},
                    "parent_task_id":   {"type": "string", "description": "id of an existing task to nest this one under as a subtask. Set to make this task a subtask of another."},
                    "psych_reasoning":  {
                        "type": "string",
                        "description": (
                            "Required whenever you change fear_level, ambiguity_level, energy_type, "
                            "or estimated_effort on this call: a short (1-2 sentence) explanation "
                            "of why you inferred those specific values. Shown to the user next to "
                            "the fields so they can see and correct your reasoning."
                        ),
                    },
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
            "name": "delete_event",
            "description": "Permanently delete an event from a local calendar by ID. Only works on local events, never on Google Calendar (read-only).",
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
    {
        "type": "function",
        "function": {
            "name": "find_connections",
            "description": (
                "Find non-obvious connections between a vault note and other notes — "
                "moderate cross-folder semantic overlap the user likely hasn't noticed, "
                "as opposed to straightforward similarity search (use search_vault for that). "
                "Use when the user asks how notes relate to each other, asks what connects "
                "to a specific note, or wants to be challenged on how they're thinking about "
                "something."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Vault-relative path of the note to find connections for (e.g. 'journal/2026-06-20.md')",
                    },
                    "k": {"type": "integer", "description": "Max connections to return (1-10, default 5)"},
                },
                "required": ["path"],
            },
        },
    },
]

_EVENT_WRITE_FIELDS = {
    "title", "description", "start_datetime", "end_datetime",
    "all_day", "location", "calendar_id",
}

_PROJECT_WRITE_FIELDS = {"title", "description", "status"}

_TASK_WRITE_FIELDS = {
    "title", "description", "priority", "status", "energy_type",
    "due_date", "project_id", "fear_level", "ambiguity_level", "estimated_effort",
    "psych_reasoning", "recurring", "parent_task_id",
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


_TASK_LISTING_RE = re.compile(
    r"\b(list|show|view|see|display)\b.{0,20}\btasks?\b"
    r"|\btasks?\b.{0,25}\b(list|do i have|are there|remain(ing)?|left|pending|available)\b"
    r"|\bmy tasks?\b"
    r"|\btask list\b"
    r"|\bwhat tasks?\b",
    re.IGNORECASE,
)


def _is_pure_task_listing(message: str) -> bool:
    """Gemini 2.5 Flash Lite, when offered create_task/update_task/delete_task as
    function-calling tools, reliably claims it "can't list tasks" for a plain listing
    request — it over-indexes on tool affordances (no tool literally named "list") and
    disregards the CURRENT TASKS block already in its context, even with explicit
    instructions to the contrary right next to that block. Verified: the identical
    context/system-prompt answers correctly when tools are simply omitted from the
    request. Same class of documented Gemini function-calling flakiness as the
    search_vault/list_events single-tool-call shortcuts elsewhere in this file —
    handled the same way: detect the narrow case and skip chat_with_tools entirely."""
    return bool(_TASK_LISTING_RE.search(message))


_AFFIRM_PHRASES = {
    "y", "yes", "yep", "yeah", "yup", "sure", "ok", "okay", "confirm", "confirmed",
    "do it", "go ahead", "proceed", "sounds good", "yes please", "yes, please",
}
_AFFIRM_DELETE_RE = re.compile(r"^delete (them|it|those|all)$", re.IGNORECASE)


def _is_affirmative(text: str) -> bool:
    normalized = (text or "").strip().rstrip(".!,").strip().lower()
    return normalized in _AFFIRM_PHRASES or bool(_AFFIRM_DELETE_RE.match(normalized))


# Every delete-capable tool shares one confirmation gate, keyed by a "kind" prefix
# (task/project/event) rather than one gate per tool — "delete all tasks" and "delete
# all projects" are the same failure mode (see the original incident this whole
# mechanism exists for), so a tool added later that deletes something is a one-line
# addition here, not a parallel gate to remember to build.
_DELETE_TOOL_KIND = {"delete_task": "task", "delete_project": "project", "delete_event": "event"}
_KIND_TO_DELETE_TOOL = {v: k for k, v in _DELETE_TOOL_KIND.items()}
_KIND_LABEL = {"task": "tasks", "project": "projects", "event": "events"}
_KIND_LABEL_SINGULAR = {"task": "task", "project": "project", "event": "event"}


def _kind_label(kind: str, count: int) -> str:
    return _KIND_LABEL_SINGULAR[kind] if count == 1 else _KIND_LABEL[kind]


def _resolve_pattern_tasks(tasks: list[dict], pattern: str, match_type: str) -> list[dict]:
    """Deterministic, exhaustive title matching against every task — the server-side
    resolver behind delete_tasks_matching, built specifically so completeness never
    depends on a model correctly recalling every match from the CURRENT TASKS text
    block. Verified empirically: asked to delete "all tasks that begin with TEST"
    across a 13-task list, the model picked 11 and silently dropped 2 — a plain Python
    scan over the same rows this function receives cannot skip a row the way skimming a
    long serialized block can. Case-insensitive; `tasks` is the same full (every status)
    list already loaded once per chat() call for the CURRENT TASKS block, so this adds
    no extra query."""
    if match_type == "all":
        return list(tasks)
    needle = (pattern or "").strip().lower()
    if not needle:
        return []
    if match_type == "starts_with":
        return [t for t in tasks if t["title"].lower().startswith(needle)]
    return [t for t in tasks if needle in t["title"].lower()]

# [ref: kind:id,kind:id,...|hmac] — the hmac binds the (kind, id) list to this server
# process (keyed off FLASK_SECRET_KEY) so the marker can't be forged by untrusted text
# the model might reproduce verbatim from an indirect prompt-injection source (e.g. a
# malicious vault note pulled in via passive RAG/search_vault/read_document). Without
# this, any assistant-role text ending in a bracket pattern that merely *looks* like a
# pending delete, followed by an unrelated affirmative user reply, could otherwise be
# read as genuine confirmation and delete real data with no tool-calling round at all.
_PENDING_DELETE_REF_RE = re.compile(
    r"\[ref:\s*([a-z]+:[0-9a-fA-F-]+(?:,[a-z]+:[0-9a-fA-F-]+)*)\|([0-9a-f]+)\]\s*$"
)


def _sign_delete_refs(refs: list[str]) -> str:
    key = os.environ.get("FLASK_SECRET_KEY", "").encode()
    return hmac.new(key, ",".join(refs).encode(), hashlib.sha256).hexdigest()[:24]


def strip_pending_delete_marker(text: str) -> str:
    """Public helper for callers (app.py's browser chat route) that persist/display AI
    replies — strips the internal [ref: ...] marker from what's shown to the user while
    leaving the raw text (marker included) as what's actually stored/round-tripped, since
    _pending_bulk_delete_refs needs the marker to survive in conversation history for the
    confirmation flow to work."""
    return _PENDING_DELETE_REF_RE.sub("", text or "").rstrip()


def has_pending_delete_marker(text: str) -> bool:
    """Public helper so callers can tell a bulk-delete confirmation reply apart from an
    ordinary one *without* re-exposing the raw [ref: ...] marker itself (which stays
    server-side only, per strip_pending_delete_marker). Lets a frontend show something
    more prominent than a plain chat bubble — a real confirm dialog — for exactly the
    turn where a "confirm"/"cancel" reply actually matters, while the already-stripped
    reply text (the human-readable "This will permanently delete N ..." sentence) is
    reused as-is for that dialog's message, so there's no duplicated formatting logic."""
    return bool(_PENDING_DELETE_REF_RE.search(text or ""))


def _pending_bulk_delete_refs(messages: list[dict]) -> list[tuple[str, str]] | None:
    """Detects the second half of a bulk-delete confirmation round trip. Deliberately
    stateless (mirrors the rest of this module/ai_routes.py's stateless design) — the
    "pending" state lives entirely in the signed [ref: kind:id,kind:id,...|hmac] marker
    this module itself appended to its own prior confirmation reply, which the client
    resends verbatim as part of `messages`. Only fires on an exact affirmative reply
    immediately following a marker with a valid signature — see _confirm_bulk_delete_reply
    for where the marker is produced, and the multi-delete interception in chat() below
    for why this exists: the model cannot be trusted to reliably hold off on calling a
    delete tool itself for a bulk request (verified empirically — Gemini 2.5 Flash Lite
    executed a 2-task delete immediately despite explicit prompt instructions to ask
    first). Returns a list of (kind, id) pairs."""
    if len(messages) < 2:
        return None
    last, prev = messages[-1], messages[-2]
    if last.get("role") != "user" or prev.get("role") != "assistant":
        return None
    if not _is_affirmative(last.get("content") or ""):
        return None
    m = _PENDING_DELETE_REF_RE.search(prev.get("content") or "")
    if not m:
        return None
    raw_refs, sig = m.group(1).split(","), m.group(2)
    if not hmac.compare_digest(_sign_delete_refs(raw_refs), sig):
        return None
    refs = []
    for ref in raw_refs:
        kind, _, id_ = ref.partition(":")
        if kind not in _KIND_TO_DELETE_TOOL or not id_:
            return None
        refs.append((kind, id_))
    return refs


def _resolve_delete_title(db, kind: str, id_: str, tasks_by_id: dict, projects_by_id: dict) -> str | None:
    if kind == "task":
        return tasks_by_id.get(id_, {}).get("title")
    if kind == "project":
        return projects_by_id.get(id_, {}).get("title")
    row = db.execute("SELECT title FROM events WHERE id = ?", (id_,)).fetchone()
    return row["title"] if row else None


def _confirm_bulk_delete_reply(
    db, tasks_by_id: dict, projects_by_id: dict, refs: list[tuple[str, str]]
) -> str:
    """Builds the confirmation prompt for a would-be multi-item delete, ending in a
    signed [ref: ...] marker that _pending_bulk_delete_refs parses back out and verifies
    on the user's next turn. Only refs that actually resolve to a real, currently-existing
    row are included — both in the displayed title list and in what gets signed/deleted
    on confirm — so the displayed count and titles always match exactly what confirming
    would do, and a stale/bogus id can't sneak into the marker.

    When the resolved set spans more than one kind, the message groups by kind
    ("13 tasks and 6 projects") instead of a single flat "19 items" count/list —
    surfaced by testing: a request scoped to only one kind ("delete all projects") can
    still incorrectly resolve items of another kind too (a prompt-adherence failure,
    not something this function can prevent), and a flat undifferentiated list makes
    that kind of scope leak easy to skim past. Grouping by kind is a second, independent
    layer behind the prompt fix — the confirmation gate is exactly the place a human
    catches a wrong proposed scope before anything is actually deleted, so making the
    wrongness visible here matters as much as making the model less likely to do it."""
    resolved = []
    by_kind: dict[str, list[str]] = {}
    for kind, id_ in refs:
        title = _resolve_delete_title(db, kind, id_, tasks_by_id, projects_by_id)
        if title is None:
            continue
        resolved.append((kind, id_))
        by_kind.setdefault(kind, []).append(title)
    sig_refs = [f"{kind}:{id_}" for kind, id_ in resolved]
    sig = _sign_delete_refs(sig_refs)

    if len(by_kind) <= 1:
        kind = next(iter(by_kind)) if by_kind else None
        noun = _kind_label(kind, len(resolved)) if kind else "items"
        titles = [f'"{t}"' for titles in by_kind.values() for t in titles]
        summary = f"{len(resolved)} {noun}: {', '.join(titles)}"
    else:
        breakdown = " and ".join(
            f"{len(titles)} {_kind_label(kind, len(titles))}" for kind, titles in sorted(by_kind.items())
        )
        per_kind = "; ".join(
            f"{_kind_label(kind, len(titles))} — " + ", ".join(f'"{t}"' for t in titles)
            for kind, titles in sorted(by_kind.items())
        )
        summary = f"{len(resolved)} items ({breakdown}): {per_kind}"

    return (
        f"This will permanently delete {summary}. "
        f'Reply "confirm" to proceed, or say anything else to cancel.\n\n'
        f"[ref: {','.join(sig_refs)}|{sig}]"
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_TOOL_CONFIRM_VERBS = {
    "create_project": "Created project",
    "update_project": "Updated project",
    "create_task": "Created task",
    "delete_project": "Deleted project",
    "delete_task": "Deleted task",
    "update_task": "Updated task",
    "create_event": "Created",
    "update_event": "Updated",
    "delete_event": "Deleted event",
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
        enforce_recurring_invariant(fields)
        if fields["status"] == "done":
            fields["completed_at"] = _now()
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

    if name == "update_project":
        project_id = args["id"]
        updates = {k: v for k, v in args.items() if k in _PROJECT_WRITE_FIELDS}
        if not updates:
            return {"success": False, "error": "No valid fields to update"}
        updates["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        cur = db.execute(f"UPDATE projects SET {set_clause} WHERE id = ?", [*updates.values(), project_id])
        db.commit()
        return {"success": cur.rowcount > 0, "id": project_id}

    if name == "delete_project":
        cur = db.execute("DELETE FROM projects WHERE id = ?", (args["id"],))
        db.commit()
        return {"success": cur.rowcount > 0, "id": args["id"]}

    if name == "delete_task":
        cur = db.execute("DELETE FROM tasks WHERE id = ?", (args["id"],))
        db.commit()
        return {"success": cur.rowcount > 0, "id": args["id"]}

    if name == "update_task":
        task_id = args["id"]
        updates = {k: v for k, v in args.items() if k in _TASK_WRITE_FIELDS}
        if not updates:
            return {"success": False, "error": "No valid fields to update"}
        existing_row = db.execute("SELECT recurring, status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        enforce_recurring_invariant(updates, existing_row["recurring"] if existing_row else None)
        enforce_no_self_parent(updates, task_id)
        # completed_at isn't a model-writable field (see _TASK_WRITE_FIELDS) — derive it
        # from the status transition instead, mirroring app.py's update_task route. Without
        # this, the AI could mark a task 'done' with no completed_at ever set, which
        # silently breaks recurring-task auto-reset (reset_due_recurring_tasks requires
        # completed_at IS NOT NULL to consider a task eligible).
        existing_status = existing_row["status"] if existing_row else None
        if "status" in updates:
            if updates["status"] == "done" and existing_status != "done":
                updates["completed_at"] = _now()
            elif updates["status"] != "done":
                updates["completed_at"] = None
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

    if name == "delete_event":
        cur = db.execute("DELETE FROM events WHERE id = ?", (args["id"],))
        db.commit()
        return {"success": cur.rowcount > 0, "id": args["id"]}

    if name == "read_document":
        try:
            from services.rag.chunker import _extract_text
            from services.vault import storage

            rel = args["path"].lstrip("/")
            if ".." in rel.split("/"):
                return {"found": False, "error": "Path is outside the vault"}
            try:
                data = storage.download(rel)
            except FileNotFoundError:
                return {"found": False, "error": f"File not found: {args['path']}"}
            _, content = _extract_text(data, rel)
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
                        "source": c.source_path,
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
            from services.vault import storage
            from services.rag.indexer import index_file

            slug = re.sub(r"[^\w\-]", "-", args["filename"].lower().strip()).strip("-")
            if not slug:
                slug = "note"
            filename = slug + ".md"
            # AI-generated notes live in their own folder, always flagged unreviewed —
            # never treated as ground truth by retrieval or the user (see CLAUDE.md's
            # vault convention). Must not land in inbox/ alongside the user's own notes.
            key = f"ai_generated/{filename}"

            if storage.exists(key):
                ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
                key = f"ai_generated/{slug}-{ts}.md"

            meta = {
                "title": args["title"],
                "topic": args["topic"],
                "tags": args.get("tags", []),
                "ai_generated": True,
                "reviewed": False,
                "created_at": _now(),
                "updated_at": _now(),
            }
            post = fm.Post(args["content"], **meta)
            storage.upload(key, fm.dumps(post).encode("utf-8"), content_type="text/markdown")

            # Index immediately so it's searchable in this same session
            try:
                index_file(key)
            except Exception:
                logger.warning("Immediate index of created note failed")

            return {"success": True, "path": key}
        except Exception as e:
            logger.exception("create_note tool failed")
            return {"success": False, "error": str(e)}

    if name == "find_connections":
        try:
            from services.connections.engine import discover_connections
            path = args["path"].lstrip("/")
            k = min(int(args.get("k", 5)), 10)
            connections = discover_connections(path, k=k, db=db)
            if not connections:
                return {"found": False, "message": "No non-obvious connections found for this note."}
            return {
                "found": True,
                "connections": [
                    {"target": c.target_path, "summary": c.summary, "distance": round(c.score, 4)}
                    for c in connections
                ],
            }
        except Exception as e:
            logger.exception("find_connections tool failed")
            return {"found": False, "error": str(e)}

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
        parts = [f"[{t['id']}] {t['title']}", f"status:{t['status']}"]
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
        if t.get("recurring"):
            parts.append(f"recurring:{t['recurring']}")
        if t.get("description"):
            parts.append(f'desc:"{t["description"][:120]}"')
        lines.append("  " + " | ".join(parts))
    return "\n".join(lines)


def _chunks_to_sources(chunks) -> list[dict]:
    """Turns retrieved vault chunks into the deduped {source, heading} list the app
    renders as a separate footnote/aside under a chat reply — never inline in the
    answer text. Mirrors the tiny bit of path-stripping injector.py's build_context()
    does for display, duplicated here (not imported) since injector.py is a RAG
    pipeline file this feature must not touch."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for c in chunks:
        src = c.source_path
        if "/data/vault/" in src:
            src = src.split("/data/vault/", 1)[1]
        elif src.startswith("chats/"):
            src = "[past conversation]"
        key = (src, c.heading)
        if key in seen:
            continue
        seen.add(key)
        out.append({"source": src, "heading": c.heading})
    return out


def _mask_chat_source(path: str) -> str:
    """Indexed chat transcripts live under the internal `chats/<chat-uuid>` source path
    — never show that raw identifier to the user. _chunks_to_sources() already does this
    for chunk-derived sources; this covers the one other spot (_execute_tool's raw
    search_vault result, used when search_vault runs alongside other tools in the same
    round) that builds a source string without going through that helper."""
    return "[past conversation]" if path.startswith("chats/") else path


def _dedupe_sources(sources: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    for s in sources:
        key = (s.get("source"), s.get("heading"))
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


class AIService:
    def __init__(self, provider: AIProvider):
        self.provider = provider

    def get_recommendations(self, db) -> dict:
        rows = db.execute("""
            SELECT id, title, description, status, priority, due_date,
                   fear_level, ambiguity_level, energy_type, estimated_effort, recurring
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

    def chat(self, db, messages: list[dict], client_tz: str | None = None) -> tuple[str, list[dict]]:
        # Every task regardless of status, not just inbox/active — the chat context
        # (unlike get_recommendations, which stays scoped to actionable tasks on
        # purpose) should give the model full visibility so it can answer questions
        # about done/blocked/archived tasks too, not just what's currently open.
        rows = db.execute("""
            SELECT id, title, status, priority, due_date, fear_level, ambiguity_level, energy_type, project_id, recurring
            FROM tasks
            ORDER BY (status = 'done'), (status = 'archived'), priority DESC, due_date ASC
        """).fetchall()
        tasks = [dict(r) for r in rows]
        tasks_by_id = {t["id"]: t for t in tasks}

        projects = db.execute("SELECT id, title, status FROM projects ORDER BY title").fetchall()
        projects_by_id = {p["id"]: dict(p) for p in projects}
        project_lines = "\n".join(f"  [{p['id']}] {p['title']} ({p['status']})" for p in projects) or "  None"

        # Second half of the bulk-delete confirmation round trip (see
        # _pending_bulk_delete_refs) — resolved entirely deterministically, no model
        # call needed, before any of the normal chat machinery below runs.
        pending_refs = _pending_bulk_delete_refs(messages)
        if pending_refs is not None:
            deleted_titles = []
            for kind, id_ in pending_refs:
                title = _resolve_delete_title(db, kind, id_, tasks_by_id, projects_by_id) or id_
                result = _execute_tool(db, _KIND_TO_DELETE_TOOL[kind], {"id": id_})
                if result.get("success"):
                    deleted_titles.append(title)
            if deleted_titles:
                return f"Deleted {len(deleted_titles)}: {', '.join(deleted_titles)}.", []
            return "Nothing was deleted — those may already be gone.", []

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
{task_block}

[The list above is EVERY task regardless of status (inbox, active, done, blocked,
archived — each shown via status:<value>) — answer any "list/show/what are my
tasks" question directly from it, including counting or filtering by the fields
shown. This explicitly includes tasks with status:done and status:archived — if
asked to list, count, or discuss done or archived tasks specifically, filter the
list above by status yourself and answer directly; do not claim you lack access
to done/archived tasks, you have them all right here. When recommending what to
work on next or summarizing open work (and only then), exclude done/archived
tasks yourself using the status field — don't wait to be asked.
There is no separate tool for listing tasks; only call
create_task/update_task/delete_task when the user wants to create, modify, or
remove one.]"""

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

        passive_chunks: list = []
        if last_user and not _should_skip_rag(last_user):
            try:
                from services.rag.retriever import retrieve
                from services.rag.injector import build_context
                chunks = retrieve(last_user, k=5)
                ctx = build_context(chunks)
                if ctx:
                    passive_chunks = chunks
                    system = (
                        f"{system}\n\n{ctx}\n\n"
                        "[Note: vault context already retrieved above — "
                        "answer directly from it rather than calling search_vault.]"
                    )
            except Exception:
                logger.exception("Passive RAG retrieval failed")

        # See _is_pure_task_listing's docstring — offering create_task/update_task/
        # delete_task as tools makes the model reliably refuse a plain listing
        # request even though the CURRENT TASKS block is already right there in
        # `system`. Bypass chat_with_tools entirely for this narrow, detected case.
        if last_user and _is_pure_task_listing(last_user):
            reply = self.provider.chat(system, messages) or ""
            return reply, _dedupe_sources(_chunks_to_sources(passive_chunks))

        # Agentic loop: keep executing tool calls until the model stops.
        # Cap at 5 rounds to prevent runaway chains.
        internal = list(messages)
        last_tool_results: list[tuple[str, dict, dict]] = []
        # Sources accumulated from any search_vault/read_document tool executions during
        # the loop, surfaced to the caller alongside the reply so the app can render them
        # as a separate footnote/aside rather than the model citing them inline (see
        # _SYSTEM_PROMPT's vault-usage rules). Only used once a tool round has actually
        # happened — before that, passive_chunks (if any) are what the first-round answer
        # was actually grounded in.
        used_sources: list[dict] = []
        tool_round_happened = False
        # Tracks (kind, id) pairs actually deleted so far across ALL rounds of this
        # single chat() call — the gate below is cumulative, not per-round, specifically
        # so a model can't bypass confirmation by spreading a bulk delete across
        # multiple single-item rounds (each individually under the per-round threshold),
        # and cumulative *across* delete tools too (2 tasks + 1 project in one turn is
        # still 3 deletions).
        executed_delete_refs: list[tuple[str, str]] = []
        for _ in range(5):
            reply_text, tool_calls = self.provider.chat_with_tools(system, internal, TOOLS)

            # delete_tasks_matching is a deterministic macro, not something _execute_tool
            # knows how to run directly — expand it here into literal delete_task calls
            # with ids resolved by _resolve_pattern_tasks (exhaustive server-side
            # matching), before any gate/execution logic below ever sees it. Everything
            # downstream (the confirmation gate, per-call execution, title backfill)
            # then handles these exactly like delete_task calls the model wrote itself,
            # so completeness comes from Python's matching, not the model's recall.
            # Deduped against any ids already present (from this or another pattern
            # call, or an explicit delete_task in the same round) so overlapping
            # patterns/calls can't double-count the same task in the confirmation.
            if any(tc["name"] == "delete_tasks_matching" for tc in tool_calls):
                seen_ids = {tc["arguments"]["id"] for tc in tool_calls if tc["name"] == "delete_task"}
                # match_type="all" bundled into the same round as a delete_project/
                # delete_event call is treated as a mistake, not a legitimate compound
                # request, and is dropped rather than expanded — enforced here in code
                # because a prompt-only warning against it (see BULK OR AMBIGUOUS
                # REFERENCES above) did NOT reliably prevent it: verified empirically,
                # asked to "delete all projects" with no mention of tasks at all, the
                # model still called delete_tasks_matching(match_type="all") alongside
                # delete_project once per project, which would have silently wiped
                # every task too as an undocumented side effect. A genuine "wipe both
                # my tasks and my projects" request just needs two separate turns.
                blanket_all_unsafe = any(tc["name"] in ("delete_project", "delete_event") for tc in tool_calls)
                expanded, no_match_notes = [], []
                for tc in tool_calls:
                    if tc["name"] != "delete_tasks_matching":
                        expanded.append(tc)
                        continue
                    pattern = tc["arguments"].get("pattern")
                    match_type = tc["arguments"].get("match_type", "contains")
                    if match_type == "all" and blanket_all_unsafe:
                        continue
                    matches = _resolve_pattern_tasks(tasks, pattern, match_type)
                    if not matches:
                        no_match_notes.append(f'No tasks matched "{pattern}".' if pattern else "No tasks matched.")
                        continue
                    for t in matches:
                        if t["id"] in seen_ids:
                            continue
                        seen_ids.add(t["id"])
                        expanded.append({
                            "call_id": f"{tc['call_id']}_{t['id']}",
                            "name": "delete_task",
                            "arguments": {"id": t["id"]},
                        })
                tool_calls = expanded
                if not tool_calls and no_match_notes:
                    return " ".join(no_match_notes), []

            # Intercept any delete_task/delete_project/delete_event set before executing
            # anything if it would push this turn's total deletions past 1, regardless
            # of what the model's own reply_text says it's going to do — do not trust
            # the model to hold off on the delete calls itself (see
            # _pending_bulk_delete_refs docstring for why this is enforced in code
            # rather than by prompt alone). Any non-delete calls in the same round still
            # execute normally so a compound request ("delete A and B, and add C")
            # doesn't silently drop the C part.
            delete_calls = [tc for tc in tool_calls if tc["name"] in _DELETE_TOOL_KIND]
            other_calls = [tc for tc in tool_calls if tc["name"] not in _DELETE_TOOL_KIND]
            pending_refs = [(_DELETE_TOOL_KIND[tc["name"]], tc["arguments"]["id"]) for tc in delete_calls]
            if pending_refs and len(executed_delete_refs) + len(pending_refs) > 1:
                side_replies = []
                for tc in other_calls:
                    result = _execute_tool(db, tc["name"], tc["arguments"])
                    if tc["name"] == "search_vault" and result.get("found"):
                        for c in result["chunks"]:
                            used_sources.append({"source": _mask_chat_source(c["source"]), "heading": c["heading"]})
                    if tc["name"] == "read_document" and result.get("found"):
                        used_sources.append({"source": tc["arguments"]["path"], "heading": ""})
                    if tc["name"] == "find_connections" and result.get("found"):
                        for c in result["connections"]:
                            used_sources.append({"source": c["target"], "heading": ""})
                    side_replies.append(_synthesize_tool_confirmation(tc["name"], tc["arguments"], result))
                confirm_reply = _confirm_bulk_delete_reply(db, tasks_by_id, projects_by_id, pending_refs)
                reply = "\n".join(side_replies + [confirm_reply]) if side_replies else confirm_reply
                return reply, _dedupe_sources(used_sources)

            if not tool_calls:
                if reply_text:
                    sources = used_sources if tool_round_happened else _chunks_to_sources(passive_chunks)
                    # Verify against the actual tool results rather than trusting the
                    # model's own phrasing at face value — on a multi-item write round
                    # (e.g. bulk create/update), a model that glosses over one failed
                    # call among several successes would otherwise leave the user
                    # thinking everything went through. Only appends when something in
                    # the round actually failed; never overrides a genuinely clean reply.
                    failures = [(n, a, r) for n, a, r in last_tool_results if not r.get("success", True)]
                    if failures:
                        fail_note = " ".join(
                            f'"{a.get("title") or a.get("id", "")}" failed ({r.get("error", "unknown error")}).'
                            for n, a, r in failures
                        )
                        reply_text = f"{reply_text}\n\nNote: {fail_note}"
                    return reply_text, _dedupe_sources(sources)
                if last_tool_results:
                    reply = "\n".join(
                        _synthesize_tool_confirmation(n, a, r) for n, a, r in last_tool_results
                    )
                    return reply, _dedupe_sources(used_sources)
                return "", []

            # search_vault tool responses cause Gemini 2.5 Flash Lite to emit 0 output
            # tokens in the follow-up round (confirmed via costs.log: out=0 every time).
            # Bypass the tool-response format entirely: perform the retrieval here,
            # inject the results via the system prompt, and answer with a plain chat()
            # call (no tools) — the same injection path used by passive RAG.
            if len(tool_calls) == 1 and tool_calls[0]["name"] == "search_vault":
                tc = tool_calls[0]
                chunks = []
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
                reply = self.provider.chat(fresh_system, messages) or ""
                return reply, _chunks_to_sources(chunks)

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
                reply = self.provider.chat(fresh_system, messages) or ""
                return reply, []  # calendar data, not vault content — no source citation

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
                reply = self.provider.chat(fresh_system, messages) or ""
                sources = [{"source": tc["arguments"]["path"], "heading": ""}] if result.get("found") else []
                return reply, sources

            if len(tool_calls) == 1 and tool_calls[0]["name"] == "find_connections":
                tc = tool_calls[0]
                result = _execute_tool(db, "find_connections", tc["arguments"])
                if result.get("found"):
                    lines = [f"NON-OBVIOUS CONNECTIONS for {tc['arguments']['path']}:"]
                    for c in result["connections"]:
                        lines.append(f"  → {c['target']}: {c['summary']}")
                    ctx = "\n".join(lines)
                else:
                    ctx = f"[find_connections: {result.get('message') or result.get('error') or 'no connections found'}]"
                fresh_system = f"{base_system}\n\n{ctx}"
                reply = self.provider.chat(fresh_system, messages) or ""
                sources = (
                    [{"source": c["target"], "heading": ""} for c in result["connections"]]
                    if result.get("found") else []
                )
                return reply, sources

            # Drop passive RAG context for subsequent rounds — tool results already
            # provide the context and the combined injection causes empty Gemini responses.
            system = base_system
            tool_round_happened = True

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
                # Delete-tool results only ever carry an id, never a title —
                # _synthesize_tool_confirmation would otherwise show the raw UUID to the
                # user. Resolved *before* executing (not backfilled after) because
                # delete_event's title lives only in the row itself, which is gone the
                # instant the delete succeeds — tasks/projects use the preloaded dicts
                # above so are unaffected either way, but events need the pre-image.
                pre_delete_title = None
                if tc["name"] in _DELETE_TOOL_KIND:
                    pre_delete_title = _resolve_delete_title(
                        db, _DELETE_TOOL_KIND[tc["name"]], tc["arguments"].get("id"), tasks_by_id, projects_by_id
                    )
                result = _execute_tool(db, tc["name"], tc["arguments"])
                if result.get("success") and "title" not in result and tc["name"] in _DELETE_TOOL_KIND:
                    result["title"] = pre_delete_title
                if tc["name"] in _DELETE_TOOL_KIND and result.get("success"):
                    executed_delete_refs.append((_DELETE_TOOL_KIND[tc["name"]], tc["arguments"]["id"]))
                # update_task/update_project/update_event results carry only an id too.
                # Same UUID-leak risk as the delete tools above (surfaced by testing: a
                # status-only update — no new title in the call — fell through
                # _synthesize_tool_confirmation's args.get("title") fallback straight to
                # the raw id). Skipped when the call itself supplies a new title, so a
                # rename shows the *new* name rather than being overwritten by the old
                # one looked up here.
                if (
                    result.get("success")
                    and "title" not in result
                    and "title" not in tc["arguments"]
                    and tc["name"] in ("update_task", "update_project", "update_event")
                ):
                    item_id = tc["arguments"].get("id")
                    if tc["name"] == "update_task":
                        result["title"] = tasks_by_id.get(item_id, {}).get("title")
                    elif tc["name"] == "update_project":
                        result["title"] = projects_by_id.get(item_id, {}).get("title")
                    else:
                        row = db.execute("SELECT title FROM events WHERE id = ?", (item_id,)).fetchone()
                        result["title"] = row["title"] if row else None
                last_tool_results.append((tc["name"], tc["arguments"], result))
                if tc["name"] == "search_vault" and result.get("found"):
                    for c in result["chunks"]:
                        used_sources.append({"source": _mask_chat_source(c["source"]), "heading": c["heading"]})
                if tc["name"] == "read_document" and result.get("found"):
                    used_sources.append({"source": tc["arguments"]["path"], "heading": ""})
                if tc["name"] == "find_connections" and result.get("found"):
                    for c in result["connections"]:
                        used_sources.append({"source": c["target"], "heading": ""})
                internal.append({
                    "role": "tool",
                    "tool_call_id": tc["call_id"],
                    "content": json.dumps(result),
                })

        final_reply = self.provider.chat(base_system, internal) or ""
        if final_reply:
            return final_reply, _dedupe_sources(used_sources)
        if last_tool_results:
            reply = "\n".join(
                _synthesize_tool_confirmation(n, a, r) for n, a, r in last_tool_results
            )
            return reply, _dedupe_sources(used_sources)
        return "", []
