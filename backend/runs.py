"""Own run lifetimes and persistence, independently of HTTP connections."""

import asyncio
import logging
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from backend import branches, council
from backend.config import CouncilConfig, Settings, use_settings
from backend.council_settings import CouncilSettingsStore
from backend.credentials import CredentialsStore
from backend.failures import ProviderError
from backend.projects import ProjectStore
from backend.storage import Conversation, ConversationStore
from backend.usage import track_requests

logger = logging.getLogger(__name__)


class RunError(Exception):
    def __init__(self, message: str, status_code: int = 400, *, setup_required: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.setup_required = setup_required


@dataclass
class Run:
    conversation_id: str
    query: str
    history: list[dict[str, Any]]
    configuration: CouncilConfig
    message_id: str
    council_enabled: bool = True
    id: str = field(default_factory=lambda: str(uuid4()))
    events: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] = field(
        default_factory=lambda: {
            "stage1": [],
            "stage2": [],
            "stage3": None,
            "metadata": {},
            "requests": [],
        }
    )
    loading_stage: str | None = None
    error: str | None = None
    error_setup_required: bool = False
    done: bool = False
    deleted: bool = False
    saved: bool = False
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None

    def publish(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        self.changed.set()

    async def listen(self, after: int = 0) -> AsyncIterator[tuple[int, dict] | None]:
        """Replay missed events, then wait for changes. None is a heartbeat."""
        cursor = max(0, min(after, len(self.events)))
        while not self.deleted:
            while cursor < len(self.events) and not self.deleted:
                event = self.events[cursor]
                cursor += 1
                yield cursor, event
            if self.done:
                return
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=15)
            except asyncio.TimeoutError:
                yield None


