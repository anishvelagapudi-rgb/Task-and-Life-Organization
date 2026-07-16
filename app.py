from dotenv import load_dotenv
load_dotenv()

import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime, timezone
from flask import Flask, render_template, request, redirect, url_for, make_response
from db import get_db, init_db, reset_due_recurring_tasks

from classes.Task import Task
from classes.Project import Project
from api import api_bp
from ai_routes import ai_bp
from services.ai.gemini_provider import GeminiProvider
from services.ai.nvidia_provider import NvidiaProvider
from services.ai.service import AIService, strip_pending_delete_marker, has_pending_delete_marker
from services.ai.budget import BudgetExceededError
from authlib.integrations.flask_client import OAuth
import uuid
import os

app = Flask(__name__)
app.secret_key = os.environ['FLASK_SECRET_KEY']
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Wires up per-request DB connection cleanup. Must run at import time (not just
# under __main__) so it also applies when Vercel imports `app` directly as a WSGI
# callable — __main__ never executes there.
init_db(app)

# Rotate at 1 MB, keep 3 backups → errors.log, errors.log.1, errors.log.2
# Vercel's filesystem is read-only everywhere except /tmp — writing here at import
# time would crash the whole app on every request if left pointed at the repo root.
# /tmp is also always writable in local dev, so this needs no local-only branch.
_log_dir = "/tmp" if os.environ.get("VERCEL") else "."
_log_handler = RotatingFileHandler(os.path.join(_log_dir, "errors.log"), maxBytes=1_000_000, backupCount=3)
_log_handler.setLevel(logging.ERROR)
_log_handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(levelname)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))
logger = logging.getLogger("app")
logger.setLevel(logging.ERROR)
logger.addHandler(_log_handler)

# Register the API blueprint — wires in all /api/* routes from api.py
app.register_blueprint(api_bp)
# Register the AI blueprint — /api/ai/* routes
app.register_blueprint(ai_bp)

# OAUTH Library SETUP (authlib)
oauth = OAuth(app)

google = oauth.register(
    name='google',
    client_id=os.environ['GOOGLE_CLIENT_ID'],
    client_secret=os.environ['GOOGLE_CLIENT_SECRET'],
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile https://www.googleapis.com/auth/calendar.readonly'}
)

# Cookie holds a UUID. VALID_SESSIONS maps that UUID -> user info server-side.
# Supports multiple simultaneous sessions (different browsers/devices).
# Resets on server restart.
VALID_SESSIONS = {}
OWNER_EMAIL = os.environ['OWNER_EMAIL']

# Shared AI service instance — initialized once after .env is loaded. Gemini
# remains the sole tool-decider; NvidiaProvider only ever gets the tools-free
# reasoning/synthesis calls (see AIService.__init__'s docstring and README's
# "NVIDIA/Gemma-4-31B-IT as an alternate provider" section for why this split,
# not a straight swap, and what was tested before wiring it in here).
_ai_service = AIService(GeminiProvider(), reasoning_provider=NvidiaProvider())


def get_current_user():
    return VALID_SESSIONS.get(request.cookies.get('UserID'))


# ─── auth ─────────────────────────────────────────────────────────────────────

@app.route('/login')
def login():
    if get_current_user():
        return redirect(url_for('dashboard'))
    error = request.args.get('error')
    return render_template('login.html', error=error)


@app.route('/auth/start')
def auth_start():
    try:
        has_refresh = bool(get_db().execute(
            "SELECT 1 FROM tokens WHERE provider='google' AND refresh_token IS NOT NULL"
        ).fetchone())
    except Exception:
        has_refresh = False
    prompt = 'select_account' if has_refresh else 'consent'
    return google.authorize_redirect(url_for('authorize', _external=True), prompt=prompt, access_type='offline')


@app.route('/authorize')
def authorize():
    token = google.authorize_access_token()
    email = token['userinfo'].get('email')
    if email != OWNER_EMAIL:
        return redirect(url_for('login', error='This app is private. Sign in with the correct account.'))
    cookie = str(uuid.uuid4())
    VALID_SESSIONS[cookie] = {'email': email, 'name': token['userinfo'].get('name', '')}

    # Persist GCal OAuth token for calendar.readonly access
    try:
        db = get_db()
        expires_at = None
        if token.get('expires_at'):
            expires_at = datetime.fromtimestamp(float(token['expires_at']), tz=timezone.utc).isoformat()
        if token.get('refresh_token'):
            db.execute(
                """INSERT INTO tokens (provider, access_token, refresh_token, token_type, expires_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (provider) DO UPDATE SET
                       access_token = EXCLUDED.access_token,
                       refresh_token = EXCLUDED.refresh_token,
                       token_type = EXCLUDED.token_type,
                       expires_at = EXCLUDED.expires_at""",
                ('google', token['access_token'], token['refresh_token'], token.get('token_type', 'Bearer'), expires_at),
            )
        else:
            existing = db.execute("SELECT refresh_token FROM tokens WHERE provider='google'").fetchone()
            if existing:
                db.execute(
                    "UPDATE tokens SET access_token=?, expires_at=? WHERE provider='google'",
                    (token['access_token'], expires_at),
                )
            else:
                db.execute(
                    "INSERT INTO tokens (provider, access_token, refresh_token, token_type, expires_at) VALUES (?, ?, NULL, ?, ?)",
                    ('google', token['access_token'], token.get('token_type', 'Bearer'), expires_at),
                )
        db.commit()
    except Exception:
        logger.exception("Failed to store GCal token")

    res = make_response(redirect(url_for('dashboard')))
    res.set_cookie('UserID', cookie)
    return res


@app.route('/auth/reconnect-gcal')
def reconnect_gcal():
    """Force re-auth with calendar scope by clearing the stored GCal token."""
    if not get_current_user():
        return redirect(url_for('login'))
    try:
        get_db().execute("DELETE FROM tokens WHERE provider = 'google'")
        get_db().commit()
    except Exception:
        pass
    return redirect(url_for('auth_start'))


@app.route('/logout', methods=['POST'])
def logout():
    cookie = request.cookies.get('UserID')
    if cookie:
        VALID_SESSIONS.pop(cookie, None)
    res = make_response(redirect(url_for('login')))
    res.delete_cookie('UserID')
    return res


# ─── helpers ──────────────────────────────────────────────────────────────────

