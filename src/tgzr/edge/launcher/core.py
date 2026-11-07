"""The launch flow (D51), shared by the headless command and the UI.

1. Find or create the org record and the client chain; save the client spec
   when flags change it.
2. Fail if the (org, client) instance lock is held.
3. Choose the venv: ``--switch-to N``, else the current one, else the newest
   working one, else build ``<1>`` (client spec only; the client adds the
   org's plugins later).
4. Start the client detached and wait for its ready signal. A venv that
   fails is renamed ``<n>_failing`` and the previous working one is started
   instead; if there is none, or it fails too, the launch fails.
5. On success: prune, record the launcher command in ``tgzr_home.json``,
   and return. The launcher never stays alive.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from tgzr.edge.home_manager import (
    ENV_CLIENT,
    ENV_HOME,
    ENV_LAUNCHER,
    ENV_ORG,
    BuildError,
    ClientChain,
    ClientSpec,
    Home,
    InstanceInfo,
    LaunchInProgress,
    NoSuchVenv,
    Venv,
    spawn_detached,
    wait_pid,
)

EXIT_FAILED = 1
EXIT_ALREADY_RUNNING = 3  # also what a client exits with when its instance lock is held
LAUNCH_LOCK_TIMEOUT = 900.0  # seconds: another launcher of the chain may be building its first venv
POLL = 0.2

Progress = Callable[[str], None]


class LaunchError(Exception):
    """The launch failed; ``log_path`` points at the client's log when there is one."""

    exit_code = EXIT_FAILED

    def __init__(self, message: str, log_path: Path | None = None) -> None:
        super().__init__(message)
        self.log_path = log_path


class ClientAlreadyRunning(LaunchError):
    exit_code = EXIT_ALREADY_RUNNING


@dataclass(frozen=True)
class SpecOverrides:
    """What the flags (or the form) say about the client spec; ``None`` and
    empty lists leave a saved spec unchanged."""

    package: str | None = None
    version: str | None = None
    extras: tuple[str, ...] = ()
    index_urls: tuple[str, ...] = ()
    find_links: tuple[str, ...] = ()
    editable: tuple[str, ...] = ()

    @property
    def any(self) -> bool:
        return bool(self.version is not None or self.extras or self.index_urls or self.find_links or self.editable)


@dataclass(frozen=True)
class LaunchResult:
    venv: Venv
    pid: int
    exited: bool  # the client finished (exit code 0) before reporting ready
    fell_back: bool  # a failing venv was replaced by the previous working one
    url: str | None = None  # the client's one-time link, when it did not open a browser itself


def client_env(*, native: bool = True, hand_over_link: bool = False) -> dict[str, str]:
    """Environment variables for the client: its window mode (both
    variables, so they also override a ``.env`` saying the opposite), and,
    for a web client, its one-time link in the ready signal instead of a new
    browser tab (``hand_over_link``)."""
    env = {"TGZR_CLIENT_NATIVE": "1" if native else "0", "TGZR_CLIENT_WEB": "0" if native else "1"}
    if hand_over_link and not native:
        env["TGZR_CLIENT_NO_BROWSER"] = "1"
    return env


