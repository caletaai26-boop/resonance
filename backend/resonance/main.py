from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import io
import json
import mimetypes
import re
import shutil
import tempfile
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import auth, director, gates, jobs, llm
from . import models as m
from .agents import AGENTS
from .agents.base import new_version
from .agents.production import MASTER_TARGETS, default_mix, faithful_violations
from .agents.separation import engine_status as demucs_status
from .agents.structure_lyrics import _asr_status, align_syllables_to_notes
from .audio import io as aio
from .audio import synth
from .audio.dsp import REVERB_PRESETS
from .config import settings
from .db import SessionLocal, get_db, init_db
from .music import analysis as mu
from .music import language as lang
from .score import io as sio
from .score.model import GM_PROGRAMS, validate_score
from .storage import UploadTooLarge, storage

AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a"}
SCORE_EXT = {".mid", ".midi", ".musicxml", ".xml", ".mxl", ".pdf"}


def purge_expired() -> int:
    """Data retention: projects with retention_days set are deleted that many days after their last change."""
    n = 0
    with SessionLocal() as db:
        for p in db.execute(select(m.Project).where(m.Project.retention_days.is_not(None))).scalars():
            upd = p.updated_at if p.updated_at.tzinfo else p.updated_at.replace(tzinfo=dt.timezone.utc)
            if upd + dt.timedelta(days=p.retention_days) < m.now():
                storage.delete_prefix(p.id)
                db.delete(p)
                n += 1
        db.commit()
    return n


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    jobs.recover_interrupted()
    purge_expired()
    if settings.run_worker:
        jobs.worker.start()
    yield
    jobs.worker.stop()


app = FastAPI(title="RESONANCE", version="0.1.0", lifespan=lifespan)


@app.exception_handler(gates.GateError)
async def _gate_error(_: Request, exc: gates.GateError):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


def audit(db: Session, project_id: str | None, user: m.User | None, kind: str, message: str, data: dict | None = None) -> None:
    db.add(m.AuditEvent(project_id=project_id, user_id=user.id if user else None, kind=kind, message=message, data=data or {}))


# =============================================================================================== auth
class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=200)
    name: str = Field(default="", max_length=255)


def _set_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax", secure=settings.cookie_secure, max_age=auth.SESSION_DAYS * 86400, path="/")


@app.post("/api/auth/register")
def register(body: Credentials, response: Response, db: Session = Depends(get_db)):
    if not settings.allow_registration and db.execute(select(func.count(m.User.id))).scalar():
        raise HTTPException(403, "Registration is closed on this server.")
    email = str(body.email).strip().lower()
    if "@" not in email:
        raise HTTPException(422, "Enter a valid email address.")
    if db.execute(select(m.User).where(m.User.email == email)).scalars().first():
        raise HTTPException(409, "An account with this email already exists.")
    u = m.User(email=email, name=body.name.strip() or email.split("@")[0], password_hash=auth.hash_password(body.password))
    db.add(u)
    db.flush()
    _set_cookie(response, auth.create_session(db, u))
    db.commit()
    return {"id": u.id, "email": u.email, "name": u.name}


@app.post("/api/auth/login")
def login(body: Credentials, response: Response, db: Session = Depends(get_db)):
    u = db.execute(select(m.User).where(m.User.email == str(body.email).strip().lower())).scalars().first()
    if not u or not auth.verify_password(body.password, u.password_hash):
        raise HTTPException(401, "Email or password is incorrect.")
    _set_cookie(response, auth.create_session(db, u))
    db.commit()
    return {"id": u.id, "email": u.email, "name": u.name}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    tok = request.cookies.get(auth.COOKIE)
    if tok:
        s = db.get(m.AuthSession, hashlib.sha256(tok.encode()).hexdigest())
        if s:
            db.delete(s)
            db.commit()
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: m.User = Depends(auth.current_user)):
    return {"id": user.id, "email": user.email, "name": user.name}


# =============================================================================================== projects
class ProjectIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=5000)
    mode: str = Field(default="faithful", pattern="^(restore|faithful|creative)$")
    language: str = Field(default="la", pattern="^(la|es|en)$")
    rights: dict = {}
    rights_acknowledged: bool = False


class ProjectPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=5000)
    mode: str | None = Field(default=None, pattern="^(restore|faithful|creative)$")
    language: str | None = Field(default=None, pattern="^(la|es|en)$")
    rights: dict | None = None
    retention_days: int | None = Field(default=None, ge=0, le=3650)
    allow_external_ai: bool | None = None


def asset_dict(a: m.Asset) -> dict:
    return {"id": a.id, "kind": a.kind, "label": a.label, "filename": a.filename, "mime": a.mime, "size_bytes": a.size_bytes,
            "sha256": a.sha256, "immutable": a.immutable, "source_asset_id": a.source_asset_id, "parent_asset_id": a.parent_asset_id,
            "job_id": a.job_id, "method": a.method, "engine": a.engine, "engine_version": a.engine_version, "settings": a.settings,
            "meta": a.meta, "verification": a.verification, "created_at": a.created_at.isoformat(), "url": f"/api/assets/{a.id}/file"}


def project_dict(db: Session, p: m.Project, full: bool = False) -> dict:
    src = db.execute(select(m.Asset).where(m.Asset.project_id == p.id, m.Asset.kind == "source").order_by(m.Asset.created_at.desc())).scalars().first()
    d = {"id": p.id, "title": p.title, "description": p.description, "mode": p.mode, "language": p.language, "rights": p.rights,
         "retention_days": p.retention_days, "allow_external_ai": p.allow_external_ai, "created_at": p.created_at.isoformat(),
         "updated_at": p.updated_at.isoformat(), "source": asset_dict(src) if src else None}
    st = director.status(db, p)
    d["stage"], d["next_action"] = st["stage"], st["next_action"]
    d["pending_gate"] = next((g["gate"] for g in st["gates"] if g["state"] != "approved"), None)
    d["active_jobs"] = len(st["active_jobs"])
    d["blockers"] = len(st["blockers"])
    if full:
        d["status"] = st
    return d


@app.get("/api/projects")
def list_projects(user: m.User = Depends(auth.current_user), db: Session = Depends(get_db)):
    rows = db.execute(select(m.Project).where(m.Project.owner_id == user.id).order_by(m.Project.updated_at.desc())).scalars().all()
    return [project_dict(db, p) for p in rows]


@app.post("/api/projects", status_code=201)
def create_project(body: ProjectIn, user: m.User = Depends(auth.current_user), db: Session = Depends(get_db)):
    if not body.rights_acknowledged:
        raise HTTPException(422, "Please confirm you have the right to upload and process this recording.")
    rights = {"melody": "", "arrangement": "", "recording": "", "notes": "", **body.rights,
              "acknowledged_at": m.now().isoformat(), "acknowledged_by": user.email, "used_for_model_training": False}
    p = m.Project(owner_id=user.id, title=body.title.strip(), description=body.description, mode=body.mode, language=body.language, rights=rights, heads={})
    db.add(p)
    db.flush()
    audit(db, p.id, user, "project.created", f"Project '{p.title}' created in {p.mode} mode")
    db.commit()
    return project_dict(db, p, full=True)


