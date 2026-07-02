# Personal Execution OS — Status Report
_Last updated: 2026-07-02_

---

## What This Is

A single-user cognitive infrastructure system whose actual design goal is
to eliminate friction now while making the user need it less over time —
success is not "opens this every day forever," it's "gets moving in the
first 30 seconds, and is faster/sharper a year from now with or without
the tool." The dashboard's job is to unstick the user, not micromanage
them through the whole task. Tasks carry psychological fields (fear,
ambiguity, energy type, effort) but these are AI-inferred in the
background, not user-facing form fields, and the AI must show its
reasoning when it infers them. An AI assistant (Gemini) can read and
mutate the task list, local calendar, and vault through a chat interface
— every action it takes must be visible to the user, nothing silent. This
is a private tool, not a SaaS product, and it's built for exactly one user
indefinitely — no multi-tenancy, no monetization.

**Full requirements/rationale**: see `overall_prompt.txt` (redrafted
2026-07-02 from `requirements_questionnaire.txt`).

**Who's building this:** one person, solo, no team.

**Development approach:** "vibe code" a full feature fast (including AI-generated code the developer doesn't yet own line-by-line), understand the resulting system design, then replace each component with hand-written code one at a time ("Ship of Theseus") once it's well understood. A replacement is only considered valid once the app retains full functionality and tests pass. So parts of this codebase are intentionally rough first-pass AI output awaiting a rewrite — that's the plan, not an oversight.

Occasional references below to "the roadmap" mean the developer's own original planning notes (not attached to this file) — the relevant specifics are quoted inline wherever mentioned, so no external document is needed to follow this report.

---

## Stack (Actual, Right Now)

| Layer | Technology |
|---|---|
| Backend | Flask (Python) |
| Database | SQLite (`dev.db`), raw SQL |
| Templates | Jinja2 server-rendered HTML |
| AI | Gemini 2.5 Flash Lite via `google-genai` SDK |
| Auth | Google OAuth 2.0 (Authlib), single-owner email gate |
| Config | `.env` file |
| Vector Store | ChromaDB (`/data/chroma/`) |
| Embeddings | `text-embedding-004` (Google) |
| Calendar sync | Google Calendar API (read-only), `icalendar` for ICS import |
| Date parsing | `dateparser` (deterministic NL date parsing, no AI) |

---

## What Is Done

### Authentication
- Google OAuth login (`/login` → `/auth/start` → `/authorize`)
- Email check against `OWNER_EMAIL` env var — anyone else is rejected
- UUID cookie + server-side `VALID_SESSIONS` dict (supports multiple devices)
- All routes are protected; unauthenticated requests redirect to login
- OAuth scope now also requests `calendar.readonly`; access/refresh tokens stored in the `tokens` table and auto-refreshed when near expiry

### Database Schema
SQLite tables:

**`tasks`**
- Core fields: `id`, `title`, `description`, `status`, `priority`, `due_date`, `completed_at`
- Psychological fields: `fear_level` (1–5), `ambiguity_level` (1–5), `energy_type`, `estimated_effort`
- Relational fields: `project_id`, `parent_task_id` (subtasks via self-reference)
- Extra fields: `source_type`, `ai_generated`, `created_at`, `updated_at`
- JSON columns: `tags`, `dependencies`, `task_notes`

**`projects`**
- Fields: `id`, `title`, `description`, `status`, `progress`, `created_at`, `updated_at`

**`chats` + `chat_messages`**
- `chats`: `id`, `title`, `indexed`, `created_at`, `updated_at`
- `chat_messages`: `id`, `chat_id`, `role`, `content`, `created_at`
- Chat history is persisted to DB; AI retains conversation context across server restarts
- `indexed` flag controls whether a chat gets embedded into the vault's vector store

**`calendars`**
- Fields: `id`, `name`, `color`, `source` (`local`/ICS), `ics_url`, `visible`, `created_at`, `updated_at`
- Local calendars are user-created; ICS calendars sync on demand via a "sync" button (pulls and stores events)

**`events`**
- Fields: `id`, `calendar_id`, `title`, `description`, `start_datetime`, `end_datetime`, `all_day`, `location`, `source_uid`, `created_at`, `updated_at`
- Only local + ICS-synced events are stored here. Google Calendar events are never written to this table — they're fetched live/cached (see Calendar section below) and stay read-only.

**`tokens`**
- Fields: `provider` (PK, e.g. `'google'`), `access_token`, `refresh_token`, `token_type`, `expires_at`
- Holds the Google OAuth token used for both login identity and Calendar API reads

### Web UI (Jinja2 Templates)
- `/dashboard` — active tasks + active projects overview
- `/tasks` — full task list with filter/sort controls, tree view for subtasks, complete toggle
- `/tasks/<id>` — task detail: edit all fields, dependency viewer, parent task selector, subtask tree, breadcrumb
- `/projects` — project list
- `/projects/<id>` — project detail + tasks belonging to project
- `/chat` — multi-chat list view; each chat is a persistent session
- `/chat/<id>` — individual chat with auto-title on first message, save-to-vault toggle
- `/calendar` — month/week calendar view (FullCalendar-style), merges local events, task deadlines, and live Google Calendar events; calendar picker for which local/GCal calendars to show; create/edit/delete for local events, read-only display for Google events
- `/vault` — vault file browser with folder tree
- `/vault/file/<path>` — vault file viewer
- `/login` — Google OAuth entry point

### REST API (`/api/*`, bearer token auth)
- `GET/POST /api/tasks` — list (filterable by status/priority/project_id) + create/update
- `GET/DELETE /api/tasks/<id>` — fetch + delete
- `GET/POST /api/projects` — list (filterable by status) + create/update
- `GET/DELETE /api/projects/<id>` — fetch + delete
- `GET /api/health` — liveness check

### Calendar (`/services/calendar/`, `/calendar/*` routes)
- **Local calendars** — full CRUD (`/calendar/api/calendars`), each with a name/color, editable events
- **ICS import** (`ics_service.py`) — paste an ICS URL onto a calendar, hit "sync," events get pulled and stored in the local `events` table (one-way, on-demand, not live)
- **Google Calendar** (`gcal_service.py`) — read-only, live API access:
  - `list_calendars(db)` / `list_events(db, calendar_id, time_min, time_max)` — direct Google Calendar API calls, used by the `/calendar` page for on-screen display
  - `refresh_upcoming_cache(db, days_back=60, days_ahead=60)` / `get_cached_upcoming()` — a process-global cache refreshed on **page load only** (`/chat/<id>` and `/calendar` GET routes), never triggered by the AI. This is what the chat AI reads from — it has no tool that can reach Google Calendar directly, by design, so it can't fabricate having checked something it never fetched.
  - Google Calendar events can never be created/updated/deleted from this app — read-only end to end

### AI Layer (`/services/ai/`)
- **Provider abstraction** (`provider.py`) — interface any LLM backend implements
- **Gemini provider** (`gemini_provider.py`) — Gemini 2.5 Flash Lite with full tool-calling support
- **Groq provider** (`groq_provider.py`) — exists but not actively used
- **AIService** (`service.py`) — agentic loop (up to 5 tool-call rounds), task/project/calendar context injected on every call
- **Tools the AI can use**: `create_task`, `update_task`, `delete_task`, `create_project`, `delete_project`, `read_document`, `search_vault`, `create_note`, `list_events`, `create_event`, `update_event`
- **Budget guard** (`budget.py`) — rolling 1-hour window (not tied to session or server restart): sums the dollar cost of every API call (generative + embedding) in the trailing 60 minutes and raises `BudgetExceededError` if it exceeds the limit; recovers automatically as old calls age out of the window. Configurable via `AI_HOURLY_BUDGET` env var (defaults to $0.05/hour). Logs every call's token counts and running hourly total to `costs.log`.
- **Recommendations endpoint** (`GET /api/ai/recommendations`) — returns top 3 tasks + an insight, JSON
- **Web chat** — multi-session, DB-persisted, auto-titled, supports save-to-vault

#### Calendar-aware chat (added 2026-07-01)
This was the source of a long debugging session and is worth documenting in detail since the failure modes were subtle:

- **Client-side timezone**: every chat message sends the browser's IANA timezone (`Intl.DateTimeFormat().resolvedOptions().timeZone`) to the server; `chat()` converts server UTC time into that zone via `zoneinfo` for the "current date" the model reasons from. Falls back to UTC if missing/invalid. (Fixes: assistant previously used server UTC date, which could be a day ahead of the user's actual local date.)
- **Deterministic date-range resolution** (`_resolve_calendar_range` and helpers) — tried delegating "what date range does this message mean" to the AI; it was unreliable (wrong year, "last Sunday" computed as a week off depending on what day today was, "day after tomorrow" computed as plain "tomorrow"). Replaced with a layered deterministic pipeline, AI only as last resort:
  1. `_weekday_range` — hand-written arithmetic for "last/next/this `<weekday>`" (neither the AI nor the `dateparser` library handles this reliably — confirmed by testing both)
  2. `_relative_day_range` — hand-written arithmetic for today/tomorrow/yesterday/day-after-tomorrow/day-before-yesterday/"N days ago"/"N days from now"
  3. `_absolute_date_range` — uses the `dateparser` library (`search_dates`) for explicit dates ("June 28th", "the 15th", "07/15", "in two weeks"), with a guard against dateparser false-positives on stray words like "on"
  4. AI fallback (with a short conversation excerpt) — only reached for genuinely anaphoric follow-ups with no date words at all, e.g. "what about the day after?" referring back to a prior turn about "tomorrow"
  5. Default: today → +14 days, for generic questions ("what's on my calendar")