def parse_task(d):
    """Deserialize JSON columns on a task dict loaded from the DB."""
    for field in ("tags", "dependencies", "task_notes"):
        raw = d.get(field)
        d[field] = json.loads(raw) if raw else []
    return d


def all_projects():
    return [dict(r) for r in get_db().execute("SELECT * FROM projects ORDER BY title").fetchall()]


def tasks_by_project():
    """All tasks that belong to a project, grouped by project_id. Used to nest a
    project's tasks inside its collapsible row on /projects and /dashboard."""
    grouped = {}
    for t in get_db().execute(
        "SELECT * FROM tasks WHERE project_id IS NOT NULL ORDER BY created_at DESC"
    ).fetchall():
        grouped.setdefault(t["project_id"], []).append(dict(t))
    return grouped


# ─── index ────────────────────────────────────────────────────────────────────

@app.route("/favicon.ico")
def favicon():
    return "", 204

@app.route("/")
def index():
    return redirect(url_for("login"))


# ─── dashboard ────────────────────────────────────────────────────────────────

def _reset_recurring(db):
    """Reads the client's IANA timezone from the `tz` cookie (set sitewide by
    layout.html) and runs the lazy recurring-task reset. Called from every route that
    reads recurring-task state, so a stale 'done' status is never shown."""
    reset_due_recurring_tasks(db, request.cookies.get('tz'))


@app.route("/dashboard")
def dashboard():
    if not get_current_user():
        return redirect(url_for('login'))
    db = get_db()
    _reset_recurring(db)
    active_tasks = db.execute("""
        SELECT t.*, p.title as project_title
        FROM tasks t LEFT JOIN projects p ON t.project_id = p.id
        WHERE t.status IN ('inbox', 'active') AND t.recurring IS NULL
        ORDER BY t.priority DESC, t.due_date ASC
    """).fetchall()
    projects = [dict(r) for r in db.execute(
        "SELECT * FROM projects WHERE status = 'active' ORDER BY title"
    ).fetchall()]
    grouped = tasks_by_project()
    for p in projects:
        p["tasks"] = grouped.get(p["id"], [])
    return render_template("dashboard.html",
                           active_tasks=[dict(r) for r in active_tasks],
                           projects=projects)


# ─── tasks ────────────────────────────────────────────────────────────────────

@app.route("/tasks")
def get_tasks():
    if not get_current_user():
        return redirect(url_for('login'))
    db = get_db()
    _reset_recurring(db)
    rows = db.execute("""
        SELECT t.*, p.title as project_title
        FROM tasks t LEFT JOIN projects p ON t.project_id = p.id
        WHERE t.recurring IS NULL
        ORDER BY t.created_at DESC
    """).fetchall()
    tasks = [parse_task(dict(r)) for r in rows]

    # list view shows only "edge" tasks (no subtasks of their own) — tasks with
    # children are only browsable via the Tree view. Leaves that do have a parent
    # get a parent_title so the list view can still show what they belong to.
    task_by_id = {t["id"]: t for t in tasks}
    parent_ids = {t["parent_task_id"] for t in tasks if t.get("parent_task_id")}
    leaf_tasks = []
    for t in tasks:
        if t["id"] in parent_ids:
            continue
        parent = task_by_id.get(t.get("parent_task_id"))
        leaf_tasks.append({**t, "parent_title": parent["title"] if parent else None})

    return render_template("tasks.html",
                           tasks=tasks,
                           leaf_tasks=leaf_tasks,
                           projects=all_projects())


@app.route("/tasks/recurring")
def tasks_recurring():
    """JSON backing the Daily/Weekly recurring-tasks modal on the dashboard and tasks
    pages — recurring tasks are excluded from the normal task lists above, this is
    their only home in the UI."""
    if not get_current_user():
        return json.dumps({"error": "Unauthorized"}), 401, {"Content-Type": "application/json"}
    db = get_db()
    _reset_recurring(db)
    rows = db.execute("""
        SELECT t.id, t.title, t.status, t.recurring, t.priority, p.title as project_title
        FROM tasks t LEFT JOIN projects p ON t.project_id = p.id
        WHERE t.recurring IS NOT NULL
        ORDER BY t.title
    """).fetchall()
    tasks = [dict(r) for r in rows]
    return json.dumps({
        "daily": [t for t in tasks if t["recurring"] == "daily"],
        "weekly": [t for t in tasks if t["recurring"] == "weekly"],
    }), 200, {"Content-Type": "application/json"}


def _recurring_from_form(data) -> str | None:
    """Reads the 'recurring' form field, dropping anything that isn't daily/weekly."""
    recurring = (data.get("recurring") or "").strip() or None
    return recurring if recurring in ("daily", "weekly") else None


@app.route("/tasks", methods=["POST"])
def create_task():
    if not get_current_user():
        return redirect(url_for('login'))
    data = request.form
    recurring = _recurring_from_form(data)
    task = Task(
        title=data["title"],
        description=data.get("description") or None,
        status=data.get("status", "inbox"),
        priority=data.get("priority", "medium"),
        due_date=None if recurring else (data.get("due_date") or None),
        estimated_effort=data.get("estimated_effort") or None,
        energy_type=data.get("energy_type") or None,
        fear_level=data.get("fear_level") or None,
        ambiguity_level=data.get("ambiguity_level") or None,
        project_id=data.get("project_id") or None,
        recurring=recurring,
    )
    task.db_push(get_db())
    return redirect(url_for("get_tasks"))


