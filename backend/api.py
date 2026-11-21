"""JSON endpoints using the same runs as the HTML UI."""

import asyncio
import json

from fastapi import APIRouter
from pydantic import BaseModel

from backend.dependencies import Runs
from backend.events import encode_event, event_response
from backend.runs import Run, RunError

router = APIRouter(prefix="/api/conversations")


class SendMessageRequest(BaseModel):
    content: str
    council_enabled: bool = True


async def completed_result(run: Run):
    try:
        await asyncio.shield(run.task)
    except asyncio.CancelledError:
        if run.deleted:
            raise RunError("Conversation deleted.", 404) from None
        raise
    if run.error:
        raise RunError(run.error, 502)
    return run.result


def stream_result(run: Run):
    async def events():
        async for item in run.listen():
            if item is None:
                yield ": keep-alive\n\n"
            else:
                sequence, event = item
                yield encode_event(json.dumps(event), event_id=sequence)

    return event_response(events())


@router.get("")
async def list_conversations(runs: Runs):
    return runs.store.list()


@router.post("")
async def create_conversation(runs: Runs):
    return runs.store.create()


@router.get("/{conversation_id}")
async def get_conversation(conversation_id: str, runs: Runs):
    return runs.conversation(conversation_id)


@router.post("/{conversation_id}/message")
async def send_message(conversation_id: str, request: SendMessageRequest, runs: Runs):
    return await completed_result(
        runs.start(conversation_id, request.content, council_enabled=request.council_enabled)
    )


@router.post("/{conversation_id}/retry")
async def retry_message(conversation_id: str, runs: Runs):
    return await completed_result(runs.start(conversation_id))


@router.post("/{conversation_id}/message/stream")
async def send_message_stream(conversation_id: str, request: SendMessageRequest, runs: Runs):
    return stream_result(
        runs.start(conversation_id, request.content, council_enabled=request.council_enabled)
    )


@router.post("/{conversation_id}/retry/stream")
async def retry_message_stream(conversation_id: str, runs: Runs):
    return stream_result(runs.start(conversation_id))
