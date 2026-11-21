"""Conversation persistence, compatible with the original JSON files."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from backend import branches
from backend.branches import Conversation
from backend.persistence import write_json

logger = logging.getLogger(__name__)


class ConversationStore:
    """Repository for a single-process local application.

    Read/modify/write operations contain no awaits, so tasks on the event loop
    cannot interleave them. Atomic replacement prevents partial JSON reads.
    """

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def _path(self, conversation_id: str) -> Path:
        return self.directory / f"{UUID(conversation_id)}.json"

    def get(self, conversation_id: str) -> Conversation | None:
        try:
            path = self._path(conversation_id)
        except (ValueError, AttributeError):
            return None
        try:
            conversation = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        conversation.setdefault("title", "New Conversation")
        return branches.prepare(conversation)

    @staticmethod
    def draft() -> Conversation:
        """Build an unsaved conversation; the first message makes it durable."""
        return branches.prepare(
            {
                "id": str(uuid4()),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "title": "New Conversation",
                "messages": [],
            }
        )

    def create(self) -> Conversation:
        conversation = self.draft()
        self.save(conversation)
        return conversation

    def delete(self, conversation_id: str) -> None:
        self._path(conversation_id).unlink(missing_ok=True)

    def save(self, conversation: Conversation) -> None:
        branches.prepare(conversation)
        # The flat history is a view of the selected path, not a second stored copy.
        write_json(
            self._path(conversation["id"]),
            {key: value for key, value in conversation.items() if key != "messages"},
        )

    def list(self) -> list[Conversation]:
        conversations = []
        for path in self.directory.glob("*.json"):
            try:
                conversation = self.get(path.stem)
                if conversation is not None:
                    conversations.append(
                        {
                            "id": conversation["id"],
                            "created_at": conversation["created_at"],
                            "title": conversation["title"],
                            "message_count": len(conversation["messages"]),
                            "project_id": conversation.get("project_id"),
                        }
                    )
            except (ValueError, KeyError, TypeError):
                logger.warning("Skipping invalid conversation file: %s", path.name)
        return sorted(conversations, key=lambda item: item["created_at"], reverse=True)

    def assign_project(self, conversation_id: str, project_id: str | None) -> None:
        conversation = self.get(conversation_id)
        if conversation is None:
            raise ValueError("Conversation not found")
        conversation["project_id"] = project_id
        self.save(conversation)

    def append_message(
        self,
        conversation_id: str,
        message: dict[str, Any],
        *,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        conversation = self.get(conversation_id)
        if conversation is None:
            raise ValueError("Conversation not found")
        if parent_id is None and conversation["messages"]:
            parent_id = conversation["messages"][-1]["id"]
        node = branches.add_message(conversation, message, parent_id)
        self.save(conversation)
        return node

    def update_title(self, conversation_id: str, title: str) -> None:
        conversation = self.get(conversation_id)
        if conversation is None:
            raise ValueError("Conversation not found")
        conversation["title"] = title
        self.save(conversation)

    def update_usage(self, conversation_id: str, message_id: str, requests: list[dict]) -> None:
        conversation = self.get(conversation_id)
        if conversation is None:
            raise ValueError("Conversation not found")
        conversation.setdefault("request_usage", {})[message_id] = requests
        self.save(conversation)

    def set_message_error(self, conversation_id: str, message_id: str, error: dict) -> None:
        conversation = self.get(conversation_id)
        if conversation is None:
            raise ValueError("Conversation not found")
        conversation["message_nodes"][message_id]["last_error"] = error
        self.save(conversation)
