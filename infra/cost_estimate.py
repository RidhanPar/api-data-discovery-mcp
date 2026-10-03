"""Monthly cost estimate for the Azure footprint, from live Azure list prices.

    uv run python infra/cost_estimate.py [--region swedencentral] [--active-hours 40]
                                         [--currency USD]

Prices come from the public Azure Retail Prices API (https://prices.azure.com, no login)
at the moment you run it; every meter used is printed, so the estimate can be checked.
It is a list-price estimate: no reservations, no enterprise discounts, and usage-based
parts (Azure OpenAI tokens, log volume) depend on traffic, so they are shown per unit.

The footprint mirrors infra/apps.tf. Apps with min_replicas >= 1 are billed for the
whole month (idle rate when not serving); scale-to-zero apps for --active-hours.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from typing import Any

API = "https://prices.azure.com/api/retail/prices"
HOURS = 730
SECONDS = HOURS * 3600

# (app, vCPU, GiB, always_on): keep in sync with infra/apps.tf
APPS = [
    ("catalog", 0.5, 1.0, False),
    ("mcp", 0.25, 0.5, False),
    ("agent", 0.25, 0.5, False),
    ("keycloak", 0.5, 1.0, False),
    ("n8n", 0.5, 1.0, True),
    ("mailpit", 0.25, 0.5, True),
]


def query(filter_: str, currency: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    url: str | None = f"{API}?currencyCode='{currency}'&$filter={urllib.parse.quote(filter_)}"
    while url:
        with urllib.request.urlopen(url, timeout=30) as resp:
            page = json.load(resp)
        items += page["Items"]
        url = page.get("NextPageLink")
    return [i for i in items if i.get("type") == "Consumption" and not i.get("reservationTerm")]


def pick(items: list[dict[str, Any]], *needles: str, unit: str | None = None) -> list[dict[str, Any]]:
    """Rows whose meter/sku/product names contain every needle (case-insensitive), all tiers."""
    out = []
    for i in items:
        hay = f"{i['productName']} | {i['skuName']} | {i['meterName']}".lower()
        if all(n.lower() in hay for n in needles) and (unit is None or i["unitOfMeasure"] == unit):
            out.append(i)
    if not out:
        raise SystemExit(f"no price row matched {needles!r} ({unit}); the API's meter names may have changed")
    return sorted(out, key=lambda i: i.get("tierMinimumUnits", 0))


def tiered(rows: list[dict[str, Any]], quantity: float) -> float:
    """Cost of `quantity` units under tiered pricing (tierMinimumUnits = free grant / volume tiers)."""
    rows = [r for r in rows if r["meterId"] == rows[0]["meterId"]]
    cost = 0.0
    for idx, r in enumerate(rows):
        lo = r.get("tierMinimumUnits", 0.0)
        hi = rows[idx + 1]["tierMinimumUnits"] if idx + 1 < len(rows) else float("inf")
        if quantity > lo:
            cost += (min(quantity, hi) - lo) * r["retailPrice"]
    return cost


def show(label: str, rows: list[dict[str, Any]], qty: float, *, seconds: bool = False) -> float:
    """`seconds`: qty is in seconds; converted if the meter is billed per hour."""
    unit = rows[0]["unitOfMeasure"].lower()
    scale = 3600.0 if seconds and "hour" in unit else 1.0
    cost = tiered(rows, qty / scale)
    r = rows[-1]
    print(
        f"  {label:<38} {qty:>14,.0f}  {r['meterName']} @ {r['retailPrice']} per {r['unitOfMeasure']}"
        f"{' (tiered)' if len(rows) > 1 else ''}  = {cost:,.2f}"
    )
    return cost


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--region", default="swedencentral")
    p.add_argument("--active-hours", type=float, default=40, help="busy hours/month of scale-to-zero apps")
    p.add_argument("--currency", default="USD")
    a = p.parse_args()
    region = f"armRegionName eq '{a.region}'"

    print(f"Azure list prices for {a.region} in {a.currency}, fetched now from {API}\n")
    total = 0.0

    aca = query(f"serviceName eq 'Azure Container Apps' and {region}", a.currency)
    active_vcpu = sum(c * (SECONDS if on else a.active_hours * 3600) for _, c, _, on in APPS)
    active_mem = sum(m * (SECONDS if on else a.active_hours * 3600) for _, _, m, on in APPS)
    # Always-on apps are mostly idle; count them as idle outside the active hours.
    idle_vcpu = sum(c * (SECONDS - a.active_hours * 3600) for _, c, _, on in APPS if on)
    idle_mem = sum(m * (SECONDS - a.active_hours * 3600) for _, _, m, on in APPS if on)
    active_vcpu -= idle_vcpu
    active_mem -= idle_mem
    print("Container Apps (Consumption):")
    total += show("vCPU active (vCPU-s)", pick(aca, "vcpu", "active"), active_vcpu, seconds=True)
    total += show("memory active (GiB-s)", pick(aca, "memory", "active"), active_mem, seconds=True)
    total += show("vCPU idle (vCPU-s)", pick(aca, "vcpu", "idle"), idle_vcpu, seconds=True)
    total += show("memory idle (GiB-s)", pick(aca, "memory", "idle"), idle_mem, seconds=True)

    pg = query(f"serviceName eq 'Azure Database for PostgreSQL' and {region}", a.currency)
    print("PostgreSQL Flexible Server:")
    total += show("B1ms compute (hours)", pick(pg, "flexible", "b1ms"), HOURS)
    total += show("storage 32 GiB (GB-month)", pick(pg, "flexible", "storage data stored"), 32)

    acr = query(f"serviceName eq 'Container Registry' and {region}", a.currency)
    print("Container Registry:")
    total += show("Basic registry (days)", pick(acr, "basic", "registry unit"), 30.4)

    print(
        f"\nFixed monthly estimate: {total:,.2f} {a.currency} "
        f"(Container Apps with {a.active_hours:.0f} busy hours, PostgreSQL B1ms, ACR Basic)"
    )

    print("\nUsage-based, per unit (multiply by your traffic):")
    la = query(f"serviceName eq 'Log Analytics' and {region}", a.currency)
    r = pick(la, "analytics logs", "data ingestion")[-1]
    print(f"  Log Analytics ingestion: {r['retailPrice']} per {r['unitOfMeasure']} (daily cap 1 GB in infra/main.tf)")
    kv = query(f"serviceName eq 'Key Vault' and {region}", a.currency)
    r = pick(kv, "standard", "operations")[-1]
    print(f"  Key Vault operations: {r['retailPrice']} per {r['unitOfMeasure']}")
    print("  Azure OpenAI: per token; see eval/results/agent-*.json for measured tokens per question")
    return 0


if __name__ == "__main__":
    sys.exit(main())
