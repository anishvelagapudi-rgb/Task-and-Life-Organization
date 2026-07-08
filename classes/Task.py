import json
import uuid
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
        psych_reasoning=None,
        project_id=None,
        parent_task_id=None,
        source_type="manual",
        ai_generated=False,
        tags=None,
        dependencies=None,
        task_notes=None,
        recurring=None,
        completed_at=None,
        source_uid=None,
        source_calendar_id=None,
        id=None,
    ):
        self.id = id or str(uuid.uuid4())
        self.title = title
        self.description = description
        self.status = status          # inbox | active | blocked | done | archived
        self.priority = priority      # low | medium | high | critical
        self.due_date = due_date
        self.completed_at = completed_at  # preserved by callers that reload the existing row; defaults None for genuinely new tasks
        self.recurring = recurring    # None (not recurring) | "daily" | "weekly" — never combined with due_date
        self.estimated_effort = estimated_effort  # minutes
        self.energy_type = energy_type            # deep_focus | light_admin | social | creative | low_energy
        self.fear_level = fear_level              # 1–5
        self.ambiguity_level = ambiguity_level    # 1–5
        self.psych_reasoning = psych_reasoning    # short AI explanation for fear/ambiguity/energy/effort, if set by the AI
        self.project_id = project_id
        self.parent_task_id = parent_task_id
        self.source_type = source_type
        self.ai_generated = ai_generated
        self.source_uid = source_uid              # external UID (e.g. ICS UID) this task was imported from, if any
        self.source_calendar_id = source_calendar_id  # calendars.id this task was imported from, if any
        self.tags = tags if tags is not None else []              # list of strings
        self.dependencies = dependencies if dependencies is not None else []  # list of task IDs
        self.task_notes = task_notes if task_notes is not None else []  # list of {note_path, relationship_type}
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)

    def db_push(self, conn):
        """Insert or update this task in the database. Sets self.id on insert.

        Builds a {column: value} dict from a hardcoded literal (never from external
        input or raw self.__dict__, to avoid smuggling non-column attributes into SQL)
        and derives the INSERT/UPDATE statements from it — avoids maintaining parallel
        column lists, placeholder lists, and parameter tuples by hand as fields grow."""
        self.updated_at = datetime.now(timezone.utc)
        cursor = conn.cursor()

        existing = cursor.execute("SELECT id FROM tasks WHERE id = ?", (self.id,)).fetchone()

        fields = {
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "priority": self.priority,
            "due_date": self.due_date,
            "completed_at": self.completed_at,
            "estimated_effort": self.estimated_effort,
            "energy_type": self.energy_type,
            "fear_level": self.fear_level,
            "ambiguity_level": self.ambiguity_level,
            "psych_reasoning": self.psych_reasoning,
            "project_id": self.project_id,
            "parent_task_id": self.parent_task_id,
            "source_type": self.source_type,
            "ai_generated": int(self.ai_generated),
            "source_uid": self.source_uid,
            "source_calendar_id": self.source_calendar_id,
            "tags": json.dumps(self.tags),
            "dependencies": json.dumps(self.dependencies),
            "task_notes": json.dumps(self.task_notes),
            "recurring": self.recurring,
        }

        if existing is None:
            fields["id"] = self.id
            fields["created_at"] = self.created_at.isoformat()
            fields["updated_at"] = self.updated_at.isoformat()
            cols = ", ".join(fields)
            placeholders = ", ".join("?" * len(fields))
            cursor.execute(
                f"INSERT INTO tasks ({cols}) VALUES ({placeholders})",
                list(fields.values()),
            )
        else:
            fields["updated_at"] = self.updated_at.isoformat()
            set_clause = ", ".join(f"{k}=?" for k in fields)
            cursor.execute(
                f"UPDATE tasks SET {set_clause} WHERE id=?",
                [*fields.values(), self.id],
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