class RunManager:
    """One active turn per conversation, with bounded completed-event retention."""

    def __init__(
        self,
        store: ConversationStore,
        settings: CouncilSettingsStore,
        projects: ProjectStore,
        credentials: CredentialsStore,
    ):
        self.store = store
        self.settings = settings
        self.projects = projects
        self.credentials = credentials
        self.runs: dict[str, Run] = {}
        self.active: dict[str, Run] = {}

    def conversation(self, conversation_id: str) -> Conversation:
        conversation = self.store.get(conversation_id)
        if conversation is None:
            raise RunError("Conversation not found", 404)
        return conversation

    def project(self, project_id: str) -> dict:
        project = self.projects.get(project_id)
        if project is None:
            raise RunError("Project not found", 404)
        return project

    def assign_project(self, conversation_id: str, project_id: str | None) -> None:
        conversation = self.conversation(conversation_id)
        project = self.project(project_id) if project_id else None
        self.store.assign_project(conversation["id"], project["id"] if project else None)

    async def delete(self, conversation_id: str) -> None:
        """Remove saved history and stop its work before returning to the caller."""
        conversation_id = self.conversation(conversation_id)["id"]
        try:
            self.store.delete(conversation_id)
        except OSError as error:
            raise RunError("The conversation could not be deleted. Please retry.", 500) from error
        tasks = []
        for run in list(self.runs.values()):
            if run.conversation_id != conversation_id:
                continue
            run.deleted = True
            run.done = True
            run.changed.set()
            del self.runs[run.id]
            if run.task is not None and not run.task.done():
                run.task.cancel()
                tasks.append(run.task)
        self.active.pop(conversation_id, None)
        await asyncio.gather(*tasks, return_exceptions=True)

    def start(
        self,
        conversation_id: str | None,
        content: str | None = None,
        *,
        council_enabled: bool = True,
        edit_message_id: str | None = None,
        project_id: str | None = None,
    ) -> Run:
        """Start a turn, saving a new conversation only with its first valid message."""
        conversation = (
            self.conversation(conversation_id)
            if conversation_id is not None
            else self.store.draft()
        )
        if conversation_id is None and project_id:
            conversation["project_id"] = self.project(project_id)["id"]
        conversation_id = conversation["id"]
        if conversation_id in self.active:
            raise RunError("This conversation already has a response in progress.", 409)

        messages = conversation["messages"]
        try:
            configuration = self.settings.get()
        except ValidationError as error:
            issues = error.errors(include_input=False)
            empty_members = any(
                issue["loc"] == ("members",) and issue["type"] == "too_short" for issue in issues
            )
            message = (
                "Configure at least one council model in Setup before sending a message."
                if empty_members
                else "Model settings are missing or invalid. Open Setup to review the council, chairman, and title model."
            )
            raise RunError(message, 503, setup_required=True) from None
        except (ValueError, OSError) as error:
            raise RunError(
                "Council settings could not be loaded. Open Setup to repair them.",
                503,
                setup_required=True,
            ) from error
        try:
            provider_settings = self.credentials.get()
        except (ValueError, OSError) as error:
            raise RunError(
                "API key settings could not be loaded. Open Setup to replace the key.",
                503,
                setup_required=True,
            ) from error
        key = provider_settings.openrouter_api_key
        if key is None or not key.get_secret_value().strip():
            raise RunError(str(ProviderError("missing_key")), setup_required=True)
        pending = bool(messages and messages[-1]["role"] == "user")
        if edit_message_id is not None:
            original = self.user_message(conversation, edit_message_id)
            if content is None or not content.strip():
                raise RunError("Please enter a question.")
            history = messages[: messages.index(original)]
            query = content
            council_enabled = original.get("council_enabled", True)
            question = branches.add_message(
                conversation,
                {"role": "user", "content": query, "council_enabled": council_enabled},
                original["parent_id"],
            )
        elif content is None:
            if not pending:
                raise RunError("No unanswered message to retry.")
            query, history = messages[-1]["content"], messages[:-1]
            council_enabled = messages[-1].get("council_enabled", True)
            question = messages[-1]
        else:
            if not content.strip():
                raise RunError("Please enter a question.")
            if pending:
                raise RunError("Retry the unanswered message before sending a follow-up.", 409)
            query, history = content, messages
            question = branches.add_message(
                conversation,
                {"role": "user", "content": query, "council_enabled": council_enabled},
                messages[-1]["id"] if messages else None,
            )

        question.pop("last_error", None)
        try:
            self.store.save(conversation)
        except OSError:
            raise RunError(
                "Your question could not be saved. Check storage permissions and free space, then try again.",
                503,
            ) from None

        completed = [key for key, run in self.runs.items() if run.done]
        for key in completed[:-99]:
            del self.runs[key]

        run = Run(
            conversation_id,
            query,
            history,
            configuration,
            message_id=question["id"],
            council_enabled=council_enabled,
        )
        run.result["requests"] = conversation.get("request_usage", {}).get(question["id"], [])
        self.runs[run.id] = run
        self.active[conversation_id] = run
        run.task = asyncio.create_task(
            self._execute(run, provider_settings), name=f"council-{run.id}"
        )
        return run

    @staticmethod
    def user_message(conversation: Conversation, message_id: str) -> dict[str, Any]:
        message = conversation["message_nodes"].get(message_id)
        if message is None or message["role"] != "user":
            raise RunError("User message not found", 404)
        if message not in conversation["messages"]:
            raise RunError("This branch has changed. Reopen the message before editing it.", 409)
        return message

    def select_branch(self, conversation_id: str, message_id: str, target_id: str) -> None:
        conversation = self.conversation(conversation_id)
        message = self.user_message(conversation, message_id)
        target = conversation["message_nodes"].get(target_id)
        if (
            target is None
            or target["role"] != "user"
            or target["parent_id"] != message["parent_id"]
        ):
            raise RunError("Message branch not found", 404)
        branches.select_branch(conversation, target_id)
        self.store.save(conversation)

    def open_branch(self, conversation_id: str, message_id: str | None = None) -> None:
        conversation = self.conversation(conversation_id)
        if message_id is not None:
            message = conversation["message_nodes"].get(message_id)
            if message is None or message["role"] != "user":
                raise RunError("Message branch not found", 404)
        previous = conversation["selected_children"].copy()
        branches.select_recent(conversation, message_id)
        if conversation["selected_children"] != previous:
            self.store.save(conversation)

    async def _title(self, run: Run) -> None:
        try:
            title = await council.generate_conversation_title(
                run.query, run.configuration.title_model
            )
            if run.deleted or not title:
                return
            self.store.update_title(run.conversation_id, title)
            run.publish({"type": "title_complete", "data": {"title": title}})
        except Exception as error:
            # A failed title must never discard a successful council answer.
            logger.error("Could not generate a conversation title (%s)", type(error).__name__)

    def _usage_changed(self, run: Run) -> None:
        if run.deleted:
            return
        self.store.update_usage(run.conversation_id, run.message_id, run.result["requests"])
        run.publish({"type": "usage_update", "data": deepcopy(run.result["requests"])})

    async def _execute(self, run: Run, settings: Settings) -> None:
        with (
            use_settings(settings),
            track_requests(run.result["requests"], lambda: self._usage_changed(run), run.id),
        ):
            await self._execute_turn(run)

    async def _execute_turn(self, run: Run) -> None:
        title_task = asyncio.create_task(self._title(run)) if not run.history else None
        try:
            async for event in council.stream_turn(
                run.query,
                run.history,
                configuration=run.configuration,
                council_enabled=run.council_enabled,
            ):
                if run.deleted:
                    return
                stage, _, transition = event["type"].partition("_")
                if transition == "start":
                    run.loading_stage = stage
                elif transition == "complete":
                    run.result[stage] = event["data"]
                    run.loading_stage = None
                    if "metadata" in event:
                        run.result["metadata"] = event["metadata"]
                    if stage == "stage3":
                        self.store.append_message(
                            run.conversation_id,
                            {
                                "role": "assistant",
                                **{
                                    key: value
                                    for key, value in run.result.items()
                                    if key != "requests"
                                },
                            },
                            parent_id=run.message_id,
                        )
                        run.saved = True
                run.publish(event)
            if title_task is not None:
                await title_task
        except asyncio.CancelledError:
            run.error = (
                "Conversation deleted."
                if run.deleted
                else "The server stopped before the response finished. Please retry."
            )
            raise
        except Exception as error:
            logger.error("Council run %s failed (%s)", run.id, type(error).__name__)
            if isinstance(error, (council.CouncilError, ProviderError)):
                run.error = str(error)
                run.error_setup_required = getattr(error, "setup_required", False)
            elif isinstance(error, OSError):
                run.error = "The response could not be saved. Check storage permissions and free space, then retry."
            else:
                run.error = "The response could not be completed. Please retry."
        finally:
            if title_task is not None and not title_task.done():
                title_task.cancel()
                await asyncio.gather(title_task, return_exceptions=True)
            run.loading_stage = None
            run.done = True
            self.active.pop(run.conversation_id, None)
            if run.error and not run.deleted:
                try:
                    self.store.set_message_error(
                        run.conversation_id,
                        run.message_id,
                        {
                            "message": run.error,
                            "setup_required": run.error_setup_required,
                        },
                    )
                except (OSError, ValueError):
                    logger.error("Could not save failure details for run %s", run.id)
            # Subscribers render controls from current state; announce completion only
            # after cleanup releases the conversation for edits and retries.
            if not run.deleted:
                run.publish(
                    {
                        "type": "error",
                        "message": run.error,
                        "setup_required": run.error_setup_required,
                    }
                    if run.error
                    else {"type": "complete"}
                )

    async def close(self) -> None:
        active = list(self.active.values())
        tasks = [run.task for run in active if run.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for run in active:
            if not run.done:
                # A task cancelled before its first instruction cannot run finally.
                run.error = "The server stopped before the response started. Please retry."
                run.publish({"type": "error", "message": run.error})
                run.done = True
        self.active.clear()
