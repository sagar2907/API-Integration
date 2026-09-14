"""OAuth 2.0 authorization code flow.

Some providers — Google above all — will not issue a token you can paste. The
only way to obtain one is the redirect dance: send the user to the provider,
they approve, the provider hands back a short-lived code, and the code is
exchanged server-side for an access token and a refresh token.

Two properties matter and are easy to get wrong:

- **The client secret and the code exchange stay on the server.** The browser
  only ever sees the authorization URL and the returned code.
- **Refresh tokens are the durable credential.** Access tokens expire in about
  an hour, so without a refresh token every automation stops working after
  lunch. Google only issues one when explicitly asked, which is why the
  authorization request sets `access_type=offline` and `prompt=consent`.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

from engine.config import settings

log = logging.getLogger(__name__)


class OAuthError(RuntimeError):
    """The flow could not be started or completed."""


@dataclass(frozen=True)
class OAuthProvider:
    key: str
    name: str
    provider_id: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    # Extra parameters the provider needs on the authorization request.
    extra_authorize: dict[str, str] = field(default_factory=dict)

    def client_id(self) -> str:
        return getattr(settings, f"{self.key}_client_id", "")

    def client_secret(self) -> str:
        return getattr(settings, f"{self.key}_client_secret", "")

    @property
    def configured(self) -> bool:
        return bool(self.client_id() and self.client_secret())


PROVIDERS: dict[str, OAuthProvider] = {
    "google": OAuthProvider(
        key="google",
        name="Google",
        provider_id="googleapis_com",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=(
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/calendar.readonly",
        ),
        extra_authorize={
            # Without these Google returns an access token but no refresh
            # token, and the connection silently dies after an hour.
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        },
    ),
}


def get_provider(key: str) -> OAuthProvider:
    provider = PROVIDERS.get(key.lower())
    if provider is None:
        raise OAuthError(f"unknown OAuth provider {key!r}; known: {sorted(PROVIDERS)}")
    return provider


# `state` protects the callback from cross-site request forgery: a value we
# generated must come back with the code, or the callback is not ours. Kept in
# memory because the flow completes in a single sitting on one machine.
_pending_states: dict[str, float] = {}
_STATE_TTL_SECONDS = 600


def issue_state() -> str:
    _expire_states()
    state = secrets.token_urlsafe(24)
    _pending_states[state] = time.time()
    return state


def consume_state(state: str) -> bool:
    _expire_states()
    return _pending_states.pop(state, None) is not None


def _expire_states() -> None:
    cutoff = time.time() - _STATE_TTL_SECONDS
    for key, created in list(_pending_states.items()):
        if created < cutoff:
            _pending_states.pop(key, None)


def authorize_url(provider: OAuthProvider, *, redirect_uri: str, state: str) -> str:
    if not provider.configured:
        raise OAuthError(
            f"{provider.name} OAuth is not configured. Add "
            f"{provider.key.upper()}_CLIENT_ID and {provider.key.upper()}_CLIENT_SECRET "
            "to .env — see the setup steps on the Credentials page."
        )
    params = {
        "client_id": provider.client_id(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(provider.scopes),
        "state": state,
        **provider.extra_authorize,
    }
    return f"{provider.authorize_url}?{urlencode(params)}"


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_in: int | None
    scope: str | None = None

    @property
    def expires_at(self) -> float | None:
        # A minute of slack, so a token is refreshed slightly early rather than
        # used a moment after it expires.
        return time.time() + self.expires_in - 60 if self.expires_in else None


def _post_token(provider: OAuthProvider, data: dict[str, str]) -> TokenSet:
    try:
        with httpx.Client(timeout=settings.llm_timeout_seconds) as client:
            response = client.post(provider.token_url, data=data)
    except httpx.HTTPError as err:
        raise OAuthError(f"could not reach {provider.name}: {err}") from err

    if response.status_code >= 400:
        # The body may echo the client secret back, so it is not surfaced raw.
        detail = ""
        try:
            payload = response.json()
            detail = str(payload.get("error_description") or payload.get("error") or "")
        except ValueError:
            detail = ""
        raise OAuthError(f"{provider.name} rejected the exchange: {detail or response.status_code}")

    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise OAuthError(f"{provider.name} returned no access token")
    return TokenSet(
        access_token=token,
        refresh_token=payload.get("refresh_token"),
        expires_in=payload.get("expires_in"),
        scope=payload.get("scope"),
    )


def exchange_code(provider: OAuthProvider, *, code: str, redirect_uri: str) -> TokenSet:
    """Swap the one-time code for tokens. Runs server-side only."""
    return _post_token(
        provider,
        {
            "code": code,
            "client_id": provider.client_id(),
            "client_secret": provider.client_secret(),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )


def refresh(provider: OAuthProvider, refresh_token: str) -> TokenSet:
    """Obtain a fresh access token.

    Providers usually omit the refresh token from this response because the
    existing one stays valid, so the caller must keep its copy.
    """
    tokens = _post_token(
        provider,
        {
            "refresh_token": refresh_token,
            "client_id": provider.client_id(),
            "client_secret": provider.client_secret(),
            "grant_type": "refresh_token",
        },
    )
    if tokens.refresh_token is None:
        tokens.refresh_token = refresh_token
    return tokens


def provider_for_id(provider_id: str) -> OAuthProvider | None:
    """The OAuth provider that owns an indexed provider id, if any."""
    return next((p for p in PROVIDERS.values() if p.provider_id == provider_id), None)
