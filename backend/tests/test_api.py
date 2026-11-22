"""Uploads, permissions/privacy, job system, versioning. No slow audio analysis here."""
import hashlib
import io
import datetime as dt

import pytest
from sqlalchemy import select

import helpers as h
from conftest import make_client, new_project
from resonance import jobs
from resonance import models as m
from resonance.agents import AGENTS, Blocked
from resonance.config import settings
from resonance.db import SessionLocal
from resonance.storage import storage


def _post_source(client, pid, name, data, mime="audio/mpeg"):
    return client.post(f"/api/projects/{pid}/source", files={"file": (name, io.BytesIO(data), mime)})


# ------------------------------------------------------------------ 1. upload validation
def test_upload_rejects_wrong_type_empty_and_fake_audio(client, fixture_audio):
    pid = new_project(client, "upload validation")["id"]
    r = _post_source(client, pid, "notes.txt", b"hello")
    assert r.status_code == 415 and ".mp3" in r.json()["detail"]
    assert _post_source(client, pid, "malware.exe", b"MZ" * 100).status_code == 415
    assert _post_source(client, pid, "empty.mp3", b"").status_code == 422
    r = _post_source(client, pid, "fake.mp3", b"this is definitely not an mp3 file\n" * 200)
    assert r.status_code == 422 and "not readable audio" in r.json()["detail"]
    # nothing was kept from the rejected attempts, so a real upload still works
    assert h.ok(client.get(f"/api/projects/{pid}/assets")) == []
    assert not any((storage.local_path(f"{pid}/source").glob("*")) if storage.local_path(f"{pid}/source").exists() else [])
    assert h.upload_source(client, pid, fixture_audio["mp3"]).status_code == 201


def test_upload_size_limit(client, fixture_audio, monkeypatch):
    pid = new_project(client, "size limit")["id"]
    monkeypatch.setattr(settings, "max_upload_mb", 0)
    r = h.upload_source(client, pid, fixture_audio["mp3"])
    assert r.status_code == 413
    assert h.ok(client.get(f"/api/projects/{pid}/assets")) == []


def test_source_is_immutable_hashed_and_filename_sanitised(client, fixture_audio):
    pid = new_project(client, "immutable source")["id"]
    raw = fixture_audio["mp3"].read_bytes()
    r = _post_source(client, pid, "../../etc/pass wd<script>.mp3", raw)
    assert r.status_code == 201
    a = r.json()
    assert "/" not in a["filename"] and ".." not in a["filename"] and "<" not in a["filename"] and a["filename"].endswith(".mp3")
    assert a["immutable"] is True and a["kind"] == "source"
    assert a["sha256"] == hashlib.sha256(raw).hexdigest() and a["size_bytes"] == len(raw)
    assert a["meta"]["probe"]["codec"] == "mp3"
    # the stored bytes are the uploaded bytes, and the key stays inside the project folder
    with SessionLocal() as db:
        row = db.get(m.Asset, a["id"])
        assert row.storage_key.startswith(f"{pid}/source/")
        assert storage.local_path(row.storage_key).read_bytes() == raw
    assert client.get(a["url"]).content == raw
    r2 = h.upload_source(client, pid, fixture_audio["mp3"])
    assert r2.status_code == 409 and "immutable" in r2.json()["detail"]


def test_storage_refuses_keys_outside_root():
    with pytest.raises(ValueError):
        storage.local_path("../../outside.txt")


def test_project_requires_rights_acknowledgement(client):
    r = client.post("/api/projects", json={"title": "x", "rights_acknowledged": False})
    assert r.status_code == 422
    p = new_project(client, "rights")
    assert p["rights"]["used_for_model_training"] is False and p["rights"]["acknowledged_by"] == "ivan@example.org"
    assert p["allow_external_ai"] is False                      # external AI is opt-in per project


# ------------------------------------------------------------------ 13. permissions / privacy
def test_unauthenticated_requests_are_rejected(app, client):
    from fastapi.testclient import TestClient
    pid = new_project(client, "auth")["id"]
    anon = TestClient(app)
    for method, url in [("get", "/api/projects"), ("get", f"/api/projects/{pid}"), ("get", f"/api/projects/{pid}/assets"),
                        ("post", f"/api/projects/{pid}/pipeline/analyse"), ("get", "/api/dashboard"), ("get", "/api/settings/providers"),
                        ("get", f"/api/projects/{pid}/export/archive"), ("delete", f"/api/projects/{pid}")]:
        assert getattr(anon, method)(url).status_code == 401, url