@app.get("/api/projects/{project_id}")
def get_project(p: m.Project = Depends(auth.owned_project), db: Session = Depends(get_db)):
    return project_dict(db, p, full=True)


@app.patch("/api/projects/{project_id}")
def patch_project(body: ProjectPatch, p: m.Project = Depends(auth.owned_project), user: m.User = Depends(auth.current_user), db: Session = Depends(get_db)):
    data = body.model_dump(exclude_unset=True)
    if "mode" in data and data["mode"] != p.mode:
        audit(db, p.id, user, "project.mode", f"Mode changed from {p.mode} to {data['mode']}")
        p.mode = data.pop("mode")
        gates.invalidate(db, p, "arrangement", "The reconstruction mode was changed.")
    if "rights" in data and data["rights"] is not None:
        p.rights = {**(p.rights or {}), **data.pop("rights")}
    for k, v in data.items():
        if k == "retention_days" and v == 0:
            v = None
        setattr(p, k, v)
    p.updated_at = m.now()
    db.commit()
    return project_dict(db, p, full=True)


@app.delete("/api/projects/{project_id}")
def delete_project(p: m.Project = Depends(auth.owned_project), user: m.User = Depends(auth.current_user), db: Session = Depends(get_db)):
    active = db.execute(select(m.Job).where(m.Job.project_id == p.id, m.Job.status == "running")).scalars().first()
    if active:
        raise HTTPException(409, "An agent is working on this project right now. Wait for it to finish, then delete.")
    pid, title = p.id, p.title
    for model in (m.Job, m.AgentOutput, m.ScoreVersion, m.SectionSet, m.LyricsVersion, m.Arrangement, m.MixSettings, m.MasterSettings,
                  m.Reconstruction, m.FidelityReport, m.ReviewDecision, m.AuditEvent):
        for row in db.execute(select(model).where(model.project_id == pid)).scalars():
            db.delete(row)
    db.delete(p)
    audit(db, None, user, "project.deleted", f"Project '{title}' and all of its files were permanently deleted", {"project_id": pid})
    db.commit()
    storage.delete_prefix(pid)
    return {"deleted": True}


# =============================================================================================== uploads / assets
def _safe_name(name: str) -> str:
    base = Path(name or "upload").name
    return re.sub(r"[^A-Za-z0-9._ ()\-]", "_", base)[:180] or "upload"


def _receive(db: Session, p: m.Project, file: UploadFile, kind: str, allowed: set[str], label: str, immutable: bool = False, meta: dict | None = None) -> m.Asset:
    name = _safe_name(file.filename or "")
    ext = Path(name).suffix.lower()
    if ext not in allowed:
        raise HTTPException(415, f"'{ext or 'this file type'}' is not accepted here. Allowed: {', '.join(sorted(allowed))}")
    a = m.Asset(project_id=p.id, kind=kind, label=label, filename=name, storage_key="", immutable=immutable,
                mime=mimetypes.guess_type(name)[0] or "application/octet-stream", method="upload", engine="user", meta=meta or {})
    db.add(a)
    db.flush()
    a.storage_key = f"{p.id}/{kind}/{a.id}{ext}"
    try:
        a.size_bytes, a.sha256 = storage.put_stream(a.storage_key, file.file, settings.max_upload_mb * 1024 * 1024)
    except UploadTooLarge:
        db.rollback()
        raise HTTPException(413, f"File is larger than the {settings.max_upload_mb} MB limit.")
    if a.size_bytes == 0:
        storage.delete(a.storage_key)
        db.rollback()
        raise HTTPException(422, "The uploaded file is empty.")
    return a


@app.post("/api/projects/{project_id}/source", status_code=201)
def upload_source(file: UploadFile = File(...), p: m.Project = Depends(auth.owned_project), user: m.User = Depends(auth.current_user), db: Session = Depends(get_db)):
    if db.execute(select(m.Asset).where(m.Asset.project_id == p.id, m.Asset.kind == "source")).scalars().first():
        raise HTTPException(409, "This project already has its source recording, and originals are immutable. Create a new project for a different recording.")
    a = _receive(db, p, file, "source", AUDIO_EXT, "Original recording (untouched)", immutable=True)
    try:
        probe = aio.ffprobe(storage.local_path(a.storage_key))
    except aio.AudioDecodeError as e:
        storage.delete(a.storage_key)
        db.rollback()
        raise HTTPException(422, f"This file is not readable audio: {e}")
    a.meta = {"probe": probe}
    a.verification = "approved"  # it IS the reference of truth
    p.updated_at = m.now()
    audit(db, p.id, user, "source.uploaded", f"Source recording '{a.filename}' uploaded ({a.size_bytes} bytes, sha256 {a.sha256[:12]}…)")
    db.commit()
    return asset_dict(a)


@app.post("/api/projects/{project_id}/score/import", status_code=201)
def import_score(file: UploadFile = File(...), p: m.Project = Depends(auth.owned_project), user: m.User = Depends(auth.current_user), db: Session = Depends(get_db)):
    ext = Path(_safe_name(file.filename or "")).suffix.lower()
    if ext == ".pdf":
        a = _receive(db, p, file, "score_reference_pdf", SCORE_EXT, "Reference score (PDF)")
        audit(db, p.id, user, "score.pdf", f"Reference PDF '{a.filename}' stored")
        db.commit()
        return {"asset": asset_dict(a), "score_version": None,
                "message": "The PDF is stored as a visual reference. Reading notes out of a PDF (optical music recognition) is not available in this build, "
                           "so it was NOT converted to an editable score. Export MusicXML or MIDI from your notation software for a score the system can use."}
    a = _receive(db, p, file, "score_import", SCORE_EXT, "Imported score")
    path = storage.local_path(a.storage_key)
    try:
        data = sio.midi_to_score(path) if ext in (".mid", ".midi") else sio.musicxml_to_score(path)
    except Exception as e:
        storage.delete(a.storage_key)
        db.rollback()
        raise HTTPException(422, f"The score could not be read: {str(e)[:200]}")
    if not any(pt["notes"] for pt in data["parts"]):
        storage.delete(a.storage_key)
        db.rollback()
        raise HTTPException(422, "The score file contains no notes.")
    origin = "import:midi" if ext in (".mid", ".midi") else "import:musicxml"
    work = db.execute(select(m.Asset).where(m.Asset.project_id == p.id, m.Asset.kind == "working")).scalars().first()
    warn = list(data.get("warnings", []))
    if work and abs((work.meta or {}).get("duration", 0) - data["duration"]) > 5:
        warn.append(f"The imported score lasts {data['duration']:.0f}s but the recording lasts {(work.meta or {}).get('duration', 0):.0f}s. "
                    "An imported score follows its own tempo, not the performers' timing. Until it is aligned to the recording, A/B comparison uses an alignment map rather than a shared clock.")
    data["warnings"] = warn
    had = (p.heads or {}).get("score")
    sv = new_version(db, p, m.ScoreVersion, "score", data, origin=origin, note=f"Imported from {a.filename}", provenance={"asset_id": a.id, "sha256": a.sha256})
    inv = gates.invalidate(db, p, "score", "A score was imported and became the current score.") if had else None
    audit(db, p.id, user, "score.imported", f"Imported '{a.filename}' as score v{sv.version}")
    p.updated_at = m.now()
    db.commit()
    return {"asset": asset_dict(a), "score_version": version_dict(sv), "invalidation": inv, "warnings": warn,
            "message": "The imported score is now the current score and takes priority over automatic transcription."}


