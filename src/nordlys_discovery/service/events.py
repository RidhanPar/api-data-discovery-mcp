"""Outbound event: tell the workflow engine (n8n, Workflow A) about a new access request.

The request is already committed as `pending_approval` before this runs, so a failed
notification can never lose or approve a request. It only delays the human review: the
failure is written to the audit log (`access_request.workflow_notify`, decision `error`)
and the request stays visible via GET /v1/access-requests?status=pending_approval.
Known limitation: delivery is best-effort with retries, not guaranteed. A transactional
outbox (event row written in the same transaction, relayed by a worker) would make it
at-least-once; see README "What I'd do next".

Payloads are signed: X-Nordlys-Signature = "sha256=" + HMAC-SHA256(secret, f"{ts}.{body}"),
with X-Nordlys-Timestamp. The receiver rejects stale timestamps (replay) and bad MACs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

import httpx
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from . import audit
from .access import AccessRequestOut

log = logging.getLogger(__name__)


def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify(secret: str, timestamp: str, body: bytes, signature: str, *, max_age_s: int = 300) -> bool:
    if not timestamp.isdigit() or abs(time.time() - int(timestamp)) > max_age_s:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), signature)


class AccessRequestNotifier:
    def __init__(self, url: str, secret: str, *, attempts: int = 3, timeout_s: float = 5.0) -> None:
        self.url = url
        self.secret = secret
        self.attempts = attempts
        self.timeout_s = timeout_s

    @classmethod
    def from_settings(cls, settings: Settings) -> AccessRequestNotifier | None:
        if not settings.access_request_webhook_url:
            return None
        if settings.access_request_webhook_secret is None:
            raise ValueError("NORDLYS_ACCESS_REQUEST_WEBHOOK_SECRET is required when the webhook URL is set")
        return cls(settings.access_request_webhook_url, settings.access_request_webhook_secret.get_secret_value())

    def payload(self, out: AccessRequestOut, request_id: str) -> dict[str, Any]:
        return {
            "event": "access_request.created",
            "request_id": request_id,
            "access_request": out.model_dump(mode="json"),
        }

    def notify(self, out: AccessRequestOut, request_id: str, sessions: sessionmaker[Session]) -> bool:
        body = json.dumps(self.payload(out, request_id), separators=(",", ":")).encode()
        error = "not attempted"
        for attempt in range(1, self.attempts + 1):
            ts = str(int(time.time()))
            try:
                resp = httpx.post(
                    self.url,
                    content=body,
                    timeout=self.timeout_s,
                    headers={
                        "Content-Type": "application/json",
                        "X-Nordlys-Timestamp": ts,
                        "X-Nordlys-Signature": sign(self.secret, ts, body),
                        "X-Request-ID": request_id,
                    },
                )
                if resp.is_success:
                    self._audit(sessions, out, request_id, "info", f"workflow notified (attempt {attempt})")
                    return True
                error = f"HTTP {resp.status_code}"
                if resp.status_code < 500 and resp.status_code != 429:
                    break  # a 4xx will not get better by retrying
            except httpx.HTTPError as exc:
                error = type(exc).__name__
            time.sleep(0.5 * 2 ** (attempt - 1))
        log.error("access request %s: workflow notification failed: %s", out.id, error)
        self._audit(sessions, out, request_id, "error", f"workflow notification failed: {error}")
        return False

    @staticmethod
    def _audit(
        sessions: sessionmaker[Session], out: AccessRequestOut, request_id: str, decision: str, reason: str
    ) -> None:
        with sessions() as s:
            audit.write(
                s,
                source="catalog",
                actor=out.requester,
                action="access_request.workflow_notify",
                resource=f"access_request:{out.id}",
                decision=decision,
                reason=reason,
                request_id=request_id,
            )
            s.commit()
