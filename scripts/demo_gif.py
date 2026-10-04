"""Record the README demo GIF: one real agent run, replayed as a terminal animation.

    NORDLYS_LLM_PROVIDER=anthropic|azure-openai|openai \\
    uv run python -m scripts.demo_gif [--question "..."]

The agent answers through the real MCP server and catalog service, in-process on the
`<db>_eval` database (same set-up as eval/agent_eval.py, so Postgres must be running:
`docker compose up -d --wait db`). Nothing in the GIF is typed by hand: the question,
every tool call, the answer, the citations and the timings come from this run. The run
itself is also saved as docs/img/agent-demo.json, so the GIF can be checked against it.

The animation is sped up (the footer shows the real latency); it is not a screen capture.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import textwrap
from pathlib import Path
from typing import Any

import httpx
from mcp import Client
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from eval.agent_eval import EVAL_USER
from eval.retrieval import ROOT, prepare_database
from nordlys_discovery.agent.discover import AgentAnswer, McpToolSource, ask
from nordlys_discovery.config import get_settings
from nordlys_discovery.embeddings import create_provider
from nordlys_discovery.ingest.pipeline import ingest_catalog
from nordlys_discovery.llm import LLMUnavailable, create_llm
from nordlys_discovery.mcp_server.catalog_client import CatalogClient
from nordlys_discovery.mcp_server.server import build_server
from nordlys_discovery.service.app import create_app

OUT = ROOT / "docs" / "img"
QUESTION = "Which API gives me open claims for Norway, and how do I get access?"
FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")
BOLD = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf")

WIDTH, COLS, SIZE, LINE_H, PAD = 1180, 96, 17, 24, 28
BG, FG, DIM, ACCENT, GOOD, WARN = "#0f172a", "#e2e8f0", "#94a3b8", "#7dd3fc", "#86efac", "#fcd34d"


# --------------------------------------------------------------------------- run


async def run_agent(question: str) -> AgentAnswer:
    settings = get_settings()
    llm = create_llm(settings)
    embedder = create_provider(settings)
    engine = create_engine(prepare_database(settings.database_url))
    with sessionmaker(bind=engine)() as s:
        ingest_catalog(s, settings.catalog_dir, embedder)
        s.commit()
    app = create_app(settings, engine=engine, embedder=embedder)
    async with app.router.lifespan_context(app):
        catalog = CatalogClient("http://catalog", transport=httpx.ASGITransport(app=app), retries=0)
        async with Client(build_server(catalog, dev_caller=EVAL_USER)) as client:
            return await ask(llm, McpToolSource(client), question, max_steps=settings.agent_max_steps)


# --------------------------------------------------------------------------- render

Line = tuple[str, str, bool]  # text, colour, bold


def wrap(text: str, colour: str, indent: str = "", bold: bool = False) -> list[Line]:
    out: list[Line] = []
    for para in text.splitlines() or [""]:
        lead = re.match(r"\s*(?:[-*]|\d+\.)\s+", para)
        sub = " " * len(lead.group(0)) if lead else ""
        lines = textwrap.wrap(para, COLS - len(indent), subsequent_indent=sub) or [""]
        out += [(indent + ln, colour, bold) for ln in lines]
    return out


def plain(markdown: str) -> str:
    """Markdown to terminal text: drop emphasis and code markers, keep lists."""
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), markdown)
    text = re.sub(r"^#+\s*", "", text, flags=re.M)
    return text.replace("`", "")


def args_preview(arguments: dict[str, Any]) -> str:
    s = json.dumps(arguments, ensure_ascii=False)
    return s if len(s) <= 70 else s[:67] + "..."


def frames_for(a: AgentAnswer) -> list[tuple[list[Line], int]]:
    """The animation as (screen contents, frame duration in ms)."""
    frames: list[tuple[list[Line], int]] = []
    screen: list[Line] = []

    def show(lines: list[Line], ms: int) -> None:
        screen.extend(lines)
        frames.append((list(screen), ms))

    for i in range(0, len(a.question), 3):  # type the question
        frames.append(([("$ ask " + a.question[:i], FG, True)], 45))
    show([("$ ask " + a.question, FG, True), ("", FG, False)], 500)
    for t in a.tool_calls:
        ok = bool(t.get("ok"))
        status = f"[{'ok' if ok else 'error'}, {t.get('ms', 0):.0f} ms]"
        line = f"  -> {t['name']} {args_preview(t.get('arguments') or {})}  {status}"
        show([(line, DIM if ok else WARN, False)], 650)
    show([("", FG, False)], 300)
    for ln in wrap(plain(a.answer), FG):
        show([ln], 70)
    show([("", FG, False), ("Citations (checked against what the tools returned):", ACCENT, True)], 400)
    for c in a.citations:
        key = c.asset_id + (f" v{c.version}" if c.version else "") + (f"  {c.endpoint}" if c.endpoint else "")
        grounded = c.key not in a.ungrounded_citations and f"{c.key} {c.endpoint}" not in a.ungrounded_citations
        show([(f"  [{'grounded' if grounded else 'NOT grounded'}] {key}", GOOD if grounded else WARN, False)], 250)
    footer = (
        f"{a.model} | {len(a.tool_calls)} MCP tool calls | answered in {a.latency_ms / 1000:.1f} s (animation sped up)"
    )
    show([("", FG, False), (footer, DIM, False)], 7000)
    return frames


def render(a: AgentAnswer, path: Path) -> int:
    font, bold = ImageFont.truetype(str(FONT), SIZE), ImageFont.truetype(str(BOLD), SIZE)
    frames = frames_for(a)
    height = PAD * 2 + 40 + max(len(lines) for lines, _ in frames) * LINE_H
    images: list[Image.Image] = []
    for lines, _ in frames:
        img = Image.new("RGB", (WIDTH, height), BG)
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, WIDTH, 32), fill="#1e293b")
        for x, c in ((18, "#f87171"), (40, "#fbbf24"), (62, "#4ade80")):
            d.ellipse((x - 6, 10, x + 6, 22), fill=c)
        d.text((90, 7), "Nordlys discovery agent, via the MCP server", fill=DIM, font=font)
        for n, (text, colour, is_bold) in enumerate(lines):
            d.text((PAD, PAD + 40 + n * LINE_H), text, fill=colour, font=bold if is_bold else font)
        images.append(img.convert("P", palette=Image.Palette.ADAPTIVE, colors=32))
    durations = [ms for _, ms in frames]
    images[0].save(path, save_all=True, append_images=images[1:], duration=durations, loop=0, optimize=True)
    return len(images)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--question", default=QUESTION)
    args = p.parse_args()
    try:
        a = asyncio.run(run_agent(args.question))
    except LLMUnavailable as exc:
        print(f"No LLM configured ({exc}); the demo needs a real agent run.", file=sys.stderr)
        return 2
    if a.stop != "final_answer":
        print(f"The agent did not finish ({a.stop}: {a.error}); not recording a GIF.", file=sys.stderr)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "agent-demo.json").write_text(json.dumps(a.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    frames = render(a, OUT / "agent-demo.gif")
    print(f"written docs/img/agent-demo.gif ({frames} frames) and docs/img/agent-demo.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
