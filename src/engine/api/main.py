"""HTTP API.

One FastAPI application for the prototype. The route layout already matches the
service split described in architecture.md, so pulling search or planning into
its own process later is a routing change rather than a rewrite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from engine.agent import llm as llm_module
from engine.agent.planner import plan_workflow
from engine.config import settings
from engine.db import (
    Api,
    AuthScheme,
    Credential,
    Endpoint,
    Parameter,
    Provider,
    SchemaRecord,
    SessionLocal,
    init_db,
)
from engine.execution import credentials as creds
from engine.execution import oauth as oauth_module
from engine.execution import policy, runner
from engine.search import embeddings as emb
from engine.search.index import indexed_count
from engine.search.retrieve import search as run_search
from engine.workflow import store
from engine.workflow.compiler import compile_workflow
from engine.workflow.dag import CompileResult, WorkflowDefinition


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Autonomous API Discovery & Integration Engine",
    version="0.1.0",
    description="Natural-language API discovery over an indexed OpenAPI knowledge base.",
)


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]


# ------------------------------------------------------------------- schemas


class CandidateOut(BaseModel):
    endpoint_id: str
    method: str
    path: str
    summary: str | None
    operation_id: str | None
    api_name: str
    provider_id: str
    is_deprecated: bool
    is_destructive: bool
    score: float
    rank: int
    matched_terms: list[str] = Field(default_factory=list)
    component_ranks: dict[str, int] = Field(default_factory=dict)


class SearchOut(BaseModel):
    query: str
    mode: str
    took_ms: float
    count: int
    candidates: list[CandidateOut]


class ParameterOut(BaseModel):
    location: str
    name: str
    type: str | None
    required: bool
    description: str | None


class SchemaOut(BaseModel):
    direction: str
    status_code: str
    content_type: str
    json_schema: dict[str, Any]


class EndpointDetailOut(BaseModel):
    endpoint_id: str
    method: str
    path: str
    operation_id: str | None
    summary: str | None
    description: str | None
    tags: list[str]
    servers: list[str]
    is_deprecated: bool
    is_destructive: bool
    api_name: str
    api_version: str
    provider_id: str
    documentation_url: str | None
    parameters: list[ParameterOut]
    schemas: list[SchemaOut]
    auth_schemes: list[dict[str, Any]]


class HealthOut(BaseModel):
    status: str
    endpoints: int
    providers: int
    search_index: int
    embeddings_available: bool
    llm_configured: bool


# -------------------------------------------------------------------- routes


@app.get("/health", response_model=HealthOut, tags=["system"])
def health(session: SessionDep) -> HealthOut:
    return HealthOut(
        status="ok",
        endpoints=session.scalar(select(func.count()).select_from(Endpoint)) or 0,
        providers=session.scalar(select(func.count()).select_from(Provider)) or 0,
        search_index=indexed_count(),
        embeddings_available=emb.vector_store_available(),
        llm_configured=bool(settings.llm_api_key),
    )


@app.get("/v1/search", response_model=SearchOut, tags=["search"])
def search_endpoints(
    session: SessionDep,
    q: Annotated[str, Query(min_length=2, description="Natural-language capability.")],
    mode: Annotated[Literal["bm25", "vector", "hybrid"], Query()] = "hybrid",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    provider: Annotated[str | None, Query()] = None,
    method: Annotated[str | None, Query()] = None,
    include_deprecated: Annotated[bool, Query()] = False,
) -> SearchOut:
    """Rank endpoints against a natural-language description of a capability."""
    if indexed_count() == 0:
        raise HTTPException(
            status_code=503,
            detail="search index is empty — run `engine index`",
        )
    try:
        result = run_search(
            session,
            q,
            mode=mode,
            limit=limit,
            provider=provider,
            method=method,
            include_deprecated=include_deprecated,
        )
    except emb.EmbeddingError as err:
        # Only reachable for mode=vector; hybrid degrades to lexical internally.
        raise HTTPException(status_code=503, detail=str(err)) from err

    return SearchOut(
        query=result.query,
        mode=result.mode,
        took_ms=result.took_ms,
        count=len(result.candidates),
        candidates=[CandidateOut(**vars(candidate)) for candidate in result.candidates],
    )


@app.get("/v1/endpoints/{endpoint_id}", response_model=EndpointDetailOut, tags=["search"])
def get_endpoint(endpoint_id: str, session: SessionDep) -> EndpointDetailOut:
    """Full metadata for one endpoint — what the compiler validates against."""
    row = session.execute(
        select(Endpoint, Api, Provider)
        .join(Api, Api.api_id == Endpoint.api_id)
        .join(Provider, Provider.provider_id == Api.provider_id)
        .where(Endpoint.endpoint_id == endpoint_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown endpoint {endpoint_id}")
    endpoint, api, provider = row

    parameters = session.scalars(
        select(Parameter).where(Parameter.endpoint_id == endpoint_id)
    ).all()
    schemas = session.scalars(
        select(SchemaRecord).where(SchemaRecord.endpoint_id == endpoint_id)
    ).all()
    auth = session.scalars(select(AuthScheme).where(AuthScheme.api_id == api.api_id)).all()

    return EndpointDetailOut(
        endpoint_id=endpoint.endpoint_id,
        method=endpoint.method,
        path=endpoint.path,
        operation_id=endpoint.operation_id,
        summary=endpoint.summary,
        description=endpoint.description,
        tags=endpoint.tags or [],
        servers=endpoint.servers or [],
        is_deprecated=endpoint.is_deprecated,
        is_destructive=endpoint.is_destructive,
        api_name=api.name,
        api_version=api.version,
        provider_id=provider.provider_id,
        documentation_url=provider.documentation_url,
        parameters=[
            ParameterOut(
                location=p.location,
                name=p.name,
                type=p.type,
                required=p.required,
                description=p.description,
            )
            for p in parameters
        ],
        schemas=[
            SchemaOut(
                direction=s.direction,
                status_code=s.status_code,
                content_type=s.content_type,
                json_schema=s.json_schema or {},
            )
            for s in schemas
        ],
        auth_schemes=[
            {
                "name": a.name,
                "scheme": a.scheme,
                "location": a.location,
                "parameter_name": a.parameter_name,
                "scopes": a.scopes,
                "supported": a.supported,
            }
            for a in auth
        ],
    )


@app.get("/v1/providers", tags=["search"])
def list_providers(session: SessionDep) -> dict[str, Any]:
    """Every indexed provider with its endpoint count."""
    rows = session.execute(
        select(Provider.provider_id, Provider.name, func.count(Endpoint.endpoint_id))
        .join(Api, Api.provider_id == Provider.provider_id)
        .join(Endpoint, Endpoint.api_id == Api.api_id)
        .group_by(Provider.provider_id)
        .order_by(func.count(Endpoint.endpoint_id).desc())
    ).all()
    return {
        "count": len(rows),
        "providers": [
            {"provider_id": pid, "name": name, "endpoints": count} for pid, name, count in rows
        ],
    }


# ----------------------------------------------------------------- workflows


class WorkflowSummary(BaseModel):
    workflow_id: str
    name: str
    version: int
    status: str
    node_count: int
    created_at: str


class SaveWorkflowOut(BaseModel):
    workflow_id: str
    version: int
    status: str
    validation: CompileResult


@app.post("/v1/workflows", response_model=SaveWorkflowOut, tags=["workflows"])
def create_workflow(
    definition: WorkflowDefinition,
    session: SessionDep,
    parent_version: Annotated[str | None, Query()] = None,
) -> SaveWorkflowOut:
    """Save a workflow and validate it in the same call.

    The workflow is stored either way — an invalid draft is still worth keeping
    so the author can see what was wrong and fix it. `status` records which it
    is, and only a compiled workflow is ever executable.
    """
    result = compile_workflow(session, definition)
    row = store.save_workflow(
        session,
        definition,
        status="validated" if result.ok else "draft",
        parent_version=parent_version,
    )
    if result.ok and result.workflow is not None:
        store.save_compiled(session, row, result.workflow)
    session.commit()
    return SaveWorkflowOut(
        workflow_id=row.workflow_id,
        version=row.version,
        status=row.status,
        validation=result,
    )


@app.post("/v1/workflows/validate", response_model=CompileResult, tags=["workflows"])
def validate_workflow(definition: WorkflowDefinition, session: SessionDep) -> CompileResult:
    """Validate without saving. Used by the planner's repair loop."""
    return compile_workflow(session, definition)


