# Personal Second Brain / Execution OS

_This file is meant to be the single, complete, pasteable source of truth for this
project — everything worth knowing, ordered from most to least important. If you're
an AI being handed this file with no other repo access, read top to bottom; by the
time you reach "Running Locally" at the end you have the full picture. Last synced
to actual codebase state: 2026-07-04._

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
| Database | SQLite (`dev.db`), raw SQL, no ORM |
| Templates | Jinja2 server-rendered HTML |
| AI | Gemini 2.5 Flash Lite via `google-genai` SDK |
| Auth | Google OAuth 2.0 (Authlib), single-owner email gate |
| Config | `.env` file |
| Vector store | ChromaDB (`/data/chroma/`), local, disk-persisted |
| Embeddings | `text-embedding-004` (Google) |
| Calendar sync | Google Calendar API (read-only) + `icalendar` for one-way ICS import |
| Date parsing | `dateparser` (deterministic NL date parsing, no AI) |

SQLite was chosen deliberately: single user, no concurrent writes, no operational
overhead. SQLAlchemy/Flask-Migrate/MySQL were considered and rejected for this use
case — don't introduce an ORM or a different DB without checking with the owner
first; this gets revisited only at actual deployment time, not preemptively.

---

## Architecture — Entry Points & File Map

- `app.py` — Flask app setup, OAuth, all HTML-rendering routes (dashboard, tasks,
  projects, chat, calendar, vault browser). Large, flat file (~1200+ lines); routes
  aren't split into further blueprints beyond `api`/`ai`.
- `api.py` — `/api/*` blueprint. Bearer-token REST API (CRUD on tasks/projects,
  health check) for external callers (e.g. N8N).
- `ai_routes.py` — `/api/ai/*` blueprint. `GET /api/ai/recommendations` and
  `POST /api/ai/chat`, both bearer-token gated.
- `db.py` — raw `sqlite3` access via Flask's `g`, plus `init_db()` which creates all
  tables with `CREATE TABLE IF NOT EXISTS`. **No migration system** — schema changes
  are additive edits to this script, backward-compatible by construction. Also holds
  `enforce_recurring_invariant()` (shared between the REST API and the AI tool
  executor) and `reset_due_recurring_tasks()` (see Data Model below).
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

**Auth is two separate, non-interchangeable mechanisms:** `api.py` and
`ai_routes.py` gate on `Authorization: Bearer <key>` checked against `API_KEY_HASH`
(SHA-512) via `hmac.compare_digest`. The browser-facing routes in `app.py` instead
gate on a cookie (`UserID`) looked up in the in-memory `VALID_SESSIONS` dict, which
resets on server restart.

---

## Data Model (SQLite, `dev.db`, raw SQL — no ORM)

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
vault file are best-effort cleaned up on vault delete/move, mirroring how the
ChromaDB index is already cleaned up on delete.

---

## AI Layer (`services/ai/`)

- `provider.py` — `AIProvider` interface (`chat`, `chat_with_tools`); swap backends
  by implementing it. `gemini_provider.py` (primary, Gemini 2.5 Flash Lite, full
  tool-calling) and `groq_provider.py` (implemented, not actively used).
- `service.py` (~1100+ lines) — the core: system prompt, the agentic tool-calling
  loop (up to 5 rounds per message), all calendar-date-resolution logic, task/
  project/calendar context injected on every call.
- **Tools the AI can use:** `create_task`, `update_task`, `delete_task`,
  `create_project`, `delete_project`, `read_document`, `search_vault`, `create_note`,
  `list_events`, `create_event`, `update_event`, `find_connections`. Any new AI tool
  that returns vault/note content should follow the same single-tool-call shortcut
  pattern as `search_vault`/`read_document`/`list_events`/`find_connections` (bypass
  the tool-response round-trip, inject results via a fresh plain-chat call instead of
  trusting the tool-response round-trip) — this works around a documented Gemini bug
  where it occasionally emits 0 output tokens on the round right after a real tool
  call.
- `AIService.chat()` returns `(reply_text, sources)`, not a bare string — every call
  site (`app.py`, `ai_routes.py`, `rag_test.py`) unpacks the tuple. `sources` feeds
  the chat UI's citation chips and is populated even by the single-tool shortcut
  paths that bypass the normal round-trip.
