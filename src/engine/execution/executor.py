"""Node execution.

Requests are assembled from the compiled IR — method, base URL and path all
come from the endpoint record, never from anything a model wrote. Credentials
are fetched at the moment of the call and dropped immediately after.

Failures are classified rather than merely raised: the category decides whether
a retry happens, and it is the same taxonomy the recovery agent and the metrics
read.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy.orm import Session

from engine.config import settings
from engine.execution import credentials as creds
from engine.execution.guards import BlockedRequest, check_url
from engine.execution.policy import is_allowed
from engine.workflow.dag import CompiledNode

log = logging.getLogger(__name__)

# Failure taxonomy. Every failure is exactly one of these, and the category
# alone determines retry behaviour.
RETRYABLE = frozenset({"rate_limit", "network", "provider", "internal"})

# Must stay identical to the compiler's resolver, or the validator will approve
# paths the executor cannot follow. See engine.workflow.schema_match.
_ARRAY_SUFFIX = re.compile(r"\[\d*\]$")


@dataclass
class BuiltRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    body: dict[str, Any] | None = None
    idempotency_key: str | None = None

    def describe(self) -> dict[str, Any]:
        """A loggable summary. Never includes header values or the body."""
        return {
            "method": self.method,
            "url": self.url,
            "header_names": sorted(self.headers),
            "query_names": sorted(self.params),
            "body_fields": sorted(self.body) if self.body else [],
            "body_bytes": len(json.dumps(self.body)) if self.body else 0,
        }


@dataclass
class NodeOutcome:
    ok: bool
    status_code: int | None = None
    output: dict[str, Any] = field(default_factory=dict)
    error_category: str | None = None
    error_message: str | None = None
    attempts: int = 1
    latency_ms: int = 0
    request: BuiltRequest | None = None
    dry_run: bool = False


class ExecutionError(RuntimeError):
    def __init__(self, message: str, category: str) -> None:
        super().__init__(message)
        self.category = category


# ------------------------------------------------------------ request build


def _set_nested(target: dict[str, Any], dotted: str, value: Any) -> None:
    """Place a value at a dotted path, creating intermediate objects."""
    parts = dotted.split(".")
    cursor = target
    for part in parts[:-1]:
        nested = cursor.get(part)
        if not isinstance(nested, dict):
            nested = {}
            cursor[part] = nested
        cursor = nested
    cursor[parts[-1]] = value


def _read_path(data: Any, dotted: str) -> Any:
    """Read a dotted path out of an upstream response.

    Array syntax must match the compiler's resolver exactly. It accepts both
    `items[]` and `items[0]`; if this reader understood only one of them, the
    validator would approve a path the executor could not follow — a workflow
    that passes every check and then fails at runtime, which is the precise
    failure this project exists to prevent.
    """
    cursor: Any = data
    for raw in dotted.split("."):
        if cursor is None:
            return None

        match = _ARRAY_SUFFIX.search(raw)
        name = _ARRAY_SUFFIX.sub("", raw) if match else raw

        if name:
            if isinstance(cursor, dict):
                cursor = cursor.get(name)
            elif isinstance(cursor, list) and name.isdigit():
                index = int(name)
                cursor = cursor[index] if index < len(cursor) else None
            else:
                return None

        if match and cursor is not None:
            if not isinstance(cursor, list):
                return None
            # "[]" means the first element; "[n]" means that element.
            digits = match.group(0)[1:-1]
            index = int(digits) if digits else 0
            cursor = cursor[index] if index < len(cursor) else None

    return cursor


def build_request(
    node: CompiledNode,
    upstream: dict[str, dict[str, Any]],
    *,
    execution_id: str = "",
    dry_run: bool = False,
) -> BuiltRequest:
    """Assemble the outgoing request for one node.

    In a dry run the upstream nodes did not really call anything, so values
    they would have produced do not exist. Those are filled with visible
    placeholders rather than failing: the point of a dry run is to prove the
    request can be *built* and passes the egress guard, which is impossible to
    show for any step after the first if a missing upstream value aborts it.
    """
    if not node.base_url or not node.path or not node.method:
        raise ExecutionError(f"node {node.id} has no endpoint to call", "validation")

    path = node.path
    params: dict[str, Any] = {}
    headers: dict[str, str] = {"Accept": "application/json"}
    body: dict[str, Any] = {}

    for node_input in node.inputs:
        if node_input.literal is not None:
            value = node_input.literal
        else:
            source = upstream.get(node_input.source_node or "", {})
            value = _read_path(source, node_input.source_path or "")
            if value is None:
                if dry_run:
                    value = f"<dry-run:{node_input.source_node}.{node_input.source_path}>"
                else:
                    raise ExecutionError(
                        f"node {node.id}: {node_input.source_node}."
                        f"{node_input.source_path} produced no value at runtime",
                        "data_mapping",
                    )

        if node_input.location == "path":
            path = path.replace(f"{{{node_input.name}}}", str(value))
        elif node_input.location == "query":
            params[node_input.name] = value
        elif node_input.location == "header":
            headers[node_input.name] = str(value)
        else:
            _set_nested(body, node_input.name, value)

    url = f"{node.base_url.rstrip('/')}/{path.lstrip('/')}"

    request = BuiltRequest(
        method=node.method.upper(),
        url=url,
        headers=headers,
        params=params,
        body=body or None,
    )

    # Idempotency covers only calls that change something. It is derived from
    # the execution, the node and the payload, so a replay after a crash sends
    # the same key and the provider can recognise the duplicate.
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        material = json.dumps(
            {"e": execution_id, "n": node.id, "u": url, "b": body}, sort_keys=True
        )
        request.idempotency_key = hashlib.sha256(material.encode()).hexdigest()[:32]

    return request


# ---------------------------------------------------------------- dispatch


def _classify(status_code: int) -> str:
    """Map a status code onto the failure taxonomy.

    The 4xx/5xx split is the important one, because the category alone decides
    whether a retry happens. A 500 may well succeed on a second attempt; a 400
    never will — the request itself is malformed, so retrying it only burns
    quota and delays the error the user needs to see.
    """
    if status_code == 429:
        return "rate_limit"
    if status_code in (401, 403):
        return "auth"
    if 500 <= status_code < 600:
        return "provider"
    if 400 <= status_code < 500:
        return "validation"
    return "provider"


def _body_reports_failure(payload: Any) -> str | None:
    """Detect a failure hidden inside a 200 response.

    Slack replies `200 OK` with `{"ok": false, "error": "..."}`, and it is not
    alone. Trusting the status code alone means an automation reports success
    while silently doing nothing — the exact failure this project exists to
    prevent.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("ok") is False:
        return str(payload.get("error") or "provider reported ok=false")
    if payload.get("success") is False:
        return str(payload.get("error") or payload.get("message") or "success=false")
    status = payload.get("status")
    if isinstance(status, str) and status.lower() in ("error", "failure", "failed"):
        return str(payload.get("message") or payload.get("error") or status)
    return None


