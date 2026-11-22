"""Human review gates and dependency invalidation.

A gate is 'approved' only if its most recent ReviewDecision is an approval. Editing upstream material
appends an 'invalidated' decision to every dependent gate and marks dependent artifacts stale. Nothing
is deleted or overwritten: history stays intact and auditable.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m

GATES = {
    1: {"key": "analysis", "title": "Recording analysed", "explain": "You have looked at the technical report on the recording and accept it as the starting point."},
    2: {"key": "structure", "title": "Musical structure approved", "explain": "The section timeline (intro, verses, refrains, interludes, ending) matches what you hear."},
    3: {"key": "score", "title": "Lyrics and transcription approved", "explain": "The notes and words are what was actually performed. This is the musical source of truth from here on."},
    4: {"key": "arrangement", "title": "Arrangement approved", "explain": "Each part is assigned to the right voice or instrument and nothing has been added that was not in the recording."},
    5: {"key": "render", "title": "Rendered performance reviewed", "explain": "You have listened to the rendered reconstruction."},
    6: {"key": "fidelity", "title": "Fidelity comparison approved", "explain": "You have reviewed the discrepancy report comparing the reconstruction with the original."},
    7: {"key": "master", "title": "Final master authorised", "explain": "The mastered file may be exported as the final version."},
}

MODE_GATES = {"restore": [1, 5, 7], "faithful": [1, 2, 3, 4, 5, 6, 7], "creative": [1, 2, 3, 4, 5, 6, 7]}

# what changed -> which gates lose their approval
INVALIDATES = {
    "analysis": [1, 2, 3, 4, 5, 6, 7],
    "structure": [2, 6, 7],
    "lyrics": [3, 6, 7],
    "score": [3, 4, 5, 6, 7],
    "arrangement": [4, 5, 6, 7],
    "render": [5, 6, 7],
    "fidelity": [6, 7],
    "master": [7],
}


class GateError(Exception):
    pass


def applicable(project: m.Project) -> list[int]:
    return MODE_GATES.get(project.mode, MODE_GATES["faithful"])


def gate_states(db: Session, project: m.Project) -> list[dict]:
    rows = db.execute(select(m.ReviewDecision).where(m.ReviewDecision.project_id == project.id)
                      .order_by(m.ReviewDecision.created_at, m.ReviewDecision.id)).scalars().all()
    latest: dict[int, m.ReviewDecision] = {}
    for r in rows:
        latest[r.gate] = r
    out = []
    for g in applicable(project):
        d = latest.get(g)
        out.append({"gate": g, **GATES[g], "state": d.decision if d else "pending",
                    "comment": d.comment if d else "", "decided_at": d.created_at.isoformat() if d else None,
                    "refs": d.refs if d else {}})
    return out


def is_approved(db: Session, project: m.Project, gate: int) -> bool:
    return any(s["gate"] == gate and s["state"] == "approved" for s in gate_states(db, project))


def decide(db: Session, project: m.Project, gate: int, decision: str, user_id: str | None, comment: str, refs: dict) -> m.ReviewDecision:
    if gate not in applicable(project):
        raise GateError(f"Gate {gate} does not apply in {project.mode} mode.")
    if decision not in ("approved", "rejected"):
        raise GateError("decision must be 'approved' or 'rejected'")
    if decision == "approved":
        states = {s["gate"]: s["state"] for s in gate_states(db, project)}
        missing = [g for g in applicable(project) if g < gate and states.get(g) != "approved"]
        if missing:
            raise GateError("Earlier checkpoints must be approved first: " + ", ".join(f"Gate {g} ({GATES[g]['title']})" for g in missing))
    d = m.ReviewDecision(project_id=project.id, gate=gate, decision=decision, user_id=user_id, comment=comment, refs=refs)
    db.add(d)
    if decision == "rejected":  # a rejection upstream withdraws downstream approvals
        _invalidate_gates(db, project, [g for g in applicable(project) if g > gate], f"Gate {gate} was rejected")
    db.flush()
    return d


def _invalidate_gates(db: Session, project: m.Project, gates: list[int], reason: str) -> list[int]:
    states = {s["gate"]: s["state"] for s in gate_states(db, project)}
    hit = []
    for g in gates:
        if states.get(g) == "approved":
            db.add(m.ReviewDecision(project_id=project.id, gate=g, decision="invalidated", comment=reason, refs={}))
            hit.append(g)
    return hit


def invalidate(db: Session, project: m.Project, changed: str, reason: str) -> dict:
    """Call whenever `changed` (a key of INVALIDATES) gets a new version."""
    hit = _invalidate_gates(db, project, INVALIDATES[changed], reason)
    stale = {"reconstructions": 0, "fidelity_reports": 0, "arrangements": 0, "assets": 0}
    if changed in ("analysis", "score", "arrangement"):
        if changed != "arrangement":
            for a in db.execute(select(m.Arrangement).where(m.Arrangement.project_id == project.id, m.Arrangement.verification != "stale")).scalars():
                a.verification = "stale"; stale["arrangements"] += 1
        for r in db.execute(select(m.Reconstruction).where(m.Reconstruction.project_id == project.id, m.Reconstruction.status != "stale")).scalars():
            if r.mode == "restore" and changed != "analysis":
                continue
            r.status = "stale"; stale["reconstructions"] += 1
        for a in db.execute(select(m.Asset).where(m.Asset.project_id == project.id, m.Asset.kind.in_(["render_stem", "mix", "master"]), m.Asset.verification != "stale")).scalars():
            if (a.meta or {}).get("mode") == "restore" and changed != "analysis":
                continue
            a.verification = "stale"; stale["assets"] += 1
    if changed in ("analysis", "structure", "lyrics", "score", "arrangement", "render"):
        for f in db.execute(select(m.FidelityReport).where(m.FidelityReport.project_id == project.id, m.FidelityReport.stale.is_(False))).scalars():
            f.stale = True; stale["fidelity_reports"] += 1
    if hit or any(stale.values()):
        db.add(m.AuditEvent(project_id=project.id, kind="invalidation", message=reason, data={"changed": changed, "gates": hit, "stale": stale}))
    db.flush()
    return {"gates_invalidated": hit, "stale": stale}