@app.get("/v1/workflows", tags=["workflows"])
def list_workflows(session: SessionDep) -> dict[str, Any]:
    rows = store.list_workflows(session)
    return {
        "count": len(rows),
        "workflows": [
            WorkflowSummary(
                workflow_id=r.workflow_id,
                name=r.name,
                version=r.version,
                status=r.status,
                node_count=len((r.definition or {}).get("nodes", [])),
                created_at=r.created_at.isoformat(),
            )
            for r in rows
        ],
    }


@app.get("/v1/workflows/{workflow_id}", tags=["workflows"])
def get_workflow(workflow_id: str, session: SessionDep) -> dict[str, Any]:
    definition = store.load_definition(session, workflow_id)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"unknown workflow {workflow_id}")
    compiled = store.latest_compiled(session, workflow_id)
    return {
        "workflow_id": workflow_id,
        "definition": definition.model_dump(),
        "compiled": compiled.ir if compiled else None,
    }


@app.post("/v1/workflows/{workflow_id}/compile", response_model=CompileResult, tags=["workflows"])
def compile_saved_workflow(workflow_id: str, session: SessionDep) -> CompileResult:
    """Re-validate a stored workflow against the current knowledge base.

    Worth running again over time: an endpoint that was live when the workflow
    was written may since have been deprecated or removed by its provider.
    """
    definition = store.load_definition(session, workflow_id)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"unknown workflow {workflow_id}")
    result = compile_workflow(session, definition)
    row = session.get(store.Workflow, workflow_id)
    if row is not None:
        row.status = "validated" if result.ok else "draft"
        if result.ok and result.workflow is not None:
            store.save_compiled(session, row, result.workflow)
    session.commit()
    return result


