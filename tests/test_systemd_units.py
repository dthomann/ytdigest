"""Tests for systemd unit rendering and service management."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ytdigest.config import ConfigError, load_config, update_config_file
from ytdigest.systemd_units import (
    HANDOFF_MARKER,
    UnitStatus,
    _maybe_sudo_cli as _real_maybe_sudo_cli,
    _sudo_services_argv,
    install_context_from_config,
    install_web_service,
    render_unit,
    sudo_available,
    update_run_schedule,
)


@pytest.fixture(autouse=True)
def no_cli_elevate(monkeypatch):
    """Keep unit tests on the in-process path; elevation is covered separately."""
    monkeypatch.setattr("ytdigest.systemd_units._maybe_sudo_cli", lambda *a, **k: None)


@pytest.fixture
def install_tree(tmp_path):
    install_dir = tmp_path / "ytdigest"
    install_dir.mkdir()
    (install_dir / "venv" / "bin").mkdir(parents=True)
    (install_dir / "venv" / "bin" / "ytdigest").write_text("#!/bin/sh\n")
    (install_dir / "systemd").mkdir()
    repo_systemd = Path(__file__).resolve().parent.parent / "systemd"
    for name in (
        "ytdigest-web.service",
        "ytdigest.service",
        "ytdigest.timer",
        "ytdigest-retry.service",
        "ytdigest-retry.timer",
    ):
        (install_dir / "systemd" / name).write_text(
            (repo_systemd / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    config_path = install_dir / "config.yaml"
    config_path.write_text(
        "data_dir: data\ntimezone: Europe/Zurich\ndigest_hour: 8\n",
        encoding="utf-8",
    )
    return install_dir, config_path


def test_render_units_home_setup(install_tree):
    install_dir, config_path = install_tree
    config = load_config(config_path)
    ctx = install_context_from_config(config)
    assert ctx.setup_mode == "home"
    assert ctx.install_dir == install_dir.resolve()
    web = render_unit("ytdigest-web.service", ctx, digest_hour=8, timezone="Europe/Zurich")
    assert f"User={ctx.service_user}" in web
    assert str(install_dir) in web
    assert "/opt/ytdigest" not in web
    timer = render_unit("ytdigest.timer", ctx, digest_hour=8, timezone="Europe/Zurich")
    assert "OnCalendar=*-*-* 08:00:00 Europe/Zurich" in timer
    run = render_unit("ytdigest.service", ctx, digest_hour=8, timezone="Europe/Zurich")
    assert "run --scheduled" in run
    retry = render_unit("ytdigest-retry.service", ctx, digest_hour=8, timezone="Europe/Zurich")
    assert "run --scheduled --retry-only" in retry
    retry_timer = render_unit("ytdigest-retry.timer", ctx, digest_hour=8, timezone="Europe/Zurich")
    assert "OnCalendar=*:0/15" in retry_timer


def test_render_units_dedicated_paths(tmp_path):
    install_dir = tmp_path / "opt" / "ytdigest"
    install_dir.mkdir(parents=True)
    (install_dir / "venv" / "bin").mkdir(parents=True)
    (install_dir / "venv" / "bin" / "ytdigest").write_text("#!/bin/sh\n")
    repo_systemd = Path(__file__).resolve().parent.parent / "systemd"
    (install_dir / "systemd").mkdir()
    (install_dir / "systemd" / "ytdigest-web.service").write_text(
        (repo_systemd / "ytdigest-web.service").read_text(encoding="utf-8")
    )
    config_path = install_dir / "config.yaml"
    config_path.write_text("digest_hour: 6\n", encoding="utf-8")

    with patch("ytdigest.systemd_units.DEDICATED_INSTALL_DIR", install_dir.resolve()):
        config = load_config(config_path)
        ctx = install_context_from_config(config)
        assert ctx.setup_mode == "dedicated"
        web = render_unit("ytdigest-web.service", ctx, digest_hour=6, timezone="Europe/Zurich")
        assert "User=ytdigest" not in web or ctx.service_user in web


def test_update_config_file(install_tree):
    _, config_path = install_tree
    update_config_file(config_path, {"digest_hour": 7, "timezone": "America/New_York"})
    config = load_config(config_path)
    assert config.digest_hour == 7
    assert config.timezone == "America/New_York"


def test_update_run_schedule_invalid_hour(install_tree):
    _, config_path = install_tree
    config = load_config(config_path)
    with pytest.raises(ConfigError):
        update_run_schedule(config, digest_hour=25, timezone="Europe/Zurich")


def test_update_run_schedule_invalid_timezone(install_tree):
    _, config_path = install_tree
    config = load_config(config_path)
    with pytest.raises(ConfigError):
        update_run_schedule(config, digest_hour=6, timezone="Not/A/Timezone")


@patch("ytdigest.systemd_units.get_unit_status")
@patch("ytdigest.systemd_units.install_timer_service")
def test_update_run_schedule_without_timer(mock_install, mock_status, install_tree):
    _, config_path = install_tree
    config = load_config(config_path)
    mock_status.return_value.installed = False
    message = update_run_schedule(config, digest_hour=9, timezone="Europe/Zurich")
    assert "Schedule saved" in message
    assert "install the timer" in message.lower()
    mock_install.assert_not_called()


@patch("ytdigest.systemd_units.install_timer_service")
@patch("ytdigest.systemd_units.get_unit_status")
def test_update_run_schedule_with_timer_installed(mock_status, mock_install, install_tree):
    _, config_path = install_tree
    config = load_config(config_path)
    mock_status.return_value.installed = True
    mock_install.return_value = "ok"
    message = update_run_schedule(config, digest_hour=9, timezone="Europe/Zurich")
    assert "timer updated" in message.lower()
    mock_install.assert_called_once()


@patch("ytdigest.systemd_units._systemctl")
@patch("ytdigest.systemd_units.get_unit_status")
@patch("ytdigest.systemd_units.sudo_available", return_value=True)
def test_restart_web_service(mock_sudo, mock_status, mock_systemctl, install_tree):
    _, config_path = install_tree
    config = load_config(config_path)
    from ytdigest.systemd_units import restart_web_service

    mock_status.return_value = UnitStatus(
        name="ytdigest-web.service",
        label="Web UI",
        installed=True,
        enabled=True,
        active=True,
        detail=None,
    )
    message = restart_web_service(config)
    assert "restarted" in message.lower()
    mock_systemctl.assert_called_once_with("restart", "ytdigest-web", privileged=True)


@patch("ytdigest.systemd_units.get_unit_status")
@patch("ytdigest.systemd_units.sudo_available", return_value=True)
def test_restart_web_service_skips_when_not_installed(mock_sudo, mock_status, install_tree):
    _, config_path = install_tree
    config = load_config(config_path)
    from ytdigest.systemd_units import restart_web_service

    mock_status.return_value = UnitStatus(
        name="ytdigest-web.service",
        label="Web UI",
        installed=False,
        enabled=None,
        active=None,
        detail="not installed",
    )
    message = restart_web_service(config)
    assert "skipped" in message.lower()


@patch("ytdigest.systemd_units._systemctl")
@patch("ytdigest.systemd_units.get_unit_status")
@patch("ytdigest.systemd_units.sudo_available", return_value=True)
def test_restart_bot_service(mock_sudo, mock_status, mock_systemctl, install_tree):
    _, config_path = install_tree
    config = load_config(config_path)
    from ytdigest.systemd_units import restart_bot_service

    mock_status.return_value = UnitStatus(
        name="ytdigest-bot.service",
        label="Telegram Q&A bot",
        installed=True,
        enabled=True,
        active=True,
        detail=None,
    )
    message = restart_bot_service(config)
    assert "restarted" in message.lower()
    mock_systemctl.assert_called_once_with("restart", "ytdigest-bot", privileged=True)


@patch("ytdigest.systemd_units.get_unit_status")
@patch("ytdigest.systemd_units.sudo_available", return_value=True)
def test_restart_bot_service_skips_when_not_installed(mock_sudo, mock_status, install_tree):
    _, config_path = install_tree
    config = load_config(config_path)
    from ytdigest.systemd_units import restart_bot_service

    mock_status.return_value = UnitStatus(
        name="ytdigest-bot.service",
        label="Telegram Q&A bot",
        installed=False,
        enabled=None,
        active=None,
        detail="not installed",
    )
    message = restart_bot_service(config)
    assert "skipped" in message.lower()


@patch("ytdigest.systemd_units.get_unit_status")
@patch("ytdigest.systemd_units.sudo_available", return_value=True)
def test_restart_bot_service_skips_when_not_running(mock_sudo, mock_status, install_tree):
    _, config_path = install_tree
    config = load_config(config_path)
    from ytdigest.systemd_units import restart_bot_service

    mock_status.return_value = UnitStatus(
        name="ytdigest-bot.service",
        label="Telegram Q&A bot",
        installed=True,
        enabled=True,
        active=False,
        detail=None,
    )
    message = restart_bot_service(config)
    assert "not running" in message.lower()


@patch("ytdigest.systemd_units._systemctl")
@patch("ytdigest.systemd_units._daemon_reload")
@patch("ytdigest.systemd_units._write_unit_file")
@patch("ytdigest.systemd_units.sudo_available", return_value=True)
def test_install_bot_service(mock_sudo, mock_write, mock_reload, mock_systemctl, install_tree):
    _, config_path = install_tree
    repo_systemd = Path(__file__).resolve().parent.parent / "systemd"
    install_dir = config_path.parent
    (install_dir / "systemd" / "ytdigest-bot.service").write_text(
        (repo_systemd / "ytdigest-bot.service").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    config = load_config(config_path)
    from ytdigest.systemd_units import install_bot_service

    message = install_bot_service(config)
    assert "bot enabled" in message.lower()
    mock_systemctl.assert_any_call("enable", "--now", "ytdigest-bot", privileged=True)


@patch("ytdigest.systemd_units._systemctl")
@patch("ytdigest.systemd_units._daemon_reload")
@patch("ytdigest.systemd_units._remove_unit_file")
@patch("ytdigest.systemd_units.sudo_available", return_value=True)
def test_uninstall_bot_service(mock_sudo, mock_remove, mock_reload, mock_systemctl, install_tree):
    _, config_path = install_tree
    config = load_config(config_path)
    from ytdigest.systemd_units import uninstall_bot_service

    message = uninstall_bot_service(config)
    assert "bot stopped" in message.lower()
    mock_systemctl.assert_any_call("disable", "--now", "ytdigest-bot", privileged=True)


@patch("ytdigest.systemd_units._schedule_web_service_start")
@patch("ytdigest.systemd_units._systemctl")
@patch("ytdigest.systemd_units._daemon_reload")
@patch("ytdigest.systemd_units._write_unit_file")
@patch("ytdigest.systemd_units.sudo_available", return_value=True)
@patch("ytdigest.systemd_units._port_in_use", return_value=True)
@patch("ytdigest.systemd_units.get_unit_status")
def test_install_web_service_handoff_from_manual(
    mock_status,
    mock_port,
    mock_sudo,
    mock_write,
    mock_reload,
    mock_systemctl,
    mock_schedule,
    install_tree,
):
    _, config_path = install_tree
    config = load_config(config_path)
    mock_status.return_value = UnitStatus(
        name="ytdigest-web.service",
        label="Web UI",
        installed=False,
        enabled=False,
        active=False,
    )
    result = install_web_service(config)
    assert result.handoff is True
    assert "handing off" in result.message.lower()
    mock_schedule.assert_called_once()
    mock_systemctl.assert_any_call("enable", "ytdigest-web", privileged=True)
    assert not any(call.args[:2] == ("start", "ytdigest-web") for call in mock_systemctl.call_args_list)


@patch("ytdigest.systemd_units._systemctl")
@patch("ytdigest.systemd_units._daemon_reload")
@patch("ytdigest.systemd_units._write_unit_file")
@patch("ytdigest.systemd_units.sudo_available", return_value=True)
@patch("ytdigest.systemd_units._port_in_use", return_value=False)
@patch("ytdigest.systemd_units.get_unit_status")
def test_install_web_service_starts_when_port_free(
    mock_status,
    mock_port,
    mock_sudo,
    mock_write,
    mock_reload,
    mock_systemctl,
    install_tree,
):
    _, config_path = install_tree
    config = load_config(config_path)
    mock_status.return_value = UnitStatus(
        name="ytdigest-web.service",
        label="Web UI",
        installed=False,
        enabled=False,
        active=False,
    )
    result = install_web_service(config)
    assert result.handoff is False
    mock_systemctl.assert_any_call("start", "ytdigest-web", privileged=True)


def test_sudo_services_argv_matches_sudoers(install_tree):
    _, config_path = install_tree
    config = load_config(config_path)
    ctx = install_context_from_config(config)
    argv = _sudo_services_argv(ctx, "install-web")
    assert argv[:2] == ["sudo", "-n"]
    assert argv[2] == str(ctx.ytdigest_bin)
    assert argv[3:7] == ["--config", str(ctx.config_path), "services", "install-web"]
    hint = f"{ctx.service_user} ALL=(root) NOPASSWD: {ctx.ytdigest_bin} --config {ctx.config_path} services *"
    assert str(ctx.ytdigest_bin) in hint
    assert "services *" in hint


@patch("ytdigest.systemd_units.os.geteuid", return_value=1000)
@patch("ytdigest.systemd_units._run")
def test_sudo_available_probes_services_status(mock_run, _euid, install_tree):
    _, config_path = install_tree
    config = load_config(config_path)
    ctx = install_context_from_config(config)
    mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")
    assert sudo_available(ctx) is True
    cmd = mock_run.call_args[0][0]
    assert cmd[:2] == ["sudo", "-n"]
    assert cmd[2] == str(ctx.ytdigest_bin)
    assert cmd[-2:] == ["services", "status"]
    assert "true" not in cmd


@patch("ytdigest.systemd_units.os.geteuid", return_value=0)
@patch("ytdigest.systemd_units._run")
def test_sudo_available_true_when_root(mock_run, _euid, install_tree):
    _, config_path = install_tree
    config = load_config(config_path)
    ctx = install_context_from_config(config)
    assert sudo_available(ctx) is True
    mock_run.assert_not_called()


def test_install_web_parses_elevated_handoff(install_tree, monkeypatch):
    _, config_path = install_tree
    config = load_config(config_path)
    proc = subprocess.CompletedProcess(
        [],
        0,
        "Web service installed — handing off to systemd now.\n",
        f"{HANDOFF_MARKER}\n",
    )
    monkeypatch.setattr("ytdigest.systemd_units._maybe_sudo_cli", lambda *a, **k: proc)
    result = install_web_service(config)
    assert result.handoff is True
    assert "handing off" in result.message.lower()


def test_maybe_sudo_cli_runs_allowed_command(install_tree, monkeypatch):
    _, config_path = install_tree
    config = load_config(config_path)
    ctx = install_context_from_config(config)
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

    monkeypatch.setattr("ytdigest.systemd_units.os.geteuid", lambda: 1000)
    monkeypatch.setattr("ytdigest.systemd_units.subprocess.run", fake_run)
    result = _real_maybe_sudo_cli(ctx, "install-bot")
    assert result is not None
    assert result.stdout.strip() == "ok"
    assert captured["cmd"] == _sudo_services_argv(ctx, "install-bot")


def test_maybe_sudo_cli_skips_when_root(install_tree, monkeypatch):
    _, config_path = install_tree
    config = load_config(config_path)
    ctx = install_context_from_config(config)
    monkeypatch.setattr("ytdigest.systemd_units.os.geteuid", lambda: 0)
    assert _real_maybe_sudo_cli(ctx, "install-bot") is None


def test_maybe_sudo_cli_friendly_error_when_denied(install_tree, monkeypatch):
    from ytdigest.systemd_units import SystemdError

    _, config_path = install_tree
    config = load_config(config_path)
    ctx = install_context_from_config(config)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "sudo: a password is required\n")

    monkeypatch.setattr("ytdigest.systemd_units.os.geteuid", lambda: 1000)
    monkeypatch.setattr("ytdigest.systemd_units.subprocess.run", fake_run)
    with pytest.raises(SystemdError, match="sudo is not configured"):
        _real_maybe_sudo_cli(ctx, "install-bot")
