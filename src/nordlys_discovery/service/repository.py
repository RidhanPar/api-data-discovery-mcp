"""Read-side queries for the catalog service. All functions are pure reads."""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..catalog.diff import VersionDiff, diff_specs
from ..catalog.metadata import description_coverage, iter_operations
from ..db.models import ApiSpec, DataProductRow
from ..ingest.chunking import resolve_ref
from . import schemas as S

MAX_DEREF_DEPTH = 6


class NotFound(LookupError):
    def __init__(self, what: str) -> None:
        super().__init__(what)
        self.what = what


def dereference(doc: dict[str, Any], node: Any, depth: int = 0, stack: tuple[str, ...] = ()) -> Any:
    """Inline local $refs so callers get a self-contained JSON Schema. Cycles become a stub."""
    if isinstance(node, dict):
        if "$ref" in node and isinstance(node["$ref"], str):
            ref = node["$ref"]
            if ref in stack or depth >= MAX_DEREF_DEPTH:
                return {"$comment": f"recursive or deep reference to {ref} omitted"}
            return dereference(doc, resolve_ref(doc, ref), depth + 1, (*stack, ref))
        return {k: dereference(doc, v, depth, stack) for k, v in node.items()}
    if isinstance(node, list):
        return [dereference(doc, v, depth, stack) for v in node]
    return node


def _scopes(op: dict[str, Any], doc: dict[str, Any]) -> list[str]:
    return sorted({s for req in op.get("security", doc.get("security", [])) for v in req.values() for s in v})


def _summary(row: ApiSpec) -> S.ApiSummary:
    return S.ApiSummary(
        api_id=row.api_id,
        major_version=row.major_version,
        version=row.version,
        title=row.title,
        domain=row.domain,
        owner_team=row.owner_team,
        countries=row.countries,
        audience=row.audience,
        lifecycle_status=row.lifecycle_status,
        sunset=row.sunset,
        replacement=row.replacement,
    )


def list_apis(
    session: Session, *, domain: str | None = None, country: str | None = None, deprecated: bool | None = None
) -> list[S.ApiSummary]:
    stmt = select(ApiSpec).order_by(ApiSpec.domain, ApiSpec.api_id, ApiSpec.major_version)
    if domain:
        stmt = stmt.where(ApiSpec.domain == domain)
    if country:
        stmt = stmt.where(ApiSpec.countries.contains([country]))
    if deprecated is not None:
        stmt = stmt.where((ApiSpec.lifecycle_status == "deprecated") == deprecated)
    return [_summary(r) for r in session.scalars(stmt)]


def _get_api(session: Session, api_id: str, major: int) -> ApiSpec:
    row = session.scalar(select(ApiSpec).filter_by(api_id=api_id, major_version=major))
    if row is None:
        raise NotFound(f"API {api_id} v{major}")
    return row


def api_versions(session: Session, api_id: str) -> list[S.ApiSummary]:
    rows = list(session.scalars(select(ApiSpec).filter_by(api_id=api_id).order_by(ApiSpec.major_version)))
    if not rows:
        raise NotFound(f"API {api_id}")
    return [_summary(r) for r in rows]


def api_details(session: Session, api_id: str, major: int) -> S.ApiDetails:
    row = _get_api(session, api_id, major)
    doc = row.raw
    endpoints = [
        S.EndpointSummary(
            method=m,
            path=p,
            summary=o.get("summary"),
            operation_id=o.get("operationId"),
            deprecated=bool(o.get("deprecated")),
            scopes=_scopes(o, doc),
        )
        for p, m, o in iter_operations(doc)
    ]
    schemes = doc.get("components", {}).get("securitySchemes", {})
    return S.ApiDetails(
        **_summary(row).model_dump(),
        description=row.description,
        data_classification=row.data_classification,
        servers=[s.get("url", "") for s in doc.get("servers", [])],
        security_schemes={k: v.get("type", "?") for k, v in schemes.items()},
        scopes=sorted({s for e in endpoints for s in e.scopes}),
        endpoints=endpoints,
        documentation_quality=S.DocQuality(
            description_coverage=round(description_coverage(doc), 2),
            has_api_description=bool(row.description),
            metadata_provenance=row.metadata_provenance,
        ),
        other_versions=[
            r.major_version
            for r in session.scalars(select(ApiSpec).filter_by(api_id=api_id).where(ApiSpec.major_version != major))
        ],
    )


