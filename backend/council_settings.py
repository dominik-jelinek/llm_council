"""Persistent council preferences, independent of secrets and conversation history."""

from pathlib import Path

from backend.config import CouncilConfig
from backend.persistence import write_json


class CouncilSettingsStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def get(self) -> CouncilConfig:
        try:
            return CouncilConfig.model_validate_json(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return CouncilConfig()

    def save(self, configuration: CouncilConfig) -> None:
        write_json(self.path, configuration.model_dump(mode="json"))
