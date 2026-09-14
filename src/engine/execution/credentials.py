"""Encrypted credential storage.

Secrets are encrypted at rest, decrypted only at the moment of a call, and
never written to a log, an error message, an execution event, or a prompt. The
agent layer sees provider names; it never sees a token.

Rotation is supported by storing the key version alongside each row, so a new
key can be introduced and old rows re-encrypted incrementally rather than in
one risky migration.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from engine import ids
from engine.config import settings
from engine.db import Credential, CredentialAudit, naive_utcnow

log = logging.getLogger(__name__)


class CredentialError(RuntimeError):
    """A credential is missing, expired, or cannot be decrypted."""


def _cipher() -> Fernet:
    key = settings.credential_encryption_key
    if not key:
        raise CredentialError(
            "CREDENTIAL_ENCRYPTION_KEY is not set. Generate one with:\n"
            '  python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as err:
        raise CredentialError(
            f"CREDENTIAL_ENCRYPTION_KEY is not a valid Fernet key: {err}"
        ) from err


def store_credential(
    session: Session,
    *,
    provider_id: str,
    secret: str,
    scheme: str = "bearer",
    owner_id: str = "local",
    label: str | None = None,
    scopes: list[str] | None = None,
    expires_at: datetime | None = None,
    refresh_token: str | None = None,
    oauth_provider: str | None = None,
) -> Credential:
    """Encrypt and save a provider secret.

    An OAuth connection stores two secrets: the short-lived access token and
    the refresh token that replaces it. Both are encrypted; only the access
    token is handed to the executor.
    """
    credential_id = f"cr_{ids.digest(owner_id, provider_id, scheme)}"
    row = Credential(
        credential_id=credential_id,
        owner_id=owner_id,
        provider_id=provider_id,
        scheme=scheme,
        label=label,
        ciphertext=_cipher().encrypt(secret.encode()),
        refresh_ciphertext=(_cipher().encrypt(refresh_token.encode()) if refresh_token else None),
        oauth_provider=oauth_provider,
        key_version=settings.credential_key_version,
        scopes=scopes or [],
        expires_at=expires_at,
    )
    session.merge(row)
    session.flush()
    # The value is deliberately absent from this line.
    log.info("stored %s credential for %s", scheme, provider_id)
    return row


def find_credential(
    session: Session, provider_id: str, *, owner_id: str = "local"
) -> Credential | None:
    return session.scalar(
        select(Credential).where(
            Credential.provider_id == provider_id, Credential.owner_id == owner_id
        )
    )


def _refresh_in_place(session: Session, row: Credential) -> None:
    """Exchange the stored refresh token for a new access token."""
    from engine.execution import oauth  # imported here to avoid a cycle

    try:
        provider = oauth.get_provider(row.oauth_provider or "")
        refresh_token = _cipher().decrypt(row.refresh_ciphertext).decode()
        tokens = oauth.refresh(provider, refresh_token)
    except (oauth.OAuthError, InvalidToken) as err:
        raise CredentialError(
            f"could not refresh the {row.provider_id} connection: {err}. "
            "Reconnect it from the Credentials page."
        ) from err

    row.ciphertext = _cipher().encrypt(tokens.access_token.encode())
    if tokens.refresh_token:
        row.refresh_ciphertext = _cipher().encrypt(tokens.refresh_token.encode())
    row.expires_at = (
        datetime.fromtimestamp(tokens.expires_at, tz=UTC).replace(tzinfo=None)
        if tokens.expires_at
        else None
    )
    session.flush()
    log.info("refreshed the %s access token", row.provider_id)


def resolve_secret(
    session: Session,
    provider_id: str,
    *,
    owner_id: str = "local",
    execution_id: str | None = None,
    node_id: str | None = None,
) -> str:
    """Decrypt a secret for immediate use, recording the access.

    The returned string lives in memory for one request. Callers must not log
    it, store it, or attach it to an event.
    """
    row = find_credential(session, provider_id, owner_id=owner_id)
    if row is None:
        raise CredentialError(
            f"no credential stored for {provider_id!r} — "
            f"add one with `engine credential add {provider_id} --secret ...`"
        )
    if row.expires_at is not None and row.expires_at < naive_utcnow():
        # An expired OAuth access token is not a dead end: the refresh token
        # buys a new one without troubling the user. Only a credential with no
        # way to renew itself is genuinely expired.
        if row.refresh_ciphertext and row.oauth_provider:
            _refresh_in_place(session, row)
        else:
            raise CredentialError(f"credential for {provider_id!r} expired at {row.expires_at}")

    try:
        secret = _cipher().decrypt(row.ciphertext).decode()
    except InvalidToken as err:
        raise CredentialError(
            f"cannot decrypt the credential for {provider_id!r}: it was encrypted "
            f"with key version {row.key_version} and the current key does not match"
        ) from err

    session.add(
        CredentialAudit(
            credential_id=row.credential_id,
            execution_id=execution_id,
            node_id=node_id,
            action="read",
        )
    )
    return secret


def apply_auth(
    headers: dict[str, str],
    params: dict[str, str],
    *,
    scheme: str | None,
    secret: str,
    location: str | None = None,
    parameter_name: str | None = None,
) -> None:
    """Place a secret into the outgoing request, in place.

    OAuth 2.0 is treated as bearer: the authorization *flow* is out of scope,
    but a token the user already holds is sent exactly like any bearer token,
    which is how Slack bot tokens and GitHub PATs work.
    """
    normalized = (scheme or "bearer").lower()

    if normalized in ("bearer", "oauth2", "openidconnect"):
        headers["Authorization"] = f"Bearer {secret}"
    elif normalized == "basic":
        headers["Authorization"] = f"Basic {secret}"
    elif normalized == "apikey":
        name = parameter_name or "Authorization"
        if (location or "header").lower() == "query":
            params[name] = secret
        else:
            headers[name] = secret
    else:
        raise CredentialError(f"cannot apply auth scheme {scheme!r}")


def redact(text: str, *secrets: str) -> str:
    """Remove secret values from any text before it is logged or stored."""
    for secret in secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, "<REDACTED>")
    return text
