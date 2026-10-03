"""Concrete LLM providers: Azure OpenAI (default in Azure), OpenAI, Anthropic.

All calls have a timeout and bounded retries with exponential backoff (the SDKs retry
429/5xx/connection errors and honour Retry-After). Azure OpenAI can authenticate with a
managed identity (Entra ID token) instead of an API key.
"""

from __future__ import annotations

import json
from typing import Any

from .base import ChatResult, LLMError, Message, ToolCall, ToolSpec, Usage, dumps


class OpenAICompatible:
    """OpenAI chat completions API; also used for Azure OpenAI."""

    def __init__(self, client: Any, model: str, provider: str, *, temperature: float | None = 0.0) -> None:
        self._client = client
        self._model = model
        self._provider = provider
        # 0 for repeatable evals; None for reasoning deployments that reject the parameter.
        self._temperature = temperature

    @classmethod
    def azure(
        cls,
        *,
        endpoint: str,
        deployment: str,
        api_version: str,
        api_key: str | None,
        timeout_s: float = 60.0,
        max_retries: int = 3,
    ) -> OpenAICompatible:
        from openai import AsyncAzureOpenAI

        if api_key:
            client = AsyncAzureOpenAI(
                azure_endpoint=endpoint,
                api_key=api_key,
                api_version=api_version,
                timeout=timeout_s,
                max_retries=max_retries,
            )
        else:
            # Managed identity / workload identity / az login: no secret to store or rotate.
            from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider

            provider = get_bearer_token_provider(
                DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
            )
            client = AsyncAzureOpenAI(
                azure_endpoint=endpoint,
                azure_ad_token_provider=provider,
                api_version=api_version,
                timeout=timeout_s,
                max_retries=max_retries,
            )
        return cls(client, deployment, "azure-openai")

    @classmethod
    def openai(cls, *, api_key: str, model: str, timeout_s: float = 60.0, max_retries: int = 3) -> OpenAICompatible:
        from openai import AsyncOpenAI

        return cls(AsyncOpenAI(api_key=api_key, timeout=timeout_s, max_retries=max_retries), model, "openai")

    @property
    def model_id(self) -> str:
        return f"{self._provider}:{self._model}"

    @staticmethod
    def _messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if m.role == "tool":
                out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
            elif m.role == "assistant" and m.tool_calls:
                out.append(
                    {
                        "role": "assistant",
                        "content": m.content or None,
                        "tool_calls": [
                            {
                                "id": c.id,
                                "type": "function",
                                "function": {"name": c.name, "arguments": dumps(c.arguments)},
                            }
                            for c in m.tool_calls
                        ],
                    }
                )
            else:
                out.append({"role": m.role, "content": m.content})
        return out

    async def chat(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        force_tool: str | None = None,
        max_tokens: int = 1024,
    ) -> ChatResult:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": self._messages(system, messages),
            "max_completion_tokens": max_tokens,
        }
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in tools
            ]
            if force_tool:
                kwargs["tool_choice"] = {"type": "function", "function": {"name": force_tool}}
        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # SDK already retried transient errors
            raise LLMError(f"{self.model_id}: {type(exc).__name__}: {exc}") from exc
        choice = resp.choices[0]
        calls = []
        for c in choice.message.tool_calls or []:
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_unparseable": c.function.arguments}
            calls.append(ToolCall(c.id, c.function.name, args))
        usage = Usage(getattr(resp.usage, "prompt_tokens", 0) or 0, getattr(resp.usage, "completion_tokens", 0) or 0)
        stop = "refusal" if getattr(choice.message, "refusal", None) else str(choice.finish_reason)
        return ChatResult(choice.message.content or "", calls, usage, self.model_id, stop)


