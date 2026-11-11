from __future__ import annotations

import argparse
import getpass
import logging
import os
from pathlib import Path

from .app import create_app
from .store import Store
from .config import load_environment


def acquire_server_lock(root: Path):
    stream = (root / "server.lock").open("a+b")
    if stream.tell() == 0:
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise RuntimeError("Сервер с этой папкой данных уже запущен") from None
    return stream


def main():
    load_environment()
    parser = argparse.ArgumentParser(description="Снабжение: веб-приложение")
    parser.add_argument("command", choices=["serve", "create-user", "reset-password", "import-desktop", "seed-demo"])
    parser.add_argument("--email")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if args.command == "seed-demo":
        from .demo_data import seed_demo
        root = Path(os.environ.get("WEB_DATA_DIR", str(Path(__file__).resolve().parents[1] / ".dev-data" / "web")))
        try:
            result = seed_demo(Store(root), args.email or "demo@example.test")
        except ValueError as exc:
            parser.error(str(exc))
        print(result)
        return
    if args.command == "import-desktop":
        if not args.email or not args.source:
            parser.error("Укажите --email и --source. Сервер и настольная программа должны быть остановлены.")
        from .migration import import_desktop
        root = Path(os.environ.get("WEB_DATA_DIR", str(Path(__file__).resolve().parents[1] / ".dev-data" / "web")))
        try:
            import_desktop(Store(root), args.email, args.source)
        except ValueError as exc:
            parser.error(str(exc))
        print("Данные импортированы. Подключите почту заново в настройках веб-приложения.")
        return
    if args.command != "serve":
        if not args.email:
            parser.error("Укажите --email")
        password = getpass.getpass("Пароль (не менее 12 символов): ")
        if password != getpass.getpass("Повторите пароль: "):
            parser.error("Пароли не совпадают")
        root = Path(os.environ.get("WEB_DATA_DIR", str(Path(__file__).resolve().parents[1] / ".dev-data" / "web")))
        store = Store(root)
        try:
            if args.command == "create-user":
                store.create_user(args.email, password)
            else:
                store.reset_password(args.email, password)
        except ValueError as exc:
            parser.error(str(exc))
        print("Аккаунт создан." if args.command == "create-user" else "Пароль изменён. Прежние сессии отозваны.")
        return
    from waitress import serve
    app = create_app()
    runtime = app.extensions["runtime"]
    try:
        server_lock = acquire_server_lock(runtime.store.root)
    except RuntimeError as exc:
        parser.error(str(exc))
    logs = runtime.store.root / "logs"
    logs.mkdir(exist_ok=True)
    logging.basicConfig(filename=logs / "server.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    runtime.start()
    print(f"Снабжение: http://{args.host}:{args.port}", flush=True)
    try:
        proxy = os.environ.get("WEB_TRUSTED_PROXY")
        proxy_options = {"trusted_proxy": proxy, "trusted_proxy_headers": {"x-forwarded-for", "x-forwarded-proto"}} if proxy else {}
        serve(app, host=args.host, port=args.port, threads=8,
              max_request_body_size=app.config["MAX_CONTENT_LENGTH"], **proxy_options)
    finally:
        runtime.close()
        server_lock.close()


if __name__ == "__main__":
    main()
