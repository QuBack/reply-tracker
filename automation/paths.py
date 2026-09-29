from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DATA_DIR_NAME = "AutomationSystem"
# Папка прежних версий. База хранит абсолютные пути к письмам и вложениям,
# поэтому существующую папку не переименовываем, а продолжаем использовать.
LEGACY_DATA_DIR_NAME = "RosaMailCollector"
LEGACY_DATA_DIR_ENV = "ROSA_MAIL_DATA_DIR"


def _adopt_legacy_folder(legacy: Path, current: Path) -> Path:
    if not current.exists() and legacy.is_dir():
        return legacy
    return current


@dataclass(frozen=True)
class AppPaths:
    root: Path
    database: Path
    campaigns: Path
    unmatched: Path
    backups: Path
    logs: Path

    @classmethod
    def discover(cls) -> "AppPaths":
        override = os.environ.get("AUTOMATION_DATA_DIR") or os.environ.get(LEGACY_DATA_DIR_ENV)
        if override:
            root = Path(override).expanduser().resolve()
        else:
            local_app_data = os.environ.get("LOCALAPPDATA")
            base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
            root = _adopt_legacy_folder(base / LEGACY_DATA_DIR_NAME, base / DATA_DIR_NAME)
        return cls.from_root(root)

    @classmethod
    def from_root(cls, root: Path) -> "AppPaths":
        root = root.resolve()
        return cls(
            root=root,
            database=root / "app.db",
            campaigns=root / "campaigns",
            unmatched=root / "unmatched",
            backups=root / "backups",
            logs=root / "logs",
        )

    def ensure(self) -> None:
        for directory in (self.root, self.campaigns, self.unmatched, self.backups, self.logs):
            directory.mkdir(parents=True, exist_ok=True)

