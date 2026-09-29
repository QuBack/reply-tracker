from __future__ import annotations

import logging
import sys

from automation.paths import AppPaths
from automation.tk_runtime import prepare_tk_runtime


def configure_logging(paths: AppPaths) -> None:
    paths.ensure()
    logging.basicConfig(
        filename=paths.logs / "application.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        encoding="utf-8",
    )


def main() -> int:
    paths = AppPaths.discover()
    configure_logging(paths)
    prepare_tk_runtime(paths.root / "runtime" / "tk")
    try:
        import tkinter as tk
        from tkinter import messagebox

        from automation.credentials import WindowsCredentialStore
        from automation.database import Database
        from automation.service import AppService
        from automation.ui import AutomationApp

        database = Database(paths.database)
        credentials = WindowsCredentialStore()
        service = AppService(paths, database, credentials)
        app = AutomationApp(service, database)
        app.mainloop()
        return 0
    except Exception as exc:
        logging.exception("Не удалось запустить приложение")
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "Ошибка запуска",
                f"Не удалось запустить приложение:\n\n{exc}\n\nЖурнал: {paths.logs / 'application.log'}",
            )
            root.destroy()
        except Exception:
            print(f"Не удалось запустить приложение: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
