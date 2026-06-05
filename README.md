# Personal Second Brain OS

This is my answer to a problem I kept running into: I had tasks in Canvas, deadlines in my head, notes scattered across apps, and an AI assistant that forgot everything the moment I closed the tab. I wanted one place that knew everything about my work and could actually help me decide what to do next.

So I built it.

---

## Honest Context

Most of this codebase was written by Claude (Anthropic's AI coding assistant). I want to be upfront about that. What I can say is:

- I approved every line. Nothing went in that I didn't read, understand, and decide was right.
- I had a strong enough grasp of Flask, SQLite, OAuth, and REST API design to catch mistakes, ask the right follow-up questions, and make architectural decisions (the SQLite-over-MySQL call, the provider abstraction pattern, the psychological task schema).
- I used AI to move fast on a project I actually wanted to exist, not to avoid learning.

The one part I'm building by hand is the RAG pipeline — embeddings, vector search, chunk retrieval, context injection. I'm doing that myself because it's the most technically interesting piece and I want to actually understand it.

---

## What It Is

A private, self-hosted task and knowledge management system for one user. Three layers:

- **Execution layer** — task management with psychological fields. Every task has a fear level, ambiguity score, energy type, and dependency chain. The goal is that the dashboard answers "what should I work on right now?" in a way a normal task manager can't.
- **Intelligence layer** — Gemini 2.5 Flash runs an agentic loop with tool access. It can read and mutate tasks, reason over them, and have real conversations about what's in front of you.
- **Knowledge layer** *(in progress)* — a local vault of markdown notes indexed with embeddings. The AI retrieves relevant context on every query instead of getting a dumped list of everything.

---

## Stack

| Layer | Technology |
|---|---|
| Backend | Flask (Python) |
| Database | SQLite, raw SQL |
| Templates | Jinja2 server-rendered HTML |
| AI Provider | Gemini 2.5 Flash Lite via `google-genai` SDK |
| Auth | Google OAuth 2.0 (Authlib), single-owner email gate |

I chose SQLite intentionally — single user, no concurrent writes, no operational overhead. I considered SQLAlchemy and Flask-Migrate and decided against both for this use case.

---

## What's Built

### Authentication
- Google OAuth login with a hard email gate (`OWNER_EMAIL` env var)
- UUID cookie + server-side session dict for multi-device support
- All routes protected

### Task Management
- Psychological task schema: `fear_level`, `ambiguity_level`, `energy_type`, `estimated_effort`, `parent_task_id` (subtasks), `dependencies` and `tags` as JSON columns
- `/tasks` — filterable list, subtask tree view, inline status toggle
- `/tasks/<id>` — full detail and edit, dependency viewer

### Projects
- `/projects` and `/projects/<id>` with task linkage

### AI Layer
- Provider abstraction so the LLM backend is swappable (Gemini and Groq both implemented)
- Agentic loop up to 5 tool-call rounds per message
- Tools: `create_task`, `update_task`, `delete_task`, `create_project`, `delete_project`
- Budget guard: per-session token spend tracking with a hard limit, every call logged to `costs.log`
- `/chat` web interface
- `GET /api/ai/recommendations` — top 3 prioritized tasks + a short insight

### REST API
Bearer token auth, full CRUD on tasks and projects, health endpoint.

---

## Roadmap

> Design decisions in this section are still being finalized. Details marked `[TBD]` are intentionally left open.

### RAG Pipeline *(building this one myself)*

Index my markdown notes and have the AI retrieve relevant ones automatically on every query.

- Vault folder structure: `[TBD]`
- Vector store: `[TBD]`, persisted to disk
- Embeddings model: `[TBD]`
- File watcher that re-indexes on any vault change
- Passive retrieval: every chat message pulls top-k relevant chunks
- Active retrieval: explicit `search_vault` tool the AI can call
- AI-generated notes land in a separate folder, flagged as unreviewed, never treated as ground truth

### Chat History Persistence

Right now chat history lives in memory and resets on server restart. Fix: persist conversation history so the AI actually remembers prior sessions.

- Storage: `[TBD]`
- Retention: `[TBD]`

### Fast Capture Inbox

A single text field visible on every page. Type a task title, hit Enter, done. Everything else defaults to null and lands in a triage queue.

- Keyboard shortcut: `[TBD]`
- Triage view at `/inbox`
- AI-assisted triage: "process my inbox" suggests fear levels, energy types, project assignments

### External Calendar Import

Pull in deadlines from external sources and normalize them into tasks with deduplication.

- Source: `[TBD]` (Canvas ICS, Google Calendar, or both)
- Sync frequency: `[TBD]`
- Imported items land in inbox, never auto-activated

### Vault Browser

A web UI to browse notes, see which are AI-generated and unreviewed, and approve or edit them inline.

### Deployment

- Platform: `[TBD]`
- Persistence strategy for SQLite and vector store across deploys: `[TBD]`

---

## Running Locally

```bash
cp .env.example .env
# Fill in GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, OWNER_EMAIL, GEMINI_API_KEY

python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python app.py
```

Needs a Google Cloud Console app with `http://localhost:5000/authorize` as an authorized redirect URI. Make sure to the .env file with everything mentioned in the file.
