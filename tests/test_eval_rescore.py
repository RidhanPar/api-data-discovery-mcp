"""The committed agent results must be what eval/rescore.py makes of the committed runs."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from eval import rescore
from eval.retrieval import RESULTS

RUNS = RESULTS / "runs" / "2026-10-04-anthropic"
COMMITTED = RESULTS / "agent-anthropic-claude-opus-5-5.json"


def test_committed_agent_results_reproduce(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "agent.json"
    argv = ["rescore", str(RUNS / "run1-q01-q29.json"), str(RUNS / "run2-q30-q45.json"), "--out", str(out)]
    monkeypatch.setattr(sys, "argv", argv)
    assert rescore.main() == 0
    fresh, committed = json.loads(out.read_text()), json.loads(COMMITTED.read_text())
    assert fresh["summary"] == committed["summary"]
    assert fresh["per_question"] == committed["per_question"]
    assert fresh["subset"] == "full"
