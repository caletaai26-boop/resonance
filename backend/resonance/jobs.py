"""Durable, database-backed job queue with one in-process worker thread.

Why not Redis/Celery: audio jobs here are CPU-bound, minutes long and run one at a time per machine. A DB
table gives durability (jobs survive restarts), retries and history without extra infrastructure. The
queue interface (`enqueue`, `Worker.run_one`) is small enough to swap for Celery/RQ when scaling out.
"""
from __future__ import annotations

import threading
import time
import traceback

from sqlalchemy import select

from . import models as m
from .config import settings
from .db import SessionLocal

TERMINAL = ("succeeded", "failed", "blocked", "cancelled")


def enqueue(db, project_id: str, agent: str, params: dict | None = None, after: str | None = None) -> m.Job:
    from .agents import AGENTS

    if agent not in AGENTS:
        raise ValueError(f"unknown agent '{agent}'")
    p = dict(params or {})
    if after:
        p["after"] = after
    job = m.Job(project_id=project_id, agent=agent, params=p, max_attempts=settings.job_max_attempts)
    db.add(job)
    db.flush()
    return job


def recover_interrupted() -> int:
    """On startup: jobs left 'running' by a crash go back to the queue (or fail if out of attempts)."""
    n = 0
    with SessionLocal() as db:
        for j in db.execute(select(m.Job).where(m.Job.status == "running")).scalars():
            if j.attempts < j.max_attempts:
                j.status, j.step = "queued", "Recovered after an interrupted run; waiting to retry"
            else:
                j.status, j.error, j.finished_at = "failed", "Interrupted by a server restart and out of retry attempts.", m.now()
            n += 1
        db.commit()
    return n


class Worker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="resonance-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.run_one():
                    time.sleep(0.5)
            except Exception:  # pragma: no cover - keep the worker alive
                traceback.print_exc()
                time.sleep(1.0)

    def _claim(self, db) -> m.Job | None:
        for j in db.execute(select(m.Job).where(m.Job.status == "queued").order_by(m.Job.created_at, m.Job.id)).scalars():
            dep_id = (j.params or {}).get("after")
            if dep_id:
                dep = db.get(m.Job, dep_id)
                if dep and dep.status in ("failed", "cancelled"):
                    j.status, j.finished_at = "cancelled", m.now()
                    j.error = f"Not run because the step it depends on ({dep.agent}) did not complete."
                    db.commit()
                    continue
                if dep and dep.status not in ("succeeded", "blocked"):
                    continue
            j.status, j.started_at, j.attempts = "running", m.now(), j.attempts + 1
            j.progress, j.step, j.error = 0.0, "Starting", ""
            db.commit()
            return j
        return None

    def run_one(self) -> bool:
        """Claims and executes one job. Returns False when the queue has nothing runnable."""
        from .agents import AGENTS
        from .agents.base import Blocked, Ctx

        with SessionLocal() as db:
            job = self._claim(db)
            if not job:
                return False
            ctx = Ctx(db, job)
            try:
                if ctx.project is None:
                    raise Blocked("The project no longer exists.")
                AGENTS[job.agent]["run"](ctx)
                job.status, job.progress, job.step = "succeeded", 1.0, "Finished"
            except Blocked as e:
                db.rollback()
                job = db.get(m.Job, job.id)
                job.status, job.error, job.step = "blocked", str(e), "Blocked"
            except Exception as e:
                db.rollback()
                job = db.get(m.Job, job.id)
                tb = traceback.format_exc(limit=6)
                job.log = [*(job.log or []), {"t": m.now().isoformat(), "msg": f"Attempt {job.attempts} failed: {e}"}]
                if job.attempts < job.max_attempts:
                    job.status, job.step, job.error = "queued", f"Retrying after error (attempt {job.attempts} of {job.max_attempts})", f"{e}"
                else:
                    job.status, job.error, job.step = "failed", f"{e.__class__.__name__}: {e}\n{tb}", "Failed"
            finally:
                ctx.close()
            if job.status in TERMINAL:
                job.finished_at = m.now()
            db.commit()
            return True


worker = Worker()
