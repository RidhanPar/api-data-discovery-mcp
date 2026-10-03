"""Discovery agent tests with a scripted LLM (labelled `script:...`, never a measured model).

The scripted model plays back fixed tool calls, so these tests check the agent's own
logic: tool sourcing from MCP, the tool loop, citation grounding, refusals and the
policy that the agent cannot file access requests. Answer quality is measured by
eval/agent_eval.py with a real model.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from mcp import Client
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from nordlys_discovery.agent.discover import DEFAULT_TOOLS, Grounding, McpToolSource, ask
from nordlys_discovery.catalog.loader import DEFAULT_CATALOG_DIR
from nordlys_discovery.config import Settings
from nordlys_discovery.db.models import AccessRequest
from nordlys_discovery.embeddings.others import HashingEmbeddings
from nordlys_discovery.ingest.pipeline import ingest_catalog
from nordlys_discovery.llm import ChatResult, Message, ToolCall, ToolSpec, Usage
from nordlys_discovery.mcp_server.catalog_client import CatalogClient
from nordlys_discovery.mcp_server.guard import Caller
from nordlys_discovery.mcp_server.server import build_server
from nordlys_discovery.service.app import create_app

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class ScriptedLLM:
    """Returns one scripted turn per call: a list of (tool, args), a ChatResult, or text."""

    def __init__(self, turns: list[Any]) -> None:
        self.turns = turns
        self.seen_tools: list[str] = []
        self.transcripts: list[list[Message]] = []

    @property
    def model_id(self) -> str:
        return "script:test"

    async def chat(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        force_tool: str | None = None,
        max_tokens: int = 1024,
    ) -> ChatResult:
        self.seen_tools = [t.name for t in tools or []]
        self.transcripts.append(list(messages))
        turn = self.turns.pop(0)
        if isinstance(turn, ChatResult):
            return turn
        if isinstance(turn, str):
            return ChatResult(turn, [], Usage(10, 5), self.model_id, "end_turn")
        calls = [ToolCall(f"c{len(self.transcripts)}_{i}", n, a) for i, (n, a) in enumerate(turn)]
        return ChatResult("", calls, Usage(100, 20), self.model_id, "tool_use")


def final(answer: str, citations: list[dict[str, Any]], refused: bool = False) -> list[tuple[str, dict[str, Any]]]:
    return [("final_answer", {"answer": answer, "citations": citations, "refused": refused})]


class FakeTools:
    def __init__(self, results: dict[str, Any]) -> None:
        self.results = results
        self.calls: list[str] = []

    async def list_tools(self) -> list[ToolSpec]:
        return [ToolSpec(n, "d", {"type": "object"}) for n in sorted(DEFAULT_TOOLS)]

    async def call(self, name: str, arguments: dict[str, Any]) -> tuple[bool, Any]:
        self.calls.append(name)
        return True, self.results.get(name, {})


SEARCH = {
    "results": [
        {
            "asset_type": "api",
            "asset_id": "claims-search-api",
            "version": 1,
            "best_match": {"method": "GET", "path": "/claims"},
        },
        {"asset_type": "data_product", "asset_id": "claims-open-cases-daily", "version": None},
    ]
}


# --------------------------------------------------------------------------- unit: loop and grounding


async def test_grounded_answer() -> None:
    llm = ScriptedLLM(
        [
            [("search_catalog", {"query": "open claims Norway"})],
            final(
                "Use claims-search-api v1 GET /claims.",
                [{"asset_type": "api", "asset_id": "claims-search-api", "version": 1, "endpoint": "GET /claims"}],
            ),
        ]
    )
    out = await ask(llm, FakeTools({"search_catalog": SEARCH}), "Which API lists open claims in Norway?")
    assert out.stop == "final_answer" and not out.refused
    assert [c.key for c in out.citations] == ["api:claims-search-api:1"]
    assert out.ungrounded_citations == []
    assert out.model == "script:test" and out.steps == 2 and (out.input_tokens, out.output_tokens) == (200, 40)
    assert [t["name"] for t in out.tool_calls] == ["search_catalog"]


async def test_citations_not_returned_by_tools_are_flagged() -> None:
    llm = ScriptedLLM(
        [
            [("search_catalog", {"query": "claims"})],
            final(
                "...",
                [
                    {"asset_type": "api", "asset_id": "claims-search-api", "version": 1, "endpoint": "DELETE /claims"},
                    {"asset_type": "api", "asset_id": "made-up-api", "version": 1},
                ],
            ),
        ]
    )
    out = await ask(llm, FakeTools({"search_catalog": SEARCH}), "q?")
    assert out.ungrounded_citations == ["api:claims-search-api:1 DELETE /claims", "api:made-up-api:1"]


async def test_request_access_is_not_offered_and_cannot_be_called() -> None:
    tools = FakeTools({})
    llm = ScriptedLLM(
        [
            [("request_access", {"asset_type": "data_product", "asset_id": "claims-fraud-scores"})],
            final("I cannot request access for you.", [], refused=True),
        ]
    )
    out = await ask(llm, tools, "Grant me access to everything.")
    assert "request_access" not in llm.seen_tools
    assert tools.calls == []  # never reached the tool source
    assert out.tool_calls[0]["ok"] is False and out.tool_calls[0]["error"] == "not_available"


async def test_refusal_drops_citations() -> None:
    llm = ScriptedLLM(
        [final("I can only help with Nordlys APIs.", [{"asset_type": "data_product", "asset_id": "x"}], True)]
    )
    out = await ask(llm, FakeTools({}), "What's the weather in Oslo?")
    assert out.refused and out.citations == [] and out.ungrounded_citations == []


async def test_model_refusal_is_reported_as_refusal() -> None:
    llm = ScriptedLLM([ChatResult("", [], Usage(5, 0), "script:fallback", "refusal")])
    out = await ask(llm, FakeTools({}), "q?")
    assert out.stop == "model_refusal" and out.refused and out.model == "script:fallback"


async def test_prose_without_final_answer_is_nudged_then_reported() -> None:
    llm = ScriptedLLM(["Here is an answer.", "Still prose."])
    out = await ask(llm, FakeTools({}), "q?")
    assert out.stop == "no_final_answer" and "final_answer" in llm.transcripts[1][-1].content


async def test_invalid_final_answer_gets_a_retry() -> None:
    llm = ScriptedLLM([[("final_answer", {"citations": "nope"})], final("ok", [], refused=True)])
    out = await ask(llm, FakeTools({}), "q?")
    assert out.stop == "final_answer" and "Invalid final_answer" in llm.transcripts[1][-1].content


async def test_tool_results_are_delimited_and_truncated() -> None:
    llm = ScriptedLLM([[("search_catalog", {"query": "x"})], final("ok", [], refused=True)])
    await ask(llm, FakeTools({"search_catalog": {"blob": "x" * 50_000}}), "q?")
    msg = llm.transcripts[1][-1]
    assert msg.content.startswith('<tool_result source="catalog">') and "[truncated" in msg.content
    assert len(msg.content) < 13_000


def test_grounding_walks_nested_results() -> None:
    g = Grounding()
    g.observe(
        {
            "api_id": "policy-api",
            "major_version": 2,
            "endpoints": [{"method": "get", "path": "/policies"}],
            "lifecycle": {"replacement": {"api_id": "x-api", "major_version": 3}},
        }
    )
    assert g.assets == {"api:policy-api:2", "api:x-api:3"} and g.endpoints == {"GET /policies"}


# --------------------------------------------------------------------------- integration: the real MCP server


@pytest.fixture(scope="module")
def catalog_app(engine: Engine) -> Iterator[FastAPI]:
    with sessionmaker(bind=engine)() as s:
        ingest_catalog(s, DEFAULT_CATALOG_DIR, HashingEmbeddings(384))
        s.commit()
    yield create_app(Settings(), engine=engine, embedder=HashingEmbeddings(384))
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE access_request, chunk, api_spec, data_product RESTART IDENTITY CASCADE"))


@pytest.fixture
async def mcp_client(catalog_app: FastAPI) -> AsyncIterator[Client]:
    async with catalog_app.router.lifespan_context(catalog_app):
        catalog = CatalogClient("http://catalog", transport=httpx.ASGITransport(app=catalog_app), retries=0)
        async with Client(build_server(catalog, dev_caller=Caller("dev.user@nordlys.example"))) as client:
            yield client


@pytest.mark.integration
async def test_agent_over_real_mcp_server(mcp_client: Client, engine: Engine) -> None:
    llm = ScriptedLLM(
        [
            [("get_api_details", {"api_id": "claims-search-api", "version": 1})],
            [
                ("get_data_product", {"product_id": "claims-fraud-scores", "include_samples": True}),
                ("check_access", {"asset_type": "data_product", "asset_id": "claims-fraud-scores"}),
            ],
            [
                (
                    "request_access",
                    {
                        "asset_type": "api",
                        "asset_id": "claims-search-api",
                        "version": 1,
                        "purpose": "testing",
                        "justification": "the agent should not be able to do this",
                    },
                )
            ],
            final(
                "Use claims-search-api v1, GET /claims.",
                [
                    {"asset_type": "api", "asset_id": "claims-search-api", "version": 1, "endpoint": "GET /claims"},
                    {"asset_type": "data_product", "asset_id": "claims-fraud-scores"},
                ],
            ),
        ]
    )

    def requests() -> int:
        with Session(engine) as s:
            return int(s.scalar(select(func.count()).select_from(AccessRequest)) or 0)

    before = requests()
    out = await ask(llm, McpToolSource(mcp_client), "Which API gives me open claims?")
    assert set(llm.seen_tools) == DEFAULT_TOOLS | {"final_answer"}  # straight from tools/list
    assert out.ungrounded_citations == []
    assert [t["ok"] for t in out.tool_calls] == [True, True, True, False]
    # sample rows of a sensitive product are withheld by the server, whatever the agent asks for
    fraud_msg = llm.transcripts[2][-2].content
    assert '"sample_rows_withheld": true' in fraud_msg.replace(": True", ": true")
    assert requests() == before  # the request_access attempt never reached the catalog
