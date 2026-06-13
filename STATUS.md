# Personal Execution OS — Status Report
_Last updated: 2026-06-12_

---

## What This Is

A single-user cognitive infrastructure system. The goal is one thing: the dashboard should instantly answer "what should I do right now?" Tasks are psychologically-aware objects with fear, ambiguity, energy type, and dependency fields. An AI assistant (Gemini) can read and mutate the task list through a chat interface. This is a private tool, not a SaaS product.

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

---

## What Is Done

### Authentication
- Google OAuth login (`/login` → `/auth/start` → `/authorize`)
- Email check against `OWNER_EMAIL` env var — anyone else is rejected
- UUID cookie + server-side `VALID_SESSIONS` dict (supports multiple devices)
- All routes are protected; unauthenticated requests redirect to login

### Database Schema
Two tables live in SQLite:

**`tasks`**
- Core fields: `id`, `title`, `description`, `status`, `priority`, `due_date`, `completed_at`
- Psychological fields: `fear_level` (1–5), `ambiguity_level` (1–5), `energy_type`, `estimated_effort`
- Relational fields: `project_id`, `parent_task_id` (subtasks via self-reference)
- Extra fields: `source_type`, `ai_generated`, `created_at`, `updated_at`
- JSON columns: `tags` (list of strings), `dependencies` (list of task IDs), `task_notes` (list of note path objects)

**`projects`**
- Fields: `id`, `title`, `description`, `status`, `progress`, `created_at`, `updated_at`

### Web UI (Jinja2 Templates)
- `/dashboard` — active tasks + active projects overview
- `/tasks` — full task list with filter/sort controls, tree view for subtasks, complete toggle
- `/tasks/<id>` — task detail: edit all fields, dependency viewer, parent task selector, subtask tree, breadcrumb
- `/projects` — project list
- `/projects/<id>` — project detail + tasks belonging to project
- `/chat` — AI chat interface with streaming-style message append, in-memory history
- `/login` — Google OAuth entry point

### REST API (`/api/*`, bearer token auth)
- `GET/POST /api/tasks` — list (filterable by status/priority/project_id) + create/update
- `GET/DELETE /api/tasks/<id>` — fetch + delete
- `GET/POST /api/projects` — list (filterable by status) + create/update
- `GET/DELETE /api/projects/<id>` — fetch + delete
- `GET /api/health` — liveness check

### AI Layer (`/services/ai/`)
- **Provider abstraction** (`provider.py`) — interface that any LLM backend implements
- **Gemini provider** (`gemini_provider.py`) — Gemini 2.5 Flash Lite with full tool-calling support
- **Groq provider** (`groq_provider.py`) — exists but not actively used
- **AIService** (`service.py`) — agentic loop (up to 5 tool-call rounds), task context injected on every call
- **Tools the AI can use**: `create_task`, `update_task`, `delete_task`, `create_project`, `delete_project`
- **Budget guard** (`budget.py`) — tracks per-session token spend, raises hard error at configurable limit (`AI_SESSION_BUDGET` env var, defaults to $0.05), logs every call to `costs.log`
- **Recommendations endpoint** (`GET /api/ai/recommendations`) — returns top 3 tasks + an insight, JSON
- **Web chat** (`/chat/message`, `/chat/history`) — session-scoped message history, AI reads full task context on every message

### Infrastructure
- Rotating error log (`errors.log`, 1MB × 3 backups)
- Rotating cost log (`costs.log`, 1MB × 3 backups)
- All secrets in `.env` (`FLASK_SECRET_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `OWNER_EMAIL`, `GEMINI_API_KEY`, `API_KEY_HASH`, `AI_SESSION_BUDGET`)

---

## What Is Not Done

### Definitely Still Needed

**1. RAG Pipeline (knowledge layer)**
The vault + embedding + retrieval system described in the roadmap. This is the highest-priority unbuilt feature. The implementation is being AI-generated as the first pass — all the design decisions are locked (ChromaDB, `text-embedding-004`, per-folder collections, ~500-token chunks, watchdog file watcher). Once working and tested, each component (chunker, embedder, vector store, retriever, injector) is a candidate for Ship of Theseus replacement.

**2. AI chat history persistence**
Chat history lives in a Python list (`CHAT_HISTORY` in `app.py`). It resets every time the server restarts. The AI loses all conversation context. This is the most annoying current limitation. A `chat_messages` table in SQLite would fix it.

**2. Inbox / fast-capture interface**
The original vision included an ultra-low-friction capture screen — type a task title, hit enter, done. Right now you have to fill out a form on `/tasks`. The capture friction is real and breaks the "adding a task takes seconds" requirement.

**3. Deployment**
The app only runs locally. No Render config, no Gunicorn setup, no production environment. This needs to be done before the app becomes a daily-use tool.

**4. Missing project fields**
The DB schema for `projects` is minimal. The roadmap specifies `goal`, `risk_level`, and `target_date` — none of these are in the DB or UI. Whether these matter depends on how you actually use the project view.

### Needs a Decision Before Building

**5. MySQL + SQLAlchemy migration**
The roadmap specifies MySQL + SQLAlchemy + Flask-Migrate. The current SQLite + raw SQL setup works fine for a single user with no concurrent writes. The question is whether the operational overhead of running MySQL locally and on Render is worth it. SQLite can handle this app's load indefinitely. This decision has downstream effects on deployment complexity.

**6. External calendar imports (Google Calendar, Canvas ICS)**
The original spec includes a full normalization pipeline for pulling in external deadlines. This is a real behavioral problem (scattered obligations) but also a non-trivial build. Worth doing if your deadlines live in Google Calendar or Canvas and you want them surfaced in the dashboard automatically. Skip it if you're fine manually entering them.

### Dropped or Already Solved Differently

**Separate recommendation API endpoints** (`/recommendations/now`, `/recommendations/deep-focus`, etc.)
The roadmap called for these, but `/api/ai/recommendations` already does this via Gemini. You can ask the AI in chat for deep-focus recommendations or low-energy tasks. Separate endpoints would be redundant.

**Numeric scoring engine** (urgency_score, resistance_score, momentum_score columns)
The roadmap described a formula-based scoring system. This was explicitly dropped in favor of letting the LLM do the reasoning. The AI prompt already encodes the recommendation philosophy. No code needed.

**Separate `subtasks` table**
Solved with `parent_task_id` on the tasks table. The tree view in the UI works off this. No separate table needed.

**Separate `tags`, `task_tags`, `task_dependencies`, `task_notes` tables**
All solved with JSON columns on tasks. Acceptable for single-user; a relational schema would only matter if you needed to query "all tasks with tag X" at the DB level, which the AI can handle anyway.

---

## Open Questions (Unresolved)

1. **SQLite or MySQL?** Single-user load makes SQLite fine forever. MySQL only makes sense if deployment to Render requires it or you want proper migrations tooling.
2. **Should chat history persist to the DB?** Almost certainly yes — the in-memory approach means the AI has no memory between sessions.
3. **External imports: yes or no?** If your deadlines are scattered across Google Calendar and Canvas, this is high-value. If you're fine entering them manually, skip it.
4. ~~**Obsidian integration: yes or no?**~~ **Decided: no.** A custom vault at `/data/vault/` with the defined folder structure replaces this. No dependency on Obsidian being installed or running.
5. **Project fields**: Is `goal`, `risk_level`, `target_date` worth adding to the project schema, or is the current minimal schema enough?
6. **Session budget**: $0.05 per server restart is very tight. What should the real limit be once this is in daily use?