- `budget.py` — rolling 1-hour wall-clock dollar-cost window across all API calls
  (generative + embedding); raises `BudgetExceededError` over the limit, recovers as
  old calls age out. In-memory only (clears on restart, intentional — not persisted
  to disk). Cost derived from `prompt_token_count` + `tool_use_prompt_token_count`
  (input) and `candidates_token_count` + `thoughts_token_count` (output) — earlier
  versions undercounted by dropping the tool-use/thinking-token fields. Configurable
  via `AI_HOURLY_BUDGET` (currently $0.15/hour, raised 2026-07-03 from an initial
  $0.05 placeholder). Every call logged to `costs.log`.
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

- **Embeddings** — `text-embedding-004` turns any string into a 768-float vector
  representing semantic meaning. Similar meaning → similar direction ("fear of
  failure before an exam" and "test anxiety holding me back" land close together;
  "the French Revolution" and "chicken tikka masala" land far apart). Similarity is
  measured via **cosine similarity** (1.0 = identical, 0 = unrelated, -1 = opposite);
  relevant matches typically score 0.7–0.9.
- **Chunking** — notes are split into ~500-token chunks at heading/paragraph
  boundaries before embedding, not embedded whole (a 3000-word note's embedding would
  be a blurry average of all its topics — not precise enough for retrieval). Sharper
  precision, cheaper injection (500 targeted tokens instead of 3000 tangential ones).
  Each chunk stores its original text alongside its vector so it can be read back
  when retrieved.
- **Indexing** (on startup, and automatically on file change via `watchdog`): read
  markdown → parse YAML frontmatter → chunk the body → embed each chunk → write
  vector + text + metadata to ChromaDB.
- **Retrieval** (on every AI query): embed the user's message → ask ChromaDB for the
  k nearest stored chunks → format as context → prepend to the prompt. The AI never
  needs to know anything in advance; it just receives relevant context inline.
- **ChromaDB** — local, in-process, disk-persisted at `/data/chroma/`. One
  **collection per vault folder** (so retrieval can be scoped — a project question
  doesn't search journal entries). Supports metadata filtering (e.g. exclude
  `ai_generated=true`), which is why vault frontmatter conventions matter — they map
  directly to ChromaDB metadata.

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
| Embeddings model | `text-embedding-004` | Already paying for Gemini; same key, no extra cost |
| Vector store | ChromaDB, local | Zero ops overhead, fits single-user |
| Collections | One per vault folder | Scoped retrieval |
| Chunk size | ~500 tokens | Precision vs. context completeness |
| Chunk splitting | Heading/paragraph boundaries | Preserves semantic units |
| Top-k default | 5 (adjustable to 10) | Enough context without token bloat |
| File watcher | `watchdog` | Auto re-index on vault changes |
| Retrieval modes | Passive (every query) + Active (`search_vault` tool) | Ambient vs. explicit lookup |
| AI-generated notes | `/data/vault/ai_generated/`, always `reviewed: false` | Never cited as ground truth |

### Component seams (for the Ship of Theseus rewrite)

Five distinct, swappable components. Contract for any replacement: app runs, tests
pass, answer quality stays the same or improves.

| Component | Does | Interface |
|---|---|---|
| `chunker.py` | Splits a note into text segments; carries `ai_generated`/`reviewed` flags read from frontmatter through to each `Chunk` | in: raw markdown string → out: list of `Chunk` objects |
| `embedder.py` | Text → vector | in: string → out: list of floats. Swappable to local models (e.g. `nomic-embed-text` via Ollama) |
| `store.py` | Stores/searches vectors (ChromaDB wrapper) | `upsert(id, vector, text, metadata)`, `query(vector, k, filters)` — could become SQLite+manual cosine, Weaviate, pgvector |
| `retriever.py` | Query → top-k chunks | in: query string + optional filters → out: `{text, metadata, score}` list |
| `injector.py` | Formats retrieved chunks into prompt context | in: chunk list → out: formatted string |

Other files: `indexer.py` (drives full vault indexing), `watcher.py` (watchdog
re-indexing — guarded against Flask's debug reloader double-starting it, see the
`WERKZEUG_RUN_MAIN` bug below), `chat_indexer.py` (indexes chat transcripts when
"save to vault" is on). RAG is skipped for short/simple messages
(`_should_skip_rag`).

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
   first place.

### A third, older bug fixed here

