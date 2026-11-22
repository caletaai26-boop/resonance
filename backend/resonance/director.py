"""Agent 12: Production Director. Deterministic orchestration: it reads real project state, decides what
can run next, enqueues dependent jobs, enforces the review gates and explains status in plain English."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import gates, jobs
from . import models as m
from .agents import AGENTS

PIPELINES = {
    # name -> ordered agents; each waits for the previous one ('blocked' steps do not stop the chain)
    "analyse": ["forensics", "separation", "transcription", "structure"],
    "produce": ["instrumental", "mixing", "mastering", "fidelity"],
    "restore": ["restore", "mastering"],
}


def start_pipeline(db: Session, project: m.Project, name: str, params: dict | None = None) -> list[m.Job]:
    if name not in PIPELINES:
        raise ValueError(f"unknown pipeline '{name}'")
    if name == "produce" and project.mode == "restore":
        name = "restore"
    out, prev = [], None
    for agent in PIPELINES[name]:
        p = dict((params or {}).get(agent, {}))
        if agent == "forensics" and latest_output(db, project.id, "forensics"):
            p["reanalysis"] = True
        j = jobs.enqueue(db, project.id, agent, p, after=prev.id if prev else None)
        out.append(j)
        prev = j
    db.add(m.AuditEvent(project_id=project.id, kind="pipeline", message=f"Director started the '{name}' pipeline", data={"jobs": [j.id for j in out]}))
    return out


def latest_output(db: Session, project_id: str, agent: str) -> m.AgentOutput | None:
    return db.execute(select(m.AgentOutput).where(m.AgentOutput.project_id == project_id, m.AgentOutput.agent == agent)
                      .order_by(m.AgentOutput.created_at.desc(), m.AgentOutput.id.desc())).scalars().first()


def latest_job(db: Session, project_id: str, agent: str) -> m.Job | None:
    return db.execute(select(m.Job).where(m.Job.project_id == project_id, m.Job.agent == agent)
                      .order_by(m.Job.created_at.desc(), m.Job.id.desc())).scalars().first()


def job_dict(j: m.Job) -> dict:
    return {"id": j.id, "agent": j.agent, "status": j.status, "progress": j.progress, "step": j.step, "attempts": j.attempts,
            "max_attempts": j.max_attempts, "error": j.error, "params": j.params, "log": (j.log or [])[-30:],
            "created_at": j.created_at.isoformat(), "started_at": j.started_at.isoformat() if j.started_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None}


NEXT_HINT = {
    "forensics": "Run the analysis", "separation": "Runs after forensics", "transcription": "Runs after separation", "alignment": "Import a score on the Score tab, then align it",
    "lyrics": "Paste the words you hear or load a reference text", "structure": "Runs after transcription",
    "arrangement": "Build after the score is approved", "choir": "Prepare rehearsal pack once an arrangement exists",
    "instrumental": "Render once the arrangement is approved", "mixing": "Runs after rendering", "mastering": "Runs after mixing",
    "fidelity": "Runs after mixing", "restore": "Mode A only",
}


def agents_view(db: Session, project: m.Project) -> list[dict]:
    """Agent panel data. Everything shown comes from Job / AgentOutput rows: no synthetic progress."""
    out = []
    for key, a in sorted(AGENTS.items(), key=lambda kv: kv[1]["number"]):
        if key == "restore" and project.mode != "restore":
            continue
        j, o = latest_job(db, project.id, key), latest_output(db, project.id, key)
        status = j.status if j else "idle"
        out.append({"key": key, "number": a["number"], "name": a["name"], "role": a["role"], "status": status,
                    "job": job_dict(j) if j else None,
                    "output": {"id": o.id, "summary": o.summary, "data": o.data, "stale": o.stale, "created_at": o.created_at.isoformat(),
                               "from_job": o.job_id} if o else None,
                    "next_action": ("Fix the blocker below, then run again" if status == "blocked" else "Look at the error and retry" if status == "failed"
                                    else "Working" if status in ("queued", "running") else "Review the findings" if o and not o.stale else NEXT_HINT.get(key, ""))})
    out.append({"key": "director", "number": 12, "name": "Production Director", "role": "Coordinates the other agents, enforces review gates and keeps version history.",
                "status": "running" if any(x["status"] in ("queued", "running") for x in out) else "idle", "job": None, "output": None, "next_action": ""})
    return out


def status(db: Session, project: m.Project) -> dict:
    gs = gates.gate_states(db, project)
    heads = project.heads or {}
    src = db.execute(select(m.Asset).where(m.Asset.project_id == project.id, m.Asset.kind == "source")).scalars().first()
    active = db.execute(select(m.Job).where(m.Job.project_id == project.id, m.Job.status.in_(["queued", "running"]))).scalars().all()
    problems = db.execute(select(m.Job).where(m.Job.project_id == project.id, m.Job.status.in_(["failed", "blocked"]))
                          .order_by(m.Job.created_at.desc())).scalars().all()
    latest_by_agent = {}
    for j in problems:
        cur = latest_job(db, project.id, j.agent)
        if cur and cur.id == j.id:
            latest_by_agent[j.agent] = j
    rec = db.execute(select(m.Reconstruction).where(m.Reconstruction.project_id == project.id).order_by(m.Reconstruction.version.desc())).scalars().first()
    fid = db.execute(select(m.FidelityReport).where(m.FidelityReport.project_id == project.id, m.FidelityReport.stale.is_(False))
                     .order_by(m.FidelityReport.created_at.desc())).scalars().first()
    pending = next((g for g in gs if g["state"] != "approved"), None)

    # --- plain-English next step, derived from actual state
    if not src:
        stage, nxt = "Awaiting source recording", "Upload your recording to begin."
    elif active:
        stage, nxt = "Working", "Agents are running: " + ", ".join(sorted({AGENTS[j.agent]["name"] for j in active})) + "."
    elif not latest_output(db, project.id, "forensics"):
        stage, nxt = "Recording uploaded", "Start the analysis."
    elif pending is None:
        stage, nxt = "Final master authorised", "Export the final master."
    else:
        g = pending["gate"]
        stage = f"Gate {g}: {pending['title']}"
        ready = {
            1: True, 2: bool(heads.get("sections")), 3: bool(heads.get("score")), 4: bool(heads.get("arrangement")),
            5: bool(rec and rec.status in ("mixed", "mastered", "final")), 6: bool(fid and rec and fid.reconstruction_id == rec.id),
            7: bool(rec and rec.status in ("mastered", "final")),
        }[g]
        todo = {
            2: "Run structural analysis so there is a section timeline to review.",
            3: "Import the score the choir used (MusicXML/MIDI) and align it, or run the transcription.",
            4: "Build the arrangement from the approved score.",
            5: "Render and mix the arrangement." if project.mode != "restore" else "Run the restoration.",
            6: "Run the Fidelity Inspector on the current render.",
            7: "Master the mix.",
        }
        if ready:
            nxt = f"Review and approve Gate {g} ({pending['title']})" + (" again: it was invalidated by a later change." if pending["state"] == "invalidated" else ".")
        else:
            nxt = todo.get(g, "Continue the workflow.")
    blockers = [{"agent": AGENTS[a]["name"], "status": j.status, "message": j.error.splitlines()[0] if j.error else ""} for a, j in latest_by_agent.items()]
    return {
        "stage": stage, "next_action": nxt, "gates": gs, "blockers": blockers, "active_jobs": [job_dict(j) for j in active],
        "has_source": bool(src), "mode": project.mode,
        "current": {"score_version_id": heads.get("score"), "sections_id": heads.get("sections"), "lyrics_id": heads.get("lyrics"),
                    "arrangement_id": heads.get("arrangement"), "reconstruction": {"id": rec.id, "version": rec.version, "status": rec.status, "mode": rec.mode} if rec else None,
                    "fidelity_report_id": fid.id if fid else None},
        "summary": f"{stage}. {nxt}" + (f" {len(blockers)} step(s) need attention: " + "; ".join(f"{b['agent']} is {b['status']}" for b in blockers) + "." if blockers else ""),
    }


def can_export_final(db: Session, project: m.Project) -> tuple[bool, str]:
    need = gates.applicable(project)
    states = {s["gate"]: s["state"] for s in gates.gate_states(db, project)}
    missing = [g for g in need if states.get(g) != "approved"]
    if missing:
        return False, "A final master can only be exported after every checkpoint is approved. Still open: " + ", ".join(f"Gate {g}" for g in missing) + ". You can export a DRAFT instead."
    return True, ""
