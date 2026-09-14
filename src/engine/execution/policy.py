"""Which providers may receive a real request.

The execution allowlist is a safety catch: the engine builds and sends requests
to third-party APIs on its own, and without a list a mis-planned workflow could
reach any of the indexed providers. It is deliberately separate from having a
credential, because some APIs need no authentication at all.

Two sources, either of which permits a provider:

- `EXECUTION_ALLOWLIST` in the environment, for deployment-level policy
- the `allowlisted` flag on the provider row, set from the interface

The database flag exists so enabling a provider does not require editing a file
and restarting the server.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from engine.config import settings
from engine.db import Provider


def env_allowlist() -> set[str]:
    return {p.lower().replace(".", "_") for p in settings.allowlisted_providers}


def is_allowed(session: Session, provider_id: str | None) -> bool:
    """Whether this provider may be called for real."""
    if not settings.execution_allowlist_only:
        return True
    if not provider_id:
        return False
    if provider_id.lower() in env_allowlist():
        return True
    provider = session.get(Provider, provider_id)
    return bool(provider and provider.allowlisted)


def set_allowed(session: Session, provider_id: str, allowed: bool) -> bool:
    """Enable or disable a provider for real execution. Returns the new state."""
    provider = session.get(Provider, provider_id)
    if provider is None:
        raise LookupError(f"unknown provider {provider_id!r}")
    provider.allowlisted = allowed
    session.flush()
    return allowed
