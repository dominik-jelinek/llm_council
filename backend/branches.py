"""Message trees and branch selection, independent of persistence and HTTP."""

from typing import Any
from uuid import UUID, uuid4, uuid5

Conversation = dict[str, Any]


def prepare(conversation: Conversation) -> Conversation:
    """Read legacy linear histories without writing files during a page request."""
    if "message_nodes" not in conversation:
        conversation["message_nodes"] = {}
        conversation["selected_children"] = {}
        parent_id = None
        for index, message in enumerate(conversation.get("messages", [])):
            # Stable IDs also make an edit form opened before migration valid afterward.
            message_id = str(uuid5(UUID(conversation["id"]), f"message-{index}"))
            node = {**message, "id": message_id, "parent_id": parent_id}
            conversation["message_nodes"][message_id] = node
            conversation["selected_children"][parent_id or ""] = message_id
            parent_id = message_id
    conversation["messages"] = selected_messages(conversation)
    return conversation


def selected_messages(conversation: Conversation) -> list[dict[str, Any]]:
    messages = []
    visited = set()
    parent_id = None
    while message_id := conversation["selected_children"].get(parent_id or ""):
        if message_id in visited:
            raise ValueError("Conversation contains a message cycle")
        message = conversation["message_nodes"][message_id]
        if message["parent_id"] != parent_id:
            raise ValueError("Conversation contains an invalid branch")
        visited.add(message_id)
        messages.append(message)
        parent_id = message_id
    return messages


def add_message(
    conversation: Conversation, message: dict[str, Any], parent_id: str | None
) -> dict[str, Any]:
    if parent_id is not None and parent_id not in conversation["message_nodes"]:
        raise ValueError("Parent message not found")
    node = {**message, "id": str(uuid4()), "parent_id": parent_id}
    conversation["message_nodes"][node["id"]] = node
    conversation["selected_children"][parent_id or ""] = node["id"]
    conversation["messages"] = selected_messages(conversation)
    return node


def select_branch(conversation: Conversation, message_id: str) -> None:
    select_recent(conversation, message_id)


def select_recent(conversation: Conversation, message_id: str | None = None) -> None:
    """Choose the newest continuation, optionally beneath an explicitly chosen message.

    Nodes retain creation order in JSON. Propagating each newest descendant toward
    the root also recognizes a new follow-up made on an older branch.
    """
    nodes = conversation["message_nodes"]
    recent = {}
    for node in reversed(list(nodes.values())):
        child_id, parent_id = node["id"], node["parent_id"]
        while (parent_id or "") not in recent:
            recent[parent_id or ""] = child_id
            if parent_id is None:
                break
            child_id, parent_id = parent_id, nodes[parent_id]["parent_id"]
    selections = conversation["selected_children"]
    # A deep link must also restore the ancestors leading to the chosen message.
    ancestor_id = message_id
    while ancestor_id:
        parent_id = nodes[ancestor_id]["parent_id"]
        selections[parent_id or ""] = ancestor_id
        ancestor_id = parent_id
    parent_id = message_id
    while child_id := recent.get(parent_id or ""):
        selections[parent_id or ""] = child_id
        parent_id = child_id
    conversation["messages"] = selected_messages(conversation)


def navigation(conversation: Conversation) -> dict[str, dict[str, Any]]:
    siblings: dict[str | None, list[str]] = {}
    for message in conversation["message_nodes"].values():
        if message["role"] == "user":
            siblings.setdefault(message["parent_id"], []).append(message["id"])
    result = {}
    for message in conversation["messages"]:
        if message["role"] != "user":
            continue
        versions = siblings[message["parent_id"]]
        index = versions.index(message["id"])
        result[message["id"]] = {
            "index": index + 1,
            "count": len(versions),
            "previous_id": versions[index - 1] if index else None,
            "next_id": versions[index + 1] if index + 1 < len(versions) else None,
        }
    return result