def launcher_command() -> list[str]:
    """How a client can run this launcher again (``TGZR_LAUNCHER``)."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "tgzr.edge.launcher"]


def client_module(package: str) -> str:
    """The module run with ``python -m``: the distribution name with ``-`` as ``_``."""
    return package.replace("-", "_")


def _absolute(paths: Sequence[str]) -> tuple[str, ...]:
    return tuple(str(Path(p).expanduser().absolute()) for p in paths)


def select_chain(home: Home, org_url: str, client: str | None, overrides: SpecOverrides) -> ClientChain:
    """Find or create the org record and the client chain, and save the spec."""
    org = home.add_org(org_url)
    if client is None:
        chains = [c for c in org.clients() if c.spec is not None]
        if len(chains) != 1:
            known = ", ".join(c.slug for c in chains) or "none"
            raise LaunchError(f"which client for {org.url}? give --client (known: {known})")
        chain = chains[0]
    else:
        chain = org.client(client)
    spec = chain.spec
    editable = _absolute(overrides.editable)
    find_links = tuple(fl if "://" in fl else str(Path(fl).expanduser().absolute()) for fl in overrides.find_links)
    if spec is None:
        if client is None:
            raise LaunchError("give --client to launch a new client")
        spec = ClientSpec(package=client, version=overrides.version or "", extras=overrides.extras,
                          index_urls=overrides.index_urls, find_links=find_links, editable=editable)
        chain.save_spec(spec)
    elif overrides.any:
        chain.save_spec(spec.merged(version=overrides.version, extras=overrides.extras or None,
                                    index_urls=overrides.index_urls or None, find_links=find_links or None,
                                    editable=editable or None))
    return chain


def _start(chain: ClientChain, venv: Venv, home: Home, client_env: Mapping[str, str]) -> subprocess.Popen:
    spec = chain.spec
    assert spec is not None
    chain.clear_instance()
    env = {**client_env, ENV_HOME: str(home.path), ENV_ORG: chain.org.url, ENV_CLIENT: chain.slug,
           ENV_LAUNCHER: json.dumps(launcher_command())}
    return spawn_detached([str(venv.python), "-m", client_module(spec.package)],
                          log_path=chain.logs_dir / f"client-{venv.number}.log", env=env, cwd=chain.path)


def wait_ready(chain: ClientChain, proc: subprocess.Popen, venv: Venv, timeout: float) -> InstanceInfo | None:
    """The ready signal, or ``None`` if the client exited 0 before that.
    Raises :class:`ClientAlreadyRunning` (exit code 3) or :class:`LaunchError`."""
    log = chain.logs_dir / f"client-{venv.number}.log"
    deadline = time.monotonic() + timeout
    while True:
        info = chain.read_instance()
        # instance.json was cleared before the start: a ready one is our client's.
        if info is not None and info.state == "ready" and (info.pid == proc.pid or info.n == venv.number):
            return info
        code = proc.poll()
        if code is not None:
            if code == 0:
                return None
            if code == EXIT_ALREADY_RUNNING:
                raise ClientAlreadyRunning(f"{chain.slug} is already running for {chain.org.url}", log)
            raise LaunchError(f"the client in venv {venv.number} exited with code {code} before it was ready", log)
        if time.monotonic() > deadline:
            proc.kill()
            proc.wait(10)
            raise LaunchError(f"the client in venv {venv.number} was not ready within {timeout:.0f}s; killed", log)
        time.sleep(POLL)


def _choose(chain: ClientChain, switch_to: int | None, progress: Progress) -> Venv:
    if switch_to is not None:
        try:
            return chain.switch(switch_to)
        except NoSuchVenv as e:
            raise LaunchError(str(e)) from e
    current = chain.current()
    if current is not None:
        return current
    previous = chain.previous_working(before=None)
    if previous is not None:
        return chain.switch(previous.number)
    spec = chain.spec
    assert spec is not None
    progress(f"building venv {chain.next_number()} for {spec.package} (this takes a while the first time)")
    try:
        venv = chain.build([spec.install_step()])
    except BuildError as e:
        raise LaunchError(f"could not build the client venv: {e}") from e
    return chain.switch(venv.number)


def launch(
    home: Home,
    chain: ClientChain,
    *,
    switch_to: int | None = None,
    ready_timeout: float = 60.0,
    progress: Progress = lambda msg: None,
    client_env: Mapping[str, str] | None = None,
    lock_timeout: float = LAUNCH_LOCK_TIMEOUT,
) -> LaunchResult:
    """``client_env``: more environment variables for the client, e.g.
    ``TGZR_CLIENT_NO_BROWSER=1`` to get its link in the result instead of a
    new browser tab.

    Everything from the "already running?" check to the ready signal (or the
    failure) happens under the chain's ``launch.lock``: another launcher of
    the same chain waits up to ``lock_timeout`` seconds, then usually finds
    the client running."""
    try:
        lock = chain.launch_lock()
    except LaunchInProgress:
        progress(f"waiting for another launch of {chain.slug} for {chain.org.url}")
        try:
            lock = chain.launch_lock(timeout=lock_timeout)
        except LaunchInProgress as e:
            raise LaunchError(f"{e} (waited {lock_timeout:.0f}s)") from e
    with lock:
        return _launch_locked(home, chain, switch_to, ready_timeout, progress, client_env or {})


def _launch_locked(
    home: Home,
    chain: ClientChain,
    switch_to: int | None,
    ready_timeout: float,
    progress: Progress,
    client_env: Mapping[str, str],
) -> LaunchResult:
    if chain.is_running():
        raise ClientAlreadyRunning(f"{chain.slug} is already running for {chain.org.url}")
    venv = _choose(chain, switch_to, progress)
    fell_back = False
    while True:
        progress(f"starting {chain.slug} from venv {venv.number}")
        proc = _start(chain, venv, home, client_env)
        try:
            ready = wait_ready(chain, proc, venv, ready_timeout)
            break
        except ClientAlreadyRunning:
            raise
        except LaunchError as e:
            if fell_back:
                raise LaunchError(f"{e}; the previous venv failed too", e.log_path) from e
            chain.mark_failing(venv.number)
            previous = chain.previous_working(before=venv.number)
            progress(f"{e}: renamed it {venv.number}_failing")
            if previous is None:
                raise LaunchError(f"{e}; there is no previous working venv to go back to", e.log_path) from e
            venv = chain.switch(previous.number)
            fell_back = True
    chain.prune()
    home.launcher_command = launcher_command()
    chain.org.touch()
    progress(f"{chain.slug} is {'ready' if ready else 'done'} (venv {venv.number})")
    return LaunchResult(venv, proc.pid, exited=ready is None, fell_back=fell_back,
                        url=ready.url if ready is not None else None)


def wait_for(pid: int, timeout: float, progress: Progress = lambda msg: None) -> None:
    progress(f"waiting for process {pid} to exit")
    if not wait_pid(pid, timeout):
        raise LaunchError(f"process {pid} is still running after {timeout:.0f}s")


__all__ = [
    "ClientAlreadyRunning",
    "LaunchError",
    "LaunchResult",
    "SpecOverrides",
    "launch",
    "launcher_command",
    "select_chain",
    "wait_for",
]
