import uuid
from datetime import datetime, timezone


class Project:
    def __init__(
        self,
        title,
        description=None,
        status="active",
        progress=0,
        id=None,
    ):
        self.id = id or str(uuid.uuid4())
        self.title = title
        self.description = description
        self.status = status        # active | paused | completed | archived
        self.progress = progress    # 0–100
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)

    def db_push(self, conn):
        """Insert or update this project in the database. Sets self.id on insert.

        Builds a {column: value} dict from a hardcoded literal (never from external
        input or raw self.__dict__) and derives the INSERT/UPDATE statements from it —
        same dict-driven pattern as Task.db_push, avoiding hand-maintained parallel
        column/placeholder/parameter lists."""
        self.updated_at = datetime.now(timezone.utc)
        cursor = conn.cursor()

        existing = cursor.execute("SELECT id FROM projects WHERE id = ?", (self.id,)).fetchone()

        fields = {
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "progress": self.progress,
        }

        if existing is None:
            fields["id"] = self.id
            fields["created_at"] = self.created_at
            fields["updated_at"] = self.updated_at
            cols = ", ".join(fields)
            placeholders = ", ".join("?" * len(fields))
            cursor.execute(
                f"INSERT INTO projects ({cols}) VALUES ({placeholders})",
                list(fields.values()),
            )
        else:
            fields["updated_at"] = self.updated_at
            set_clause = ", ".join(f"{k}=?" for k in fields)
            cursor.execute(
                f"UPDATE projects SET {set_clause} WHERE id=?",
                [*fields.values(), self.id],
            )

        conn.commit()
        cursor.close()

    def to_dict(self):
        return self.__dict__.copy()

    def __repr__(self):
        return (
            f"Project(title={self.title!r}, description={self.description!r}, "
            f"status={self.status!r}, progress={self.progress})"
        )
