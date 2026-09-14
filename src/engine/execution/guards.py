"""Egress guards.

The compiler already guarantees a URL is built from indexed metadata rather
than from model output. This is the second line: even a legitimate provider
record could point somewhere it should not, whether through a malicious spec, a
hijacked domain, or a redirect.

Every outbound request is checked here immediately before dispatch.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlsplit

from engine.config import settings

log = logging.getLogger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https"})

# Hostnames that resolve to infrastructure rather than to a public API. The
# cloud metadata endpoint is the classic SSRF target: reachable from inside a
# VM and willing to hand out credentials.
BLOCKED_HOSTNAMES = frozenset(
    {"localhost", "metadata.google.internal", "metadata", "instance-data"}
)


class BlockedRequest(RuntimeError):
    """The request was refused before it left the process."""


def _is_private(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def resolve_addresses(hostname: str) -> list[str]:
    """Every address a hostname resolves to.

    All of them are checked, not just the first: a hostname can resolve to a
    public address and a private one, and a check that looked at only one could
    be walked past.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as err:
        raise BlockedRequest(f"cannot resolve host {hostname!r}: {err}") from err
    return sorted({info[4][0] for info in infos})


def check_url(url: str, *, provider_id: str | None = None, allowlisted: bool | None = None) -> None:
    """Refuse anything that should not be dispatched. Raises BlockedRequest.

    Redirects need no separate check because the executor does not follow them:
    a 3xx is treated as a provider error. Following one would mean trusting an
    allowlisted host to pick the next destination, and an open redirect is a
    standard way to reach an internal address from a permitted origin.
    """
    parts = urlsplit(url)

    if parts.scheme not in ALLOWED_SCHEMES:
        raise BlockedRequest(f"scheme {parts.scheme!r} is not allowed")

    hostname = (parts.hostname or "").lower()
    if not hostname:
        raise BlockedRequest("request has no host")

    if hostname in BLOCKED_HOSTNAMES:
        raise BlockedRequest(f"host {hostname!r} is blocked")

    if settings.execution_allowlist_only and provider_id:
        # `allowlisted` is decided by the caller, which has database access and
        # can honour a provider enabled from the interface. Falling back to the
        # environment alone would mean the only way to permit a provider is to
        # edit a file and restart — a poor trade for a safety catch that exists
        # to stop *accidental* calls, not deliberate ones.
        permitted = (
            allowlisted
            if allowlisted is not None
            else provider_id.lower()
            in {p.lower().replace(".", "_") for p in settings.allowlisted_providers}
        )
        if not permitted:
            raise BlockedRequest(f"provider {provider_id!r} is not on the execution allowlist")

    if settings.ssrf_block_private_ranges:
        if _is_private(hostname):
            raise BlockedRequest(f"host {hostname!r} is a private address")
        for address in resolve_addresses(hostname):
            if _is_private(address):
                raise BlockedRequest(f"host {hostname!r} resolves to private address {address}")