# ------------------------------------------------------- connection status


class ConnectionStatus(BaseModel):
    """Whether one provider can be connected, and if not, why not."""

    provider_id: str
    name: str
    # connected | paste | oauth | oauth_setup | impossible
    status: str
    reason: str
    scheme: str | None = None
    documentation_url: str | None = None
    oauth_provider: str | None = None
    allowlisted: bool = False


def _connection_status(session: Session, provider_id: str) -> ConnectionStatus:
    """Classify a provider so the interface can ask for exactly what it needs.

    Every branch resolves to something the user can act on: connect it, sign in,
    finish a one-time setup, or a plain statement that it cannot be connected
    and why. An unexplained blank is the one outcome worth avoiding.
    """
    provider = session.get(Provider, provider_id)
    name = provider.name if provider else provider_id
    base_url = (provider.base_url if provider else "") or ""
    documentation_url = provider.documentation_url if provider else None
    allowlisted = policy.is_allowed(session, provider_id)

    def build(status: str, reason: str, **extra: Any) -> ConnectionStatus:
        return ConnectionStatus(
            provider_id=provider_id,
            name=name,
            status=status,
            reason=reason,
            documentation_url=documentation_url,
            allowlisted=allowlisted,
            **extra,
        )

    if creds.find_credential(session, provider_id) is not None:
        return build("connected", "A token is stored for this service.")

    # Impossible cases first — offering a token box for these would be a lie.
    if "{" in base_url:
        return build(
            "impossible",
            f"This service puts the token inside its address ({base_url}). Requests "
            "are built from indexed metadata and the engine does not substitute into "
            "the host, so it cannot be called yet even with a token stored.",
        )

    host = urlsplit(base_url).hostname or ""
    if host.endswith(".local") or host in ("localhost", "127.0.0.1"):
        return build(
            "impossible",
            f"This specification describes a self-hosted service at {base_url}. "
            "There is no public endpoint to connect to — you would have to run it "
            "yourself first.",
        )

    schemes = session.scalars(
        select(AuthScheme)
        .join(Api, Api.api_id == AuthScheme.api_id)
        .where(Api.provider_id == provider_id)
    ).all()

    oauth_provider = oauth_module.provider_for_id(provider_id)
    if oauth_provider is not None:
        if oauth_provider.configured:
            return build(
                "oauth",
                f"Sign in with {oauth_provider.name} to grant access.",
                oauth_provider=oauth_provider.key,
                scheme="bearer",
            )
        return build(
            "oauth_setup",
            f"{oauth_provider.name} only issues tokens to a registered application, "
            "so a one-time registration is needed before you can sign in.",
            oauth_provider=oauth_provider.key,
            scheme="bearer",
        )

    pasteable = [s for s in schemes if s.scheme != "oauth2"]
    if pasteable:
        scheme = pasteable[0].scheme
        where = ""
        if pasteable[0].parameter_name:
            where = f" as {pasteable[0].location or 'header'} {pasteable[0].parameter_name}"
        return build(
            "paste",
            f"Paste a {scheme} token{where}.",
            scheme="apikey" if scheme == "apikey" else ("basic" if scheme == "basic" else "bearer"),
        )

    if schemes:
        # OAuth 2.0 in the specification does not mean a sign-in flow is the
        # only way in. Slack declares oauth2 and hands out long-lived xoxb- bot
        # tokens; GitHub and Asana behave the same way. Calling these impossible
        # would be wrong, and would contradict the setup steps we ship for them.
        # Genuine impossibility is structural — a token in the URL, or a service
        # that has no public host — and both are handled above.
        return build(
            "paste",
            "This service uses OAuth, but like most it also issues a long-lived "
            "token from its developer settings that you can copy and paste here.",
            scheme="bearer",
        )

    # A specification that declares nothing is not a specification that needs
    # nothing. GitHub's official document omits securitySchemes entirely while
    # obviously requiring a token, so saying "declares no authentication" here
    # would be true of the file and misleading about the service.
    return build(
        "paste",
        "This specification does not say how to authenticate. Most APIs still "
        "require a token — check the documentation, then paste it here.",
        scheme="bearer",
    )


