"""Request guard for the MCP server: who is calling, may they use this tool, and the audit trail.

Runs as SDK middleware around every MCP request, so it also sees calls that fail schema
validation. For `tools/call` it:

1. resolves the caller (validated JWT claims; or the configured dev identity when auth
   is off),
2. enforces the tool-scope policy (security/policy.py): deny by default,
3. applies a per-caller rate limit,
4. writes one audit event per call: who, which tool, which resource, decision, timestamp.

The audit write is fail-closed for request_access, which changes state: if the event
cannot be recorded, the call is refused. Read-only tools fail open (logged), so an audit
outage does not take discovery down. That is a deliberate availability/compliance
trade-off, documented in the README.
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token

from ..security.jwt import principal_from_access_token
from ..security.policy import TOOL_SCOPES, asset_scopes, tool_decision
from ..security.ratelimit import RateLimiter

log = logging.getLogger(__name__)
STATE_CHANGING_TOOLS = {"request_access"}


@dataclass(frozen=True)
class Caller:
    subject: str  # the name recorded in audit and access requests (preferred_username)
    asset_scopes: frozenset[str] = field(default_factory=frozenset)
    tool_scopes: frozenset[str] = field(default_factory=lambda: frozenset(TOOL_SCOPES.values()))
    client_id: str = "local-dev"

    @property
    def all_scopes(self) -> frozenset[str]:
        return self.asset_scopes | self.tool_scopes


current_caller: contextvars.ContextVar[Caller | None] = contextvars.ContextVar("current_caller", default=None)
AuditSink = Callable[[dict[str, Any]], Awaitable[None]]


def caller_from_token(fallback: Caller | None) -> Caller | None:
    token = get_access_token()
    if token is None:
        return fallback
    p = principal_from_access_token(token)
    return Caller(
        subject=p.username,
        asset_scopes=asset_scopes(p.scopes),
        tool_scopes=frozenset(s for s in p.scopes if s in set(TOOL_SCOPES.values())),
        client_id=p.client_id,
    )


def _resource(tool: str, args: dict[str, Any]) -> str | None:
    if "asset_id" in args:
        version = f":v{args['version']}" if args.get("version") else ""
        return f"{args.get('asset_type', 'api')}:{args['asset_id']}{version}"
    if "api_id" in args:
        return f"api:{args['api_id']}" + (f":v{args['version']}" if args.get("version") else "")
    if "product_id" in args:
        return f"data_product:{args['product_id']}"
    if "query" in args:
        return f"search:{str(args['query'])[:200]}"
    return None


def _error_result(code: str, message: str) -> dict[str, Any]:
    """A tools/call result in wire format (middleware sits below model serialisation)."""
    payload = {"error": {"code": code, "message": message, "retryable": code == "rate_limited"}}
    return {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": True, "resultType": "complete"}


def _outcome(result: Any) -> tuple[str, str | None]:
    """Map a tools/call result (wire-format dict, or a result model) to an audit decision."""
    if isinstance(result, dict):
        is_error = bool(result.get("isError"))
        content = result.get("content") or []
        text = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    else:
        is_error = bool(getattr(result, "is_error", False))
        text = " ".join(getattr(c, "text", "") for c in getattr(result, "content", []) or [])
    if not is_error:
        return "allowed", None
    if "validation error" in text:
        return "invalid", text[:300]
    for code, decision in (("rule_violation", "denied"), ("forbidden", "denied"), ("not_found", "allowed")):
        if f'"code": "{code}"' in text:
            return decision, text[:300]
    return "error", text[:300]


class Guard:
    def __init__(
        self,
        *,
        audit: AuditSink,
        limiter: RateLimiter,
        dev_caller: Caller | None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.audit = audit
        self.limiter = limiter
        self.dev_caller = dev_caller  # None when auth is enabled: no token, no access
        self.clock = clock

    async def __call__(self, ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        if getattr(ctx, "method", None) != "tools/call":
            return await call_next(ctx)

        params = ctx.params if isinstance(ctx.params, dict) else {}
        tool = str(params.get("name", ""))
        args = params.get("arguments") or {}
        args = args if isinstance(args, dict) else {}
        caller = caller_from_token(self.dev_caller)
        request_id = str(getattr(ctx, "request_id", "") or "")
        started = self.clock()

        async def record(decision: str, reason: str | None) -> None:
            event = {
                "actor": caller.subject if caller else "anonymous",
                "action": f"tool:{tool}"[:80] if tool else "tool:unknown",
                "resource": _resource(tool, args),
                "decision": decision,
                "reason": reason,
                "request_id": request_id[:80] or None,
                "details": {
                    "client_id": caller.client_id if caller else None,
                    "latency_ms": round((self.clock() - started) * 1000, 1),
                },
            }
            try:
                await self.audit(event)
            except Exception:
                # The fail-closed gate for state-changing tools is the "call started" event below;
                # once a call has run, a failed outcome record is logged, not turned into an error.
                log.exception("audit write failed for %s", tool)

        if caller is None:
            await record("denied", "no authenticated caller")
            return _error_result("unauthenticated", "Authentication required")

        decision = tool_decision(tool, caller.tool_scopes)
        if not decision.allowed:
            await record("denied", decision.reason)
            return _error_result("forbidden", f"Not allowed to call {tool}: {decision.reason}")

        allowed, retry_after = self.limiter.allow(caller.subject)
        if not allowed:
            await record("denied", "rate limited")
            return _error_result("rate_limited", f"Rate limit exceeded; retry in {retry_after:.1f}s")

        if tool in STATE_CHANGING_TOOLS:
            # Fail closed: if we cannot write the audit trail we do not change state.
            try:
                await self.audit(
                    {
                        "actor": caller.subject,
                        "action": f"tool:{tool}",
                        "resource": _resource(tool, args),
                        "decision": "info",
                        "reason": "call started",
                        "request_id": request_id[:80] or None,
                        "details": {"client_id": caller.client_id},
                    }
                )
            except Exception:
                log.exception("audit unavailable; refusing %s", tool)
                return _error_result("audit_unavailable", "Audit log unavailable; state-changing call refused")

        token = current_caller.set(caller)
        try:
            result = await call_next(ctx)
        except Exception as exc:
            await record("error", type(exc).__name__)
            raise
        finally:
            current_caller.reset(token)
        outcome, reason = _outcome(result)
        await record(outcome, reason)
        return result