def test_login_rules_and_password_hashing(app):
    from fastapi.testclient import TestClient
    c = TestClient(app)
    assert c.post("/api/auth/register", json={"email": "pw@example.org", "password": "short"}).status_code == 422
    assert c.post("/api/auth/register", json={"email": "pw@example.org", "password": "a long enough password"}).status_code == 200
    assert c.post("/api/auth/register", json={"email": "PW@example.org", "password": "a long enough password"}).status_code == 409
    with SessionLocal() as db:
        u = db.execute(select(m.User).where(m.User.email == "pw@example.org")).scalars().one()
        assert "a long enough password" not in u.password_hash and u.password_hash.startswith("scrypt$")
        sess = db.execute(select(m.AuthSession).where(m.AuthSession.user_id == u.id)).scalars().first()
        assert len(sess.token_hash) == 64                       # only a hash of the session token is stored
    fresh = TestClient(app)
    assert fresh.post("/api/auth/login", json={"email": "pw@example.org", "password": "the wrong password"}).status_code == 401
    assert fresh.post("/api/auth/login", json={"email": "nobody@example.org", "password": "the wrong password"}).status_code == 401
    assert fresh.get("/api/auth/me").status_code == 401
    assert fresh.post("/api/auth/login", json={"email": "pw@example.org", "password": "a long enough password"}).status_code == 200
    assert fresh.get("/api/auth/me").json()["email"] == "pw@example.org"
    fresh.post("/api/auth/logout")
    assert fresh.get("/api/auth/me").status_code == 401


def test_other_users_cannot_see_or_touch_a_project(app, client, fixture_audio, monkeypatch):
    pid = new_project(client, "private project")["id"]
    asset = h.ok(h.upload_source(client, pid, fixture_audio["mp3"]))

    def boom(ctx):
        raise Blocked("blocked for the test")
    monkeypatch.setitem(AGENTS["forensics"], "run", boom)
    job = h.ok(client.post(f"/api/projects/{pid}/agents/forensics/run"))
    h.drain()

    other = make_client(app, "intruder@example.org")
    assert pid not in [p["id"] for p in other.get("/api/projects").json()]
    checks = [("get", f"/api/projects/{pid}"), ("patch", f"/api/projects/{pid}"), ("delete", f"/api/projects/{pid}"),
              ("get", f"/api/projects/{pid}/assets"), ("get", asset["url"]), ("get", f"/api/projects/{pid}/status"),
              ("get", f"/api/projects/{pid}/jobs"), ("post", f"/api/jobs/{job['id']}/retry"), ("post", f"/api/jobs/{job['id']}/cancel"),
              ("get", f"/api/projects/{pid}/exports"), ("get", f"/api/projects/{pid}/export/archive"), ("get", f"/api/projects/{pid}/doc/score"),
              ("post", f"/api/projects/{pid}/gates/1"), ("get", f"/api/projects/{pid}/events"), ("get", f"/api/projects/{pid}/history")]
    for method, url in checks:
        kw = {"json": {"decision": "approved"}} if "gates" in url else ({"json": {}} if method == "patch" else {})
        assert getattr(other, method)(url, **kw).status_code == 404, url
    assert other.post(f"/api/projects/{pid}/source", files={"file": ("a.mp3", io.BytesIO(b"x"), "audio/mpeg")}).status_code == 404
    assert client.get(f"/api/projects/{pid}").status_code == 200       # still intact for the owner


def test_delete_project_removes_rows_and_files(client, fixture_audio):
    pid = new_project(client, "to delete")["id"]
    h.ok(h.upload_source(client, pid, fixture_audio["mp3"]))
    assert h.run_agent(client, pid, "forensics")["status"] == "succeeded"
    folder = storage.local_path(pid)
    assert folder.exists() and any(folder.rglob("*.wav"))
    assert client.delete(f"/api/projects/{pid}").json() == {"deleted": True}
    assert not folder.exists()
    assert client.get(f"/api/projects/{pid}").status_code == 404
    with SessionLocal() as db:
        for model in (m.Asset, m.Job, m.AgentOutput, m.ReviewDecision, m.ScoreVersion):
            assert db.execute(select(model).where(model.project_id == pid)).scalars().first() is None, model.__name__


