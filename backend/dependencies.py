"""Shared HTTP dependencies; application state owns the concrete services."""

from typing import Annotated

from fastapi import Depends, Request

from backend.runs import RunError, RunManager


async def get_runs(request: Request) -> RunManager:
    origin = request.headers.get("Origin")
    if request.method == "POST" and origin and origin != str(request.base_url).rstrip("/"):
        raise RunError("This request must come from the application itself.", 403)
    return request.app.state.runs


Runs = Annotated[RunManager, Depends(get_runs)]
