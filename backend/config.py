"""Configuration for the LLM Council."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Explicit UI values override environment and .env; secrets stay masked."""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Optional so saved conversations remain accessible without provider credentials.
    openrouter_api_key: SecretStr | None = None
    data_dir: Path = Path("data/conversations")
    host: str = "127.0.0.1"
    port: int = Field(default=8001, ge=1, le=65535)

    @field_validator("data_dir", mode="before")
    @classmethod
    def normalize_data_dir(cls, value: str | Path) -> Path:
        if isinstance(value, str) and not value.strip():
            raise ValueError("DATA_DIR must not be empty.")
        return Path(value).expanduser().resolve()


_active_settings: ContextVar[Settings | None] = ContextVar("active_settings", default=None)


def get_settings() -> Settings:
    return _active_settings.get() or Settings()


@contextmanager
def use_settings(settings: Settings) -> Iterator[None]:
    """Keep a run and its parallel requests on the same credentials."""
    token = _active_settings.set(settings)
    try:
        yield
    finally:
        _active_settings.reset(token)


ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
ModelId = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[^\s/]+/[^\s]+$")]
ModelSpec = tuple[ModelId, ReasoningEffort | None]


def model_label(model: str, reasoning_effort: ReasoningEffort | None = None) -> str:
    """Distinguish council members using the same model at different efforts."""
    return f"{model} ({reasoning_effort})" if reasoning_effort is not None else model


# (OpenRouter model identifier, reasoning effort). None keeps the provider default.
COUNCIL_MODELS: list[ModelSpec] = [
    ("openai/gpt-6.2-sol", None),
    ("anthropic/claude-fable-5.2", None),
    ("google/gemini-4-neon", None),
]

# Chairman model - synthesizes final response
CHAIRMAN_MODEL: ModelSpec = ("anthropic/claude-fable-5.2", None)

# Fast title generation uses the same SDK and model configuration format.
TITLE_MODEL: ModelSpec = ("google/gemini-4-neon", None)


class CouncilConfig(BaseModel):
    """An immutable, validated council configuration captured at the start of a run."""

    model_config = ConfigDict(frozen=True, validate_default=True, extra="forbid")

    members: tuple[ModelSpec, ...] = Field(
        default_factory=lambda: tuple(COUNCIL_MODELS), min_length=1, max_length=26
    )
    chairman: ModelSpec = Field(default_factory=lambda: CHAIRMAN_MODEL)
    title_model: ModelSpec = Field(default_factory=lambda: TITLE_MODEL)