# ------------------------------------------------------------------ planning


class PlanRequest(BaseModel):
    goal: str = Field(min_length=4, description="What the automation should do.")
    max_repairs: int | None = Field(default=None, ge=0, le=5)
    save: bool = Field(default=True, description="Persist the workflow if it validates.")


class PlanAttemptOut(BaseModel):
    iteration: int
    accepted: bool
    issues: list[dict[str, Any]]


class PlanOut(BaseModel):
    ok: bool
    goal: str
    workflow_id: str | None = None
    requirement: dict[str, Any] | None = None
    definition: dict[str, Any] | None = None
    compiled: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    attempts: list[PlanAttemptOut] = Field(default_factory=list)
    issues: list[dict[str, Any]] = Field(default_factory=list)
    tokens: int = 0
    # Exactly the services this plan needs, and whether each can be connected.
    # Asking only for what the request actually mentions beats presenting a
    # catalogue of a hundred providers and leaving the user to work it out.
    required_connections: list[ConnectionStatus] = Field(default_factory=list)


@app.post("/v1/workflows/plan", response_model=PlanOut, tags=["planning"])
def plan(request: PlanRequest, session: SessionDep) -> PlanOut:
    """Natural language in, validated workflow out.

    The model only ever chooses among endpoints this request's searches
    returned, and whatever it produces still has to pass the compiler. A plan
    that fails is returned with its reasons rather than raising, because those
    reasons are the useful part.
    """
    if indexed_count() == 0:
        raise HTTPException(status_code=503, detail="search index is empty — run `engine index`")
    try:
        client = llm_module.get_client()
        outcome = plan_workflow(session, request.goal, client, max_repairs=request.max_repairs)
    except llm_module.LLMError as err:
        raise HTTPException(status_code=503, detail=str(err)) from err

    workflow_id = None
    if outcome.ok and outcome.definition is not None and request.save:
        row = store.save_workflow(session, outcome.definition, status="validated")
        if outcome.compiled is not None:
            store.save_compiled(session, row, outcome.compiled)
        session.commit()
        workflow_id = row.workflow_id

    # Providers actually referenced by the compiled plan; if it was rejected,
    # fall back to the providers among the retrieved candidates so the user can
    # still see what the goal would have needed.
    if outcome.compiled is not None:
        provider_ids = [n.provider_id for n in outcome.compiled.nodes if n.provider_id]
    else:
        provider_ids = [c.provider_id for c in outcome.candidates]
    seen_providers: list[str] = []
    for pid in provider_ids:
        if pid not in seen_providers:
            seen_providers.append(pid)
    required = [_connection_status(session, pid) for pid in seen_providers[:8]]

    return PlanOut(
        ok=outcome.ok,
        goal=outcome.goal,
        workflow_id=workflow_id,
        required_connections=required,
        requirement=outcome.requirement.model_dump() if outcome.requirement else None,
        definition=outcome.definition.model_dump() if outcome.definition else None,
        compiled=outcome.compiled.model_dump(mode="json") if outcome.compiled else None,
        candidates=[
            {
                "endpoint_id": c.endpoint_id,
                "method": c.method,
                "path": c.path,
                "provider_id": c.provider_id,
                "summary": c.summary,
                "rank": c.rank,
            }
            for c in outcome.candidates
        ],
        attempts=[
            PlanAttemptOut(
                iteration=a.iteration,
                accepted=a.accepted,
                issues=[i.model_dump(exclude_none=True) for i in a.issues],
            )
            for a in outcome.attempts
        ],
        issues=[i.model_dump(exclude_none=True) for i in outcome.issues],
        tokens=outcome.usage.total_tokens,
    )


