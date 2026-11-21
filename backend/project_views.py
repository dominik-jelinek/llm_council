"""Project creation and conversation assignment, shared by forms and drag-and-drop."""

import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from backend.dependencies import Runs
from backend.rendering import render
from backend.runs import RunError, RunManager
from backend.views import context

router = APIRouter()


def editor_response(
    request: Request,
    runs: RunManager,
    *,
    current_conversation_id: str,
    current_project_id: str,
    target_conversation: dict | None = None,
    name: str = "",
    project_id: str = "",
    error: str | None = None,
    status_code: int = 200,
):
    partial = request.headers.get("HX-Request") == "true"
    return HTMLResponse(
        render(
            "projects/editor.html" if partial else "projects/page.html",
            target_conversation=target_conversation,
            projects=runs.projects.list(),
            name=name,
            project_id=project_id,
            current_conversation_id=current_conversation_id,
            current_project_id=current_project_id,
            error=error,
            standalone_setup=not partial,
        ),
        status_code=status_code,
        headers={"HX-Retarget": "#project-editor", "HX-Reswap": "outerHTML"},
    )


def updated_sidebar(request: Request, runs: RunManager, conversation_id: str, project_id: str):
    if request.headers.get("HX-Request") != "true":
        url = f"/conversations/{conversation_id}" if conversation_id else "/"
        if not conversation_id and project_id:
            url = f"/?project={runs.project(project_id)['id']}"
        return RedirectResponse(url, status_code=303)
    return HTMLResponse(
        render("sidebar.html", **context(runs, conversation_id or None, project_id or None)),
        headers={"HX-Trigger-After-Swap": json.dumps({"projectEditorSaved": True})},
    )


@router.get("/projects/new", response_class=HTMLResponse)
async def new_project_form(
    request: Request, runs: Runs, current_conversation_id: str = "", current_project_id: str = ""
):
    return editor_response(
        request,
        runs,
        current_conversation_id=current_conversation_id,
        current_project_id=current_project_id,
    )


@router.post("/projects", response_class=HTMLResponse)
async def create_project(
    request: Request,
    runs: Runs,
    name: str = Form(""),
    current_conversation_id: str = Form(""),
    current_project_id: str = Form(""),
):
    try:
        runs.projects.create(name)
    except (ValidationError, OSError) as error:
        invalid = isinstance(error, ValidationError)
        return editor_response(
            request,
            runs,
            name=name,
            current_conversation_id=current_conversation_id,
            current_project_id=current_project_id,
            error="Enter a project name between 1 and 80 characters."
            if invalid
            else "The project could not be saved. Please retry.",
            status_code=422 if invalid else 500,
        )
    return updated_sidebar(request, runs, current_conversation_id, current_project_id)


@router.get("/conversations/{conversation_id}/project", response_class=HTMLResponse)
async def assignment_form(
    conversation_id: str,
    request: Request,
    runs: Runs,
    current_conversation_id: str = "",
    current_project_id: str = "",
):
    conversation = runs.conversation(conversation_id)
    return editor_response(
        request,
        runs,
        target_conversation=conversation,
        project_id=conversation.get("project_id") or "",
        current_conversation_id=current_conversation_id,
        current_project_id=current_project_id,
    )


@router.post("/conversations/{conversation_id}/project", response_class=HTMLResponse)
async def assign_project(
    conversation_id: str,
    request: Request,
    runs: Runs,
    project_id: str = Form(""),
    editor: bool = Form(False),
    current_conversation_id: str = Form(""),
    current_project_id: str = Form(""),
):
    conversation = runs.conversation(conversation_id)
    try:
        runs.assign_project(conversation_id, project_id or None)
    except (RunError, OSError) as error:
        status = error.status_code if isinstance(error, RunError) else 500
        message = (
            str(error)
            if isinstance(error, RunError)
            else "The conversation could not be moved. Please retry."
        )
        if not editor:
            raise RunError(message, status) from error
        return editor_response(
            request,
            runs,
            target_conversation=conversation,
            project_id=project_id,
            current_conversation_id=current_conversation_id,
            current_project_id=current_project_id,
            error=message,
            status_code=status,
        )
    return updated_sidebar(request, runs, current_conversation_id, current_project_id)
