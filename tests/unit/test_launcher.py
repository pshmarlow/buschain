"""Pins for the double-click launcher (`start-buschain.bat` -> `main()`).
Each test names the bad UX it forbids: a browser tab on a connection error, a
hung wait when the server never comes up, and an 'address already in use'
traceback in a window that closes before it can be read."""
import re
import socket
import tomllib
from pathlib import Path

import pytest

from buschain.web import __main__ as launcher

ROOT = Path(__file__).resolve().parents[2]
BAT = (ROOT / "start-buschain.bat").read_text(encoding="utf-8")
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


@pytest.fixture
def listening():
    """A real bound, listening socket on an ephemeral port. Real sockets, not
    a mocked _is_serving: the point of these tests is the socket probe."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    yield sock.getsockname()
    sock.close()


@pytest.fixture
def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def opened(monkeypatch):
    """Records webbrowser.open calls instead of launching a browser."""
    calls = []
    monkeypatch.setattr(launcher.webbrowser, "open", calls.append)
    return calls


def test_browser_opens_only_once_the_port_accepts(listening, opened):
    host, port = listening
    launcher._open_when_ready(host, port, timeout=5.0)
    assert opened == [f"http://{host}:{port}"]


def test_browser_is_not_opened_when_nothing_ever_listens(free_port, opened):
    # A server that never comes up must not leave a tab on a connection error.
    launcher._open_when_ready("127.0.0.1", free_port, timeout=0.5)
    assert opened == []


def test_second_launch_reuses_the_running_server_instead_of_binding(
    listening, opened, monkeypatch, capsys
):
    host, port = listening
    monkeypatch.setattr(launcher, "HOST", host)
    monkeypatch.setattr(launcher, "PORT", port)
    monkeypatch.setattr(
        launcher.uvicorn, "run", lambda *a, **k: pytest.fail("must not start a second server")
    )

    assert launcher.main() == 0
    assert opened == [f"http://{host}:{port}"]
    # It has to say so: the port could be occupied by something that isn't us.
    assert "already serving" in capsys.readouterr().out


# The .bat also bootstraps its own venv, and it duplicates three facts that
# live in pyproject.toml. These pin the text, not the behavior — a batch file
# cannot be meaningfully unit-tested without running it — but each duplicate is
# one an ordinary edit can desync, on machines nobody is watching.


def test_bat_installs_an_extra_that_exists():
    """A renamed extra would fail at install time, on the dispatcher's PC, on
    the one run that is supposed to need no help."""
    extra = re.search(r'pip install -e "\.\[([^\]]+)\]"', BAT)
    assert extra, "the .bat no longer installs the project"
    assert extra.group(1) in PYPROJECT["project"]["optional-dependencies"]


def test_bat_probes_only_declared_runtime_deps():
    """The probe decides whether to reinstall. A module that is not a declared
    dependency can never import, so it would reinstall on every launch."""
    probe = re.search(r'python\.exe -c "import ([^";]+)"', BAT)
    assert probe, "the .bat no longer probes its dependencies"
    deps = PYPROJECT["project"]["dependencies"]
    declared = {re.split(r"[<>=!~\[ ]", d, maxsplit=1)[0] for d in deps}
    assert {m.strip() for m in probe.group(1).split(",")} <= declared


def test_bat_pins_its_directory_before_touching_the_venv():
    """DEFAULT_DB is relative: running from anywhere else silently opens a
    second, empty database instead of failing."""
    assert BAT.index('pushd "%~dp0"') < BAT.index(".venv")
