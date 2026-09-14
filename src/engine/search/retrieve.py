"""Retrieval: BM25, semantic, and hybrid fusion.

The three modes are deliberately swappable behind one signature so Day 3 can
benchmark them against the same labeled query set and produce a comparison that
means something.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from engine.config import settings
from engine.search import embeddings as emb
from engine.search.index import FTS_TABLE
from engine.search.text import build_match_expression, normalize_query

log = logging.getLogger(__name__)

Mode = Literal["bm25", "vector", "hybrid"]

# Positional weights for bm25(), matching FTS_COLUMNS order. A term in a
# one-line summary is a far stronger signal than the same term buried in a
# 2000-character description, and provider/tag matches are weak on their own.
BM25_WEIGHTS = (0.0, 10.0, 8.0, 6.0, 1.5, 3.0, 2.0)

DETAIL_COLUMNS = """
    e.endpoint_id, e.method, e.path, e.summary, e.operation_id,
    e.is_deprecated, e.is_destructive, e.servers,
    a.name AS api_name, p.provider_id, p.name AS provider_name
"""


@dataclass
class Candidate:
    endpoint_id: str
    method: str
    path: str
    summary: str | None
    operation_id: str | None
    api_name: str
    provider_id: str
    provider_name: str
    is_deprecated: bool
    is_destructive: bool
    score: float
    rank: int
    matched_terms: list[str] = field(default_factory=list)
    component_ranks: dict[str, int] = field(default_factory=dict)


@dataclass
class SearchResult:
    query: str
    mode: str
    candidates: list[Candidate]
    took_ms: float
    total_considered: int


def _filter_clause(provider: str | None, method: str | None, include_deprecated: bool) -> str:
    clauses = []
    if provider:
        clauses.append("p.provider_id = :provider")
    if method:
        clauses.append("upper(e.method) = :method")
    if not include_deprecated:
        clauses.append("e.is_deprecated = 0")
    return (" AND " + " AND ".join(clauses)) if clauses else ""


def _bind(provider: str | None, method: str | None) -> dict[str, str]:
    params: dict[str, str] = {}
    if provider:
        params["provider"] = provider
    if method:
        params["method"] = method.upper()
    return params


def _row_to_candidate(row, score: float, rank: int) -> Candidate:
    return Candidate(
        endpoint_id=row.endpoint_id,
        method=row.method,
        path=row.path,
        summary=row.summary,
        operation_id=row.operation_id,
        api_name=row.api_name,
        provider_id=row.provider_id,
        provider_name=row.provider_name,
        is_deprecated=bool(row.is_deprecated),
        is_destructive=bool(row.is_destructive),
        score=score,
        rank=rank,
    )


# ------------------------------------------------------------------- lexical


def bm25_search(
    session: Session,
    query: str,
    *,
    limit: int = 20,
    provider: str | None = None,
    method: str | None = None,
    include_deprecated: bool = False,
) -> list[Candidate]:
    match_expression = build_match_expression(query, expand=settings.search_expand_synonyms)
    if not match_expression:
        return []

    weights = ", ".join(str(w) for w in BM25_WEIGHTS)
    sql = text(
        f"""
        SELECT {DETAIL_COLUMNS}, bm25({FTS_TABLE}, {weights}) AS bm25_score
        FROM {FTS_TABLE}
        JOIN endpoints e ON e.endpoint_id = {FTS_TABLE}.endpoint_id
        JOIN apis a ON a.api_id = e.api_id
        JOIN providers p ON p.provider_id = a.provider_id
        WHERE {FTS_TABLE} MATCH :match
          {_filter_clause(provider, method, include_deprecated)}
        ORDER BY bm25_score ASC
        LIMIT :limit
        """
    )
    params = {"match": match_expression, "limit": limit, **_bind(provider, method)}
    rows = session.execute(sql, params).all()

    query_terms = set(normalize_query(query))
    candidates: list[Candidate] = []
    for rank, row in enumerate(rows, start=1):
        # SQLite's bm25() is negative, more negative meaning a better match.
        candidate = _row_to_candidate(row, score=-float(row.bm25_score), rank=rank)
        haystack = f"{row.summary or ''} {row.path} {row.operation_id or ''}".lower()
        candidate.matched_terms = sorted(term for term in query_terms if term in haystack)
        candidates.append(candidate)
    return candidates


# ------------------------------------------------------------------ semantic


def _fetch_details(session: Session, endpoint_ids: list[str]) -> dict[str, object]:
    if not endpoint_ids:
        return {}
    placeholders = ", ".join(f":id{i}" for i in range(len(endpoint_ids)))
    sql = text(
        f"""
        SELECT {DETAIL_COLUMNS}
        FROM endpoints e
        JOIN apis a ON a.api_id = e.api_id
        JOIN providers p ON p.provider_id = a.provider_id
        WHERE e.endpoint_id IN ({placeholders})
        """
    )
    params = {f"id{i}": value for i, value in enumerate(endpoint_ids)}
    return {row.endpoint_id: row for row in session.execute(sql, params).all()}


def vector_search(
    session: Session,
    query: str,
    *,
    limit: int = 20,
    provider: str | None = None,
    method: str | None = None,
    include_deprecated: bool = False,
) -> list[Candidate]:
    store = emb.load_vector_store()
    query_vector = emb.embed_query(query)

    # Over-fetch so post-filtering still leaves a full result set.
    overfetch = limit * 5 if (provider or method or not include_deprecated) else limit
    hits = store.search(query_vector, min(overfetch, len(store)))

    details = _fetch_details(session, [endpoint_id for endpoint_id, _ in hits])
    candidates: list[Candidate] = []
    for endpoint_id, score in hits:
        row = details.get(endpoint_id)
        if row is None:
            continue
        if provider and row.provider_id != provider:
            continue
        if method and row.method.upper() != method.upper():
            continue
        if not include_deprecated and row.is_deprecated:
            continue
        candidates.append(_row_to_candidate(row, score=score, rank=len(candidates) + 1))
        if len(candidates) >= limit:
            break
    return candidates


# -------------------------------------------------------------------- hybrid


def reciprocal_rank_fusion(
    rankings: dict[str, list[Candidate]], *, k: int | None = None, limit: int = 20
) -> list[Candidate]:
    """Fuse ranked lists by rank rather than by score.

    BM25 scores and cosine similarities live on incomparable scales, so adding
    or averaging them requires a normalisation step that is itself a tuning
    problem. RRF sidesteps that: only the ordinal position matters, which makes
    the fusion robust and gives it exactly one parameter.
    """
    k = k or settings.rrf_k
    fused: dict[str, float] = {}
    best: dict[str, Candidate] = {}
    components: dict[str, dict[str, int]] = {}

    for source, candidates in rankings.items():
        for candidate in candidates:
            fused[candidate.endpoint_id] = fused.get(candidate.endpoint_id, 0.0) + 1.0 / (
                k + candidate.rank
            )
            components.setdefault(candidate.endpoint_id, {})[source] = candidate.rank
            existing = best.get(candidate.endpoint_id)
            if existing is None or candidate.rank < existing.rank:
                best[candidate.endpoint_id] = candidate

    ordered = sorted(fused.items(), key=lambda item: item[1], reverse=True)[:limit]
    results: list[Candidate] = []
    for rank, (endpoint_id, score) in enumerate(ordered, start=1):
        candidate = best[endpoint_id]
        merged = Candidate(
            endpoint_id=candidate.endpoint_id,
            method=candidate.method,
            path=candidate.path,
            summary=candidate.summary,
            operation_id=candidate.operation_id,
            api_name=candidate.api_name,
            provider_id=candidate.provider_id,
            provider_name=candidate.provider_name,
            is_deprecated=candidate.is_deprecated,
            is_destructive=candidate.is_destructive,
            score=score,
            rank=rank,
            matched_terms=candidate.matched_terms,
            component_ranks=components.get(endpoint_id, {}),
        )
        results.append(merged)
    return results


def hybrid_search(
    session: Session,
    query: str,
    *,
    limit: int = 20,
    provider: str | None = None,
    method: str | None = None,
    include_deprecated: bool = False,
) -> list[Candidate]:
    pool = max(limit * 3, 50)
    kwargs = {
        "limit": pool,
        "provider": provider,
        "method": method,
        "include_deprecated": include_deprecated,
    }
    lexical = bm25_search(session, query, **kwargs)
    try:
        semantic = vector_search(session, query, **kwargs)
    except emb.EmbeddingError as err:
        # Degrading to lexical-only is strictly better than failing the request,
        # but it must be visible rather than silent.
        log.warning("semantic retrieval unavailable, falling back to BM25 only: %s", err)
        return lexical[:limit]

    return reciprocal_rank_fusion({"bm25": lexical, "vector": semantic}, limit=limit)


# --------------------------------------------------------------------- entry


SEARCHERS = {"bm25": bm25_search, "vector": vector_search, "hybrid": hybrid_search}


def search(
    session: Session,
    query: str,
    *,
    mode: Mode = "hybrid",
    limit: int | None = None,
    provider: str | None = None,
    method: str | None = None,
    include_deprecated: bool = False,
) -> SearchResult:
    if mode not in SEARCHERS:
        raise ValueError(f"unknown mode {mode!r}; expected one of {sorted(SEARCHERS)}")
    limit = limit or settings.search_top_k

    started = time.perf_counter()
    candidates = SEARCHERS[mode](
        session,
        query,
        limit=limit,
        provider=provider,
        method=method,
        include_deprecated=include_deprecated,
    )
    took_ms = (time.perf_counter() - started) * 1000

    return SearchResult(
        query=query,
        mode=mode,
        candidates=candidates,
        took_ms=round(took_ms, 2),
        total_considered=len(candidates),
    )