def test_retention_purges_lapsed_projects_only(client):
    from resonance.main import purge_expired
    old, keep, unlimited = (new_project(client, t)["id"] for t in ("lapsed", "recent", "no retention"))
    with SessionLocal() as db:
        db.get(m.Project, old).retention_days = 7
        db.get(m.Project, old).updated_at = m.now() - dt.timedelta(days=8)
        db.get(m.Project, keep).retention_days = 7
        db.get(m.Project, unlimited).updated_at = m.now() - dt.timedelta(days=4000)
        db.commit()
    assert purge_expired() == 1
    assert client.get(f"/api/projects/{old}").status_code == 404
    assert client.get(f"/api/projects/{keep}").status_code == 200
    assert client.get(f"/api/projects/{unlimited}").status_code == 200


def test_provider_secret_is_never_returned(client):
    secret = "sk-ant-test-SECRETVALUE-9876"
    try:
        r = client.put("/api/settings/providers", json={"anthropic_api_key": secret})
        assert r.status_code == 200 and secret not in r.text
        r = client.get("/api/settings/providers")
        body = r.json()["anthropic"]
        assert secret not in r.text and "SECRETVALUE" not in r.text
        assert body["configured"] is True and body["api_key_masked"].endswith("9876")
    finally:
        client.put("/api/settings/providers", json={"anthropic_api_key": ""})
    assert client.get("/api/settings/providers").json()["anthropic"]["configured"] is False


def test_narration_is_off_unless_project_opts_in(client):
    pid = new_project(client, "no external ai")["id"]
    st = h.ok(client.get(f"/api/projects/{pid}/status?narrate=1"))
    assert st["narration"]["source"] == "disabled" and st["narration"]["text"] is None
    h.ok(client.patch(f"/api/projects/{pid}", json={"allow_external_ai": True}))
    st = h.ok(client.get(f"/api/projects/{pid}/status?narrate=1"))
    assert st["narration"]["source"] == "unconfigured"          # no key: says so instead of inventing text


# ------------------------------------------------------------------ 7. job system
@pytest.fixture()
def bare_project(client):
    return new_project(client, "jobs")["id"]


def _enqueue(pid, agent, after=None):
    with SessionLocal() as db:
        j = jobs.enqueue(db, pid, agent, {}, after=after)
        db.commit()
        return j.id


def _job(job_id):
    with SessionLocal() as db:
        return db.get(m.Job, job_id)


def test_failing_job_retries_then_fails(bare_project, monkeypatch):
    calls = []

    def flaky(ctx):
        calls.append(ctx.job.attempts)
        ctx.progress(0.3, "about to break")
        raise RuntimeError("disk on fire")
    monkeypatch.setitem(AGENTS["forensics"], "run", flaky)
    jid = _enqueue(bare_project, "forensics")
    h.drain()
    j = _job(jid)
    assert calls == [1, 2] and j.attempts == j.max_attempts == settings.job_max_attempts == 2
    assert j.status == "failed" and "disk on fire" in j.error and j.finished_at is not None
    assert any("Attempt 1 failed" in e["msg"] for e in j.log) and any("about to break" in e["msg"] for e in j.log)


def test_job_that_recovers_on_retry_succeeds(bare_project, monkeypatch):
    state = {"n": 0}

    def once(ctx):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("transient")
    monkeypatch.setitem(AGENTS["forensics"], "run", once)
    jid = _enqueue(bare_project, "forensics")
    h.drain()
    j = _job(jid)
    assert j.status == "succeeded" and j.attempts == 2 and j.progress == 1.0


def test_blocked_job_is_not_retried(bare_project, monkeypatch):
    calls = []

    def blocked(ctx):
        calls.append(1)
        raise Blocked("The model weights are missing. Install them.")
    monkeypatch.setitem(AGENTS["separation"], "run", blocked)
    jid = _enqueue(bare_project, "separation")
    h.drain()
    j = _job(jid)
    assert len(calls) == 1 and j.status == "blocked" and j.error == "The model weights are missing. Install them."