@app.route("/tasks/<task_id>")
def get_task(task_id):
    if not get_current_user():
        return redirect(url_for('login'))
    db = get_db()
    _reset_recurring(db)

    # fetch the full task row (all columns) for the detail view
    row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return "Task not found", 404
    task = parse_task(dict(row))

    # if this task was synced in from an ICS feed, look up the source calendar's name
    # so the UI can warn that title/description get overwritten on the next sync
    source_calendar_name = None
    if task.get("source_type") == "ics_import" and task.get("source_calendar_id"):
        cal_row = db.execute("SELECT name FROM calendars WHERE id = ?", (task["source_calendar_id"],)).fetchone()
        source_calendar_name = cal_row["name"] if cal_row else None

    # fetch a lightweight version of every task (only what the client needs to
    # render the tree). the browser uses parent_task_id to figure out
    # relationships, so we never build a tree structure on the server.
    all_tasks = [dict(r) for r in db.execute(
        "SELECT id, title, status, priority, parent_task_id FROM tasks"
    ).fetchall()]

    # id → task dict so we can do O(1) lookups by id on the server too
    task_map = {t['id']: t for t in all_tasks}

    # walk up the parent_task_id chain until we hit a task with no parent.
    # root_id is used by the client to know where to start the "full tree" view.
    root_id = task_id
    cur = task
    while cur.get('parent_task_id'):
        parent = task_map.get(cur['parent_task_id'])
        if not parent:
            break
        cur = parent
        root_id = cur['id']

    # look up the immediate parent for the breadcrumb shown at the top of the page
    parent_task = task_map.get(task['parent_task_id']) if task.get('parent_task_id') else None

    # resolve dependency IDs → task dicts so the template can render titles + links
    dep_tasks = [task_map[d] for d in task['dependencies'] if d in task_map]

    # all tasks except the current one — used to populate the "parent task" dropdown
    other_tasks = [t for t in all_tasks if t['id'] != task_id]

    return render_template("task_detail.html",
                           task=task,
                           source_calendar_name=source_calendar_name,
                           all_tasks=all_tasks,  # sent to JS for tree rendering, via |tojson (escapes for safe <script> embedding)
                           root_id=root_id,
                           parent_task=parent_task,
                           dep_tasks=dep_tasks,
                           other_tasks=other_tasks,
                           projects=all_projects())


@app.route("/tasks/<task_id>", methods=["POST"])
def update_task(task_id):
    if not get_current_user():
        return redirect(url_for('login'))
    db = get_db()

    row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return "Task not found", 404

    # load existing task so we can preserve fields that aren't in the edit form.
    # dependencies and task_notes have no form UI yet, so we carry them forward
    # unchanged rather than wiping them on every save.
    existing = parse_task(dict(row))
    data = request.form

    # tags come in as a comma-separated string from the form (e.g. "coding, research").
    # if the field is empty we keep whatever tags were already saved.
    tags_raw = data.get("tags", "").strip()
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else existing['tags']

    # psych_reasoning explains *why* the AI set the 4 psych fields to their current
    # values. If the user manually changes any of those fields on this form, the old
    # reasoning no longer describes the new value — carrying it forward unchanged
    # would misattribute a manual edit to the AI, so clear it whenever any of the 4
    # fields actually changed. Only carried forward untouched when none of them did.
    def _psych_field_changed(existing_val, form_val_raw):
        new_val = form_val_raw or None
        if existing_val is None and new_val is None:
            return False
        return str(existing_val) != str(new_val)

    psych_changed = any(
        _psych_field_changed(existing.get(f), data.get(f))
        for f in ("fear_level", "ambiguity_level", "energy_type", "estimated_effort")
    )
    psych_reasoning = None if psych_changed else existing.get('psych_reasoning')

    # The edit form's Status dropdown can move a task into/out of 'done' too, not just
    # the dedicated Complete/Revert button — keep completed_at consistent with whichever
    # one last changed it instead of always preserving (stale timestamp on a reopened
    # task) or always wiping (breaks recurring-task reset, which needs completed_at to
    # know when a recurring task was last finished).
    new_status = data.get("status", "inbox")
    if new_status == 'done' and existing['status'] != 'done':
        completed_at = datetime.now(timezone.utc)
    elif new_status != 'done':
        completed_at = None
    else:
        completed_at = existing['completed_at']

    recurring = _recurring_from_form(data)

    task = Task(
        id=task_id,
        title=data["title"],
        description=data.get("description") or None,
        status=data.get("status", "inbox"),
        priority=data.get("priority", "medium"),
        due_date=None if recurring else (data.get("due_date") or None),
        estimated_effort=data.get("estimated_effort") or None,
        energy_type=data.get("energy_type") or None,
        fear_level=data.get("fear_level") or None,
        ambiguity_level=data.get("ambiguity_level") or None,
        psych_reasoning=psych_reasoning,  # cleared if the user just changed one of the 4 fields it explains
        project_id=data.get("project_id") or None,
        parent_task_id=data.get("parent_task_id") or None,  # None = root-level task
        tags=tags,
        dependencies=existing['dependencies'],   # carried forward unchanged
        task_notes=existing['task_notes'],        # carried forward unchanged
        recurring=recurring,
        completed_at=completed_at,
    )
    task.db_push(db)

    # redirect back to the same task page, not the list
    return redirect(url_for('get_task', task_id=task_id))


@app.route("/tasks/<task_id>/complete", methods=["POST"])
def complete_task(task_id):
    if not get_current_user():
        return redirect(url_for('login'))
    db = get_db()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    row = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return "Not found", 404

    # toggle: if already done, revert to inbox; otherwise mark done
    if row['status'] == 'done':
        new_status = 'inbox'
        db.execute(
            "UPDATE tasks SET status='inbox', completed_at=NULL, updated_at=? WHERE id=?",
            (now, task_id)
        )
    else:
        new_status = 'done'
        db.execute(
            "UPDATE tasks SET status='done', completed_at=?, updated_at=? WHERE id=?",
            (now, now, task_id)
        )

    db.commit()

    # fetch calls get JSON back so JS knows which direction the toggle went
    if request.headers.get('X-Requested-With') == 'fetch':
        return json.dumps({'status': new_status}), 200, {'Content-Type': 'application/json'}
    return redirect(request.referrer or url_for('get_tasks'))


