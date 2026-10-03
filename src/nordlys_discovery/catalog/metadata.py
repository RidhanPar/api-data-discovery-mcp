"""Normalise catalog metadata from OpenAPI specs.

Teams describe their APIs inconsistently. Three conventions exist in the catalog:

1. The standard ``info.x-nordlys`` block (most teams).
2. Older flat extensions: ``x-owner``, ``x-countries`` (comma string), ``x-domain``.
3. Nothing useful at all (``x-team`` only), e.g. the DK document archive.

``extract_metadata`` maps all three onto one shape and records *how* each value was
obtained, so data-quality gaps are visible instead of silently papered over.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

KNOWN_COUNTRIES = ("SE", "NO", "FI", "DK", "EE", "LV", "LT")
KNOWN_DOMAINS = ("policy", "claims", "quotes", "customer", "partner", "payments", "documents", "fraud")
HTTP_METHODS = ("get", "post", "put", "patch", "delete")


@dataclass(frozen=True)
class ApiMetadata:
    api_id: str
    title: str
    version: str
    major_version: int
    domain: str
    owner_team: str
    countries: tuple[str, ...]
    audience: str
    lifecycle_status: str
    sunset: date | None
    replacement: str | None
    # Where each value came from: "x-nordlys", "legacy-extension", "inferred:<how>", "missing".
    provenance: dict[str, str] = field(default_factory=dict)

    @property
    def deprecated(self) -> bool:
        return self.lifecycle_status == "deprecated"


def _countries_from_string(raw: str) -> tuple[str, ...]:
    return tuple(c.strip().upper() for c in raw.split(",") if c.strip())


def _infer_countries(doc: dict[str, Any]) -> tuple[str, ...]:
    """Last resort: a country code as a path segment of the server URL, e.g. /dk/arkiv/v1."""
    for server in doc.get("servers", []):
        for segment in str(server.get("url", "")).lower().split("/"):
            if segment.upper() in KNOWN_COUNTRIES:
                return (segment.upper(),)
    return ()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def extract_metadata(doc: dict[str, Any], path: Path) -> ApiMetadata:
    info = doc.get("info", {})
    ext = info.get("x-nordlys")
    version = str(info.get("version", "0"))
    major_match = re.match(r"\d+", version)
    major = int(major_match.group()) if major_match else 0
    # File names follow <api-id>.v<major>.yaml; the parent directory is the domain.
    file_api_id = path.name.split(".v")[0]
    dir_domain = path.parent.name
    prov: dict[str, str] = {}

    ext = ext if isinstance(ext, dict) else {}
    complete = all(k in ext for k in ("api-id", "domain", "owner-team"))
    if complete:
        prov.update(dict.fromkeys(("api_id", "domain", "owner_team", "countries", "lifecycle"), "x-nordlys"))
        lifecycle = ext.get("lifecycle", {})
        sunset_raw = lifecycle.get("sunset")
        return ApiMetadata(
            api_id=ext["api-id"],
            title=info["title"],
            version=version,
            major_version=major,
            domain=ext["domain"],
            owner_team=ext["owner-team"],
            countries=tuple(ext.get("countries", ())),
            audience=ext.get("audience", "internal"),
            lifecycle_status=lifecycle.get("status", "active"),
            sunset=date.fromisoformat(str(sunset_raw)) if sunset_raw else None,
            replacement=lifecycle.get("replacement"),
            provenance=prov,
        )

    # Partial standard block, legacy extensions, or nothing: resolve each field on its own.
    if "domain" in ext:
        domain, prov["domain"] = str(ext["domain"]).lower(), "x-nordlys"
    elif "x-domain" in info:
        domain, prov["domain"] = str(info["x-domain"]).lower(), "legacy-extension"
    elif dir_domain in KNOWN_DOMAINS:
        domain, prov["domain"] = dir_domain, "inferred:catalog-folder"
    else:
        domain, prov["domain"] = "unknown", "missing"

    if "owner-team" in ext:
        owner, prov["owner_team"] = str(ext["owner-team"]), "x-nordlys"
    elif "x-owner" in info:
        owner, prov["owner_team"] = str(info["x-owner"]), "legacy-extension"
    elif "x-team" in info:
        owner, prov["owner_team"] = str(info["x-team"]), "legacy-extension"
    else:
        owner, prov["owner_team"] = "unknown", "missing"

    if ext.get("countries"):
        countries, prov["countries"] = tuple(str(c).upper() for c in ext["countries"]), "x-nordlys"
    elif "x-countries" in info:
        countries, prov["countries"] = _countries_from_string(str(info["x-countries"])), "legacy-extension"
    else:
        countries = _infer_countries(doc)
        prov["countries"] = "inferred:server-url" if countries else "missing"

    prov["api_id"] = "x-nordlys" if "api-id" in ext else "inferred:file-name"
    prov["lifecycle"] = "x-nordlys" if "lifecycle" in ext else "missing"
    return ApiMetadata(
        api_id=str(ext.get("api-id") or file_api_id or _slug(info.get("title", "unknown"))),
        title=str(info.get("title", file_api_id)),
        version=version,
        major_version=major,
        domain=domain,
        owner_team=owner,
        countries=countries,
        audience="internal",
        lifecycle_status="active",
        sunset=None,
        replacement=None,
        provenance=prov,
    )


def iter_operations(doc: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """(path, METHOD, operation) for every operation in the spec."""
    ops = []
    for p, item in doc.get("paths", {}).items():
        for method in HTTP_METHODS:
            if method in item:
                ops.append((p, method.upper(), item[method]))
    return ops


def description_coverage(doc: dict[str, Any]) -> float:
    """Share of operations with a summary or description (0.0-1.0)."""
    ops = iter_operations(doc)
    if not ops:
        return 0.0
    return sum(1 for _, _, o in ops if o.get("summary") or o.get("description")) / len(ops)
