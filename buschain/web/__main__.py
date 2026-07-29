"""Launcher for `python -m buschain.web` and for `start-buschain.bat`.

None of this belongs in `create_app()`: the factory is what the tests and any
embedding use, and it must never open a browser or probe sockets. The launcher
is the only place that knows a human is watching.
"""
from __future__ import annotations

import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from .app import create_app

HOST = "127.0.0.1"
PORT = 8000


def _url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def _is_serving(host: str, port: int) -> bool:
    """True if something already answers on the port.

    A connect probe rather than a trial bind: the question is 'is buschain
    already up', and connect_ex answers it without dragging in the
    SO_REUSEADDR / lingering-socket subtleties of binding a port we then have
    to release before uvicorn can take it.
    """
    with socket.socket() as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) == 0


def _open_when_ready(host: str, port: int, timeout: float = 15.0) -> None:
    """Wait for the server to accept connections, then open the browser.

    Opening immediately would land the tab on a connection error: uvicorn binds
    a few hundred ms after this thread starts, and considerably later on a
    first run because `create_app()` opens the SQLite connection and runs the
    migrations *before* the server binds. On timeout we give up silently — the
    URL was printed either way, and a browser that won't launch must not take
    the server down with it.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_serving(host, port):
            webbrowser.open(_url(host, port))
            return
        time.sleep(0.2)


def main() -> int:
    url = _url(HOST, PORT)

    if _is_serving(HOST, PORT):
        # Almost always a second double-click. Reuse it: the alternative is an
        # 'address already in use' traceback in a window that then closes.
        print(f"Something is already serving {url} — opening it.")
        print("If that isn't buschain, stop it and start this again.")
        webbrowser.open(url)
        return 0

    print(f"buschain is starting at {url}  (close this window to stop it)")
    # --no-browser has a present caller: the headless-Chrome UI checks, where a
    # real browser window popping up is noise.
    if "--no-browser" not in sys.argv[1:]:
        # Daemon, so closing the window or Ctrl-C is never held up by the wait.
        threading.Thread(target=_open_when_ready, args=(HOST, PORT), daemon=True).start()

    # The gap between the check above and this bind is a race we accept —
    # losing it just reproduces uvicorn's own error, which is what happened
    # before this launcher existed.
    uvicorn.run(create_app(), host=HOST, port=PORT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