@app.route("/capture", methods=["POST"])
def capture():
    """Sitewide fast-capture: single title, saved immediately as an inbox task.
    Equally first-class as chat-based capture — reachable from every page via the
    nav bar in layout.html, not tied to /tasks."""
    if not get_current_user():
        return json.dumps({"error": "Unauthorized"}), 401, {"Content-Type": "application/json"}
    data = request.get_json(force=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return json.dumps({"error": "Title is required"}), 400, {"Content-Type": "application/json"}
    task = Task(title=title, status="inbox")
    task.db_push(get_db())
    return json.dumps({"ok": True, "id": task.id, "title": task.title}), 200, {"Content-Type": "application/json"}


@app.route("/tasks/<task_id>/delete", methods=["POST"])
def delete_task(task_id):
    if not get_current_user():
        return redirect(url_for('login'))
    db = get_db()
    db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    db.commit()
    return redirect(url_for("get_tasks"))


# ─── projects ─────────────────────────────────────────────────────────────────

@app.route("/projects")
def get_projects():
    if not get_current_user():
        return redirect(url_for('login'))
    projects = all_projects()
    grouped = tasks_by_project()
    for p in projects:
        p["tasks"] = grouped.get(p["id"], [])
    return render_template("projects.html", projects=projects)


@app.route("/projects", methods=["POST"])
def create_project():
    if not get_current_user():
        return redirect(url_for('login'))
    data = request.form
    project = Project(
        title=data["title"],
        description=data.get("description") or None,
        status=data.get("status", "active"),
        progress=int(data.get("progress") or 0),
    )
    project.db_push(get_db())
    return redirect(url_for("get_projects"))


@app.route("/projects/<project_id>")
def get_project(project_id):
    if not get_current_user():
        return redirect(url_for('login'))
    db = get_db()
    row = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        return "Project not found", 404
    tasks = db.execute("SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at DESC", (project_id,)).fetchall()
    return render_template("project_detail.html",
                           project=dict(row),
                           tasks=[dict(t) for t in tasks])


@app.route("/projects/<project_id>", methods=["POST"])
def update_project(project_id):
    if not get_current_user():
        return redirect(url_for('login'))
    db = get_db()
    row = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        return "Project not found", 404
    data = request.form
    # progress has no edit-form UI anymore (progress bars were removed from the
    # frontend) — preserve whatever value the project already had instead of
    # defaulting to 0, since it's still a valid field for external API callers.
    project = Project(
        id=project_id,
        title=data["title"],
        description=data.get("description") or None,
        status=data.get("status", "active"),
        progress=dict(row)["progress"],
    )
    project.db_push(db)
    return redirect("/projects")


@app.route("/projects/<project_id>/complete", methods=["POST"])
def complete_project(project_id):
    if not get_current_user():
        return redirect(url_for('login'))
    db = get_db()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    row = db.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        return "Not found", 404

    # toggle: if already completed, revert to active; otherwise mark completed
    new_status = 'active' if row['status'] == 'completed' else 'completed'
    db.execute(
        "UPDATE projects SET status=?, updated_at=? WHERE id=?",
        (new_status, now, project_id)
    )
    db.commit()

    if request.headers.get('X-Requested-With') == 'fetch':
        return json.dumps({'status': new_status}), 200, {'Content-Type': 'application/json'}
    return redirect(request.referrer or url_for('get_projects'))


# ─── chat ─────────────────────────────────────────────────────────────────────

def _chat_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


@app.route("/chat")
def chat_list():
    if not get_current_user():
        return redirect(url_for('login'))
    chats = [dict(r) for r in get_db().execute(
        "SELECT * FROM chats ORDER BY updated_at DESC"
    ).fetchall()]
    return render_template("chats.html", chats=chats)


@app.route("/chat/new", methods=["POST"])
def chat_new():
    if not get_current_user():
        return redirect(url_for('login'))
    chat_id = str(uuid.uuid4())
    now = _chat_now()
    db = get_db()
    db.execute(
        "INSERT INTO chats (id, title, indexed, created_at, updated_at) VALUES (?, ?, 0, ?, ?)",
        (chat_id, "New Chat", now, now),
    )
    db.commit()
    return redirect(url_for('chat_view', chat_id=chat_id))


@app.route("/chat/<chat_id>")
def chat_view(chat_id):
    if not get_current_user():
        return redirect(url_for('login'))
    db = get_db()
    row = db.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
    if row is None:
        return "Chat not found", 404
    from services.calendar.gcal_service import refresh_upcoming_cache
    try:
        refresh_upcoming_cache(db)
    except Exception:
        logger.exception("GCal upcoming cache refresh failed on chat view")
    messages = [dict(m) for m in db.execute(
        "SELECT role, content, sources FROM chat_messages WHERE chat_id = ? ORDER BY created_at",
        (chat_id,),
    ).fetchall()]
    for m in messages:
        m["sources"] = json.loads(m["sources"]) if m.get("sources") else []
        if m["role"] == "assistant":
            m["content"] = strip_pending_delete_marker(m["content"])
    return render_template("chat.html", chat=dict(row), messages=messages)


@app.route("/chat/<chat_id>/message", methods=["POST"])
def chat_message(chat_id):
    if not get_current_user():
        return json.dumps({"error": "Unauthorized"}), 401, {"Content-Type": "application/json"}
    db = get_db()
    row = db.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
    if row is None:
        return json.dumps({"error": "Chat not found"}), 404, {"Content-Type": "application/json"}

    data = request.get_json(force=True) or {}
    if data.get("content") is not None and not isinstance(data.get("content"), str):
        logger.warning(
            "DEBUG_BADBODY content=%r headers=%r raw_body=%r",
            data.get("content"), dict(request.headers), request.get_data(as_text=True)[:2000],
        )
        return json.dumps({"error": "content must be a string"}), 400, {"Content-Type": "application/json"}
    content = (data.get("content") or "").strip()
    client_tz = data.get("timezone")
    if not content:
        return json.dumps({"error": "content is required"}), 400, {"Content-Type": "application/json"}

    history = [dict(m) for m in db.execute(
        "SELECT role, content FROM chat_messages WHERE chat_id = ? ORDER BY created_at",
        (chat_id,),
    ).fetchall()]

    try:
        reply, sources = _ai_service.chat(db, history + [{"role": "user", "content": content}], client_tz=client_tz)
    except BudgetExceededError as e:
        logger.error("BUDGET EXCEEDED — AI disabled until server restart: %s", e)
        reply, sources = str(e), []
    except Exception as e:
        logger.exception("chat_message failed | user_input=%r", content)
        reply, sources = _ai_service.explain_error(db, content, str(e)), []

    now = _chat_now()
    db.execute(
        "INSERT INTO chat_messages (id, chat_id, role, content, created_at) VALUES (?, ?, 'user', ?, ?)",
        (str(uuid.uuid4()), chat_id, content, now),
    )
    db.execute(
        "INSERT INTO chat_messages (id, chat_id, role, content, created_at, sources) VALUES (?, ?, 'assistant', ?, ?, ?)",
        (str(uuid.uuid4()), chat_id, reply, now, json.dumps(sources) if sources else None),
    )

    # Auto-title from the first user message
    if not history:
        title = content[:60] + ("..." if len(content) > 60 else "")
        db.execute("UPDATE chats SET title = ?, updated_at = ? WHERE id = ?", (title, now, chat_id))
    else:
        db.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (now, chat_id))

    db.commit()

    # Re-index if this chat is saved to the vault
    if dict(row)["indexed"]:
        try:
            from services.rag.chat_indexer import index_chat
            title_row = db.execute("SELECT title FROM chats WHERE id = ?", (chat_id,)).fetchone()
            all_msgs = history + [
                {"role": "user", "content": content},
                {"role": "assistant", "content": strip_pending_delete_marker(reply)},
            ]
            index_chat(chat_id, title_row["title"], all_msgs)
        except Exception:
            logger.exception("Failed to re-index chat %s", chat_id)

    # `reply` (stored above, raw) intentionally keeps any [ref: ...] marker so the
    # confirmation round trip survives in chat_messages/history — only what's actually
    # shown to the user here gets it stripped. `pending_delete` lets the frontend show
    # a real confirm dialog for this turn instead of a plain chat bubble, without ever
    # exposing the marker itself to the browser.
    return json.dumps({
        "reply": strip_pending_delete_marker(reply),
        "sources": sources,
        "pending_delete": has_pending_delete_marker(reply),
    }), 200, {"Content-Type": "application/json"}