`app.py`'s Flask debug reloader ran the vault indexer/watcher **twice per process**
(missing a `WERKZEUG_RUN_MAIN` guard) — two live `watchdog` observers racing against
each other and against test scripts on the same persistent ChromaDB store. This
affected the whole RAG pipeline, not any one feature — it just never surfaced
visibly until the connection engine's test suite made the resulting flakiness
obvious. Fixed with a guard clause near `app.py`'s `if __name__ == "__main__":`
block.

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
are cached in `note_connections` (SQLite) — deliberately **not** a new ChromaDB
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

## Vault (`services/vault/`, files at `data/vault/`, git-ignored)

`processor.py` handles upload/conversion (`.md .txt .pdf .html .docx .csv`) and URL
fetch (fetch a URL, save its content to the vault). Also: file move/delete, folder
create/delete, folder picker UI. Folder taxonomy is **fixed**: `people / projects /
reference / journal / inbox`, plus `ai_generated/` for AI-authored notes — always
`ai_generated: true`/`reviewed: false` in frontmatter, never treated as ground
truth. The `create_note` AI tool previously wrote to `inbox/` with no flags at all,
contradicting this convention — fixed to write to `ai_generated/` with the correct
frontmatter. Frontmatter fields map directly to ChromaDB metadata (via
`chunker.py`), so keep frontmatter conventions consistent when adding vault-writing
code. Viewer at `/vault/file/<path>`.

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

- No migration tool — schema changes go directly into `CREATE TABLE IF NOT EXISTS`
  blocks in `db.py`, additive/backward-compatible by construction.
- Secrets only in `.env` (git-ignored): `FLASK_SECRET_KEY`, `GOOGLE_CLIENT_ID`,
  `GOOGLE_CLIENT_SECRET`, `OWNER_EMAIL`, `GEMINI_API_KEY`, `API_KEY_HASH`,
  `AI_HOURLY_BUDGET`. `api_test.js` intentionally hardcodes a *local dev* API key for
  test-script convenience — not an acceptable pattern in application code.
- `errors.log`, `costs.log` — rotating (1MB × 3 backups), git-ignored.

---

## Current Test Status (verified 2026-07-04)

- `python rag_test.py` — **19/19 pass** (the last remaining failure,
  `test_chunker_frontmatter`, is now fixed — see RAG Pipeline bugs above)
- `python connection_test.py` — **6/6 pass**, stable across repeated runs
- `node api_test.js` — **29/29 pass**

No known-broken tests remain. Neither test harness covers HTML-facing/cookie-authed
browser routes at all (e.g. the capture bar, the recurring-tasks modal, the
psych-field disclosure) — verification for those has been ad hoc (Flask's
`test_client()`, manual DB checks, and manual browser use), not part of either
permanent suite. This is an open regression-protection gap, not something either
harness was designed to cover.

---

## What Is Not Done

### Deferred — not needed right now

- **Deployment** — local-only is fine; not currently blocking daily use. SQLite-vs-
  something-else gets revisited at that point, not before.
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

---

## Working-Tree State (as of 2026-07-04)

A large amount of work — the capture bar, psych-field collapse, source citations,
connection engine v1, recurring tasks, the tech-debt/timezone/`db_push` fixes, and
the two RAG bug fixes described above — exists in the local working tree but has
**not been committed** (deliberately deferred by the owner, to be handled
separately). `git diff --stat` touches ~24 files plus new ones (`services/
connections/`, `templates/_recurring_modal.html`). No destructive git operations
have been run. Check `git status`/`git diff` before assuming `HEAD` reflects current
functionality — it doesn't.

**Not yet done, flagged explicitly:** none of this has been exercised in a real
browser by an AI session — verification so far has used `py_compile`, direct Python
calls, and Flask's headless `test_client()` (real routing/view code and real cookie
round-trips, but no rendered CSS, no clicking through the recurring modal, no visual
check of the disclosure/progress-bar-removal layout). The owner is handling this
pass themselves.

---

## Running Locally

```bash
cp .env.example .env
# Fill in FLASK_SECRET_KEY, GOOGLE_CLIENT_ID/SECRET, OWNER_EMAIL,
# API_KEY_HASH (via gen_api_key.py), GEMINI_API_KEY, optionally
# GROQ_API_KEY and AI_HOURLY_BUDGET

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
