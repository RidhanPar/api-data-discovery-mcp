"""Search the catalog from the command line.

uv run python -m nordlys_discovery.search "open claims in Norway" [--mode hybrid|vector|lexical]
    [--country NO] [--domain claims] [--version 2] [--deprecated true|false] [--limit 5]
"""

from __future__ import annotations

import argparse

from ..config import get_settings
from ..db.session import session_scope
from ..embeddings import create_provider
from .hybrid import SearchParams, search
from .models import SearchFilters, SearchQuery


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("query")
    p.add_argument("--mode", default="hybrid", choices=["hybrid", "vector", "lexical"])
    p.add_argument("--country")
    p.add_argument("--domain")
    p.add_argument("--version", type=int)
    p.add_argument("--deprecated", choices=["true", "false"])
    p.add_argument("--limit", type=int, default=5)
    a = p.parse_args()

    settings = get_settings()
    filters = SearchFilters.model_validate(
        {
            "country": a.country,
            "domain": a.domain,
            "version": a.version,
            "deprecated": None if a.deprecated is None else a.deprecated == "true",
        }
    )
    q = SearchQuery(query=a.query, mode=a.mode, limit=a.limit, filters=filters)
    embedder = create_provider(settings)
    params = SearchParams(candidates=settings.search_candidates, rrf_k=settings.rrf_k)
    with session_scope() as session:
        r = search(session, embedder, q, params)
    print(f"{len(r.hits)} of {r.total_assets} assets, {r.took_ms} ms ({r.mode})\n")
    for i, h in enumerate(r.hits, 1):
        version = f" v{h.major_version}" if h.major_version else ""
        flag = f"  [DEPRECATED, sunset {h.sunset} -> {h.replacement}]" if h.deprecated else ""
        print(f"{i}. {h.asset_id}{version} ({h.asset_type}, {h.domain}, pii={h.pii_level}){flag}")
        b = h.best_match
        print(f"   best match: {b.title}  [lexical #{b.lexical_rank}, vector #{b.vector_rank}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
