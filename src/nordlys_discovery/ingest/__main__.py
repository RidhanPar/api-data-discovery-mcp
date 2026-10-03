"""Ingest the catalog into Postgres.

    uv run python -m nordlys_discovery.ingest [--catalog-dir PATH] [--no-prune] [--reembed]

Prints a JSON report. Exit code 2 if any file was invalid (valid files are still ingested).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from ..config import get_settings
from ..db.session import session_scope
from ..embeddings import create_provider
from .pipeline import ingest_catalog, reset_embeddings


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--catalog-dir", type=Path, default=settings.catalog_dir)
    parser.add_argument("--no-prune", action="store_true", help="keep assets whose files were removed")
    parser.add_argument("--reembed", action="store_true", help="re-embed every chunk")
    args = parser.parse_args()
    logging.basicConfig(level=settings.log_level)

    embedder = create_provider(settings)
    with session_scope() as session:
        if args.reembed:
            reset_embeddings(session)
        extra = [settings.registrations_dir] if settings.registrations_dir else []
        report = ingest_catalog(session, args.catalog_dir, embedder, prune=not args.no_prune, extra_roots=extra)
    print(json.dumps(report.as_dict(), indent=2))
    return 2 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
