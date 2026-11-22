import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="resonance-test-")
os.environ["RESONANCE_DATA_DIR"] = _tmp
os.environ["RESONANCE_RUN_WORKER"] = "0"       # tests drive the worker synchronously
os.environ["RESONANCE_FRONTEND_DIST"] = "/nonexistent"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def fixture_audio():
    import fixtures
    return fixtures.build(Path(_tmp) / "fixture")


@pytest.fixture(scope="session")
def app():
    from resonance.main import app
    from resonance.db import init_db
    init_db()
    return app


def make_client(app, email="ivan@example.org"):
    c = TestClient(app)
    r = c.post("/api/auth/register", json={"email": email, "password": "correct horse battery", "name": "Tester"})
    if r.status_code == 409:
        r = c.post("/api/auth/login", json={"email": email, "password": "correct horse battery"})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture()
def client(app):
    return make_client(app)


def drain(max_jobs=50):
    """Run queued jobs to completion in-process."""
    from resonance.jobs import worker
    n = 0
    while n < max_jobs and worker.run_one():
        n += 1
    return n


def new_project(client, title="Test hymn", mode="faithful"):
    r = client.post("/api/projects", json={"title": title, "mode": mode, "language": "la", "rights_acknowledged": True})
    assert r.status_code == 201, r.text
    return r.json()