@app.post("/api/projects/{project_id}/human-stem", status_code=201)
def upload_human_stem(track_id: str = Form(...), file: UploadFile = File(...), p: m.Project = Depends(auth.owned_project),
                      user: m.User = Depends(auth.current_user), db: Session = Depends(get_db)):
    arr = db.get(m.Arrangement, (p.heads or {}).get("arrangement") or "")
    if not arr or not any(t["id"] == track_id for t in arr.data.get("tracks", [])):
        raise HTTPException(404, "That track does not exist in the current arrangement.")
    a = _receive(db, p, file, "human_stem", AUDIO_EXT, "Human-recorded stem", meta={"track_id": track_id})
    try:
        a.meta = {**a.meta, "probe": aio.ffprobe(storage.local_path(a.storage_key))}
    except aio.AudioDecodeError as e:
        storage.delete(a.storage_key)
        db.rollback()
        raise HTTPException(422, f"This file is not readable audio: {e}")
    data = json.loads(json.dumps(arr.data))
    for t in data["tracks"]:
        if t["id"] == track_id:
            t.update(rendering="human-stem", human_stem_asset_id=a.id, instrument="Human recording", instrument_confirmed=True)
    new = new_version(db, p, m.Arrangement, "arrangement", data, origin="user-edit", note=f"Human stem attached to '{track_id}'")
    inv = gates.invalidate(db, p, "arrangement", "A human-recorded stem was attached to a track.")
    audit(db, p.id, user, "stem.imported", f"Human stem '{a.filename}' attached")
    db.commit()
    return {"asset": asset_dict(a), "arrangement": version_dict(new), "invalidation": inv}


@app.get("/api/projects/{project_id}/assets")
def list_assets(kind: str | None = None, p: m.Project = Depends(auth.owned_project), db: Session = Depends(get_db)):
    q = select(m.Asset).where(m.Asset.project_id == p.id)
    if kind:
        q = q.where(m.Asset.kind.in_(kind.split(",")))
    return [asset_dict(a) for a in db.execute(q.order_by(m.Asset.created_at.desc())).scalars()]


@app.get("/api/assets/{asset_id}/file")
def asset_file(asset_id: str, download: int = 0, user: m.User = Depends(auth.current_user), db: Session = Depends(get_db)):
    a = db.get(m.Asset, asset_id)
    p = db.get(m.Project, a.project_id) if a else None
    if not a or not p or p.owner_id != user.id:
        raise HTTPException(404, "File not found")
    path = storage.local_path(a.storage_key)
    if not path.exists():
        raise HTTPException(410, "The file is no longer in storage.")
    return FileResponse(path, media_type=a.mime, filename=a.filename if download else None,
                        headers={"Cache-Control": "private, max-age=3600"})


# =============================================================================================== jobs / agents / director
class RunBody(BaseModel):
    params: dict = {}


@app.get("/api/projects/{project_id}/agents")
def agents_view(p: m.Project = Depends(auth.owned_project), db: Session = Depends(get_db)):
    return director.agents_view(db, p)


@app.get("/api/projects/{project_id}/status")
def project_status(narrate: int = 0, p: m.Project = Depends(auth.owned_project), db: Session = Depends(get_db)):
    st = director.status(db, p)
    if narrate:
        st["narration"] = llm.narrate(db, p, {k: st[k] for k in ("stage", "next_action", "gates", "blockers", "mode")}
                                      | {"agent_summaries": {a["key"]: a["output"]["summary"] for a in director.agents_view(db, p) if a["output"]}})
    return st


def _busy(db: Session, p: m.Project, agent: str | None = None) -> None:
    q = select(m.Job).where(m.Job.project_id == p.id, m.Job.status.in_(["queued", "running"]))
    if agent:
        q = q.where(m.Job.agent == agent)
    if db.execute(q).scalars().first():
        raise HTTPException(409, "That step is already queued or running for this project.")


@app.post("/api/projects/{project_id}/pipeline/{name}", status_code=202)
def run_pipeline(name: str, body: RunBody = RunBody(), p: m.Project = Depends(auth.owned_project), db: Session = Depends(get_db)):
    if name not in director.PIPELINES:
        raise HTTPException(404, "Unknown pipeline")
    _busy(db, p)
    if not db.execute(select(m.Asset).where(m.Asset.project_id == p.id, m.Asset.kind == "source")).scalars().first():
        raise HTTPException(409, "Upload the source recording first.")
    out = director.start_pipeline(db, p, name, body.params)
    db.commit()
    return [director.job_dict(j) for j in out]


@app.post("/api/projects/{project_id}/agents/{agent}/run", status_code=202)
def run_agent(agent: str, body: RunBody = RunBody(), p: m.Project = Depends(auth.owned_project), db: Session = Depends(get_db)):
    if agent not in AGENTS:
        raise HTTPException(404, "Unknown agent")
    _busy(db, p, agent)
    params = dict(body.params)
    if agent == "forensics" and director.latest_output(db, p.id, "forensics"):
        params["reanalysis"] = True
    if agent == "mixing" and db.execute(select(m.Asset).where(m.Asset.project_id == p.id, m.Asset.kind == "mix")).scalars().first():
        params["remix"] = True
    j = jobs.enqueue(db, p.id, agent, params)
    db.commit()
    return director.job_dict(j)


@app.get("/api/projects/{project_id}/jobs")
def list_jobs(limit: int = 50, p: m.Project = Depends(auth.owned_project), db: Session = Depends(get_db)):
    rows = db.execute(select(m.Job).where(m.Job.project_id == p.id).order_by(m.Job.created_at.desc()).limit(min(limit, 200))).scalars()
    return [director.job_dict(j) for j in rows]


def _owned_job(job_id: str, user: m.User, db: Session) -> m.Job:
    j = db.get(m.Job, job_id)
    p = db.get(m.Project, j.project_id) if j else None
    if not j or not p or p.owner_id != user.id:
        raise HTTPException(404, "Job not found")
    return j


@app.post("/api/jobs/{job_id}/retry", status_code=202)
def retry_job(job_id: str, user: m.User = Depends(auth.current_user), db: Session = Depends(get_db)):
    j = _owned_job(job_id, user, db)
    if j.status not in ("failed", "blocked", "cancelled"):
        raise HTTPException(409, "Only failed, blocked or cancelled jobs can be retried.")
    params = {k: v for k, v in (j.params or {}).items() if k != "after"}
    n = jobs.enqueue(db, j.project_id, j.agent, params)
    db.commit()
    return director.job_dict(n)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, user: m.User = Depends(auth.current_user), db: Session = Depends(get_db)):
    j = _owned_job(job_id, user, db)
    if j.status != "queued":
        raise HTTPException(409, "Only queued jobs can be cancelled; a running step finishes on its own.")
    j.status, j.finished_at, j.error = "cancelled", m.now(), "Cancelled by user."
    db.commit()
    return director.job_dict(j)