@app.route("/chat/<chat_id>/save", methods=["POST"])
def chat_save(chat_id):
    if not get_current_user():
        return json.dumps({"error": "Unauthorized"}), 401, {"Content-Type": "application/json"}
    db = get_db()
    row = db.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
    if row is None:
        return json.dumps({"error": "Chat not found"}), 404, {"Content-Type": "application/json"}

    chat = dict(row)
    new_indexed = 0 if chat["indexed"] else 1
    db.execute(
        "UPDATE chats SET indexed = ?, updated_at = ? WHERE id = ?",
        (new_indexed, _chat_now(), chat_id),
    )
    db.commit()

    try:
        if new_indexed:
            from services.rag.chat_indexer import index_chat
            messages = [dict(m) for m in db.execute(
                "SELECT role, content FROM chat_messages WHERE chat_id = ? ORDER BY created_at",
                (chat_id,),
            ).fetchall()]
            index_chat(chat_id, chat["title"], messages)
        else:
            from services.rag.chat_indexer import deindex_chat
            deindex_chat(chat_id)
    except Exception:
        logger.exception("Failed to update RAG index for chat %s", chat_id)

    return json.dumps({"indexed": bool(new_indexed)}), 200, {"Content-Type": "application/json"}


@app.route("/chat/<chat_id>/delete", methods=["POST"])
def chat_delete(chat_id):
    if not get_current_user():
        return redirect(url_for('login'))
    db = get_db()
    row = db.execute("SELECT indexed FROM chats WHERE id = ?", (chat_id,)).fetchone()
    if row and row["indexed"]:
        try:
            from services.rag.chat_indexer import deindex_chat
            deindex_chat(chat_id)
        except Exception:
            logger.exception("Failed to deindex chat %s before deletion", chat_id)
    db.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
    db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
    db.commit()
    return redirect(url_for('chat_list'))


@app.route("/projects/<project_id>/delete", methods=["POST"])
def delete_project(project_id):
    if not get_current_user():
        return redirect(url_for('login'))
    db = get_db()
    db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    db.commit()
    return redirect(url_for("get_projects"))


@app.errorhandler(Exception)
def handle_exception(e):
    logger.exception("Unhandled exception | %s %s", request.method, request.path)
    # Re-raise so Flask's normal error handling (debug page / 500) still works
    raise e


# ─── vault ────────────────────────────────────────────────────────────────────

def _is_safe_vault_key(key: str) -> bool:
    """Storage keys have no real filesystem to traversal-guard against, but a
    `..` segment (however it got there) should never be treated as part of a
    valid vault path."""
    return ".." not in key.split("/")


def _vault_tree() -> dict:
    """Build {folder_name: [file_dicts]} from the vault Storage bucket."""
    from services.vault import storage
    tree = {}
    folders = sorted(storage.list_top_level_folders(), key=lambda n: (n != "inbox", n))
    for folder in folders:
        files = []
        for f in sorted(storage.list_files(folder), key=lambda e: e["name"]):
            updated_at = datetime.fromisoformat(f["updated_at"].replace("Z", "+00:00"))
            files.append({
                "name": f["name"],
                "path": f"{folder}/{f['name']}",
                "ext": Path(f["name"]).suffix.lower().lstrip(".") or "file",
                "mtime": updated_at.timestamp(),
                "mtime_str": updated_at.strftime("%Y-%m-%d"),
                "size": f["size"],
            })
        tree[folder] = files
    return tree


@app.route("/vault")
def vault_browser():
    if not get_current_user():
        return redirect(url_for("login"))
    tree = _vault_tree()
    total = sum(len(files) for files in tree.values())
    return render_template("vault.html", tree=tree, total=total)


@app.route("/vault/upload", methods=["POST"])
def vault_upload():
    if not get_current_user():
        xhr = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        if xhr:
            return json.dumps({"error": "Unauthorized"}), 401, {"Content-Type": "application/json"}
        return redirect(url_for("login"))

    folder = request.form.get("folder", "inbox").strip().lower()
    # Sanitize: only allow simple folder names, no path traversal
    folder = "".join(c for c in folder if c.isalnum() or c in "-_")
    if not folder:
        folder = "inbox"

    uploaded_files = request.files.getlist("file")
    saved, errors = [], []

    for f in uploaded_files:
        if not f or not f.filename:
            continue
        try:
            from services.vault.processor import save_upload
            key = save_upload(f, folder)
            saved.append(Path(key).name)
            try:
                from services.rag.indexer import index_file
                index_file(key)
            except Exception:
                logger.exception("Failed to index uploaded file %s", key)
        except ValueError as e:
            errors.append(str(e))
        except Exception as e:
            logger.exception("vault_upload failed for %s", f.filename)
            errors.append(f"Failed to process '{f.filename}': {e}")

    xhr = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if xhr:
        if errors and not saved:
            return json.dumps({"error": errors[0]}), 400, {"Content-Type": "application/json"}
        return json.dumps({"saved": saved, "errors": errors}), 200, {"Content-Type": "application/json"}

    return redirect(url_for("vault_browser"))


