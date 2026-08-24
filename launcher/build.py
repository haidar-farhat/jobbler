"""Build LocalApply.exe.

    services\\api\\.venv\\Scripts\\python.exe launcher\\build.py

Produces a single self-contained exe at the repo root. It bundles only the launcher --
roughly 8 MB of Python runtime -- not the application, because the launcher shells out to
the project virtualenv. That keeps the build fast and means editing the app never requires
rebuilding the exe.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "launcher"
NAME = "LocalApply"


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("installing pyinstaller...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "pyinstaller", "-q"]
        )


def main() -> int:
    ensure_pyinstaller()

    build_dir = LAUNCHER / "build"
    spec_dir = LAUNCHER / "spec"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--console",
        "--name", NAME,
        "--distpath", str(ROOT),
        "--workpath", str(build_dir),
        "--specpath", str(spec_dir),
        "--noconfirm",
        # Nothing from the app is imported, so keep the bundle minimal.
        "--exclude-module", "tkinter",
        "--exclude-module", "numpy",
        "--exclude-module", "PIL",
        str(LAUNCHER / "launcher.py"),
    ]
    icon = LAUNCHER / "icon.ico"
    if icon.is_file():
        cmd[cmd.index("--noconfirm")] = "--noconfirm"
        cmd += ["--icon", str(icon)]

    print(" ".join(cmd), "\n")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        return result.returncode

    for path in (build_dir, spec_dir):
        shutil.rmtree(path, ignore_errors=True)

    exe = ROOT / f"{NAME}.exe"
    if exe.is_file():
        print(f"\nBuilt {exe}  ({exe.stat().st_size / 1_048_576:.1f} MB)")
        print("Double-click it, or run it from a terminal for the startup log.")
        return 0

    print("\nBuild reported success but no exe was produced.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
