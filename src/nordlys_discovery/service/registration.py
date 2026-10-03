"""New API registrations (Workflow B): deterministic checks and publishing.

Everything in this module is a rule. The generative step (classification, owner
suggestion, documentation critique) lives in the agent service; n8n combines both and
decides whether a human must review (see deploy/n8n/build_workflows.py, workflow B).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from openapi_spec_validator import validate
from openapi_spec_validator.validation.exceptions import OpenAPIValidationError
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..catalog.metadata import KNOWN_DOMAINS, description_coverage, extract_metadata, iter_operations
from ..db.models import ApiSpec
from ..embeddings import EmbeddingProvider
from ..ingest.pipeline import IngestReport, ingest_catalog
from ..search.hybrid import SearchParams, search
from ..search.models import SearchFilters, SearchQuery
from ..security.injection import scan_document

MAX_SPEC_BYTES = 512 * 1024
_PARAM = re.compile(r"\{[^}]+\}")


class RegistrationError(ValueError):
    pass


class _NoAliasLoader(yaml.SafeLoader):
    """SafeLoader that refuses YAML aliases: blocks 'billion laughs' expansion bombs."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("YAML aliases are not allowed in submitted specs")
        return super().compose_node(parent, index)


def parse_spec(text: str) -> dict[str, Any]:
    if len(text.encode("utf-8")) > MAX_SPEC_BYTES:
        raise RegistrationError(f"spec larger than {MAX_SPEC_BYTES // 1024} KB")
    try:
        doc = yaml.load(text, Loader=_NoAliasLoader)
    except yaml.YAMLError as exc:
        raise RegistrationError(f"not valid YAML/JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise RegistrationError("top level must be a mapping")
    return doc


class DuplicateCandidate(BaseModel):
    api_id: str
    major_version: int
    title: str
    endpoint_overlap: float = Field(description="Jaccard overlap of (method, path) pairs, parameters ignored")
    search_rank: int | None


class RegistrationCheck(BaseModel):
    valid: bool
    errors: list[str]
    api_id: str | None = None
    major_version: int | None = None
    title: str | None = None
    declared_domain: str | None = None
    declared_owner_team: str | None = None
    countries: list[str] = []
    description: str | None = None
    endpoints: list[str] = []
    schema_names: list[str] = []
    description_coverage: float = 0.0
    content_warnings: list[str] = []
    version_exists: bool = False
    duplicates: list[DuplicateCandidate] = []
    known_domains: list[str] = list(KNOWN_DOMAINS)
    known_owner_teams: list[str] = []


def _ops(doc: dict[str, Any]) -> set[str]:
    return {f"{m} {_PARAM.sub('{}', p)}" for p, m, _ in iter_operations(doc)}


def check(session: Session, embedder: EmbeddingProvider | None, text: str) -> RegistrationCheck:
    try:
        doc = parse_spec(text)
    except RegistrationError as exc:
        return RegistrationCheck(valid=False, errors=[str(exc)])
    errors: list[str] = []
    if not str(doc.get("openapi", "")).startswith("3.1"):
        errors.append(f"OpenAPI 3.1 required, got {doc.get('openapi')!r}")
    else:
        try:
            validate(doc)
        except OpenAPIValidationError as exc:
            errors.append(f"OpenAPI validation failed: {exc.message}")
    raw_info = doc.get("info")
    info: dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
    teams = sorted(set(session.scalars(select(ApiSpec.owner_team))) - {"unknown"})
    if errors:
        return RegistrationCheck(valid=False, errors=errors, title=info.get("title"), known_owner_teams=teams)

    title = str(info.get("title", "untitled"))
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "api"
    ext = info.get("x-nordlys") or {}
    major_match = re.match(r"\d+", str(info.get("version", "1")))
    major = int(major_match.group()) if major_match else 1
    api_id = str(ext.get("api-id") or slug)
    meta = extract_metadata(doc, Path(f"/registrations/apis/unknown/{api_id}.v{major}.yaml"))

    new_ops = _ops(doc)
    dupes: dict[tuple[str, int], DuplicateCandidate] = {}
    for row in session.scalars(select(ApiSpec)):
        existing = _ops(row.raw)
        union = new_ops | existing
        overlap = len(new_ops & existing) / len(union) if union else 0.0
        if overlap >= 0.3:
            dupes[(row.api_id, row.major_version)] = DuplicateCandidate(
                api_id=row.api_id,
                major_version=row.major_version,
                title=row.title,
                endpoint_overlap=round(overlap, 2),
                search_rank=None,
            )
    if embedder is not None:
        query = f"{title}. {(info.get('description') or '')[:300]}"
        hits = search(
            session,
            embedder,
            SearchQuery(query=query[:500], filters=SearchFilters(asset_type="api"), limit=3),
            SearchParams(),
        ).hits
        for rank, h in enumerate(hits, 1):
            key = (h.asset_id, h.major_version or 0)
            if key in dupes:
                dupes[key].search_rank = rank
            elif rank == 1:
                dupes[key] = DuplicateCandidate(
                    api_id=h.asset_id,
                    major_version=h.major_version or 0,
                    title=h.title,
                    endpoint_overlap=0.0,
                    search_rank=rank,
                )

    exists = session.scalar(select(ApiSpec).filter_by(api_id=api_id, major_version=major)) is not None
    schemas = (doc.get("components") or {}).get("schemas") or {}
    return RegistrationCheck(
        valid=True,
        errors=[],
        api_id=api_id,
        major_version=major,
        title=title,
        declared_domain=meta.domain
        if meta.domain in KNOWN_DOMAINS and meta.provenance.get("domain") != "missing"
        else None,
        declared_owner_team=meta.owner_team if meta.owner_team != "unknown" else None,
        countries=list(meta.countries),
        description=(info.get("description") or "")[:1500] or None,
        endpoints=sorted(
            f"{m} {p}" + (f" - {o.get('summary')}" if o.get("summary") else "") for p, m, o in iter_operations(doc)
        )[:60],
        schema_names=sorted(schemas)[:40],
        description_coverage=round(description_coverage(doc), 2),
        content_warnings=scan_document(doc),
        version_exists=exists,
        duplicates=sorted(dupes.values(), key=lambda d: (-d.endpoint_overlap, d.search_rank or 99))[:5],
        known_owner_teams=teams,
    )


class PublishRequest(BaseModel):
    spec: str = Field(max_length=MAX_SPEC_BYTES)
    domain: str
    owner_team: str = Field(pattern=r"^[a-z][a-z0-9-]{1,60}$")
    submitted_by: str = Field(max_length=200)
    decision: str = Field(pattern=r"^(auto_accepted|human_approved)$")
    reviewed_by: str | None = Field(None, max_length=200)


class PublishResult(BaseModel):
    api_id: str
    major_version: int
    path: str
    ingest: dict[str, Any]


def publish(
    session: Session, embedder: EmbeddingProvider, body: PublishRequest, catalog_dir: Path, registrations_dir: Path
) -> PublishResult:
    if body.domain not in KNOWN_DOMAINS:
        raise RegistrationError(f"unknown domain {body.domain!r}")
    if body.decision == "human_approved" and not body.reviewed_by:
        raise RegistrationError("human_approved registrations need reviewed_by")
    result = check(session, None, body.spec)
    if not result.valid or result.api_id is None or result.major_version is None:
        raise RegistrationError("; ".join(result.errors) or "invalid spec")
    if result.version_exists:
        raise RegistrationError(f"{result.api_id} v{result.major_version} already exists; bump the major version")
    if result.content_warnings and body.decision != "human_approved":
        raise RegistrationError("specs with content warnings can only be published after human review")

    doc = parse_spec(body.spec)
    ext = dict(doc["info"].get("x-nordlys") or {})
    ext.update(
        {
            "api-id": result.api_id,
            "domain": body.domain,
            "owner-team": body.owner_team,
            "countries": ext.get("countries") or result.countries,
            "audience": ext.get("audience", "internal"),
            "data-classification": ext.get("data-classification", "personal"),
            "support": ext.get("support", f"#team-{body.owner_team}"),
            "lifecycle": ext.get("lifecycle") or {"status": "active"},
            "registration": {
                "submitted-by": body.submitted_by,
                "decision": body.decision,
                "reviewed-by": body.reviewed_by,
            },
        }
    )
    doc["info"]["x-nordlys"] = ext
    target = registrations_dir / "apis" / body.domain / f"{result.api_id}.v{result.major_version}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    report: IngestReport = ingest_catalog(session, catalog_dir, embedder, extra_roots=[registrations_dir])
    return PublishResult(
        api_id=result.api_id,
        major_version=result.major_version,
        path=str(target.relative_to(registrations_dir)),
        ingest={"apis": report.apis, "chunks": report.chunks, "errors": report.errors},
    )
