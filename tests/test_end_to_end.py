"""The real launcher and the real client, in venvs built by uv from the
editable sources of this workspace (slow; needs uv and PyPI or uv's cache)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tgzr.edge.home_manager import Home, pid_alive

pytestmark = pytest.mark.slow

PACKAGES = Path(__file__).resolve().parents[2]
EDITABLE = [PACKAGES / n for n in ("tgzr.edge.nice_client", "tgzr.edge.home_manager", "tgzr.libs.package_management",
                                   "tgzr.orgs.connect", "tgzr.orgs.client")]
ORG = "https://org.invalid"


def _launcher(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("TGZR_") and k != "VIRTUAL_ENV"}
    env["TGZR_CLIENT_NO_BROWSER"] = "1"
    return subprocess.run([sys.executable, "-m", "tgzr.edge.launcher", "--headless", "--home-dir", str(home), *args],
                          capture_output=True, text=True, timeout=900, env=env)


def _stop(pid: int) -> None:
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 20
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)


def test_build_launch_switch(tmp_path):
    home_dir = tmp_path / "home"
    editable = [a for p in EDITABLE for a in ("--editable", str(p))]
    first = _launcher(home_dir, "--create-home", "--org", ORG, "--client", "tgzr.edge.nice_client", *editable,
                      "--ready-timeout", "120")
    assert first.returncode == 0, first.stderr
    assert "building venv 1" in first.stderr
    home = Home.open(home_dir)
    chain = home.org(ORG).client("tgzr.edge.nice_client")
    info = chain.read_instance()
    assert info is not None and info.n == 1 and chain.current_number() == 1 and chain.is_running()
    assert info.url and "?launch=" in info.url  # TGZR_CLIENT_NO_BROWSER: the real client hands its link over
    pids = [info.pid]
    try:
        again = _launcher(home_dir, "--org", ORG)
        assert again.returncode == 3 and "already running" in again.stderr

        # What the client's Update does: build the next venv; then its Restart.
        spec = chain.spec
        venv2 = chain.build([spec.install_step()])
        assert venv2.number == 2
        old = info.pid
        switch = subprocess.Popen(
            [sys.executable, "-m", "tgzr.edge.launcher", "--headless", "--home-dir", str(home_dir), "--org", ORG,
             "--client", "tgzr.edge.nice_client", "--switch-to", "2", "--wait-pid", str(old), "--ready-timeout", "120"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**{k: v for k, v in os.environ.items() if not k.startswith("TGZR_")}, "TGZR_CLIENT_NO_BROWSER": "1"},
        )
        time.sleep(1.0)
        assert switch.poll() is None  # waiting for the old client
        _stop(old)
        out, err = switch.communicate(timeout=300)
        assert switch.returncode == 0, err
        info2 = chain.read_instance()
        assert info2.n == 2 and info2.pid != old and chain.current_number() == 2
        pids.append(info2.pid)
        assert chain.names() == ["1", "2"]  # the previous working venv is kept
    finally:
        for pid in pids:
            if pid_alive(pid):
                _stop(pid)
