"""Install and manage ytdigest systemd units (web UI + daily timer)."""
from __future__ import annotations

import os
import pwd
import re
import socket
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from .config import Config, ConfigError, load_config, update_config_file

DEDICATED_INSTALL_DIR = Path("/opt/ytdigest")
UNIT_NAMES = {
    "web": "ytdigest-web.service",
    "bot": "ytdigest-bot.service",
    "run": "ytdigest.service",
    "timer": "ytdigest.timer",
    "retry_run": "ytdigest-retry.service",
    "retry_timer": "ytdigest-retry.timer",
}


class SystemdError(Exception):
    pass


@dataclass(frozen=True)
class InstallContext:
    install_dir: Path
    service_user: str
    config_path: Path
    ytdigest_bin: Path
    setup_mode: str  # "dedicated" | "home"

    @property
    def env_file(self) -> Path:
        return self.install_dir / ".env"


@dataclass
class UnitStatus:
    name: str
    label: str
    installed: bool
    enabled: bool | None
    active: bool | None
    detail: str | None = None


@dataclass
class ServicesSnapshot:
    context: InstallContext
    sudo_available: bool
    digest_hour: int
    timezone: str
    web: UnitStatus
    bot: UnitStatus
    timer: UnitStatus
    next_run: str | None = None
    sudo_hint: str | None = None
    manual_web_running: bool = False


@dataclass(frozen=True)
class InstallWebResult:
    message: str
    handoff: bool = False


def install_context_from_config(config: Config) -> InstallContext:
    if config.config_path is None:
        raise SystemdError("config_path is required for service management")
    config_path = config.config_path.resolve()
    install_dir = config_path.parent
    service_user = pwd.getpwuid(os.getuid()).pw_name
    ytdigest_bin = install_dir / "venv" / "bin" / "ytdigest"
    if not ytdigest_bin.is_file():
        raise SystemdError(f"ytdigest binary not found at {ytdigest_bin}")
    setup_mode = "dedicated" if install_dir == DEDICATED_INSTALL_DIR else "home"
    return InstallContext(
        install_dir=install_dir,
        service_user=service_user,
        config_path=config_path,
        ytdigest_bin=ytdigest_bin,
        setup_mode=setup_mode,
    )


def _systemd_source_dir(install_dir: Path) -> Path:
    bundled = install_dir / "systemd"
    if bundled.is_dir():
        return bundled
    repo = Path(__file__).resolve().parent.parent / "systemd"
    if repo.is_dir():
        return repo
    raise SystemdError("systemd unit templates not found (expected install_dir/systemd/)")


def _patch_unit(template: str, ctx: InstallContext) -> str:
    text = template.replace("/opt/ytdigest", str(ctx.install_dir))
    text = re.sub(r"^User=.*$", f"User={ctx.service_user}", text, flags=re.MULTILINE)
    return text


def _render_timer_on_calendar(template: str, digest_hour: int, timezone: str) -> str:
    line = f"OnCalendar=*-*-* {digest_hour:02d}:00:00 {timezone}"
    return re.sub(r"^OnCalendar=.*$", line, template, flags=re.MULTILINE)


def render_unit(name: str, ctx: InstallContext, *, digest_hour: int, timezone: str) -> str:
    source = _systemd_source_dir(ctx.install_dir) / name
    if not source.is_file():
        raise SystemdError(f"unit template missing: {source}")
    text = _patch_unit(source.read_text(encoding="utf-8"), ctx)
    if name == UNIT_NAMES["timer"]:
        text = _render_timer_on_calendar(text, digest_hour, timezone)
    return text


def _unit_path(name: str) -> Path:
    return Path("/etc/systemd/system") / name


HANDOFF_MARKER = "ytdigest:handoff"


def _run(cmd: list[str], *, privileged: bool = False) -> subprocess.CompletedProcess[str]:
    if privileged and os.geteuid() != 0:
        cmd = ["sudo", "-n", *cmd]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=10)
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired):
        result = subprocess.CompletedProcess(cmd, returncode=127, stdout="", stderr="command unavailable")
        return result