def test_dependent_job_cancelled_on_failure_but_runs_after_blocked(bare_project, monkeypatch):
    ran = []
    monkeypatch.setitem(AGENTS["forensics"], "run", lambda ctx: (_ for _ in ()).throw(RuntimeError("nope")))
    monkeypatch.setitem(AGENTS["separation"], "run", lambda ctx: (_ for _ in ()).throw(Blocked("no model")))
    monkeypatch.setitem(AGENTS["transcription"], "run", lambda ctx: ran.append("transcription"))
    monkeypatch.setitem(AGENTS["structure"], "run", lambda ctx: ran.append("structure"))
    a = _enqueue(bare_project, "forensics")
    b = _enqueue(bare_project, "structure", after=a)
    h.drain()
    assert _job(a).status == "failed" and _job(b).status == "cancelled" and "forensics" in _job(b).error and ran == []
    c = _enqueue(bare_project, "separation")
    d = _enqueue(bare_project, "transcription", after=c)
    h.drain()
    assert _job(c).status == "blocked" and _job(d).status == "succeeded" and ran == ["transcription"]


def test_dependent_job_waits_for_running_dependency(bare_project, monkeypatch):
    a = _enqueue(bare_project, "forensics")
    b = _enqueue(bare_project, "structure", after=a)
    with SessionLocal() as db:
        db.get(m.Job, a).status = "running"          # someone else is working on it
        db.commit()
    assert jobs.worker.run_one() is False             # nothing runnable
    assert _job(b).status == "queued"
    with SessionLocal() as db:
        db.get(m.Job, a).status = "cancelled"
        db.commit()
    h.drain()
    assert _job(b).status == "cancelled"


def test_recover_interrupted_requeues_or_fails(bare_project, monkeypatch):
    monkeypatch.setitem(AGENTS["forensics"], "run", lambda ctx: None)
    a, b = _enqueue(bare_project, "forensics"), _enqueue(bare_project, "forensics")
    with SessionLocal() as db:
        ja, jb = db.get(m.Job, a), db.get(m.Job, b)
        ja.status, ja.attempts = "running", 1         # crashed mid-run with attempts left
        jb.status, jb.attempts = "running", 2         # crashed on its last attempt
        db.commit()
    assert jobs.recover_interrupted() == 2
    assert _job(a).status == "queued" and _job(b).status == "failed" and "restart" in _job(b).error
    h.drain()
    assert _job(a).status == "succeeded" and _job(a).attempts == 2


def test_retry_and_cancel_endpoints(client, bare_project, monkeypatch):
    monkeypatch.setitem(AGENTS["forensics"], "run", lambda ctx: (_ for _ in ()).throw(Blocked("not yet")))
    first = h.ok(client.post(f"/api/projects/{bare_project}/agents/forensics/run"))
    assert client.post(f"/api/projects/{bare_project}/agents/forensics/run").status_code == 409     # already queued
    assert client.post(f"/api/jobs/{first['id']}/retry").status_code == 409                          # not finished yet
    h.drain()
    monkeypatch.setitem(AGENTS["forensics"], "run", lambda ctx: None)
    second = h.ok(client.post(f"/api/jobs/{first['id']}/retry"))
    assert second["id"] != first["id"] and second["status"] == "queued"
    assert h.ok(client.post(f"/api/jobs/{second['id']}/cancel"))["status"] == "cancelled"
    assert client.post(f"/api/jobs/{second['id']}/cancel").status_code == 409
    h.drain()
    assert _job(second["id"]).status == "cancelled" and _job(first["id"]).status == "blocked"
    assert client.post(f"/api/projects/{bare_project}/agents/nonsense/run").status_code == 404


def test_pipeline_needs_a_source_and_refuses_double_start(client, bare_project, fixture_audio, monkeypatch):
    assert client.post(f"/api/projects/{bare_project}/pipeline/analyse").status_code == 409
    h.ok(h.upload_source(client, bare_project, fixture_audio["mp3"]))
    for a in ("forensics", "separation", "transcription", "structure"):
        monkeypatch.setitem(AGENTS[a], "run", lambda ctx: None)
    started = h.ok(client.post(f"/api/projects/{bare_project}/pipeline/analyse"))
    assert [j["agent"] for j in started] == ["forensics", "separation", "transcription", "structure"]
    assert [j["params"].get("after") for j in started] == [None, started[0]["id"], started[1]["id"], started[2]["id"]]
    assert client.post(f"/api/projects/{bare_project}/pipeline/analyse").status_code == 409
    assert h.drain() == 4
    assert client.post(f"/api/projects/{bare_project}/pipeline/nope").status_code == 404


