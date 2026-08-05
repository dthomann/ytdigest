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


def test_settings_page_loads(config, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("digest_hour: 6\ntimezone: Europe/Zurich\n")
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "ytdigest").write_text("#!/bin/sh\n")
    config.config_path = config_path
    db.init_db(config.db_path)
    app = create_app(config)
    client = TestClient(app)
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "Settings" in resp.text
    assert "Daily run schedule" in resp.text
    assert "Web service" in resp.text
