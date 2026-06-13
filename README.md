# Personal Second Brain OS

This is my answer to a problem I kept running into: I had tasks in Canvas, deadlines in my head, notes scattered across apps, and an AI assistant that forgot everything the moment I closed the tab. I wanted one place that knew everything about my work and could actually help me decide what to do next.

So I built it.

---

## Honest Context

Most of this codebase was written by Claude (Anthropic's AI coding assistant). I want to be upfront about that. What I can say is:

- I approved every line. Nothing went in that I didn't read, understand, and decide was right.
- I had a strong enough grasp of Flask, SQLite, OAuth, and REST API design to catch mistakes, ask the right follow-up questions, and make architectural decisions (the SQLite-over-MySQL call, the provider abstraction pattern, the psychological task schema).
- I used AI to move fast on a project I actually wanted to exist, not to avoid learning.

The RAG pipeline is also AI-implemented. See the section below on how it works and why — understanding the design is how I intend to engage with it, not by writing it cold from scratch.

The longer-term plan is the Ship of Theseus method: once the full system is working and tested, replace each component one at a time with a version I wrote myself. A replacement is only valid if the app retains full functionality and all tests pass. This way I learn each piece in context — against a working reference implementation, with a clear contract to satisfy — instead of building into a void. The RAG pipeline is the most technically interesting part and will be the primary target for this.

---

## What It Is

A private, self-hosted task and knowledge management system for one user. Three layers:

- **Execution layer** — task management with psychological fields. Every task has a fear level, ambiguity score, energy type, and dependency chain. The goal is that the dashboard answers "what should I work on right now?" in a way a normal task manager can't.
- **Intelligence layer** — Gemini 2.5 Flash runs an agentic loop with tool access. It can read and mutate tasks, reason over them, and have real conversations about what's in front of you.
- **Knowledge layer** — a local vault of markdown notes indexed with embeddings. The AI retrieves relevant context on every query instead of getting a dumped list of everything.

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

### RAG Pipeline

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

## The RAG Pipeline — How It Works

This section is a reference for me as the developer. It explains what RAG is, how it works conceptually, what data it can hold, and how this project uses it specifically — so I have enough understanding to eventually replace each component on my own terms.

---

### The Problem RAG Solves

An LLM has a limited context window and charges per token. If you have 500 notes totaling 200,000 tokens, you cannot paste them all into every prompt. You'd hit the context limit and pay a fortune even if you didn't.

The naive alternative — "just summarize everything" — loses detail. You need specific facts, not averages.

RAG solves this by making the AI retrieve only what's relevant to the current question, then answer against that. Instead of "here are all 500 notes, now answer," you get "here are the 5 notes most likely to be useful, now answer." For this project that's roughly a 40x cost reduction on knowledge queries, which is the whole reason it exists.

---

### Embeddings

An embedding is a fixed-length list of floating point numbers — a vector — that represents the *semantic meaning* of a piece of text. The model `text-embedding-004` takes any string and returns a vector of 768 numbers.

The key property: texts with similar meaning produce vectors that point in similar directions. "Fear of failure before an exam" and "test anxiety holding me back" will have nearly identical vectors. "The French Revolution" and "chicken tikka masala" will be far apart.

You measure similarity between two vectors using **cosine similarity** — the angle between them in 768-dimensional space. Score of 1.0 means identical direction (same meaning), 0 means unrelated, -1 means opposite. In practice, relevant matches come back around 0.7–0.9.

This is how semantic search works. You're not matching keywords — you're matching meaning.

---

### Chunking

You don't embed a whole note as one vector. A 3,000-word note covers multiple topics, so its embedding is a blurry average of all of them — not precise enough for retrieval.

Instead, you split each note into **chunks** of ~500 tokens, cut at heading and paragraph boundaries. Each chunk gets its own embedding. When retrieved, only the relevant chunk is injected into the prompt — not the whole file.

This matters for two reasons:
1. **Precision** — a chunk about one specific topic has a sharp, accurate embedding.
2. **Cost** — you inject 500 tokens of targeted context, not 3,000 tokens of tangentially related content.

Each chunk stores the original text alongside its vector so it can be read back when retrieved.

---

### The Indexing Phase

This happens once at startup, and then automatically whenever a vault file changes (via the `watchdog` file watcher):

1. Read a markdown note from `/data/vault/`
2. Parse its YAML frontmatter (type, tags, ai_generated, reviewed, etc.)
3. Split the body into chunks at heading/paragraph boundaries, ~500 tokens each
4. For each chunk: call `text-embedding-004` to get its 768-dimension vector
5. Write to ChromaDB: the vector, the chunk text, and metadata (source path, folder, frontmatter fields)

The result is a searchable index of meaning across every note in the vault.

---

### The Retrieval Phase

This happens on every AI query:

1. User sends a message: "What do I know about my linear algebra professor?"
2. Embed the message with the same model — get its vector
3. Ask ChromaDB: "Give me the 5 stored chunks whose vectors are closest to this one"
4. ChromaDB returns: 5 chunk texts + their metadata (source path, note type, tags, etc.)
5. Format those chunks as context and prepend them to the AI's prompt
6. The AI generates a response grounded in what was actually retrieved

The AI doesn't have to know anything in advance. It just receives relevant context inline.

---

### ChromaDB

ChromaDB is a local vector database. It runs in-process (no separate server) and persists to disk at `/data/chroma/`. It handles:

- Storing embedding vectors efficiently
- Approximate nearest-neighbor search (fast even at scale)
- **Collections** — separate namespaces per vault folder (classes, people, journal, etc.)
- **Metadata filtering** — you can say "search only in the `classes` collection" or "exclude chunks where `ai_generated=true`"

The collection-per-folder design means retrieval can be scoped. When the user asks about a project, you don't need to search journal entries. When asking about a class, you skip people notes. This sharpens results and cuts latency.

---

### What Data Can Be Stored

Anything that can be meaningfully described in text can be embedded and retrieved.

**Works well:**
- Markdown notes of any kind (the primary use case here)
- Class notes, lecture summaries, problem set reflections
- Journal entries and personal reflections
- Notes on people — what you know about them, your history with them
- Project planning documents and thinking logs
- Goals, values, long-term ambitions
- Reference material — articles and research saved as summaries
- AI-generated notes (flagged separately, never treated as ground truth)
- Self-model notes — how you think, your patterns, your preferences

**Works but requires serialization:**
- Structured data, if written out as natural language. A task summary like "Task: finish essay draft, due Friday, fear level 4, energy type deep_focus" embeds fine. Raw JSON does not.

**Doesn't work well:**
- Binary files (images, PDFs) without text extraction first
- Spreadsheets and numerical data — exact queries are better served by SQL
- Very short strings under ~20 tokens — not enough semantic content for a meaningful vector
- Live, rapidly-changing state (the task list is already handled by tool-calling, not RAG)
- Data where you need exact match, not semantic approximation (use SQL for that)

**The metadata layer is underrated.** ChromaDB chunks carry arbitrary key-value metadata alongside their vector. That means you can filter before searching: "top 5 relevant chunks, but only from notes tagged `calculus` that are not `ai_generated`." The frontmatter in each vault note maps directly to this metadata, which is why the frontmatter standard in this project is defined explicitly.

---

### This Project's Specific Design

| Decision | Choice | Why |
|---|---|---|
| Embeddings model | `text-embedding-004` | Already paying for Gemini; same API key, no extra cost |
| Vector store | ChromaDB (local, disk-persisted) | Zero ops overhead, no server, fits single-user |
| Collections | One per vault folder | Enables scoped retrieval without searching everything |
| Chunk size | ~500 tokens | Balance between precision and context completeness |
| Chunk splitting | Heading/paragraph boundaries | Preserves semantic units, avoids mid-sentence cuts |
| Top-k default | 5 chunks | Enough context without token bloat; adjustable to 10 |
| File watcher | `watchdog` library | Automatic re-indexing on vault changes without manual triggers |
| Retrieval modes | Passive (every query) + Active (`search_vault` tool) | Passive for ambient context; active for explicit lookups |
| AI-generated notes | `/data/vault/ai_generated/`, always flagged `reviewed: false` | Prevents model-generated content from being cited as ground truth |

---

### Component Seams for the Ship of Theseus

The RAG pipeline has five distinct, swappable components. When I replace them, the contract for each is: the app runs, the tests pass, answer quality stays the same or improves.

| Component | What it does | Seam |
|---|---|---|
| **Chunker** | Splits a note into text segments | Input: raw markdown string. Output: list of text strings. |
| **Embedder** | Turns a text string into a vector | Input: string. Output: list of floats. Swappable to local models (e.g. `nomic-embed-text` via Ollama). |
| **Vector store** | Stores and searches vectors | Interface: `upsert(id, vector, text, metadata)` and `query(vector, k, filters)`. ChromaDB today; could be SQLite + manual cosine similarity, Weaviate, or pgvector. |
| **Retriever** | Takes a query, returns top-k chunks | Input: query string + optional filters. Output: list of `{text, metadata, score}` objects. |
| **Injector** | Formats retrieved chunks into prompt context | Input: list of chunks. Output: formatted string prepended to the system or user prompt. |

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
