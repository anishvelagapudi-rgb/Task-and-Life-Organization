# Personal Second Brain / Execution OS

_This file is meant to be the single, complete, pasteable source of truth for this
project — everything worth knowing, ordered from most to least important. If you're
an AI being handed this file with no other repo access, read top to bottom; by the
time you reach "Running Locally" at the end you have the full picture. Last synced
to actual codebase state: 2026-07-06._

This is my answer to a problem I kept running into: I had tasks in Canvas, deadlines
in my head, notes scattered across apps, and an AI assistant that forgot everything
the moment I closed the tab. I wanted one place that knew everything about my work
and could actually help me decide what to do next. So I built it.

---

## Honest Context — How This Was Built

Most of this codebase was written by Claude (Anthropic's AI coding assistant),
deliberately. I want to be upfront about that:

- I approved every line. Nothing went in that I didn't read, understand, and decide
  was right.
- I had a strong enough grasp of Flask, SQLite, OAuth, and REST API design to catch
  mistakes, ask the right follow-up questions, and make the real architectural calls
  myself (SQLite over MySQL, the provider abstraction pattern, the psychological task
  schema, keeping projects deliberately minimal).
- I used AI to move fast on a project I actually wanted to exist, not to avoid
  learning it.

**Development approach — "vibe code," then Ship of Theseus:** build a full feature
fast, including AI-generated code I don't yet own line-by-line, understand the
resulting system design, then replace each component with hand-written code one at a
time once it's well understood. A replacement is only valid once the app retains full
functionality and all tests still pass. This way I learn each piece in context —
against a working reference implementation, with a clear contract to satisfy —
instead of building into a void. So expect some code to be intentionally rough
first-pass AI output awaiting a rewrite, especially in `services/rag/` (the most
technically interesting part, and the primary target for this). That's the plan, not
an oversight.

**Who's building this:** one person, solo, no team.

---

## What This Is

A single-user cognitive infrastructure system whose actual design goal is to
eliminate friction *now* while making the user need it *less* over time — success is
not "opens this every day forever," it's "gets moving in the first 30 seconds, and is
faster/sharper a year from now with or without the tool." The dashboard's job is to
unstick the user, not micromanage them through the whole task. This is a private
tool, not a SaaS product, built for exactly one user indefinitely (gated by
`OWNER_EMAIL`) — no multi-tenancy, no monetization planned, ever.

Three layers:

- **Execution layer** — task/project management with a psychological task model
  (fear level, ambiguity, energy type, effort). These fields are AI-inferred in the
  background and hidden by default in the UI — never user-facing form fields by
  default — with the AI showing its reasoning whenever it infers or changes one.
- **Intelligence layer** — Gemini runs an agentic tool-calling loop that can read and
  mutate tasks, projects, and the local calendar through a chat interface. Every
  action it takes must be visible to the user — nothing silent, no hidden batch
  actions.
- **Knowledge layer** — a local markdown vault indexed with embeddings (RAG), so the
  AI retrieves only relevant context per query instead of dumping everything into the
  prompt, plus a connection engine that surfaces non-obvious links between notes.

---

## Stack (Actual, Current)

| Layer | Technology |
|---|---|
| Backend | Flask (Python) |
| Database | Postgres via Supabase, raw SQL through a thin psycopg2 wrapper, no ORM |
| Templates | Jinja2 server-rendered HTML |
| AI | Gemini 2.5 Flash Lite via `google-genai` SDK |
| Auth | Google OAuth 2.0 (Authlib), single-owner email gate |
| Config | `.env` file |
| Vector store | Postgres + `pgvector` (same Supabase project), exact cosine search, no ANN index |
| File storage | Supabase Storage (private `vault` bucket) — replaces local `data/vault/` |
| Embeddings | `gemini-embedding-001` (Google), 3072-dim |
| Calendar sync | Google Calendar API (read-only) + `icalendar` for one-way ICS import |
| Date parsing | `dateparser` (deterministic NL date parsing, no AI) |
| Spreadsheet export | `openpyxl` (Training Journal's `export_training_data` tool, `.xlsx` only — CSV needs no library) |

**Migrated from SQLite/ChromaDB/local vault files to Supabase (2026-07-06)**, driven
by the decision to deploy on Vercel: serverless functions have no persistent local
disk and no long-running background process, both of which the original design
depended on (`dev.db`, a local ChromaDB store, vault markdown files on disk, and a
`watchdog` file-watcher thread). SQLite/local-disk was still the right call for pure
local-only use — this migration exists specifically *because* deployment moved to a
platform without a writable persistent filesystem, not because the original choice
was wrong. See `supabase_setup.sql` for the schema and the "Vault," "RAG Pipeline,"
and "AI Layer" sections below for what changed in each subsystem. `pgvector`'s ANN
index (HNSW) caps out at 2000 dimensions; `gemini-embedding-001` produces 3072, so
`vault_chunks` has no vector index and does an exact brute-force `<=>` cosine scan
per query instead — more accurate than an approximate index, and fast enough at this
app's scale (a personal vault, low thousands of chunks at most).

---

## Architecture — Entry Points & File Map

- `app.py` — Flask app setup, OAuth, all HTML-rendering routes (dashboard, tasks,
  projects, chat, calendar, vault browser). Large, flat file (~1200+ lines); routes
  aren't split into further blueprints beyond `api`/`ai`.
- `api.py` — `/api/*` blueprint. Bearer-token REST API (CRUD on tasks/projects,
  health check) for external callers (e.g. N8N).
- `ai_routes.py` — `/api/ai/*` blueprint. `GET /api/ai/recommendations` and
  `POST /api/ai/chat`, both bearer-token gated.
- `db.py` — `psycopg2` access via Flask's `g`, wrapped in a small `_PGConnection`
  class that adds sqlite3-style `.execute(sql, params)` (psycopg2 only puts that on
  cursors) and rewrites the codebase's `?` placeholders to psycopg2's `%s`, so the
  vast majority of call sites across the app needed zero changes when the DB moved
  off SQLite. Rows come back via `RealDictCursor`, so `row["col"]`/`dict(row)` still
  work unchanged. **Schema lives in `supabase_setup.sql`** (run once, directly
  against the Supabase project) — `init_db()` no longer creates tables, it just wires
  up per-request connection cleanup (`app.teardown_appcontext(close_db)`). Also holds
  `enforce_recurring_invariant()` (shared between the REST API and the AI tool
  executor) and `reset_due_recurring_tasks()` (see Data Model below). `get_db()`
  also now checks the cached connection's `.closed` state and reconnects if it's
  gone stale, not just whether one exists yet — Supabase's pooled connection
  (Supavisor) can silently drop an idle connection mid-request during a slow
  multi-round Gemini tool-calling chat (surfaced by a slow Training Journal
  data query, 2026-07-19); previously this cascaded into an unhandled 500 even
  on the `explain_error()` fallback path that's specifically supposed to
  handle an error gracefully.
- `classes/Task.py`, `classes/Project.py` — plain Python objects with a
  `db_push(conn)` method that does its own INSERT-or-UPDATE (a lightweight
  hand-rolled ORM substitute), used by the HTML routes. `db_push` builds a single
  `{column: value}` dict literal and derives INSERT/UPDATE from it (refactored
  2026-07-03 to stop hand-duplicating parallel SQL column/placeholder lists on every
  new field — a real bug was found and fixed in the process: it previously hardcoded
  `completed_at=None` on every construction, silently wiping completion timestamps on
  every web-form save; `completed_at` is now a preservable constructor param).
  `api.py` instead builds SQL dynamically from field whitelists
  (`TASK_FIELDS`/`PROJECT_FIELDS`). **Both paths write to the same tables — keep them
  in sync when changing schema.**
- `services/vault/storage.py` — the single choke point for the Supabase Storage
  `vault` bucket (`upload`/`download`/`exists`/`list_keys`/`list_top_level_folders`/
  `delete`/`delete_prefix`/`move`). Replaced three previously-independent local-disk
  write paths (`processor.py`'s upload converter, `app.py`'s URL-fetch route, and the
  `create_note` AI tool's inline `open()` call) — all three now call this module
  instead of touching a filesystem.

**Auth is two separate, non-interchangeable mechanisms:** `api.py` and
`ai_routes.py` gate on `Authorization: Bearer <key>` checked against `API_KEY_HASH`
(SHA-512) via `hmac.compare_digest`. The browser-facing routes in `app.py` instead
gate on a cookie (`UserID`) looked up in the in-memory `VALID_SESSIONS` dict, which
resets on server restart.

---

## Data Model (Postgres via Supabase, raw SQL — no ORM)

**`tasks`**
- Core: `id` (TEXT/UUID), `title`, `description`, `status`, `priority`, `due_date`,
  `completed_at`
- Psychological fields: `fear_level` (1–5), `ambiguity_level` (1–5), `energy_type`,
  `estimated_effort`, plus `psych_reasoning` (free-text). These four scoring fields
  are collapsed behind a closed-by-default `<details>` disclosure on both the task
  detail edit form (`/tasks/<id>`) and the "New Task" creation form (`/tasks`) — not
  just the detail page, since leaving the creation form exposed would defeat the
  point. Input bounds were fixed from `1–10` to the correct `1–5` while this was
  built. `create_task`/`update_task` AI tool schemas take an optional
  `psych_reasoning` string; the system prompt instructs the model to always explain
  itself in 1–2 sentences whenever it sets/changes any of the 4 fields, and not to
  fabricate a reason when it didn't actually infer anything. `psych_reasoning` is one
  combined free-text field covering whichever subset of the 4 fields the AI touched
  on a given call — not per-field — a deliberate simplicity tradeoff (open question:
  revisit if per-field granularity turns out to matter in practice). Manually editing
  one of the 4 fields on the web form clears any stale `psych_reasoning` attached to
  it (previously a real bug: stale AI reasoning stayed visible next to a value the AI
  never actually set).
- Relational: `project_id`, `parent_task_id` (subtasks via self-reference, not a
  separate table)
- Recurrence: `recurring` (`NULL` | `'daily'` | `'weekly'`). **Invariant: a recurring
  task can never have a `due_date`**, enforced server-side at every write path — full
  HTML form submissions in `app.py`, and a single shared `enforce_recurring_invariant()`
  in `db.py` used by both the REST API (`api.py`) and the AI tool executor
  (`services/ai/service.py`) — originally duplicated between those last two and
  drifting, now consolidated. Both support partial updates, which surfaced two real
  gaps, both fixed: (1) a payload setting `due_date` without mentioning `recurring`
  could slip past a guard that only inspected the current payload — fixed by falling
  back to the task's existing `recurring` value from the DB when the payload omits
  it; (2) the first fix over-corrected and only cleared `due_date` when the payload
  already contained a `due_date` key, reopening the hole from the other side —
  `enforce_recurring_invariant()` now unconditionally nulls `due_date` whenever the
  *effective* recurring value (payload, falling back to existing) is truthy,
  regardless of whether the payload touched `due_date` at all. The AI tool executor's
  `update_task` also derives `completed_at` from the status transition, mirroring
  `app.py`'s HTML route — without this, the AI could mark a recurring task `'done'`
  via chat with `completed_at` never populated, and reset requires
  `completed_at IS NOT NULL` to consider a task eligible, so an AI-completed recurring
  task would never auto-reset. No separate "last completed" column — reset logic
  reuses `completed_at` as the recurrence clock.
  - **Reset is lazy, not a scheduler**: `reset_due_recurring_tasks()` runs on read
    from `/dashboard`, `/tasks`, and `/tasks/recurring` (also `/tasks/<id>`, added
    after being the one route that was missing it) — selects only the narrow
    candidate set (`recurring` set AND `status='done'`) into Python.
  - **Local-timezone-aware**: a sitewide script in `layout.html` stashes the
    browser's IANA timezone into a `tz` cookie, **not** percent-encoded — an earlier
    version used `encodeURIComponent`, which escapes the `/` in almost every real IANA
    zone name (`America/New_York` → `America%2FNew_York`), but Flask/Werkzeug never
    percent-decodes cookie values on the way back out, so the still-encoded literal
    failed the server-side validator and silently fell back to UTC for nearly all
    real users. Fixed by not encoding at all. The cookie is resolved via
    `zoneinfo.ZoneInfo` with a broad `except Exception` (not just
    `ZoneInfoNotFoundError`/`ValueError` — a regex-valid key like `"America"` or
    `"Etc"` is a real tzdata *directory*, raising `IsADirectoryError`). `completed_at`
    (stored UTC) is re-anchored to that same local zone before comparing calendar
    days, not compared as a raw UTC string prefix — a task completed at 23:50 UTC and
    one at 00:10 UTC the next day can be the same local day in most US timezones.
  - **UI**: recurring tasks are excluded from `/dashboard`/`/tasks` entirely; their
    only surface is a Daily/Weekly tabbed modal (`templates/_recurring_modal.html`)
    backed by `GET /tasks/recurring`, reusing the existing `/tasks/<id>/complete`
    toggle rather than a separate endpoint.
- Extra: `source_type`, `ai_generated`, `created_at`, `updated_at`
- JSON-encoded TEXT columns (not join tables): `tags`, `dependencies`, `task_notes`

**`projects`** — `id`, `title`, `description`, `status`, `progress`, `created_at`,
`updated_at`. Deliberately minimal — no `goal`/`risk_level`/`target_date` fields; the
owner wants projects to stay loose "continuous efforts," not structured planning
objects (explicitly decided against, not an oversight). `progress` is still
readable/writable via the REST API (e.g. for an n8n workflow) but no longer
rendered/editable in the web UI — progress bars were removed from `/projects`,
`/dashboard`, and `/projects/<id>`'s edit form as visual clutter with no actionable
use; `update_project` now preserves the stored value instead of resetting it to 0
now that the form no longer submits one.

**`chats` + `chat_messages`** — chat history is persisted to DB (not in-memory), so
the AI retains conversation context across server restarts. `chats.indexed` controls
whether a chat gets embedded into the vault's vector store ("save to vault").
`chat_messages.sources` (JSON TEXT, nullable) — a deduped list of
`{"source": <vault-relative path or "[past conversation]">, "heading": <str>}` the
AI's reply drew on. See "Source Citations" under AI Layer below for the full story.

**`calendars`** — local calendars (user-created, full CRUD) and ICS calendars
(`name`, `color`, `source`, `ics_url`, `visible`), synced on demand via a "sync"
button.

**`events`** — `id`, `calendar_id`, `title`, `description`, `start_datetime`,
`end_datetime`, `all_day`, `location`, `source_uid`, `created_at`, `updated_at`. Only
local + ICS-synced events are stored here. Google Calendar events are never written
to this table — fetched live/cached and stay read-only.

**`tokens`** — `provider` (PK, e.g. `'google'`), `access_token`, `refresh_token`,
`token_type`, `expires_at`. Holds the Google OAuth token used for both login identity
and Calendar API reads, auto-refreshed near expiry.

**`note_connections`** (connection engine, see below) — `id`, `source_path`,
`target_path`, `source_collection`, `target_collection`, `distance`, `summary`,
`created_at`. Upserted by `(source_path, target_path)` on every discovery call — acts
as a cache/log, not a rigorously-designed invalidation strategy. Rows for a deleted
vault file are best-effort cleaned up on vault delete/move, mirroring how the vector
store's index is already cleaned up on delete.

**`training_entries`** — the Training Journal's raw, immutable-by-default log.
`id`, `entry_date` (local calendar date, `YYYY-MM-DD`, resolved via the same
`tz`-cookie mechanism recurring tasks use), `content` (raw text, never edited
except through the explicit edit route below), `processed` (0/1, drives lazy
extraction — see "Training Journal" section below), `created_at`.

**`training_attachments`** — images/PDFs attached to a `training_entries` row
(Phase 1 scope only). `storage_key` points into a dedicated `training-journal`
Supabase Storage bucket, deliberately separate from the `vault` bucket —
vault uploads trigger RAG indexing side effects attachments must never go
through.

**`training_extractions`** — append-only structured data an LLM extracts from
`training_entries.content` lazily (on read, not on every message — see
Training Journal section below). `source_entry_id`, `entry_date`
(denormalized for cheap date-range queries), `metric_type` (one of `weight`,
`body_measurement`, `nutrition`, `sleep`, `resting_hr`, `run`, `workout_set`,
`soreness_injury`, `mood_energy`, `recovery`, `steps`, `note`), `data` (JSON,
shape depends on `metric_type` — same JSON-TEXT-column convention as
`tags`/`dependencies`/`task_notes` above, so new metric types don't need
schema churn), `confidence`, `extraction_model`, `extracted_at`, `superseded`
(0/1 — reprocessing an entry never `UPDATE`s a row here, it `INSERT`s new ones
and flags the old ones superseded, so results stay reproducible against a
better model later; also reused, new as of the edit-in-place feature, to
invalidate extractions after a user edits an entry's text).

**No foreign key constraints anywhere** (`project_id`, `parent_task_id`,
`calendar_id`, etc. are plain `TEXT` columns, not `REFERENCES`) — a deliberate
carry-over from SQLite, not an oversight. SQLite never enforced the `REFERENCES`
clauses the old schema declared (`PRAGMA foreign_keys` was never turned on), and
`app.py`'s `delete_project()` route actively depends on that being unenforced — it
deletes a project without first clearing `tasks.project_id` for tasks that belong to
it. Postgres enforces FKs by default, so declaring them for real would have broken
that route the first time someone deleted a project with tasks in it. Revisit only if
that route's behavior is intentionally changed first.

---

## AI Layer (`services/ai/`)

- `provider.py` — `AIProvider` interface (`chat`, `chat_with_tools`); swap backends
  by implementing it. `gemini_provider.py` (primary, Gemini 2.5 Flash Lite, full
  tool-calling) and `groq_provider.py` (implemented, not actively used).
  `gemini_provider.py`'s `chat_with_tools` also degrades to an empty response
  (`"", []`) instead of raising `AttributeError` when
  `response.candidates[0].content` is `None` — can happen on a non-`STOP`
  finish reason (safety/recitation/malformed-function-call, or the same
  "0 output tokens" quirk documented below) — callers already treat "no tool
  calls, no text" as a valid empty response, so this just stops it from
  crashing the call outright (found 2026-07-19 during Training Journal work).
- `service.py` (~1100+ lines) — the core: system prompt, the agentic tool-calling
  loop (up to 5 rounds per message), all calendar-date-resolution logic, task/
  project/calendar context injected on every call. As of 2026-07-06, the injected
  `CURRENT TASKS` block includes **every task regardless of status** (inbox,
  active, done, blocked, archived — each shown via `status:<value>` in
  `_serialize_tasks`), not just inbox/active — a deliberate token-cost tradeoff so
  the model can answer questions about completed/archived work too, not just
  what's currently open. `get_recommendations()` (the separate `/api/ai/recommendations`
  endpoint) intentionally keeps its own inbox/active-only filter — recommending a
  done task makes no sense there.
- **Tools the AI can use:** `create_task`, `update_task`, `delete_task`,
  `delete_tasks_matching`, `create_project`, `update_project`, `delete_project`,
  `read_document`, `search_vault`, `create_note`, `list_events`, `create_event`,
  `update_event`, `delete_event`, `find_connections`, `query_training_data`,
  `export_training_data`, `graph_training_metric` (the last three read/export/
  chart the Training Journal — see that section below). Any new AI tool
  that returns vault/note content should follow the same single-tool-call shortcut
  pattern as `search_vault`/`read_document`/`list_events`/`find_connections`/
  `query_training_data` (bypass
  the tool-response round-trip, inject results via a fresh plain-chat call instead of
  trusting the tool-response round-trip) — this works around a documented Gemini bug
  where it occasionally emits 0 output tokens on the round right after a real tool
  call. `query_training_data`'s shortcut needed one extra fix on top of the
  pattern — see "Training-journal tool shortcut" under the NVIDIA/Gemma
  findings below.
- `AIService.chat()` returns `(reply_text, sources)`, not a bare string — every call
  site (`app.py`, `ai_routes.py`, `rag_test.py`) unpacks the tuple. `sources` feeds
  the chat UI's citation chips and is populated even by the single-tool shortcut
  paths that bypass the normal round-trip.
- `budget.py` — rolling 1-hour wall-clock dollar-cost window across all API calls
  (generative + embedding); raises `BudgetExceededError` over the limit, recovers as
  old calls age out. **Backed by a Postgres table (`ai_usage_log`)**, not an
  in-memory deque — deliberately migrated off in-memory (2026-07-06) because a
  process-local window silently gets *weaker*, not just non-persistent, once more
  than one server instance can be running (e.g. multiple Vercel serverless
  instances): each instance would get its own independent budget, multiplying the
  effective cap instead of sharing one. `check()` and `record_usage()`/
  `record_embedding_usage()` keep the exact same signatures and pre-flight/post-hoc
  semantics as before — only the storage backend changed. Cost derived from
  `prompt_token_count` + `tool_use_prompt_token_count` (input) and
  `candidates_token_count` + `thoughts_token_count` (output) — earlier versions
  undercounted by dropping the tool-use/thinking-token fields. Configurable via
  `AI_HOURLY_BUDGET` (currently $0.15/hour, raised 2026-07-03 from an initial $0.05
  placeholder). Every call also logged to `costs.log` (local file, write-only audit
  trail, never read back — harmless to lose on a serverless cold start).
- `GET /api/ai/recommendations` — top 3 prioritized tasks + a short insight, JSON.
  Recommendation logic and the AI's ability to set the 4 psychological fields have
  stayed untouched through every later feature added on top.
- The AI never has direct Google Calendar tool access — it only reads a
  process-global cache refreshed on page load (never triggered by the AI itself), by
  design, so it can't fabricate having checked something it never fetched.

### Source citations as a separate footnote/aside

`AIService.chat()`'s every return path (passive RAG's first-round answer, the
`search_vault`/`read_document`/`list_events`/`find_connections` single-tool
shortcuts, the generic multi-tool loop, the final fallback) threads a deduped
`sources` list through. The system prompt no longer tells the model to cite
filenames inline — replaced with an explicit instruction not to, since sources now
render as a separate `.msg-sources` chip row directly under the assistant bubble in
`templates/chat.html` (server-rendered for history, client-appended via
`appendSources()` for live replies, built with `createElement`/`textContent`, never
`innerHTML` — this codebase's established XSS-safe rendering pattern).
`chat_messages.sources` persists this across reloads. A chat-transcript UUID is
masked to `"[past conversation]"` via `_mask_chat_source()` everywhere a source list
is built, including the multi-tool-round path where this masking was originally
missed (a real, since-fixed leak of internal chat UUIDs into the visible source
list).

### Calendar-aware chat — subtle failure modes already fixed

This was the source of a long debugging session; read this before touching date
handling so these aren't reintroduced:

- Client sends its IANA timezone with every chat message; server converts UTC to
  that zone for the "current date" the model reasons from (falls back to UTC).
  Fixes a bug where the assistant used server UTC date, sometimes a day ahead of the
  user's real local date.
- Date-range resolution is a **layered deterministic pipeline**, AI only as last
  resort — delegating this to the AI directly was unreliable (wrong year, off-by-a-
  week weekday math, "day after tomorrow" computed as plain "tomorrow"):
  1. Hand-written weekday arithmetic ("last/next/this `<weekday>`") — neither the AI
     nor `dateparser` handles this reliably.
  2. Hand-written relative-day arithmetic (today/tomorrow/yesterday/N-days-ago/etc.)
  3. `dateparser` for explicit absolute dates, with a guard against false-positives
     on stray words like "on"
  4. AI fallback, only for genuinely anaphoric follow-ups with no date words ("what
     about the day after?")
  5. Default: today → +14 days for generic questions ("what's on my calendar")
- No read/write classifier anymore. There used to be a regex guessing whether a
  message was a calendar read or write, routing reads through a no-tools path for
  reliability. Real bug this caused: "Block 2 hours of my day tomorrow" matched no
  write verb, got routed to the no-tools path, and the model **fabricated a fake
  confirmation** ("I've blocked 2 hours... 9-11am") because it structurally couldn't
  have created anything. Fixed by removing the classifier entirely — tools
  (`create_event`, `update_event`) are always available whenever calendar context is
  in play; the model's own tool-calling judgment decides, never a Python heuristic.
- `_synthesize_tool_confirmation` — Gemini occasionally returns 0 output tokens on
  the round right after a real tool call; this builds a plain confirmation directly
  from the tool's actual result so the user is never shown a blank reply after a real
  write.
- Explicit system-prompt rule: pick one reasonable time and call `create_event`
  exactly once — the model was sometimes creating two duplicate events for one
  ambiguous-time request.
- **Known residual issue:** even with verified-correct injected context,
  `gemini-2.5-flash-lite` occasionally misreads/mislabels it (~1-in-4 in a repeated
  identical-input stress test) — e.g. calling the right date "tomorrow" instead of
  "the day after tomorrow," or claiming "no events" when the block wasn't empty.
  Model-capability ceiling, not a logic bug — every deterministic piece upstream of
  the final answer has been verified correct. **Decision made:** stick with the
  current model and invest in engineering (better deterministic pre-processing,
  verification passes) rather than upgrading to a pricier model — revisit only if
  that effort provably can't close the gap.

### Task listing — a related model quirk, only partially fixed (2026-07-06)

`gemini-2.5-flash-lite`, when offered `create_task`/`update_task`/`delete_task` as
function-calling tools, reliably refused plain listing requests ("list all my
tasks") even with the full task list already sitting in its context — it fixates
on "no tool literally named list" and ignores contradicting instructions and data.
Verified directly: the identical context/system-prompt answers correctly when
tools are simply omitted from the request. Fixed for general listing phrasing via
`_is_pure_task_listing()` in `service.py` — a regex-detected bypass (same pattern
as the `search_vault`/`list_events` single-tool-call shortcuts) that skips
`chat_with_tools` entirely and answers with a plain `chat()` call instead.

**Known residual issue, not fixed:** asking specifically about **done** tasks by
name (e.g. "list my done tasks") still triggers a near-100%-reproducible false
refusal ("I can only see inbox/active/blocked tasks") — confirmed at 4/4 across
three escalating prompt rewrites, including one explicitly stating the done task
is right there in context. Oddly, indirect phrasing about the same data ("how many
tasks are done?") answers correctly. This is the word "done" specifically
triggering something in the model, not a tools-vs-no-tools question like the
general case above — a deeper model-capability ceiling in the same family as the
calendar issue, not a logic bug. **Decision: leave as a known quirk for now.** A
more robust fix would pre-filter the task list in Python by status when the
message names one (deterministic, bypasses the model's status-reasoning
entirely) rather than relying on prompt wording — revisit if this proves
disruptive in practice.

### Bulk delete — signed confirmation gate (2026-07-13)

**Incident:** a user asked the chat AI to "delete all tasks." The model asked for
task IDs one at a time instead of resolving them from its own already-injected task
context; the user replied "all of them, just take each id and delete that"; the
model then called `delete_task` once per task with no further confirmation,
deleting every task with no way to undo it. Prompt-only instructions telling the
model to ask before bulk-deleting are not reliable enough on their own — verified
empirically, Gemini 2.5 Flash Lite executed a 2-task delete immediately despite an
explicit system-prompt instruction to confirm first.

That "asked for task IDs one at a time" step is itself a separate bug, independent
of the missing confirmation gate: the model should never ask a person to look up
and paste back an internal database ID — it already has every task's id/title in
context and should resolve a name itself. Asking a clarifying question at all
should only happen when a given name is genuinely ambiguous (matches more than one
existing item), and even then by describing the candidates in plain language, never
by requesting an ID. Fixed in the system prompt's `IMPORTANT RULES` (a general rule,
not scoped to bulk/delete requests, since the same failure mode applies to any
single-item reference-by-name too, e.g. "add a subtask to my English Class HW" when
two tasks share a similar name) — this is prompt-only, not code-enforced, so unlike
the confirmation gate above it carries the same reliability caveat as other
prompt-level fixes in this file (see the task-listing and calendar quirks above).

**Fix, enforced in code, not just prompt:** `chat()` tracks every `delete_task` /
`delete_project` / `delete_event` call across all rounds of a single turn
(cumulative, so a model can't dodge the gate by spreading a bulk delete across
several single-item tool-calling rounds). The moment the running total would exceed
1 deletion — in any combination across the three tools — execution stops before
anything is deleted, and the reply becomes a confirmation prompt listing the exact
resolved titles, e.g. `This will permanently delete 3 tasks: "Buy milk", "Pay rent",
"Call mom". Reply "confirm" to proceed...`, ending in an internal
`[ref: task:<id>,task:<id>,...|<hmac>]` marker (`strip_pending_delete_marker` hides
it from what's actually shown to the user; the raw marker is what's persisted/
round-tripped through chat history). The `hmac` is keyed off `FLASK_SECRET_KEY`, so
a look-alike `[ref: ...]` string reproduced verbatim from an untrusted source (e.g.
injected via a malicious vault note reachable through `search_vault`/
`read_document`) can't be forged into a real confirmation. On the user's next
message, `_pending_bulk_delete_refs` checks for an exact affirmative reply
immediately following a validly-signed marker — only then are the referenced rows
actually deleted, resolved fresh at that moment (a stale id whose row is already
gone is silently skipped, not errored).

The gate is generalized by a `kind` prefix (`task:`/`project:`/`event:`) rather than
being task-specific, specifically so **any** future delete-capable tool is a
one-line addition to `_DELETE_TOOL_KIND`, not a parallel confirmation mechanism to
remember to build — `delete_project` had no gate at all until this fix (a live gap
of the exact same shape as the original incident, just never exercised), and
`delete_event` was added as a new tool with the gate already in place rather than
bolted on afterward. **Creates remain intentionally ungated** — the prompt (system
prompt's "BULK OR AMBIGUOUS REFERENCES" section) explicitly tells the model to batch
multiple `create_task`/`create_event` calls in one turn with no per-item
confirmation, since creation isn't destructive and gating it would just reintroduce
the "asks for details one at a time" friction the original bulk-delete incident was
adjacent to.

**Two more bugs this fix's own smoke-testing surfaced, both fixed the same session:**
- `update_task`/`update_project`/`update_event` results carried only an id, never a
  title, same as the delete tools originally did — `_synthesize_tool_confirmation`'s
  fallback (Gemini's 0-output-token quirk) would show the raw UUID to the user on a
  status-only update with no new title in the call. Backfilled the same way the
  delete tools are, skipped when the call itself supplies a new title (so a rename
  shows the new name, not an old one looked up here).
- `parent_task_id` (subtasks) was a real, writable column (`api.py`'s `TASK_FIELDS`,
  the browser's subtask UI) with **zero** AI tool exposure — asking the AI to "add a
  subtask to X" had no working path, so it improvised by asking the user for a task
  ID instead of saying it couldn't do it (a second, independent source of the
  ID-asking behavior above, this one caused by a missing tool rather than a
  resolve-by-name failure). Added `parent_task_id` to `create_task`/`update_task`.
  Doing so surfaced a *third* bug during live testing: asked to add a subtask, the
  model sometimes calls `update_task(id=<parent>, parent_task_id=<parent>)` on the
  parent itself instead of setting `parent_task_id` on the new child — a
  self-referential row that would infinite-loop `app.py`'s parent-chain walk used to
  render the subtask tree. Fixed with `enforce_no_self_parent()` in `db.py` (same
  shared-invariant pattern as `enforce_recurring_invariant`, called from both
  `api.py` and `service.py`) — silently drops `parent_task_id` when it equals the
  row's own id rather than erroring. Only guards direct self-reference, not longer
  cycles (A→B→A), which were already reachable via the REST API before
  `parent_task_id` was ever AI-exposed and are a separate, pre-existing gap.

### Bulk delete completeness — `delete_tasks_matching` (2026-07-14)

**Incident:** asked to "delete all tasks that begin with TEST" against a 13-task
list, the model correctly went through the confirmation gate above (no ID-asking,
exact titles shown, nothing removed until confirmed) — but the *set it proposed* was
wrong: it picked 11 of 13 matching tasks and silently dropped 2, one of them a
`status:done` task. The gate can only ever be as complete as the set of ids it's
handed; it has no way to know the model's enumeration undercounted.

**Why "keep looping until the request is satisfied" doesn't fix this:** the failure
is in *selection*, not *execution* — the model already terminated normally,
considering the job done, having genuinely (if incorrectly) read through the task
list once. A supervisory loop that re-checks "is my selection complete?" would have
the same model re-skim the same long text block a second time — no more reliable
than the first pass, and if it *does* catch a miss and autonomously issues more
delete calls without a fresh confirmation, that's unattended repeated
destructive action-taking, which is the opposite of what the confirmation gate above
exists to prevent.

**Actual fix:** stop asking the model to enumerate matches from memory at all. Before
this, "delete all tasks matching X" meant the model read `CURRENT TASKS` and called
`delete_task` once per item *it personally noticed* — reliability bounded by however
well an LLM skims a serialized text block. `delete_tasks_matching(pattern,
match_type)` instead asks the model only to state the *pattern* (`contains` /
`starts_with` / `all` for literally every task) — a much simpler, lower-stakes
classification than exhaustive enumeration — and `_resolve_pattern_tasks()` in
`service.py` does the actual matching with a plain Python scan over the same full
task list already loaded for the `CURRENT TASKS` block, which cannot skip a row the
way skimming a long block can. The tool call is expanded into literal `delete_task`
calls (with ids resolved server-side) before the round even reaches the confirmation
gate/execution logic, so everything downstream — the signed-marker gate, per-call
execution, title backfill — handles it exactly like `delete_task` calls the model
wrote by hand; completeness comes from Python's matching, not model recall, but
nothing about the confirm-before-delete safety property changes. `match_type: all`
also closes the loop on the *original* "delete all tasks" incident this whole
mechanism exists for — that phrasing has the identical enumeration-reliability
exposure as a keyword pattern and now goes through the same deterministic path
instead of the model hand-picking every id from a long list.

Scoped to tasks only for now (`delete_projects_matching`/`delete_events_matching`
would be a straightforward extension of the same pattern if bulk project/event
deletion ever needs it — projects/events are typically far fewer per user, so the
enumeration-miss risk that motivated this for tasks is much lower there today).

Verified with a unit test of `_resolve_pattern_tasks` directly (contains vs.
starts_with vs. all, case-insensitivity, empty/`None`/whitespace-only pattern,
substring-boundary cases like "CONTEST" containing but not starting with "TEST")
and a live end-to-end test: created a known 13-task batch (8 true positives + 3
"contains but doesn't start with" decoys + 2 unrelated decoys), ran the delete
through actual chat, and diffed the *exact id set* removed against the *exact id
set* expected — not a spot check of a few expected items, but a full-list
comparison proving nothing outside the intended set was touched.

### `delete_tasks_matching(match_type="all")` scope leak (2026-07-14)

**Incident:** asked to "Delete all projects" — no mention of tasks anywhere in the
request — the model called `delete_project` once per project (correct) *and also*
`delete_tasks_matching(match_type="all")` (not requested, not implied). The
confirmation gate correctly intercepted the whole batch before anything was deleted
and displayed all 19 resolved items, so nothing was actually lost — the user read
the confirmation, noticed tasks were listed for a request that only mentioned
projects, and stopped before confirming. But had they skimmed a flat 19-item list
and confirmed, every task in the account would have been deleted as an undocumented
side effect of a projects-only request.

First fix attempt was prompt-only: an explicit `CRITICAL` rule added to `BULK OR
AMBIGUOUS REFERENCES` stating that the noun after "all" determines scope and that
`delete_tasks_matching` must never fire for a request about a different resource
type. Re-tested against the identical "Delete all projects" request — **the model
made the exact same mistake anyway.** Consistent with every other model-reliability
finding in this file: a prompt instruction, however explicit, is not a substitute
for a code-enforced invariant once the stakes are "could delete everything."

**Actual fix, in code:** in `chat()`'s per-round tool-call handling, a
`delete_tasks_matching(match_type="all")` call is dropped (not expanded into
deletes) whenever the same round also contains a `delete_project` or `delete_event`
call — the co-occurrence itself is the signal, no NLP/keyword-matching on the user's
message required. Re-tested against the identical request post-fix: correctly
resolves to project deletions only, zero tasks. A genuine "wipe both my tasks and my
projects" request just needs two separate turns — an acceptable, rare cost for
closing a scope-leak that could otherwise silently 10x the blast radius of an
unrelated request.

**Second, independent layer:** `_confirm_bulk_delete_reply()` now groups the
confirmation message by kind ("19 items (13 tasks and 6 projects): tasks — ...;
projects — ...") instead of one flat undifferentiated list, specifically so a wrong
proposed scope — from *this* bug, or a different one not yet found — is legible at
the one point a human reviews it before anything is deleted, rather than buried in a
long flat list that invites skimming. This is deliberately redundant with the code
fix above: the code fix prevents the known failure mode, the grouped message is a
generic safety net for an unknown one.

### Two more frontend/perf findings from the same testing session (2026-07-14)

- **Chat-open latency:** `chat_view()` (`app.py`) called `gcal_service.refresh_upcoming_cache()`
  unconditionally on every page load — a live network round trip to the Google
  Calendar API (one call per connected calendar) with no throttling at all, measured
  at ~1.4s, repeating on *every single click* into a chat even seconds apart. Fixed
  with a `_CACHE_FRESH_SECONDS = 60` window in `gcal_service.py`: the refresh is a
  no-op if the cache is under a minute old. Verified directly — first call 1.38s,
  immediate second call 0.00s. A personal calendar doesn't change fast enough to
  justify refetching on every click within the same minute.
- **Bulk-delete confirm dialog — disabled, unresolved:** the `window.confirm()` dialog
  added on top of the existing text-based delete confirmation (see above) was
  followed by reports of `[object Object]` chat bubbles and repeated 500s
  (`app.py`'s `chat_message()`: `content` arrived as a JSON object, not a string).
  First hypothesis — the new `sendChatMessage()` function name colliding with the
  browser's native `window.postMessage` — was fixed (renamed) but did **not** stop
  the recurrence, so it was at most a contributing factor, not the root cause. The
  confirmed reproduction showed 4 identical malformed POSTs firing automatically,
  within the same second, immediately after a **brand-new** chat page load (so not
  stale cached JS) — not tied to any visible user action. Root cause not found.
  Mitigated two ways rather than left as an open crash: (1) the `window.confirm()`
  call is commented out in `chat.html` — bulk deletes still work via the
  already-solid typed-"confirm" flow, just without the extra native dialog; (2)
  `chat_message()` now checks `isinstance(content, str)` and returns a clean 400 with
  full request diagnostics logged (`DEBUG_BADBODY`: content value, headers, raw body)
  instead of crashing with a 500, so *if* this recurs there's a real diagnostic trail
  instead of another blind investigation. Not reproduced since these two changes.

### Silent truncation on large bulk creates (2026-07-14)

**Incident:** asked to create one task with 19 subtasks (one per name in a list),
the model created the parent plus only 4 subtasks, then returned a clean-sounding
confirmation as if the request were fully done. Nothing errored; the user only
noticed because they counted the result.

**Root cause:** `chat()`'s agentic loop (`for _ in range(5)`) caps tool-calling at 5
rounds per message. Subtasks need the parent's server-generated UUID
(`create_task` assigns `id = str(uuid.uuid4())` and takes whatever
`parent_task_id` it's given with no existence check), so the parent must be
created and its id read back from the tool result before any subtask calls can
reference it — ruling out doing parent + 19 subtasks in one round. Gemini then
paced itself at a handful of `create_task` calls per round rather than batching
the rest, so all 5 rounds were consumed without finishing. When every round
returns at least one tool call, the loop never hits its only early-return branch
(`if not tool_calls:`) and instead falls out of the `for` loop to a final,
tool-less `chat()` call that summarizes whatever fit in those 5 rounds — with no
signal that the original request was cut short, and no reason for the model to
volunteer that on its own.

**Fix:** same "verify against what actually happened, don't trust the model's own
phrasing" principle as the failures/`fail_note` check earlier in `chat()`.
Falling out of the loop (as opposed to returning early from inside it) is itself
a deterministic signal that every round was still doing tool work when the
budget ran out, regardless of what the final summary text says — so a fixed
notice ("reached the assistant's per-message tool-call limit before finishing
... send another message to have it continue") is now appended to whatever reply
comes back on that path. This doesn't make one message create all 19 subtasks
in one shot (no change to the round cap or to Gemini's per-round pacing) — it
just stops the assistant from silently implying a partial bulk operation was
complete. Resuming a truncated bulk create still requires the user to notice
and send a follow-up message asking it to continue.

### NVIDIA/Gemma-4-31B-IT as an alternate provider — a worse-shaped bug than the one above (2026-07-14)

**Context:** added `services/ai/nvidia_provider.py` (`NvidiaProvider`), a second
`AIProvider` implementation hitting NVIDIA's free developer-tier NIM catalog
(`google/gemma-4-31b-it` by default) over its OpenAI-compatible endpoint, as an
alternative to `gemini_provider.py`. Not wired in as the default — `app.py`/
`ai_routes.py` still construct `GeminiProvider()`; this exists to be swapped in
and A/B tested. `chat_template_kwargs: {"enable_thinking": True}` is sent via
`extra_body` on every call (a NIM/vLLM-specific field the `openai` SDK doesn't
expose directly) — NVIDIA's own docs say tool calling on this model family works
best in thinking mode. Not hooked into `budget.py`: that module's per-token rates
are Gemini-specific pricing, not a lookup table, and this tier is free — same
reasoning `groq_provider.py` already skips it for.

**First test, 1 parent + 19 subtasks (the exact shape that exposed the round-cap
bug above):** clean. 3 of 5 rounds used — round 1 created the parent, round 2
batched *all 19* subtask `create_task` calls into one round (unlike Gemini's
pacing), round 3 confirmed. 20/20 tasks, 19/19 names, zero duplicates.

**Pushed to 50 subtasks — found a worse bug.** This time the model batched the
parent's `create_task` *and* all 50 subtask `create_task` calls into a single
round. Since every call in that round is emitted before any tool result comes
back, none of the subtask calls could know the parent's real server-generated
UUID (`create_task` assigns `id = str(uuid.uuid4())`; nothing is returned until
the round's results round-trip on the next turn). Rather than omit
`parent_task_id`, the model filled it with the parent's **title string**. Nothing
validated the value at all, so all 50 subtasks silently stored a
`parent_task_id` matching no real row. Checked the blast radius directly in
`app.py`: no crash — the parent-chain walk (`task_map.get(cur['parent_task_id'])`)
just treats an unresolvable id exactly like "no parent" and gives up — but the
50 subtasks would have rendered as orphaned root-level tasks with zero visible
link to the parent, while every count/name check said "50/50, all correct."
Strictly worse than the round-cap truncation above: that one was honestly
incomplete; this one looked fully successful while being structurally broken.

**Fix, general schema invariant, not provider-specific:** `enforce_parent_exists()`
in `db.py` — same lenient silent-drop pattern as `enforce_no_self_parent`/
`enforce_recurring_invariant` already there. If `parent_task_id` is set but
doesn't match a real row, it's dropped (task becomes visibly root-level) rather
than stored as a dangling reference. Wired into both `services/ai/service.py`'s
`create_task`/`update_task` *and* `api.py`'s REST create/update — the REST path
accepts `parent_task_id` from any external caller with the exact same lack of
validation, so this was never actually an AI-only exposure, just one the AI's
batching behavior happened to trigger first. Verified three ways: (1) a live
retest of the identical 50-subtask batch — 50/50 correctly parented to the real
UUID; (2) a direct unit check that the exact bogus value from the incident
(the parent's title string) gets dropped to `None`; (3) a real, valid
`parent_task_id` and an absent one both pass through untouched.

**Separate, still-open finding: NVIDIA's gateway timed out outright on the first
50-subtask attempt** (`openai.InternalServerError: Error code: 504`) generating
that many tool calls in one round — surviving the `openai` SDK's own built-in
retry (2 attempts on 5xx/429) before raising, meaning more client-side retries
alone won't reliably fix it. A second attempt succeeded. Caught in `app.py`'s
browser chat route already (broad `except Exception` → `explain_error()`, no
corruption — just an honest incomplete parent). **Was not** caught in
`ai_routes.py`'s REST `/api/ai/chat`/`/api/ai/recommendations` — that blueprint
only caught `httpx.NetworkError`, a `google-genai`-specific exception type; an
`openai.InternalServerError` fell straight through to Flask's bare default 500
with no JSON body, breaking the API contract every other response there honors.
Fixed: both routes now catch `BudgetExceededError` distinctly (429) and any
other exception broadly (503 with a logged traceback), provider-agnostic rather
than narrowed to Gemini's own exception types. The underlying gateway-timeout
risk on very large single-round batches is not "fixed" by this — just no longer
silently ugly when it happens. Single-round batches up to 19 items tested clean
twice; 50 is where it started to strain.

### Broader NVIDIA/Gemma-4-31B-IT evaluation — tool-call reliability gap, a hybrid split, and an unrelated Gemini bug it surfaced (2026-07-14)

**Round 2 of testing** (RAG/vault Q&A, `read_document`, `find_connections`, multi-event
create, both calendar date-resolution paths including the AI-only elliptical
fallback, `get_recommendations`, psych-field inference on `create_task`) all came
back clean — as good as or better than Gemini, notably including the exact class
of date-arithmetic bug ("day after tomorrow") this file documents Gemini having
had.

**But `update_event`/`delete_event` (name → resolved-id operations) broke:**
instead of populating the real `tool_calls` field, the model sometimes wrote what
looked like a tool call as plain text in `content` — at least five distinct
malformed shapes across two short test runs (parens-call style, dict-literal
style, JSON-object-in-parens with quoted keys, all inconsistent with each other),
and using **hallucinated field names that don't match the real tool schema**
(`event_id`/`start_time`/`end_time` instead of `id`/`start_datetime`/
`end_datetime`) — a schema miss, not just a formatting miss. Confirmed this is
not a thinking-mode artifact: disabling `enable_thinking` did not improve the
failure rate (0/6 across a dedicated 3-trial-each retest, same as `enable_thinking`
on) and made responses noticeably slower. Confirmed it isn't even confined to
tool-calling requests: the same malformed text showed up once in a **plain,
tools-free `chat()` call** too (the `list_events` single-tool-call shortcut's
answer-synthesis step), meaning the quirk can leak from thinking-mode habit even
when there's no tool schema in the request to be attempting to satisfy.

**Mitigated in `nvidia_provider.py`, not "fixed":** a deterministic (no extra
model call) regex-based recovery layer. `chat_with_tools()` tries to parse the
malformed text back into a real tool call — extracting the function name and
args, aliasing the known hallucinated field names, and a generic `*_id → id`
rule (every write/delete tool's real schema names its target-row parameter
literally `id`) — and falls back to an honest "something went wrong" message if
it can't, never letting the raw `<|tool_call>...` text reach the user either way.
`chat()` gets the same never-show-raw-garbage treatment minus the recovery step
(there's no tool schema to recover into for a plain prose call): the matched
block is stripped, and if nothing legible remains, the same honest fallback is
returned. Verified against all six real malformed samples collected across both
test rounds. This is explicitly a safety net for a known quirk, not a solved
reliability problem — new malformed shapes could still appear uncaught.

**Given all this, added a hybrid mode rather than switching the default outright:**
`AIService.__init__` now takes an optional `reasoning_provider` (defaults to the
same provider passed as `provider`, so existing single-provider callers are
unaffected). `provider` remains the sole tool-decider — the whole `chat_with_tools`
loop and its inline no-tool-call replies always run on it. `reasoning_provider`
only gets the handful of already-separate, tools-free plain `chat()` calls:
`get_recommendations`, `explain_error`, the pure-task-listing bypass,
`_resolve_calendar_range`'s AI fallback, and the four single-tool-call
answer-synthesis calls (`search_vault`/`list_events`/`read_document`/
`find_connections`) — exactly the set that tested clean above. The round-cap-
exhaustion fallback (built from a conversation history containing real
`tool_calls`/`tool`-role messages) deliberately was **not** switched — untested
territory to hand a foreign tool-call history to a provider that never produced
it. Verified end-to-end with `AIService(GeminiProvider(), reasoning_provider=NvidiaProvider())`.

**That hybrid-config test surfaced a real, pre-existing, Gemini-specific bug —
unrelated to any of the above.** Asked to move an event by name ("Move X to 10am
instead"), Gemini called `update_event` with a **fabricated id matching no real
row** — no prior `list_events` call, no calendar context injected (the message
didn't contain any word `_CALENDAR_WORDS` matches, so the deterministic
events-in-system-prompt injection never fired), so it had no real data to resolve
the name from and invented a plausible-looking id anyway. `update_event`'s
`_execute_tool` handler always returned `{"success": True}` regardless of whether
the `UPDATE` actually matched a row (unlike `update_project`, which already
checked `cur.rowcount > 0`) — so the silent no-op got reported as success, and
the model confidently told the user it moved an event that was never touched.
Exactly the "silently hallucinated confirmations" failure class this file already
documents fighting for calendar chat, in a gap that fix didn't cover. Fixed the
mechanical half: `update_task`/`update_event` now check `cur.rowcount > 0` the
same way `update_project` does, so a fabricated/stale id now honestly reports
failure (surfaced to the user via the existing `_synthesize_tool_confirmation`/
fail-note machinery, unchanged) instead of a false success. The root cause — why
Gemini fabricated an id instead of calling `list_events` — is not fixed, only
made loud instead of silent when it happens.

**Dug into the root cause directly.** Reproduced with a 3-trial repro (same
"move X to 10am instead" phrasing, fresh real event each time, spying on every
`_execute_tool` call): **2 of 3 fabricated an id** (one trial asked the user for
the ID outright instead — itself a violation of a separate explicit system-prompt
rule never to do that), despite the system prompt already stating, verbatim,
"events are one `list_events` call away — resolve which item(s) a name refers to
yourself" and "Only claim an action was taken after a tool call confirms it
succeeded." One fabricated id was literally the event's own **title string**
reused as the id — the same title-as-id confusion pattern found independently in
NVIDIA/Gemma's `parent_task_id` corruption bug above, suggesting this specific
confusion is a cross-model tendency, not something particular to either provider.
With the rowcount fix in place, all fabricated-id attempts now fail honestly
(no false confirmations) — confirming that fix closes the dangerous half
regardless of root cause.

Tried a fix, and it made things worse — worth recording so it isn't retried.
Added clock-time patterns (`10am`, `3:30pm`, `noon`) to `_CALENDAR_WORDS` so a
bare time-of-day phrase would trigger the same deterministic EVENTS-block
injection weekday/date words already get. Re-ran the identical repro: **3 of 3
fabricated afterward — worse, not better.** Cause: `_CALENDAR_WORDS` matching
doesn't just add default event context, it routes the message through
`_resolve_calendar_range`, which tries its deterministic date-extraction layers
first and, finding nothing (a bare time has no actual *date* for those to grab),
falls through to its AI-guessing JSON fallback. With nothing date-like in the
message to ground it, the model hallucinated date ranges months away (October
2026, January 2027) with no basis at all, so the resulting EVENTS-block query
covered the wrong window and still contained no real data for the event being
referenced — new failure stacked on the old one instead of fixing it. Reverted
the regex change; `_resolve_calendar_range`'s AI fallback has no sanity check
against a message with zero date content producing a wildly-off answer, and
fixing *that* is a separate, deeper piece of work than this gap warranted doing
un-planned. The rowcount fix is what's actually load-bearing here.

**Net result: wired in.** `app.py`'s `_ai_service` and `ai_routes.py`'s
`_get_service()` both now construct `AIService(GeminiProvider(),
reasoning_provider=NvidiaProvider())` — Gemini remains the sole tool-decider
(unaffected by any of NVIDIA's tool-calling issues above, by construction),
NvidiaProvider only ever gets the tools-free reasoning/synthesis calls it tested
clean on. `NVIDIA_API_KEY` is consequently now a required `.env` value, not an
optional one — both providers are constructed eagerly at import time in `app.py`,
same as every other required secret in this codebase (no fallback if it's
missing, matching how `GEMINI_API_KEY`/`FLASK_SECRET_KEY`/etc. already behave).

### Training-journal tool shortcut — a fifth instance of the reasoning-provider quirk (2026-07-19)

`query_training_data` got the same single-tool-call shortcut treatment as
`search_vault`/`list_events`/`read_document`/`find_connections` (see "Tools the
AI can use" above) — but its synthesis call needed an extra fix the other four
didn't. The shortcut's tools-free synthesis call runs on `reasoning_provider`
(NVIDIA/Gemma), but was still being fed the full system prompt, including the
"TRAINING JOURNAL DATA: Use query_training_data to..." instruction block — a
paragraph that names real tools by name and tells the model to "use" them, sent
into a call with no tools actually attached. Result: the model reliably (6/6 in
live testing) hallucinated a text tool-call instead of writing a plain-language
answer, tripping `nvidia_provider.py`'s tool-call-hallucination guard (see
above) and returning its honest-failure fallback text instead of a real answer.
Fixed with `_SYSTEM_PROMPT_NO_TRAINING_TOOLS`, a derived system-prompt variant
with that instruction block sliced off, used only for this one shortcut's
synthesis call (plus one bounded retry if the fallback text still comes back,
same pattern as `extraction.py`'s retry — see Training Journal section below).
Untested whether the same failure mode reaches the other four shortcuts. They
actually carry the identical phrasing pattern already — "Use search_vault
when...", "Use read_document to...", "Use find_connections when...", "Use
list_events to..." all appear in the base prompt the same way
`query_training_data`'s instruction did — so this fix was verified for
`query_training_data` specifically (6/6 repro, then 4/4 clean after the fix)
but not proven to be unnecessary for the other four; they just haven't been
live-tested closely enough yet to know their failure rate. If one of them
turns out to need it too, the same `_SYSTEM_PROMPT_NO_TRAINING_TOOLS`-style
strip-and-reuse approach should generalize.

---

## RAG Pipeline (`services/rag/`) — How It Works and Why

### The problem it solves

An LLM has a limited context window and charges per token — you can't paste 500
notes (200k tokens) into every prompt. The naive alternative ("just summarize
everything") loses detail — you need specific facts, not averages. RAG makes the AI
retrieve only what's relevant to the current question first, then answer against
that: "here are the 5 notes most likely to be useful, now answer" instead of "here
are all 500 notes." Roughly a 40x cost reduction on knowledge queries for this
project, which is the whole reason it exists.

### Concepts

- **Embeddings** — `gemini-embedding-001` turns any string into a 3072-float vector
  representing semantic meaning (empirically confirmed via a live `embed_query()`
  call — this is the successor to `text-embedding-004`, which the codebase no longer
  uses, despite that older model's name lingering in some earlier notes). Similar
  meaning → similar direction ("fear of failure before an exam" and "test anxiety
  holding me back" land close together; "the French Revolution" and "chicken tikka
  masala" land far apart). Similarity is measured via **cosine distance** through
  pgvector's `<=>` operator — 0 means identical direction, larger means less similar
  (this is the inverse framing of "cosine similarity," where higher used to mean more
  similar under ChromaDB; every distance threshold in this codebase — e.g.
  `MAX_DISTANCE` in `retriever.py`/`engine.py` — is already tuned against the
  distance framing, unchanged by the pgvector migration since Chroma's collections
  were also configured for cosine distance internally).
- **Chunking** — notes are split into ~500-token chunks at heading/paragraph
  boundaries before embedding, not embedded whole (a 3000-word note's embedding would
  be a blurry average of all its topics — not precise enough for retrieval). Sharper
  precision, cheaper injection (500 targeted tokens instead of 3000 tangential ones).
  Each chunk stores its original text alongside its vector so it can be read back
  when retrieved.
- **Indexing** (on full-vault reindex at local server startup, and explicitly,
  synchronously, right after every vault write): read a file's bytes from Supabase
  Storage → parse YAML frontmatter → chunk the body → embed each chunk → write
  vector + text + metadata to the `vault_chunks` Postgres table. There is no
  background file-watcher anymore (`services/rag/watcher.py` was deleted, see below)
  — every write path (`vault_upload`, `vault_fetch_url`, `create_note`, move,
  delete) already calls `index_file`/`delete_file` immediately after writing, and
  Storage has no concept of an external process editing files underneath the app the
  way local disk did, so there's nothing left for a watcher to catch.
- **Retrieval** (on every AI query): embed the user's message → ask the
  `vault_chunks` table for the k nearest chunks per collection → format as context →
  prepend to the prompt. The AI never needs to know anything in advance; it just
  receives relevant context inline.
- **Storage backend** — Postgres + `pgvector`, same Supabase project as the rest of
  the app's data (migrated 2026-07-06 from local ChromaDB at `/data/chroma/`). One
  logical **collection per vault folder**, but physically a single `vault_chunks`
  table with a `collection` filter column, not one table per folder — collection
  names are dynamic/unbounded (any new vault folder becomes one), so a table-per-
  collection design would require runtime DDL every time the user creates a folder.
  `list_collections()` now returns only collections with ≥1 chunk (a collection
  "disappears" once its last chunk is deleted) — a harmless behavior difference from
  Chroma, which kept empty collection objects alive; `retriever.py` searching one
  fewer, empty collection produces identical results either way. Metadata filtering
  (e.g. `ai_generated`/`reviewed` flags) is now plain columns instead of a JSON
  metadata blob, which is why vault frontmatter conventions still matter — they map
  directly to those columns via `chunker.py`.

### What data belongs in RAG vs. not

Works well: markdown notes, class/lecture notes, journal entries, notes on people,
project docs, goals/values, reference material, AI-generated notes (flagged, never
treated as ground truth), self-model notes. Works with serialization: structured
data written as natural language (a task summary sentence embeds fine; raw JSON
doesn't). Doesn't work well: binary files without text extraction, spreadsheets/
numeric data (use SQL), very short strings (<~20 tokens — not enough semantic
signal; see the connection engine's "short notes" caveat below), live rapidly-
changing state (the task list is handled by tool-calling, not RAG), anything needing
exact match rather than semantic approximation.

### This project's specific choices

| Decision | Choice | Why |
|---|---|---|
| Embeddings model | `gemini-embedding-001` | Already paying for Gemini; same key, no extra cost |
| Vector store | Postgres + `pgvector` (Supabase) | Same provider as the rest of the app's data; no persistent-local-disk dependency, unlike local ChromaDB |
| Collections | One per vault folder (a `collection` column, not separate tables) | Scoped retrieval without runtime DDL for new folders |
| Chunk size | ~500 tokens | Precision vs. context completeness |
| Chunk splitting | Heading/paragraph boundaries | Preserves semantic units |
| Top-k default | 5 (adjustable to 10) | Enough context without token bloat |
| Re-indexing trigger | Explicit, synchronous, right after each vault write | No background watcher needed once vault content only exists in Storage |
| Retrieval modes | Passive (every query) + Active (`search_vault` tool) | Ambient vs. explicit lookup |
| AI-generated notes | `ai_generated/` folder in the vault Storage bucket, always `reviewed: false` | Never cited as ground truth |

### Component seams (for the Ship of Theseus rewrite)

Five distinct, swappable components. Contract for any replacement: app runs, tests
pass, answer quality stays the same or improves.

| Component | Does | Interface |
|---|---|---|
| `chunker.py` | Splits a note into text segments; carries `ai_generated`/`reviewed` flags read from frontmatter through to each `Chunk` | in: raw markdown string → out: list of `Chunk` objects |
| `embedder.py` | Text → vector | in: string → out: list of floats. Swappable to local models (e.g. `nomic-embed-text` via Ollama) |
| `store.py` | Stores/searches vectors (`pgvector` via psycopg2) | `upsert_chunks`, `delete_by_source`, `query_collection`, `list_collections` — public API kept stable across the ChromaDB→pgvector migration so this was the only file whose internals changed |
| `retriever.py` | Query → top-k chunks | in: query string + optional filters → out: `{text, metadata, score}` list |
| `injector.py` | Formats retrieved chunks into prompt context | in: chunk list → out: formatted string |

Other files: `indexer.py` (drives full vault indexing — reads file lists from
`services/vault/storage.py` instead of `os.walk` since 2026-07-06), `chat_indexer.py`
(indexes chat transcripts when "save to vault" is on). RAG is skipped for
short/simple messages (`_should_skip_rag`). `watcher.py` (a `watchdog`-based file
watcher) existed through 2026-07-06 and was then deleted outright, not just
disconnected — see "Vault" below for why it became unnecessary once vault content
moved to Supabase Storage.

### Two real bugs fixed here (2026-07-04)

1. `chunker.py`'s `Chunk` dataclass was missing `ai_generated`/`reviewed` fields
   entirely — this is what made `rag_test.py`'s `test_chunker_frontmatter` fail for a
   long time. Fixed: both fields are now read from the source note's real frontmatter
   and threaded through to `StoredChunk`/ChromaDB metadata.
2. `store.py`'s `query_collection` threw `TypeError: 'NoneType' object is not
   subscriptable` on `meta["source_path"]` whenever any single chunk in a collection
   had `metadata=None` — and its own blanket `try/except` silently returned `[]` for
   the **entire collection** when this happened, meaning legitimate results (from
   *any* caller — standard retrieval and the connection engine both) were silently
   dropped. Fixed to skip just the affected chunk (logging a warning) and keep the
   rest of the collection's real results. Don't reintroduce a blanket
   catch-and-swallow here — that's the anti-pattern that caused this bug in the
   first place. **This exact behavior was deliberately preserved** in the 2026-07-06
   pgvector rewrite of `store.py` — `query_collection` still skips just the offending
   row (defense in depth, even though `source_path` is now `NOT NULL`) and still
   catches the whole function into `[]` on any lower-level failure.

### A third, older bug fixed here

`app.py`'s Flask debug reloader ran the vault indexer/watcher **twice per process**
(missing a `WERKZEUG_RUN_MAIN` guard) — two live `watchdog` observers racing against
each other and against test scripts on the same persistent ChromaDB store. This
affected the whole RAG pipeline, not any one feature — it just never surfaced
visibly until the connection engine's test suite made the resulting flakiness
obvious. Fixed with a guard clause near `app.py`'s `if __name__ == "__main__":`
block. **Superseded, not just fixed:** the watcher this bug was about no longer
exists at all as of 2026-07-06 — see "Vault" below.

### A real bug caught during the pgvector migration (2026-07-06)

`query_collection`'s `ORDER BY embedding <=> %s` silently returned zero rows for
every query — no exception, no error, just empty results everywhere retrieval was
used. Cause: passing a plain Python `list[float]` as a query parameter is ambiguous
for Postgres — without an assignment target to infer the type from (unlike an
`INSERT` into a known `vector` column, which worked fine), it defaulted to
`numeric[]`, and `<=>` has no defined overload for `vector <=> numeric[]`. Fixed by
wrapping query embeddings in pgvector's `Vector(...)` wrapper type before binding
them as query parameters, which forces the correct type at bind time regardless of
SQL context. Caught by actually running `rag_test.py` end-to-end after the rewrite,
not by code review — a reminder that a migration like this isn't done until the
integration tests that exercise the real query path have actually been run, not just
until the code compiles and the individual pieces look right in isolation.

---

## Connection Engine (`services/connections/`)

Answers a different question than standard RAG: not "what's relevant to this
query" but "what does this note connect to that the user probably hasn't noticed" —
cross-folder semantic overlap, not similarity-ranked retrieval.

**Design decision:** a separate package, parallel to `services/rag/`, same "narrow
interface, independently testable" discipline. Reads the existing vector store
**read-only** (`store.list_collections`, `store.query_collection`,
`embedder.embed_query` — public functions only, never modified). Deliberately does
**not** import `retriever.py`/`injector.py` (those encode the standard pipeline's
own ranking/formatting opinions, which this layer intentionally overrides). Results
are cached in `note_connections` (Postgres) — deliberately **not** a new vector-store
collection, because `retriever.retrieve()` defaults to searching every collection
`list_collections()` returns (`target = collections or list_collections()`); a new
"connections" collection would have silently polluted ordinary passive-RAG
retrieval on every single chat message. A real graph layer (multi-hop traversal,
graph algorithms) was explicitly ruled out for v1 — this computes connections for
one note at a time, on demand, not a full offline all-pairs graph; a plausible v2 if
it proves worth investing further in.

**The "non-obvious" heuristic (v1):** embed the source note's first chunk
(frontmatter stripped, `.md` only), query every *other* collection directly via
`store.query_collection` (a wider net than `retriever.retrieve()`'s default, which
also caps at cosine distance ≤0.3 for "confident, directly relevant" matches).
Filter to a **distance band of 0.15–0.45** — below 0.15 is near-duplicate/obviously
the same topic (not an interesting "discovery"), above 0.45 is unrelated noise; the
band in between is where two notes are related enough to be a real signal but not
so related a human skimming folders would have already noticed it. **Cross-folder
only** (`target_collection != source_collection` — same-folder relationships are
the retrieval pipeline's job, and are, definitionally, less "non-obvious"). **One
match per target note** (dedupe to the single closest chunk per target note, so a
long note doesn't dominate the result list with several of its own chunks). Summary
is a **deterministic template sentence** (folder names + a small local
keyword-overlap check, not imported from `retriever.py`'s private helper) — no extra
AI call, keeping this cheap, fast, and testable without touching the AI budget. An
AI-generated "why this actually matters" explanation is a reasonable v2, not built
now.

**Surfaces in two places:** a "Related (non-obvious)" aside on
`templates/vault_file.html` (try/except-wrapped so an engine failure never breaks
the file viewer), and the `find_connections(path, k)` AI tool (same single-tool-call
shortcut treatment as `search_vault`/`list_events`/`read_document`, for the same
Gemini-flakiness reason — its results feed the same `sources`-as-footnote mechanism
above, shown as source chips).

**Known limitation (empirically found via `connection_test.py`):** short/generic
notes (e.g. a one-line grocery list) don't separate cleanly from unrelated content
by distance alone — general-purpose embeddings don't carry enough signal from very
short text to place it confidently in embedding space, so it drifts toward a middle
distance from almost everything. A one-line grocery-list-style note tested against a
deadline-anxiety journal entry landed at distance 0.40 — comfortably inside the
band, indistinguishable by distance alone from a genuinely plausible connection at
0.37–0.41. A longer, topically-specific "unrelated" note (a sourdough-starter
troubleshooting note) didn't even appear in the top 20 nearest candidates for the
same query — clean separation. Conclusion: this is intrinsic to general-purpose
sentence embeddings, not a tuning bug. Not fixed in v1 (would need a length-aware
confidence adjustment or a minimum-content-length gate — reasonable v2, not built
now); `connection_test.py`'s fixtures are deliberately longer/topically-rich to
sidestep this rather than paper over it. The 0.15–0.45 band itself (originally
0.15–0.45, tightened to 0.40 on the upper end after finding a genuinely-unrelated
test fixture landed at 0.4175, only ~0.006 from legitimate borderline matches at
0.41–0.412) was tuned against a handful of real notes plus test fixtures, not a
large real vault — may need adjustment as more content is indexed.

**Swap test for this whole subsystem:** replace `discover_connections` with a stub
returning `[]` and the rest of the app (the vault file viewer's aside, the
`find_connections` tool) degrades to "no connections found" — nothing else breaks.

`note_connections` schema:
```sql
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
```

---

## Vault (`services/vault/`, files in the Supabase Storage `vault` bucket)

**Migrated off local disk to Supabase Storage on 2026-07-06**, alongside the
database/vector-store migration, for the same reason: Vercel serverless has no
persistent local filesystem, and the vault previously lived entirely at
`data/vault/` with three independent local-disk write paths (`processor.py`'s
upload converter, `app.py`'s URL-fetch route, and the `create_note` AI tool's own
inline `open()` call). All three now go through `services/vault/storage.py`
instead — bucket keys mirror the old `folder/filename` layout exactly (e.g.
`journal/2026-07-05-note.md`), so `indexer.py`'s "first path segment = collection
name" rule needed no changes beyond swapping its input source. Storage has no real
"empty folder" concept, so creating a folder writes a zero-byte `folder/.keep`
placeholder, filtered out of listings the same way dotfiles were filtered on the old
local-disk browser.

`processor.py` handles upload/conversion (`.md .txt .pdf .html .docx .csv`, now
bytes-in/key-out rather than bytes-in/path-out) and URL fetch (fetch a URL, save its
content to the vault — still writes raw HTML directly, bypassing markdown
conversion, a pre-existing inconsistency preserved as-is through the migration, not
fixed). Also: file move/delete (native Storage `move`, not a local `os.rename`),
folder create/delete, folder picker UI. Folder taxonomy (`people / projects /
reference / journal / inbox`, plus `ai_generated/` for AI-authored notes) is a
**convention, not code-enforced** — `app.py` only sanitizes folder names to
alphanumeric/`-`/`_` and accepts any value, so a new top-level folder just works.
AI-authored notes are always `ai_generated: true`/`reviewed: false` in frontmatter,
never treated as ground truth. The `create_note` AI tool previously wrote to
`inbox/` with no flags at all, contradicting this convention — fixed to write to
`ai_generated/` with the correct frontmatter. Frontmatter fields map directly to
`vault_chunks` columns (via `chunker.py`, now reading bytes instead of a local
path), so keep frontmatter conventions consistent when adding vault-writing code.
Viewer at `/vault/file/<path>`, now backed by `storage.download()` instead of a
local file read, with a lighter `..`-segment check replacing the old
`os.path.realpath` traversal guard (there's no real filesystem path to guard
anymore, just a Storage key).

---

## Calendar (`services/calendar/`)

- **Local calendars** — full CRUD via `/calendar/api/*`, each with name/color,
  editable events.
- **ICS import** (`ics_service.py`) — paste an ICS URL onto a calendar, hit "sync,"
  events pull into the local `events` table. One-way, on-demand, not live.
- **Google Calendar** (`gcal_service.py`) — read-only, live API access.
  `list_calendars`/`list_events` back the `/calendar` page directly.
  `refresh_upcoming_cache`/`get_cached_upcoming` maintain a process-global cache
  refreshed only on page load (`/chat/<id>` and `/calendar` GET) — this is what the
  chat AI reads from; it has no tool reaching Google Calendar directly. Google
  Calendar events can never be created/updated/deleted from this app — read-only
  end to end, a deliberate trust-ramp decision (see Open Questions below).

---

## Training Journal (`services/training/`)

A "capture first, structure later" journal — a chat-style daily log of freeform
text (workouts, weight, runs, sleep, soreness, mood, whatever) — separate from
both the task execution layer and the vault/RAG knowledge layer, though it
shares the AI chat as its query surface (see below). Not yet committed to git
— built and verified across the last two sessions, currently only in the
working tree alongside the rest of the uncommitted work described in
"Working-Tree State" below.

### Design: immutable capture, lazy extraction

`training_entries` rows are raw text, written once and not touched again
except through an explicit user-initiated edit (see below) — capture is meant
to be as frictionless as typing a sentence and hitting enter, with zero
structuring cost paid at write time. Structured metrics
(`training_extractions`) are pulled out of that raw text lazily, on read, not
on every write: `extract_pending()` (`services/training/extraction.py`) runs
from the `/training/dashboard` route, exactly the same **lazy-on-read**
pattern `db.py`'s `reset_due_recurring_tasks()` already established for
recurring tasks — no scheduler, no background worker, just "do the deferred
work the next time someone actually looks." It selects up to 30
`processed = 0` entries, sends their raw text to Gemini in one batch asking it
to extract zero-or-more structured metric rows per entry, validates every
returned item before writing (unknown `source_entry_id`, invalid
`metric_type`, non-object `data`, or an out-of-range `confidence` gets
dropped, never trusted blindly — same discipline as `db.py`'s
`enforce_parent_exists`), and marks the whole batch `processed = 1`
regardless of whether anything was actually extracted from it (so a
genuinely-empty entry doesn't get retried forever). One bounded retry (never
more) covers a real, observed case: Gemini occasionally returns 0 extraction
items for a clearly-extractable batch (same 0-output-token-family quirk
documented throughout the AI Layer section) — indistinguishable from
"genuinely nothing extractable" without a retry. A real API/network failure
instead leaves the whole batch unprocessed so the next dashboard load retries
it, rather than marking entries processed and silently losing their content.

### Edit-in-place reuses the `superseded` flag, not a new mechanism

`training_extractions` was already designed append-only — reprocessing an
entry never `UPDATE`s a row, it `INSERT`s a new one and flags old ones
`superseded = 1` (so results stay reproducible against a better extraction
model later). The edit-in-place feature (`POST
/training/entry/<id>/edit`, today's entries only) reuses that exact same flag
for a second purpose: editing an entry's text sets `processed = 0` on it
(so the next dashboard load's lazy extraction re-runs) and immediately marks
its *existing* extractions `superseded = 1` — so every read path
(`query.py`'s aggregations, the dashboard, the AI's `query_training_data`
tool) automatically stops surfacing the stale pre-edit value the instant the
edit is saved, without needing to know anything changed. `POST
/training/entry/<id>/delete` is a hard delete (no soft-delete/audit row) —
also today's entries only, matching what `training.html` renders the
edit/delete affordances for; past days are read-only.

### Module layout (`services/training/`)

- `storage.py` — thin wrapper around a dedicated `training-journal` Supabase
  Storage bucket (`upload`/`download`/`delete`/`signed_url`), separate from
  the vault's `storage.py`/bucket on purpose: vault uploads trigger RAG
  indexing side effects attachments must never go through. Private bucket,
  so templates render attachments via a short-lived `signed_url()`, never a
  public path.
- `extraction.py` — `extract_pending()`, described above.
- `query.py` — the single shared read/aggregation layer (`query_rows`,
  `export_rows`, `weight_trend`, `weekly_mileage`, `one_rm_by_exercise`,
  `metric_series`) used by the dashboard route, the single-metric chart
  route, *and* all three AI tools below — one implementation, not three
  independently-drifting ones. Every function is a pure function of
  `(db, filters)` with no Flask/AI-provider imports, so it's equally callable
  from a route or a tool executor. `one_rm_by_exercise` uses the Epley
  formula; `query_rows` returns real rows (joined back to the original entry
  text) rather than a pre-aggregated answer — callers, including the AI, do
  their own counting/summing/filtering over them.
- `insights.py` — `compute_insights()`, see below.

### Routes (`app.py`)

`/training` (today's log, capture form), `/training/<date>` (read-only past
days), `POST /training/entry` (capture; multipart when attachments are
included), `POST /training/entry/<id>/edit` / `POST
/training/entry/<id>/delete` (today's entries only, see above),
`/training/dashboard` (triggers lazy extraction + insights, renders
weight/mileage/1RM charts), `/training/chart/<metric_type>` (single-metric
chart page; shared client-side rendering lives in
`static/training-charts.js`, used by both the dashboard and this page).

### Insights: deterministic, no LLM, no judgment language

`compute_insights()` (`insights.py`) is plain Python over `training_extractions`
— no LLM call at all, five threshold-gated detectors (PR, weight trend,
weekly-mileage trend, resting-HR streak, soreness/injury frequency). Each
detector states only a number and a comparison ("Weight down 2.3 lbs over the
last 2 weeks", "Resting heart rate has risen 3 mornings in a row") — no
encouragement/warning language ("great", "concerning", "you should") is
allowed in a detector's output, matching this codebase's existing stance
against the AI editorializing over the user's own data.

**A "logging streak" detector (consecutive days with an entry) was
deliberately not built.** Flagged during design as measuring app-usage
frequency rather than an actual training signal — the textbook shape of a
dark pattern regardless of how it's threshold-gated — and this project's own
stated philosophy (friction reduction, success measured as "needs the tool
less over time," not "opens it every day forever" — see "What This Is" above)
already argues against ever building one.

### AI integration — three tools, same chat, no new backend system

`services/ai/service.py` exposes `query_training_data`, `export_training_data`,
and `graph_training_metric` from the normal `/chat` assistant (see "Tools the
AI can use" under AI Layer above for the full list, and "Training-journal tool
shortcut" further down that section for a Gemini/NVIDIA-specific fix this
integration needed). `query_training_data` returns real rows for the model to
reason over — no pre-aggregation, same "dump the real data, let the model
reason" pattern the task list already uses. `export_training_data` writes a
CSV/Excel file (via `openpyxl` for `.xlsx`) to the `training-journal` bucket
and returns a signed URL; `graph_training_metric` returns a link to
`/training/chart/<type>`. The system prompt requires the model to include
either tool's returned `url` verbatim in its reply and never fabricate one.

There's also a dedicated entry point — an "Ask about your training" button on
the Training Journal page that `POST`s to the existing `/chat/new` route with
a new optional `?title=` query param, landing in a normal, pre-titled chat
thread with the same tools available. Not a separate chat system; `chat_new`'s
auto-titling logic (which otherwise renames a thread from its first message)
was taught to skip a thread that wasn't created with the "New Chat" default
title, so this pre-set title isn't immediately clobbered.

---

## Web UI (Jinja2 templates)

- `/dashboard` — active tasks + active projects overview (recurring tasks excluded)
- `/tasks` — full list, filter/sort, subtask tree view, complete toggle (recurring
  excluded); Project column visible by default (previously hidden behind "Advanced
  View" alongside the psych fields)
- `/tasks/<id>` — detail/edit, dependency viewer, parent selector, subtask tree
- `/projects`, `/projects/<id>` — list/detail; no progress bar (see Data Model)
- `/chat` — multi-chat list; `/chat/<id>` — individual chat, auto-titled, save-to-
  vault toggle, source-citation chips
- `/calendar` — month/week view merging local events, task deadlines, and live
  Google Calendar events; calendar picker; local event CRUD, GCal read-only
- `/vault`, `/vault/file/<path>` — vault browser and viewer, with the
  connection-engine "Related" aside
- `/training` — today's Training Journal log (capture form); `/training/<date>`
  — read-only past days; `/training/dashboard` — charts + deterministic
  insights; `/training/chart/<metric_type>` — single-metric chart (see
  "Training Journal" above)
- `/login` — Google OAuth entry point

A sitewide fast-capture bar (`POST /capture`, cookie-authed) lives as a single
`<input>` in `templates/layout.html`'s nav, present on every authenticated page
except `/login`. Enter submits via `fetch`, no page navigation; success clears the
field and shows a transient "Added ✓"; errors show inline. Creates a
`Task(status="inbox")`. The pre-existing dashboard-only "Quick Capture" form (title +
priority + energy_type) is a separate, intentionally-kept multi-field feature —
renamed to "Add Task (with details)" so the two don't collide in name. Known,
unfixed, non-blocking gaps: no same-page live update when capturing while sitting on
`/tasks`/`/dashboard` (only the transient status confirms it), and no global
keyboard shortcut to focus the bar.

---

## REST API (`/api/*`, bearer token)

- `GET/POST /api/tasks`, `GET/DELETE /api/tasks/<id>` — list (filter by
  status/priority/project_id), create/update, fetch, delete
- `GET/POST /api/projects`, `GET/DELETE /api/projects/<id>` — same, filter by status
- `GET /api/health` — liveness check

---

## Infrastructure / Conventions

- No migration tool — schema lives in `supabase_setup.sql`, applied once directly
  against the Supabase project's Postgres instance (not run by the app itself on
  boot, unlike the old SQLite `CREATE TABLE IF NOT EXISTS`-on-every-launch pattern).
  Schema changes going forward mean editing that file and re-running it by hand.
- Secrets only in `.env` (git-ignored): `FLASK_SECRET_KEY`, `GOOGLE_CLIENT_ID`,
  `GOOGLE_CLIENT_SECRET`, `OWNER_EMAIL`, `GEMINI_API_KEY`, `API_KEY_HASH`,
  `AI_HOURLY_BUDGET`, `FLASK_DEBUG` (opt-in, defaults off — Werkzeug's debugger is
  RCE-capable if reachable from outside localhost), plus the Supabase trio:
  `DATABASE_URL` (the **pooled** connection string, port 6543 — not the direct
  :5432 URL, to avoid exhausting Supabase's direct connection cap once many
  short-lived Vercel function instances exist), `SUPABASE_URL`, and
  `SUPABASE_SECRET_KEY` (Supabase's current name for what used to be called the
  service_role key — server-side only, never exposed to the browser).
  `api_test.js` intentionally hardcodes a *local dev* API key for test-script
  convenience — not an acceptable pattern in application code.
- `errors.log`, `costs.log` — rotating (1MB × 3 backups), git-ignored. Still local
  files even after the Supabase migration — harmless to lose on a serverless cold
  start since neither is read back by the app itself.

---

## Current Test Status (verified 2026-07-06, against Supabase)

- `python rag_test.py` — **19/19 pass**, now running against Postgres/pgvector/
  Storage fixtures instead of SQLite/ChromaDB/local disk (`write_test_note`/
  `cleanup` rewritten to go through `services/vault/storage.py`, `chunk_file`→
  `chunk_bytes`, `_get_collection` private-API reach-around→`count_by_source`)
- `python connection_test.py` — **6/6 pass**, same fixture rewrite applied
- `node api_test.js` — **29/29 pass** unchanged (it only hits `/api/*` over HTTP, no
  direct DB/vault access, so it needed no code changes — just a rotated API key,
  since the previously-hardcoded one had drifted out of sync with `.env`'s
  `API_KEY_HASH` before this session)

No known-broken tests remain. Neither test harness covers HTML-facing/cookie-authed
browser routes at all (e.g. the capture bar, the recurring-tasks modal, the
psych-field disclosure, or the rewritten vault routes) — verification for those
during the Supabase migration was ad hoc (Flask's `test_client()` with a fake
`VALID_SESSIONS` entry, exercising upload/browse/view/move/delete/folder-create-
delete end to end, plus direct Postgres/Storage queries to confirm no orphaned
rows/objects were left behind), not part of either permanent suite. This is an open
regression-protection gap, not something either harness was designed to cover.
The Training Journal (see that section above) is entirely in this same
uncovered category — its routes are cookie-authed/HTML-facing like the rest of
this list, and its three AI tools (`query_training_data`/
`export_training_data`/`graph_training_metric`) have no equivalent of
`rag_test.py`'s automated coverage yet either.

---

## What Is Not Done

### Deferred — not needed right now

- **Actually deploying to Vercel** — the groundwork is done (Postgres/pgvector/
  Storage migration, `api/index.py` + `vercel.json` entrypoint, `init_db(app)` moved
  to module level so it runs under Vercel's WSGI import too, not just
  `python app.py`), but a real `vercel dev`/preview deploy hasn't been exercised
  yet — that needs the owner's Vercel account. **Known, accepted limitation carried
  into that deploy:** `VALID_SESSIONS` (login) stays an in-memory dict by explicit
  owner decision (single-user app, not worth the complexity of moving session state
  to a shared store) — meaning logins can appear to randomly drop across multiple
  warm Vercel instances, the same root cause the AI budget guard used to have before
  it was moved to Postgres. Informed tradeoff, not an oversight.
- **Canvas ICS-to-task importer** — summer, not the pressure point right now. ICS
  calendar *event* sync already exists and is enough for the moment.
- **Proactive nudges** ("you haven't touched this in 2 weeks") — explicitly out of
  the AI layer's scope; if built, this is deterministic execution-layer logic, not
  an LLM inference. Deferred entirely.
- **Voice capture, email auto-ingest, phone widget** — stretch goals. Don't build
  now, but don't make architectural choices that would make them hard to add later.
- **A permanent regression-test harness for HTML/cookie-authed routes** (capture
  bar, recurring tasks, psych-field UI) — both current suites are scoped to
  RAG/connection-engine internals and the bearer-token REST API; neither covers
  browser routes at all.

### Needs a decision before building

- **Calendar model reliability ceiling** — decision already made (see Calendar-
  aware chat section above): stick with `gemini-2.5-flash-lite`, invest in
  engineering rather than a pricier model, revisit only if that provably can't
  close the gap.

### Dropped or already solved differently

MySQL/SQLAlchemy migration (not needed at current scale); extra project fields
(explicitly decided against — projects stay minimal); separate recommendation API
endpoints (handled by chat); numeric scoring engine (dropped for LLM reasoning);
separate subtasks table (solved via `parent_task_id`); separate tags/dependencies/
notes tables (solved via JSON columns); in-memory chat history (solved, persisted to
DB); Obsidian integration (decided against, custom vault instead); external
calendar imports (solved, read-only Google Calendar).

---

## Open Questions (Unresolved)

1. **AI budget** — $0.15/hour as of 2026-07-03. Owner is open to spending more and
   would consider self-hosting if cost becomes the bottleneck — AI spend is
   instrumental to reducing long-run tool dependency, not something to minimize for
   its own sake. Revisit once daily-use patterns clarify if $0.15 is enough headroom.
2. **Psych-field reasoning surfacing (UI polish)** — the current answer is one
   combined free-text `psych_reasoning` note shown inside the disclosure. Whether a
   richer UI (inline tooltip, per-field breakdown, chat-style explanation) is worth
   the complexity is still undecided — revisit if the combined-text version turns
   out to be confusing in practice.
3. **Trust-ramp criteria** — what specifically has to be true before Google Calendar
   (or any other external system) moves from read-only to write-enabled? Not yet
   defined — currently just "prove reliability first," no concrete bar.
4. **Connection-engine distance band at scale** — 0.15–0.40 was tuned against a
   handful of real notes plus test fixtures, not a large real vault — may need
   adjustment as more content is indexed.
5. **Session storage on serverless** — `VALID_SESSIONS` staying in-memory (owner's
   explicit call, see "What Is Not Done" above) means login can behave inconsistently
   once more than one warm Vercel instance exists. Not planned to be revisited unless
   it's actually disruptive in practice after a real deploy.

---

## Working-Tree State (as of 2026-07-06)

Two rounds of uncommitted work now sit in the local tree, neither committed
(deliberately deferred by the owner, to be handled separately):

1. The 2026-07-04 batch — capture bar, psych-field collapse, source citations,
   connection engine v1, recurring tasks, the tech-debt/timezone/`db_push` fixes, and
   the two RAG bug fixes described above.
2. The 2026-07-06 Supabase migration — `db.py` (psycopg2 + `_PGConnection` shim),
   `services/rag/store.py` (full pgvector rewrite), the new
   `services/vault/storage.py` module, every vault read/write call site in `app.py`/
   `processor.py`/`service.py`, `services/ai/budget.py` (Postgres-backed rolling
   window), `services/rag/chunker.py`/`indexer.py` (bytes-based, Storage-driven),
   deletion of `services/rag/watcher.py`, the `INSERT OR REPLACE`→`ON CONFLICT` and
   `datetime`→`.isoformat()` fixes in `classes/Task.py`/`classes/Project.py`, the
   `rag_test.py`/`connection_test.py` fixture rewrites, a rotated API test key, and
   new files `supabase_setup.sql`, `api/index.py`, `vercel.json`.

`git status --short` currently shows ~20 modified files, one deletion
(`services/rag/watcher.py`), and 4 new paths (`api/`, `services/vault/storage.py`,
`supabase_setup.sql`, `vercel.json`). No destructive git operations have been run.
Check `git status`/`git diff` before assuming `HEAD` reflects current functionality
— it doesn't; `HEAD` is still on plain SQLite/ChromaDB/local-disk.

A third, separate body of uncommitted work also now sits in the tree (built
across two sessions after the above, not yet folded into this section's own
"as of 2026-07-06" snapshot): the Training Journal feature — the new
`services/training/` package, its three templates, `static/training-charts.js`,
the `training_entries`/`training_attachments`/`training_extractions` tables in
`supabase_setup.sql`, the training routes in `app.py`, the three new AI tools
and `_SYSTEM_PROMPT_NO_TRAINING_TOOLS` in `services/ai/service.py`, and the two
general bugfixes (`db.py`'s stale-connection reconnect, `gemini_provider.py`'s
`None`-content guard) it surfaced. See the "Training Journal" section above for
the full picture — like the other two rounds here, deliberately not yet
committed.

**Not yet done, flagged explicitly:** the 2026-07-04 batch has not been exercised in
a real browser by an AI session (verification used `py_compile`, direct Python
calls, and Flask's headless `test_client()` — no rendered CSS, no clicking through
the recurring modal). The 2026-07-06 Supabase migration was verified more thoroughly
end-to-end against the real Supabase project — every vault HTTP route through
`test_client()`, `rag_test.py` (19/19), `connection_test.py` (6/6), `api_test.js`
(29/29), and the budget guard's `BudgetExceededError` path — but an actual Vercel
deploy has not been attempted; that's the one remaining unverified step (see "What
Is Not Done" above). The owner is handling final browser verification and the
Vercel deploy itself.

---

## Running Locally

Requires a Supabase project (see "Stack" above for why) — create one, then run
`supabase_setup.sql` once against it (Supabase's SQL Editor, or
`psql "$DATABASE_URL" -f supabase_setup.sql`) to create the schema, enable the
`vector`/`pgcrypto` extensions, and create the private `vault` Storage bucket.

```bash
cp .env.example .env
# Fill in FLASK_SECRET_KEY, GOOGLE_CLIENT_ID/SECRET, OWNER_EMAIL,
# API_KEY_HASH (via gen_api_key.py), GEMINI_API_KEY, optionally
# GROQ_API_KEY and AI_HOURLY_BUDGET, plus the Supabase trio:
# DATABASE_URL (pooled connection string, port 6543), SUPABASE_URL,
# SUPABASE_SECRET_KEY. FLASK_DEBUG defaults to false — only set it
# true for local dev; never on anything reachable off localhost.

python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python app.py          # Flask dev server, port 5000
```

Needs a Google Cloud Console app with `http://localhost:5000/authorize` as an
authorized redirect URI.

### Testing

No pytest / test framework. Testing is via standalone scripts that hit a running
server (must run `python app.py` in another terminal first):

```bash
python rag_test.py        # RAG pipeline suite; writes rag_test_results.txt
python connection_test.py # Connection-engine suite; writes connection_test_results.txt
node api_test.js          # REST API suite (Node 18+, no deps); real API key hardcoded
                           # at the top of the file for local dev convenience — update
                           # it there too if you regenerate the key
```

### Deploying to Vercel

Groundwork is in place but not yet exercised against a real Vercel project (see
"Working-Tree State" above):

- `api/index.py` (`from app import app`) and `vercel.json` route every request to
  it via `@vercel/python`.
- Vercel imports `app` as a WSGI callable directly — `if __name__ == "__main__":`
  never runs there, which is why `init_db(app)` was moved to module level in
  `app.py` rather than left inside that block.
- Set every `.env` variable above as a Vercel project env var, plus add
  `https://<project>.vercel.app/authorize` as a second authorized redirect URI in
  Google Cloud Console.
- No app-level init/reindex step is needed on Vercel — Storage-backed vault content
  already exists from prior indexing, unlike a fresh local clone's empty state.
