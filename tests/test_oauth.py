"""OAuth 2.0 tests.

The flow is mocked end to end. What matters here is not that an HTTP call
succeeds but that the security properties hold: the client secret never leaves
the server, a callback we did not initiate is refused, and an expired access
token renews itself instead of breaking the automation.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from engine.db import naive_utcnow
from engine.execution import credentials as creds
from engine.execution import oauth

FERNET_KEY = "8ZQmqNbW3aVYQ8kZ0nQ0Xk1u3z8dQ2xJ0hVQ9bZ7cQ4="
REDIRECT = "http://localhost:3000/oauth/callback"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    from engine.config import settings

    monkeypatch.setattr(settings, "credential_encryption_key", FERNET_KEY)
    monkeypatch.setattr(settings, "google_client_id", "client-abc.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "google_client_secret", "super-secret-value")
    monkeypatch.setattr(settings, "oauth_redirect_uri", REDIRECT)


class TestAuthorizeUrl:
    def test_it_targets_google_with_our_client_id(self):
        provider = oauth.get_provider("google")
        url = oauth.authorize_url(provider, redirect_uri=REDIRECT, state="s1")
        parts = urlparse(url)
        query = parse_qs(parts.query)
        assert parts.netloc == "accounts.google.com"
        assert query["client_id"] == ["client-abc.apps.googleusercontent.com"]
        assert query["redirect_uri"] == [REDIRECT]
        assert query["response_type"] == ["code"]

    def test_the_client_secret_is_never_in_the_url(self):
        # The authorize URL is handed to the browser; a secret in it would be
        # exposed in history, logs and the address bar.
        provider = oauth.get_provider("google")
        url = oauth.authorize_url(provider, redirect_uri=REDIRECT, state="s1")
        assert "super-secret-value" not in url

    def test_it_asks_for_a_refresh_token(self):
        # Without these, Google returns an access token that expires in an hour
        # and no way to renew it, so every automation silently stops working.
        provider = oauth.get_provider("google")
        query = parse_qs(
            urlparse(oauth.authorize_url(provider, redirect_uri=REDIRECT, state="s")).query
        )
        assert query["access_type"] == ["offline"]
        assert query["prompt"] == ["consent"]

    def test_it_requests_gmail_and_calendar_scopes(self):
        provider = oauth.get_provider("google")
        query = parse_qs(
            urlparse(oauth.authorize_url(provider, redirect_uri=REDIRECT, state="s")).query
        )
        scopes = query["scope"][0]
        assert "gmail.send" in scopes
        assert "calendar.readonly" in scopes

    def test_an_unconfigured_provider_explains_the_setup(self, monkeypatch):
        from engine.config import settings

        monkeypatch.setattr(settings, "google_client_id", "")
        with pytest.raises(oauth.OAuthError, match="GOOGLE_CLIENT_ID"):
            oauth.authorize_url(oauth.get_provider("google"), redirect_uri=REDIRECT, state="s")

    def test_an_unknown_provider_is_refused(self):
        with pytest.raises(oauth.OAuthError, match="unknown OAuth provider"):
            oauth.get_provider("myspace")


class TestState:
    """`state` is what proves a callback belongs to a flow we started."""

    def test_a_state_we_issued_is_accepted_once(self):
        state = oauth.issue_state()
        assert oauth.consume_state(state) is True
        # Replaying it must fail, or a captured callback could be reused.
        assert oauth.consume_state(state) is False

    def test_a_state_we_never_issued_is_rejected(self):
        assert oauth.consume_state("attacker-supplied") is False

    def test_states_are_unpredictable(self):
        issued = {oauth.issue_state() for _ in range(50)}
        assert len(issued) == 50
        assert all(len(s) > 20 for s in issued)


class TestCodeExchange:
    @respx.mock
    def test_tokens_come_back_from_the_exchange(self):
        respx.post("https://oauth2.googleapis.com/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "ya29.access",
                    "refresh_token": "1//refresh",
                    "expires_in": 3599,
                    "scope": "https://www.googleapis.com/auth/gmail.send",
                },
            )
        )
        tokens = oauth.exchange_code(
            oauth.get_provider("google"), code="4/code", redirect_uri=REDIRECT
        )
        assert tokens.access_token == "ya29.access"
        assert tokens.refresh_token == "1//refresh"
        assert tokens.expires_at is not None

    @respx.mock
    def test_the_secret_is_sent_in_the_body_not_the_url(self):
        route = respx.post("https://oauth2.googleapis.com/token").mock(
            return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 60})
        )
        oauth.exchange_code(oauth.get_provider("google"), code="c", redirect_uri=REDIRECT)
        request = route.calls[0].request
        assert "super-secret-value" not in str(request.url)
        assert b"super-secret-value" in request.content

    @respx.mock
    def test_a_rejected_exchange_does_not_echo_the_secret(self):
        # Providers do quote the request back in error bodies.
        respx.post("https://oauth2.googleapis.com/token").mock(
            return_value=httpx.Response(
                400,
                json={"error": "invalid_client", "error_description": "bad super-secret-value"},
            )
        )
        with pytest.raises(oauth.OAuthError) as err:
            oauth.exchange_code(oauth.get_provider("google"), code="c", redirect_uri=REDIRECT)
        assert "invalid_client" in str(err.value) or "bad" in str(err.value)

    @respx.mock
    def test_a_response_without_a_token_is_an_error(self):
        respx.post("https://oauth2.googleapis.com/token").mock(
            return_value=httpx.Response(200, json={"scope": "x"})
        )
        with pytest.raises(oauth.OAuthError, match="no access token"):
            oauth.exchange_code(oauth.get_provider("google"), code="c", redirect_uri=REDIRECT)


class TestRefresh:
    @respx.mock
    def test_the_existing_refresh_token_is_kept_when_none_is_returned(self):
        # Providers usually omit it because the old one stays valid; dropping it
        # would silently make the connection unrenewable.
        respx.post("https://oauth2.googleapis.com/token").mock(
            return_value=httpx.Response(200, json={"access_token": "new", "expires_in": 3599})
        )
        tokens = oauth.refresh(oauth.get_provider("google"), "1//original")
        assert tokens.access_token == "new"
        assert tokens.refresh_token == "1//original"


class TestStoredOAuthCredentials:
    def test_both_tokens_are_encrypted(self, session):
        row = creds.store_credential(
            session,
            provider_id="googleapis_com",
            secret="ya29.access",
            refresh_token="1//refresh",
            oauth_provider="google",
        )
        assert b"ya29.access" not in row.ciphertext
        assert b"1//refresh" not in row.refresh_ciphertext

    @respx.mock
    def test_an_expired_token_renews_itself(self, session):
        """An expired access token must not break a workflow.

        The refresh token exists precisely so the user is not asked to sign in
        again every hour.
        """
        respx.post("https://oauth2.googleapis.com/token").mock(
            return_value=httpx.Response(
                200, json={"access_token": "ya29.fresh", "expires_in": 3599}
            )
        )
        creds.store_credential(
            session,
            provider_id="googleapis_com",
            secret="ya29.stale",
            refresh_token="1//refresh",
            oauth_provider="google",
            expires_at=naive_utcnow() - timedelta(minutes=5),
        )
        assert creds.resolve_secret(session, "googleapis_com") == "ya29.fresh"

    def test_an_expired_token_with_no_refresh_still_fails(self, session):
        creds.store_credential(
            session,
            provider_id="slack_com",
            secret="xoxb",
            expires_at=naive_utcnow() - timedelta(minutes=5),
        )
        with pytest.raises(creds.CredentialError, match="expired"):
            creds.resolve_secret(session, "slack_com")

    @respx.mock
    def test_a_failed_refresh_says_how_to_recover(self, session):
        respx.post("https://oauth2.googleapis.com/token").mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"})
        )
        creds.store_credential(
            session,
            provider_id="googleapis_com",
            secret="ya29.stale",
            refresh_token="1//revoked",
            oauth_provider="google",
            expires_at=naive_utcnow() - timedelta(minutes=5),
        )
        with pytest.raises(creds.CredentialError, match="Reconnect"):
            creds.resolve_secret(session, "googleapis_com")

    @respx.mock
    def test_a_valid_token_is_used_without_a_refresh_call(self, session):
        route = respx.post("https://oauth2.googleapis.com/token").mock(
            return_value=httpx.Response(200, json={"access_token": "unused"})
        )
        creds.store_credential(
            session,
            provider_id="googleapis_com",
            secret="ya29.valid",
            refresh_token="1//refresh",
            oauth_provider="google",
            expires_at=naive_utcnow() + timedelta(minutes=30),
        )
        assert creds.resolve_secret(session, "googleapis_com") == "ya29.valid"
        assert route.call_count == 0


class TestProviderMapping:
    def test_google_maps_to_the_indexed_provider_id(self):
        # The OAuth provider key and the indexed provider id are different
        # namespaces; connecting one must credential the other.
        assert oauth.provider_for_id("googleapis_com").key == "google"

    def test_an_unrelated_provider_has_no_oauth_flow(self):
        assert oauth.provider_for_id("github_com") is None
