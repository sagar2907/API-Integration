"""Deterministic identifiers.

Endpoint IDs are opaque short hashes rather than readable slugs, on purpose: the
planning model references endpoints *by ID*, and an ID it cannot guess is an ID it
cannot fabricate convincingly. Every ID is a pure function of its natural key, so
re-ingesting the same spec produces the same rows instead of duplicates.
"""

from __future__ import annotations

import hashlib
import re

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    return _SLUG_RE.sub("_", value.strip().lower()).strip("_")


def digest(*parts: str, length: int = 12) -> str:
    """A short, stable hash of the given parts.

    Every identifier in the system is built from this, so the same natural key
    always yields the same id and re-ingestion updates rows rather than
    duplicating them.
    """
    joined = "|".join(parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:length]


def provider_id(name: str) -> str:
    return slugify(name)


def api_id(provider: str, name: str, version: str) -> str:
    return f"api_{digest(provider, name, version)}"


def endpoint_id(api: str, method: str, path: str) -> str:
    return f"ep_{digest(api, method.upper(), path)}"


def parameter_id(endpoint: str, location: str, name: str) -> str:
    return f"pa_{digest(endpoint, location, name)}"


def schema_id(endpoint: str, direction: str, status_code: str) -> str:
    return f"sc_{digest(endpoint, direction, status_code)}"


def auth_scheme_id(api: str, scheme_name: str) -> str:
    return f"au_{digest(api, scheme_name)}"


def spec_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]
