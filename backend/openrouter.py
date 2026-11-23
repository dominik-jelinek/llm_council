"""Async OpenRouter SDK adapter; provider failures remain isolated per model."""

import asyncio
import json
import logging
from collections.abc import Sequence
from typing import Any

import httpx
from openrouter import OpenRouter
from openrouter.errors import OpenRouterError

from backend.config import ModelSpec, get_settings, model_label
from backend.failures import ProviderError
from backend.usage import begin_request, finish_request, normalize_usage

logger = logging.getLogger(__name__)


async def query_model(
    model: ModelSpec, messages: list[dict[str, str]], timeout: float = 120.0
) -> dict[str, Any]:
    """Call one model/effort pair; failures expose only a safe, actionable message."""
    model_id, effort = model
    label = model_label(model_id, effort)
    api_key = get_settings().openrouter_api_key
    if api_key is None or not api_key.get_secret_value().strip():
        raise ProviderError("missing_key")

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
                raise ProviderError("empty")
            request["status"] = "complete"
            return {
                "content": message.content,
                "reasoning_details": message.model_dump(mode="json").get("reasoning_details"),
                "request": request,
            }
    except asyncio.CancelledError:
        request["status"] = "cancelled"
        raise
    except ProviderError as error:
        request.update(status="failed", error=error.as_dict())
        logger.warning("Request to model %s failed (%s)", label, error.code)
        raise
    except (OpenRouterError, httpx.HTTPError, IndexError, TypeError, ValueError) as error:
        failure = provider_failure(error)
        request.update(status="failed", error=failure.as_dict())
        # Provider error bodies can echo credentials; log only the failure category.
        logger.warning("Request to model %s failed (%s)", label, type(error).__name__)
        raise failure from None
    finally:
        finish_request()


def provider_failure(error: Exception) -> ProviderError:
    if isinstance(error, httpx.TimeoutException):
        return ProviderError("timeout")
    if isinstance(error, httpx.TransportError):
        return ProviderError("network")
    status = getattr(error, "status_code", None)
    if status in {400, 422}:
        try:
            detail = json.loads(error.body)["error"]
            message = (
                str(detail.get("message", ""))
                + " "
                + str(detail.get("metadata", {}).get("raw", ""))
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            message = ""
        message = message.lower()
        if any(word in message for word in ("reasoning", "thinking")) and any(
            word in message
            for word in (
                "unsupported",
                "not support",
                "cannot",
                "invalid",
                "must be",
                "only supports",
            )
        ):
            return ProviderError("reasoning")
    codes = {
        400: "request",
        401: "authentication",
        402: "credits",
        403: "access",
        404: "model",
        408: "timeout",
        422: "request",
        429: "rate_limit",
        504: "timeout",
    }
    if status in codes:
        return ProviderError(codes[status])
    return ProviderError("unavailable" if status and status >= 500 else "invalid_response")


async def query_models_parallel(
    models: Sequence[ModelSpec], messages: list[dict[str, str]]
) -> list[tuple[ModelSpec, dict[str, Any] | None]]:
    """Return one result per configured member in order, including identical entries."""
    responses = await asyncio.gather(
        *(query_model(model, messages) for model in models), return_exceptions=True
    )
    for response in responses:
        if isinstance(response, BaseException) and not isinstance(response, ProviderError):
            raise response
    failures = [response for response in responses if isinstance(response, ProviderError)]
    if failures and len(failures) == len(responses):
        # Account problems are more useful to fix first than a single model outage.
        raise next(
            (error for error in failures if error.code in {"authentication", "credits"}),
            failures[0],
        )
    return [
        (model, None if isinstance(response, ProviderError) else response)
        for model, response in zip(models, responses)
    ]
