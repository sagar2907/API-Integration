"""Saving and loading workflows.

Workflows are versioned by insert. Editing never rewrites a row: a change
creates a new version pointing back at its parent, so an execution can always
be traced to the exact definition it ran.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from engine import ids
from engine.db import CompiledIR, Workflow, WorkflowNodeRow
from engine.workflow.dag import CompiledWorkflow, WorkflowDefinition


def _workflow_id(name: str, version: int) -> str:
    return f"wf_{ids.digest(name, str(version), datetime.now(UTC).isoformat())}"


def save_workflow(
    session: Session,
    definition: WorkflowDefinition,
    *,
    owner_id: str = "local",
    status: str = "draft",
    parent_version: str | None = None,
) -> Workflow:
    """Persist a new workflow version and its denormalized node rows."""
    version = 1
    if parent_version:
        parent = session.get(Workflow, parent_version)
        if parent is not None:
            version = parent.version + 1

    workflow_id = _workflow_id(definition.name, version)
    row = Workflow(
        workflow_id=workflow_id,
        owner_id=owner_id,
        name=definition.name,
        version=version,
        status=status,
        definition=json.loads(definition.model_dump_json()),
        parent_version=parent_version,
    )
    session.add(row)

    for node in definition.nodes:
        session.add(
            WorkflowNodeRow(
                node_id=f"{workflow_id}:{node.id}",
                workflow_id=workflow_id,
                type=node.type,
                endpoint_id=node.endpoint_id,
                configuration=json.loads(node.model_dump_json()),
            )
        )

    session.flush()
    return row


def load_definition(session: Session, workflow_id: str) -> WorkflowDefinition | None:
    row = session.get(Workflow, workflow_id)
    if row is None:
        return None
    return WorkflowDefinition.model_validate(row.definition)


def save_compiled(session: Session, workflow: Workflow, compiled: CompiledWorkflow) -> CompiledIR:
    """Store the compiled form. Stored, not recomputed, so runs are reproducible."""
    row = CompiledIR(
        ir_id=f"ir_{ids.digest(workflow.workflow_id, str(workflow.version))}",
        workflow_id=workflow.workflow_id,
        workflow_version=workflow.version,
        ir=json.loads(compiled.model_dump_json()),
    )
    session.merge(row)
    session.flush()
    return row


def latest_compiled(session: Session, workflow_id: str) -> CompiledIR | None:
    return session.scalar(
        select(CompiledIR)
        .where(CompiledIR.workflow_id == workflow_id)
        .order_by(CompiledIR.compiled_at.desc())
        .limit(1)
    )


def list_workflows(session: Session, *, owner_id: str = "local", limit: int = 50) -> list[Workflow]:
    return list(
        session.scalars(
            select(Workflow)
            .where(Workflow.owner_id == owner_id)
            .order_by(Workflow.created_at.desc())
            .limit(limit)
        )
    )
