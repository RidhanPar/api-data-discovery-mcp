"""Keep the realm's roles in sync with the catalog: one realm role per grantable scope.

    uv run python deploy/keycloak/sync_roles.py [--check]

Workflow A grants access by assigning the realm role named after the approved scope.
Defining every role here (realm as code) means n8n's service account only needs to
*assign* roles (manage-users, view-realm), not create them (manage-realm). A new API
with new scopes therefore needs this script and a realm update, reviewed like any code.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from nordlys_discovery.catalog.loader import DEFAULT_CATALOG_DIR, load_catalog
from nordlys_discovery.catalog.metadata import iter_operations

REALM = Path(__file__).with_name("nordlys-realm.json")
DESCRIPTION = "Asset access, granted only via an approved access request (workflow A)"


def grantable_scopes(catalog_dir: Path = DEFAULT_CATALOG_DIR) -> set[str]:
    specs, products = load_catalog(catalog_dir)
    scopes: set[str] = set()
    for spec in specs:
        doc = spec.document
        for _, _, op in iter_operations(doc):
            for req in op.get("security", doc.get("security", [])):
                for values in req.values():
                    scopes.update(values)
    scopes.update(p.access.scope for p in products)
    return scopes


def render(realm: dict[str, object], scopes: set[str]) -> str:
    roles = realm.setdefault("roles", {})
    assert isinstance(roles, dict)
    roles["realm"] = [{"name": s, "description": DESCRIPTION} for s in sorted(scopes)]
    return json.dumps(realm, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true", help="fail if the realm file is out of date")
    args = p.parse_args()
    current = REALM.read_text(encoding="utf-8")
    wanted = render(json.loads(current), grantable_scopes())
    if args.check:
        if current != wanted:
            print("nordlys-realm.json roles are out of date: run deploy/keycloak/sync_roles.py", file=sys.stderr)
            return 1
        print("OK: realm roles match the catalog's grantable scopes")
        return 0
    REALM.write_text(wanted, encoding="utf-8")
    print(f"Wrote {len(grantable_scopes())} realm roles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
