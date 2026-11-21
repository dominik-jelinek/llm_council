"""Async OpenRouter SDK adapter; provider failures remain isolated per model."""

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

import httpx
from openrouter import OpenRouter
from openrouter.errors import OpenRouterError

from backend.config import ModelSpec, get_settings, model_label
from backend.usage import begin_request, finish_request, normalize_usage

logger = logging.getLogger(__name__)


async def query_model(
    model: ModelSpec, messages: list[dict[str, str]], timeout: float = 120.0
) -> dict[str, Any] | None:
    """Call one model/effort pair, returning plain data or None on provider failure."""
    model_id, effort = model
    label = model_label(model_id, effort)
    api_key = get_settings().openrouter_api_key
    if api_key is None or not api_key.get_secret_value().strip():
        logger.error("OPENROUTER_API_KEY is not configured; cannot query model %s", label)
        return None

    request = begin_request(model)
    try:
        async with OpenRouter(api_key=api_key.get_secret_value()) as client:
            response = await client.chat.send_async(
                model=model_id,
                messages=messages,
                reasoning={"effort": effort} if effort is not None else None,
                stream=False,
                timeout_ms=int(timeout * 1000),
                # The app exposes Retry; keep SDK retries from extending run lifetimes.
                retries=None,
            )
            payload = response.model_dump(mode="json")
            request["generation_id"] = payload.get("id")
            request["usage"] = normalize_usage(payload.get("usage"))
            message = response.choices[0].message
            if not isinstance(message.content, str) or not message.content.strip():
                request["status"] = "failed"
                logger.warning("Model %s returned an empty response", label)
                return None
            request["status"] = "complete"
            return {
                "content": message.content,
                "reasoning_details": message.model_dump(mode="json").get("reasoning_details"),
                "request": request,
            }
    except asyncio.CancelledError:
        request["status"] = "cancelled"
        raise
    except (OpenRouterError, httpx.HTTPError, IndexError, TypeError, ValueError):
        request["status"] = "failed"
        logger.warning("Request to model %s failed", label, exc_info=True)
        return None
    finally:
        finish_request()


async def query_models_parallel(
    models: Sequence[ModelSpec], messages: list[dict[str, str]]
) -> list[tuple[ModelSpec, dict[str, Any] | None]]:
    """Return one result per configured member in order, including identical entries."""
    responses = await asyncio.gather(*(query_model(model, messages) for model in models))
    return list(zip(models, responses))
