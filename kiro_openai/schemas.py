from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field

# OpenAI allows content to be a plain string or a list of typed parts.
ContentPart = Dict[str, Any]
MessageContent = Union[str, List[ContentPart], None]


class ChatMessage(BaseModel):
    role: str
    content: MessageContent = None
    name: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = None
    messages: List[ChatMessage]
    stream: bool = False

    # Accepted for client compatibility. The Kiro CLI exposes no knobs for
    # these, so they are ignored rather than silently misapplied.
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    n: Optional[int] = None
    stop: Optional[Union[str, List[str]]] = None
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    user: Optional[str] = None

    # Mapped onto the CLI's --effort flag.
    reasoning_effort: Optional[str] = None


class Model(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "kiro"


class ModelList(BaseModel):
    object: str = "list"
    data: List[Model] = Field(default_factory=list)


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
