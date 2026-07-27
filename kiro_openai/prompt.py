from __future__ import annotations

from typing import List

from .schemas import ChatMessage, MessageContent

_ROLE_LABELS = {
    "user": "User",
    "assistant": "Assistant",
    "tool": "Tool result",
    "function": "Tool result",
}


def flatten_content(content: MessageContent) -> str:
    """Collapse OpenAI's string-or-parts content into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content

    chunks: List[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            chunks.append(str(part.get("text", "")))
        elif kind in ("image_url", "input_image"):
            # The headless CLI takes text only; note the omission instead of
            # dropping it silently, so the model knows something was elided.
            chunks.append("[image omitted by kiro-openai-bridge]")
    return "\n".join(c for c in chunks if c)


def build_prompt(messages: List[ChatMessage]) -> str:
    """Render an OpenAI message array as a single headless-CLI prompt.

    OpenAI's chat API is stateless: the full history arrives on every call. The
    CLI's --no-interactive mode is also stateless, so the mapping is a faithful
    transcript rather than an incremental turn.
    """
    system_parts: List[str] = []
    turns: List[str] = []

    for message in messages:
        text = flatten_content(message.content).strip()
        if not text:
            continue
        role = (message.role or "user").lower()
        if role in ("system", "developer"):
            system_parts.append(text)
        else:
            label = _ROLE_LABELS.get(role, role.capitalize())
            turns.append("{0}: {1}".format(label, text))

    sections: List[str] = []
    if system_parts:
        sections.append("<instructions>\n{0}\n</instructions>".format("\n\n".join(system_parts)))

    if len(turns) <= 1 and not system_parts:
        # Single user turn with no system prompt: send it verbatim so simple
        # requests are not wrapped in transcript scaffolding.
        return turns[0].split(": ", 1)[-1] if turns else ""

    if len(turns) == 1:
        sections.append(turns[0].split(": ", 1)[-1])
    else:
        sections.append(
            "<conversation>\n{0}\n</conversation>\n\n"
            "Continue the conversation. Reply only as the assistant's next message, "
            "with no role label or preamble.".format("\n\n".join(turns))
        )

    return "\n\n".join(sections)


def estimate_tokens(text: str) -> int:
    """Rough token estimate. The CLI reports no usage, so this is advisory."""
    if not text:
        return 0
    return max(1, len(text) // 4)