@app.route("/vault/fetch-url", methods=["POST"])
def vault_fetch_url():
    if not get_current_user():
        return json.dumps({"error": "Unauthorized"}), 401, {"Content-Type": "application/json"}

    url = request.form.get("url", "").strip()
    folder = request.form.get("folder", "reference").strip().lower()
    folder = "".join(c for c in folder if c.isalnum() or c in "-_") or "reference"

    if not url:
        return json.dumps({"error": "URL is required"}), 400, {"Content-Type": "application/json"}

    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return json.dumps({"error": "Only http/https URLs are supported"}), 400, {"Content-Type": "application/json"}

    try:
        import requests as http_req
        import re as _re

        resp = http_req.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
        }, stream=True)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "")
        if "html" not in content_type:
            return json.dumps({"error": f"URL did not return HTML (got: {content_type})"}), 400, {"Content-Type": "application/json"}

        # Read with a 5 MB cap
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536, decode_unicode=True):
            total += len(chunk)
            if total > 5 * 1024 * 1024:
                return json.dumps({"error": "Page too large (> 5 MB)"}), 400, {"Content-Type": "application/json"}
            chunks.append(chunk)
        html = "".join(chunks)

        # Build filename from hostname + path
        raw_slug = f"{parsed.netloc}-{parsed.path}".strip("/").replace("/", "-")
        slug = _re.sub(r"[^\w\-]", "-", raw_slug)
        slug = _re.sub(r"-+", "-", slug).strip("-")[:80] or "page"
        filename = f"{slug}.html"

        from services.vault import storage
        key = f"{folder}/{filename}"
        if storage.exists(key):
            ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            filename = f"{slug}-{ts}.html"
            key = f"{folder}/{filename}"

        # Note: writes raw HTML directly, bypassing processor.py's HTML->markdown
        # conversion — a pre-existing inconsistency, preserved as-is here.
        storage.upload(key, html.encode("utf-8"), content_type="text/html")

        try:
            from services.rag.indexer import index_file
            index_file(key)
        except Exception:
            logger.exception("Failed to index fetched URL %s", key)

        return json.dumps({"saved": filename, "folder": folder}), 200, {"Content-Type": "application/json"}

    except Exception as e:
        logger.exception("vault_fetch_url failed for %s", url)
        return json.dumps({"error": str(e)}), 500, {"Content-Type": "application/json"}


@app.route("/vault/folder", methods=["POST"])
def vault_new_folder():
    if not get_current_user():
        return redirect(url_for("login"))
    name = request.form.get("name", "").strip().lower()
    name = "".join(c for c in name if c.isalnum() or c in "-_")
    if name:
        # Storage has no real "empty folder" concept — a zero-byte placeholder
        # makes the folder show up immediately, filtered out of file listings
        # the same way dotfiles were filtered on the old local-disk browser.
        from services.vault import storage
        storage.upload(f"{name}/.keep", b"", content_type="application/octet-stream")
    return redirect(url_for("vault_browser"))


@app.route("/vault/file/<path:filepath>/move", methods=["POST"])
def vault_move_file(filepath):
    if not get_current_user():
        return json.dumps({"error": "Unauthorized"}), 401, {"Content-Type": "application/json"}
    dest_folder = request.form.get("folder", "").strip().lower()
    dest_folder = "".join(c for c in dest_folder if c.isalnum() or c in "-_")
    if not dest_folder:
        return json.dumps({"error": "Destination folder is required"}), 400, {"Content-Type": "application/json"}

    from services.vault import storage
    if not _is_safe_vault_key(filepath) or not storage.exists(filepath):
        return json.dumps({"error": "File not found"}), 404, {"Content-Type": "application/json"}

    filename = filepath.rsplit("/", 1)[-1]
    dest_key = f"{dest_folder}/{filename}"
    if dest_key == filepath:
        return json.dumps({"error": "File is already in that folder"}), 400, {"Content-Type": "application/json"}

    try:
        from services.rag.indexer import delete_file, index_file
        delete_file(filepath)
        storage.move(filepath, dest_key)
        index_file(dest_key)
        new_path = dest_key
        try:
            from services.connections.engine import delete_connections_for
            delete_connections_for(get_db(), filepath)
        except Exception:
            logger.exception("Failed to clean up note_connections for moved file %s", filepath)
        return json.dumps({"ok": True, "path": new_path}), 200, {"Content-Type": "application/json"}
    except Exception as e:
        logger.exception("vault_move_file failed for %s", filepath)
        return json.dumps({"error": str(e)}), 500, {"Content-Type": "application/json"}


@app.route("/vault/file/<path:filepath>/delete", methods=["POST"])
def vault_delete_file(filepath):
    if not get_current_user():
        return json.dumps({"error": "Unauthorized"}), 401, {"Content-Type": "application/json"}
    from services.vault import storage
    if not _is_safe_vault_key(filepath):
        return json.dumps({"error": "Invalid path"}), 400, {"Content-Type": "application/json"}
    if not storage.exists(filepath):
        return json.dumps({"error": "File not found"}), 404, {"Content-Type": "application/json"}
    try:
        from services.rag.indexer import delete_file
        delete_file(filepath)
        storage.delete(filepath)
        try:
            from services.connections.engine import delete_connections_for
            delete_connections_for(get_db(), filepath)
        except Exception:
            logger.exception("Failed to clean up note_connections for deleted file %s", filepath)
        return json.dumps({"ok": True}), 200, {"Content-Type": "application/json"}
    except Exception as e:
        logger.exception("vault_delete_file failed for %s", filepath)
        return json.dumps({"error": str(e)}), 500, {"Content-Type": "application/json"}


@app.route("/vault/folder/<name>/delete", methods=["POST"])
def vault_delete_folder(name):
    if not get_current_user():
        return redirect(url_for("login"))
    name = "".join(c for c in name if c.isalnum() or c in "-_")
    if name:
        from services.vault import storage
        storage.delete_prefix(f"{name}/")
        try:
            from services.rag.store import delete_collection
            delete_collection(name)
        except Exception:
            pass
    return redirect(url_for("vault_browser"))


@app.route("/vault/file/<path:filepath>")
def vault_file_view(filepath):
    if not get_current_user():
        return redirect(url_for("login"))
    from services.vault import storage
    if not _is_safe_vault_key(filepath):
        return "Not found", 404
    try:
        content = storage.download(filepath).decode("utf-8", errors="replace")
    except FileNotFoundError:
        return "Not found", 404
    if request.args.get("raw"):
        return content, 200, {"Content-Type": "text/plain; charset=utf-8"}

    # Non-obvious cross-folder connections (services/connections/, parallel to the
    # RAG pipeline) — best-effort, never breaks the file viewer if it fails.
    connections = []
    try:
        from services.connections.engine import discover_connections
        connections = discover_connections(filepath, k=5, db=get_db())
    except Exception:
        logger.exception("Connection discovery failed for %s", filepath)

    return render_template("vault_file.html", filepath=filepath, content=content, connections=connections)