# ------------------------------------------------------------------ 8. versioning
def test_every_save_is_a_new_immutable_version(client):
    pid = new_project(client, "versions")["id"]
    v1 = h.ok(h.put_doc(client, pid, "sections", {"sections": [{"start": 0, "end": 10, "label": "Intro", "kind": "instrumental"}]}))["current"]
    v2 = h.ok(h.put_doc(client, pid, "sections", {"sections": [{"start": 0, "end": 12, "label": "Introduction", "kind": "instrumental"},
                                                              {"start": 12, "end": 30, "label": "Verse 1", "kind": "vocal"}]}))["current"]
    assert (v1["version"], v2["version"]) == (1, 2) and v1["parent_id"] is None and v2["parent_id"] == v1["id"]
    assert v2["origin"] == "user-edit" and v2["provenance"]["user"] == "ivan@example.org"
    old = h.get_doc(client, pid, "sections", v1["id"])["current"]
    assert old["data"]["sections"][0]["label"] == "Intro" and len(old["data"]["sections"]) == 1      # untouched
    doc = h.get_doc(client, pid, "sections")
    assert doc["head_id"] == v2["id"] and [v["version"] for v in doc["versions"]] == [2, 1]
    # restoring v1 does not rewind history: it appends v3 with v1's content
    v3 = h.ok(client.post(f"/api/projects/{pid}/doc/sections/{v1['id']}/make-current"))["current"]
    assert v3["version"] == 3 and v3["parent_id"] == v2["id"] and v3["data"] == v1["data"] and v3["provenance"]["restored_from"] == v1["id"]
    assert h.get_doc(client, pid, "sections", v2["id"])["current"]["data"]["sections"][1]["label"] == "Verse 1"
    assert len(h.get_doc(client, pid, "sections")["versions"]) == 3
    assert client.post(f"/api/projects/{pid}/doc/sections/doesnotexist/make-current").status_code == 404


def test_invalid_documents_are_rejected_without_creating_versions(client):
    pid = new_project(client, "invalid docs")["id"]
    note = lambda **kw: {"parts": [{"name": "P", "notes": [{"start": 0.0, "end": 1.0, "pitch": 60, **kw}]}]}
    assert h.put_doc(client, pid, "score", note(end=0.0)).status_code == 422
    assert h.put_doc(client, pid, "score", note(pitch=200)).status_code == 422
    assert h.put_doc(client, pid, "score", note(velocity=0)).status_code == 422
    assert h.get_doc(client, pid, "score")["versions"] == []
    assert h.put_doc(client, pid, "score", note()).status_code == 200
    overlap = {"sections": [{"start": 0, "end": 12, "label": "A"}, {"start": 10, "end": 20, "label": "B"}]}
    r = h.put_doc(client, pid, "sections", overlap)
    assert r.status_code == 422 and "overlap" in r.json()["detail"]
    assert h.put_doc(client, pid, "sections", {"sections": [{"start": 5, "end": 5, "label": "A"}]}).status_code == 422
    assert h.put_doc(client, pid, "sections", {"sections": [{"start": 0, "end": 5, "label": "A", "kind": "banana"}]}).status_code == 422
    assert h.get_doc(client, pid, "sections")["versions"] == []
    assert h.put_doc(client, pid, "mix", {"reverb_preset": "stadium", "tracks": {}}).status_code == 422
    assert h.put_doc(client, pid, "master", {"target": "louder_than_everything"}).status_code == 422
    assert client.put(f"/api/projects/{pid}/doc/nonsense", json={"data": {}}).status_code == 404


def test_one_users_versions_cannot_be_read_through_another_project(app, client):
    mine = new_project(client, "mine")["id"]
    v = h.ok(h.put_doc(client, mine, "sections", {"sections": [{"start": 0, "end": 10, "label": "Secret"}]}))["current"]
    other = make_client(app, "intruder2@example.org")
    theirs = new_project(other, "theirs")["id"]
    assert other.get(f"/api/projects/{theirs}/doc/sections?version_id={v['id']}").json()["current"] is None
    assert other.post(f"/api/projects/{theirs}/doc/sections/{v['id']}/make-current").status_code == 404
