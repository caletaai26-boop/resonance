"""Agent runtime: every agent is a function `run(ctx)` that does real work through tools in
resonance.audio / resonance.music / resonance.score, reports progress it actually made, stores artifacts
with provenance and returns a structured, schema-validated output."""
from __future__ import annotations

import mimetypes
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..storage import sha256_file, storage


class Blocked(Exception):
    """The agent cannot run because an engine, model or input is unavailable. Not retried.
    The message is shown to the user verbatim, so it must say what is missing and how to fix it."""


AGENTS: dict[str, dict] = {}


def agent(key: str, number: int, name: str, role: str):
    def deco(fn):
        AGENTS[key] = {"key": key, "number": number, "name": name, "role": role, "run": fn}
        return fn

    return deco


class Ctx:
    def __init__(self, db: Session, job: m.Job) -> None:
        self.db = db
        self.job = job
        self.project: m.Project = db.get(m.Project, job.project_id)
        self.params: dict = job.params or {}
        self._tmp = tempfile.TemporaryDirectory(prefix="resonance-")
        self.tmp = Path(self._tmp.name)

    # ---- progress / log (committed immediately so the UI sees real state)
    def progress(self, fraction: float, step: str) -> None:
        self.job.progress = float(max(0.0, min(1.0, fraction)))
        self.job.step = step
        self.log(step)

    def log(self, msg: str) -> None:
        self.job.log = [*(self.job.log or []), {"t": m.now().isoformat(), "msg": msg}][-200:]
        self.db.commit()

    # ---- inputs
    def latest_asset(self, kind: str, **meta_eq) -> m.Asset | None:
        rows = self.db.execute(select(m.Asset).where(m.Asset.project_id == self.project.id, m.Asset.kind == kind)
                               .order_by(m.Asset.created_at.desc())).scalars().all()
        for a in rows:
            if all((a.meta or {}).get(k) == v for k, v in meta_eq.items()):
                return a
        return None

    def source(self) -> m.Asset:
        a = self.latest_asset("source")
        if not a:
            raise Blocked("No source recording has been uploaded to this project yet.")
        return a

    def working(self) -> m.Asset:
        a = self.latest_asset("working")
        if not a:
            raise Blocked("The recording has not been analysed yet. Run Audio Forensics first.")
        return a

    def path(self, asset: m.Asset) -> Path:
        return storage.local_path(asset.storage_key)

    def head(self, model, key: str):
        hid = (self.project.heads or {}).get(key)
        return self.db.get(model, hid) if hid else None

    # ---- outputs
    def store(self, src: Path, kind: str, label: str, *, method: str, engine: str = "", engine_version: str = "",
              settings: dict | None = None, meta: dict | None = None, parent: m.Asset | None = None,
              verification: str = "unverified", filename: str | None = None) -> m.Asset:
        source = self.latest_asset("source")
        a = m.Asset(project_id=self.project.id, kind=kind, label=label, filename=filename or src.name,
                    storage_key="", mime=mimetypes.guess_type(src.name)[0] or "application/octet-stream",
                    size_bytes=src.stat().st_size, sha256=sha256_file(src), source_asset_id=source.id if source else None,
                    parent_asset_id=parent.id if parent else None, job_id=self.job.id, method=method, engine=engine,
                    engine_version=engine_version, settings=settings or {}, meta=meta or {}, verification=verification)
        self.db.add(a)
        self.db.flush()
        a.storage_key = f"{self.project.id}/{kind}/{a.id}{src.suffix.lower()}"
        storage.put_file(a.storage_key, src)
        self.db.commit()
        return a

    def output(self, summary: str, data: dict) -> m.AgentOutput:
        for old in self.db.execute(select(m.AgentOutput).where(m.AgentOutput.project_id == self.project.id,
                                                              m.AgentOutput.agent == self.job.agent,
                                                              m.AgentOutput.stale.is_(False))).scalars():
            old.stale = True
        o = m.AgentOutput(project_id=self.project.id, job_id=self.job.id, agent=self.job.agent, summary=summary, data=data)
        self.db.add(o)
        self.db.commit()
        return o

    def close(self) -> None:
        self._tmp.cleanup()


def new_version(db: Session, project: m.Project, model, head_key: str, data: dict, *, origin: str, note: str = "",
                provenance: dict | None = None, move_head: bool = True, verification: str = "unverified"):
    """Append a new immutable version. Never mutates an existing row's data."""
    from sqlalchemy import func

    n = db.execute(select(func.max(model.version)).where(model.project_id == project.id)).scalar() or 0
    heads = dict(project.heads or {})
    row = model(project_id=project.id, version=n + 1, parent_id=heads.get(head_key), origin=origin, note=note,
                data=data, provenance=provenance or {}, verification=verification)
    db.add(row)
    db.flush()
    if move_head or head_key not in heads:
        heads[head_key] = row.id
        project.heads = heads
    db.flush()
    return row