@app.get("/api/projects/{project_id}/events")
async def events(request: Request, p: m.Project = Depends(auth.owned_project)):
    """Server-sent events: pushes the real job table whenever it changes."""
    pid = p.id

    async def gen():
        last = None
        while not await request.is_disconnected():
            with SessionLocal() as db:
                rows = db.execute(select(m.Job).where(m.Job.project_id == pid).order_by(m.Job.created_at.desc()).limit(40)).scalars().all()
                payload = json.dumps([director.job_dict(j) | {"log": (j.log or [])[-5:]} for j in rows])
            if payload != last:
                last = payload
                yield f"event: jobs\ndata: {payload}\n\n"
            else:
                yield ": keep-alive\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# =============================================================================================== gates
class GateBody(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    comment: str = Field(default="", max_length=2000)


GATE_REQUIREMENTS = {
    1: lambda db, p, st: (bool(director.latest_output(db, p.id, "forensics")), "Run the analysis first: there is nothing to approve yet."),
    2: lambda db, p, st: (bool(st["current"]["sections_id"]), "There is no section timeline yet."),
    3: lambda db, p, st: (bool(st["current"]["score_version_id"]), "There is no score yet."),
    4: lambda db, p, st: (bool(st["current"]["arrangement_id"]), "There is no arrangement yet."),
    5: lambda db, p, st: (bool(st["current"]["reconstruction"] and st["current"]["reconstruction"]["status"] in ("mixed", "mastered", "final")), "There is no up-to-date mixed render to review."),
    6: lambda db, p, st: (bool(st["current"]["fidelity_report_id"]), "Run the Fidelity Inspector on the current render first."),
    7: lambda db, p, st: (bool(st["current"]["reconstruction"] and st["current"]["reconstruction"]["status"] in ("mastered", "final")), "There is no up-to-date master to authorise."),
}


@app.post("/api/projects/{project_id}/gates/{gate}")
def decide_gate(gate: int, body: GateBody, p: m.Project = Depends(auth.owned_project), user: m.User = Depends(auth.current_user), db: Session = Depends(get_db)):
    if gate not in gates.GATES:
        raise HTTPException(404, "Unknown gate")
    st = director.status(db, p)
    if body.decision == "approved":
        ok, why = GATE_REQUIREMENTS[gate](db, p, st)
        if not ok:
            raise HTTPException(409, why)
        if gate == 4 and p.mode == "faithful":
            arr, sv = db.get(m.Arrangement, st["current"]["arrangement_id"]), db.get(m.ScoreVersion, st["current"]["score_version_id"] or "")
            if arr.verification == "stale" or (sv and arr.data.get("score_version_id") != sv.id):
                raise HTTPException(409, "The arrangement was built from an older score. Rebuild it before approving.")
            v = faithful_violations(arr.data, sv.data if sv else {})
            if v:
                raise HTTPException(409, " ".join(v))
    refs = {**st["current"], "reconstruction": (st["current"]["reconstruction"] or {}).get("id")}
    d = gates.decide(db, p, gate, body.decision, user.id, body.comment, refs)
    if body.decision == "approved":
        mark = {2: (m.SectionSet, "sections_id"), 3: (m.ScoreVersion, "score_version_id"), 4: (m.Arrangement, "arrangement_id")}.get(gate)
        if mark and st["current"][mark[1]]:
            db.get(mark[0], st["current"][mark[1]]).verification = "approved"
        if gate == 7:
            rec = db.get(m.Reconstruction, st["current"]["reconstruction"]["id"])
            rec.status = "final"
            for a in db.execute(select(m.Asset).where(m.Asset.project_id == p.id, m.Asset.kind == "master", m.Asset.verification == "draft")).scalars():
                if (a.meta or {}).get("reconstruction_id") == rec.id:
                    a.verification = "final"
    audit(db, p.id, user, f"gate.{body.decision}", f"Gate {gate} ({gates.GATES[gate]['title']}) {body.decision}", {"comment": body.comment, "refs": refs})
    p.updated_at = m.now()
    db.commit()
    return {"decision": d.decision, "gates": gates.gate_states(db, p)}


# =============================================================================================== versioned documents
def version_dict(v, with_data: bool = True) -> dict:
    d = {"id": v.id, "version": v.version, "parent_id": v.parent_id, "origin": v.origin, "note": v.note, "provenance": v.provenance,
         "verification": v.verification, "created_at": v.created_at.isoformat()}
    if with_data:
        d["data"] = v.data
    return d


DOCS = {
    "score": (m.ScoreVersion, "score"), "sections": (m.SectionSet, "structure"), "lyrics": (m.LyricsVersion, "lyrics"),
    "arrangement": (m.Arrangement, "arrangement"), "mix": (m.MixSettings, None), "master": (m.MasterSettings, None),
}


class DocBody(BaseModel):
    data: dict
    note: str = Field(default="", max_length=500)


class Section(BaseModel):
    start: float = Field(ge=0)
    end: float
    label: str = Field(min_length=1, max_length=120)
    kind: str = Field(default="unknown", pattern="^(vocal|instrumental|unknown)$")
    group: str = ""
    status: str = Field(default="verified", pattern="^(detected|verified)$")
    model_config = {"extra": "allow"}


def _validate_doc(kind: str, data: dict, p: m.Project, db: Session) -> dict:
    try:
        if kind == "score":
            return validate_score(data)
        if kind == "sections":
            secs = sorted((Section.model_validate(s).model_dump() for s in data.get("sections", [])), key=lambda s: s["start"])
            for a, b in zip(secs, secs[1:]):
                if a["end"] > b["start"] + 0.01:
                    raise HTTPException(422, f"Sections '{a['label']}' and '{b['label']}' overlap.")
            if any(s["end"] <= s["start"] for s in secs):
                raise HTTPException(422, "Every section must end after it starts.")
            return {**data, "sections": secs}
        if kind == "lyrics":
            lines = data.get("lines", [])
            if not isinstance(lines, list) or any(not isinstance(l.get("text", None), str) for l in lines):
                raise HTTPException(422, "Each lyric line needs text.")
            language = data.get("language") or p.language
            return {**data, "language": language, "pronunciation": lang.pronunciation_guide([l["text"] for l in lines], language)}
        if kind == "arrangement":
            sv = db.get(m.ScoreVersion, (p.heads or {}).get("score") or "")
            if p.mode == "faithful" and sv:
                v = faithful_violations(data, sv.data)
                if v:
                    raise HTTPException(409, "Not allowed in Faithful Reconstruction mode: " + " ".join(v) + " Switch the project to Creative Reimagining to add material.")
            for t in data.get("tracks", []):
                if not 0 <= int(t.get("program", 0)) <= 127:
                    raise HTTPException(422, "Instrument program must be 0–127.")
                t["pan"] = max(-1.0, min(1.0, float(t.get("pan", 0))))
                t["gain_db"] = max(-40.0, min(12.0, float(t.get("gain_db", 0))))
            return data
        if kind == "mix":
            if data.get("reverb_preset") not in REVERB_PRESETS:
                raise HTTPException(422, "Unknown acoustic preset.")
            return data
        if kind == "master":
            if data.get("target") not in MASTER_TARGETS:
                raise HTTPException(422, "Unknown mastering target.")
            return data
    except ValidationError as e:
        raise HTTPException(422, "; ".join(f"{'.'.join(str(x) for x in er['loc'])}: {er['msg']}" for er in e.errors()[:5]))
    return data


@app.get("/api/projects/{project_id}/doc/{kind}")
def get_doc(kind: str, version_id: str | None = None, p: m.Project = Depends(auth.owned_project), db: Session = Depends(get_db)):
    if kind not in DOCS:
        raise HTTPException(404, "Unknown document")
    model, _ = DOCS[kind]
    head_id = (p.heads or {}).get(kind)
    row = db.get(model, version_id or head_id or "")
    if row and row.project_id != p.id:
        row = None
    versions = db.execute(select(model).where(model.project_id == p.id).order_by(model.version.desc())).scalars().all()
    if kind == "mix" and not row:
        arr = db.get(m.Arrangement, (p.heads or {}).get("arrangement") or "")
        return {"current": None, "default": default_mix(arr.data if arr else None), "head_id": None, "versions": []}
    if kind == "master" and not row:
        return {"current": None, "default": {"target": "sacred_choral", "tilt_db": 0.0}, "head_id": None, "versions": []}
    return {"current": version_dict(row) if row else None, "head_id": head_id, "versions": [version_dict(v, False) for v in versions]}


@app.put("/api/projects/{project_id}/doc/{kind}")
def put_doc(kind: str, body: DocBody, p: m.Project = Depends(auth.owned_project), user: m.User = Depends(auth.current_user), db: Session = Depends(get_db)):
    if kind not in DOCS:
        raise HTTPException(404, "Unknown document")
    model, changed = DOCS[kind]
    data = _validate_doc(kind, body.data, p, db)
    if kind == "arrangement":
        data["score_version_id"] = (p.heads or {}).get("score")
    row = new_version(db, p, model, kind, data, origin="user-edit", note=body.note or "Edited by user", provenance={"user": user.email})
    inv = gates.invalidate(db, p, changed, f"The {kind} was edited (now v{row.version}).") if changed else None
    audit(db, p.id, user, f"{kind}.edited", f"{kind.capitalize()} saved as v{row.version}: {row.note}")
    p.updated_at = m.now()
    db.commit()
    return {"current": version_dict(row), "invalidation": inv}


@app.post("/api/projects/{project_id}/doc/{kind}/{version_id}/make-current")
def make_current(kind: str, version_id: str, p: m.Project = Depends(auth.owned_project), user: m.User = Depends(auth.current_user), db: Session = Depends(get_db)):
    if kind not in DOCS:
        raise HTTPException(404, "Unknown document")
    model, changed = DOCS[kind]
    row = db.get(model, version_id)
    if not row or row.project_id != p.id:
        raise HTTPException(404, "Version not found")
    if (p.heads or {}).get(kind) == row.id:
        return {"current": version_dict(row), "invalidation": None}
    # restoring an old version is itself a new version, so history stays linear and nothing is overwritten
    new = new_version(db, p, model, kind, row.data, origin=row.origin if row.origin.startswith("import") else "user-edit",
                      note=f"Restored from v{row.version}", provenance={"restored_from": row.id, "user": user.email})
    inv = gates.invalidate(db, p, changed, f"{kind.capitalize()} v{row.version} was restored as the current version.") if changed else None
    audit(db, p.id, user, f"{kind}.restored", f"{kind.capitalize()} v{row.version} restored as v{new.version}")
    db.commit()
    return {"current": version_dict(new), "invalidation": inv}


@app.post("/api/projects/{project_id}/lyrics/apply-to-score")
def lyrics_to_score(p: m.Project = Depends(auth.owned_project), user: m.User = Depends(auth.current_user), db: Session = Depends(get_db)):
    lv, sv = db.get(m.LyricsVersion, (p.heads or {}).get("lyrics") or ""), db.get(m.ScoreVersion, (p.heads or {}).get("score") or "")
    if not lv or not sv:
        raise HTTPException(409, "Both lyrics and a score are needed.")
    data = json.loads(json.dumps(sv.data))
    part = next((x for x in data["parts"] if x.get("role") == "vocal"), None)
    if not part:
        raise HTTPException(409, "The score has no vocal part to attach words to.")
    part["notes"], stats = align_syllables_to_notes(lv.data.get("lines", []), part["notes"], lv.data.get("language") or p.language)
    row = new_version(db, p, m.ScoreVersion, "score", validate_score(data), origin="user-edit", note="Lyric syllables placed under the vocal line (draft underlay)",
                      provenance={"lyrics_version_id": lv.id, **stats})
    inv = gates.invalidate(db, p, "score", "Lyric underlay was added to the score.")
    audit(db, p.id, user, "score.underlay", f"Underlay applied: {stats}")
    db.commit()
    return {"current": version_dict(row), "stats": stats, "invalidation": inv,
            "message": "Syllables were placed one per note in order within each line's time window. This is a draft underlay: every placed syllable is flagged uncertain."}


@app.get("/api/reference-texts")
def reference_texts(user: m.User = Depends(auth.current_user)):
    return [{"key": k, **{x: v[x] for x in ("title", "language", "rights", "note", "blocks")}} for k, v in lang.REFERENCE_TEXTS.items()]


# =============================================================================================== reconstructions / fidelity
@app.get("/api/projects/{project_id}/reconstructions")
def reconstructions(p: m.Project = Depends(auth.owned_project), db: Session = Depends(get_db)):
    out = []
    for r in db.execute(select(m.Reconstruction).where(m.Reconstruction.project_id == p.id).order_by(m.Reconstruction.version.desc())).scalars():
        mix, mas = db.get(m.Asset, r.mix_asset_id or ""), db.get(m.Asset, r.master_asset_id or "")
        out.append({"id": r.id, "version": r.version, "mode": r.mode, "status": r.status, "score_version_id": r.score_version_id,
                    "arrangement_id": r.arrangement_id, "notes": r.notes, "created_at": r.created_at.isoformat(),
                    "stems": [asset_dict(a) for a in (db.get(m.Asset, i) for i in r.stem_asset_ids) if a],
                    "mix": asset_dict(mix) if mix else None, "master": asset_dict(mas) if mas else None})
    return out


@app.get("/api/projects/{project_id}/fidelity")
def fidelity(p: m.Project = Depends(auth.owned_project), db: Session = Depends(get_db)):
    rows = db.execute(select(m.FidelityReport).where(m.FidelityReport.project_id == p.id).order_by(m.FidelityReport.created_at.desc()).limit(10)).scalars().all()
    return [{"id": r.id, "reconstruction_id": r.reconstruction_id, "stale": r.stale, "created_at": r.created_at.isoformat(), "data": r.data} for r in rows]


class DiscBody(BaseModel):
    action: str = Field(pattern="^(approve|reject|reopen)$")


@app.post("/api/projects/{project_id}/fidelity/{report_id}/discrepancies/{disc_id}")
def decide_discrepancy(report_id: str, disc_id: str, body: DiscBody, p: m.Project = Depends(auth.owned_project),
                       user: m.User = Depends(auth.current_user), db: Session = Depends(get_db)):
    rep = db.get(m.FidelityReport, report_id)
    if not rep or rep.project_id != p.id:
        raise HTTPException(404, "Report not found")
    data = json.loads(json.dumps(rep.data))
    d = next((x for x in data["discrepancies"] if x["id"] == disc_id), None)
    if not d:
        raise HTTPException(404, "Discrepancy not found")
    result = {"applied": False}
    if body.action == "approve":
        fix = d.get("fix")
        if fix and fix.get("op") == "set_pitch":
            if rep.stale:
                raise HTTPException(409, "This report is out of date. Re-run the Fidelity Inspector before applying its corrections.")
            sv = db.get(m.ScoreVersion, (p.heads or {}).get("score") or "")
            sd = json.loads(json.dumps(sv.data))
            note = next((n for pt in sd["parts"] if pt["id"] == fix["part_id"] for n in pt["notes"] if n["id"] == fix["note_id"]), None)
            if not note:
                raise HTTPException(409, "The note this correction refers to no longer exists in the current score.")
            old = note["pitch"]
            note.update(pitch=int(fix["pitch"]), verified=True, uncertain=False)
            row = new_version(db, p, m.ScoreVersion, "score", validate_score(sd), origin="user-edit",
                              note=f"Approved fidelity correction at {d['time']}s: pitch {old} → {fix['pitch']}", provenance={"fidelity_report_id": rep.id, "discrepancy_id": disc_id})
            d["status"] = "approved"
            rep.data = data
            db.flush()
            result = {"applied": True, "score_version": version_dict(row, False),
                      "invalidation": gates.invalidate(db, p, "score", "An approved fidelity correction changed the score.")}
        else:
            d["status"] = "acknowledged"
    else:
        d["status"] = "rejected" if body.action == "reject" else "open"
    if not result["applied"]:
        rep.data = data
    audit(db, p.id, user, f"discrepancy.{body.action}", f"{d['type']} at {d['time']}s: {body.action}")
    db.commit()
    return {**result, "discrepancy": d}


@app.get("/api/projects/{project_id}/history")
def history(p: m.Project = Depends(auth.owned_project), db: Session = Depends(get_db)):
    rows = db.execute(select(m.AuditEvent).where(m.AuditEvent.project_id == p.id).order_by(m.AuditEvent.created_at.desc()).limit(200)).scalars()
    return [{"id": e.id, "kind": e.kind, "message": e.message, "data": e.data, "created_at": e.created_at.isoformat()} for e in rows]


# =============================================================================================== exports
def _latest(db: Session, p: m.Project, kind: str, **meta) -> m.Asset | None:
    for a in db.execute(select(m.Asset).where(m.Asset.project_id == p.id, m.Asset.kind == kind).order_by(m.Asset.created_at.desc())).scalars():
        if a.verification != "stale" and all((a.meta or {}).get(k) == v for k, v in meta.items()):
            return a
    return None


def export_options(db: Session, p: m.Project) -> list[dict]:
    heads = p.heads or {}
    final_ok, final_why = director.can_export_final(db, p)
    wav, mp3 = _latest(db, p, "master", format="wav"), _latest(db, p, "master", format="mp3")
    rec = db.execute(select(m.Reconstruction).where(m.Reconstruction.project_id == p.id, m.Reconstruction.status != "stale").order_by(m.Reconstruction.version.desc())).scalars().first()
    fid = db.execute(select(m.FidelityReport).where(m.FidelityReport.project_id == p.id).order_by(m.FidelityReport.created_at.desc())).scalars().first()
    has_score, has_lyrics = bool(heads.get("score")), bool(heads.get("lyrics"))
    no_master = "No up-to-date master exists yet. Render, mix and master first."
    mscore = shutil.which("mscore") or shutil.which("musescore") or shutil.which("mscore3") or shutil.which("musescore4")

    def opt(key, label, ok, why="", note=""):
        return {"key": key, "label": label, "available": bool(ok), "reason": "" if ok else why, "note": note, "url": f"/api/projects/{p.id}/export/{key}" if ok else None}

    draft = "" if final_ok else "Will be exported as a DRAFT (filename and tags say so): " + final_why
    return [
        opt("master_wav", "WAV master (24-bit)", wav, no_master, draft),
        opt("master_mp3", "MP3 master (320 kbps)", mp3, no_master, draft),
        opt("stems", "Individual stems (ZIP of WAV files)", rec and rec.stem_asset_ids, "No up-to-date rendered stems exist."),
        opt("midi", "MIDI (exact performance timing)", has_score, "There is no score yet."),
        opt("musicxml", "MusicXML (quantised notation)", has_score, "There is no score yet.", "Rhythms are rounded to sixteenth notes against the detected beat grid; treat as a draft for a notation editor."),
        opt("pdf", "PDF score", has_score and mscore, "PDF engraving needs MuseScore installed on the server, which it is not. Export MusicXML and open it in MuseScore, Sibelius or Dorico to print." if has_score else "There is no score yet."),
        opt("lyrics", "Lyrics and pronunciation guide (text)", has_lyrics, "There are no lyrics yet."),
        opt("fidelity", "Fidelity report (JSON)", fid, "No fidelity report has been produced."),
        opt("archive", "Complete project archive (ZIP)", True, "", "Original recording, every score/lyrics/section version, current audio, reports and provenance manifest."),
    ]


@app.get("/api/projects/{project_id}/exports")
def exports(p: m.Project = Depends(auth.owned_project), db: Session = Depends(get_db)):
    past = db.execute(select(m.AuditEvent).where(m.AuditEvent.project_id == p.id, m.AuditEvent.kind == "export").order_by(m.AuditEvent.created_at.desc()).limit(20)).scalars()
    return {"options": export_options(db, p), "recent": [{"message": e.message, "created_at": e.created_at.isoformat()} for e in past]}


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:60] or "project"


