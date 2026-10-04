"""Re-score saved agent runs with the current scorer, and combine runs into one result file.

    uv run python -m eval.rescore RUN.json [RUN.json ...] [--out eval/results/agent-<model>.json]

No LLM calls: the answers are read from each run's `-answers.jsonl` and scored again with
`eval/agent_eval.py`'s `score()`, so a scorer change reaches old runs without paying for
them again. Later files win for a question; answers that ended in an LLM error are
skipped, so a run split by an outage can be combined. Tool calls, latency, tokens and
judge scores are taken from the run that produced the answer (judging is not repeated).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from nordlys_discovery.agent.discover import AgentAnswer

from .agent_eval import score, summarise
from .retrieval import QUESTIONS, RESULTS, ROOT

CARRIED = (
    "tool_calls",
    "latency_ms",
    "input_tokens",
    "output_tokens",
    "model",
    "judge_score",
    "judge_rationale",
    "judge_input_tokens",
    "judge_output_tokens",
)


def load_run(path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    run = json.loads(path.read_text(encoding="utf-8"))
    answers_path = path.with_name(path.stem + "-answers.jsonl")
    answers = {
        a["id"]: a
        for a in (json.loads(line) for line in answers_path.read_text(encoding="utf-8").splitlines() if line.strip())
    }
    return run, answers


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("runs", nargs="+", type=Path, help="agent result files, oldest first")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    questions: list[dict[str, Any]] = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))["questions"]
    by_id = {q["id"]: q for q in questions}
    picked: dict[str, tuple[dict[str, Any], dict[str, Any], Path]] = {}  # id -> (old row, answer, source)
    runs = []
    for path in args.runs:
        run, answers = load_run(path)
        runs.append((path, run))
        for row in run["per_question"]:
            a = answers.get(row["id"])
            if a is not None and a["stop"] != "llm_error" and row["id"] in by_id:
                picked[row["id"]] = (row, a, path)

    rows: list[dict[str, Any]] = []
    answers_out: list[dict[str, Any]] = []
    for q in questions:
        if q["id"] not in picked:
            continue
        old, a, source = picked[q["id"]]
        row = score(q, AgentAnswer.model_validate(a))
        row |= {k: old[k] for k in CARRIED if k in old}
        row["source"] = source.name
        rows.append(row)
        answers_out.append(a)

    summary = summarise(rows)
    if not any("judge_input_tokens" in r for r in rows):  # older runs kept judge tokens in the summary only
        sources = {r["source"] for r in rows}
        for key in ("judge_input", "judge_output"):
            summary["tokens"][key] = sum(
                run["summary"]["tokens"].get(key, 0) for path, run in runs if path.name in sources
            )
    missing = [q["id"] for q in questions if q["id"] not in picked]
    models = sorted({r["model"] for r in rows})
    requested = {run["provider_model_requested"] for _, run in runs}
    out = {
        "generated_by": "eval/rescore.py",
        "sources": [
            str(path.resolve().relative_to(ROOT)) if path.resolve().is_relative_to(ROOT) else str(path)
            for path, _ in runs
        ],
        "provider_model_requested": ", ".join(sorted(requested)),
        "models_that_answered": models,
        "judge_model": next((run["judge_model"] for _, run in runs if run.get("judge_model")), None),
        "subset": "full" if not missing else f"partial: {len(rows)} of {len(questions)} questions",
        "embedding_model": runs[-1][1].get("embedding_model"),
        "caller": runs[-1][1].get("caller"),
        "summary": summary,
        "per_question": rows,
    }
    path = args.out or RESULTS / f"agent-{re.sub(r'[^a-z0-9.-]+', '-', models[0].lower())}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    with path.with_name(path.stem + "-answers.jsonl").open("w", encoding="utf-8") as fh:
        for a_ in answers_out:
            fh.write(json.dumps(a_, ensure_ascii=False) + "\n")

    print(f"Re-scored {len(rows)} answers from {len(runs)} run(s) ({out['subset']}), models {', '.join(models)}")
    for k, v in summary.items():
        print(f"  {k:<24} {v}")
    print(f"written {path} and -answers.jsonl")
    if missing:
        print(f"MISSING: no usable answer for {','.join(missing)}", file=sys.stderr)
    failed = [f"{r['id']}: {r['policy_violations']}" for r in rows if r["policy_violations"]]
    if failed:
        print("POLICY VIOLATIONS:\n  " + "\n  ".join(failed), file=sys.stderr)
        return 1
    return 3 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
