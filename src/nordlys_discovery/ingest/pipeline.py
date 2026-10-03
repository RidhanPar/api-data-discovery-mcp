"""Idempotent catalog ingestion.

Re-running ingestion on an unchanged catalog does no writes and no embedding calls.
Change detection works at two levels:

  1. File level: sha256 of (chunker version, parsed document). Unchanged -> skip.
  2. Chunk level: sha256 of (chunker version, title, body), keyed by a stable
     chunk_key. Only new or changed chunks lose their embedding and get
     re-embedded; chunks that disappeared are deleted.

A chunk is also re-embedded when its stored `embedding_model` differs from the
active provider, so switching provider is just "re-run ingestion".

Invalid files are reported and skipped; they never cause existing rows to be
deleted (a typo in one spec must not wipe that API from the catalog).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.orm import Session

from ..catalog.loader import CatalogError, load_api_spec, load_data_product
from ..catalog.metadata import ApiMetadata, extract_metadata
from ..catalog.models import DataProduct
from ..db.models import ApiSpec, Chunk, DataProductRow
from ..embeddings import EmbeddingProvider
from ..security.injection import SCANNER_VERSION, scan_document
from .chunking import CHUNKER_VERSION, ChunkDraft, api_chunks, data_product_chunks, sha256_json

log = logging.getLogger(__name__)
INGEST_LOCK_ID = 7_340_001  # arbitrary constant for pg_advisory_xact_lock


@dataclass
class IngestReport:
    apis: dict[str, int] = field(default_factory=lambda: {"added": 0, "updated": 0, "unchanged": 0, "deleted": 0})
    data_products: dict[str, int] = field(
        default_factory=lambda: {"added": 0, "updated": 0, "unchanged": 0, "deleted": 0}
    )
    chunks: dict[str, int] = field(
        default_factory=lambda: {"added": 0, "updated": 0, "unchanged": 0, "deleted": 0, "embedded": 0}
    )
    errors: list[str] = field(default_factory=list)
    embedding_model: str = ""
    duration_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _api_pii(meta_classification: str | None) -> str | None:
    return meta_classification if meta_classification in {"none", "internal", "personal", "sensitive"} else None


def _sync_chunks(
    session: Session,
    drafts: list[ChunkDraft],
    *,
    parent: dict[str, int],
    filters: dict[str, Any],
    report: IngestReport,
) -> None:
    """Upsert drafts for one parent (api_spec or data_product) and delete stale chunks."""
    existing = {c.chunk_key: c for c in session.scalars(select(Chunk).filter_by(**parent))}
    seen: set[str] = set()
    for d in drafts:
        seen.add(d.key)
        row = existing.get(d.key)
        values = dict(
            kind=d.kind, title=d.title, body=d.body, method=d.method, path=d.path, field_name=d.field_name, **filters
        )
        if row is None:
            session.add(Chunk(chunk_key=d.key, content_hash=d.content_hash, **parent, **values))
            report.chunks["added"] += 1
        elif row.content_hash != d.content_hash:
            for k, v in values.items():
                setattr(row, k, v)
            row.content_hash, row.embedding, row.embedding_model = d.content_hash, None, None
            report.chunks["updated"] += 1
        else:
            for k, v in filters.items():  # cheap; keeps denormalised filters exact
                setattr(row, k, v)
            report.chunks["unchanged"] += 1
    stale = [c for k, c in existing.items() if k not in seen]
    for c in stale:
        session.delete(c)
    report.chunks["deleted"] += len(stale)


def _ingest_api(session: Session, path: Path, rel: str, report: IngestReport, seen: set[tuple[str, int]]) -> None:
    spec = load_api_spec(path)
    meta: ApiMetadata = extract_metadata(spec.document, path)
    ident = (meta.api_id, meta.major_version)
    if ident in seen:
        raise CatalogError(path, f"duplicate api {meta.api_id} v{meta.major_version}")
    seen.add(ident)

    content_hash = sha256_json([CHUNKER_VERSION, SCANNER_VERSION, spec.document])
    row = session.scalar(select(ApiSpec).filter_by(api_id=meta.api_id, major_version=meta.major_version))
    if row is not None and row.content_hash == content_hash and row.source_path == rel:
        report.apis["unchanged"] += 1
        return

    info = spec.document.get("info", {})
    fields: dict[str, Any] = dict(
        version=meta.version,
        title=meta.title,
        description=info.get("description"),
        domain=meta.domain,
        owner_team=meta.owner_team,
        countries=list(meta.countries),
        audience=meta.audience,
        data_classification=(info.get("x-nordlys") or {}).get("data-classification"),
        lifecycle_status=meta.lifecycle_status,
        sunset=meta.sunset,
        replacement=meta.replacement,
        metadata_provenance=meta.provenance,
        content_warnings=scan_document(spec.document),
        source_path=rel,
        content_hash=content_hash,
        raw=spec.document,
    )
    if row is None:
        row = ApiSpec(api_id=meta.api_id, major_version=meta.major_version, **fields)
        session.add(row)
        session.flush()
        report.apis["added"] += 1
    else:
        for k, v in fields.items():
            setattr(row, k, v)
        report.apis["updated"] += 1

    _sync_chunks(
        session,
        api_chunks(meta, spec.document),
        parent={"api_spec_id": row.id},
        filters=dict(
            asset_type="api",
            asset_id=meta.api_id,
            major_version=meta.major_version,
            domain=meta.domain,
            countries=list(meta.countries),
            deprecated=meta.deprecated,
            pii_level=_api_pii(fields["data_classification"]),
        ),
        report=report,
    )


def _ingest_product(session: Session, path: Path, rel: str, report: IngestReport, seen: set[str]) -> None:
    dp: DataProduct = load_data_product(path)
    if dp.id in seen:
        raise CatalogError(path, f"duplicate data product {dp.id}")
    seen.add(dp.id)
    raw = dp.model_dump(mode="json", by_alias=True)
    content_hash = sha256_json([CHUNKER_VERSION, SCANNER_VERSION, raw])
    row = session.scalar(select(DataProductRow).filter_by(product_id=dp.id))
    if row is not None and row.content_hash == content_hash and row.source_path == rel:
        report.data_products["unchanged"] += 1
        return

    fields: dict[str, Any] = dict(
        name=dp.name,
        version=dp.version,
        status=dp.status,
        domain=dp.domain,
        owner_team=dp.owner.team,
        countries=list(dp.countries),
        pii_classification=dp.pii_classification.value,
        description=dp.description,
        content_warnings=scan_document(raw),
        source_path=rel,
        content_hash=content_hash,
        raw=raw,
    )
    if row is None:
        row = DataProductRow(product_id=dp.id, **fields)
        session.add(row)
        session.flush()
        report.data_products["added"] += 1
    else:
        for k, v in fields.items():
            setattr(row, k, v)
        report.data_products["updated"] += 1

    _sync_chunks(
        session,
        data_product_chunks(dp),
        parent={"data_product_id": row.id},
        filters=dict(
            asset_type="data_product",
            asset_id=dp.id,
            major_version=None,
            domain=dp.domain,
            countries=list(dp.countries),
            deprecated=dp.status == "deprecated",
            pii_level=dp.pii_classification.value,
        ),
        report=report,
    )


def _embed_pending(session: Session, embedder: EmbeddingProvider, report: IngestReport, batch: int = 64) -> None:
    pending = select(Chunk).where(or_(Chunk.embedding.is_(None), Chunk.embedding_model != embedder.model_id))
    while True:
        rows = list(session.scalars(pending.order_by(Chunk.id).limit(batch)))
        if not rows:
            return
        vectors = embedder.embed_documents([f"{r.title}\n{r.body}" for r in rows])
        for r, v in zip(rows, vectors, strict=True):
            r.embedding, r.embedding_model = v, embedder.model_id
        session.flush()
        report.chunks["embedded"] += len(rows)


def ingest_catalog(
    session: Session,
    catalog_dir: Path,
    embedder: EmbeddingProvider,
    *,
    prune: bool = True,
    extra_roots: Sequence[Path] = (),
) -> IngestReport:
    """Ingest `catalog_dir` plus any `extra_roots` (e.g. APIs registered through Workflow B).

    Each root has the same layout (apis/**, data-products/*). Pruning considers all roots
    together, so a full re-ingest never deletes a registered API.
    """
    started = time.perf_counter()
    report = IngestReport(embedding_model=embedder.model_id)
    # Serialise concurrent ingestion runs (e.g. two replicas starting at once).
    session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": INGEST_LOCK_ID})

    roots = [(catalog_dir, "")] + [(r, f"{r.name}/") for r in extra_roots if r.exists()]
    failed_paths: set[str] = set()
    seen_apis: set[tuple[str, int]] = set()
    api_files = [
        (p, prefix + str(p.relative_to(root)))
        for root, prefix in roots
        for p in sorted((root / "apis").rglob("*.yaml"))
    ]
    for path, rel in api_files:
        try:
            with session.begin_nested():
                _ingest_api(session, path, rel, report, seen_apis)
        except CatalogError as exc:
            failed_paths.add(rel)
            report.errors.append(str(exc))
            log.warning("skipping invalid spec", extra={"path": rel, "error": str(exc)})

    seen_products: set[str] = set()
    dp_files = [
        (p, prefix + str(p.relative_to(root)))
        for root, prefix in roots
        for p in sorted((root / "data-products").glob("*.yaml"))
    ]
    for path, rel in dp_files:
        try:
            with session.begin_nested():
                _ingest_product(session, path, rel, report, seen_products)
        except CatalogError as exc:
            failed_paths.add(rel)
            report.errors.append(str(exc))
            log.warning("skipping invalid data product", extra={"path": rel, "error": str(exc)})

    if prune:
        # Remove assets whose files are gone - but never ones whose file merely failed to parse.
        for api in list(session.scalars(select(ApiSpec))):
            if (api.api_id, api.major_version) not in seen_apis and api.source_path not in failed_paths:
                report.apis["deleted"] += 1
                report.chunks["deleted"] += (
                    session.scalar(select(func.count()).select_from(Chunk).where(Chunk.api_spec_id == api.id)) or 0
                )
                session.delete(api)
        for dp in list(session.scalars(select(DataProductRow))):
            if dp.product_id not in seen_products and dp.source_path not in failed_paths:
                report.data_products["deleted"] += 1
                report.chunks["deleted"] += (
                    session.scalar(select(func.count()).select_from(Chunk).where(Chunk.data_product_id == dp.id)) or 0
                )
                session.delete(dp)
        session.flush()

    _embed_pending(session, embedder, report)
    report.duration_s = round(time.perf_counter() - started, 2)
    return report


def reset_embeddings(session: Session) -> int:
    """Force a full re-embed on the next ingestion (e.g. after a model upgrade)."""
    result = session.execute(update(Chunk).values(embedding=None, embedding_model=None))
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def purge_all(session: Session) -> None:
    session.execute(delete(Chunk))
    session.execute(delete(ApiSpec))
    session.execute(delete(DataProductRow))
