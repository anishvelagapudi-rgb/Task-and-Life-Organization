from flask import Blueprint, jsonify, request
from db import get_db
from datetime import datetime, timezone
import hashlib
import hmac
import os
import uuid

# Blueprint groups all routes in this file under the "/api" prefix.
# So @api_bp.route("/tasks") → GET /api/tasks, etc.
# app.py registers this via app.register_blueprint(api_bp).
api_bp = Blueprint("api", __name__, url_prefix="/api")

API_KEY_HASH = os.environ.get("API_KEY_HASH", "")


@api_bp.before_request
def require_api_key():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "Unauthorized"}), 401
    incoming_hash = hashlib.sha512(auth[len("Bearer "):].encode()).hexdigest()
    if not hmac.compare_digest(incoming_hash, API_KEY_HASH):
        return jsonify({"error": "Unauthorized"}), 401

# Whitelists of fields that are allowed to be set via the API.
# Prevents callers from accidentally overwriting id, created_at, etc.
TASK_FIELDS = {
    "title", "description", "status", "priority", "due_date", "completed_at",
    "estimated_effort", "energy_type", "fear_level", "ambiguity_level",
    "project_id", "parent_task_id", "source_type", "ai_generated",
}

PROJECT_FIELDS = {"title", "description", "status", "progress"}


def _now():
    return datetime.now(timezone.utc).isoformat()


# ─── health ───────────────────────────────────────────────────────────────────

@api_bp.route("/health")
def health():
    # Simple liveness check. N8N and other callers can hit this to confirm the
    # server is up before running a workflow.
    return jsonify({"status": "ok"})


# ─── tasks ────────────────────────────────────────────────────────────────────

@api_bp.route("/tasks")
def list_tasks():
    db = get_db()

    # Base query joins projects so callers get project_title alongside each task.
    # "WHERE 1=1" is a trick that lets us append "AND ..." filters cleanly below
    # without worrying about whether this is the first condition or not.
    query = """
        SELECT t.*, p.title AS project_title
        FROM tasks t
        LEFT JOIN projects p ON t.project_id = p.id
        WHERE 1=1
    """
    params = []

    # Optional query-string filters: ?status=active&priority=high&project_id=3
    # We only append a filter if the caller actually provided it.
    for col in ("status", "priority", "project_id"):
        val = request.args.get(col)
        if val:
            query += f" AND t.{col} = ?"
            params.append(val)

    query += " ORDER BY t.created_at DESC"
    rows = db.execute(query, params).fetchall()

    # sqlite3.Row objects need to be cast to plain dicts before jsonify can
    # serialize them.
    return jsonify([dict(r) for r in rows])


@api_bp.route("/tasks/<task_id>")
def get_task(task_id):
    row = get_db().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dict(row))


@api_bp.route("/tasks", methods=["POST"])
def upsert_task():
    # force=True makes get_json() parse the body even if Content-Type isn't set.
    # Useful when N8N or a script sends JSON without explicitly setting the header.
    data = request.get_json(force=True) or {}
    db = get_db()
    ts = _now()
    task_id = data.get("id")

    if task_id:
        # ── UPDATE ──────────────────────────────────────────────────────────
        # id was provided → partial update. Only touch the fields the caller sent.
        if db.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
            return jsonify({"error": "Not found"}), 404

        # Filter out anything not in TASK_FIELDS (ignores id, created_at, junk keys).
        updates = {k: v for k, v in data.items() if k in TASK_FIELDS}
        if not updates:
            return jsonify({"error": "No valid fields to update"}), 400

        updates["updated_at"] = ts

        # Build "col1 = ?, col2 = ?" dynamically from whatever fields were sent.
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        db.execute(
            f"UPDATE tasks SET {set_clause} WHERE id = ?",
            [*updates.values(), task_id],
        )
        db.commit()
        return jsonify(dict(db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()))

    else:
        # ── CREATE ──────────────────────────────────────────────────────────
        if not data.get("title"):
            return jsonify({"error": "title is required"}), 400

        fields = {k: v for k, v in data.items() if k in TASK_FIELDS}
        fields.setdefault("status", "inbox")
        fields.setdefault("priority", "medium")
        fields.setdefault("source_type", "external")
        new_id = str(uuid.uuid4())
        fields["id"] = new_id
        fields["created_at"] = ts
        fields["updated_at"] = ts

        cols = ", ".join(fields)
        placeholders = ", ".join("?" * len(fields))
        db.execute(
            f"INSERT INTO tasks ({cols}) VALUES ({placeholders})",
            list(fields.values()),
        )
        db.commit()
        return jsonify(dict(db.execute("SELECT * FROM tasks WHERE id = ?", (new_id,)).fetchone())), 201


@api_bp.route("/tasks/<task_id>", methods=["DELETE"])
def delete_task(task_id):
    db = get_db()
    if db.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
        return jsonify({"error": "Not found"}), 404
    db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    db.commit()
    return jsonify({"deleted": task_id})


# ─── projects ─────────────────────────────────────────────────────────────────

@api_bp.route("/projects")
def list_projects():
    db = get_db()
    query = "SELECT * FROM projects WHERE 1=1"
    params = []

    # Optional filter: ?status=active
    status = request.args.get("status")
    if status:
        query += " AND status = ?"
        params.append(status)

    query += " ORDER BY title"
    return jsonify([dict(r) for r in db.execute(query, params).fetchall()])


@api_bp.route("/projects/<project_id>")
def get_project(project_id):
    row = get_db().execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dict(row))


@api_bp.route("/projects", methods=["POST"])
def upsert_project():
    data = request.get_json(force=True) or {}
    db = get_db()
    ts = _now()
    project_id = data.get("id")

    if project_id:
        # ── UPDATE ──────────────────────────────────────────────────────────
        if db.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            return jsonify({"error": "Not found"}), 404

        updates = {k: v for k, v in data.items() if k in PROJECT_FIELDS}
        if not updates:
            return jsonify({"error": "No valid fields to update"}), 400

        updates["updated_at"] = ts
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        db.execute(
            f"UPDATE projects SET {set_clause} WHERE id = ?",
            [*updates.values(), project_id],
        )
        db.commit()
        return jsonify(dict(db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()))

    else:
        # ── CREATE ──────────────────────────────────────────────────────────
        if not data.get("title"):
            return jsonify({"error": "title is required"}), 400

        fields = {k: v for k, v in data.items() if k in PROJECT_FIELDS}
        fields.setdefault("status", "active")
        fields.setdefault("progress", 0)
        new_id = str(uuid.uuid4())
        fields["id"] = new_id
        fields["created_at"] = ts
        fields["updated_at"] = ts

        cols = ", ".join(fields)
        placeholders = ", ".join("?" * len(fields))
        db.execute(
            f"INSERT INTO projects ({cols}) VALUES ({placeholders})",
            list(fields.values()),
        )
        db.commit()
        return jsonify(dict(db.execute("SELECT * FROM projects WHERE id = ?", (new_id,)).fetchone())), 201


@api_bp.route("/projects/<project_id>", methods=["DELETE"])
def delete_project(project_id):
    db = get_db()
    if db.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
        return jsonify({"error": "Not found"}), 404
    db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    db.commit()
    return jsonify({"deleted": project_id})
