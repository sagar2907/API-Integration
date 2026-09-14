"""Workflow definitions and validation errors.

These Pydantic models are the contract shared by the planner (which produces
them), the compiler (which rejects them), and the executor (which runs the
compiled form). Defining them once is the main reason the whole backend is one
language — in a split stack these shapes would be declared twice and drift.

Every model forbids unknown fields. A plan is largely model-generated, and
silently accepting an invented key is exactly how hallucinated structure would
slip past the gate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

NodeType = Literal["trigger", "api_call", "transform", "condition", "validate", "approval"]

# Where a value lands in the outgoing request.
InputLocation = Literal["path", "query", "header", "body"]

VALID_LOCATIONS: tuple[str, ...] = ("path", "query", "header", "body")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------- definition


class NodeInput(Strict):
    """One value flowing into a node.

    `target` is location-prefixed — `path.owner`, `query.state`, `header.token`,
    `body.message.text` — so the compiler knows which part of the request the
    value belongs to without guessing.

    The value comes either from an upstream node's output or from a literal,
    never both. Requiring exactly one source removes a whole class of ambiguous
    plans before any deeper checking starts.
    """

    target: str
    source_node: str | None = None
    source_path: str | None = None
    literal: Any | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> NodeInput:
        from_upstream = self.source_node is not None
        from_literal = self.literal is not None
        if from_upstream == from_literal:
            raise ValueError(
                f"input {self.target!r} must have exactly one source: "
                "either source_node + source_path, or literal"
            )
        if from_upstream and not self.source_path:
            raise ValueError(f"input {self.target!r} has source_node but no source_path")
        return self

    @property
    def location(self) -> str:
        return self.target.split(".", 1)[0]

    @property
    def field_path(self) -> str:
        parts = self.target.split(".", 1)
        return parts[1] if len(parts) > 1 else ""


class WorkflowNode(Strict):
    id: str
    type: NodeType
    endpoint_id: str | None = None
    name: str | None = None
    inputs: list[NodeInput] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class Edge(Strict):
    source: str
    target: str


class WorkflowDefinition(Strict):
    name: str
    description: str | None = None
    nodes: list[WorkflowNode]
    edges: list[Edge] = Field(default_factory=list)

    def node(self, node_id: str) -> WorkflowNode | None:
        return next((n for n in self.nodes if n.id == node_id), None)


# ------------------------------------------------------------------- errors


class ValidationIssue(Strict):
    """A structured rejection reason.

    Deliberately not a string. The frontend renders these, and the planner's
    repair loop consumes them — a free-text message is a dead end for both.
    """

    code: str
    message: str
    node_id: str | None = None
    field: str | None = None
    expected: str | None = None
    actual: str | None = None
    hint: str | None = None


# Every rejection the compiler can produce. Kept as constants so tests and the
# repair loop reference the same identifiers the compiler emits.
class Code:
    DUPLICATE_NODE_ID = "duplicate_node_id"
    UNKNOWN_NODE_REFERENCE = "unknown_node_reference"
    CYCLE_DETECTED = "cycle_detected"
    NO_TRIGGER = "no_trigger"
    MULTIPLE_TRIGGERS = "multiple_triggers"
    UNREACHABLE_NODE = "unreachable_node"
    MISSING_ENDPOINT_ID = "missing_endpoint_id"
    ENDPOINT_NOT_FOUND = "endpoint_not_found"
    ENDPOINT_DEPRECATED = "endpoint_deprecated"
    METHOD_MISMATCH = "method_mismatch"
    PATH_MISMATCH = "path_mismatch"
    INVALID_TARGET = "invalid_target"
    MISSING_REQUIRED_PARAMETER = "missing_required_parameter"
    UNKNOWN_PARAMETER = "unknown_parameter"
    MISSING_REQUIRED_BODY_FIELD = "missing_required_body_field"
    UNRESOLVABLE_SOURCE_PATH = "unresolvable_source_path"
    SCHEMA_INCOMPATIBLE = "schema_incompatible"
    UNSUPPORTED_AUTH = "unsupported_auth"
    PROVIDER_NOT_ALLOWLISTED = "provider_not_allowlisted"
    DESTRUCTIVE_REQUIRES_APPROVAL = "destructive_requires_approval"
    NO_BASE_URL = "no_base_url"


# --------------------------------------------------------------- compiled IR


class CompiledInput(Strict):
    location: InputLocation
    name: str
    source_node: str | None = None
    source_path: str | None = None
    literal: Any | None = None


class CompiledNode(Strict):
    """A node reduced to exactly what the executor needs.

    The executor never reads the original plan — it reads this. Method, path and
    base URL come from the endpoint record rather than from anything the planner
    wrote, which is what makes a fabricated URL structurally impossible.
    """

    id: str
    type: NodeType
    endpoint_id: str | None = None
    provider_id: str | None = None
    method: str | None = None
    base_url: str | None = None
    path: str | None = None
    auth_scheme: str | None = None
    auth_location: str | None = None
    auth_parameter: str | None = None
    is_destructive: bool = False
    inputs: list[CompiledInput] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


class CompiledWorkflow(Strict):
    name: str
    nodes: list[CompiledNode]
    execution_order: list[str]
    compiled_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def node(self, node_id: str) -> CompiledNode | None:
        return next((n for n in self.nodes if n.id == node_id), None)


class CompileResult(Strict):
    """Either a compiled workflow or the reasons it was refused. Never both."""

    ok: bool
    workflow: CompiledWorkflow | None = None
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def codes(self) -> list[str]:
        return [issue.code for issue in self.issues]
