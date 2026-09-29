from __future__ import annotations

import os
import re
import sys
import zipfile
from pathlib import Path


def prepare_tk_runtime(runtime_root: Path) -> None:
    """Подготавливает Tcl/Tk для Python-сборок, где библиотеки лежат в ZIP."""
    if os.environ.get("TCL_LIBRARY") and os.environ.get("TK_LIBRARY"):
        return

    bundled = Path(sys.base_prefix) / "tcl"
    direct_tcl = _find_direct_library(bundled, "tcl", "init.tcl")
    direct_tk = _find_direct_library(bundled, "tk", "tk.tcl")
    if direct_tcl and direct_tk:
        os.environ.setdefault("TCL_LIBRARY", str(direct_tcl))
        os.environ.setdefault("TK_LIBRARY", str(direct_tk))
        return

    tcl_zip = next(iter(sorted(bundled.glob("libtcl*.zip"))), None)
    tk_zip = next(iter(sorted(bundled.glob("libtk*.zip"))), None)
    if not tcl_zip or not tk_zip:
        return

    version = _major_minor_from_name(tcl_zip.name) or "9.0"
    tcl_target = runtime_root / f"tcl{version}"
    tk_target = runtime_root / f"tk{version}"
    if not (tcl_target / "init.tcl").is_file():
        _extract_library(tcl_zip, "tcl_library/", tcl_target)
    if not (tk_target / "tk.tcl").is_file():
        _extract_library(tk_zip, "tk_library/", tk_target)
    os.environ.setdefault("TCL_LIBRARY", str(tcl_target))
    os.environ.setdefault("TK_LIBRARY", str(tk_target))


def _find_direct_library(base: Path, prefix: str, marker: str) -> Path | None:
    for candidate in sorted(base.glob(f"{prefix}[0-9]*"), reverse=True):
        if candidate.is_dir() and (candidate / marker).is_file():
            return candidate
    return None


def _major_minor_from_name(name: str) -> str | None:
    match = re.search(r"(\d+\.\d+)", name)
    return match.group(1) if match else None


def _extract_library(archive: Path, prefix: str, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    target_root = target.resolve()
    with zipfile.ZipFile(archive) as package:
        for item in package.infolist():
            if item.is_dir() or not item.filename.startswith(prefix):
                continue
            relative = Path(item.filename[len(prefix) :])
            destination = (target / relative).resolve()
            if target_root not in destination.parents:
                raise RuntimeError(f"Небезопасный путь в архиве Tcl/Tk: {item.filename}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(package.read(item))