def _sudo_services_argv(
    ctx: InstallContext, action: str, extra_args: tuple[str, ...] = ()
) -> list[str]:
    """Exact argv allowed by systemd/ytdigest-sudoers.example (services *)."""
    return [
        "sudo",
        "-n",
        str(ctx.ytdigest_bin),
        "--config",
        str(ctx.config_path),
        "services",
        action,
        *extra_args,
    ]


def sudo_available(ctx: InstallContext) -> bool:
    if os.geteuid() == 0:
        return True
    result = _run(_sudo_services_argv(ctx, "status"))
    return result.returncode == 0


def _sudo_denied(stderr: str) -> bool:
    text = stderr.lower()
    return (
        "password is required" in text
        or "a terminal is required" in text
        or "not in the sudoers" in text
        or "is not allowed to execute" in text
    )


def _maybe_sudo_cli(
    ctx: InstallContext,
    action: str,
    extra_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str] | None:
    """Re-exec via `sudo ytdigest services …` when not root. None means caller should proceed."""
    if os.geteuid() == 0:
        return None
    cmd = _sudo_services_argv(ctx, action, extra_args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired) as exc:
        raise SystemdError("sudo is not configured — see Settings for setup instructions") from exc
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        if not err or _sudo_denied(err):
            raise SystemdError("sudo is not configured — see Settings for setup instructions")
        raise SystemdError(err)
    return result


