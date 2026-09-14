"""FTS5 full-text index construction.

SQLite ships FTS5 with a real BM25 implementation, so the lexical half of
retrieval needs no external search service. The index is rebuilt from the
endpoints table, which stays the single source of truth.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from engine.db import Api, Endpoint, Provider, get_engine, session_scope
from engine.search.text import endpoint_document

log = logging.getLogger(__name__)

FTS_TABLE = "endpoints_fts"

# Column order matters — the bm25() weights in retrieve.py are positional.
FTS_COLUMNS = ("summary", "operation", "path_tokens", "description", "tags", "provider")

CREATE_FTS = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5(
    endpoint_id UNINDEXED,
    {", ".join(FTS_COLUMNS)},
    tokenize="porter unicode61"
)
"""


def create_index(session: Session) -> None:
    session.execute(text(CREATE_FTS))


def drop_index(session: Session) -> None:
    session.execute(text(f"DROP TABLE IF EXISTS {FTS_TABLE}"))


def index_exists() -> bool:
    with session_scope() as session:
        row = session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
            {"name": FTS_TABLE},
        ).first()
        return row is not None


def indexed_count() -> int:
    if not index_exists():
        return 0
    with session_scope() as session:
        return session.execute(text(f"SELECT count(*) FROM {FTS_TABLE}")).scalar_one()


def build_index(batch_size: int = 1000) -> int:
    """Rebuild the full-text index from scratch. Returns rows indexed."""
    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(text(f"DROP TABLE IF EXISTS {FTS_TABLE}"))
        connection.execute(text(CREATE_FTS))

    insert_sql = text(
        f"INSERT INTO {FTS_TABLE} (endpoint_id, {', '.join(FTS_COLUMNS)}) "
        f"VALUES (:endpoint_id, :{', :'.join(FTS_COLUMNS)})"
    )

    total = 0
    with session_scope() as session:
        rows = session.execute(
            select(
                Endpoint.endpoint_id,
                Endpoint.summary,
                Endpoint.description,
                Endpoint.path,
                Endpoint.operation_id,
                Endpoint.tags,
                Provider.provider_id,
                Api.name,
            )
            .join(Api, Api.api_id == Endpoint.api_id)
            .join(Provider, Provider.provider_id == Api.provider_id)
        ).all()

        batch: list[dict[str, str]] = []
        for (
            endpoint_id,
            summary,
            description,
            path,
            operation_id,
            tags,
            provider_id,
            api_name,
        ) in rows:
            document = endpoint_document(
                summary=summary,
                description=description,
                path=path,
                operation_id=operation_id,
                tags=tags,
                provider=provider_id,
                api_name=api_name,
            )
            batch.append({"endpoint_id": endpoint_id, **document})
            if len(batch) >= batch_size:
                session.execute(insert_sql, batch)
                total += len(batch)
                batch = []
        if batch:
            session.execute(insert_sql, batch)
            total += len(batch)

    log.info("indexed %d endpoints into %s", total, FTS_TABLE)
    return total