def _backoff_seconds(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(float(retry_after), 60.0)
        except ValueError:
            pass
    base = settings.http_backoff_base_ms / 1000.0
    # Jitter keeps many workers from retrying in lockstep after a shared outage.
    return min(base * (2 ** (attempt - 1)), 30.0) * (0.5 + random.random())


def execute_node(
    session: Session,
    node: CompiledNode,
    upstream: dict[str, dict[str, Any]],
    *,
    execution_id: str,
    dry_run: bool = False,
    client: httpx.Client | None = None,
) -> NodeOutcome:
    """Run one node, with retries for the failure categories that warrant them."""
    started = time.perf_counter()

    try:
        request = build_request(node, upstream, execution_id=execution_id, dry_run=dry_run)
    except ExecutionError as err:
        return NodeOutcome(
            ok=False, error_category=err.category, error_message=str(err), latency_ms=0
        )

    # The destination is checked before any secret is touched. Decrypting a
    # credential for a request that will not be sent is pointless, and it would
    # write an audit entry for a call that never happened.
    policy_blocked: str | None = None
    try:
        check_url(
            request.url,
            provider_id=node.provider_id,
            allowlisted=is_allowed(session, node.provider_id),
        )
    except BlockedRequest as err:
        if not dry_run:
            return NodeOutcome(
                ok=False, error_category="policy", error_message=str(err), request=request
            )
        # A dry run answers "is this request well formed?", and it is — the
        # allowlist is a rule about *sending*, not about building. Failing here
        # would hide a correctly-built request behind a policy message and make
        # the preview useless for any provider not yet enabled. The block is
        # reported instead, so the answer is the whole truth: the request is
        # fine, and it would not be sent.
        policy_blocked = str(err)

    secret = ""
    credential_missing = False
    if node.auth_scheme and node.provider_id:
        try:
            secret = creds.resolve_secret(
                session, node.provider_id, execution_id=execution_id, node_id=node.id
            )
        except creds.CredentialError as err:
            if not dry_run:
                return NodeOutcome(
                    ok=False, error_category="auth", error_message=str(err), request=request
                )
            # A dry run exists to preview a workflow before it is wired up, so a
            # missing credential is reported rather than fatal. Failing here
            # would mean you could not inspect a plan until every provider
            # secret was already configured, which is backwards.
            credential_missing = True
            secret = "<dry-run:credential-not-configured>"
        creds.apply_auth(
            request.headers,
            request.params,
            scheme=node.auth_scheme,
            secret=secret,
            location=node.auth_location,
            parameter_name=node.auth_parameter,
        )

    if dry_run:
        # The real request is built and checked, then deliberately not sent.
        log.info("dry run %s %s", request.method, request.url)
        if credential_missing:
            log.info("dry run: no credential configured for %s", node.provider_id)
        if policy_blocked:
            log.info("dry run: request would be blocked — %s", policy_blocked)
        return NodeOutcome(
            ok=True,
            output={
                "dry_run": True,
                "request": request.describe(),
                "credential_missing": credential_missing,
                "would_be_blocked": policy_blocked,
            },
            request=request,
            dry_run=True,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    if request.idempotency_key:
        request.headers.setdefault("Idempotency-Key", request.idempotency_key)

    owns_client = client is None
    client = client or httpx.Client(
        timeout=settings.http_timeout_seconds,
        # Redirects are not followed automatically: a redirect target must be
        # re-checked against the egress guard, not trusted because the first
        # host was allowlisted.
        follow_redirects=False,
    )

    last_category = "internal"
    last_message = "no attempt completed"
    status_code: int | None = None
    retry_after: str | None = None
    attempt = 0

    try:
        for attempt in range(1, settings.http_max_retries + 2):
            try:
                response = client.request(
                    request.method,
                    request.url,
                    headers=request.headers,
                    params=request.params or None,
                    json=request.body,
                )
            except httpx.TimeoutException as err:
                last_category, last_message = "network", f"timeout: {err}"
                retry_after = None
            except httpx.HTTPError as err:
                last_category = "network"
                last_message = creds.redact(f"network error: {err}", secret)
                retry_after = None
            else:
                status_code = response.status_code
                # Honour the provider's own pacing when it supplies one.
                retry_after = response.headers.get("retry-after")
                if 300 <= status_code < 400:
                    last_category = "provider"
                    last_message = f"unexpected redirect to {response.headers.get('location')}"
                elif status_code >= 400:
                    last_category = _classify(status_code)
                    last_message = creds.redact(
                        f"http {status_code}: {response.text[:300]}", secret
                    )
                else:
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = {"raw": response.text[:2000]}

                    hidden = _body_reports_failure(payload)
                    if hidden:
                        # A 2xx that actually failed is a provider error, not a
                        # success, and it is not retryable — the request itself
                        # was wrong.
                        return NodeOutcome(
                            ok=False,
                            status_code=status_code,
                            error_category="provider",
                            error_message=creds.redact(hidden, secret),
                            attempts=attempt,
                            latency_ms=int((time.perf_counter() - started) * 1000),
                            request=request,
                        )

                    return NodeOutcome(
                        ok=True,
                        status_code=status_code,
                        output=payload if isinstance(payload, dict) else {"data": payload},
                        attempts=attempt,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        request=request,
                    )

            if last_category not in RETRYABLE or attempt > settings.http_max_retries:
                break

            delay = _backoff_seconds(attempt, retry_after)
            log.warning(
                "node %s attempt %d failed (%s), retrying in %.1fs",
                node.id,
                attempt,
                last_category,
                delay,
            )
            time.sleep(delay)
    finally:
        if owns_client:
            client.close()
        secret = ""  # drop the plaintext as soon as the call is done

    return NodeOutcome(
        ok=False,
        status_code=status_code,
        error_category=last_category,
        error_message=last_message,
        attempts=attempt,
        latency_ms=int((time.perf_counter() - started) * 1000),
        request=request,
    )