# ─── calendar ─────────────────────────────────────────────────────────────────

def _cal_auth():
    user = get_current_user()
    if not user:
        return None, redirect(url_for('login'))
    return user, None


def _json(data, status=200):
    return json.dumps(data), status, {"Content-Type": "application/json"}


@app.route("/calendar")
def calendar_view():
    _, redir = _cal_auth()
    if redir:
        return redir
    db = get_db()
    from services.calendar.gcal_service import is_connected, refresh_upcoming_cache
    try:
        refresh_upcoming_cache(db)
    except Exception:
        logger.exception("GCal upcoming cache refresh failed on calendar view")
    local_cals = [dict(r) for r in db.execute("SELECT * FROM calendars ORDER BY name").fetchall()]
    return render_template("calendar.html",
                           local_cals=local_cals,
                           local_cal_ids=[c['id'] for c in local_cals],
                           gcal_connected=is_connected(db))


@app.route("/calendar/api/events")
def calendar_api_events():
    _, redir = _cal_auth()
    if redir:
        return _json({"error": "Unauthorized"}, 401)
    db = get_db()
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    if not start or not end:
        return _json([])
    show_tasks = request.args.get("show_tasks", "1") == "1"
    local_cal_ids = [x for x in request.args.get("local_cal_ids", "").split(",") if x]
    gcal_ids = [x for x in request.args.get("gcal_ids", "").split(",") if x]

    events = []

    # Local events
    if local_cal_ids:
        ph = ",".join("?" * len(local_cal_ids))
        rows = db.execute(f"""
            SELECT e.*, c.color, c.name as cal_name
            FROM events e JOIN calendars c ON e.calendar_id = c.id
            WHERE e.calendar_id IN ({ph})
            AND e.start_datetime < ?
            AND (e.end_datetime > ? OR (e.end_datetime IS NULL AND e.start_datetime >= ?))
        """, local_cal_ids + [end, start, start]).fetchall()
        for r in rows:
            e = dict(r)
            events.append({
                "id": e["id"],
                "title": e["title"],
                "start": e["start_datetime"],
                "end": e["end_datetime"],
                "allDay": bool(e["all_day"]),
                "backgroundColor": e["color"],
                "borderColor": e["color"],
                "editable": True,
                "extendedProps": {
                    "source": "local",
                    "calendar_id": e["calendar_id"],
                    "cal_name": e["cal_name"],
                    "description": e["description"],
                    "location": e["location"],
                },
            })

    # Task deadlines as all-day events
    if show_tasks:
        task_rows = db.execute("""
            SELECT id, title, due_date, priority FROM tasks
            WHERE due_date IS NOT NULL AND status != 'done'
            AND due_date >= ? AND due_date < ?
        """, (start[:10], end[:10])).fetchall()
        for t in task_rows:
            td = dict(t)
            events.append({
                "id": f"task_{td['id']}",
                "title": td["title"],
                "start": td["due_date"],
                "allDay": True,
                "display": "list-item",
                "backgroundColor": "#ffb84a",
                "borderColor": "#ffb84a",
                "textColor": "#ffb84a",
                "editable": False,
                "classNames": ["fc-task-deadline"],
                "extendedProps": {"source": "task", "task_id": td["id"], "priority": td["priority"]},
            })

    # GCal events (live, read-only)
    if gcal_ids:
        from services.calendar.gcal_service import list_calendars as _gcal_cals, list_events as _gcal_events
        color_map = {}
        try:
            for c in _gcal_cals(db):
                color_map[c["id"]] = c.get("backgroundColor", "#4a9eff")
        except Exception:
            pass
        for cal_id in gcal_ids:
            try:
                for e in _gcal_events(db, cal_id, start, end):
                    s = e.get("start", {})
                    en = e.get("end", {})
                    all_day = "date" in s and "dateTime" not in s
                    color = color_map.get(cal_id, "#4a9eff")
                    events.append({
                        "id": f"gcal_{e['id']}",
                        "title": e.get("summary", "(no title)"),
                        "start": s.get("dateTime") or s.get("date", ""),
                        "end": en.get("dateTime") or en.get("date"),
                        "allDay": all_day,
                        "backgroundColor": color,
                        "borderColor": color,
                        "editable": False,
                        "extendedProps": {
                            "source": "gcal",
                            "description": e.get("description"),
                            "location": e.get("location"),
                            "gcal_link": e.get("htmlLink"),
                        },
                    })
            except Exception:
                logger.exception("GCal event fetch failed for %s", cal_id)

    return _json(events)


@app.route("/calendar/api/gcal-calendars")
def calendar_api_gcal_cals():
    _, redir = _cal_auth()
    if redir:
        return _json({"error": "Unauthorized"}, 401)
    from services.calendar.gcal_service import list_calendars
    return _json(list_calendars(get_db()))


