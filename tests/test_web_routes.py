from fastapi.testclient import TestClient

from ytdigest import db
from ytdigest.web.app import create_app


def test_index_loads(config):
    db.init_db(config.db_path)
    app = create_app(config)
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "ytdigest" in resp.text


def test_channels_page_loads(config):
    db.init_db(config.db_path)
    app = create_app(config)
    client = TestClient(app)
    resp = client.get("/channels")
    assert resp.status_code == 200
    assert "Channels" in resp.text
    assert "Connect &amp; sync with YouTube" in resp.text or "Connect & sync with YouTube" in resp.text


def test_runs_page_loads(config):
    db.init_db(config.db_path)
    app = create_app(config)
    client = TestClient(app)
    resp = client.get("/runs")
    assert resp.status_code == 200
    assert "Run history" in resp.text


def test_run_status_poll(config):
    db.init_db(config.db_path)
    app = create_app(config)
    client = TestClient(app)
    resp = client.get("/runs/status")
    assert resp.status_code == 200
    assert "Run now" in resp.text or "Running" in resp.text