- **Passive calendar context injection** — gated by a `_CALENDAR_WORDS` regex (checks the last 2 user turns, so a calendar conversation survives one keyword-less follow-up) that merges local `events` rows with `gcal_service.get_cached_upcoming()`, filtered to the resolved date range, into the system prompt as an `EVENTS (...)` block.
- **No read/write classification anymore** — there used to be a regex (`_CALENDAR_WRITE`) that guessed whether a message was a read or a write and routed reads through a no-tools "bypass" path for reliability. This caused a real bug: "Block 2 hours of my day tomorrow" didn't match any write verb, got routed to the no-tools path, and the model fabricated a fake confirmation ("I've blocked 2 hours... 9-11am") because it structurally could not have created anything. **Fixed** by removing the classifier entirely — tools (`create_event`, `update_event`) are always available whenever calendar context is in play; the model's own tool-calling judgment decides, never a Python heuristic.
- **Tool confirmation fallback** — Gemini occasionally returns 0 output tokens on the round right after a real tool call (a known flakiness, previously documented only for `search_vault`). `_synthesize_tool_confirmation` now builds a plain confirmation directly from the tool's actual result (e.g. `Created "Study Calculus".`) whenever the model's own follow-up text comes back empty, so the user is never shown a blank reply after a real write.
- **Single-event-per-request rule** — the model was sometimes creating two duplicate events for one ambiguous-time request (e.g. "block 2 hours tomorrow" with no time given → two separate blocks at different times). Fixed with an explicit system-prompt rule: pick one reasonable time and call `create_event` exactly once.
- **Known residual issue**: even with verified-correct injected context, `gemini-2.5-flash-lite` occasionally misreads/mislabels it (e.g. calling the right date "tomorrow" instead of "the day after tomorrow," or claiming "no events" when the block wasn't empty) — measured at roughly 1-in-4 in a repeated identical-input stress test. This is a model-capability ceiling, not a logic bug; every deterministic piece upstream of the final answer has been verified correct. Options if it becomes disruptive: route calendar turns to a stronger model, or add a post-hoc check that cross-references the model's claims against the actual injected event list.

### Vault (`/services/vault/`, `/data/vault/`)
- File upload with format conversion: `.md`, `.txt`, `.pdf`, `.html`, `.docx`, `.csv`
- URL fetch — fetches a URL and saves the content to the vault
- File move and delete
- Folder create and delete
- Folder picker UI for upload and move operations
- Vault file viewer at `/vault/file/<path>`
- Folder taxonomy: people / projects / reference / journal / inbox

### RAG Pipeline (`/services/rag/`)
- **Chunker** (`chunker.py`) — splits documents into ~500-token chunks
- **Embedder** (`embedder.py`) — `text-embedding-004` via Google
- **Store** (`store.py`) — ChromaDB wrapper, per-folder collections
- **Indexer** (`indexer.py`) — indexes vault files into ChromaDB
- **Retriever** (`retriever.py`) — semantic search over indexed chunks
- **Injector** (`injector.py`) — injects retrieved context into AI prompts
- **Watcher** (`watcher.py`) — watchdog file watcher for vault changes
- **Chat indexer** (`chat_indexer.py`) — indexes chat history into the vector store, when a chat has "save to vault" enabled
- RAG is skipped for short/simple messages (`_should_skip_rag`)
- `read_document` AI tool — lets the AI read a full vault file on demand
- `search_vault` AI tool — semantic lookup, with the same "Gemini emits 0 tokens after a tool round" workaround as calendar tools (retrieval is done server-side and injected via a fresh plain-chat call instead of trusting the tool-response round-trip)

### Infrastructure
- Rotating error log (`errors.log`, 1MB × 3 backups)
- Rotating cost log (`costs.log`, 1MB × 3 backups)
- All secrets in `.env` (`FLASK_SECRET_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `OWNER_EMAIL`, `GEMINI_API_KEY`, `API_KEY_HASH`, `AI_HOURLY_BUDGET`)

---

## What Is Not Done

### Definitely Still Needed

**1. Inbox / fast-capture interface**
An ultra-low-friction capture screen — type a task title, hit enter, done. Right now you have to fill out a form on `/tasks`. Chat already works as a capture path (describe a task conversationally, AI calls `create_task`), but the dedicated sitewide capture bar doesn't exist yet — both are meant to be equally first-class entry points into the same inbox, not one primary/one fallback.

**2. Hide psych fields from the UI, keep AI inference**
`fear_level`, `ambiguity_level`, `energy_type`, `estimated_effort` are currently exposed as editable form fields on `/tasks/<id>`. That's now explicitly the wrong UI — these should never be default-visible data entry. Needed: (a) collapse them behind an opt-in/expand control, off by default, (b) have the AI infer them automatically when ranking/recommending tasks, (c) surface the AI's reasoning for each inference (not just the label) so the user can see and correct it.

**3. Connection-engine layer on top of RAG**
Standard retrieval (chunk → embed → store → retrieve → inject) is built and should keep working untouched. New, explicitly parallel effort: a layer that surfaces non-obvious connections between vault notes and can actively challenge how the user is thinking about something — not just similarity-ranked retrieval. Should be started soon per the redraft, built with the same narrow-interface discipline as the rest of the RAG stack so it doesn't destabilize existing retrieval while both evolve.

**4. Source citations shown separately from AI answers**
When the AI answers using vault content (RAG or the future connection engine), the source should be visible as a separate footnote/aside — not inline in the answer text. Not yet implemented in the chat UI.

### Deferred — Not Needed Right Now

**Deployment.** Local-only is fine for now; going live isn't currently blocking daily use. When it happens: no Render, a managed host is acceptable, and the SQLite-vs-something-else decision gets revisited at that point, not before.

**Canvas ICS-to-task importer.** It's summer; Canvas deadlines aren't the pressure point right now. The ICS calendar link sync (events only, not tasks) already exists and is enough for the moment.

**Proactive nudges** (e.g. "you haven't touched this in 2 weeks," "3 things due tomorrow"). Explicitly out of the AI layer's scope — if built, this is deterministic logic in the execution layer, not an LLM inference. Deferred to a later phase entirely; not being built yet.

**Recurring tasks/habits.** Not modeled, not needed yet.

**Voice capture, email auto-ingest into inbox, phone widget.** Stretch goals. Don't build now, but don't make architectural choices that would make them hard to add later.

### Needs a Decision Before Building

**Calendar model reliability ceiling — decision made, engineering still needed.** `gemini-2.5-flash-lite` occasionally misreads correct calendar context (see AI Layer section above). Decision: stick with the current model and invest in engineering (better deterministic pre-processing, verification passes) rather than upgrading to a pricier model — revisit only if that effort provably can't close the gap.

### Dropped or Already Solved Differently

**MySQL + SQLAlchemy migration** — Not being built. SQLite + raw SQL is correct for current single-user, local-only load; this gets revisited at deployment time if it ever needs to, not preemptively.

**Extra project fields (`goal`, `risk_level`, `target_date`)** — Explicitly decided against. Projects stay minimal (title, description, status, progress) — the user wants to think of them as loose "continuous efforts," not structured planning objects.

**Separate recommendation API endpoints** — `/api/ai/recommendations` already handles this via Gemini. Chat can answer the same questions.

**Numeric scoring engine** — Dropped in favor of LLM reasoning. The AI prompt encodes the recommendation philosophy.

**Separate `subtasks` table** — Solved with `parent_task_id`. The tree view works off this.

**Separate `tags`, `task_tags`, `task_dependencies`, `task_notes` tables** — Solved with JSON columns.

**In-memory chat history** — Solved. Chat is now persisted to `chat_messages` table. (Note: this was confirmed to never have been a real problem for the user — kept because it's already built and cheap to maintain, not because it's a priority to invest further in.)

**Obsidian integration** — Decided against. Custom vault at `/data/vault/` replaces this.

**External calendar imports (Google Calendar)** — Solved, read-only. Google Calendar events are live-fetched/cached and surfaced both on the `/calendar` page and in AI chat. Write access stays off as a deliberate trust-ramp decision (see Open Questions) — not a permanent architectural limit.

---

## Open Questions (Unresolved)

1. **AI budget**: no hard number yet. User is open to spending more than the current $0.05/hour (rolling window) and would consider self-hosting if cost becomes the bottleneck — the AI spend is instrumental to reducing long-run dependency on the tool, not something to minimize for its own sake. Needs a concrete number once daily-use patterns are clearer.
2. **Connection-engine architecture**: how does it plug into the existing retrieve/inject pipeline without coupling to it? What's the interface boundary (separate service? separate ChromaDB collection? a graph layer alongside the vector store?) — undecided, needs a design pass before real implementation starts.
3. **Psych-field reasoning surfacing**: what does "AI shows its reasoning for an inferred field" actually look like in the UI — inline tooltip, expandable note, chat-style explanation? Not yet designed.
4. **Trust-ramp criteria**: what specifically has to be true before Google Calendar (or any other external system) moves from read-only to write-enabled? Not yet defined — currently just "prove reliability first," no concrete bar.

---
