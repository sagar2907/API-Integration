"""The deterministic validator and compiler.

This is the gate the project's central claim rests on: *LLMs propose,
deterministic systems validate and execute*. Nothing reaches the executor
without passing every check here, and no check consults a language model.

Seven passes, in order, cheapest and most fundamental first:

    1. Structural    - one trigger, no cycles, every node reachable
    2. Existence     - every endpoint_id resolves to a live database row
    3. Agreement     - method and path match the record, not the plan's claim
    4. Parameters    - every required path/query/header parameter has a source
    5. Body          - every required request-body field has a source
    6. Compatibility - upstream output actually fits downstream input
    7. Policy        - auth supported, provider allowlisted, destructive gated

Later passes are skipped for nodes that already failed an earlier one, because
a check built on an unresolved endpoint would report noise rather than a cause.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from engine.config import settings
from engine.db import Api, AuthScheme, Endpoint, Parameter, Provider, SchemaRecord
from engine.workflow import schema_match
from engine.workflow.dag import (
    VALID_LOCATIONS,
    Code,
    CompiledInput,
    CompiledNode,
    CompiledWorkflow,
    CompileResult,
    ValidationIssue,
    WorkflowDefinition,
    WorkflowNode,
)

log = logging.getLogger(__name__)

# Node types that must name a real endpoint. Transform, condition and approval
# nodes do work inside the engine and have nothing to resolve.
ENDPOINT_NODE_TYPES = frozenset({"trigger", "api_call"})


class _EndpointRecord:
    """Everything the compiler needs about one endpoint, loaded once."""

    __slots__ = (
        "endpoint",
        "provider",
        "api",
        "parameters",
        "request_schema",
        "response_schema",
        "auth_schemes",
    )

    def __init__(
        self,
        endpoint: Endpoint,
        api: Api,
        provider: Provider,
        parameters: list[Parameter],
        request_schema: dict[str, Any] | None,
        response_schema: dict[str, Any] | None,
        auth_schemes: list[AuthScheme],
    ) -> None:
        self.endpoint = endpoint
        self.api = api
        self.provider = provider
        self.parameters = parameters
        self.request_schema = request_schema
        self.response_schema = response_schema
        self.auth_schemes = auth_schemes


def _load_endpoints(session: Session, endpoint_ids: set[str]) -> dict[str, _EndpointRecord]:
    """Fetch every referenced endpoint with its parameters, schemas and auth."""
    if not endpoint_ids:
        return {}

    rows = session.execute(
        select(Endpoint, Api, Provider)
        .join(Api, Api.api_id == Endpoint.api_id)
        .join(Provider, Provider.provider_id == Api.provider_id)
        .where(Endpoint.endpoint_id.in_(endpoint_ids))
    ).all()

    found = {row[0].endpoint_id: row for row in rows}
    if not found:
        return {}

    parameters: dict[str, list[Parameter]] = {}
    for parameter in session.scalars(
        select(Parameter).where(Parameter.endpoint_id.in_(found.keys()))
    ):
        parameters.setdefault(parameter.endpoint_id, []).append(parameter)

    schemas: dict[tuple[str, str], dict[str, Any]] = {}
    for record in session.scalars(
        select(SchemaRecord).where(SchemaRecord.endpoint_id.in_(found.keys()))
    ):
        key = (record.endpoint_id, record.direction)
        # Keep the first response schema seen; ingestion stores the 2xx one.
        schemas.setdefault(key, record.json_schema or {})

    api_ids = {row[1].api_id for row in found.values()}
    auth_by_api: dict[str, list[AuthScheme]] = {}
    for scheme in session.scalars(select(AuthScheme).where(AuthScheme.api_id.in_(api_ids))):
        auth_by_api.setdefault(scheme.api_id, []).append(scheme)

    return {
        endpoint_id: _EndpointRecord(
            endpoint=endpoint,
            api=api,
            provider=provider,
            parameters=parameters.get(endpoint_id, []),
            request_schema=schemas.get((endpoint_id, "request")),
            response_schema=schemas.get((endpoint_id, "response")),
            auth_schemes=auth_by_api.get(api.api_id, []),
        )
        for endpoint_id, (endpoint, api, provider) in found.items()
    }


# ------------------------------------------------------------- 1. structural


def _check_structure(definition: WorkflowDefinition) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    seen: set[str] = set()
    for node in definition.nodes:
        if node.id in seen:
            issues.append(
                ValidationIssue(
                    code=Code.DUPLICATE_NODE_ID,
                    node_id=node.id,
                    message=f"node id {node.id!r} appears more than once",
                    hint="every node needs a unique id",
                )
            )
        seen.add(node.id)

    for edge in definition.edges:
        for end, node_id in (("source", edge.source), ("target", edge.target)):
            if node_id not in seen:
                issues.append(
                    ValidationIssue(
                        code=Code.UNKNOWN_NODE_REFERENCE,
                        field=f"edge.{end}",
                        actual=node_id,
                        message=f"edge {end} {node_id!r} is not a node in this workflow",
                    )
                )

    triggers = [n for n in definition.nodes if n.type == "trigger"]
    if not triggers:
        issues.append(
            ValidationIssue(
                code=Code.NO_TRIGGER,
                message="workflow has no trigger node",
                hint="exactly one node must have type 'trigger'",
            )
        )
    elif len(triggers) > 1:
        issues.append(
            ValidationIssue(
                code=Code.MULTIPLE_TRIGGERS,
                message=f"workflow has {len(triggers)} trigger nodes, expected exactly one",
                actual=", ".join(t.id for t in triggers),
            )
        )

    # Cycles make execution order undefined, so this must be caught before any
    # attempt to order the graph.
    if _has_cycle(definition):
        issues.append(
            ValidationIssue(
                code=Code.CYCLE_DETECTED,
                message="workflow edges form a cycle",
                hint="a workflow must be a directed acyclic graph",
            )
        )
    elif triggers:
        reachable = _reachable_from(definition, triggers[0].id)
        for node in definition.nodes:
            if node.id not in reachable:
                issues.append(
                    ValidationIssue(
                        code=Code.UNREACHABLE_NODE,
                        node_id=node.id,
                        message=f"node {node.id!r} is never reached from the trigger",
                        hint="connect it with an edge, or remove it",
                    )
                )

    return issues


def _adjacency(definition: WorkflowDefinition) -> dict[str, list[str]]:
    graph: dict[str, list[str]] = {node.id: [] for node in definition.nodes}
    for edge in definition.edges:
        if edge.source in graph and edge.target in graph:
            graph[edge.source].append(edge.target)
    return graph


def _has_cycle(definition: WorkflowDefinition) -> bool:
    graph = _adjacency(definition)
    visiting: set[str] = set()
    done: set[str] = set()

    def walk(node_id: str) -> bool:
        if node_id in visiting:
            return True
        if node_id in done:
            return False
        visiting.add(node_id)
        for neighbour in graph.get(node_id, []):
            if walk(neighbour):
                return True
        visiting.discard(node_id)
        done.add(node_id)
        return False

    return any(walk(node_id) for node_id in graph)


def _reachable_from(definition: WorkflowDefinition, start: str) -> set[str]:
    graph = _adjacency(definition)
    seen = {start}
    stack = [start]
    while stack:
        for neighbour in graph.get(stack.pop(), []):
            if neighbour not in seen:
                seen.add(neighbour)
                stack.append(neighbour)
    return seen


def _topological_order(definition: WorkflowDefinition) -> list[str]:
    graph = _adjacency(definition)
    indegree = {node.id: 0 for node in definition.nodes}
    for targets in graph.values():
        for target in targets:
            indegree[target] += 1

    queue = [node.id for node in definition.nodes if indegree[node.id] == 0]
    order: list[str] = []
    while queue:
        node_id = queue.pop(0)
        order.append(node_id)
        for neighbour in graph.get(node_id, []):
            indegree[neighbour] -= 1
            if indegree[neighbour] == 0:
                queue.append(neighbour)
    return order


# ------------------------------------------ 2-3. existence and agreement


def _check_endpoint(
    node: WorkflowNode, records: dict[str, _EndpointRecord]
) -> tuple[_EndpointRecord | None, list[ValidationIssue]]:
    if node.type not in ENDPOINT_NODE_TYPES:
        return None, []

    if not node.endpoint_id:
        return None, [
            ValidationIssue(
                code=Code.MISSING_ENDPOINT_ID,
                node_id=node.id,
                message=f"{node.type} node {node.id!r} does not name an endpoint",
                hint="every trigger and api_call node must reference an endpoint_id",
            )
        ]

    record = records.get(node.endpoint_id)
    if record is None:
        # The anti-hallucination check. An id that resolves to nothing is either
        # invented or stale, and either way it must never execute.
        return None, [
            ValidationIssue(
                code=Code.ENDPOINT_NOT_FOUND,
                node_id=node.id,
                field="endpoint_id",
                actual=node.endpoint_id,
                message=f"endpoint {node.endpoint_id!r} does not exist in the knowledge base",
                hint="endpoint ids must come from a search result, never be constructed",
            )
        ]

    issues: list[ValidationIssue] = []
    if record.endpoint.is_deprecated:
        issues.append(
            ValidationIssue(
                code=Code.ENDPOINT_DEPRECATED,
                node_id=node.id,
                field="endpoint_id",
                actual=node.endpoint_id,
                message=f"endpoint {record.endpoint.path} is marked deprecated",
            )
        )

    # The plan may restate method and path for readability. If it does, the
    # restatement is checked against the record rather than trusted.
    claimed_method = node.config.get("method")
    if isinstance(claimed_method, str) and claimed_method.upper() != record.endpoint.method:
        issues.append(
            ValidationIssue(
                code=Code.METHOD_MISMATCH,
                node_id=node.id,
                field="method",
                expected=record.endpoint.method,
                actual=claimed_method.upper(),
                message=f"node claims {claimed_method.upper()} but the endpoint is "
                f"{record.endpoint.method}",
            )
        )

    claimed_path = node.config.get("path")
    if isinstance(claimed_path, str) and claimed_path != record.endpoint.path:
        issues.append(
            ValidationIssue(
                code=Code.PATH_MISMATCH,
                node_id=node.id,
                field="path",
                expected=record.endpoint.path,
                actual=claimed_path,
                message="node claims a path that does not match the endpoint record",
            )
        )

    if not record.endpoint.servers and not record.provider.base_url:
        issues.append(
            ValidationIssue(
                code=Code.NO_BASE_URL,
                node_id=node.id,
                message=f"no base URL known for {record.provider.provider_id}",
                hint="requests are built from indexed metadata, so a base URL is required",
            )
        )

    return record, issues


# ---------------------------------------------------- 4-5. inputs and body


def _check_inputs(
    node: WorkflowNode, record: _EndpointRecord
) -> tuple[list[CompiledInput], list[ValidationIssue]]:
    issues: list[ValidationIssue] = []
    compiled: list[CompiledInput] = []

    supplied: dict[str, set[str]] = {location: set() for location in VALID_LOCATIONS}

    for node_input in node.inputs:
        location = node_input.location
        if location not in VALID_LOCATIONS:
            issues.append(
                ValidationIssue(
                    code=Code.INVALID_TARGET,
                    node_id=node.id,
                    field=node_input.target,
                    expected="one of " + ", ".join(VALID_LOCATIONS),
                    actual=location,
                    message=f"input target {node_input.target!r} has an unknown location",
                    hint="targets look like 'path.owner', 'query.state' or 'body.text'",
                )
            )
            continue

        name = node_input.field_path
        if not name:
            issues.append(
                ValidationIssue(
                    code=Code.INVALID_TARGET,
                    node_id=node.id,
                    field=node_input.target,
                    message=f"input target {node_input.target!r} names no field",
                )
            )
            continue

        supplied[location].add(name.split(".")[0])
        compiled.append(
            CompiledInput(
                location=location,  # type: ignore[arg-type]
                name=name,
                source_node=node_input.source_node,
                source_path=node_input.source_path,
                literal=node_input.literal,
            )
        )

    # 4. Required parameters
    for parameter in record.parameters:
        if not parameter.required:
            continue
        if parameter.name not in supplied.get(parameter.location, set()):
            issues.append(
                ValidationIssue(
                    code=Code.MISSING_REQUIRED_PARAMETER,
                    node_id=node.id,
                    field=f"{parameter.location}.{parameter.name}",
                    expected=parameter.type or "value",
                    message=f"required {parameter.location} parameter "
                    f"{parameter.name!r} has no source",
                    hint=f"add an input targeting '{parameter.location}.{parameter.name}'",
                )
            )

    # Parameters the endpoint does not declare are a likely invention, but path
    # templating and vendor extensions make this unreliable, so only path
    # parameters — which are unambiguous — are reported.
    declared_path_params = {p.name for p in record.parameters if p.location == "path"}
    for name in supplied["path"]:
        if declared_path_params and name not in declared_path_params:
            issues.append(
                ValidationIssue(
                    code=Code.UNKNOWN_PARAMETER,
                    node_id=node.id,
                    field=f"path.{name}",
                    expected=", ".join(sorted(declared_path_params)) or "(none)",
                    actual=name,
                    message=f"endpoint has no path parameter named {name!r}",
                )
            )

    # 5. Required body fields
    for field in schema_match.required_fields(record.request_schema):
        if field not in supplied["body"]:
            issues.append(
                ValidationIssue(
                    code=Code.MISSING_REQUIRED_BODY_FIELD,
                    node_id=node.id,
                    field=f"body.{field}",
                    message=f"required request body field {field!r} has no source",
                    hint=f"add an input targeting 'body.{field}'",
                )
            )

    return compiled, issues


# -------------------------------------------------------- 6. compatibility


def _target_schema(record: _EndpointRecord, compiled: CompiledInput) -> dict[str, Any] | None:
    if compiled.location == "body":
        return schema_match.resolve_path(record.request_schema, compiled.name)
    parameter = next(
        (
            p
            for p in record.parameters
            if p.location == compiled.location and p.name == compiled.name.split(".")[0]
        ),
        None,
    )
    return schema_match.parameter_schema(parameter.type) if parameter else None


def _check_compatibility(
    node: WorkflowNode,
    record: _EndpointRecord,
    compiled_inputs: list[CompiledInput],
    records: dict[str, _EndpointRecord],
    definition: WorkflowDefinition,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    for compiled in compiled_inputs:
        if compiled.source_node is None or compiled.source_path is None:
            continue  # literals carry no upstream schema to compare

        upstream = definition.node(compiled.source_node)
        if upstream is None:
            issues.append(
                ValidationIssue(
                    code=Code.UNKNOWN_NODE_REFERENCE,
                    node_id=node.id,
                    field=compiled.location + "." + compiled.name,
                    actual=compiled.source_node,
                    message=f"input reads from {compiled.source_node!r}, which is not a node",
                )
            )
            continue

        upstream_record = records.get(upstream.endpoint_id or "")
        if upstream_record is None:
            continue  # non-endpoint upstream (transform); output shape unknown

        source_schema = schema_match.resolve_path(
            upstream_record.response_schema, compiled.source_path
        )
        if source_schema is None:
            if upstream_record.response_schema:
                issues.append(
                    ValidationIssue(
                        code=Code.UNRESOLVABLE_SOURCE_PATH,
                        node_id=node.id,
                        field=f"{compiled.location}.{compiled.name}",
                        actual=f"{compiled.source_node}.{compiled.source_path}",
                        message=f"{upstream_record.endpoint.path} does not return "
                        f"{compiled.source_path!r}",
                        hint="check the field path against the endpoint's response schema",
                    )
                )
            continue

        target_schema = _target_schema(record, compiled)
        if target_schema is None:
            continue

        result = schema_match.compare(source_schema, target_schema)
        if not result.acceptable:
            issues.append(
                ValidationIssue(
                    code=Code.SCHEMA_INCOMPATIBLE,
                    node_id=node.id,
                    field=f"{compiled.location}.{compiled.name}",
                    expected=result.target_type,
                    actual=result.source_type,
                    message=f"{compiled.source_path!r} is a {result.source_type} but "
                    f"{compiled.name!r} needs a {result.target_type}",
                    hint=result.detail,
                )
            )

    return issues


# --------------------------------------------------------------- 7. policy


def _check_policy(
    node: WorkflowNode,
    record: _EndpointRecord,
    definition: WorkflowDefinition,
    *,
    enforce_allowlist: bool,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    if record.auth_schemes:
        supported = [s for s in record.auth_schemes if s.supported]
        if not supported:
            schemes = ", ".join(sorted({s.scheme for s in record.auth_schemes}))
            issues.append(
                ValidationIssue(
                    code=Code.UNSUPPORTED_AUTH,
                    node_id=node.id,
                    field="auth",
                    actual=schemes,
                    expected="apikey, bearer or basic",
                    message=f"{record.provider.provider_id} requires {schemes}, "
                    "which this engine cannot perform",
                    hint="reject now rather than failing at execution time",
                )
            )

    if enforce_allowlist:
        allowed = {p.lower() for p in settings.allowlisted_providers}
        provider_id = record.provider.provider_id.lower()
        provider_name = (record.provider.name or "").lower()
        # The provider row carries the same permission when it was granted from
        # the interface rather than the environment.
        if not record.provider.allowlisted and not any(
            allowed_entry.replace(".", "_") == provider_id or allowed_entry == provider_name
            for allowed_entry in allowed
        ):
            issues.append(
                ValidationIssue(
                    code=Code.PROVIDER_NOT_ALLOWLISTED,
                    node_id=node.id,
                    field="provider",
                    actual=record.provider.provider_id,
                    message=f"{record.provider.provider_id} is not on the execution allowlist",
                    hint="add it to EXECUTION_ALLOWLIST once its credentials are configured",
                )
            )

    if record.endpoint.is_destructive:
        approvals = {n.id for n in definition.nodes if n.type == "approval"}
        upstream_ids = _ancestors(definition, node.id)
        if not (approvals & upstream_ids):
            issues.append(
                ValidationIssue(
                    code=Code.DESTRUCTIVE_REQUIRES_APPROVAL,
                    node_id=node.id,
                    message=f"{record.endpoint.method} {record.endpoint.path} is destructive "
                    "and has no approval step before it",
                    hint="insert an approval node upstream of this call",
                )
            )

    return issues


def _ancestors(definition: WorkflowDefinition, node_id: str) -> set[str]:
    reverse: dict[str, list[str]] = {node.id: [] for node in definition.nodes}
    for edge in definition.edges:
        if edge.target in reverse:
            reverse[edge.target].append(edge.source)
    seen: set[str] = set()
    stack = list(reverse.get(node_id, []))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(reverse.get(current, []))
    return seen


# ---------------------------------------------------------------- compile


def compile_workflow(
    session: Session,
    definition: WorkflowDefinition,
    *,
    enforce_allowlist: bool | None = None,
) -> CompileResult:
    """Validate a workflow and, if it passes every check, compile it.

    Returns a result that is either a compiled workflow or a list of structured
    reasons. There is no partial success: a workflow with any issue does not
    execute.
    """
    if enforce_allowlist is None:
        enforce_allowlist = settings.execution_allowlist_only

    issues = _check_structure(definition)

    endpoint_ids = {n.endpoint_id for n in definition.nodes if n.endpoint_id}
    records = _load_endpoints(session, endpoint_ids)

    compiled_nodes: list[CompiledNode] = []
    dependencies: dict[str, list[str]] = {n.id: [] for n in definition.nodes}
    for edge in definition.edges:
        if edge.target in dependencies:
            dependencies[edge.target].append(edge.source)

    for node in definition.nodes:
        record, node_issues = _check_endpoint(node, records)
        issues.extend(node_issues)

        compiled_inputs: list[CompiledInput] = []
        if record is not None and not any(
            i.code in (Code.ENDPOINT_NOT_FOUND, Code.MISSING_ENDPOINT_ID) for i in node_issues
        ):
            compiled_inputs, input_issues = _check_inputs(node, record)
            issues.extend(input_issues)
            issues.extend(_check_compatibility(node, record, compiled_inputs, records, definition))
            issues.extend(
                _check_policy(node, record, definition, enforce_allowlist=enforce_allowlist)
            )

        auth = next((s for s in record.auth_schemes if s.supported), None) if record else None
        compiled_nodes.append(
            CompiledNode(
                id=node.id,
                type=node.type,
                endpoint_id=node.endpoint_id,
                provider_id=record.provider.provider_id if record else None,
                method=record.endpoint.method if record else None,
                # Built from indexed metadata only. No plan-supplied URL can
                # reach the executor, which is what makes SSRF and fabricated
                # hosts structurally impossible rather than merely unlikely.
                base_url=(
                    (record.endpoint.servers or [None])[0] or record.provider.base_url
                    if record
                    else None
                ),
                path=record.endpoint.path if record else None,
                auth_scheme=auth.scheme if auth else None,
                auth_location=auth.location if auth else None,
                auth_parameter=auth.parameter_name if auth else None,
                is_destructive=bool(record.endpoint.is_destructive) if record else False,
                inputs=compiled_inputs,
                depends_on=dependencies.get(node.id, []),
            )
        )

    if issues:
        log.info("workflow %r rejected with %d issue(s)", definition.name, len(issues))
        return CompileResult(ok=False, issues=issues)

    return CompileResult(
        ok=True,
        workflow=CompiledWorkflow(
            name=definition.name,
            nodes=compiled_nodes,
            execution_order=_topological_order(definition),
        ),
    )
