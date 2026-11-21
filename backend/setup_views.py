"""HTML editor for persistent council preferences."""

import logging
from itertools import zip_longest
from typing import get_args

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from backend.config import CouncilConfig, ReasoningEffort
from backend.dependencies import Runs
from backend.rendering import render

router = APIRouter()
logger = logging.getLogger(__name__)


def setup_response(request: Request, values: dict, *, errors=(), saved=False, status_code=200):
    partial = request.headers.get("HX-Request") == "true"
    return HTMLResponse(
        render(
            "setup/form.html" if partial else "setup/page.html",
            values=values,
            errors=errors,
            saved=saved,
            efforts=get_args(ReasoningEffort),
            standalone_setup=not partial,
        ),
        status_code=status_code,
        headers={"HX-Retarget": "#council-settings", "HX-Reswap": "outerHTML"},
    )


@router.get("/council/setup", response_class=HTMLResponse)
async def council_setup(request: Request, runs: Runs):
    try:
        configuration = runs.settings.get()
    except (ValueError, OSError):
        logger.exception("Could not read council settings")
        return setup_response(
            request,
            CouncilConfig().model_dump(),
            errors=[
                "Saved settings could not be read. Defaults are shown below; save to replace them."
            ],
        )
    return setup_response(request, configuration.model_dump())


@router.post("/council/setup", response_class=HTMLResponse)
async def save_council_setup(request: Request, runs: Runs):
    form = await request.form()
    models, efforts = form.getlist("member_model"), form.getlist("member_effort")
    values = {
        "members": [
            (model, effort or None) for model, effort in zip_longest(models, efforts, fillvalue="")
        ],
        "chairman": (form.get("chairman_model", ""), form.get("chairman_effort") or None),
        "title_model": (form.get("title_model", ""), form.get("title_effort") or None),
    }
    if len(models) != len(efforts):
        return setup_response(
            request,
            values,
            errors=["Every member needs a model and effort choice."],
            status_code=422,
        )
    try:
        configuration = CouncilConfig.model_validate(values)
    except ValidationError as error:
        errors = []
        for issue in error.errors():
            field, *path = issue["loc"]
            label = {
                "members": "Council members",
                "chairman": "Chairman",
                "title_model": "Title model",
            }[field]
            if field == "members" and path:
                label = f"Member {path[0] + 1}"
            message = issue["msg"]
            if issue["type"] == "string_pattern_mismatch":
                message = "Use an OpenRouter model ID in provider/model format, without spaces."
            errors.append(f"{label}: {message}")
        return setup_response(request, values, errors=errors, status_code=422)
    try:
        runs.settings.save(configuration)
    except OSError:
        logger.exception("Could not save council settings")
        return setup_response(
            request,
            values,
            errors=[
                "Settings could not be saved. Please check that the data directory is writable and try again."
            ],
            status_code=500,
        )
    return setup_response(request, configuration.model_dump(), saved=True)