def _tmpfile(suffix: str) -> Path:
    f = tempfile.NamedTemporaryFile(prefix="resonance-export-", suffix=suffix, delete=False)
    f.close()
    return Path(f.name)


def _lyrics_text(p: m.Project, lv: m.LyricsVersion) -> str:
    rows = [p.title, f"Lyrics v{lv.version} ({lv.origin})", f"Language: {lv.data.get('language')}", ""]
    block = None
    for l in lv.data.get("lines", []):
        if l.get("block") != block:
            block = l.get("block")
            rows += ["", f"[{block}]"]
        t = f"{l['start']:7.2f}s  " if l.get("start") is not None else "          "
        rows.append(f"{t}{l['text']}" + ("" if l.get("status") == "verified" else "   (unverified)"))
    rows += ["", "Pronunciation guide (rule-based draft)"]
    rows += [f"  {g['word']:<14} {'-'.join(g['syllables']):<18} {g.get('guide', '')}" for g in lv.data.get("pronunciation", [])]
    return "\n".join(rows) + "\n"


@app.get("/api/projects/{project_id}/export/{key}")
def export(key: str, p: m.Project = Depends(auth.owned_project), user: m.User = Depends(auth.current_user), db: Session = Depends(get_db)):
    opts = {o["key"]: o for o in export_options(db, p)}
    if key not in opts:
        raise HTTPException(404, "Unknown export")
    if not opts[key]["available"]:
        raise HTTPException(409, opts[key]["reason"])
    slug, heads = _slug(p.title), p.heads or {}
    final_ok, _ = director.can_export_final(db, p)
    tag = "FINAL" if final_ok else "DRAFT"

    def done(path: Path, name: str, mime: str):
        audit(db, p.id, user, "export", f"Exported {name}")
        db.commit()
        return FileResponse(path, media_type=mime, filename=name)

    if key in ("master_wav", "master_mp3"):
        fmt = key.split("_")[1]
        a = _latest(db, p, "master", format=fmt)
        return done(storage.local_path(a.storage_key), f"{slug}_{tag}_master.{fmt}", a.mime)
    if key == "midi":
        sv = db.get(m.ScoreVersion, heads["score"])
        out = _tmpfile(".mid")
        sio.score_to_midi(sv.data, out)
        return done(out, f"{slug}_score_v{sv.version}.mid", "audio/midi")
    if key == "musicxml":
        sv = db.get(m.ScoreVersion, heads["score"])
        out = _tmpfile(".musicxml")
        sio.score_to_musicxml(sv.data, out, p.title)
        return done(out, f"{slug}_score_v{sv.version}.musicxml", "application/vnd.recordare.musicxml+xml")
    if key == "pdf":
        sv = db.get(m.ScoreVersion, heads["score"])
        xml, out = _tmpfile(".musicxml"), _tmpfile(".pdf")
        sio.score_to_musicxml(sv.data, xml, p.title)
        import subprocess

        exe = shutil.which("mscore") or shutil.which("musescore") or shutil.which("mscore3") or shutil.which("musescore4")
        r = subprocess.run([exe, "-o", str(out), str(xml)], capture_output=True, timeout=300, env={"QT_QPA_PLATFORM": "offscreen", "PATH": "/usr/bin:/bin"})
        if r.returncode != 0 or out.stat().st_size == 0:
            raise HTTPException(500, "MuseScore failed to engrave the PDF.")
        return done(out, f"{slug}_score_v{sv.version}.pdf", "application/pdf")
    if key == "lyrics":
        lv = db.get(m.LyricsVersion, heads["lyrics"])
        out = _tmpfile(".txt")
        out.write_text(_lyrics_text(p, lv), encoding="utf-8")
        return done(out, f"{slug}_lyrics_v{lv.version}.txt", "text/plain; charset=utf-8")
    if key == "fidelity":
        fr = db.execute(select(m.FidelityReport).where(m.FidelityReport.project_id == p.id).order_by(m.FidelityReport.created_at.desc())).scalars().first()
        out = _tmpfile(".json")
        out.write_text(json.dumps({"project": p.title, "report_id": fr.id, "stale": fr.stale, "created_at": fr.created_at.isoformat(), **fr.data}, indent=2))
        return done(out, f"{slug}_fidelity_report.json", "application/json")
    if key == "stems":
        rec = db.execute(select(m.Reconstruction).where(m.Reconstruction.project_id == p.id, m.Reconstruction.status != "stale").order_by(m.Reconstruction.version.desc())).scalars().first()
        out = _tmpfile(".zip")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
            for i in rec.stem_asset_ids:
                a = db.get(m.Asset, i)
                z.write(storage.local_path(a.storage_key), f"{_slug(a.label)}.wav")
        return done(out, f"{slug}_stems_v{rec.version}.zip", "application/zip")
    if key == "archive":
        out = _tmpfile(".zip")
        manifest = {"project": project_dict(db, p), "exported_at": m.now().isoformat(), "status": tag, "assets": [], "gates": gates.gate_states(db, p)}
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for a in db.execute(select(m.Asset).where(m.Asset.project_id == p.id).order_by(m.Asset.created_at)).scalars():
                if a.kind in ("working", "waveform", "analysis") or a.verification == "stale":
                    continue
                path = storage.local_path(a.storage_key)
                if path.exists():
                    arc = f"{a.kind}/{a.id[:8]}_{a.filename}"
                    z.write(path, arc, compress_type=zipfile.ZIP_STORED if a.mime.startswith("audio") else zipfile.ZIP_DEFLATED)
                    manifest["assets"].append({**asset_dict(a), "archive_path": arc})
            for kind, (model, _) in DOCS.items():
                for v in db.execute(select(model).where(model.project_id == p.id).order_by(model.version)).scalars():
                    z.writestr(f"{kind}/v{v.version:03d}{'_CURRENT' if heads.get(kind) == v.id else ''}.json", json.dumps(version_dict(v), indent=1))
            for fr in db.execute(select(m.FidelityReport).where(m.FidelityReport.project_id == p.id)).scalars():
                z.writestr(f"fidelity/{fr.id}{'_stale' if fr.stale else ''}.json", json.dumps(fr.data, indent=1))
            z.writestr("manifest.json", json.dumps(manifest, indent=1))
        return done(out, f"{slug}_project_archive.zip", "application/zip")
    raise HTTPException(404, "Unknown export")


