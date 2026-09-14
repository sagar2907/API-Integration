"""SQLAlchemy models and session handling.

SQLite for the prototype. The models are ordinary SQLAlchemy, so moving to
PostgreSQL is a connection-string change plus a replacement for the FTS5 search
index (see search/index.py).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from engine.config import settings


def utcnow() -> datetime:
    return datetime.now(UTC)


def naive_utcnow() -> datetime:
    """UTC with the tzinfo stripped.

    SQLite stores datetimes without timezone information, so comparisons must
    all use the same convention. Mixing naive-local with naive-UTC silently
    shifts every comparison by the local offset — which, for a lease check,
    means an expired lease can look like a future one.
    """
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Provider(Base):
    __tablename__ = "providers"

    provider_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    base_url: Mapped[str | None] = mapped_column(String(512))
    documentation_url: Mapped[str | None] = mapped_column(String(512))
    auth_type: Mapped[str | None] = mapped_column(String(64))
    allowlisted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=naive_utcnow)

    apis: Mapped[list[Api]] = relationship(back_populates="provider", cascade="all, delete-orphan")


class Api(Base):
    __tablename__ = "apis"

    api_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.provider_id"), index=True)
    name: Mapped[str] = mapped_column(String(256))
    version: Mapped[str] = mapped_column(String(64), default="")
    description: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(String(512))
    spec_hash: Mapped[str | None] = mapped_column(String(64))
    is_deprecated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=naive_utcnow)

    provider: Mapped[Provider] = relationship(back_populates="apis")
    endpoints: Mapped[list[Endpoint]] = relationship(
        back_populates="api", cascade="all, delete-orphan"
    )
    auth_schemes: Mapped[list[AuthScheme]] = relationship(
        back_populates="api", cascade="all, delete-orphan"
    )


class Endpoint(Base):
    __tablename__ = "endpoints"

    endpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    api_id: Mapped[str] = mapped_column(ForeignKey("apis.api_id"), index=True)
    method: Mapped[str] = mapped_column(String(16), index=True)
    path: Mapped[str] = mapped_column(String(512))
    operation_id: Mapped[str | None] = mapped_column(String(256))
    summary: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    servers: Mapped[list[str]] = mapped_column(JSON, default=list)
    security: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_deprecated: Mapped[bool] = mapped_column(Boolean, default=False)
    is_destructive: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=naive_utcnow)

    api: Mapped[Api] = relationship(back_populates="endpoints")
    parameters: Mapped[list[Parameter]] = relationship(
        back_populates="endpoint", cascade="all, delete-orphan"
    )
    schemas: Mapped[list[SchemaRecord]] = relationship(
        back_populates="endpoint", cascade="all, delete-orphan"
    )


class Parameter(Base):
    __tablename__ = "parameters"

    parameter_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    endpoint_id: Mapped[str] = mapped_column(ForeignKey("endpoints.endpoint_id"), index=True)
    location: Mapped[str] = mapped_column(String(16))  # path | query | header | cookie
    name: Mapped[str] = mapped_column(String(256))
    type: Mapped[str | None] = mapped_column(String(64))
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(Text)

    endpoint: Mapped[Endpoint] = relationship(back_populates="parameters")


class SchemaRecord(Base):
    __tablename__ = "schemas"

    schema_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    endpoint_id: Mapped[str] = mapped_column(ForeignKey("endpoints.endpoint_id"), index=True)
    direction: Mapped[str] = mapped_column(String(16))  # request | response
    status_code: Mapped[str] = mapped_column(String(16), default="")
    content_type: Mapped[str] = mapped_column(String(128), default="application/json")
    json_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    endpoint: Mapped[Endpoint] = relationship(back_populates="schemas")


class AuthScheme(Base):
    __tablename__ = "auth_schemes"

    auth_scheme_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    api_id: Mapped[str] = mapped_column(ForeignKey("apis.api_id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    scheme: Mapped[str] = mapped_column(String(64))  # apikey | bearer | oauth2 | basic | other
    location: Mapped[str | None] = mapped_column(String(32))
    parameter_name: Mapped[str | None] = mapped_column(String(128))
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    supported: Mapped[bool] = mapped_column(Boolean, default=True)

    api: Mapped[Api] = relationship(back_populates="auth_schemes")


class Workflow(Base):
    """A workflow definition.

    Versioned by insert, never by update: a new version is a new row, so an
    execution always points at the exact definition it ran.
    """

    __tablename__ = "workflows"

    workflow_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), default="local", index=True)
    name: Mapped[str] = mapped_column(String(256))
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    parent_version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=naive_utcnow)

    nodes: Mapped[list[WorkflowNodeRow]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan"
    )


class WorkflowNodeRow(Base):
    """Denormalized node rows.

    The definition JSON is the source of truth; these exist so questions like
    "which workflows call this endpoint?" are a query rather than a scan.
    """

    __tablename__ = "workflow_nodes"

    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.workflow_id"), index=True)
    type: Mapped[str] = mapped_column(String(32))
    endpoint_id: Mapped[str | None] = mapped_column(String(64), index=True)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    workflow: Mapped[Workflow] = relationship(back_populates="nodes")


class CompiledIR(Base):
    """The immutable, validated form an execution actually runs.

    Compilation output is stored rather than recomputed, so a run is
    reproducible and the language model stays out of the execution path.
    """

    __tablename__ = "compiled_ir"

    ir_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.workflow_id"), index=True)
    workflow_version: Mapped[int] = mapped_column(Integer, default=1)
    ir: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    compiled_at: Mapped[datetime] = mapped_column(DateTime, default=naive_utcnow)


class Credential(Base):
    """An encrypted provider secret.

    Only the ciphertext is stored. The plaintext exists in memory for the
    duration of one HTTP call and is never logged, never persisted, and never
    placed in a prompt.
    """

    __tablename__ = "credentials"

    credential_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), default="local", index=True)
    provider_id: Mapped[str] = mapped_column(String(128), index=True)
    scheme: Mapped[str] = mapped_column(String(32))
    label: Mapped[str | None] = mapped_column(String(128))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    # OAuth access tokens expire within the hour. The refresh token is the
    # durable credential and is encrypted separately, so an expired access
    # token can be replaced without asking the user to sign in again.
    refresh_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    oauth_provider: Mapped[str | None] = mapped_column(String(32))
    key_version: Mapped[int] = mapped_column(Integer, default=1)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=naive_utcnow)


class CredentialAudit(Base):
    """Append-only record of every credential use."""

    __tablename__ = "credential_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    credential_id: Mapped[str] = mapped_column(String(64), index=True)
    execution_id: Mapped[str | None] = mapped_column(String(64), index=True)
    node_id: Mapped[str | None] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column(DateTime, default=naive_utcnow)
    action: Mapped[str] = mapped_column(String(32))


class Execution(Base):
    __tablename__ = "executions"

    execution_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(String(64), index=True)
    ir_id: Mapped[str | None] = mapped_column(String(64))
    trigger_type: Mapped[str] = mapped_column(String(32), default="manual")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    error_category: Mapped[str | None] = mapped_column(String(32))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=naive_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class NodeResult(Base):
    """A completed node's output — the checkpoint that makes resumption possible.

    Written before the node is marked done, so a crash between the two replays
    the node rather than losing it. Combined with an idempotency key, replay is
    safe.
    """

    __tablename__ = "node_results"

    execution_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="succeeded")
    status_code: Mapped[int | None] = mapped_column(Integer)
    output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    completed_at: Mapped[datetime] = mapped_column(DateTime, default=naive_utcnow)


class ExecutionEvent(Base):
    """Append-only execution log.

    payload_metadata stores shapes, sizes and hashes — never raw bodies, which
    may carry user data or secrets.
    """

    __tablename__ = "execution_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(String(64), index=True)
    node_id: Mapped[str | None] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column(DateTime, default=naive_utcnow)
    event_type: Mapped[str] = mapped_column(String(32))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status_code: Mapped[int | None] = mapped_column(Integer)
    error_category: Mapped[str | None] = mapped_column(String(32))
    payload_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Job(Base):
    """A queued execution.

    A database table rather than a message broker: the guarantees this project
    demonstrates — leased claiming, checkpoint before acknowledge, resumption —
    are the interesting part, and owning them explicitly is worth more here
    than delegating them to a queue.
    """

    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    claimed_by: Mapped[str | None] = mapped_column(String(64))
    deliveries: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=naive_utcnow)


class IngestRun(Base):
    __tablename__ = "ingest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=naive_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    specs_attempted: Mapped[int] = mapped_column(Integer, default=0)
    specs_ok: Mapped[int] = mapped_column(Integer, default=0)
    specs_failed: Mapped[int] = mapped_column(Integer, default=0)
    apis_written: Mapped[int] = mapped_column(Integer, default=0)
    endpoints_written: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)


_engine: Engine = create_engine(
    settings.sqlalchemy_url,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False}
    if settings.sqlalchemy_url.startswith("sqlite")
    else {},
)

SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


@event.listens_for(_engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]
    """WAL keeps the worker and the API from blocking each other on writes."""
    if not settings.sqlalchemy_url.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def get_engine() -> Engine:
    return _engine


def _add_missing_columns() -> None:
    """Add columns that create_all cannot add to an existing table.

    SQLite's create_all only creates missing *tables*, so a new column on a
    table that already exists is silently absent until something queries it.
    ALTER TABLE ADD COLUMN is safe and idempotent enough for a prototype; a
    production build would use a migration tool.
    """
    wanted = {
        "credentials": {
            "refresh_ciphertext": "BLOB",
            "oauth_provider": "VARCHAR(32)",
        },
    }
    with _engine.begin() as connection:
        for table, columns in wanted.items():
            existing = {
                row[1]
                for row in connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            }
            if not existing:
                continue  # table not created yet; create_all will handle it
            for column, sql_type in columns.items():
                if column not in existing:
                    connection.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"
                    )


def init_db() -> None:
    Base.metadata.create_all(_engine)
    _add_missing_columns()


def reset_db() -> None:
    Base.metadata.drop_all(_engine)
    Base.metadata.create_all(_engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
