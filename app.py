from dotenv import load_dotenv
load_dotenv()

import json
import logging
from logging.handlers import RotatingFileHandler
from flask import Flask, render_template, request, redirect, url_for, make_response
from db import get_db, init_db
from classes.Task import Task
from classes.Project import Project
from api import api_bp
from ai_routes import ai_bp
from services.ai.gemini_provider import GeminiProvider
from services.ai.service import AIService
from services.ai.budget import BudgetExceededError
from authlib.integrations.flask_client import OAuth
import uuid
import os

app = Flask(__name__)
app.secret_key = os.environ['FLASK_SECRET_KEY']
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Rotate at 1 MB, keep 3 backups → errors.log, errors.log.1, errors.log.2
_log_handler = RotatingFileHandler("errors.log", maxBytes=1_000_000, backupCount=3)
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
    client_kwargs={'scope': 'openid email profile'}
)

# Cookie holds a UUID. VALID_SESSIONS maps that UUID -> user info server-side.
# Supports multiple simultaneous sessions (different browsers/devices).
# Resets on server restart.
VALID_SESSIONS = {}
OWNER_EMAIL = os.environ['OWNER_EMAIL']

# In-memory chat history. Resets on server restart.
# List of {"role": "user"|"assistant", "content": "..."}
CHAT_HISTORY = []

# Shared AI service instance — initialized once after .env is loaded.
_ai_service = AIService(GeminiProvider())


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
    return google.authorize_redirect(url_for('authorize', _external=True), prompt='select_account')


@app.route('/authorize')
def authorize():
    token = google.authorize_access_token()
    email = token['userinfo'].get('email')
    if email != OWNER_EMAIL:
        return redirect(url_for('login', error='This app is private. Sign in with the correct account.'))
    cookie = str(uuid.uuid4())
    VALID_SESSIONS[cookie] = {'email': email, 'name': token['userinfo'].get('name', '')}
    res = make_response(redirect(url_for('dashboard')))
    res.set_cookie('UserID', cookie)
    return res


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


# ─── index ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return redirect(url_for("login"))


# ─── dashboard ────────────────────────────────────────────────────────────────

@app.route("/dashboard")
def dashboard():
    if not get_current_user():
        return redirect(url_for('login'))
    db = get_db()
    active_tasks = db.execute("""
        SELECT t.*, p.title as project_title
        FROM tasks t LEFT JOIN projects p ON t.project_id = p.id
        WHERE t.status IN ('inbox', 'active')
        ORDER BY t.priority DESC, t.due_date ASC
    """).fetchall()
    projects = db.execute("SELECT * FROM projects WHERE status = 'active' ORDER BY title").fetchall()
    return render_template("dashboard.html",
                           active_tasks=[dict(r) for r in active_tasks],
                           projects=[dict(r) for r in projects])


# ─── tasks ────────────────────────────────────────────────────────────────────

@app.route("/tasks")
def get_tasks():
    if not get_current_user():
        return redirect(url_for('login'))
    db = get_db()
    tasks = db.execute("""
        SELECT t.*, p.title as project_title
        FROM tasks t LEFT JOIN projects p ON t.project_id = p.id
        ORDER BY t.created_at DESC
    """).fetchall()
    return render_template("tasks.html",
                           tasks=[parse_task(dict(r)) for r in tasks],
                           projects=all_projects())


@app.route("/tasks", methods=["POST"])
def create_task():
    if not get_current_user():
        return redirect(url_for('login'))
    data = request.form
    task = Task(
        title=data["title"],
        description=data.get("description") or None,
        status=data.get("status", "inbox"),
        priority=data.get("priority", "medium"),
        due_date=data.get("due_date") or None,
        estimated_effort=data.get("estimated_effort") or None,
        energy_type=data.get("energy_type") or None,
        fear_level=data.get("fear_level") or None,
        ambiguity_level=data.get("ambiguity_level") or None,
        project_id=data.get("project_id") or None,
    )
    task.db_push(get_db())
    return redirect(url_for("get_tasks"))


@app.route("/tasks/<task_id>")
def get_task(task_id):
    if not get_current_user():
        return redirect(url_for('login'))
    db = get_db()

    # fetch the full task row (all columns) for the detail view
    row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return "Task not found", 404
    task = parse_task(dict(row))

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
                           all_tasks_json=json.dumps(all_tasks),  # sent to JS for tree rendering
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

    task = Task(
        id=task_id,
        title=data["title"],
        description=data.get("description") or None,
        status=data.get("status", "inbox"),
        priority=data.get("priority", "medium"),
        due_date=data.get("due_date") or None,
        estimated_effort=data.get("estimated_effort") or None,
        energy_type=data.get("energy_type") or None,
        fear_level=data.get("fear_level") or None,
        ambiguity_level=data.get("ambiguity_level") or None,
        project_id=data.get("project_id") or None,
        parent_task_id=data.get("parent_task_id") or None,  # None = root-level task
        tags=tags,
        dependencies=existing['dependencies'],   # carried forward unchanged
        task_notes=existing['task_notes'],        # carried forward unchanged
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


@app.route("/tasks/<task_id>/delete")
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
    project = Project(
        id=project_id,
        title=data["title"],
        description=data.get("description") or None,
        status=data.get("status", "active"),
        progress=int(data.get("progress") or 0),
    )
    project.db_push(db)
    return redirect("/projects")


# ─── chat ─────────────────────────────────────────────────────────────────────

@app.route("/chat")
def chat_page():
    if not get_current_user():
        return redirect(url_for('login'))
    return render_template("chat.html")


@app.route("/chat/history")
def chat_history():
    if not get_current_user():
        return json.dumps({"error": "Unauthorized"}), 401, {"Content-Type": "application/json"}
    return json.dumps({"messages": CHAT_HISTORY}), 200, {"Content-Type": "application/json"}


@app.route("/chat/message", methods=["POST"])
def chat_message():
    if not get_current_user():
        return json.dumps({"error": "Unauthorized"}), 401, {"Content-Type": "application/json"}
    data = request.get_json(force=True) or {}
    content = (data.get("content") or "").strip()
    if not content:
        return json.dumps({"error": "content is required"}), 400, {"Content-Type": "application/json"}
    try:
        reply = _ai_service.chat(get_db(), [{"role": "user", "content": content}])
    except BudgetExceededError as e:
        # Hard stop — do NOT call explain_error (that would make another API call).
        # Log it loudly so it's obvious a human needs to look at the server.
        logger.error("BUDGET EXCEEDED — AI disabled until server restart: %s", e)
        reply = str(e)
    except Exception as e:
        logger.exception("chat_message failed | user_input=%r", content)
        reply = _ai_service.explain_error(get_db(), content, str(e))
    CHAT_HISTORY.append({"role": "user", "content": content})
    CHAT_HISTORY.append({"role": "assistant", "content": reply})
    return json.dumps({"reply": reply}), 200, {"Content-Type": "application/json"}


@app.route("/projects/<project_id>/delete")
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


if __name__ == "__main__":
    init_db(app)
    app.run(debug=True)