@app.get("/api/projects/{project_id}/score.musicxml")
def score_musicxml(version_id: str | None = None, p: m.Project = Depends(auth.owned_project), db: Session = Depends(get_db)):
    """Notation for the on-screen score viewer (same quantised export as the MusicXML download)."""
    sv = db.get(m.ScoreVersion, version_id or (p.heads or {}).get("score") or "")
    if not sv or sv.project_id != p.id:
        raise HTTPException(404, "No score")
    out = _tmpfile(".musicxml")
    try:
        sio.score_to_musicxml(sv.data, out, p.title)
        return Response(out.read_text(encoding="utf-8"), media_type="application/vnd.recordare.musicxml+xml")
    finally:
        out.unlink(missing_ok=True)


# =============================================================================================== settings / capabilities
def capabilities() -> list[dict]:
    fs, dm, asr = synth.engine_info(), demucs_status(), _asr_status()
    bp_ok, bp_why = mu.basic_pitch_available()
    demucs_weights = False
    if dm.get("installed"):
        import os
        hub = Path(os.environ.get("TORCH_HOME", Path.home() / ".cache" / "torch")) / "hub" / "checkpoints"
        demucs_weights = bool(os.environ.get("RESONANCE_DEMUCS_REPO")) or (hub.exists() and any(hub.glob("*.th")))
    mscore = shutil.which("mscore") or shutil.which("musescore")

    def c(area, name, ok, detail, needs="", cost=""):
        return {"area": area, "name": name, "status": "ready" if ok else "needs_setup", "detail": detail, "needs": needs, "cost": cost}

    return [
        c("Decoding & restoration", "FFmpeg + RESONANCE DSP", bool(shutil.which("ffmpeg")), "MP3/WAV/FLAC/M4A decoding, noise reduction, EQ, compression, reverb, limiting, loudness.", "Install ffmpeg."),
        c("Source separation", f"Demucs ({settings.demucs_model})", dm.get("installed") and demucs_weights,
          "Voice / accompaniment stems. " + ("Model weights are present." if demucs_weights else "Library installed but model weights are NOT downloaded."),
          "Allow network access to dl.fbaipublicfiles.com once, or set RESONANCE_DEMUCS_REPO to a folder with the weights.", "Free (MIT). ~80 MB download, CPU time roughly equal to the recording length."),
        c("Melody transcription", "pYIN (librosa)", True, "Single-line pitch tracking with per-note confidence."),
        c("Polyphonic transcription", "Basic Pitch (Spotify, ONNX)", bp_ok, "Multi-note detection. Unreliable on dense choir textures; always needs review." if bp_ok else bp_why, "pip install basic-pitch onnxruntime", "Free (Apache-2.0)."),
        c("Lyric recognition", "Whisper (faster-whisper)", asr["available"], "Automatic recognition of sung words." if asr["available"] else asr["reason"],
          "pip install faster-whisper and allow one model download (huggingface.co).", "Free; sung Latin accuracy is poor without human correction."),
        c("Instrument rendering", f"FluidSynth {fs.get('version', '')} + {fs.get('soundfont_name', '')}", fs["available"],
          "General MIDI SoundFont rendering: accurate notes, basic realism." if fs["available"] else fs["reason"], "Install fluidsynth and a GM SoundFont.", "Free (FluidR3_GM, MIT)."),
        c("Professional instruments", "Sampled orchestral / pipe-organ libraries", False, "Not connected. Commercial libraries (e.g. Hauptwerk organs, Spitfire, VSL) run as desktop plug-ins, not server APIs.",
          "Export MIDI + stems to a DAW that hosts the libraries, or point RESONANCE_SOUNDFONT at a higher-quality SFZ/SF2 set.", "Roughly £150–£600 per library."),
        c("Choir synthesis", "Latin SATB choir engine", False, "No engine is connected and none is simulated. Vocal parts render as a wordless guide tone.",
          "Human singers recorded against the rehearsal tracks (stem import is built in), or a licensed word-building choir library in a DAW.", "Session singers: typically £150–£400 per singer per session. Word-building choir libraries: roughly £300–£700."),
        c("PDF engraving", "MuseScore", bool(mscore), "Print-ready score PDF." if mscore else "Not installed; MusicXML export works.", "Install MuseScore 3/4 on the server.", "Free (GPL)."),
        c("PDF score reading (OMR)", "Optical music recognition", False, "Not available. PDFs are stored as visual references only.", "Convert with Audiveris or your notation software and import MusicXML.", "Free (Audiveris, AGPL)."),
        c("Creative generation (Mode C)", "Suno or similar", False, "Not connected. There is no official public Suno API that this build can use within its terms; private interfaces are not automated.", "An officially supported API and your approval.", "Subscription-priced; varies."),
    ]


