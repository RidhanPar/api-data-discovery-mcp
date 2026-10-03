"""Hybrid retrieval: BM25 over Postgres full-text + pgvector cosine, fused with RRF.

Why both: lexical search nails exact identifiers ("KID", "claimId", "FNOL",
"Autogiro") that embeddings blur; vector search catches paraphrases ("money back
when I cancel" -> refund calculation) that share no words with the spec.
Reciprocal rank fusion combines the two ranked lists without having to calibrate
their incomparable scores: score(d) = sum over retrievers of 1 / (k + rank(d)).

BM25 note: Postgres' built-in ts_rank is *not* BM25 (no IDF, different length
normalisation). We compute textbook BM25 in SQL from the stored tsvector: term
frequency = number of positions of the lexeme, document frequency counted over the
corpus at query time. That is exact and fast for thousands of chunks; at millions
you would keep a term-statistics table or use a BM25 extension (e.g. pg_search).
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..db.models import ApiSpec, Chunk
from ..embeddings import EmbeddingProvider
from .models import MatchedChunk, SearchFilters, SearchHit, SearchQuery, SearchResponse

SNIPPET_CHARS = 280


@dataclass(frozen=True)
class SearchParams:
    candidates: int = 50
    rrf_k: int = 60
    bm25_k1: float = 1.2
    bm25_b: float = 0.75
    hnsw_ef_search: int = 100


def _filter_sql(f: SearchFilters, alias: str = "c") -> tuple[str, dict[str, Any]]:
    """WHERE fragment over fixed column names; every value is a bound parameter."""
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if f.asset_type:
        clauses.append(f"{alias}.asset_type = :f_asset_type")
        params["f_asset_type"] = f.asset_type
    if f.domain:
        clauses.append(f"{alias}.domain = :f_domain")
        params["f_domain"] = f.domain
    if f.country:
        clauses.append(f":f_country = ANY({alias}.countries)")
        params["f_country"] = f.country
    if f.version is not None:
        clauses.append(f"{alias}.major_version = :f_version")
        params["f_version"] = f.version
    if f.deprecated is not None:
        clauses.append(f"{alias}.deprecated = :f_deprecated")
        params["f_deprecated"] = f.deprecated
    if f.pii_levels:
        clauses.append(f"{alias}.pii_level = ANY(:f_pii)")
        params["f_pii"] = [p.value for p in f.pii_levels]
    return (" AND ".join(clauses) or "TRUE"), params


def query_lexemes(session: Session, query: str) -> list[str]:
    """Stemmed, stop-word-free lexemes exactly as the index sees them."""
    lexemes = session.scalar(text("SELECT tsvector_to_array(to_tsvector('english', :q))"), {"q": query})
    return list(lexemes or [])


def lexical_search(
    session: Session, query: str, filters: SearchFilters, params: SearchParams
) -> list[tuple[int, float]]:
    """Top chunks by BM25. Returns (chunk_id, score) best first."""
    terms = query_lexemes(session, query)
    if not terms:
        return []
    where, fparams = _filter_sql(filters)
    sql = text(f"""
        WITH q(term) AS (SELECT unnest(CAST(:terms AS text[]))),
        corpus AS (SELECT count(*)::float AS n, avg(length(tsv))::float AS avgdl FROM chunk),
        df AS (
            SELECT u.lexeme AS term, count(*)::float AS ndoc
            FROM chunk, unnest(chunk.tsv) AS u
            WHERE u.lexeme = ANY(CAST(:terms AS text[]))
            GROUP BY u.lexeme
        ),
        tf AS (
            SELECT c.id, u.lexeme AS term,
                   coalesce(array_length(u.positions, 1), 1)::float AS f,
                   length(c.tsv)::float AS dl
            FROM chunk c, unnest(c.tsv) AS u
            WHERE c.tsv @@ to_tsquery('simple', :tsq)
              AND u.lexeme = ANY(CAST(:terms AS text[]))
              AND {where}
        )
        SELECT tf.id,
               sum(ln(1 + (corpus.n - df.ndoc + 0.5) / (df.ndoc + 0.5))
                   * tf.f * (:k1 + 1)
                   / (tf.f + :k1 * (1 - :b + :b * tf.dl / corpus.avgdl))) AS score
        FROM tf JOIN df USING (term) CROSS JOIN corpus
        GROUP BY tf.id
        ORDER BY score DESC, tf.id
        LIMIT :limit
    """)
    # Lexemes are already stemmed, so build an OR tsquery with the 'simple' config (no re-stemming).
    tsq = " | ".join("'" + t.replace("'", "''") + "'" for t in terms)
    rows = session.execute(
        sql,
        {"terms": terms, "tsq": tsq, "k1": params.bm25_k1, "b": params.bm25_b, "limit": params.candidates, **fparams},
    )
    return [(int(r.id), float(r.score)) for r in rows]


def vector_search(
    session: Session, query_vector: list[float], filters: SearchFilters, params: SearchParams
) -> list[tuple[int, float]]:
    """Top chunks by cosine similarity via the HNSW index. Returns (chunk_id, similarity)."""
    where, fparams = _filter_sql(filters)
    # Iterative scans keep fetching from the index when filters discard candidates (pgvector >= 0.8).
    session.execute(text(f"SET LOCAL hnsw.ef_search = {int(params.hnsw_ef_search)}"))
    session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    rows = session.execute(
        text(f"""
            SELECT c.id, 1 - (c.embedding <=> CAST(:v AS vector)) AS similarity
            FROM chunk c
            WHERE c.embedding IS NOT NULL AND {where}
            ORDER BY c.embedding <=> CAST(:v AS vector), c.id
            LIMIT :limit
        """),
        {"v": str(query_vector), "limit": params.candidates, **fparams},
    )
    return [(int(r.id), float(r.similarity)) for r in rows]


def reciprocal_rank_fusion(ranked_lists: list[list[int]], k: int = 60) -> dict[int, float]:
    """RRF (Cormack, Clarke & Buettcher, 2009). Ranks are 1-based."""
    scores: dict[int, float] = defaultdict(float)
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] += 1.0 / (k + rank)
    return dict(scores)


def _snippet(body: str) -> str:
    flat = " ".join(body.split())
    return flat if len(flat) <= SNIPPET_CHARS else flat[: SNIPPET_CHARS - 1].rsplit(" ", 1)[0] + "…"


def search(
    session: Session, embedder: EmbeddingProvider, q: SearchQuery, params: SearchParams | None = None
) -> SearchResponse:
    params = params or SearchParams()
    started = time.perf_counter()

    lexical = lexical_search(session, q.query, q.filters, params) if q.mode in ("hybrid", "lexical") else []
    vector = (
        vector_search(session, embedder.embed_query(q.query), q.filters, params)
        if q.mode in ("hybrid", "vector")
        else []
    )
    lex_rank = {cid: i for i, (cid, _) in enumerate(lexical, start=1)}
    vec_rank = {cid: i for i, (cid, _) in enumerate(vector, start=1)}
    fused = reciprocal_rank_fusion([[c for c, _ in lexical], [c for c, _ in vector]], k=params.rrf_k)

    chunks = {c.id: c for c in session.scalars(select(Chunk).where(Chunk.id.in_(fused)))} if fused else {}

    # Collapse chunks to assets (an API version or a data product); asset score = best chunk score.
    groups: dict[tuple[str, str, int | None], list[tuple[float, Chunk]]] = defaultdict(list)
    for cid, score in fused.items():
        c = chunks[cid]
        groups[(c.asset_type, c.asset_id, c.major_version)].append((score, c))
    ordered = sorted(groups.items(), key=lambda kv: (-max(s for s, _ in kv[1]), kv[0][1], kv[0][2] or 0))

    api_meta: dict[tuple[str, int | None], ApiSpec] = {}
    api_keys = [(a, v) for (t, a, v), _ in ordered[: q.limit] if t == "api"]
    if api_keys:
        for row in session.scalars(select(ApiSpec).where(ApiSpec.api_id.in_({a for a, _ in api_keys}))):
            api_meta[(row.api_id, row.major_version)] = row

    def matched(c: Chunk) -> MatchedChunk:
        return MatchedChunk(
            kind=c.kind,
            title=c.title,
            method=c.method,
            path=c.path,
            field_name=c.field_name,
            snippet=_snippet(c.body),
            lexical_rank=lex_rank.get(c.id),
            vector_rank=vec_rank.get(c.id),
        )

    hits: list[SearchHit] = []
    for (asset_type, asset_id, major), members in ordered[: q.limit]:
        members.sort(key=lambda sc: (-sc[0], sc[1].id))
        best = members[0][1]
        meta = api_meta.get((asset_id, major))
        hits.append(
            SearchHit(
                asset_type=asset_type,
                asset_id=asset_id,
                major_version=major,
                title=meta.title if meta else best.title.split(" (")[0],
                domain=best.domain,
                countries=list(best.countries),
                deprecated=best.deprecated,
                sunset=meta.sunset if meta else None,
                replacement=meta.replacement if meta else None,
                pii_level=best.pii_level,
                score=round(members[0][0], 6),
                best_match=matched(best),
                other_matches=[matched(c) for _, c in members[1:4]],
            )
        )
    return SearchResponse(
        query=q.query,
        mode=q.mode,
        filters=q.filters,
        total_assets=len(groups),
        hits=hits,
        took_ms=round((time.perf_counter() - started) * 1000, 1),
    )