def endpoint_schema(session: Session, api_id: str, major: int, method: str, path: str) -> S.EndpointSchema:
    row = _get_api(session, api_id, major)
    doc = row.raw
    item = doc.get("paths", {}).get(path)
    op = item.get(method.lower()) if item else None
    if op is None:
        raise NotFound(f"endpoint {method.upper()} {path} in {api_id} v{major}")
    params = [dereference(doc, p) for p in [*item.get("parameters", []), *op.get("parameters", [])]]
    body = op.get("requestBody")
    return S.EndpointSchema(
        api_id=api_id,
        major_version=major,
        method=method.upper(),
        path=path,
        summary=op.get("summary"),
        description=op.get("description"),
        deprecated=bool(op.get("deprecated")),
        scopes=_scopes(op, doc),
        servers=[s.get("url", "") for s in doc.get("servers", [])],
        parameters=params,
        request_body=dereference(doc, body) if body else None,
        responses={str(k): dereference(doc, v) for k, v in op.get("responses", {}).items()},
    )


def raw_spec(session: Session, api_id: str, major: int) -> dict[str, Any]:
    return _get_api(session, api_id, major).raw


def compare_versions(session: Session, api_id: str, from_major: int, to_major: int) -> S.VersionComparison:
    old, new = _get_api(session, api_id, from_major), _get_api(session, api_id, to_major)
    diff: VersionDiff = diff_specs(old.raw, new.raw)
    return S.VersionComparison(
        api_id=api_id,
        from_version=_summary(old),
        to_version=_summary(new),
        is_breaking=diff.is_breaking,
        diff=diff,
    )


def deprecations(session: Session, *, today: date, sunset_before: date | None = None) -> list[S.Deprecation]:
    stmt = select(ApiSpec).where(ApiSpec.lifecycle_status == "deprecated").order_by(ApiSpec.sunset)
    out = []
    for r in session.scalars(stmt):
        if sunset_before and r.sunset and r.sunset > sunset_before:
            continue
        out.append(
            S.Deprecation(
                **_summary(r).model_dump(),
                days_until_sunset=(r.sunset - today).days if r.sunset else None,
                past_sunset=bool(r.sunset and r.sunset < today),
            )
        )
    return out


def _dp_summary(r: DataProductRow) -> S.DataProductSummary:
    return S.DataProductSummary(
        product_id=r.product_id,
        name=r.name,
        version=r.version,
        status=r.status,
        domain=r.domain,
        owner_team=r.owner_team,
        countries=r.countries,
        pii_classification=r.pii_classification,
    )


def list_data_products(
    session: Session, *, domain: str | None = None, country: str | None = None, pii_level: str | None = None
) -> list[S.DataProductSummary]:
    stmt = select(DataProductRow).order_by(DataProductRow.domain, DataProductRow.product_id)
    if domain:
        stmt = stmt.where(DataProductRow.domain == domain)
    if country:
        stmt = stmt.where(DataProductRow.countries.contains([country]))
    if pii_level:
        stmt = stmt.where(DataProductRow.pii_classification == pii_level)
    return [_dp_summary(r) for r in session.scalars(stmt)]


def data_product(session: Session, product_id: str, *, include_samples: bool = False) -> S.DataProductDetails:
    """Full contract. Sample rows are withheld unless explicitly requested.

    Phase 4 puts an access policy in front of `include_samples`; the default here is
    already the safe one.
    """
    r = session.scalar(select(DataProductRow).filter_by(product_id=product_id))
    if r is None:
        raise NotFound(f"data product {product_id}")
    contract = dict(r.raw)
    samples = contract.pop("sample_rows", [])
    return S.DataProductDetails(
        **_dp_summary(r).model_dump(),
        description=r.description,
        contract=contract,
        sample_rows=samples if include_samples else None,
        sample_rows_withheld=not include_samples and bool(samples),
    )
