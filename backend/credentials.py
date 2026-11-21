"""Local API credentials, kept separate from council preferences and history."""

import os
from pathlib import Path

from pydantic import BaseModel, SecretStr, field_validator

from backend.config import Settings
from backend.persistence import write_json


class OpenRouterCredentials(BaseModel):
    openrouter_api_key: SecretStr

    @field_validator("openrouter_api_key")
    @classmethod
    def validate_key(cls, value: SecretStr) -> SecretStr:
        key = value.get_secret_value().strip()
        if not key or any(not 33 <= ord(character) <= 126 for character in key):
            raise ValueError("Enter an API key without spaces or line breaks.")
        return SecretStr(key)


class CredentialsStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def get(self) -> Settings:
        self._check_path()
        try:
            if os.name == "posix":
                self.path.chmod(0o600)
                self.path.parent.chmod(0o700)
            saved = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return Settings()
        credentials = OpenRouterCredentials.model_validate_json(saved)
        return Settings(openrouter_api_key=credentials.openrouter_api_key)

    def save(self, credentials: OpenRouterCredentials) -> None:
        self._check_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            self.path.parent.chmod(0o700)
        # Atomic writes create the replacement file with owner-only permissions.
        write_json(
            self.path, {"openrouter_api_key": credentials.openrouter_api_key.get_secret_value()}
        )

    def _check_path(self) -> None:
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise OSError("Credential storage must not use symbolic links.")
