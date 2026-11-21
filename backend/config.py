"""Configuration for the LLM Council."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment overrides the project's .env; secrets stay masked in reprs."""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Optional so saved conversations remain accessible without provider credentials.
    openrouter_api_key: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()


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

# Data directory for conversation storage
DATA_DIR = "data/conversations"


class CouncilConfig(BaseModel):
    """An immutable, validated council configuration captured at the start of a run."""

    model_config = ConfigDict(frozen=True, validate_default=True, extra="forbid")

    members: tuple[ModelSpec, ...] = Field(
        default_factory=lambda: tuple(COUNCIL_MODELS), min_length=1, max_length=26
    )
    chairman: ModelSpec = Field(default_factory=lambda: CHAIRMAN_MODEL)
    title_model: ModelSpec = Field(default_factory=lambda: TITLE_MODEL)
