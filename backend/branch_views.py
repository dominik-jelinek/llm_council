"""Inline message editing and branch navigation over the shared workspace."""

import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.dependencies import Runs
from backend.rendering import render
from backend.runs import RunError, RunManager
from backend.views import context, workspace

router = APIRouter()


def message_fragment(
    request: Request, runs: RunManager, conversation_id: str, message_id: str, *, editing: bool
):
    state = context(runs, conversation_id)
    message = runs.user_message(state["conversation"], message_id)
    editing_message_id = message_id if editing else None
    if request.headers.get("HX-Request") != "true":
        return workspace(request, runs, conversation_id, editing_message_id=editing_message_id)
    return HTMLResponse(
        render(
            "message.html",
            **state,
            message=message,
            message_id=message_id,
            editing_message_id=editing_message_id,
        )
    )


@router.get("/conversations/{conversation_id}/messages/{message_id}")
async def show_message(conversation_id: str, message_id: str, request: Request, runs: Runs):
    return message_fragment(request, runs, conversation_id, message_id, editing=False)


@router.get("/conversations/{conversation_id}/messages/{message_id}/edit")
async def edit_message_form(conversation_id: str, message_id: str, request: Request, runs: Runs):
    return message_fragment(request, runs, conversation_id, message_id, editing=True)


def branch_workspace(request: Request, runs: RunManager, conversation_id: str, message_id: str):
    url = f"/conversations/{conversation_id}?branch={message_id}"
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(f"{url}#message-{message_id}", status_code=303)
    response = workspace(request, runs, conversation_id)
    response.headers["HX-Replace-Url"] = url
    response.headers["HX-Trigger-After-Swap"] = json.dumps({"focusMessage": message_id})
    return response


@router.post("/conversations/{conversation_id}/messages/{message_id}/edit")
async def edit_message(
    conversation_id: str, message_id: str, request: Request, runs: Runs, content: str = Form("")
):
    try:
        run = runs.start(conversation_id, content, edit_message_id=message_id)
    except RunError as error:
        if request.headers.get("HX-Request") != "true":
            raise
        return HTMLResponse(
            render("notice.html", error=str(error), setup_required=error.setup_required),
            status_code=error.status_code,
            headers={
                "HX-Retarget": f"#edit-error-{message_id}",
                "HX-Reswap": "innerHTML",
            },
        )
    return branch_workspace(request, runs, conversation_id, run.message_id)


@router.post("/conversations/{conversation_id}/messages/{message_id}/branch")
async def switch_branch(
    conversation_id: str, message_id: str, request: Request, runs: Runs, target_id: str = Form("")
):
    runs.select_branch(conversation_id, message_id, target_id)
    return branch_workspace(request, runs, conversation_id, target_id)
