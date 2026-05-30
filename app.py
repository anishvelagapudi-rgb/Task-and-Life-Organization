from flask import Flask, render_template, request, redirect, url_for
from db import get_db, init_db
from classes.Task import Task
from classes.Project import Project
from api import api_bp

app = Flask(__name__)

# Register the API blueprint — wires in all /api/* routes from api.py
app.register_blueprint(api_bp)


def check_login():
    pass  # TODO: verify session/token


# ─── helpers ──────────────────────────────────────────────────────────────────

def all_projects():
    return [dict(r) for r in get_db().execute("SELECT * FROM projects ORDER BY title").fetchall()]


# ─── index ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return redirect(url_for("dashboard"))


# ─── dashboard ────────────────────────────────────────────────────────────────

@app.route("/dashboard")
def dashboard():
    check_login()
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
    check_login()
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
    check_login()
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
    check_login()
    db = get_db()
    row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return "Task not found", 404
    return render_template("task_detail.html", task=dict(row), projects=all_projects())


@app.route("/tasks/<int:task_id>", methods=["POST"])
def update_task(task_id):
    check_login()
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
    check_login()
    db = get_db()
    db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    db.commit()
    return redirect(url_for("get_tasks"))


# ─── projects ─────────────────────────────────────────────────────────────────

@app.route("/projects")
def get_projects():
    check_login()
    projects = all_projects()
    return render_template("projects.html", projects=projects)


@app.route("/projects", methods=["POST"])
def create_project():
    check_login()
    data = request.form
    project = Project(
        title=data["title"],
        description=data.get("description") or None,
        status=data.get("status", "active"),
        progress=int(data.get("progress", 0)),
    )
    project.db_push(get_db())
    return redirect(url_for("get_projects"))


@app.route("/projects/<int:project_id>")
def get_project(project_id):
    check_login()
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
    check_login()
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
        progress=int(data.get("progress", 0)),
    )
    project.db_push(db)
    return redirect("/projects")


@app.route("/projects/<int:project_id>/delete")
def delete_project(project_id):
    check_login()
    db = get_db()
    db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    db.commit()
    return redirect(url_for("get_projects"))


if __name__ == "__main__":
    init_db(app)
    app.run(debug=True)
