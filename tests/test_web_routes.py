import time

from fastapi.testclient import TestClient

from ytdigest import db, pipeline
from ytdigest.util import utcnow_iso
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


def _insert_run(conn, *, summarized: int) -> int:
    now = utcnow_iso()
    conn.execute(
        "INSERT INTO runs (started_at, finished_at, status, summarized) VALUES (?, ?, 'ok', ?)",
        (now, now, summarized),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def test_status_swaps_latest_run_after_finish(config, monkeypatch):
    db.init_db(config.db_path)
    conn = db.connect(config.db_path)
    old_id = _insert_run(conn, summarized=1)
    conn.close()

    def fake_pipeline(conn, config):
        new_id = _insert_run(conn, summarized=3)
        return pipeline.RunResult(run_id=new_id, status="ok", summarized=3)

    monkeypatch.setattr("ytdigest.web.services.run_manager.pipeline.run_pipeline", fake_pipeline)

    app = create_app(config)
    client = TestClient(app)

    index = client.get("/")
    assert f"Run #{old_id}" in index.text
    assert "1 summarized" in index.text

    started = client.post("/runs/start")
    assert started.status_code == 200
    assert "Running pipeline" in started.text

    for _ in range(50):
        if app.state.run_manager.state == "finished":
            break
        time.sleep(0.05)
    assert app.state.run_manager.state == "finished"

    status = client.get("/runs/status")
    assert status.status_code == 200
    assert 'id="digest"' in status.text
    assert 'hx-swap-oob="true"' in status.text
    assert f"Run #{old_id + 1}" in status.text or "3 summarized" in status.text
    assert "3 summarized" in status.text

    again = client.get("/runs/status")
    assert 'hx-swap-oob' not in again.text
    assert 'id="digest"' not in again.text


def test_index_shows_latest_run_and_consumes_refresh(config, monkeypatch):
    db.init_db(config.db_path)

    def fake_pipeline(conn, config):
        new_id = _insert_run(conn, summarized=2)
        return pipeline.RunResult(run_id=new_id, status="ok", summarized=2)

    monkeypatch.setattr("ytdigest.web.services.run_manager.pipeline.run_pipeline", fake_pipeline)

    app = create_app(config)
    client = TestClient(app)
    client.post("/runs/start")
    for _ in range(50):
        if app.state.run_manager.state == "finished":
            break
        time.sleep(0.05)
    assert app.state.run_manager.state == "finished"

    index = client.get("/")
    assert "Run #" in index.text
    assert "2 summarized" in index.text

    status = client.get("/runs/status")
    assert "hx-swap-oob" not in status.text


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
    assert "Telegram Q&amp;A bot" in resp.text or "Telegram Q&A bot" in resp.text


def test_settings_bot_enable_route(config, tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("digest_hour: 6\ntimezone: Europe/Zurich\n")
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "ytdigest").write_text("#!/bin/sh\n")
    config.config_path = config_path
    db.init_db(config.db_path)
    app = create_app(config)
    client = TestClient(app)

    monkeypatch.setattr(
        "ytdigest.web.routes.settings.install_bot_service",
        lambda cfg: "Telegram Q&A bot enabled and started",
    )
    resp = client.post("/settings/bot/enable", follow_redirects=False)
    assert resp.status_code == 303
    assert "flash=" in resp.headers["location"]


def test_settings_bot_disable_route(config, tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("digest_hour: 6\ntimezone: Europe/Zurich\n")
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "ytdigest").write_text("#!/bin/sh\n")
    config.config_path = config_path
    db.init_db(config.db_path)
    app = create_app(config)
    client = TestClient(app)

    monkeypatch.setattr(
        "ytdigest.web.routes.settings.uninstall_bot_service",
        lambda cfg: "Telegram Q&A bot stopped and disabled",
    )
    resp = client.post("/settings/bot/disable", follow_redirects=False)
    assert resp.status_code == 303
    assert "flash=" in resp.headers["location"]
