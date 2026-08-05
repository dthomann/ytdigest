"""Persist one-shot sync results across OAuth redirect."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ...config import Config


@dataclass
class SyncFlash:
    connected: bool = False
    added_count: int = 0
    added_titles: list[str] = field(default_factory=list)
    suggested_removals: list[dict] = field(default_factory=list)
    unchanged_disabled_count: int = 0
    error: str | None = None


def _flash_path(config: Config) -> Path:
    return config.data_dir / ".sync_flash.json"


def save_sync_flash(config: Config, flash: SyncFlash) -> None:
    path = _flash_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(flash)), encoding="utf-8")


def load_and_clear_sync_flash(config: Config) -> SyncFlash | None:
    path = _flash_path(config)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        flash = SyncFlash(**data)
    except (json.JSONDecodeError, TypeError):
        flash = None
    path.unlink(missing_ok=True)
    return flash
