from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
        override = os.environ.get("ROSA_MAIL_DATA_DIR")
        if override:
            root = Path(override).expanduser().resolve()
        else:
            local_app_data = os.environ.get("LOCALAPPDATA")
            base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
            root = base / "RosaMailCollector"
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

