from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, request, redirect, url_for, make_response
from db import get_db, init_db
from classes.Task import Task
from classes.Project import Project
from api import api_bp
from authlib.integrations.flask_client import OAuth
import uuid
import os

app = Flask(__name__)
app.secret_key = os.environ['FLASK_SECRET_KEY']
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Register the API blueprint — wires in all /api/* routes from api.py
app.register_blueprint(api_bp)

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
                           tasks=[dict(r) for r in tasks],
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


@app.route("/tasks/<int:task_id>")
def get_task(task_id):
    if not get_current_user():
        return redirect(url_for('login'))
    db = get_db()
    row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return "Task not found", 404
    return render_template("task_detail.html", task=dict(row), projects=all_projects())


@app.route("/tasks/<int:task_id>", methods=["POST"])
def update_task(task_id):
    if not get_current_user():
        return redirect(url_for('login'))
    db = get_db()
    row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return "Task not found", 404
    data = request.form
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
    )
    task.db_push(db)
    return redirect("/tasks")


@app.route("/tasks/<int:task_id>/delete")
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


@app.route("/projects/<int:project_id>")
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


@app.route("/projects/<int:project_id>", methods=["POST"])
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


@app.route("/projects/<int:project_id>/delete")
def delete_project(project_id):
    if not get_current_user():
        return redirect(url_for('login'))
    db = get_db()
    db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    db.commit()
    return redirect(url_for("get_projects"))


if __name__ == "__main__":
    init_db(app)
    app.run(debug=True)
