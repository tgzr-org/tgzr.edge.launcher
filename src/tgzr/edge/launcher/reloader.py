"""``--reload`` (development): run the real process as a child, and restart
it whenever a Python file it watches changes.

It watches the sources of the editable installs and the ``tgzr`` namespace
(:func:`watch_dirs`). The supervisor (the process started with ``--reload``)
picks what must outlive a restart, and hands it to each child in an
environment variable:

* the port, so an open page reconnects to the restarted child (NiceGUI then
  reloads the page);
* the guard's cookie secret, so that page is still let in;
* the link secret, so ``instance.json`` stays valid.

Only the first child opens the browser (or hands its link over). The child
reads the variable once and removes it from its environment, so the
processes it starts never take it for theirs.

The supervisor exits when the child exits on its own: 0 or a signal gives
0, anything else that code. A child that fails after a restart is a broken
edit: the supervisor waits for the next change instead. A copy of
``tgzr.edge.nice_client.reloader`` (with its own variable).
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

ENV_RELOAD = "TGZR_RELOAD_LAUNCHER"
EXIT_ALREADY_RUNNING = 3
STOP_TIMEOUT = 10.0  # seconds the child gets to stop before it is killed


@dataclass(frozen=True)
class ReloadChild:
    """What a child gets from its supervisor."""

    port: int
    cookie_secret: str
    link_secret: str
    run: int  # 1 for the first child, then one more per restart

    @property
    def first(self) -> bool:
        return self.run == 1


_child: ReloadChild | None = None
_read = False


def reload_child() -> ReloadChild | None:
    """This process's :class:`ReloadChild`, or ``None`` when no supervisor
    started it. The first call takes the variable out of the environment."""
    global _child, _read
    if not _read:
        raw = os.environ.pop(ENV_RELOAD, None)
        _child = ReloadChild(**json.loads(raw)) if raw else None
        _read = True
    return _child


def _editable_sources() -> list[Path]:
    """The ``src`` folder (else the project folder) of every editable install."""
    from importlib.metadata import distributions
    from urllib.parse import unquote, urlparse

    found = []
    for dist in distributions():
        try:
            info = json.loads(dist.read_text("direct_url.json") or "{}")
        except ValueError:
            continue
        url = urlparse(info.get("url", ""))
        if not info.get("dir_info", {}).get("editable") or url.scheme != "file":
            continue
        project = Path(unquote(url.path))
        found.append(project / "src" if (project / "src").is_dir() else project)
    return found


def watch_dirs() -> list[Path]:
    """The sources of the editable installs (an app being written with
    ``--editable``, the workspace's packages), and the folders of the
    ``tgzr`` namespace not inside one of them."""
    import tgzr

    dirs = [d.absolute() for d in [*_editable_sources(), *(Path(p) for p in tgzr.__path__)] if d.is_dir()]
    dirs = list(dict.fromkeys(dirs))
    return [d for d in dirs if not any(o != d and d.is_relative_to(o) for o in dirs)]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def supervise(
    cmd: Sequence[str],
    *,
    port: int = 0,
    name: str = "tgzr.edge.launcher",
    dirs: Sequence[Path] | None = None,
    env_var: str = ENV_RELOAD,
) -> int:
    """Run ``cmd`` until it exits on its own, restarting it on changes.
    Returns the exit code for this process."""
    from watchfiles import PythonFilter, watch

    dirs = list(dirs) if dirs is not None else watch_dirs()
    port = port or _free_port()
    cookie_secret, link_secret = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    stop = threading.Event()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: stop.set())

    def say(msg: str) -> None:
        print(f"{name}: reload: {msg}", file=sys.stderr, flush=True)

    run = 0

    def start() -> subprocess.Popen:
        nonlocal run
        run += 1
        child = ReloadChild(port=port, cookie_secret=cookie_secret, link_secret=link_secret, run=run)
        return subprocess.Popen(list(cmd), env={**os.environ, env_var: json.dumps(asdict(child))})

    say(f"watching {', '.join(str(d) for d in dirs)}")
    proc: subprocess.Popen | None = start()
    try:
        for changes in watch(*dirs, watch_filter=PythonFilter(), stop_event=stop, yield_on_timeout=True,
                             rust_timeout=500):
            if changes:
                say(f"{', '.join(sorted({Path(p).name for _, p in changes}))} changed: restarting")
                if proc is not None:
                    _stop(proc)
                proc = start()
                continue
            code = proc.poll() if proc is not None else None
            if code is None:
                continue
            if code > 0 and code != EXIT_ALREADY_RUNNING and run > 1:
                say(f"exited with code {code}: waiting for a change")
                proc = None
                continue
            return code if code > 0 else 0
    except KeyboardInterrupt:
        pass
    finally:
        if proc is not None:
            _stop(proc)
    return 0
