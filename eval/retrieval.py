"""Retrieval evaluation: recall@k and MRR for lexical, vector and hybrid search.

    uv run python -m eval.retrieval [--subset ci] [--min-recall3 0.8 --min-mrr 0.7]

* Builds a dedicated database (`<db>_eval`) from the canonical catalog only (migrations +
  ingestion), so results never depend on whatever else is in your working database.
* Uses the checksum-pinned local embedding model, so the numbers are reproducible.
* Writes eval/results/retrieval[-ci].json and prints a Markdown table.
* With --min-* thresholds it exits non-zero when hybrid search falls below them: the CI
  regression gate.

No LLM is involved.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import yaml
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from nordlys_discovery.config import get_settings
from nordlys_discovery.embeddings import create_provider
from nordlys_discovery.ingest.pipeline import ingest_catalog
from nordlys_discovery.search.hybrid import SearchParams, search
from nordlys_discovery.search.models import SearchQuery

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = ROOT / "eval" / "questions.yaml"
RESULTS = ROOT / "eval" / "results"
# (label, search mode, deprecated penalty). "+demote" = deprecated assets' scores x0.5.
VARIANTS = (
    ("lexical", "lexical", 1.0),
    ("vector", "vector", 1.0),
    ("hybrid", "hybrid", 1.0),
    ("vector+demote", "vector", 0.5),
    ("hybrid+demote", "hybrid", 0.5),
)
GATED = "hybrid+demote"  # the configuration the service runs with
K = 10


def asset_key(hit: Any) -> str:
    return f"api:{hit.asset_id}:{hit.major_version}" if hit.asset_type == "api" else f"dp:{hit.asset_id}"


def load_questions(subset: str | None) -> list[dict[str, Any]]:
    qs: list[dict[str, Any]] = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))["questions"]
    return [q for q in qs if subset != "ci" or q.get("ci")]


def prepare_database(base_url: str) -> str:
    """Create <db>_eval if needed, migrate it and ingest the canonical catalog."""
    url = make_url(base_url)
    eval_db = f"{url.database}_eval"
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": eval_db})
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{eval_db}"'))
    admin.dispose()
    eval_url = url.set(database=eval_db).render_as_string(hide_password=False)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = eval_url
    command.upgrade(cfg, "head")
    return eval_url


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--subset", choices=["ci"], default=None)
    p.add_argument("--min-recall3", type=float, default=None)
    p.add_argument("--min-mrr", type=float, default=None)
    args = p.parse_args()

    settings = get_settings()
    embedder = create_provider(settings)
    eval_url = prepare_database(settings.database_url)
    engine = create_engine(eval_url)
    sessions = sessionmaker(bind=engine)
    with sessions() as s:
        report = ingest_catalog(s, settings.catalog_dir, embedder)
        s.commit()

    questions = [q for q in load_questions(args.subset) if q.get("expected")]
    per_mode: dict[str, dict[str, Any]] = {}
    details: list[dict[str, Any]] = []
    with sessions() as s:
        for label, mode, penalty in VARIANTS:
            params = SearchParams(
                candidates=settings.search_candidates, rrf_k=settings.rrf_k, deprecated_penalty=penalty
            )
            ranks, latencies, trap_above = [], [], []
            for q in questions:
                t0 = time.perf_counter()
                r = search(s, embedder, SearchQuery(query=q["question"], mode=mode, limit=K), params)
                latencies.append((time.perf_counter() - t0) * 1000)
                keys = [asset_key(h) for h in r.hits]
                rank = next((i for i, k in enumerate(keys, 1) if k in q["expected"]), None)
                ranks.append(rank)
                if q.get("traps"):
                    trap_rank = next((i for i, k in enumerate(keys, 1) if k in q["traps"]), None)
                    trap_above.append(trap_rank is not None and (rank is None or trap_rank < rank))
                details.append(
                    {"mode": label, "id": q["id"], "category": q["category"], "rank": rank, "top3": keys[:3]}
                )
            n = len(ranks)
            per_mode[label] = {
                "questions": n,
                "recall@1": round(sum(1 for r in ranks if r and r <= 1) / n, 3),
                "recall@3": round(sum(1 for r in ranks if r and r <= 3) / n, 3),
                "recall@10": round(sum(1 for r in ranks if r) / n, 3),
                "mrr@10": round(sum(1 / r for r in ranks if r) / n, 3),
                "trap_ranked_above_answer": f"{sum(trap_above)}/{len(trap_above)}",
                "latency_ms_p50": round(statistics.median(latencies), 1),
                "latency_ms_p95": round(sorted(latencies)[int(0.95 * (n - 1))], 1),
            }

    by_category: dict[str, dict[str, float]] = {}
    for mode, _, _ in VARIANTS:
        cats: dict[str, list[int | None]] = {}
        for d in details:
            if d["mode"] == mode:
                cats.setdefault(d["category"], []).append(d["rank"])
        for cat, rs in cats.items():
            by_category.setdefault(cat, {})[mode] = round(sum(1 for r in rs if r and r <= 3) / len(rs), 3)

    out = {
        "generated_by": "eval/retrieval.py",
        "subset": args.subset or "full",
        "embedding_model": embedder.model_id,
        "catalog": {"apis": report.apis, "data_products": report.data_products},
        "k": K,
        "metrics": per_mode,
        "recall@3_by_category": by_category,
        "details": details,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"retrieval{'-ci' if args.subset else ''}.json"
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    print(
        f"Retrieval evaluation ({out['subset']}, {per_mode['hybrid']['questions']} questions with expected assets, "
        f"model {embedder.model_id})\n"
    )
    print("| configuration | recall@1 | recall@3 | recall@10 | MRR@10 | trap above answer | p50 ms | p95 ms |")
    print("|---|---|---|---|---|---|---|---|")
    for mode, m in per_mode.items():
        print(
            f"| {mode} | {m['recall@1']} | {m['recall@3']} | {m['recall@10']} | {m['mrr@10']} | "
            f"{m['trap_ranked_above_answer']} | {m['latency_ms_p50']} | {m['latency_ms_p95']} |"
        )
    print("\nrecall@3 by category:")
    for cat, vals in by_category.items():
        print(f"  {cat:<18} " + "  ".join(f"{m}={v}" for m, v in vals.items()))
    print(f"\nwritten {path.relative_to(ROOT)}")

    h = per_mode[GATED]
    failed = []
    if args.min_recall3 is not None and h["recall@3"] < args.min_recall3:
        failed.append(f"{GATED} recall@3 {h['recall@3']} < {args.min_recall3}")
    if args.min_mrr is not None and h["mrr@10"] < args.min_mrr:
        failed.append(f"{GATED} MRR {h['mrr@10']} < {args.min_mrr}")
    if failed:
        print("REGRESSION: " + "; ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
