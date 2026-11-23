"""HTML routes and HTMX events. Execution and storage live in their own services."""

import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from backend import branches
from backend.dependencies import Runs
from backend.events import encode_event, event_response
from backend.rendering import render
from backend.runs import Run, RunManager

router = APIRouter()


def context(
    runs: RunManager, conversation_id: str | None = None, project_id: str | None = None
) -> dict:
    conversation = runs.conversation(conversation_id) if conversation_id else None
    run = runs.active.get(conversation_id)
    messages = conversation["messages"] if conversation else []
    run_visible = bool(run and any(message["id"] == run.message_id for message in messages))
    # The active run renders its own answer while a title is still finishing.
    visible_messages = messages[:-1] if run_visible and run.saved else messages
    last_question = next(
        (message for message in reversed(messages) if message["role"] == "user"), {}
    )
    projects = runs.projects.list()
    conversations = runs.store.list()
    project_ids = {project["id"] for project in projects}
    selected_project_id = conversation.get("project_id") if conversation else project_id
    project = next((item for item in projects if item["id"] == selected_project_id), None)
    pending = bool(messages and messages[-1]["role"] == "user")
    error = last_question.get("last_error", {}) if pending and run is None else {}
    return {
        "conversation": conversation,
        "conversations": conversations,
        "project": project,
        "projects": [
            {
                **item,
                "conversations": [c for c in conversations if c.get("project_id") == item["id"]],
            }
            for item in projects
        ],
        "ungrouped_conversations": [
            item for item in conversations if item.get("project_id") not in project_ids
        ],
        "messages": visible_messages,
        "run": run,
        "run_visible": run_visible,
        "branch_navigation": branches.navigation(conversation) if conversation else {},
        "busy": run is not None,
        "pending": pending,
        "error": error.get("message"),
        "setup_required": error.get("setup_required", False),
        "council_enabled": last_question.get("council_enabled", True),
    }


def workspace(
    request: Request,
    runs: RunManager,
    conversation_id: str | None = None,
    *,
    project_id: str | None = None,
    **extra,
):
    partial = request.headers.get("HX-Request") == "true"
    return HTMLResponse(
        render(
            "workspace.html" if partial else "page.html",
            **context(runs, conversation_id, project_id),
            **extra,
        )
    )


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, runs: Runs, project: str | None = None):
    project_id = runs.project(project)["id"] if project else None
    return workspace(request, runs, project_id=project_id)


@router.get("/conversations/{conversation_id}", response_class=HTMLResponse)
async def conversation_page(
    conversation_id: str, request: Request, runs: Runs, branch: str | None = None
):
    runs.open_branch(conversation_id, branch)
    return workspace(request, runs, conversation_id)


@router.post("/conversations", response_class=HTMLResponse)
async def create_conversation(request: Request, runs: Runs):
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse("/", status_code=303)
    response = workspace(request, runs)
    response.headers["HX-Push-Url"] = "/"
    return response


@router.post("/conversations/{conversation_id}/delete", response_class=HTMLResponse)
async def delete_conversation(
    conversation_id: str,
    request: Request,
    runs: Runs,
    current_conversation_id: str = Form(""),
    current_project_id: str = Form(""),
):
    await runs.delete(conversation_id)
    current_id = current_conversation_id or None
    deleted_current = current_id is not None and runs.store.get(current_id) is None
    if deleted_current:
        current_id = None
    if request.headers.get("HX-Request") != "true":
        url = f"/conversations/{current_id}" if current_id else "/"
        return RedirectResponse(url, status_code=303)
    if deleted_current:
        response = workspace(request, runs)
        response.headers.update(
            {"HX-Retarget": "#workspace", "HX-Reswap": "outerHTML", "HX-Replace-Url": "/"}
        )
    else:
        response = HTMLResponse(
            render("sidebar.html", **context(runs, current_id, current_project_id))
        )
    response.headers["HX-Trigger"] = json.dumps({"conversationDeleted": conversation_id})
    return response


