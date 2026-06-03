import json
from datetime import datetime, timezone


class Task:
    def __init__(
        self,
        title,
        description=None,
        status="inbox",
        priority="medium",
        due_date=None,
        estimated_effort=None,
        energy_type=None,
        fear_level=None,
        ambiguity_level=None,
        project_id=None,
        parent_task_id=None,
        source_type="manual",
        ai_generated=False,
        tags=None,
        dependencies=None,
        task_notes=None,
        id=None,
    ):
        self.id = id
        self.title = title
        self.description = description
        self.status = status          # inbox | active | blocked | done | archived
        self.priority = priority      # low | medium | high | critical
        self.due_date = due_date
        self.completed_at = None
        self.estimated_effort = estimated_effort  # minutes
        self.energy_type = energy_type            # deep_focus | light_admin | social | creative | low_energy
        self.fear_level = fear_level              # 1–10
        self.ambiguity_level = ambiguity_level    # 1–10
        self.project_id = project_id
        self.parent_task_id = parent_task_id
        self.source_type = source_type
        self.ai_generated = ai_generated
        self.tags = tags if tags is not None else []              # list of strings
        self.dependencies = dependencies if dependencies is not None else []  # list of task IDs
        self.task_notes = task_notes if task_notes is not None else []  # list of {note_path, relationship_type}
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)

    def db_push(self, conn):
        """Insert or update this task in the database. Sets self.id on insert."""
        self.updated_at = datetime.now(timezone.utc)
        cursor = conn.cursor()

        if self.id is None:
            cursor.execute(
                """
                INSERT INTO tasks (
                    title, description, status, priority,
                    due_date, completed_at, estimated_effort,
                    energy_type, fear_level, ambiguity_level,
                    project_id, parent_task_id,
                    source_type, ai_generated,
                    created_at, updated_at,
                    tags, dependencies, task_notes
                ) VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?, ?
                )
                """,
                (
                    self.title, self.description, self.status, self.priority,
                    self.due_date, self.completed_at, self.estimated_effort,
                    self.energy_type, self.fear_level, self.ambiguity_level,
                    self.project_id, self.parent_task_id,
                    self.source_type, self.ai_generated,
                    self.created_at, self.updated_at,
                    json.dumps(self.tags), json.dumps(self.dependencies), json.dumps(self.task_notes),
                ),
            )
            self.id = cursor.lastrowid
        else:
            cursor.execute(
                """
                UPDATE tasks SET
                    title=?, description=?, status=?, priority=?,
                    due_date=?, completed_at=?, estimated_effort=?,
                    energy_type=?, fear_level=?, ambiguity_level=?,
                    project_id=?, parent_task_id=?,
                    source_type=?, ai_generated=?,
                    updated_at=?,
                    tags=?, dependencies=?, task_notes=?
                WHERE id=?
                """,
                (
                    self.title, self.description, self.status, self.priority,
                    self.due_date, self.completed_at, self.estimated_effort,
                    self.energy_type, self.fear_level, self.ambiguity_level,
                    self.project_id, self.parent_task_id,
                    self.source_type, self.ai_generated,
                    self.updated_at,
                    json.dumps(self.tags), json.dumps(self.dependencies), json.dumps(self.task_notes),
                    self.id,
                ),
            )

        conn.commit()
        cursor.close()

    def to_dict(self):
        return self.__dict__.copy()

    def __repr__(self):
        return (
            f"Task(title={self.title!r}, description={self.description!r}, "
            f"status={self.status!r}, priority={self.priority!r}, due_date={self.due_date}, "
            f"estimated_effort={self.estimated_effort}, energy_type={self.energy_type!r}, "
            f"fear_level={self.fear_level}, ambiguity_level={self.ambiguity_level})"
        )
