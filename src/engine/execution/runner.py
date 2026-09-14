"""Workflow execution: ordering, checkpointing, resumption.

The ordering here is the whole reliability story:

    run the node  ->  write its result  ->  only then mark it done

A crash between the call and the write replays the node, and the idempotency
key makes that replay safe. A crash between the write and the mark also
replays, and the stored checkpoint short-circuits it. What must never happen is
marking a node done before its result is durable — that loses work silently,
which is worse than doing it twice.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from engine import ids
from engine.config import settings
from engine.db import (
    CompiledIR,
    Execution,
    ExecutionEvent,
    Job,
    NodeResult,
    naive_utcnow,
)
from engine.execution.executor import NodeOutcome, execute_node
from engine.workflow.dag import CompiledWorkflow

log = logging.getLogger(__name__)


def _event(
    session: Session,
    execution_id: str,
    node_id: str | None,
    event_type: str,
    *,
    outcome: NodeOutcome | None = None,
) -> None:
    """Append to the execution log.

    Only shapes and sizes are recorded — never request or response bodies,
    which may carry user data or secrets.
    """
    session.add(
        ExecutionEvent(
            execution_id=execution_id,
            node_id=node_id,
            event_type=event_type,
            attempt=outcome.attempts if outcome else 1,
            latency_ms=outcome.latency_ms if outcome else None,
            status_code=outcome.status_code if outcome else None,
            error_category=outcome.error_category if outcome else None,
            payload_metadata=(
                {
                    "request": outcome.request.describe() if outcome and outcome.request else {},
                    "output_fields": sorted(outcome.output)[:20] if outcome else [],
                    "error": (outcome.error_message or "")[:300] if outcome else "",
                }
                if outcome
                else {}
            ),
        )
    )


def create_execution(
    session: Session,
    *,
    workflow_id: str,
    ir_id: str | None = None,
    trigger_type: str = "manual",
    dry_run: bool = False,
) -> Execution:
    execution = Execution(
        execution_id=f"ex_{ids.digest(workflow_id, datetime.now(UTC).isoformat())}",
        workflow_id=workflow_id,
        ir_id=ir_id,
        trigger_type=trigger_type,
        dry_run=dry_run,
        status="pending",
    )
    session.add(execution)
    session.flush()
    return execution


def enqueue(session: Session, execution: Execution) -> Job:
    job = Job(
        job_id=f"jb_{ids.digest(execution.execution_id)}",
        execution_id=execution.execution_id,
        status="queued",
    )
    session.add(job)
    session.flush()
    return job


def claim_job(session: Session, *, worker_id: str, lease_seconds: int | None = None) -> Job | None:
    """Take the next queued job, or reclaim one whose lease expired.

    An expired lease is how a crashed worker's job returns to circulation: the
    worker that died never renewed it, so after the lease elapses another
    worker may take it. This is at-least-once delivery, which is why the node
    checkpoints and idempotency keys matter.
    """
    lease_seconds = lease_seconds or settings.queue_lease_seconds
    now = naive_utcnow()
    candidate = session.scalar(
        select(Job)
        .where(
            Job.status.in_(("queued", "running")),
            (Job.lease_until.is_(None)) | (Job.lease_until < now),
        )
        .order_by(Job.created_at)
        .limit(1)
    )
    if candidate is None:
        return None

    # Conditional update: two workers racing for the same row, only one wins.
    result = session.execute(
        update(Job)
        .where(
            Job.job_id == candidate.job_id,
            (Job.lease_until.is_(None)) | (Job.lease_until < now),
        )
        .values(
            status="running",
            claimed_by=worker_id,
            lease_until=now + timedelta(seconds=lease_seconds),
            deliveries=Job.deliveries + 1,
        )
    )
    session.commit()
    if result.rowcount == 0:
        return None
    session.refresh(candidate)
    return candidate


def _completed_nodes(session: Session, execution_id: str) -> dict[str, dict[str, Any]]:
    """Outputs of nodes already finished — the resume point after a crash."""
    rows = session.scalars(
        select(NodeResult).where(
            NodeResult.execution_id == execution_id, NodeResult.status == "succeeded"
        )
    )
    return {row.node_id: row.output or {} for row in rows}


def run_execution(
    session: Session,
    execution: Execution,
    compiled: CompiledWorkflow,
    *,
    client: httpx.Client | None = None,
) -> Execution:
    """Run every node in order, resuming from whatever already completed."""
    execution.status = "running"
    session.flush()
    _event(session, execution.execution_id, None, "execution_started")
    session.commit()

    outputs = _completed_nodes(session, execution.execution_id)
    if outputs:
        log.info(
            "resuming execution %s: %d node(s) already done",
            execution.execution_id,
            len(outputs),
        )

    for node_id in compiled.execution_order:
        if node_id in outputs:
            _event(session, execution.execution_id, node_id, "node_skipped_already_done")
            continue

        node = compiled.node(node_id)
        if node is None or node.type in ("transform", "condition", "approval"):
            # Engine-side node types do no outbound work in this build; they are
            # recorded so the execution history stays complete.
            outputs[node_id] = {}
            session.add(
                NodeResult(
                    execution_id=execution.execution_id,
                    node_id=node_id,
                    status="succeeded",
                    output={},
                )
            )
            _event(session, execution.execution_id, node_id, "node_noop")
            session.commit()
            continue

        _event(session, execution.execution_id, node_id, "node_started")
        session.commit()

        outcome = execute_node(
            session,
            node,
            outputs,
            execution_id=execution.execution_id,
            dry_run=execution.dry_run,
            client=client,
        )

        # Checkpoint first, mark done second. Never the other way round.
        session.add(
            NodeResult(
                execution_id=execution.execution_id,
                node_id=node_id,
                status="succeeded" if outcome.ok else "failed",
                status_code=outcome.status_code,
                output=outcome.output,
                idempotency_key=outcome.request.idempotency_key if outcome.request else None,
                attempts=outcome.attempts,
                latency_ms=outcome.latency_ms,
            )
        )
        _event(
            session,
            execution.execution_id,
            node_id,
            "node_succeeded" if outcome.ok else "node_failed",
            outcome=outcome,
        )
        session.commit()

        if not outcome.ok:
            execution.status = "failed"
            execution.error_category = outcome.error_category
            execution.error_message = (outcome.error_message or "")[:1000]
            execution.completed_at = naive_utcnow()
            _event(session, execution.execution_id, None, "execution_failed")
            session.commit()
            log.warning(
                "execution %s failed at %s (%s)",
                execution.execution_id,
                node_id,
                outcome.error_category,
            )
            return execution

        outputs[node_id] = outcome.output

    execution.status = "succeeded"
    execution.completed_at = naive_utcnow()
    _event(session, execution.execution_id, None, "execution_succeeded")
    session.commit()
    log.info("execution %s succeeded", execution.execution_id)
    return execution


def load_compiled(session: Session, workflow_id: str) -> CompiledWorkflow | None:
    row = session.scalar(
        select(CompiledIR)
        .where(CompiledIR.workflow_id == workflow_id)
        .order_by(CompiledIR.compiled_at.desc())
        .limit(1)
    )
    return CompiledWorkflow.model_validate(row.ir) if row else None


def finish_job(session: Session, job: Job, execution: Execution) -> None:
    """Acknowledge a job only once its execution reached a terminal state."""
    if execution.status == "succeeded":
        job.status = "done"
    elif job.deliveries >= settings.queue_max_deliveries:
        # Repeated failure stops here rather than looping forever; a human looks
        # at the dead-letter queue.
        job.status = "dead_letter"
        job.last_error = execution.error_message
        log.error("job %s dead-lettered after %d deliveries", job.job_id, job.deliveries)
    else:
        job.status = "queued"
        job.lease_until = None
        job.last_error = execution.error_message
    session.commit()
