"""Persistent project names; conversation membership lives with each conversation."""

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from backend.persistence import write_json


class Project(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=80)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ProjectStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def list(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        projects = [
            Project.model_validate(item).model_dump(mode="json") for item in data["projects"]
        ]
        return sorted(projects, key=lambda item: (item["name"].casefold(), item["created_at"]))

    def get(self, project_id: str) -> dict | None:
        try:
            project_id = str(UUID(project_id))
        except (ValueError, AttributeError):
            return None
        return next((project for project in self.list() if project["id"] == project_id), None)

    def create(self, name: str) -> dict:
        project = Project(name=name).model_dump(mode="json")
        write_json(self.path, {"projects": [*self.list(), project]})
        return project
