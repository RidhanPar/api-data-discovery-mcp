"""Structured logs and in-process metrics, shared by the catalog, MCP server and agent.

* Logs are one JSON object per line (stdout), with the request id of the current request,
  so Log Analytics (Azure) or `docker compose logs | jq` can filter and aggregate them.
  Events carry their fields as `extra={...}` and come out as top-level keys.
* `Metrics` keeps counts and a rolling latency window per key (tool or route) and serves
  p50/p95, error rate and, for search, the zero-result rate at `/metrics`. It is per
  process: with several replicas, aggregate the `tool_call` / `http_request` log events
  instead (queries in docs/observability.md).
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_STANDARD = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = getattr(record, "request_id", None) or request_id_var.get()
        if rid:
            out["request_id"] = rid
        for k, v in record.__dict__.items():
            if k not in _STANDARD and k not in out and not k.startswith("_"):
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str, ensure_ascii=False)


def configure_logging(service: str, level: str = "INFO", *, json_logs: bool = True, stream: Any = None) -> None:
    """Route all logging (uvicorn's included) to one handler. Idempotent."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(
        JsonFormatter(service) if json_logs else logging.Formatter("%(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    # Requests are logged by our own middleware as `http_request` events, with timings.
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("httpx").setLevel(logging.WARNING)


@dataclass
class _Series:
    calls: int = 0
    errors: int = 0
    zero_results: int = 0
    zero_checked: int = 0
    latencies: deque[float] = field(default_factory=lambda: deque(maxlen=1000))


def _pct(sorted_values: list[float], p: float) -> float | None:
    if not sorted_values:
        return None
    return round(sorted_values[min(len(sorted_values) - 1, int(p * (len(sorted_values) - 1) + 0.5))], 1)


class Metrics:
    def __init__(self) -> None:
        self._series: dict[str, _Series] = {}
        self._lock = threading.Lock()
        self._since = time.time()

    def observe(self, key: str, ms: float, *, error: bool = False, zero_results: bool | None = None) -> None:
        with self._lock:
            s = self._series.setdefault(key, _Series())
            s.calls += 1
            s.errors += int(error)
            s.latencies.append(ms)
            if zero_results is not None:
                s.zero_checked += 1
                s.zero_results += int(zero_results)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            out: dict[str, Any] = {}
            for key, s in sorted(self._series.items()):
                lat = sorted(s.latencies)
                item: dict[str, Any] = {
                    "calls": s.calls,
                    "errors": s.errors,
                    "error_rate": round(s.errors / s.calls, 4) if s.calls else 0.0,
                    "latency_ms_p50": _pct(lat, 0.5),
                    "latency_ms_p95": _pct(lat, 0.95),
                    "latency_window": len(lat),
                }
                if s.zero_checked:
                    item["zero_result_rate"] = round(s.zero_results / s.zero_checked, 4)
                out[key] = item
            return {"since": datetime.fromtimestamp(self._since, UTC).isoformat(timespec="seconds"), "series": out}
