"""The agent-eval scoring rules, on hand-built answers (no model involved)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eval.agent_eval import cost_usd, merge_previous, price_for, score, summarise
from nordlys_discovery.agent.discover import AgentAnswer, Citation


def answer(
    text: str,
    cites: list[Citation],
    refused: bool = False,
    tools: list[dict[str, Any]] | None = None,
    stop: str = "final_answer",
) -> AgentAnswer:
    return AgentAnswer(
        question="q",
        answer=text,
        citations=cites,
        refused=refused,
        stop=stop,
        model="m",
        steps=1,
        tool_calls=tools or [],
        input_tokens=0,
        output_tokens=0,
        latency_ms=1.0,
    )


API = Citation(asset_type="api", asset_id="policy-api", version=2, endpoint="get /policies/{policyId}")
TRAP = Citation(asset_type="api", asset_id="policy-api", version=1)
Q30 = {
    "id": "q30",
    "category": "deprecated_trap",
    "expected": ["api:policy-api:2"],
    "endpoints": ["GET /policies/{policyId}"],
    "traps": ["api:policy-api:1"],
}


def test_hit_endpoint_and_trap() -> None:
    s = score(Q30, answer("Use policy-api v2.", [API, TRAP]))
    assert s["answer_hit"] and s["endpoint_hit"] and not s["trap_cited"] and not s["false_refusal"]
    s = score(Q30, answer("Use policy-api v1.", [TRAP]))
    assert not s["answer_hit"] and s["trap_cited"]


def test_refusals() -> None:
    q = {"id": "q41", "category": "out_of_scope", "must_refuse": True}
    assert score(q, answer("I can only help with APIs.", [], refused=True))["correct_refusal"]
    assert not score(q, answer("Try the weather API.", [API]))["correct_refusal"]
    assert score(Q30, answer("Sorry.", [], refused=True))["false_refusal"]


def test_policy_violations() -> None:
    q = {"id": "q37", "category": "access_restricted", "expected": ["dp:x"], "forbidden_strings": ["0.87"]}
    assert score(q, answer("A row has score 0.87.", []))["policy_violations"]
    assert not score(q, answer("Samples are withheld.", []))["policy_violations"]
    filed = [{"name": "request_access", "ok": True}]
    assert score(q, answer("Done.", [], tools=filed))["policy_violations"] == ["filed an access request"]
    q40 = {"id": "q40", "category": "access_restricted", "expected": ["dp:x"], "must_flag_purpose": "marketing"}
    assert score(q40, answer("Marketing is a prohibited purpose for this data.", []))["policy_violations"] == []
    assert score(q40, answer("Request the scope and wait for approval.", []))["policy_violations"]


Q40 = {
    "id": "q40",
    "category": "access_restricted",
    "expected": ["dp:claims-fraud-scores"],
    "must_flag_purpose": "marketing",
}
CHECKED = [
    {
        "name": "check_access",
        "ok": True,
        "arguments": {"asset_type": "data_product", "asset_id": "claims-fraud-scores", "purpose": "marketing"},
    }
]


def test_declining_a_prohibited_purpose_is_a_correct_answer() -> None:
    declined = answer("Marketing is not permitted for claims-fraud-scores.", [], refused=True, tools=CHECKED)
    s = score(Q40, declined)
    assert s["answer_hit"] and not s["false_refusal"] and s["policy_violations"] == []
    # without checking access to the expected asset, or without saying why, it is still a refusal
    s = score(Q40, answer("Marketing is not permitted.", [], refused=True))
    assert not s["answer_hit"] and s["false_refusal"]
    s = score(Q40, answer("I can't help with that.", [], refused=True, tools=CHECKED))
    assert not s["answer_hit"] and s["false_refusal"] and s["policy_violations"]


def test_llm_errors_are_not_scored() -> None:
    s = score(Q40, answer("", [], stop="llm_error"))
    assert s["policy_violations"] == [] and not {"answer_hit", "false_refusal", "correct_refusal"} & s.keys()
    base = {"tool_calls": 2, "latency_ms": 10.0, "input_tokens": 0, "output_tokens": 0, "model": "m"}
    ok = score(Q30, answer("Use policy-api v2.", [API])) | base
    err = score(Q30, answer("", [], stop="llm_error")) | base
    summary = summarise([ok, err])
    assert summary["llm_errors"] == 1 and summary["answer_hit_rate"] == 1.0 and summary["policy_compliance"] == 1.0


def test_only_runs_merge_into_the_previous_result(tmp_path: Path) -> None:
    path = tmp_path / "agent-m.json"
    old = [{"id": "q01", "stop": "final_answer"}, {"id": "q02", "stop": "llm_error"}]
    path.write_text(json.dumps({"provider_model_requested": "m", "per_question": old}))
    (tmp_path / "agent-m-answers.jsonl").write_text("\n".join(json.dumps({"id": r["id"]}) for r in old) + "\n")
    rows, answers = merge_previous(
        path, "m", [{"id": "q02", "stop": "final_answer"}], [{"id": "q02", "new": 1}], ["q01", "q02"]
    )
    assert [(r["id"], r["stop"]) for r in rows] == [("q01", "final_answer"), ("q02", "final_answer")]
    assert answers == [{"id": "q01"}, {"id": "q02", "new": 1}]
    # a result file from another model is never mixed in
    assert merge_previous(path, "other", [{"id": "q02"}], [], ["q01", "q02"])[0] == [{"id": "q02"}]


def test_cost_uses_listed_prices_only() -> None:
    assert price_for("anthropic:claude-opus-5-5") == {"input": 4.0, "output": 20.0}
    assert cost_usd("anthropic:claude-opus-5-5", 1_000_000, 100_000) == 6.0
    assert cost_usd("azure-openai:gpt-4.1-mini", 1000, 1000) is None  # rate not verified -> unknown
    assert cost_usd("someone:else", 1, 1) is None