class AnthropicProvider:
    """Claude through the Messages API, following the rules of current models:

    * no sampling parameters (temperature is rejected) and adaptive thinking, with the
      effort level set explicitly;
    * no forced tool_choice (rejected while thinking): `force_tool` becomes an instruction
      and the caller checks the reply;
    * assistant turns are replayed with their original content blocks (`Message.raw`),
      because thinking blocks must come back unchanged in a tool loop;
    * server-side refusal fallbacks are enabled (`fallbacks="default"`): if the requested
      model declines, Anthropic retries on a fallback model, and the result records the
      model that actually answered.
    """

    BETAS = ("server-side-fallback-2026-07-01",)
    # Thinking shares max_tokens with the visible answer; this headroom keeps the caller's
    # budget for the answer itself.
    THINKING_HEADROOM = 8192

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        effort: str = "medium",
        fallbacks: bool = True,
        timeout_s: float = 60.0,
        max_retries: int = 3,
        aws_workspace_id: str | None = None,
        aws_region: str | None = None,
    ) -> None:
        """With `aws_workspace_id` the client targets Claude Platform on AWS (same API,
        routed to that workspace in `aws_region`); otherwise the Claude API."""
        if aws_workspace_id:
            from anthropic import AsyncAnthropicAWS

            self._client: Any = AsyncAnthropicAWS(
                api_key=api_key,  # AWS short-term API key; None -> SigV4 from the AWS credential chain
                workspace_id=aws_workspace_id,
                aws_region=aws_region,
                timeout=timeout_s,
                max_retries=max_retries,
            )
            self._provider = "anthropic-aws"
        else:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=api_key, timeout=timeout_s, max_retries=max_retries)
            self._provider = "anthropic"
        self._model = model
        self._effort = effort
        self._fallbacks = fallbacks

    @property
    def model_id(self) -> str:
        return f"{self._provider}:{self._model}"

    @staticmethod
    def _messages(messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "tool":
                block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
                # Consecutive tool results belong in one user turn.
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            elif m.role == "assistant":
                if m.raw is not None:
                    out.append({"role": "assistant", "content": m.raw})  # unchanged, thinking included
                    continue
                blocks: list[dict[str, Any]] = [{"type": "text", "text": m.content}] if m.content else []
                blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in m.tool_calls]
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
            else:
                out.append({"role": "user", "content": m.content})
        return out

    def _request(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        force_tool: str | None,
        max_tokens: int,
    ) -> dict[str, Any]:
        if force_tool:
            system += f"\n\nRespond only by calling the `{force_tool}` tool."
        kwargs: dict[str, Any] = {
            "model": self._model,
            "system": system,
            "messages": self._messages(messages),
            "max_tokens": max_tokens + self.THINKING_HEADROOM,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self._effort},
        }
        if self._fallbacks:
            kwargs["betas"] = list(self.BETAS)
            kwargs["fallbacks"] = "default"
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
            ]
        return kwargs

    @staticmethod
    def served_model(resp: Any) -> str:
        """The model that produced the returned content (a fallback model, if one served it)."""
        served = str(resp.model)
        for it in getattr(resp.usage, "iterations", None) or []:
            if getattr(it, "type", "") == "fallback_message":
                served = str(it.model)
        return served

    async def chat(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        force_tool: str | None = None,
        max_tokens: int = 1024,
    ) -> ChatResult:
        kwargs = self._request(system, messages, tools, force_tool, max_tokens)
        try:
            if self._fallbacks:
                resp = await self._client.beta.messages.create(**kwargs)
            else:
                resp = await self._client.messages.create(**kwargs)
        except Exception as exc:
            raise LLMError(f"{self.model_id}: {type(exc).__name__}: {exc}") from exc
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in resp.content if getattr(b, "type", "") == "tool_use"]
        usage = Usage(resp.usage.input_tokens or 0, resp.usage.output_tokens or 0)
        raw = [b.model_dump(mode="json", by_alias=True, exclude_none=True) for b in resp.content]
        return ChatResult(text, calls, usage, f"anthropic:{self.served_model(resp)}", str(resp.stop_reason), raw=raw)
