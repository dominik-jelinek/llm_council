"""HTML presentation helpers. Model output is always treated as untrusted text."""

import re
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt
from markupsafe import Markup

from backend.config import model_label
from backend.council import ranking_metadata, result_model_label
from backend.usage import PHASE_LABELS, format_cost, format_tokens, summarize_usage

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
markdown = MarkdownIt("commonmark", {"html": False})


def static_url(filename: str) -> str:
    """Refresh cached assets when the file changes, including during development."""
    version = (FRONTEND_DIR / "static" / filename).stat().st_mtime_ns
    return f"/static/{filename}?v={version}"


@lru_cache(maxsize=256)
def render_markdown(text: str) -> Markup:
    # CommonMark with raw HTML disabled also rejects unsafe link protocols.
    return Markup(markdown.render(text or ""))


def short_model(model: str) -> str:
    return model.partition("/")[2] or model


def deanonymize(text: str, labels: dict[str, str]) -> str:
    return re.sub(
        r"\bResponse [A-Z]\b",
        lambda match: f"**{short_model(labels[match[0]])}**" if match[0] in labels else match[0],
        text,
    )


templates = Environment(
    loader=FileSystemLoader(FRONTEND_DIR / "templates"),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
templates.filters.update(markdown=render_markdown, short_model=short_model, deanonymize=deanonymize)
templates.filters.update(cost=format_cost, tokens=format_tokens)
templates.globals.update(
    static_url=static_url,
    ranking_metadata=ranking_metadata,
    model_label=model_label,
    result_model_label=result_model_label,
    summarize_usage=summarize_usage,
    phase_labels=PHASE_LABELS,
)


def render(template: str, **context) -> str:
    return templates.get_template(template).render(**context)
