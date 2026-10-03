"""JSON logs, request ids and the /metrics endpoints."""

from __future__ import annotations

import io
import json
import logging

from fastapi.testclient import TestClient

from nordlys_discovery.config import Settings
from nordlys_discovery.observability import JsonFormatter, Metrics, configure_logging, request_id_var


def test_json_log_line_has_request_id_and_extra_fields() -> None:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter("test"))
    lg = logging.getLogger("nordlys.test.json")
    lg.addHandler(handler)
    lg.setLevel(logging.INFO)
    token = request_id_var.set("req-123")
    try:
        lg.info("tool_call", extra={"event": "tool_call", "tool": "search_catalog", "latency_ms": 12.5})
    finally:
        request_id_var.reset(token)
        lg.removeHandler(handler)
    line = json.loads(buf.getvalue())
    assert line["msg"] == "tool_call" and line["service"] == "test" and line["request_id"] == "req-123"
    assert line["tool"] == "search_catalog" and line["latency_ms"] == 12.5 and line["level"] == "INFO"


def test_configure_logging_routes_uvicorn_through_one_json_handler() -> None:
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        buf = io.StringIO()
        configure_logging("svc", "INFO", stream=buf)
        logging.getLogger("uvicorn.error").info("started")
        assert json.loads(buf.getvalue())["msg"] == "started"
        assert logging.getLogger("uvicorn.access").disabled
    finally:
        root.handlers, _ = saved
        root.setLevel(saved[1])
        logging.getLogger("uvicorn.access").disabled = False


def test_metrics_percentiles_error_and_zero_result_rates() -> None:
    m = Metrics()
    for ms in range(1, 101):
        m.observe("tool:search_catalog", float(ms), zero_results=ms <= 10)
    m.observe("tool:get_api_details", 5.0, error=True)
    snap = m.snapshot()["series"]
    s = snap["tool:search_catalog"]
    assert (s["calls"], s["latency_ms_p50"], s["latency_ms_p95"], s["zero_result_rate"]) == (100, 51.0, 95.0, 0.1)
    assert snap["tool:get_api_details"]["error_rate"] == 1.0
    assert "zero_result_rate" not in snap["tool:get_api_details"]


def test_catalog_metrics_and_request_id_header() -> None:
    from nordlys_discovery.embeddings.others import HashingEmbeddings
    from nordlys_discovery.service.app import create_app

    app = create_app(Settings(), embedder=HashingEmbeddings(384))
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/v1/search", headers={"X-Request-ID": "abc"})  # 422 before any DB access
        assert r.status_code == 422 and r.headers["X-Request-ID"] == "abc"
        snap = client.get("/metrics").json()["series"]
    assert snap["GET /v1/search"]["calls"] == 1 and snap["GET /v1/search"]["error_rate"] == 0.0
