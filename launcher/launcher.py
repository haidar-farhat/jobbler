"""LocalApply launcher.

One double-click brings the whole stack up: containers, schema, API, dashboard, browser.
Ctrl+C (or closing the window) takes the API back down cleanly.

Built to `LocalApply.exe` by `build.py`. The exe is only an orchestrator -- it shells out to
the project's own virtualenv rather than bundling the application, so Playwright's browsers,
the models, and the source stay where they are and an app update needs no rebuild.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

# --------------------------------------------------------------------------------------
# Console output. Windows Terminal and PowerShell handle ANSI; plain conhost may not, so
# colour is disabled when the stream is not a tty.
# --------------------------------------------------------------------------------------

_COLOUR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOUR else text


def dim(t: str) -> str:
    return _c("2", t)


def cyan(t: str) -> str:
    return _c("36", t)


def green(t: str) -> str:
    return _c("32", t)


def red(t: str) -> str:
    return _c("31", t)


def yellow(t: str) -> str:
    return _c("33", t)


def step(n: int, total: int, label: str) -> None:
    print(f"\n{cyan(f'[{n}/{total}]')} {label}")


def ok(msg: str) -> None:
    print(f"      {green('OK')}  {msg}")


def warn(msg: str) -> None:
    print(f"      {yellow('!')}   {msg}")


def die(msg: str, hint: str = "") -> None:
    print(f"\n{red('FAILED')}  {msg}")
    if hint:
        print(f"        {hint}")
    print("\nPress Enter to close...")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass
    sys.exit(1)


# --------------------------------------------------------------------------------------
# Locating the project
# --------------------------------------------------------------------------------------


def find_repo_root() -> Path:
    """Locate the repo whether running as a script or as a frozen exe.

    Checks the launcher's own directory and its parents, so the exe works from the repo
    root or from launcher/ or dist/.
    """
    base = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent
    )
    for candidate in [base, *base.parents][:5]:
        if (candidate / "services" / "api" / "localapply" / "main.py").is_file():
            return candidate
    die(
        "Could not find the LocalApply project.",
        f"Looked upward from {base}. Keep LocalApply.exe inside the repo.",
    )
    raise SystemExit(1)  # unreachable; keeps type checkers happy


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 300) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,  # callers inspect the return code themselves
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]} timed out after {timeout}s"


def http_json(url: str, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None


# --------------------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------------------


def ensure_docker() -> None:
    code, out = run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=60)
    if code == 127:
        die("Docker was not found on PATH.", "Install Docker Desktop, then run this again.")
    if code != 0:
        die(
            "Docker is installed but the engine is not running.",
            "Start Docker Desktop, wait for the whale icon to settle, then run this again.",
        )
    ok(f"Docker engine {out.strip().splitlines()[0] if out.strip() else 'ready'}")


def ensure_containers(root: Path) -> None:
    compose = root / "infrastructure" / "docker" / "docker-compose.yml"
    code, out = run(["docker", "compose", "-f", str(compose), "up", "-d"], timeout=300)
    if code != 0:
        die("Could not start Postgres and Redis.", out.strip()[-500:])

    for _ in range(90):
        code, state = run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", "localapply-postgres"],
            timeout=20,
        )
        if state.strip() == "healthy":
            ok("Postgres and Redis healthy")
            return
        time.sleep(1)
    die("Postgres did not become healthy within 90s.", "Check: docker logs localapply-postgres")


def ensure_python(root: Path) -> Path:
    python = root / "services" / "api" / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        python = root / "services" / "api" / ".venv" / "bin" / "python"
    if not python.is_file():
        die(
            "No virtualenv found.",
            "Run this once from the repo root:  .\\dev.ps1 setup",
        )
    ok(f"Interpreter {dim(str(python))}")
    return python


def ensure_schema(root: Path, python: Path) -> None:
    api = root / "services" / "api"
    versions = api / "migrations" / "versions"
    if not any(versions.glob("*.py")):
        warn("No migration yet; generating the initial revision")
        code, out = run(
            [str(python), "-m", "alembic", "revision", "--autogenerate", "-m", "initial schema"],
            cwd=api,
            timeout=300,
        )
        if code != 0:
            die("Could not generate the initial migration.", out.strip()[-500:])

    code, out = run([str(python), "-m", "alembic", "upgrade", "head"], cwd=api, timeout=300)
    if code != 0:
        die("Database migration failed.", out.strip()[-500:])
    ok("Schema up to date")


def ensure_profile(root: Path, python: Path) -> None:
    api = root / "services" / "api"
    code, out = run([str(python), "scripts/dev_bootstrap.py"], cwd=api, timeout=180)
    if code != 0:
        warn("Could not seed the profile; the dashboard will tell you what is missing")
        return
    first = next((line for line in out.splitlines() if line.strip()), "")
    ok(first or "Profile ready")


def report_subsystems(health: dict) -> None:
    """Say what the app can actually do, from its own health report.

    The launcher used to announce "Ready" whatever state things were in, so a missing model
    or an unusable browser only showed up as a failed run later.
    """
    subsystems = health.get("subsystems", {})

    browser = subsystems.get("browser", {})
    if not browser.get("ok", True):
        warn(f"Browser unavailable: {browser.get('error', 'unknown reason')}")
    elif not browser.get("headless", True):
        print(f"      {dim('A Chromium window will open when a run starts.')}")

    ai = subsystems.get("ai", {})
    reasoner = ai.get("reasoner", "stub")
    if reasoner == "stub":
        print(
            f"      {dim('Reasoner: scripted (deterministic). Set LA_REASONER=ollama '
                         'for the local model.')}"
        )
        return

    if not ai.get("ok", True):
        warn(ai.get("error", "The AI engine is not reachable."))
        warn("Runs will pause and ask for help. Set LA_REASONER=stub to run without it.")
        return

    resident = ", ".join(ai.get("resident") or []) or "none loaded yet"
    used, budget = ai.get("vram_used_mb"), ai.get("vram_budget_mb")
    vram = f", {used}/{budget} MB VRAM" if used is not None else ""
    ok(f"Local model: {resident}{vram}")


def check_ai_engine(root: Path) -> None:
    """Warn early when the configured model backend is not actually there.

    Only advisory: the app starts either way and degrades to asking you for help, which is
    better than refusing to boot over an optional component.
    """
    reasoner = os.environ.get("LA_REASONER", "").strip().lower()
    if not reasoner:
        env_file = root / ".env"
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.strip().startswith("LA_REASONER="):
                    reasoner = line.split("=", 1)[1].strip().lower()
                    break

    if reasoner != "ollama":
        ok("Reasoner: scripted (no model needed)")
        return

    base = os.environ.get("LA_OLLAMA_BASE_URL", "http://localhost:11434")
    tags = http_json(f"{base}/api/tags", timeout=4.0)
    if tags is None:
        warn(f"LA_REASONER=ollama but nothing is answering at {base}.")
        warn("Start Ollama, or set LA_REASONER=stub. Starting anyway.")
        return

    models = [m.get("name", "") for m in tags.get("models", [])]
    if not models:
        warn("Ollama is running but has no models. Import one, or set LA_REASONER=stub.")
        return
    ok(f"Ollama ready: {', '.join(models[:3])}")


def start_api(root: Path, python: Path, port: int) -> subprocess.Popen:
    api = root / "services" / "api"
    if http_json(f"http://127.0.0.1:{port}/health") is not None:
        die(
            f"Something is already serving on port {port}.",
            "Close it, or start this launcher with --port <other>.",
        )

    log_path = root / "var" / "launcher-api.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 - lives as long as the process

    proc = subprocess.Popen(
        # Never --reload. On Windows uvicorn's reload mode runs on a SelectorEventLoop,
        # which cannot spawn subprocesses, so Playwright cannot start its driver and every
        # run dies with a bare NotImplementedError.
        [str(python), "-m", "uvicorn", "localapply.main:app", "--port", str(port)],
        cwd=str(api),
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )

    for _ in range(60):
        if proc.poll() is not None:
            die("The API exited during startup.", f"See {log_path}")

        health = http_json(f"http://127.0.0.1:{port}/health")
        if health is not None:
            mode = "DRY RUN" if health["safety"]["dry_run"] else "LIVE - SUBMITS ARE REAL"
            ok(f"API listening on port {port}  [{mode}]")
            report_subsystems(health)
            if not health["safety"]["dry_run"]:
                warn("DRY_RUN is off. Approving a submit will send a real application.")
            return proc
        time.sleep(1)

    proc.terminate()
    die("The API did not answer /health within 60s.", f"See {log_path}")
    raise SystemExit(1)


def shutdown(proc: subprocess.Popen, port: int) -> None:
    print(f"\n{cyan('Stopping')} the API...")
    # Ask the app to stop all automation first, so any in-flight browser session is closed
    # and no run is left half-way through a form.
    with contextlib.suppress(Exception):
        # Best effort: we terminate the process either way, but a clean stop closes any
        # in-flight browser session instead of leaving a run half-way through a form.
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/agent/kill-switch",
            data=json.dumps({"reason": "Launcher shutting down"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(request, timeout=5).read()

    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    print(f"      {green('OK')}  API stopped")
    print(dim("      Postgres and Redis are still running, so the next start is quick."))
    print(dim("      Stop them with:  docker compose -f infrastructure/docker/"
              "docker-compose.yml down"))


# --------------------------------------------------------------------------------------


def main() -> int:
    # Progress must appear as it happens, not in one burst at exit. Python block-buffers
    # stdout whenever it is not a terminal (piped to a file, launched from a shortcut), which
    # would leave a user staring at an empty window through a 60s startup.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(prog="LocalApply", description="Start LocalApply.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser.")
    parser.add_argument("--no-seed", action="store_true", help="Skip the profile seed check.")
    args = parser.parse_args()

    print(cyan("\n  LocalApply") + dim("  -  local-first job-application workstation\n"))

    root = find_repo_root()
    print(dim(f"  {root}"))

    total = 5 if args.no_seed else 6
    n = 1

    step(n, total, "Docker")
    ensure_docker()
    n += 1

    step(n, total, "Postgres + Redis")
    ensure_containers(root)
    n += 1

    step(n, total, "Python environment")
    python = ensure_python(root)
    n += 1

    step(n, total, "Database schema")
    ensure_schema(root, python)
    n += 1

    if not args.no_seed:
        step(n, total, "Profile")
        ensure_profile(root, python)
        n += 1

    step(n, total, "API + dashboard")
    proc = start_api(root, python, args.port)

    url = f"http://127.0.0.1:{args.port}/"
    print(f"\n  {green('Ready')}  {url}\n")
    if not args.no_browser:
        webbrowser.open(url)

    print(dim("  Press Ctrl+C to stop.\n"))

    # Idle here until interrupted, but notice if the API dies on its own.
    try:
        while True:
            if proc.poll() is not None:
                print(red("\n  The API exited unexpectedly."))
                print(dim(f"  See {root / 'var' / 'launcher-api.log'}"))
                return 1
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if proc.poll() is None:
            shutdown(proc, args.port)

    return 0


if __name__ == "__main__":
    # Ctrl+C should reach us, not the child, so shutdown stays orderly.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    sys.exit(main())
