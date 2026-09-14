"""Shared fixtures.

The compiler is tested against a small in-memory database rather than the
ingested corpus, so the tests are fast, deterministic, and unaffected by which
APIs happen to have been downloaded.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from engine.db import Api, AuthScheme, Base, Endpoint, Parameter, Provider, SchemaRecord

ISSUE_RESPONSE = {
    "type": "object",
    "required": ["number", "title"],
    "properties": {
        "number": {"type": "integer"},
        "title": {"type": "string"},
        "state": {"type": "string"},
        "locked": {"type": "boolean"},
        "user": {
            "type": "object",
            "required": ["login"],
            "properties": {"login": {"type": "string"}, "id": {"type": "integer"}},
        },
        "labels": {
            "type": "array",
            "items": {"type": "object", "properties": {"name": {"type": "string"}}},
        },
    },
}

ISSUE_REQUEST = {
    "type": "object",
    "required": ["title"],
    "properties": {
        "title": {"type": "string"},
        "body": {"type": "string"},
        "labels": {"type": "array", "items": {"type": "string"}},
    },
}

SLACK_REQUEST = {
    "type": "object",
    "required": ["channel"],
    "properties": {
        "channel": {"type": "string"},
        "text": {"type": "string"},
        "thread_ts": {"type": "string"},
        "reply_broadcast": {"type": "boolean"},
    },
}


def _provider(
    session: Session, provider_id: str, base_url: str, *, allowlisted: bool = False
) -> None:
    """Create a provider.

    Not allowlisted by default, matching the database default. The fixture used
    to mark everything allowlisted, which was harmless while the flag was never
    read — and quietly wrong the moment it became authoritative.
    """
    session.add(
        Provider(
            provider_id=provider_id,
            name=provider_id,
            base_url=base_url,
            allowlisted=allowlisted,
        )
    )


def _api(session: Session, api_id: str, provider_id: str, scheme: str, supported: bool) -> None:
    session.add(Api(api_id=api_id, provider_id=provider_id, name=f"{provider_id} API", version="1"))
    session.add(
        AuthScheme(
            auth_scheme_id=f"au_{api_id}",
            api_id=api_id,
            name="auth",
            scheme=scheme,
            location="header",
            parameter_name="Authorization",
            scopes=[],
            supported=supported,
        )
    )


def _endpoint(
    session: Session,
    *,
    endpoint_id: str,
    api_id: str,
    method: str,
    path: str,
    servers: list[str],
    params: list[tuple[str, str, str, bool]] = (),
    request: dict | None = None,
    response: dict | None = None,
    deprecated: bool = False,
    destructive: bool = False,
) -> None:
    session.add(
        Endpoint(
            endpoint_id=endpoint_id,
            api_id=api_id,
            method=method,
            path=path,
            operation_id=endpoint_id,
            summary=f"{method} {path}",
            tags=[],
            servers=servers,
            security=[],
            is_deprecated=deprecated,
            is_destructive=destructive,
        )
    )
    for location, name, type_, required in params:
        session.add(
            Parameter(
                parameter_id=f"pa_{endpoint_id}_{location}_{name}",
                endpoint_id=endpoint_id,
                location=location,
                name=name,
                type=type_,
                required=required,
            )
        )
    if request is not None:
        session.add(
            SchemaRecord(
                schema_id=f"sc_{endpoint_id}_req",
                endpoint_id=endpoint_id,
                direction="request",
                status_code="",
                json_schema=request,
            )
        )
    if response is not None:
        session.add(
            SchemaRecord(
                schema_id=f"sc_{endpoint_id}_res",
                endpoint_id=endpoint_id,
                direction="response",
                status_code="201",
                json_schema=response,
            )
        )


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    db = factory()

    # github_com — allowlisted, bearer auth (supported)
    _provider(db, "github_com", "https://api.github.com")
    _api(db, "api_gh", "github_com", "bearer", True)
    _endpoint(
        db,
        endpoint_id="ep_gh_issue_create",
        api_id="api_gh",
        method="POST",
        path="/repos/{owner}/{repo}/issues",
        servers=["https://api.github.com"],
        params=[("path", "owner", "string", True), ("path", "repo", "string", True)],
        request=ISSUE_REQUEST,
        response=ISSUE_RESPONSE,
    )
    _endpoint(
        db,
        endpoint_id="ep_gh_repo_delete",
        api_id="api_gh",
        method="DELETE",
        path="/repos/{owner}/{repo}",
        servers=["https://api.github.com"],
        params=[("path", "owner", "string", True), ("path", "repo", "string", True)],
        destructive=True,
    )
    _endpoint(
        db,
        endpoint_id="ep_gh_legacy",
        api_id="api_gh",
        method="GET",
        path="/teams/{team_id}/repos",
        servers=["https://api.github.com"],
        params=[("path", "team_id", "string", True)],
        deprecated=True,
    )

    # slack_com — allowlisted, bearer auth (supported)
    _provider(db, "slack_com", "https://slack.com/api")
    _api(db, "api_slack", "slack_com", "bearer", True)
    _endpoint(
        db,
        endpoint_id="ep_slack_post",
        api_id="api_slack",
        method="POST",
        path="/chat.postMessage",
        servers=["https://slack.com/api"],
        params=[("header", "token", "string", True)],
        request=SLACK_REQUEST,
    )

    # oauthonly_com — OAuth 2.0 only, which this engine cannot perform
    _provider(db, "oauthonly_com", "https://oauthonly.example.com")
    _api(db, "api_oauth", "oauthonly_com", "oauth2", False)
    _endpoint(
        db,
        endpoint_id="ep_oauth_thing",
        api_id="api_oauth",
        method="POST",
        path="/things",
        servers=["https://oauthonly.example.com"],
        request={"type": "object", "properties": {"name": {"type": "string"}}},
    )

    # stranger_com — supported auth but absent from the execution allowlist
    _provider(db, "stranger_com", "https://stranger.example.com")
    _api(db, "api_stranger", "stranger_com", "apikey", True)
    _endpoint(
        db,
        endpoint_id="ep_stranger_thing",
        api_id="api_stranger",
        method="POST",
        path="/things",
        servers=["https://stranger.example.com"],
    )

    db.commit()
    try:
        yield db
    finally:
        db.close()