@app.get("/api/capabilities")
def get_capabilities(user: m.User = Depends(auth.current_user)):
    return {"engines": capabilities(), "instruments": GM_PROGRAMS, "reverb_presets": REVERB_PRESETS, "master_targets": MASTER_TARGETS,
            "limits": {"max_upload_mb": settings.max_upload_mb, "audio_types": sorted(AUDIO_EXT), "score_types": sorted(SCORE_EXT)}}


class ProviderBody(BaseModel):
    anthropic_api_key: str | None = Field(default=None, max_length=300)
    anthropic_model: str | None = Field(default=None, max_length=100)


def _mask(v: str) -> str:
    return "" if not v else ("•" * 8 + v[-4:] if len(v) > 8 else "•" * 8)


@app.get("/api/settings/providers")
def get_providers(user: m.User = Depends(auth.current_user), db: Session = Depends(get_db)):
    key = llm.get_setting(db, "anthropic_api_key")
    return {"anthropic": {"configured": bool(key), "api_key_masked": _mask(key), "model": llm.get_setting(db, "anthropic_model") or llm.DEFAULT_MODEL,
                          "used_for": "Optional plain-English summaries of findings. Receives structured numbers and text only, never audio. Off unless enabled per project."}}


@app.put("/api/settings/providers")
def put_providers(body: ProviderBody, user: m.User = Depends(auth.current_user), db: Session = Depends(get_db)):
    for k, v in body.model_dump(exclude_unset=True).items():
        if v is None:
            continue
        row = db.get(m.ProviderSetting, k)
        if row:
            row.value = v.strip()
        else:
            db.add(m.ProviderSetting(key=k, value=v.strip()))
    audit(db, None, user, "settings.providers", "Provider settings updated")
    db.commit()
    return get_providers(user, db)


