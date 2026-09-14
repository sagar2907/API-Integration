"""Ingestion orchestration: cached specs in, normalized database rows out.

Every identifier is derived from its natural key, so re-running ingestion updates
rows in place rather than duplicating them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from engine import ids
from engine.config import settings
from engine.db import (
    Api,
    AuthScheme,
    Endpoint,
    IngestRun,
    Parameter,
    Provider,
    SchemaRecord,
    init_db,
    session_scope,
    utcnow,
)
from engine.ingest.fetch import fetch_all
from engine.ingest.parse import ParsedApi, SpecError, parse_spec

log = logging.getLogger(__name__)


@dataclass
class IngestStats:
    attempted: int = 0
    ok: int = 0
    failed: int = 0
    apis: int = 0
    endpoints: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.apis} APIs, {self.endpoints} endpoints, "
            f"{self.ok}/{self.attempted} specs parsed, {self.failed} skipped"
        )


def _write_api(session, parsed: ParsedApi, *, source_url: str | None, spec_hash: str) -> int:
    """Upsert one API and its endpoints. Returns the endpoint count written."""
    prov_id = ids.provider_id(parsed.provider_name)
    session.merge(
        Provider(
            provider_id=prov_id,
            name=parsed.provider_name,
            base_url=parsed.base_url,
            documentation_url=parsed.documentation_url,
            auth_type=parsed.auth_schemes[0].scheme if parsed.auth_schemes else None,
            allowlisted=prov_id in {ids.provider_id(p) for p in settings.allowlisted_providers},
        )
    )

    a_id = ids.api_id(prov_id, parsed.title, parsed.version)
    session.merge(
        Api(
            api_id=a_id,
            provider_id=prov_id,
            name=parsed.title,
            version=parsed.version,
            description=(parsed.description or "")[:4000] or None,
            source_url=source_url,
            spec_hash=spec_hash,
        )
    )

    for scheme in parsed.auth_schemes:
        session.merge(
            AuthScheme(
                auth_scheme_id=ids.auth_scheme_id(a_id, scheme.name),
                api_id=a_id,
                name=scheme.name,
                scheme=scheme.scheme,
                location=scheme.location,
                parameter_name=scheme.parameter_name,
                scopes=scheme.scopes,
                supported=scheme.supported,
            )
        )

    written = 0
    for endpoint in parsed.endpoints:
        e_id = ids.endpoint_id(a_id, endpoint.method, endpoint.path)
        session.merge(
            Endpoint(
                endpoint_id=e_id,
                api_id=a_id,
                method=endpoint.method,
                path=endpoint.path,
                operation_id=endpoint.operation_id,
                summary=(endpoint.summary or "")[:2000] or None,
                description=(endpoint.description or "")[:4000] or None,
                tags=endpoint.tags,
                servers=endpoint.servers,
                security=endpoint.security,
                is_deprecated=endpoint.is_deprecated,
                is_destructive=endpoint.is_destructive,
            )
        )
        for param in endpoint.parameters:
            session.merge(
                Parameter(
                    parameter_id=ids.parameter_id(e_id, param.location, param.name),
                    endpoint_id=e_id,
                    location=param.location,
                    name=param.name,
                    type=param.type,
                    required=param.required,
                    description=(param.description or "")[:1000] or None,
                )
            )
        for schema in endpoint.schemas:
            session.merge(
                SchemaRecord(
                    schema_id=ids.schema_id(e_id, schema.direction, schema.status_code),
                    endpoint_id=e_id,
                    direction=schema.direction,
                    status_code=schema.status_code,
                    content_type=schema.content_type,
                    json_schema=schema.json_schema,
                )
            )
        written += 1

    return written


def ingest_cached_specs(limit: int | None = None) -> IngestStats:
    """Parse every cached spec into the database."""
    init_db()
    stats = IngestStats()
    paths = sorted(settings.specs_dir.glob("*.json"))
    if limit:
        paths = paths[:limit]

    for path in paths:
        stats.attempted += 1
        try:
            raw = path.read_bytes()
            spec = json.loads(raw)
            parsed = parse_spec(spec, max_endpoints=settings.ingest_max_endpoints_per_api)
        except (SpecError, json.JSONDecodeError, ValueError, OSError) as err:
            stats.failed += 1
            stats.failures.append({"spec": path.name, "error": f"{type(err).__name__}: {err}"})
            log.warning("skipped %s: %s", path.name, err)
            continue

        try:
            with session_scope() as session:
                written = _write_api(
                    session,
                    parsed,
                    source_url=None,
                    spec_hash=ids.spec_hash(raw),
                )
            stats.ok += 1
            stats.apis += 1
            stats.endpoints += written
        except Exception as err:  # a database problem on one spec, not a parse problem
            stats.failed += 1
            stats.failures.append({"spec": path.name, "error": f"write failed: {err}"})
            log.warning("write failed for %s: %s", path.name, err)

        if stats.attempted % 25 == 0:
            log.info("processed %d/%d specs", stats.attempted, len(paths))

    with session_scope() as session:
        session.add(
            IngestRun(
                finished_at=utcnow(),
                specs_attempted=stats.attempted,
                specs_ok=stats.ok,
                specs_failed=stats.failed,
                apis_written=stats.apis,
                endpoints_written=stats.endpoints,
                failures=stats.failures[:100],
            )
        )

    log.info("ingestion complete: %s", stats.summary())
    return stats


def full_run(limit: int | None = None, *, force: bool = False) -> IngestStats:
    """Download specs then ingest them."""
    fetch_all(limit=limit, force=force)
    return ingest_cached_specs()


def cached_spec_paths() -> list[Path]:
    return sorted(settings.specs_dir.glob("*.json"))
