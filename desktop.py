"""Launch the staff UI in a native desktop window.

Starts the FastAPI server on a free localhost port in a background thread, then opens
a pywebview window pointed at it. Staff double-click the Desktop icon and get an
application - no browser, no URL bar, no "which tab was it".

    python desktop.py          with a console, for debugging
    pythonw desktop.py         no console - what the Desktop shortcut runs

Anything written to stdout/stderr goes to desktop.log, because under pythonw there
is no console to print to and a silent exit is impossible to diagnose otherwise.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "desktop.log"

# --------------------------------------------------------------------------
# This block must run BEFORE importing anything that writes to stderr.
#
# pythonw.exe gives the process no console, so sys.stdout and sys.stderr are
# None. Any library that writes to them - uvicorn's logger, our own config
# warning - raises AttributeError on None.write() and the process dies with no
# window and no message. Redirecting to a log file fixes both the crash and the
# "it just doesn't start and I can't tell why" problem.
# --------------------------------------------------------------------------
if sys.stderr is None or sys.stdout is None:
    _log = open(LOG, "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

import socket          # noqa: E402
import threading       # noqa: E402
import time            # noqa: E402

import uvicorn         # noqa: E402

HOST = "127.0.0.1"


def free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def serve(port: int) -> None:
    from app.main import app
    uvicorn.run(app, host=HOST, port=port, log_level="warning")


def wait_until_up(port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((HOST, port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def _practice_name() -> str:
    """What goes in the title bar - the practice's own name, not the app's.

    Read from the same config the pages use, so renaming the practice renames
    the window too. Falls back to a plain word if the config cannot be read,
    because a missing config should not stop the window opening.
    """
    try:
        from app.config import load
        details = load()
        return details.get("short_name") or details.get("name") or "Practice"
    except Exception:                                   # noqa: BLE001
        return "Practice"


def main() -> None:
    os.chdir(HERE)          # so a shortcut with the wrong working dir still works
    sys.path.insert(0, str(HERE))

    port = free_port()
    threading.Thread(target=serve, args=(port,), daemon=True).start()
    url = f"http://{HOST}:{port}/"

    if not wait_until_up(port):
        print("Server did not start within 30s. See the traceback above.", file=sys.stderr)
        sys.exit(1)

    try:
        import webview
    except ImportError:
        import webbrowser
        print("pywebview not installed - opening in the default browser instead.")
        print("  pip install pywebview     for the desktop window")
        print(f"  {url}")
        webbrowser.open(url)
        threading.Event().wait()     # keep the daemon server thread alive
        return

    icon = HERE / "app" / "static" / "icon.ico"
    window_args = dict(width=1440, height=900, min_size=(1024, 680))
    webview.create_window(_practice_name(), url, **window_args)
    try:
        webview.start(icon=str(icon) if icon.exists() else None)
    except TypeError:
        webview.start()              # older pywebview has no icon kwarg


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Without this, a crash under pythonw leaves no trace at all.
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        raise