def _elevated_message(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout.strip()


def _elevated_handoff(result: subprocess.CompletedProcess[str]) -> bool:
    return HANDOFF_MARKER in result.stderr.splitlines()


def _sudo_hint(ctx: InstallContext) -> str:
    return (
        f"{ctx.service_user} ALL=(root) NOPASSWD: "
        f"{ctx.ytdigest_bin} --config {ctx.config_path} services *"
    )


def _write_unit_file(name: str, content: str, *, privileged: bool) -> None:
    target = _unit_path(name)
    if os.geteuid() == 0:
        target.write_text(content, encoding="utf-8")
        return
    tmp = Path(f"/tmp/ytdigest-unit-{name}")
    tmp.write_text(content, encoding="utf-8")
    result = _run(["sudo", "-n", "cp", str(tmp), str(target)], privileged=False)
    tmp.unlink(missing_ok=True)
    if result.returncode != 0:
        raise SystemdError(result.stderr.strip() or f"failed to install {name}")


def _remove_unit_file(name: str, *, privileged: bool) -> None:
    target = _unit_path(name)
    if not target.exists():
        return
    if os.geteuid() == 0:
        target.unlink()
        return
    result = _run(["sudo", "-n", "rm", "-f", str(target)], privileged=False)
    if result.returncode != 0:
        raise SystemdError(result.stderr.strip() or f"failed to remove {name}")


def _systemctl(*args: str, privileged: bool) -> None:
    result = _run(["systemctl", *args], privileged=privileged)
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip()
        raise SystemdError(msg or f"systemctl {' '.join(args)} failed")


def _systemctl_show(name: str, prop: str) -> str | None:
    result = _run(["systemctl", "show", name, f"-p{prop}", "--value"])
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _parse_bool_prop(value: str | None) -> bool | None:
    if value is None:
        return None
    if value in ("enabled", "active", "running"):
        return True
    if value in ("disabled", "inactive", "dead", "failed"):
        return False
    return None


def get_unit_status(name: str, label: str) -> UnitStatus:
    installed = _unit_path(name).exists()
    enabled_raw = _systemctl_show(name, "UnitFileState")
    active_raw = _systemctl_show(name, "ActiveState")
    enabled = _parse_bool_prop(enabled_raw) if enabled_raw not in (None, "not-found") else None
    active = _parse_bool_prop(active_raw) if active_raw not in (None, "not-found") else None
    detail = None
    if enabled_raw == "not-found" or active_raw == "not-found":
        detail = "not installed"
    elif active_raw == "failed":
        detail = "failed — check journalctl"
    return UnitStatus(
        name=name,
        label=label,
        installed=installed,
        enabled=enabled,
        active=active,
        detail=detail,
    )


def get_next_timer_run() -> str | None:
    result = _run(
        ["systemctl", "list-timers", UNIT_NAMES["timer"], "--no-pager", "--no-legend"]
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    # NEXT LEFT LAST PASSED UNIT ACTIVATES
    parts = result.stdout.split()
    if len(parts) >= 1:
        return parts[0]
    return None


def _port_in_use(port: int) -> bool:
    probe_host = "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((probe_host, port)) == 0


def _manual_web_likely_running(config: Config) -> bool:
    """True when the web port is in use but not by the systemd web unit."""
    web = get_unit_status(UNIT_NAMES["web"], "Web UI")
    if web.active:
        return False
    return _port_in_use(config.web_port)


def _schedule_web_service_start(*, port: int, privileged: bool) -> None:
    """Start ytdigest-web once the web port is free (after manual process exits)."""
    start_cmd = "systemctl start ytdigest-web"
    if privileged:
        start_cmd = f"sudo -n {start_cmd}"
    script = (
        f"for i in $(seq 1 60); do "
        f"if ! ss -tln 2>/dev/null | grep -q ':{port} '; then break; fi; "
        f"sleep 0.2; "
        f"done; "
        f"{start_cmd}"
    )
    subprocess.Popen(
        ["sh", "-c", script],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def schedule_process_exit(*, delay_seconds: float = 1.0) -> None:
    """Exit the current process after a short delay (manual → systemd handoff)."""

    def _exit() -> None:
        import time

        time.sleep(delay_seconds)
        os._exit(0)

    threading.Thread(target=_exit, daemon=True).start()


def get_services_snapshot(config: Config) -> ServicesSnapshot:
    ctx = install_context_from_config(config)
    sudo_ok = sudo_available(ctx)
    web = get_unit_status(UNIT_NAMES["web"], "Web UI")
    bot = get_unit_status(UNIT_NAMES["bot"], "Telegram Q&A bot")
    timer = get_unit_status(UNIT_NAMES["timer"], "Daily run timer")
    next_run = get_next_timer_run() if timer.installed else None
    return ServicesSnapshot(
        context=ctx,
        sudo_available=sudo_ok,
        digest_hour=config.digest_hour,
        timezone=config.timezone,
        web=web,
        bot=bot,
        timer=timer,
        next_run=next_run,
        sudo_hint=None if sudo_ok else _sudo_hint(ctx),
        manual_web_running=_manual_web_likely_running(config),
    )


def _daemon_reload(*, privileged: bool) -> None:
    _systemctl("daemon-reload", privileged=privileged)


def install_web_service(config: Config) -> InstallWebResult:
    ctx = install_context_from_config(config)
    elevated = _maybe_sudo_cli(ctx, "install-web")
    if elevated is not None:
        return InstallWebResult(
            message=_elevated_message(elevated),
            handoff=_elevated_handoff(elevated),
        )
    content = render_unit(
        UNIT_NAMES["web"], ctx, digest_hour=config.digest_hour, timezone=config.timezone
    )
    privileged = os.geteuid() != 0
    if privileged and not sudo_available(ctx):
        raise SystemdError("sudo is not configured — see Settings for setup instructions")

    web = get_unit_status(UNIT_NAMES["web"], "Web UI")
    port_busy = _port_in_use(config.web_port)
    handoff = port_busy and not web.active

    if port_busy and web.active:
        # Re-install while already under systemd — bounce the service.
        _write_unit_file(UNIT_NAMES["web"], content, privileged=privileged)
        _daemon_reload(privileged=privileged)
        _systemctl("restart", "ytdigest-web", privileged=privileged)
        return InstallWebResult(message="Web service updated and restarted")

    if port_busy and not handoff:
        raise SystemdError(
            f"Port {config.web_port} is in use by another process — free it before installing"
        )

    _write_unit_file(UNIT_NAMES["web"], content, privileged=privileged)
    _daemon_reload(privileged=privileged)
    _systemctl("enable", "ytdigest-web", privileged=privileged)

    if handoff:
        _schedule_web_service_start(port=config.web_port, privileged=privileged)
        return InstallWebResult(
            message=(
                "Web service installed — handing off to systemd now. "
                "This page will disconnect briefly; reload in a few seconds."
            ),
            handoff=True,
        )

    _systemctl("start", "ytdigest-web", privileged=privileged)
    return InstallWebResult(message="Web service installed and started")


def uninstall_web_service(config: Config) -> str:
    ctx = install_context_from_config(config)
    elevated = _maybe_sudo_cli(ctx, "uninstall-web")
    if elevated is not None:
        return _elevated_message(elevated)
    privileged = os.geteuid() != 0
    if privileged and not sudo_available(ctx):
        raise SystemdError("sudo is not configured — see Settings for setup instructions")
    _systemctl("disable", "--now", "ytdigest-web", privileged=privileged)
    _remove_unit_file(UNIT_NAMES["web"], privileged=privileged)
    _daemon_reload(privileged=privileged)
    return "Web service stopped and removed"


def restart_web_service(config: Config) -> str:
    ctx = install_context_from_config(config)
    elevated = _maybe_sudo_cli(ctx, "restart-web")
    if elevated is not None:
        return _elevated_message(elevated)
    privileged = os.geteuid() != 0
    if privileged and not sudo_available(ctx):
        raise SystemdError("sudo is not configured — see Settings for setup instructions")
    web = get_unit_status(UNIT_NAMES["web"], "Web UI")
    if not web.installed:
        return "Web service not installed — skipped restart"
    _systemctl("restart", "ytdigest-web", privileged=privileged)
    return "Web service restarted"


def restart_bot_service(config: Config) -> str:
    ctx = install_context_from_config(config)
    elevated = _maybe_sudo_cli(ctx, "restart-bot")
    if elevated is not None:
        return _elevated_message(elevated)
    privileged = os.geteuid() != 0
    if privileged and not sudo_available(ctx):
        raise SystemdError("sudo is not configured — see Settings for setup instructions")
    bot = get_unit_status(UNIT_NAMES["bot"], "Telegram Q&A bot")
    if not bot.installed:
        return "Telegram Q&A bot not installed — skipped restart"
    if not bot.active:
        return "Telegram Q&A bot not running — skipped restart"
    _systemctl("restart", "ytdigest-bot", privileged=privileged)
    return "Telegram Q&A bot restarted"


def install_bot_service(config: Config) -> str:
    ctx = install_context_from_config(config)
    elevated = _maybe_sudo_cli(ctx, "install-bot")
    if elevated is not None:
        return _elevated_message(elevated)
    content = render_unit(
        UNIT_NAMES["bot"], ctx, digest_hour=config.digest_hour, timezone=config.timezone
    )
    privileged = os.geteuid() != 0
    if privileged and not sudo_available(ctx):
        raise SystemdError("sudo is not configured — see Settings for setup instructions")
    _write_unit_file(UNIT_NAMES["bot"], content, privileged=privileged)
    _daemon_reload(privileged=privileged)
    _systemctl("enable", "--now", "ytdigest-bot", privileged=privileged)
    return "Telegram Q&A bot enabled and started"


def uninstall_bot_service(config: Config) -> str:
    ctx = install_context_from_config(config)
    elevated = _maybe_sudo_cli(ctx, "uninstall-bot")
    if elevated is not None:
        return _elevated_message(elevated)
    privileged = os.geteuid() != 0
    if privileged and not sudo_available(ctx):
        raise SystemdError("sudo is not configured — see Settings for setup instructions")
    _systemctl("disable", "--now", "ytdigest-bot", privileged=privileged)
    _remove_unit_file(UNIT_NAMES["bot"], privileged=privileged)
    _daemon_reload(privileged=privileged)
    return "Telegram Q&A bot stopped and disabled"


def install_timer_service(config: Config) -> str:
    ctx = install_context_from_config(config)
    elevated = _maybe_sudo_cli(ctx, "install-timer")
    if elevated is not None:
        return _elevated_message(elevated)
    privileged = os.geteuid() != 0
    if privileged and not sudo_available(ctx):
        raise SystemdError("sudo is not configured — see Settings for setup instructions")
    run_content = render_unit(
        UNIT_NAMES["run"], ctx, digest_hour=config.digest_hour, timezone=config.timezone
    )
    timer_content = render_unit(
        UNIT_NAMES["timer"], ctx, digest_hour=config.digest_hour, timezone=config.timezone
    )
    retry_run_content = render_unit(
        UNIT_NAMES["retry_run"], ctx, digest_hour=config.digest_hour, timezone=config.timezone
    )
    retry_timer_content = render_unit(
        UNIT_NAMES["retry_timer"], ctx, digest_hour=config.digest_hour, timezone=config.timezone
    )
    _write_unit_file(UNIT_NAMES["run"], run_content, privileged=privileged)
    _write_unit_file(UNIT_NAMES["timer"], timer_content, privileged=privileged)
    _write_unit_file(UNIT_NAMES["retry_run"], retry_run_content, privileged=privileged)
    _write_unit_file(UNIT_NAMES["retry_timer"], retry_timer_content, privileged=privileged)
    _daemon_reload(privileged=privileged)
    _systemctl("enable", "--now", "ytdigest.timer", privileged=privileged)
    _systemctl("enable", "--now", "ytdigest-retry.timer", privileged=privileged)
    return f"Daily run timer installed ({config.digest_hour:02d}:00 {config.timezone})"


def uninstall_timer_service(config: Config) -> str:
    ctx = install_context_from_config(config)
    elevated = _maybe_sudo_cli(ctx, "uninstall-timer")
    if elevated is not None:
        return _elevated_message(elevated)
    privileged = os.geteuid() != 0
    if privileged and not sudo_available(ctx):
        raise SystemdError("sudo is not configured — see Settings for setup instructions")
    _systemctl("disable", "--now", "ytdigest.timer", privileged=privileged)
    if _unit_path(UNIT_NAMES["retry_timer"]).exists():
        _systemctl("disable", "--now", "ytdigest-retry.timer", privileged=privileged)
    _remove_unit_file(UNIT_NAMES["timer"], privileged=privileged)
    _remove_unit_file(UNIT_NAMES["run"], privileged=privileged)
    _remove_unit_file(UNIT_NAMES["retry_timer"], privileged=privileged)
    _remove_unit_file(UNIT_NAMES["retry_run"], privileged=privileged)
    _daemon_reload(privileged=privileged)
    return "Daily run timer removed"


def update_run_schedule(config: Config, *, digest_hour: int, timezone: str) -> str:
    if not (0 <= digest_hour <= 23):
        raise ConfigError("digest_hour must be 0-23")
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(timezone)
    except Exception as exc:
        raise ConfigError(f"invalid timezone: {timezone}") from exc

    if config.config_path is None:
        raise SystemdError("config_path is required")
    update_config_file(
        config.config_path,
        {"digest_hour": digest_hour, "timezone": timezone},
    )
    reloaded = load_config(config.config_path)
    config.values.update(reloaded.values)

    timer = get_unit_status(UNIT_NAMES["timer"], "Daily run timer")
    if timer.installed:
        install_timer_service(config)
        return f"Schedule saved and timer updated to {digest_hour:02d}:00 {timezone}"
    return f"Schedule saved ({digest_hour:02d}:00 {timezone}) — install the timer to activate"
