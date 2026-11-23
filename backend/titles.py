"""Short conversation names, with a local fallback before generation completes."""

DEFAULT_TITLE = "New Conversation"


def short_title(text: str) -> str:
    title = " ".join(text.split()).strip("\"'").strip()
    return title[:47] + "..." if len(title) > 50 else title


def conversation_title(title: str | None, messages: list[dict]) -> str:
    if isinstance(title, str) and title.strip() and title != DEFAULT_TITLE:
        return title
    question = next((message["content"] for message in messages if message["role"] == "user"), "")
    return short_title(question) or DEFAULT_TITLE