@app.post("/api/settings/providers/anthropic/test")
def test_provider(user: m.User = Depends(auth.current_user), db: Session = Depends(get_db)):
    prov = llm.get_provider(db)
    if not prov:
        return {"ok": False, "detail": "No API key is saved."}
    return prov.test()


@app.get("/api/dashboard")
def dashboard(user: m.User = Depends(auth.current_user), db: Session = Depends(get_db)):
    projects = db.execute(select(m.Project).where(m.Project.owner_id == user.id).order_by(m.Project.updated_at.desc())).scalars().all()
    ids = [p.id for p in projects]
    titles = {p.id: p.title for p in projects}
    cards = [project_dict(db, p) for p in projects]
    recent_jobs = db.execute(select(m.Job).where(m.Job.project_id.in_(ids)).order_by(m.Job.created_at.desc()).limit(12)).scalars().all() if ids else []
    exports_ = db.execute(select(m.AuditEvent).where(m.AuditEvent.project_id.in_(ids), m.AuditEvent.kind == "export").order_by(m.AuditEvent.created_at.desc()).limit(8)).scalars().all() if ids else []
    return {
        "projects": cards,
        "pending_approvals": [{"project_id": c["id"], "title": c["title"], "gate": c["pending_gate"], "stage": c["stage"], "next_action": c["next_action"]}
                              for c in cards if c["pending_gate"] and c["source"] and not c["active_jobs"]],
        "completed": [c for c in cards if c["pending_gate"] is None],
        "processing": [director.job_dict(j) | {"project_title": titles.get(j.project_id), "project_id": j.project_id, "agent_name": AGENTS[j.agent]["name"]} for j in recent_jobs],
        "recent_exports": [{"project_id": e.project_id, "title": titles.get(e.project_id), "message": e.message, "created_at": e.created_at.isoformat()} for e in exports_],
    }


@app.get("/api/health")
def health():
    return {"ok": True, "worker": bool(jobs.worker._thread and jobs.worker._thread.is_alive())}


# =============================================================================================== frontend (production build)
if settings.frontend_dist.exists():
    from fastapi.staticfiles import StaticFiles

    app.mount("/assets", StaticFiles(directory=settings.frontend_dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "Not found")
        f = (settings.frontend_dist / path).resolve()
        if path and settings.frontend_dist.resolve() in f.parents and f.is_file():
            return FileResponse(f)
        return FileResponse(settings.frontend_dist / "index.html")
