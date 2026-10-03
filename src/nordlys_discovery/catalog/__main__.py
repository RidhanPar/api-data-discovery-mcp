"""Validate the catalog and print a summary report.

    uv run python -m nordlys_discovery.catalog [--catalog-dir PATH] [--today YYYY-MM-DD]

Exit code 1 if any file is invalid. Every number printed is computed from the files.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from .loader import DEFAULT_CATALOG_DIR, CatalogError, load_catalog
from .metadata import description_coverage, extract_metadata, iter_operations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--catalog-dir", type=Path, default=DEFAULT_CATALOG_DIR)
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()

    try:
        specs, products = load_catalog(args.catalog_dir)
    except CatalogError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1

    metas = [(s, extract_metadata(s.document, s.path)) for s in specs]
    n_ops = sum(len(iter_operations(s.document)) for s in specs)

    print(f"Catalog: {args.catalog_dir}")
    print(f"  OpenAPI 3.1 specs : {len(specs)} valid ({n_ops} operations)")
    print(f"  Data products     : {len(products)} valid ({sum(len(p.schema_) for p in products)} fields)")

    print("\nAPIs per domain:")
    for domain, n in sorted(Counter(m.domain for _, m in metas).items()):
        print(f"  {domain:<10} {n}")

    print("\nMarket coverage (APIs / data products):")
    for c in ("SE", "NO", "FI", "DK", "EE", "LV", "LT"):
        a = sum(c in m.countries for _, m in metas)
        d = sum(c in p.countries for p in products)
        print(f"  {c}  {a:>2} / {d:>2}")

    print("\nDeprecated APIs:")
    for _, m in sorted(metas, key=lambda x: x[1].sunset or date.max):
        if m.deprecated and m.sunset:
            days = (m.sunset - args.today).days
            print(
                f"  {m.api_id} v{m.major_version}: sunset {m.sunset} ({days} days from {args.today}) -> {m.replacement}"
            )

    print("\nMetadata quality warnings:")
    for spec, m in metas:
        non_standard = {k: v for k, v in m.provenance.items() if v != "x-nordlys"}
        cov = description_coverage(spec.document)
        if non_standard or cov < 1.0 or "description" not in spec.document["info"]:
            print(
                f"  {spec.path.relative_to(args.catalog_dir)}: description coverage {cov:.0%}"
                + (", no info.description" if "description" not in spec.document["info"] else "")
                + (f", metadata {non_standard}" if non_standard else "")
            )

    print("\nData products by PII classification:")
    for level, n in sorted(Counter(p.pii_classification.value for p in products).items()):
        names = ", ".join(p.id for p in products if p.pii_classification.value == level)
        print(f"  {level:<9} {n}: {names}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