# ----------------------------------------------------------------- execution


class RunRequest(BaseModel):
    dry_run: bool = False
    queue: bool = Field(default=False, description="Hand to a worker instead of running inline.")


class ExecutionOut(BaseModel):
    execution_id: str
    workflow_id: str
    status: str
    dry_run: bool
    error_category: str | None = None
    error_message: str | None = None
    nodes: list[dict[str, Any]] = Field(default_factory=list)


@app.post("/v1/workflows/{workflow_id}/executions", response_model=ExecutionOut, tags=["execution"])
def run_workflow(workflow_id: str, request: RunRequest, session: SessionDep) -> ExecutionOut:
    """Execute a compiled workflow.

    Only a compiled workflow can run: without a stored compiled form there is
    nothing to execute, which is the guarantee the validator provides.
    """
    compiled = runner.load_compiled(session, workflow_id)
    if compiled is None:
        raise HTTPException(
            status_code=409,
            detail=f"workflow {workflow_id} has no compiled form — validate it first",
        )

    execution = runner.create_execution(session, workflow_id=workflow_id, dry_run=request.dry_run)
    if request.queue:
        runner.enqueue(session, execution)
        session.commit()
    else:
        runner.run_execution(session, execution, compiled)

    return _execution_out(session, execution.execution_id)


def _dry_run_warnings(output: dict[str, Any]) -> list[str]:
    """Things a dry run built successfully but a real run would refuse."""
    warnings: list[str] = []
    if output.get("would_be_blocked"):
        warnings.append(str(output["would_be_blocked"]))
    if output.get("credential_missing"):
        warnings.append("no credential is configured for this provider")
    return warnings


def _execution_out(session: Session, execution_id: str) -> ExecutionOut:
    from engine.db import Execution, NodeResult

    execution = session.get(Execution, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail=f"unknown execution {execution_id}")
    results = session.scalars(
        select(NodeResult).where(NodeResult.execution_id == execution_id)
    ).all()
    return ExecutionOut(
        execution_id=execution.execution_id,
        workflow_id=execution.workflow_id,
        status=execution.status,
        dry_run=execution.dry_run,
        error_category=execution.error_category,
        error_message=execution.error_message,
        nodes=[
            {
                "node_id": r.node_id,
                "status": r.status,
                "status_code": r.status_code,
                "attempts": r.attempts,
                "latency_ms": r.latency_ms,
                "output_fields": sorted(r.output or {})[:20],
                # A dry run can succeed at building a request that would still
                # be refused at send time. Reporting only "succeeded" would be
                # a half-truth, so the reasons travel with the result.
                "warnings": _dry_run_warnings(r.output or {}),
            }
            for r in results
        ],
    )