def started_turn(
    request: Request, runs: RunManager, run: Run, *, retry: bool, new_conversation: bool = False
):
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(f"/conversations/{run.conversation_id}", status_code=303)
    if retry or new_conversation:
        # Replace the blank workspace or any partially rendered failed attempt.
        response = workspace(request, runs, run.conversation_id)
        if new_conversation:
            response.headers["HX-Push-Url"] = f"/conversations/{run.conversation_id}"
        return response
    return HTMLResponse(
        render(
            "turn.html",
            **context(runs, run.conversation_id),
            retry=retry,
        )
    )


@router.post("/conversations/messages", response_class=HTMLResponse)
async def send_first_message(
    request: Request,
    runs: Runs,
    content: str = Form(""),
    council_enabled: bool = Form(False),
    project_id: str = Form(""),
):
    return started_turn(
        request,
        runs,
        runs.start(None, content, council_enabled=council_enabled, project_id=project_id or None),
        retry=False,
        new_conversation=True,
    )


@router.post("/conversations/{conversation_id}/messages", response_class=HTMLResponse)
async def send_message(
    conversation_id: str,
    request: Request,
    runs: Runs,
    content: str = Form(""),
    council_enabled: bool = Form(False),
):
    return started_turn(
        request,
        runs,
        runs.start(conversation_id, content, council_enabled=council_enabled),
        retry=False,
    )


@router.post("/conversations/{conversation_id}/retry", response_class=HTMLResponse)
async def retry_message(conversation_id: str, request: Request, runs: Runs):
    return started_turn(request, runs, runs.start(conversation_id), retry=True)


def stage_html(run: Run, event: dict) -> tuple[str, str]:
    stage, _, transition = event["type"].partition("_")
    if transition == "start":
        return stage, render("stages/loading.html", stage=stage, message_id=run.id)
    message = {**run.result, stage: event["data"]}
    metadata = event.get("metadata", message["metadata"])
    html = render(
        f"stages/{stage}.html",
        message=message,
        message_id=run.id,
        metadata=metadata,
    )
    if stage == "stage3":
        html += render(
            "response_header.html",
            message=message,
            message_id=run.id,
            council_enabled=run.council_enabled,
            oob=True,
        )
        html += response_footer(run)
    return stage, html


def response_footer(run: Run, requests: list[dict] | None = None) -> str:
    return render(
        "response_footer.html",
        message=run.result,
        message_id=run.id,
        requests=run.result["requests"] if requests is None else requests,
        oob=True,
    )


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str, request: Request, runs: Runs, after: int = 0):
    run = runs.runs.get(run_id)
    if run is None:
        # 204 tells EventSource to stop reconnecting to an expired run.
        return Response(status_code=204)
    try:
        cursor = int(request.headers.get("Last-Event-ID", after))
    except ValueError:
        cursor = 0

    async def events():
        async for item in run.listen(cursor):
            if item is None:
                yield ": keep-alive\n\n"
                continue
            sequence, event = item
            kind = event["type"]
            if kind.startswith("stage"):
                name, html = stage_html(run, event)
            elif kind == "usage_update":
                name = "controls"
                html = response_footer(run, event["data"])
            elif kind == "title_complete":
                name = "controls"
                html = render("sidebar.html", **context(runs, run.conversation_id), oob=True)
            else:
                name = "controls"
                state = context(runs, run.conversation_id)
                error = event.get("message")
                if error and not any(
                    message["id"] == run.message_id for message in state["messages"]
                ):
                    error = "A response on another branch failed. Switch back to retry it."
                state.update(error=error, setup_required=event.get("setup_required", False))
                html = render("finished.html", **state, run_id=run.id)
            yield encode_event(html, event=name, event_id=sequence)
        yield encode_event("", event="done")

    return event_response(events())
