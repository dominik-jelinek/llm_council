"""Request accounting and display helpers; never estimate unreported charges."""

import logging
import math
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from backend.config import ModelSpec

logger = logging.getLogger(__name__)
PHASE_LABELS = {"stage1": "Stage 1", "stage2": "Stage 2", "stage3": "Chairman", "title": "Title"}
_phase = ContextVar("request_phase", default="request")


@dataclass
class Tracker:
    requests: list[dict[str, Any]]
    changed: Callable[[], None]
    attempt: str


_tracker: ContextVar[Tracker | None] = ContextVar("usage_tracker", default=None)


@contextmanager
def track_requests(requests: list[dict], changed: Callable[[], None], attempt: str) -> Iterator:
    token = _tracker.set(Tracker(requests, changed, attempt))
    try:
        yield
    finally:
        _tracker.reset(token)


@contextmanager
def request_phase(phase: str) -> Iterator:
    token = _phase.set(phase)
    try:
        yield
    finally:
        _phase.reset(token)


def begin_request(model: ModelSpec) -> dict[str, Any]:
    tracker = _tracker.get()
    request = {
        "model": model[0],
        "reasoning_effort": model[1],
        "phase": _phase.get(),
        "status": "running",
        "usage": {},
        "generation_id": None,
        "attempt": tracker.attempt if tracker else None,
    }
    if tracker:
        tracker.requests.append(request)
    return request


def finish_request() -> None:
    tracker = _tracker.get()
    if tracker:
        try:
            tracker.changed()
        except OSError:
            # Accounting persistence must not discard a completed model answer.
            logger.exception("Could not save request usage")


def _number(value: Any, *, integer: bool = False) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or (integer and int(value) != value):
        return None
    return int(value) if integer else value


def normalize_usage(raw: dict | None) -> dict[str, Any]:
    raw = raw or {}
    inputs = raw.get("prompt_tokens_details") or {}
    outputs = raw.get("completion_tokens_details") or {}
    costs = raw.get("cost_details") or {}
    fields = {
        "input_tokens": raw.get("prompt_tokens"),
        "output_tokens": raw.get("completion_tokens"),
        "total_tokens": raw.get("total_tokens"),
        "cached_tokens": inputs.get("cached_tokens"),
        "cache_write_tokens": inputs.get("cache_write_tokens"),
        "reasoning_tokens": outputs.get("reasoning_tokens"),
    }
    usage = {key: _number(value, integer=True) for key, value in fields.items()}
    usage["cost_usd"] = _number(raw.get("cost"))
    usage["upstream_input_cost_usd"] = _number(costs.get("upstream_inference_prompt_cost"))
    usage["upstream_output_cost_usd"] = _number(costs.get("upstream_inference_completions_cost"))
    usage["is_byok"] = raw.get("is_byok", False)
    # Reasoning is part of output; cached reads are part of input, never extra tokens.
    for child, parent in (("cached_tokens", "input_tokens"), ("reasoning_tokens", "output_tokens")):
        if usage[child] is not None and usage[parent] is not None and usage[child] > usage[parent]:
            usage[child] = None
    return usage


def summarize_usage(requests: list[dict] | None) -> dict[str, Any]:
    requests = requests or []
    usages = [request.get("usage") or {} for request in requests]
    costs = [usage["cost_usd"] for usage in usages if usage.get("cost_usd") is not None]
    cost = sum((Decimal(str(value)) for value in costs), Decimal(0)) if costs else None

    def total(field):
        values = [usage.get(field) for usage in usages]
        return sum(values) if values and all(value is not None for value in values) else None

    inputs, cached = total("input_tokens"), total("cached_tokens")
    outputs, reasoning = total("output_tokens"), total("reasoning_tokens")
    return {
        "cost_usd": cost,
        "partial": len(costs) < len(requests),
        "input_tokens": inputs,
        "cached_tokens": cached,
        "cache_percent": cached / inputs * 100 if inputs and cached is not None else None,
        "cache_write_tokens": total("cache_write_tokens"),
        "output_tokens": outputs,
        "reasoning_tokens": reasoning,
        "other_output_tokens": outputs - reasoning
        if outputs is not None and reasoning is not None
        else None,
        "total_tokens": total("total_tokens"),
    }


def format_cost(value: Any) -> str:
    if value is None:
        return "Unavailable"
    amount = Decimal(str(value))
    if 0 < amount < Decimal("0.000001"):
        return "< $0.000001"
    return "$" + f"{amount:.6f}".rstrip("0").rstrip(".")


def format_tokens(value: Any) -> str:
    return f"{value:,}" if value is not None else "—"
