"""Fake client venvs: ``<n>/bin/python`` is a symlink to this interpreter's
base Python, and ``fake_client`` comes from PYTHONPATH."""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

import pytest

from tgzr.edge.home_manager import ClientChain, ClientSpec, Home

FAKE_SRC = Path(__file__).parent / "fake_client_src"
ORG = "https://org.test"


def fake_venv(chain: ClientChain, n: int | str, behavior: str = "ready") -> Path:
    path = chain.path / str(n)
    (path / ".venv" / "bin").mkdir(parents=True)
    (path / ".venv" / "bin" / "python").symlink_to(os.path.realpath(sys.executable))
    (path / "behavior").write_text(behavior)
    return path


class Clients:
    """Started fake clients, killed at the end of the test."""

    def __init__(self) -> None:
        self.pids: list[int] = []

    def add(self, pid: int) -> int:
        self.pids.append(pid)
        return pid

    def kill_all(self) -> None:
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass


@pytest.fixture
def home(tmp_path, monkeypatch) -> Home:
    if sys.platform == "win32":  # pragma: no cover
        pytest.skip("the fake client uses flock")
    monkeypatch.setenv("PYTHONPATH", str(FAKE_SRC))
    for k in list(os.environ):
        if k.startswith("TGZR_"):
            monkeypatch.delenv(k)
    return Home.create(tmp_path / "home")


@pytest.fixture
def chain(home) -> ClientChain:
    c = home.add_org(ORG).client("fake_client")
    c.save_spec(ClientSpec("fake_client"))
    return c


@pytest.fixture
def clients():
    c = Clients()
    yield c
    c.kill_all()


@pytest.fixture(autouse=True)
def _no_org_info(monkeypatch):
    """New orgs are not asked for their name (no network in tests)."""
    from tgzr.edge.home_manager import OrgUnreachable

    from tgzr.edge.launcher import core

    def unreachable(url, timeout=0.0):
        raise OrgUnreachable(f"{url}: no network in tests")

    monkeypatch.setattr(core, "fetch_org_info", unreachable)
