"""Provider-neutral LLM interface: chat with tool calling, plus structured JSON output.

One small abstraction serves both the discovery agent (multi-turn tool use) and
single-shot structured tasks such as classifying an API registration. Every response
says exactly which provider and model produced it and how many tokens it used, so that
every measured result can name its source.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError


class LLMError(RuntimeError):
    """The model call failed after retries, or returned something unusable."""


class LLMUnavailable(LLMError):
    """No LLM provider is configured (NORDLYS_LLM_PROVIDER=none)."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    role: Literal["user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)  # assistant only
    tool_call_id: str | None = None  # tool only
    name: str | None = None  # tool only


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens)


@dataclass(frozen=True)
class ChatResult:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    model: str  # "<provider>:<model or deployment>", recorded with every result
    stop_reason: str


class LLMProvider(Protocol):
    @property
    def model_id(self) -> str: ...

    async def chat(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        force_tool: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> ChatResult: ...


async def structured[M: BaseModel](
    llm: LLMProvider, system: str, user: str, schema: type[M], *, max_tokens: int = 1024
) -> tuple[M, ChatResult]:
    """Get an instance of `schema` from the model by forcing a single tool call.

    Tool calling is the one structured-output mechanism all three providers support, so
    this works the same on Azure OpenAI, OpenAI and Anthropic. Output is validated with
    Pydantic; one repair attempt is made if validation fails.
    """
    tool = ToolSpec(name="submit", description=f"Submit the {schema.__name__}.", parameters=schema.model_json_schema())
    messages = [Message("user", user)]
    for attempt in range(2):
        result = await llm.chat(system, messages, [tool], force_tool="submit", max_tokens=max_tokens)
        call = next((c for c in result.tool_calls if c.name == "submit"), None)
        if call is None:
            raise LLMError(f"{llm.model_id} did not call the submit tool")
        try:
            return schema.model_validate(call.arguments), result
        except ValidationError as exc:
            if attempt == 1:
                raise LLMError(f"{llm.model_id} returned invalid {schema.__name__}: {exc}") from exc
            messages += [
                Message("assistant", "", [call]),
                Message(
                    "tool",
                    f"Invalid: {exc}. Call submit again with valid arguments.",
                    tool_call_id=call.id,
                    name="submit",
                ),
            ]
    raise LLMError("unreachable")  # pragma: no cover


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
