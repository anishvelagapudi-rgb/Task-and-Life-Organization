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
        """Insert or update this project in the database. Sets self.id on insert."""
        self.updated_at = datetime.now(timezone.utc)
        cursor = conn.cursor()

        existing = cursor.execute("SELECT id FROM projects WHERE id = ?", (self.id,)).fetchone()
        if existing is None:
            cursor.execute(
                """
                INSERT INTO projects (
                    id, title, description, status,
                    progress, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.id, self.title, self.description, self.status,
                    self.progress, self.created_at, self.updated_at,
                ),
            )
        else:
            cursor.execute(
                """
                UPDATE projects SET
                    title=?, description=?, status=?,
                    progress=?, updated_at=?
                WHERE id=?
                """,
                (
                    self.title, self.description, self.status,
                    self.progress, self.updated_at, self.id,
                ),
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
