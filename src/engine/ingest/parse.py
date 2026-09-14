"""Turn an OpenAPI 3.x or Swagger 2.0 document into normalized records.

Real-world specs are inconsistent: missing fields, circular `$ref`s, Swagger 2.0
mixed in with OpenAPI 3, bodies described five different ways. Everything here is
defensive — a malformed spec must produce a logged skip, never an exception that
kills the ingestion run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options")

# POST/PUT operations whose names imply an irreversible effect. Used to flag
# workflows that should require human approval before execution.
DESTRUCTIVE_HINTS = (
    "delete",
    "remove",
    "destroy",
    "purge",
    "revoke",
    "cancel",
    "terminate",
    "deactivate",
    "uninstall",
    "drop",
)

# Auth schemes this build can actually execute.
#
# oauth2 is included deliberately. What is out of scope is the OAuth *flow* —
# browser redirects, authorization-code exchange, refresh-token rotation. Using
# a token the user already holds is not: in practice every OAuth2 API accepts
# `Authorization: Bearer <token>`, which is exactly how a Slack bot token or a
# GitHub personal access token is used. Excluding oauth2 outright would reject
# most of the corpus for a capability gap that does not exist at call time.
#
# Genuinely unsupported schemes — mutual TLS, request signing such as AWS
# SigV4, vendor HMAC schemes — fall through to "other" and are still refused.
SUPPORTED_SCHEMES = {"apikey", "bearer", "basic", "oauth2", "none"}

MAX_REF_DEPTH = 8

# Plain nesting is not a cycle risk, so this only guards against pathological
# documents and must be far larger than the reference budget.
MAX_STRUCTURAL_DEPTH = 60

# Total nodes one resolved schema may contain. Without this, sibling properties
# each re-expand the same referenced schema and the result grows combinatorially
# — an unbounded run reached 5.9 GB on this corpus before being stopped.
MAX_SCHEMA_NODES = 20_000


class SpecError(ValueError):
    """The document could not be understood well enough to ingest."""


@dataclass
class ParsedParameter:
    location: str
    name: str
    type: str | None
    required: bool
    description: str | None


@dataclass
class ParsedSchema:
    direction: str
    status_code: str
    content_type: str
    json_schema: dict[str, Any]


@dataclass
class ParsedAuthScheme:
    name: str
    scheme: str
    location: str | None
    parameter_name: str | None
    scopes: list[str]
    supported: bool


@dataclass
class ParsedEndpoint:
    method: str
    path: str
    operation_id: str | None
    summary: str | None
    description: str | None
    tags: list[str]
    servers: list[str]
    security: list[str]
    is_deprecated: bool
    is_destructive: bool
    parameters: list[ParsedParameter] = field(default_factory=list)
    schemas: list[ParsedSchema] = field(default_factory=list)


@dataclass
class ParsedApi:
    provider_name: str
    title: str
    version: str
    description: str | None
    documentation_url: str | None
    base_url: str | None
    auth_schemes: list[ParsedAuthScheme]
    endpoints: list[ParsedEndpoint]


# --------------------------------------------------------------------------- refs


def _lookup(root: dict[str, Any], pointer: str) -> Any:
    node: Any = root
    for raw in pointer.lstrip("#/").split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and token in node:
            node = node[token]
        else:
            return None
    return node


class _Budget:
    """A shared node allowance for one schema expansion.

    Cycle detection alone does not bound the output. `seen` is per-branch, so
    sibling properties each re-expand the same referenced schema in full: an
    object with twenty properties referencing Event yields twenty complete
    copies, and each of those expands its own references. On real specifications
    that reached gigabytes.

    Counting emitted nodes against one shared budget bounds the total rather
    than the shape, which is what actually matters for memory.
    """

    __slots__ = ("remaining",)

    def __init__(self, limit: int) -> None:
        self.remaining = limit

    def take(self) -> bool:
        self.remaining -= 1
        return self.remaining > 0


def resolve_refs(
    node: Any,
    root: dict[str, Any],
    depth: int = 0,
    seen: frozenset[str] = frozenset(),
    structural_depth: int = 0,
    budget: _Budget | None = None,
) -> Any:
    """Inline local `$ref`s, breaking cycles rather than recursing forever.

    Three limits, each counting something different — conflating them caused
    two separate bugs.

    `depth` counts **reference hops**, so a self-referential schema (a comment
    with replies, each reply a comment) terminates.

    `structural_depth` counts **plain nesting** and only guards against
    pathological documents. It must be generous: a response object is routinely
    eight levels deep before any reference is involved, and counting ordinary
    nesting toward the reference budget truncated real schemas mid-object —
    that is how Google Calendar's event `start` lost date, dateTime and
    timeZone, and why the planner reported fields the API plainly returns did
    not exist.

    `budget` counts **total emitted nodes**, because neither of the other two
    bounds the size of the result.
    """
    if budget is None:
        budget = _Budget(MAX_SCHEMA_NODES)

    if depth > MAX_REF_DEPTH or structural_depth > MAX_STRUCTURAL_DEPTH or not budget.take():
        return {"type": "object"}

    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            if not ref.startswith("#/") or ref in seen:
                return {"type": "object"}
            target = _lookup(root, ref)
            if target is None:
                return {"type": "object"}
            # Only following a reference costs reference budget.
            return resolve_refs(target, root, depth + 1, seen | {ref}, structural_depth + 1, budget)
        return {
            key: resolve_refs(value, root, depth, seen, structural_depth + 1, budget)
            for key, value in node.items()
            if key != "$ref"
        }

    if isinstance(node, list):
        return [
            resolve_refs(item, root, depth, seen, structural_depth + 1, budget) for item in node
        ]

    return node


# ------------------------------------------------------------------------- helpers


def _is_destructive(method: str, operation_id: str | None, path: str, summary: str | None) -> bool:
    if method.upper() == "DELETE":
        return True
    haystack = " ".join(filter(None, (operation_id, path, summary))).lower()
    return any(hint in haystack for hint in DESTRUCTIVE_HINTS)


def _param_type(param: dict[str, Any]) -> str | None:
    schema = param.get("schema")
    if isinstance(schema, dict) and schema.get("type"):
        return str(schema["type"])
    if param.get("type"):
        return str(param["type"])
    return None


def _base_url_v3(spec: dict[str, Any]) -> str | None:
    servers = spec.get("servers")
    if isinstance(servers, list) and servers:
        url = servers[0].get("url") if isinstance(servers[0], dict) else None
        if isinstance(url, str) and url.startswith("http"):
            return url.rstrip("/")
    return None


def _base_url_v2(spec: dict[str, Any]) -> str | None:
    host = spec.get("host")
    if not isinstance(host, str) or not host:
        return None
    schemes = spec.get("schemes")
    scheme = schemes[0] if isinstance(schemes, list) and schemes else "https"
    base_path = spec.get("basePath") or ""
    return f"{scheme}://{host}{base_path}".rstrip("/")


# ------------------------------------------------------------------------ security


def _auth_schemes_v3(spec: dict[str, Any]) -> list[ParsedAuthScheme]:
    components = spec.get("components") or {}
    raw = components.get("securitySchemes") or {}
    return [_normalize_scheme(name, defn) for name, defn in raw.items() if isinstance(defn, dict)]


def _auth_schemes_v2(spec: dict[str, Any]) -> list[ParsedAuthScheme]:
    raw = spec.get("securityDefinitions") or {}
    return [_normalize_scheme(name, defn) for name, defn in raw.items() if isinstance(defn, dict)]


def _normalize_scheme(name: str, defn: dict[str, Any]) -> ParsedAuthScheme:
    raw_type = str(defn.get("type") or "").lower()
    scopes: list[str] = []
    if raw_type == "apikey":
        scheme = "apikey"
    elif raw_type == "http":
        scheme = str(defn.get("scheme") or "bearer").lower()
    elif raw_type == "basic":  # swagger 2.0 spelling
        scheme = "basic"
    elif raw_type == "oauth2":
        scheme = "oauth2"
        flows = defn.get("flows")
        if isinstance(flows, dict):
            for flow in flows.values():
                if isinstance(flow, dict) and isinstance(flow.get("scopes"), dict):
                    scopes.extend(flow["scopes"].keys())
        elif isinstance(defn.get("scopes"), dict):
            scopes.extend(defn["scopes"].keys())
    else:
        scheme = raw_type or "other"

    return ParsedAuthScheme(
        name=name,
        scheme=scheme,
        location=defn.get("in"),
        parameter_name=defn.get("name"),
        scopes=sorted(set(scopes))[:50],
        supported=scheme in SUPPORTED_SCHEMES,
    )


# ------------------------------------------------------------------------ bodies


def _request_schema_v3(operation: dict[str, Any], root: dict[str, Any]) -> ParsedSchema | None:
    body = operation.get("requestBody")
    if not isinstance(body, dict):
        return None
    content = body.get("content")
    if not isinstance(content, dict):
        return None
    for content_type in ("application/json", "application/x-www-form-urlencoded"):
        media = content.get(content_type)
        if isinstance(media, dict) and isinstance(media.get("schema"), dict):
            resolved = resolve_refs(media["schema"], root)
            if isinstance(resolved, dict):
                return ParsedSchema("request", "", content_type, resolved)
    return None


def _response_schema_v3(operation: dict[str, Any], root: dict[str, Any]) -> ParsedSchema | None:
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return None
    for status in ("200", "201", "202", "default"):
        response = responses.get(status)
        if not isinstance(response, dict):
            continue
        content = response.get("content")
        if not isinstance(content, dict):
            continue
        media = content.get("application/json")
        if isinstance(media, dict) and isinstance(media.get("schema"), dict):
            resolved = resolve_refs(media["schema"], root)
            if isinstance(resolved, dict):
                return ParsedSchema("response", status, "application/json", resolved)
    return None


def _response_schema_v2(operation: dict[str, Any], root: dict[str, Any]) -> ParsedSchema | None:
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return None
    for status in ("200", "201", "202", "default"):
        response = responses.get(status)
        if isinstance(response, dict) and isinstance(response.get("schema"), dict):
            resolved = resolve_refs(response["schema"], root)
            if isinstance(resolved, dict):
                return ParsedSchema("response", status, "application/json", resolved)
    return None


# ---------------------------------------------------------------------- operations


def _parse_operation(
    *,
    spec: dict[str, Any],
    swagger_2: bool,
    method: str,
    path: str,
    operation: dict[str, Any],
    path_level_params: list[Any],
    base_url: str | None,
) -> ParsedEndpoint:
    raw_params = [*path_level_params, *(operation.get("parameters") or [])]
    parameters: list[ParsedParameter] = []
    request_schema: ParsedSchema | None = None

    for raw in raw_params:
        resolved = resolve_refs(raw, spec)
        if not isinstance(resolved, dict) or not resolved.get("name"):
            continue
        location = str(resolved.get("in") or "query").lower()
        if swagger_2 and location == "body":
            schema = resolved.get("schema")
            if isinstance(schema, dict):
                inlined = resolve_refs(schema, spec)
                if isinstance(inlined, dict):
                    request_schema = ParsedSchema("request", "", "application/json", inlined)
            continue
        if location not in ("path", "query", "header", "cookie"):
            continue
        parameters.append(
            ParsedParameter(
                location=location,
                name=str(resolved["name"]),
                type=_param_type(resolved),
                required=bool(resolved.get("required", location == "path")),
                description=resolved.get("description"),
            )
        )

    if not swagger_2:
        request_schema = _request_schema_v3(operation, spec)
    response_schema = (
        _response_schema_v2(operation, spec) if swagger_2 else _response_schema_v3(operation, spec)
    )

    schemas = [s for s in (request_schema, response_schema) if s is not None]
    operation_id = operation.get("operationId")
    summary = operation.get("summary")

    security_names: list[str] = []
    for entry in operation.get("security") or spec.get("security") or []:
        if isinstance(entry, dict):
            security_names.extend(entry.keys())

    return ParsedEndpoint(
        method=method.upper(),
        path=path,
        operation_id=str(operation_id) if operation_id else None,
        summary=str(summary) if summary else None,
        description=str(operation.get("description")) if operation.get("description") else None,
        tags=[str(t) for t in (operation.get("tags") or []) if isinstance(t, str)][:10],
        servers=[base_url] if base_url else [],
        security=sorted(set(security_names)),
        is_deprecated=bool(operation.get("deprecated", False)),
        is_destructive=_is_destructive(method, operation_id, path, summary),
        parameters=parameters,
        schemas=schemas,
    )


def parse_spec(spec: dict[str, Any], *, max_endpoints: int = 400) -> ParsedApi:
    """Normalize one specification document. Raises SpecError if unusable."""
    if not isinstance(spec, dict):
        raise SpecError("spec is not an object")

    swagger_2 = "swagger" in spec and "openapi" not in spec
    paths = spec.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise SpecError("spec has no paths")

    info = spec.get("info") or {}
    if not isinstance(info, dict):
        raise SpecError("spec has no info block")

    base_url = _base_url_v2(spec) if swagger_2 else _base_url_v3(spec)
    auth_schemes = _auth_schemes_v2(spec) if swagger_2 else _auth_schemes_v3(spec)

    external_docs = spec.get("externalDocs")
    documentation_url = (external_docs.get("url") if isinstance(external_docs, dict) else None) or (
        info.get("contact") or {}
    ).get("url")

    endpoints: list[ParsedEndpoint] = []
    for path, path_item in paths.items():
        if not isinstance(path_item, dict) or len(endpoints) >= max_endpoints:
            continue
        path_level_params = path_item.get("parameters") or []
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict) or len(endpoints) >= max_endpoints:
                continue
            try:
                endpoints.append(
                    _parse_operation(
                        spec=spec,
                        swagger_2=swagger_2,
                        method=method,
                        path=str(path),
                        operation=operation,
                        path_level_params=path_level_params,
                        base_url=base_url,
                    )
                )
            except Exception as err:  # one bad operation must not lose the whole spec
                log.debug("skipping %s %s: %s", method, path, err)

    if not endpoints:
        raise SpecError("no parseable operations")

    provider_name = str(
        info.get("x-providerName")
        or (spec.get("host") or "").split(":")[0]
        or info.get("title")
        or "unknown"
    )

    return ParsedApi(
        provider_name=provider_name,
        title=str(info.get("title") or provider_name),
        version=str(info.get("version") or ""),
        description=str(info.get("description")) if info.get("description") else None,
        documentation_url=str(documentation_url) if documentation_url else None,
        base_url=base_url,
        auth_schemes=auth_schemes,
        endpoints=endpoints,
    )
