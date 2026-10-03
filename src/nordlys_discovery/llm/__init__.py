"""LLM providers behind one interface. See base.py."""

from __future__ import annotations

from ..config import Settings
from .base import (
    ChatResult,
    LLMError,
    LLMProvider,
    LLMUnavailable,
    Message,
    ToolCall,
    ToolSpec,
    Usage,
    structured,
)

__all__ = [
    "ChatResult",
    "LLMError",
    "LLMProvider",
    "LLMUnavailable",
    "Message",
    "ToolCall",
    "ToolSpec",
    "Usage",
    "create_llm",
    "structured",
]


def create_llm(settings: Settings) -> LLMProvider:
    """Build the configured provider. Raises LLMUnavailable when none is configured."""
    from .providers import AnthropicProvider, OpenAICompatible

    match settings.llm_provider:
        case "azure-openai":
            if not settings.azure_openai_endpoint:
                raise LLMUnavailable("NORDLYS_AZURE_OPENAI_ENDPOINT is not set")
            key = settings.azure_openai_api_key.get_secret_value() if settings.azure_openai_api_key else None
            return OpenAICompatible.azure(
                endpoint=settings.azure_openai_endpoint,
                deployment=settings.azure_openai_chat_deployment,
                api_version=settings.azure_openai_api_version,
                api_key=key,  # None -> managed identity
                timeout_s=settings.llm_timeout_s,
            )
        case "anthropic":
            workspace = settings.anthropic_aws_workspace_id
            if workspace and not settings.aws_region:
                raise LLMUnavailable("ANTHROPIC_AWS_WORKSPACE_ID is set, so AWS_REGION is required too")
            if settings.anthropic_api_key is None and not workspace:
                raise LLMUnavailable("ANTHROPIC_API_KEY / NORDLYS_ANTHROPIC_API_KEY is not set")
            anthropic_key = settings.anthropic_api_key
            return AnthropicProvider(
                api_key=anthropic_key.get_secret_value() if anthropic_key else None,
                aws_workspace_id=workspace,
                aws_region=settings.aws_region,
                model=settings.anthropic_model,
                effort=settings.anthropic_effort,
                fallbacks=settings.anthropic_fallbacks,
                timeout_s=settings.llm_timeout_s,
            )
        case "openai":
            if settings.openai_api_key is None:
                raise LLMUnavailable("NORDLYS_OPENAI_API_KEY is not set")
            return OpenAICompatible.openai(
                api_key=settings.openai_api_key.get_secret_value(),
                model=settings.openai_model,
                timeout_s=settings.llm_timeout_s,
            )
        case _:
            raise LLMUnavailable("no LLM provider configured (NORDLYS_LLM_PROVIDER=none)")