@app.get("/v1/executions/{execution_id}", response_model=ExecutionOut, tags=["execution"])
def get_execution(execution_id: str, session: SessionDep) -> ExecutionOut:
    return _execution_out(session, execution_id)


@app.get("/v1/executions", tags=["execution"])
def list_executions(session: SessionDep, limit: int = 25) -> dict[str, Any]:
    from engine.db import Execution

    rows = session.scalars(
        select(Execution).order_by(Execution.started_at.desc()).limit(limit)
    ).all()
    return {
        "count": len(rows),
        "executions": [
            {
                "execution_id": r.execution_id,
                "workflow_id": r.workflow_id,
                "status": r.status,
                "dry_run": r.dry_run,
                "error_category": r.error_category,
                "started_at": r.started_at.isoformat(),
            }
            for r in rows
        ],
    }


@app.get("/v1/executions/{execution_id}/events", tags=["execution"])
def execution_events(execution_id: str, session: SessionDep) -> dict[str, Any]:
    """Per-node timeline. Bodies are never stored, only shapes and sizes."""
    from engine.db import ExecutionEvent

    rows = session.scalars(
        select(ExecutionEvent)
        .where(ExecutionEvent.execution_id == execution_id)
        .order_by(ExecutionEvent.id)
    ).all()
    return {
        "count": len(rows),
        "events": [
            {
                "ts": r.ts.isoformat(),
                "node_id": r.node_id,
                "event_type": r.event_type,
                "attempt": r.attempt,
                "latency_ms": r.latency_ms,
                "status_code": r.status_code,
                "error_category": r.error_category,
                "payload_metadata": r.payload_metadata,
            }
            for r in rows
        ],
    }


# --------------------------------------------------------------- credentials


class CredentialIn(BaseModel):
    provider_id: str = Field(min_length=1, description="Provider id, e.g. slack_com")
    secret: str = Field(min_length=1, description="Token or key. Stored encrypted.")
    scheme: str = Field(default="bearer", description="bearer | apikey | basic")
    label: str | None = None


class CredentialOut(BaseModel):
    """A credential's metadata. The secret itself is never returned."""

    credential_id: str
    provider_id: str
    scheme: str
    label: str | None
    created_at: str


@app.get("/v1/credentials", response_model=list[CredentialOut], tags=["credentials"])
def list_credentials(session: SessionDep) -> list[CredentialOut]:
    """Which providers have a credential stored. Secrets are never returned."""
    rows = session.scalars(select(Credential).order_by(Credential.provider_id)).all()
    return [
        CredentialOut(
            credential_id=row.credential_id,
            provider_id=row.provider_id,
            scheme=row.scheme,
            label=row.label,
            created_at=row.created_at.isoformat(),
        )
        for row in rows
    ]


@app.post("/v1/credentials", response_model=CredentialOut, tags=["credentials"])
def add_credential(body: CredentialIn, session: SessionDep) -> CredentialOut:
    """Store a provider secret, encrypted at rest.

    The value is encrypted before it touches the database and is decrypted only
    at the moment of an outbound call. It is never logged, never returned by
    this API, and never placed in a prompt.
    """
    try:
        row = creds.store_credential(
            session,
            provider_id=body.provider_id.strip(),
            secret=body.secret,
            scheme=body.scheme.strip().lower(),
            label=body.label,
        )
        session.commit()
    except creds.CredentialError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err

    return CredentialOut(
        credential_id=row.credential_id,
        provider_id=row.provider_id,
        scheme=row.scheme,
        label=row.label,
        created_at=row.created_at.isoformat(),
    )