@app.route("/calendar/api/events", methods=["POST"])
def calendar_api_create_event():
    _, redir = _cal_auth()
    if redir:
        return _json({"error": "Unauthorized"}, 401)
    db = get_db()
    data = request.get_json(force=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return _json({"error": "title is required"}, 400)
    cal_id = data.get("calendar_id")
    if not cal_id:
        first = db.execute("SELECT id FROM calendars ORDER BY created_at LIMIT 1").fetchone()
        if not first:
            return _json({"error": "No local calendars exist — create one first"}, 400)
        cal_id = first["id"]
    new_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """INSERT INTO events
           (id, calendar_id, title, description, start_datetime, end_datetime, all_day, location, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (new_id, cal_id, title, data.get("description"),
         data.get("start_datetime"), data.get("end_datetime"),
         int(bool(data.get("all_day", False))), data.get("location"), now, now),
    )
    db.commit()
    return _json({"ok": True, "id": new_id})


@app.route("/calendar/api/events/<event_id>")
def calendar_api_get_event(event_id):
    _, redir = _cal_auth()
    if redir:
        return _json({"error": "Unauthorized"}, 401)
    row = get_db().execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not row:
        return _json({"error": "Not found"}, 404)
    return _json(dict(row))


@app.route("/calendar/api/events/<event_id>", methods=["POST"])
def calendar_api_update_event(event_id):
    _, redir = _cal_auth()
    if redir:
        return _json({"error": "Unauthorized"}, 401)
    db = get_db()
    data = request.get_json(force=True) or {}
    allowed = {"title", "description", "start_datetime", "end_datetime", "all_day", "location", "calendar_id"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if "all_day" in updates:
        updates["all_day"] = int(bool(updates["all_day"]))
    if not updates:
        return _json({"error": "Nothing to update"}, 400)
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    db.execute(f"UPDATE events SET {set_clause} WHERE id = ?", [*updates.values(), event_id])
    db.commit()
    return _json({"ok": True})


@app.route("/calendar/api/events/<event_id>/delete", methods=["POST"])
def calendar_api_delete_event(event_id):
    _, redir = _cal_auth()
    if redir:
        return _json({"error": "Unauthorized"}, 401)
    db = get_db()
    db.execute("DELETE FROM events WHERE id = ?", (event_id,))
    db.commit()
    return _json({"ok": True})


@app.route("/calendar/api/calendars")
def calendar_api_list_cals():
    _, redir = _cal_auth()
    if redir:
        return _json({"error": "Unauthorized"}, 401)
    rows = get_db().execute("SELECT * FROM calendars ORDER BY name").fetchall()
    return _json([dict(r) for r in rows])


@app.route("/calendar/api/calendars", methods=["POST"])
def calendar_api_create_cal():
    _, redir = _cal_auth()
    if redir:
        return _json({"error": "Unauthorized"}, 401)
    db = get_db()
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return _json({"error": "name is required"}, 400)
    new_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    ics_url = data.get("ics_url") or None
    import_as = data.get("import_as") if data.get("import_as") in ("events", "tasks") else "events"
    db.execute(
        """INSERT INTO calendars (id, name, color, source, ics_url, import_as, visible, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        (new_id, name, data.get("color", "#4a9eff"), "ics" if ics_url else "local", ics_url, import_as, now, now),
    )
    db.commit()
    if ics_url:
        try:
            from services.calendar.ics_service import fetch_and_store
            fetch_and_store(db, new_id, ics_url)
        except Exception as e:
            logger.exception("ICS initial fetch failed for %s", ics_url)
            return _json({"ok": True, "id": new_id, "warning": f"Calendar created but ICS import failed: {e}"})
    return _json({"ok": True, "id": new_id})


@app.route("/calendar/api/calendars/<cal_id>", methods=["POST"])
def calendar_api_update_cal(cal_id):
    _, redir = _cal_auth()
    if redir:
        return _json({"error": "Unauthorized"}, 401)
    db = get_db()
    data = request.get_json(force=True) or {}
    allowed = {"name", "color", "visible", "import_as"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if "import_as" in updates and updates["import_as"] not in ("events", "tasks"):
        updates.pop("import_as")
    if "import_as" in updates:
        row = db.execute("SELECT import_as FROM calendars WHERE id = ?", (cal_id,)).fetchone()
        current = (row["import_as"] if row else None) or "events"
        if updates["import_as"] != current:
            # Switching modes on a calendar that's already synced would either orphan
            # its existing rows (old mode) or silently duplicate every item under the
            # new mode on next sync — block it rather than reconcile two representations.
            if current == "events":
                has_data = db.execute("SELECT 1 FROM events WHERE calendar_id = ? LIMIT 1", (cal_id,)).fetchone()
            else:
                has_data = db.execute("SELECT 1 FROM tasks WHERE source_calendar_id = ? LIMIT 1", (cal_id,)).fetchone()
            if has_data:
                return _json({"error": "This calendar already has synced items. Delete and recreate it to change how it imports."}, 400)
    if not updates:
        return _json({"error": "Nothing to update"}, 400)
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    db.execute(f"UPDATE calendars SET {set_clause} WHERE id = ?", [*updates.values(), cal_id])
    db.commit()
    return _json({"ok": True})


@app.route("/calendar/api/calendars/<cal_id>/delete", methods=["POST"])
def calendar_api_delete_cal(cal_id):
    _, redir = _cal_auth()
    if redir:
        return _json({"error": "Unauthorized"}, 401)
    db = get_db()
    db.execute("DELETE FROM events WHERE calendar_id = ?", (cal_id,))
    # Deliberately not deleting tasks with source_calendar_id = cal_id: once imported,
    # they're first-class tasks the user may have edited/completed — not disposable
    # calendar-sync artifacts — so they outlive the calendar that created them.
    db.execute("DELETE FROM calendars WHERE id = ?", (cal_id,))
    db.commit()
    return _json({"ok": True})


@app.route("/calendar/api/calendars/<cal_id>/sync", methods=["POST"])
def calendar_api_sync_cal(cal_id):
    _, redir = _cal_auth()
    if redir:
        return _json({"error": "Unauthorized"}, 401)
    db = get_db()
    row = db.execute("SELECT ics_url, import_as FROM calendars WHERE id = ?", (cal_id,)).fetchone()
    if not row or not row["ics_url"]:
        return _json({"error": "No ICS URL for this calendar"}, 400)
    try:
        from services.calendar.ics_service import fetch_and_store
        count = fetch_and_store(db, cal_id, row["ics_url"])
        return _json({"ok": True, "imported": count, "imported_as": row["import_as"] or "events"})
    except Exception as e:
        logger.exception("ICS sync failed for calendar %s", cal_id)
        return _json({"error": str(e)}, 500)


def _start_rag():
    """Run an initial full vault reindex in a background daemon thread. No file
    watcher: vault content now only exists in Supabase Storage, and every write
    path (upload, fetch-url, create_note, move, delete) already reindexes
    explicitly right after writing — there's no "external edit" a watcher could
    still be needed to catch."""
    try:
        from services.rag import indexer
        indexer.index_all()
    except Exception:
        logger.exception("RAG startup failed — vault indexing disabled")


if __name__ == "__main__":
    import threading
    # Debug mode is opt-in (off by default) since Werkzeug's debugger allows
    # remote code execution if an unhandled exception is ever reachable from
    # outside localhost. Set FLASK_DEBUG=true for local development only.
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    # With debug=True, Werkzeug's reloader re-execs this whole module in a child
    # process (WERKZEUG_RUN_MAIN=true there) and keeps the original as a stub
    # watching for restarts. Without this guard, both processes ran their own
    # reindex pass concurrently.
    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        threading.Thread(target=_start_rag, daemon=True).start()
    app.run(debug=debug)
