"""The worker process.

Runs separately from the API so execution scales independently of search, and
so a slow provider never blocks a request thread.

    uv run python -m engine.execution.worker

Shutdown is graceful: the current execution finishes (or checkpoints) before
the process exits, so a redeploy mid-run does not strand work.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
from types import FrameType

from engine.db import Execution, init_db, session_scope
from engine.execution.runner import (
    claim_job,
    finish_job,
    load_compiled,
    run_execution,
)

log = logging.getLogger(__name__)

_shutdown = False


def _request_shutdown(signum: int, _frame: FrameType | None) -> None:
    global _shutdown
    _shutdown = True
    log.info("signal %s received — finishing the current job then stopping", signum)


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def run_once(*, worker: str) -> bool:
    """Claim and run one job. Returns False when the queue is empty."""
    with session_scope() as session:
        job = claim_job(session, worker_id=worker)
        if job is None:
            return False

        execution = session.get(Execution, job.execution_id)
        if execution is None:
            job.status = "dead_letter"
            job.last_error = "execution row is missing"
            session.commit()
            return True

        compiled = load_compiled(session, execution.workflow_id)
        if compiled is None:
            execution.status = "failed"
            execution.error_category = "validation"
            execution.error_message = "workflow has no compiled form — compile it first"
            session.commit()
            finish_job(session, job, execution)
            return True

        log.info("claimed %s for workflow %s", job.job_id, execution.workflow_id)
        run_execution(session, execution, compiled)
        finish_job(session, job, execution)
        return True


def serve(*, poll_seconds: float = 1.0, max_jobs: int | None = None) -> int:
    """Poll for work until interrupted. Returns the number of jobs handled."""
    init_db()
    worker = worker_id()
    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)

    log.info("worker %s started", worker)
    handled = 0
    while not _shutdown:
        if max_jobs is not None and handled >= max_jobs:
            break
        try:
            if run_once(worker=worker):
                handled += 1
                continue
        except Exception:
            # One poisoned job must not take the worker down; its lease will
            # expire and another attempt will follow.
            log.exception("worker loop error")
        time.sleep(poll_seconds)

    log.info("worker %s stopped after %d job(s)", worker, handled)
    return handled


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    serve()


if __name__ == "__main__":
    main()