@app.delete("/v1/credentials/{credential_id}", tags=["credentials"])
def delete_credential(credential_id: str, session: SessionDep) -> dict[str, str]:
    row = session.get(Credential, credential_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such credential")
    session.delete(row)
    session.commit()
    return {"status": "deleted", "credential_id": credential_id}


@app.get(
    "/v1/providers/{provider_id}/connection",
    response_model=ConnectionStatus,
    tags=["credentials"],
)
def provider_connection(provider_id: str, session: SessionDep) -> ConnectionStatus:
    """Can this one provider be connected, and how."""
    return _connection_status(session, provider_id)


@app.get("/v1/providers/{provider_id}/auth", tags=["credentials"])
def provider_auth(provider_id: str, session: SessionDep) -> dict[str, Any]:
    """What this provider needs, and whether we can perform it.

    OAuth 2.0 is reported honestly: a token you already hold is sent like any
    bearer token, but this engine cannot run the authorization flow that issues
    one, so there is no "sign in with..." path.
    """
    schemes = session.scalars(
        select(AuthScheme)
        .join(Api, Api.api_id == AuthScheme.api_id)
        .where(Api.provider_id == provider_id)
    ).all()
    stored = creds.find_credential(session, provider_id)
    return {
        "provider_id": provider_id,
        "has_credential": stored is not None,
        "allowlisted": provider_id
        in {p.lower().replace(".", "_") for p in settings.allowlisted_providers},
        "schemes": [
            {
                "name": s.name,
                "scheme": s.scheme,
                "location": s.location,
                "parameter_name": s.parameter_name,
                "needs_authorization_flow": s.scheme == "oauth2",
            }
            for s in schemes
        ],
    }


# --------------------------------------------------------------------- oauth


class OAuthStartOut(BaseModel):
    provider: str
    configured: bool
    authorize_url: str | None = None
    redirect_uri: str
    scopes: list[str]
    setup_hint: str | None = None


class OAuthExchangeIn(BaseModel):
    code: str = Field(min_length=4)
    state: str


@app.get("/v1/oauth/{provider_key}/start", response_model=OAuthStartOut, tags=["oauth"])
def oauth_start(provider_key: str) -> OAuthStartOut:
    """Begin an OAuth connection: hand the browser a URL to send the user to."""
    try:
        provider = oauth_module.get_provider(provider_key)
    except oauth_module.OAuthError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err

    redirect_uri = settings.oauth_redirect_uri
    if not provider.configured:
        return OAuthStartOut(
            provider=provider.key,
            configured=False,
            redirect_uri=redirect_uri,
            scopes=list(provider.scopes),
            setup_hint=(
                f"Register an OAuth client with {provider.name}, add {redirect_uri} "
                f"as an authorised redirect URI, then set "
                f"{provider.key.upper()}_CLIENT_ID and {provider.key.upper()}_CLIENT_SECRET "
                "in .env and restart the API."
            ),
        )

    state = oauth_module.issue_state()
    return OAuthStartOut(
        provider=provider.key,
        configured=True,
        authorize_url=oauth_module.authorize_url(provider, redirect_uri=redirect_uri, state=state),
        redirect_uri=redirect_uri,
        scopes=list(provider.scopes),
    )


@app.post("/v1/oauth/{provider_key}/exchange", tags=["oauth"])
def oauth_exchange(provider_key: str, body: OAuthExchangeIn, session: SessionDep) -> dict[str, Any]:
    """Complete the flow: swap the returned code for tokens and store them.

    The exchange happens here rather than in the browser because it needs the
    client secret, which must never be shipped to a client.
    """
    try:
        provider = oauth_module.get_provider(provider_key)
    except oauth_module.OAuthError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err

    # The state proves this callback belongs to a flow we started.
    if not oauth_module.consume_state(body.state):
        raise HTTPException(
            status_code=400,
            detail="this sign-in did not come from a request we started, or it expired",
        )

    try:
        tokens = oauth_module.exchange_code(
            provider, code=body.code, redirect_uri=settings.oauth_redirect_uri
        )
    except oauth_module.OAuthError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err

    expires_at = (
        datetime.fromtimestamp(tokens.expires_at, tz=UTC).replace(tzinfo=None)
        if tokens.expires_at
        else None
    )
    try:
        creds.store_credential(
            session,
            provider_id=provider.provider_id,
            secret=tokens.access_token,
            scheme="bearer",
            label=f"{provider.name} (OAuth)",
            scopes=(tokens.scope or "").split(),
            expires_at=expires_at,
            refresh_token=tokens.refresh_token,
            oauth_provider=provider.key,
        )
        session.commit()
    except creds.CredentialError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err

    return {
        "status": "connected",
        "provider": provider.key,
        "provider_id": provider.provider_id,
        # Without a refresh token the connection dies in about an hour, so the
        # caller is told plainly rather than discovering it later.
        "renewable": tokens.refresh_token is not None,
        "scopes": (tokens.scope or "").split(),
    }


class ConnectableProviderOut(BaseModel):
    provider_id: str
    name: str
    endpoints: int
    documentation_url: str | None
    base_url: str | None
    has_credential: bool
    allowlisted: bool
    schemes: list[dict[str, Any]]
    # Some providers put the token in the URL itself (Telegram's base URL is
    # literally .../bot{token}). The executor builds URLs from indexed metadata
    # and does not substitute into the host, so this is surfaced rather than
    # failing silently at run time.
    token_in_url: bool


@app.get(
    "/v1/providers/connectable",
    response_model=list[ConnectableProviderOut],
    tags=["credentials"],
)
def connectable_providers(session: SessionDep) -> list[ConnectableProviderOut]:
    """Every indexed provider, with whatever the specification says about auth.

    The credentials page is built from this rather than from a hand-written
    list. A curated list only ever covers the providers whoever wrote it
    happened to think of, which leaves the other hundred unreachable — the
    corpus has 120 providers and any of them may turn up in a plan.
    """
    counts = dict(
        session.execute(
            select(Provider.provider_id, func.count(Endpoint.endpoint_id))
            .join(Api, Api.provider_id == Provider.provider_id)
            .join(Endpoint, Endpoint.api_id == Api.api_id)
            .group_by(Provider.provider_id)
        ).all()
    )

    schemes_by_provider: dict[str, list[dict[str, Any]]] = {}
    for scheme, provider_id in session.execute(
        select(AuthScheme, Api.provider_id).join(Api, Api.api_id == AuthScheme.api_id)
    ).all():
        entry = {
            "scheme": scheme.scheme,
            "location": scheme.location,
            "parameter_name": scheme.parameter_name,
            "supported": scheme.supported,
        }
        bucket = schemes_by_provider.setdefault(provider_id, [])
        if entry not in bucket:
            bucket.append(entry)

    stored = {row.provider_id for row in session.scalars(select(Credential))}
    env_allowed = policy.env_allowlist()

    out: list[ConnectableProviderOut] = []
    for provider in session.scalars(select(Provider).order_by(Provider.provider_id)):
        count = counts.get(provider.provider_id, 0)
        if count == 0:
            continue
        base_url = provider.base_url or ""
        out.append(
            ConnectableProviderOut(
                provider_id=provider.provider_id,
                name=provider.name,
                endpoints=count,
                documentation_url=provider.documentation_url,
                base_url=provider.base_url,
                has_credential=provider.provider_id in stored,
                allowlisted=(provider.allowlisted or provider.provider_id.lower() in env_allowed),
                schemes=schemes_by_provider.get(provider.provider_id, []),
                token_in_url="{" in base_url,
            )
        )
    out.sort(key=lambda p: (-p.endpoints, p.provider_id))
    return out


class AllowlistIn(BaseModel):
    allowed: bool


@app.post("/v1/providers/{provider_id}/allowlist", tags=["credentials"])
def set_provider_allowlist(
    provider_id: str, body: AllowlistIn, session: SessionDep
) -> dict[str, Any]:
    """Permit or forbid real calls to a provider.

    Separate from having a credential on purpose: some APIs need no
    authentication, so a token cannot be the only thing standing between a
    plan and an outbound request.
    """
    try:
        allowed = policy.set_allowed(session, provider_id, body.allowed)
    except LookupError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    session.commit()
    return {"provider_id": provider_id, "allowlisted": allowed}
